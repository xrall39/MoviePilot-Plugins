import datetime
import re
import threading
import traceback
from pathlib import Path
from typing import List, Tuple, Dict, Any, Optional

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from app import schemas
from app.chain.tmdb import TmdbChain
from app.chain.transfer import TransferChain
from app.core.config import settings
from app.core.event import eventmanager, Event
from app.core.metainfo import MetaInfoPath
from app.db.transferhistory_oper import TransferHistoryOper
from app.log import logger
from app.plugins import _PluginBase
from app.core.context import MediaInfo
from app.schemas import Notification, NotificationType, TransferInfo
from app.schemas.types import EventType, MediaType
from app.utils.string import StringUtils
from app.utils.system import SystemUtils

lock = threading.Lock()


class FileMonitorHandler(FileSystemEventHandler):
    """
    目录监控响应类
    """

    def __init__(self, monpath: str, sync: Any, **kwargs):
        super(FileMonitorHandler, self).__init__(**kwargs)
        self._watch_path = monpath
        self.sync = sync

    def on_created(self, event):
        self.sync.event_handler(event=event, text="创建",
                                mon_path=self._watch_path, event_path=event.src_path)

    def on_moved(self, event):
        self.sync.event_handler(event=event, text="移动",
                                mon_path=self._watch_path, event_path=event.dest_path)


class CloudLinkMonitor(_PluginBase):
    # 插件名称
    plugin_name = "云盘实时链接"
    # 插件描述
    plugin_desc = "监控云盘目录文件变化，自动转移链接（不刮削）。"
    # 插件图标
    plugin_icon = "Linkease_A.png"
    # 插件版本
    plugin_version = "1.1"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "cloudlinkmonitor_"
    # 加载顺序
    plugin_order = 5
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _scheduler = None
    _observer = []
    _enabled = False
    _notify = False
    _onlyonce = False
    tmdbchain = None
    transferhis = None
    transferchian = None
    _cron = None
    _size = 0
    # 转移方式
    _monitor_dirs = ""
    _exclude_keywords = ""
    _cloud_medias = {}

    # 模式 compatibility/fast
    _mode = "compatibility"
    _transfer_type = "link"
    # 存储源目录与目的目录关系
    _dirconf: Dict[str, Optional[Path]] = {}
    # 存储源目录转移方式
    _transferconf: Dict[str, Optional[str]] = {}
    # 退出事件
    _event = threading.Event()

    def init_plugin(self, config: dict = None):
        # 清空配置
        self._dirconf = {}
        self._transferconf = {}
        self.tmdbchain = TmdbChain()
        self.transferhis = TransferHistoryOper()
        self.transferchian = TransferChain()

        # 读取配置
        if config:
            self._enabled = config.get("enabled")
            self._notify = config.get("notify")
            self._onlyonce = config.get("onlyonce")
            self._mode = config.get("mode")
            self._transfer_type = config.get("transfer_type")
            self._monitor_dirs = config.get("monitor_dirs") or ""
            self._exclude_keywords = config.get("exclude_keywords") or ""
            self._cron = config.get("cron")
            self._size = config.get("size") or 0

        # 停止现有任务
        self.stop_service()

        if self._enabled or self._onlyonce:
            # 定时服务管理器
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            # 追加入库消息统一发送服务
            self._scheduler.add_job(self.send_msg, trigger='interval', seconds=15)

            # 读取目录配置
            monitor_dirs = self._monitor_dirs.split("\n")
            if not monitor_dirs:
                return
            for mon_path in monitor_dirs:
                # 格式源目录:目的目录
                if not mon_path:
                    continue

                # 存储目的目录
                if SystemUtils.is_windows():
                    if mon_path.count(":") > 1:
                        paths = [mon_path.split(":")[0] + ":" + mon_path.split(":")[1],
                                 mon_path.split(":")[2] + ":" + mon_path.split(":")[3]]
                    else:
                        paths = [mon_path]
                else:
                    paths = mon_path.split(":")

                # 目的目录
                if len(paths) > 1:
                    mon_path = paths[0]
                    target_path = Path(paths[1])
                    self._dirconf[mon_path] = target_path
                else:
                    logger.warn(f"{mon_path} 未配置目的目录，将不会进行连接")
                    self.systemmessage.put(f"{mon_path} 未配置目的目录，将不会进行连接！")
                    continue

                # 启用目录监控
                if self._enabled:
                    # 检查媒体库目录是不是下载目录的子目录
                    try:
                        if target_path and target_path.is_relative_to(Path(mon_path)):
                            logger.warn(f"{target_path} 是监控目录 {mon_path} 的子目录，无法监控")
                            self.systemmessage.put(f"{target_path} 是下载目录 {mon_path} 的子目录，无法监控")
                            continue
                    except Exception as e:
                        logger.debug(str(e))
                        pass

                    try:
                        if self._mode == "compatibility":
                            # 兼容模式，目录同步性能降低且NAS不能休眠，但可以兼容挂载的远程共享目录如SMB
                            observer = PollingObserver(timeout=10)
                        else:
                            # 内部处理系统操作类型选择最优解
                            observer = Observer(timeout=10)
                        self._observer.append(observer)
                        observer.schedule(FileMonitorHandler(mon_path, self), path=mon_path, recursive=True)
                        observer.daemon = True
                        observer.start()
                        logger.info(f"{mon_path} 的目录监控服务启动")
                    except Exception as e:
                        err_msg = str(e)
                        if "inotify" in err_msg and "reached" in err_msg:
                            logger.warn(
                                f"目录监控服务启动出现异常：{err_msg}，请在宿主机上（不是docker容器内）执行以下命令并重启："
                                + """
                                     echo fs.inotify.max_user_watches=524288 | sudo tee -a /etc/sysctl.conf
                                     echo fs.inotify.max_user_instances=524288 | sudo tee -a /etc/sysctl.conf
                                     sudo sysctl -p
                                     """)
                        else:
                            logger.error(f"{mon_path} 启动目录监控失败：{err_msg}")
                        self.systemmessage.put(f"{mon_path} 启动目录监控失败：{err_msg}")

            # 运行一次定时服务
            if self._onlyonce:
                logger.info("目录监控服务启动，立即运行一次")
                self._scheduler.add_job(func=self.sync_all, trigger='date',
                                        run_date=datetime.datetime.now(
                                            tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3)
                                        )
                # 关闭一次性开关
                self._onlyonce = False
                # 保存配置
                self.__update_config()

            # 全量同步定时
            if self._enabled and self._cron:
                try:
                    self._scheduler.add_job(func=self.sync_all,
                                            trigger=CronTrigger.from_crontab(self._cron),
                                            name="云盘实时链接")
                except Exception as err:
                    logger.error(f"定时任务配置错误：{str(err)}")
                    # 推送实时消息
                    self.systemmessage.put(f"执行周期配置错误：{str(err)}")

            # 启动定时服务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def __update_config(self):
        """
        更新配置
        """
        self.update_config({
            "enabled": self._enabled,
            "notify": self._notify,
            "onlyonce": self._onlyonce,
            "mode": self._mode,
            "transfer_type": self._transfer_type,
            "monitor_dirs": self._monitor_dirs,
            "exclude_keywords": self._exclude_keywords,
            "cron": self._cron,
            "size": self._size
        })

    @eventmanager.register(EventType.PluginAction)
    def remote_sync(self, event: Event):
        """
        远程全量同步
        """
        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "realtime_link":
                return
            self.post_message(channel=event.event_data.get("channel"),
                              title="开始实时连接 ...",
                              userid=event.event_data.get("user"))
        self.sync_all()
        if event:
            self.post_message(channel=event.event_data.get("channel"),
                              title="实时连接完成！", userid=event.event_data.get("user"))

    def sync_all(self):
        """
        立即运行一次，全量同步目录中所有文件
        """
        logger.info("开始全量实时连接 ...")
        # 遍历所有监控目录
        for mon_path in self._dirconf.keys():
            # 遍历目录下所有文件
            for file_path in SystemUtils.list_files(Path(mon_path), ['.*']):
                self.__handle_file(event_path=str(file_path), mon_path=mon_path)
        logger.info("全量实时连接完成！")

    def event_handler(self, event, mon_path: str, text: str, event_path: str):
        """
        处理文件变化
        :param event: 事件
        :param mon_path: 监控目录
        :param text: 事件描述
        :param event_path: 事件文件路径
        """
        if not event.is_directory:
            # 文件发生变化
            logger.debug("文件%s：%s" % (text, event_path))
            self.__handle_file(event_path=event_path, mon_path=mon_path)

    def _link_file(self, src_path: Path, mon_path: str,
                   target_path: Path, transfer_type: str = "softlink") -> Tuple[bool, str]:
        """
        对文件做纯链接处理，不做识别重命名，则监控模块调用
        :param : 来源渠道
        :param src_path: 源文件
        :param target_path: 目标目录
        :param transfer_type: 转移方式
        """
        new_file = str(src_path).replace(mon_path, str(target_path))
        new_path = Path(new_file)
        if new_path.exists():
            return True, "目标路径文件已存在"
        else:
            # 创建目标目录
            if not new_path.parent.exists():
                new_path.parent.mkdir(parents=True, exist_ok=True)
            # 转移
            if transfer_type == "copy":
                code, errmsg = SystemUtils.copy(src_path, new_path)
                return True if code == 0 else False, errmsg
            else:
                # 元数据
                file_meta = MetaInfoPath(src_path)
                # 识别媒体信息
                mediainfo: MediaInfo = self.chain.recognize_media(meta=file_meta)
                # 获取集数据
                if mediainfo.type == MediaType.TV:
                    episodes_info = self.tmdbchain.tmdb_episodes(tmdbid=mediainfo.tmdb_id,
                                                                 season=file_meta.begin_season or 1)
                else:
                    episodes_info = None
                # 转移
                transferinfo: TransferInfo = self.chain.transfer(mediainfo=mediainfo,
                                                                 path=src_path,
                                                                 transfer_type=transfer_type,
                                                                 target=target_path,
                                                                 meta=file_meta,
                                                                 episodes_info=episodes_info)
                if not transferinfo:
                    logger.error("文件转移模块运行失败")
                    return False, "文件转移模块运行失败"
                if not transferinfo.success:
                    if not transferinfo.success:
                        # 转移失败
                        logger.warn(f"{src_path.name} 入库失败：{transferinfo.message}")
                        # 新增转移失败历史记录
                        self.transferhis.add_fail(
                            src_path=src_path,
                            mode=transfer_type,
                            meta=file_meta,
                            mediainfo=mediainfo,
                            transferinfo=transferinfo
                        )
                        if self._notify:
                            self.chain.post_message(Notification(
                                mtype=NotificationType.Manual,
                                title=f"{mediainfo.title_year}{file_meta.season_episode} 入库失败！",
                                text=f"原因：{transferinfo.message or '未知'}",
                                image=mediainfo.get_message_image()
                            ))
                        return False, "文件转移模块运行失败"

                        # 新增转移成功历史记录
                    self.transferhis.add_success(
                        src_path=src_path,
                        mode=transfer_type,
                        meta=file_meta,
                        mediainfo=mediainfo,
                        transferinfo=transferinfo
                    )

                    # 发送消息汇总
                    media_list = self._cloud_medias.get(mediainfo.title_year + " " + file_meta.season) or {}
                    if media_list:
                        media_files = media_list.get("files") or []
                        if media_files:
                            file_exists = False
                            for file in media_files:
                                if str(src_path) == file.get("path"):
                                    file_exists = True
                                    break
                            if not file_exists:
                                media_files.append({
                                    "path": src_path,
                                    "mediainfo": mediainfo,
                                    "file_meta": file_meta,
                                    "transferinfo": transferinfo
                                })
                        else:
                            media_files = [
                                {
                                    "path": src_path,
                                    "mediainfo": mediainfo,
                                    "file_meta": file_meta,
                                    "transferinfo": transferinfo
                                }
                            ]
                        media_list = {
                            "files": media_files,
                            "time": datetime.datetime.now()
                        }
                    else:
                        media_list = {
                            "files": [
                                {
                                    "path": src_path,
                                    "mediainfo": mediainfo,
                                    "file_meta": file_meta,
                                    "transferinfo": transferinfo
                                }
                            ],
                            "time": datetime.datetime.now()
                        }
                    self._cloud_medias[mediainfo.title_year + " " + file_meta.season] = media_list

                    # 广播事件
                    self.eventmanager.send_event(EventType.TransferComplete, {
                        'meta': file_meta,
                        'mediainfo': mediainfo,
                        'transferinfo': transferinfo
                    })

                    return True, "文件转移模块运行成功"

    def __handle_file(self, event_path: str, mon_path: str):
        """
        同步一个文件
        :param event_path: 事件文件路径
        :param mon_path: 监控目录
        """
        file_path = Path(event_path)
        try:
            if not file_path.exists():
                return
            # 全程加锁
            with lock:

                # 回收站及隐藏的文件不处理
                if event_path.find('/@Recycle/') != -1 \
                        or event_path.find('/#recycle/') != -1 \
                        or event_path.find('/.') != -1 \
                        or event_path.find('/@eaDir') != -1:
                    logger.debug(f"{event_path} 是回收站或隐藏的文件")
                    return

                # 命中过滤关键字不处理
                if self._exclude_keywords:
                    for keyword in self._exclude_keywords.split("\n"):
                        if keyword and re.findall(keyword, event_path):
                            logger.info(f"{event_path} 命中过滤关键字 {keyword}，不处理")
                            return

                # 判断文件大小
                if self._size and float(self._size) > 0 and file_path.stat().st_size < float(self._size) * 1024:
                    logger.info(f"{event_path} 文件大小小于最小文件大小，复制...")
                    _transfer_type = self._transfer_type
                else:
                    _transfer_type = "copy"

                # 查询转移目的目录
                target: Path = self._dirconf.get(mon_path)
                if not target:
                    logger.warn(f"{mon_path} 未配置目的目录，将不会进行软连接")
                    return

                # 开始连接
                self._link_file(src_path=file_path, mon_path=mon_path,
                                target_path=target, transfer_type=_transfer_type)

        except Exception as e:
            logger.error("目录监控发生错误：%s - %s" % (str(e), traceback.format_exc()))

    def get_state(self) -> bool:
        return self._enabled

    def send_msg(self):
        """
        定时检查是否有媒体处理完，发送统一消息
        """
        if not self._cloud_medias or not self._cloud_medias.keys():
            return

        # 遍历检查是否已刮削完，发送消息
        for medis_title_year_season in list(self._cloud_medias.keys()):
            media_list = self._cloud_medias.get(medis_title_year_season)
            logger.info(f"开始处理媒体 {medis_title_year_season} 消息")

            if not media_list:
                continue

            # 获取最后更新时间
            last_update_time = media_list.get("time")
            media_files = media_list.get("files")
            if not last_update_time or not media_files:
                continue

            transferinfo = media_files[0].get("transferinfo")
            file_meta = media_files[0].get("file_meta")
            mediainfo = media_files[0].get("mediainfo")
            # 判断剧集最后更新时间距现在是已超过10秒或者电影，发送消息
            if (datetime.datetime.now() - last_update_time).total_seconds() > int(10) \
                    or mediainfo.type == MediaType.MOVIE:
                # 发送通知
                if self._notify:
                    # 汇总处理文件总大小
                    total_size = 0
                    file_count = 0

                    # 剧集汇总
                    episodes = []
                    for file in media_files:
                        transferinfo = file.get("transferinfo")
                        total_size += transferinfo.total_size
                        file_count += 1

                        file_meta = file.get("file_meta")
                        if file_meta and file_meta.begin_episode:
                            episodes.append(file_meta.begin_episode)

                    transferinfo.total_size = total_size
                    # 汇总处理文件数量
                    transferinfo.file_count = file_count

                    # 剧集季集信息 S01 E01-E04 || S01 E01、E02、E04
                    season_episode = None
                    # 处理文件多，说明是剧集，显示季入库消息
                    if mediainfo.type == MediaType.TV:
                        # 季集文本
                        season_episode = f"{file_meta.season} {StringUtils.format_ep(episodes)}"
                    # 发送消息
                    self.transferchian.send_transfer_message(meta=file_meta,
                                                             mediainfo=mediainfo,
                                                             transferinfo=transferinfo,
                                                             season_episode=season_episode)
                # 发送完消息，移出key
                del self._cloud_medias[medis_title_year_season]
                continue

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        定义远程控制命令
        :return: 命令关键字、事件、描述、附带数据
        """
        return [{
            "cmd": "/realtime_link",
            "event": EventType.PluginAction,
            "desc": "实时软连接",
            "category": "管理",
            "data": {
                "action": "realtime_link"
            }
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        return [{
            "path": "/realtime_link",
            "endpoint": self.sync,
            "methods": ["GET"],
            "summary": "实时软连接",
            "description": "实时软连接",
        }]

    def sync(self) -> schemas.Response:
        """
        API调用目录同步
        """
        self.sync_all()
        return schemas.Response(success=True)

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'notify',
                                            'label': '发送通知',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'mode',
                                            'label': '监控模式',
                                            'items': [
                                                {'title': '兼容模式', 'value': 'compatibility'},
                                                {'title': '性能模式', 'value': 'fast'}
                                            ]
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'transfer_type',
                                            'label': '转移方式',
                                            'items': [
                                                {'title': '移动', 'value': 'move'},
                                                {'title': '复制', 'value': 'copy'},
                                                {'title': '硬链接', 'value': 'link'},
                                                {'title': '软链接', 'value': 'softlink'},
                                                {'title': 'Rclone复制', 'value': 'rclone_copy'},
                                                {'title': 'Rclone移动', 'value': 'rclone_move'}
                                            ]
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '定时全量同步周期',
                                            'placeholder': '5位cron表达式，留空关闭'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'size',
                                            'label': '最小文件大小（KB）',
                                            'placeholder': ''
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'monitor_dirs',
                                            'label': '监控目录',
                                            'rows': 5,
                                            'placeholder': '每一行一个目录，支持以下几种配置方式：\n'
                                                           '监控目录\n'
                                                           '监控目录:转移目的目录\n'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'exclude_keywords',
                                            'label': '排除关键词',
                                            'rows': 2,
                                            'placeholder': '每一行一个关键词'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '最小文件大小：小于最小文件大小的文件将直接复制，其余则软链接。'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "notify": False,
            "onlyonce": False,
            "mode": "compatibility",
            "transfer_type": "link",
            "monitor_dirs": "",
            "exclude_keywords": "",
            "cron": "",
            "size": ""
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        if self._observer:
            for observer in self._observer:
                try:
                    observer.stop()
                    observer.join()
                except Exception as e:
                    print(str(e))
        self._observer = []
        if self._scheduler:
            self._scheduler.remove_all_jobs()
            if self._scheduler.running:
                self._event.set()
                self._scheduler.shutdown()
                self._event.clear()
            self._scheduler = None

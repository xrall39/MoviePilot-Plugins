import re
from typing import Tuple
from urllib.parse import urljoin

from lxml import etree

from app.core.config import settings
from app.log import logger
from app.plugins.autosignin.sites import _ISiteSigninHandler
from app.utils.http import RequestUtils
from app.utils.string import StringUtils


class Pt52(_ISiteSigninHandler):
    """
    52pt
    站点已改为滑块验证签到，滑块验证值由页面脚本生成
    """
    # 匹配的站点Url，每一个实现类都需要设置为自己的站点Url
    site_url = "52pt.site"

    # 已签到
    _sign_regex = ['今天已经签过到了']

    # 签到成功
    _success_regex = ['(?:签到成功|连续签到\\s*\\d+\\s*天)，获得\\s*\\d+\\s*魔力值', '\\d+点魔力值']

    # 滑块完成后页面脚本写入 sign_captcha
    _captcha_regex = re.compile(r"captchaInput\.value\s*=\s*['\"]([^'\"]+)['\"]")

    @classmethod
    def match(cls, url: str) -> bool:
        """
        根据站点Url判断是否匹配当前站点签到类，大部分情况使用默认实现即可
        :param url: 站点Url
        :return: 是否匹配，如匹配则会调用该类的signin方法
        """
        return True if StringUtils.url_equal(url, cls.site_url) else False

    def signin(self, site_info: dict) -> Tuple[bool, str]:
        """
        执行签到操作
        :param site_info: 站点信息，含有站点Url、站点Cookie、UA等信息
        :return: 签到结果信息
        """
        site = site_info.get("name")
        site_cookie = site_info.get("cookie")
        ua = site_info.get("ua") or settings.NORMAL_USER_AGENT
        render = site_info.get("render")
        proxy = site_info.get("proxy")

        # bakatest.php 仍是签到入口，但表单会指向站点当前使用的签到页面
        entry_url = 'https://52pt.site/bakatest.php'
        entry_html = self.get_page_source(url=entry_url,
                                          cookie=site_cookie,
                                          ua=ua,
                                          proxy=proxy,
                                          render=render)
        
        if not entry_html:
            logger.error(f"{site} 签到失败，请检查站点连通性")
            return False, '签到失败，请检查站点连通性'

        if "login.php" in entry_html:
            logger.error(f"{site} 签到失败，Cookie已失效")
            return False, '签到失败，Cookie已失效'

        sign_status = self.sign_in_result(html_res=entry_html,
                                          regexs=self._sign_regex)
        if sign_status:
            logger.info(f"今日已签到")
            return True, '今日已签到'

        entry_doc = etree.HTML(entry_html)
        if entry_doc is None:
            return False, '签到失败'

        sign_link = entry_doc.xpath("//a[@id='game' or contains(., '签到赚魔力')]/@href")
        if not sign_link:
            logger.error(f"{site} 签到失败，未获取到签到地址")
            return False, f"【{site}】签到失败，未获取到签到地址"
        sign_url = urljoin(entry_url, sign_link[0])
        if "bakatest" not in sign_url:
            logger.info(f"今日已签到")
            return True, '今日已签到'

        # sign_token 与真实签到页绑定，需要带首页 Referer 重新获取签到页表单
        headers = {
            'User-Agent': ua,
            'Cookie': site_cookie,
            'Referer': 'https://52pt.site/index.php'
        }
        sign_page_res = RequestUtils(headers=headers,
                                     proxies=settings.PROXY if proxy else None).get_res(url=sign_url)
        html_text = sign_page_res.text if sign_page_res is not None else ''
        if not html_text:
            logger.error(f"{site} 签到失败，请检查站点连通性")
            return False, '签到失败，请检查站点连通性'

        if "login.php" in html_text:
            logger.error(f"{site} 签到失败，Cookie已失效")
            return False, '签到失败，Cookie已失效'

        sign_status = self.sign_in_result(html_res=html_text,
                                          regexs=self._sign_regex)
        if sign_status:
            logger.info(f"今日已签到")
            return True, '今日已签到'

        # 解析滑块表单
        html = etree.HTML(html_text)

        if html is None:
            return False, '签到失败'

        sign_form = html.xpath("//form[.//input[@name='sign_submit']]")
        if not sign_form:
            logger.error(f"{site} 签到失败，未获取到签到表单")
            return False, f"【{site}】签到失败，未获取到签到表单"

        form = sign_form[0]
        action = form.xpath("./@action")
        if action:
            sign_url = urljoin(sign_url, action[0])
        token = form.xpath(".//input[@name='sign_token']/@value")
        captcha = self._captcha_regex.search(html_text)

        if not token or not captcha:
            logger.error(f"{site} 签到失败，未获取到签到参数")
            return False, f"【{site}】签到失败，未获取到签到参数"

        return self.__signin(sign_url=sign_url,
                             sign_token=token[0],
                             sign_captcha=captcha.group(1),
                             site_cookie=site_cookie,
                             ua=ua,
                             proxy=proxy,
                             site=site)

    def __signin(self, sign_url: str,
                 sign_token: str,
                 sign_captcha: str,
                 site: str,
                 site_cookie: str,
                 ua: str,
                 proxy: bool) -> Tuple[bool, str]:
        """
        签到请求
        sign_captcha: 滑块验证值
        sign_token: 页面令牌
        sign_submit: 固定为1
        """
        data = {
            'sign_captcha': sign_captcha,
            'sign_token': sign_token,
            'sign_submit': '1'
        }
        logger.debug(f"签到请求参数 {data}")

        sign_res = RequestUtils(cookies=site_cookie,
                                ua=ua,
                                referer=sign_url,
                                proxies=settings.PROXY if proxy else None
                                ).post_res(url=sign_url, data=data)
        if not sign_res or sign_res.status_code != 200:
            logger.error(f"{site} 签到失败，签到接口请求失败")
            return False, '签到失败，签到接口请求失败'

        if "login.php" in sign_res.text:
            logger.error(f"{site} 签到失败，Cookie已失效")
            return False, '签到失败，Cookie已失效'

        # 判断是否签到成功
        sign_status = self.sign_in_result(html_res=sign_res.text,
                                          regexs=self._success_regex)
        if sign_status:
            logger.info(f"{site} 签到成功")
            return True, '签到成功'
        else:
            sign_status = self.sign_in_result(html_res=sign_res.text,
                                              regexs=self._sign_regex)
            if sign_status:
                logger.info(f"{site} 今日已签到")
                return True, '今日已签到'

            logger.error(f"{site} 签到失败，请到页面查看")
            return False, '签到失败，请到页面查看'

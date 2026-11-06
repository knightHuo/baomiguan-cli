"""Small, bounded client for the official Baomiguan endpoints.

Login request formats originate from NB-XX/AutoBaomiGuan (MIT).
"""

import base64
import json as json_module
import time
from urllib.parse import urlsplit

import requests


class ApiError(Exception):
    """An upstream request could not be completed or understood."""


class BaomiClient:
    BASE_URL = "https://www.baomi.org.cn"
    TIMEOUT = (5, 20)
    SITE_ID = "95"
    QR_TOKEN_PATH = "/portal/main-api/v2/spc/getQrToken.do"
    QR_CHECK_PATH = "/portal/api/v2/spc/checkQrToken.do"
    # Some official endpoints mutate data with GET. Only known reads may retry.
    READ_PATHS = frozenset({
        "/portal/main-api/checkToken.do",
        "/portal/main-api/getPublishKey.do",
        "/portal/main-api/spc/cms/publish/listByCat.do",
        "/portal/main-api/v2/coursePacket/getCourseDirectoryList",
        "/portal/main-api/v2/coursePacket/getCoursePacket",
        "/portal/main-api/v2/coursePacket/getCourseResourceList",
        "/portal/main-api/v2/coursePacket/getResourceUserStatistic",
        "/portal/main-api/v2/coursePacket/getCourseUserStatistic",
        "/portal/main-api/v2/coursePacket/getCourseRelateExam",
        "/portal/main-api/v2/activity/exam/getExamResultMaxScore.do",
    })

    def __init__(self, token="", session=None):
        self.token = token or ""
        self.session = session if session is not None else requests.Session()

    def set_token(self, token):
        self.token = token or ""

    def _url(self, path):
        if not isinstance(path, str):
            raise ApiError("接口地址格式错误")
        url = self.BASE_URL + path if path.startswith("/") else path
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.netloc != "www.baomi.org.cn"
                or parsed.username or parsed.password or parsed.fragment):
            raise ApiError("只允许请求保密观官方 HTTPS 接口")
        return url, parsed.path

    def _request(self, method, path, params=None, json=None):
        url, endpoint = self._url(path)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) BaomiguanCLI/2.0",
            "Content-Type": "application/json",
            "siteId": self.SITE_ID,
        }
        if self.token:
            headers.update(token=self.token, authToken=self.token)
        attempts = 2 if method == "GET" and endpoint in self.READ_PATHS else 1
        for attempt in range(attempts):
            try:
                response = self.session.request(
                    method, url, params=params, json=json, headers=headers,
                    timeout=self.TIMEOUT, allow_redirects=False,
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt + 1 < attempts:
                    time.sleep(0.4)
                    continue
                # Request exception strings can contain token-bearing query URLs.
                raise ApiError("连接保密观超时或网络不可用，请稍后重试") from exc
            except requests.RequestException as exc:
                raise ApiError("保密观请求失败，请检查网络后重试") from exc

            if response.status_code in (429, 502, 503, 504) and attempt + 1 < attempts:
                time.sleep(0.4)
                continue
            if not 200 <= response.status_code < 300:
                raise ApiError(f"保密观接口返回 HTTP {response.status_code}")
            try:
                data = response.json()
            except (ValueError, TypeError) as exc:
                raise ApiError("保密观接口未返回有效 JSON，可能正在维护或已改版") from exc
            if not isinstance(data, dict):
                raise ApiError("保密观接口返回结构变化，需要更新适配")
            return data
        raise ApiError("保密观请求未完成")

    def get(self, path, params=None):
        return self._request("GET", path, params=params)

    def post(self, path, json=None):
        return self._request("POST", path, json=json)

    def check_login(self):
        if not self.token:
            return {"loggedIn": False, "nickname": ""}
        response = self.get("/portal/main-api/checkToken.do")
        result = response.get("result")
        valid = result is True or result == 1 or result == "1"
        data = response.get("data")
        if not valid or not isinstance(data, dict):
            return {"loggedIn": False, "nickname": ""}
        return {"loggedIn": True, "nickname": data.get("nickName") or "未设定姓名"}

    def login_password(self, username, password):
        if not isinstance(username, str) or not username.strip() or not password:
            raise ApiError("请输入账号和密码")
        from Crypto.Cipher import PKCS1_v1_5
        from Crypto.PublicKey import RSA

        public_key = self.get("/portal/main-api/getPublishKey.do").get("data")
        if not isinstance(public_key, str) or not public_key.strip():
            raise ApiError("未能获取登录公钥")
        if not public_key.strip().startswith("-----BEGIN"):
            public_key = "-----BEGIN PUBLIC KEY-----\n" + public_key.strip() + "\n-----END PUBLIC KEY-----"
        try:
            cipher = PKCS1_v1_5.new(RSA.import_key(public_key))
            encrypted_user = base64.b64encode(cipher.encrypt(username.strip().encode("utf-8"))).decode("ascii")
            encrypted_password = base64.b64encode(cipher.encrypt(password.encode("utf-8"))).decode("ascii")
        except (TypeError, ValueError, IndexError) as exc:
            raise ApiError("登录公钥或账号密码格式无效") from exc
        response = self.post("/portal/main-api/loginInNew.do", json={
            "loginName": encrypted_user, "passWord": encrypted_password,
            "deviceId": 1711, "deviceOs": "pc", "lon": 40, "lat": 30,
            "siteId": self.SITE_ID, "sinopec": "false",
        })
        token = response.get("token")
        if (not isinstance(token, str) or not token
                or response.get("successResult") is False
                or str(response.get("result")) == "0"):
            raise ApiError("登录失败，请检查账号密码或在官网完成必要验证")
        return self._accept_login_token(token)

    def _accept_login_token(self, token):
        previous = self.token
        self.set_token(token)
        try:
            state = self.check_login()
            if not state["loggedIn"]:
                raise ApiError("登录凭据未通过验证，请在官网播放任意课程后重试")
        except Exception:
            self.set_token(previous)
            raise
        return state

    def create_qr(self):
        response = self.post(self.QR_TOKEN_PATH)
        try:
            payload = response["data"]["data"]
            decoded = json_module.loads(payload)
            token = decoded["params"]["qrToken"]
            if not isinstance(token, str) or not token:
                raise ValueError("empty token")
        except (KeyError, TypeError, ValueError) as exc:
            raise ApiError("二维码接口返回结构变化，需要更新适配") from exc
        return {"qrToken": token, "content": payload}

    def poll_qr(self, qr_token):
        if not isinstance(qr_token, str) or not qr_token:
            raise ApiError("请先获取登录二维码")
        response = self._request("POST", self.QR_CHECK_PATH, params={"qrToken": qr_token})
        try:
            status = int(response["data"]["data"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ApiError("扫码状态接口返回结构变化，需要更新适配") from exc
        if status == -1:
            return {"state": "expired"}
        if status == 1:
            state = self._accept_login_token(qr_token)
            return {"state": "confirmed", "nickname": state["nickname"]}
        return {"state": "waiting"}

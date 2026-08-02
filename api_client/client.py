# -*- coding: utf-8 -*-
"""服务端 API 客户端（JWT 封装 + 自动刷新）。

P01 骨架版：定义 APIClient 基础结构与 init()；具体业务端点在 endpoints.py。
开发期无服务端（base_url 为空）时，所有调用返回 (False, {'detail': 'no server'}) 而不抛错，
便于在 mock 模式下跑通客户端界面。
"""
import logging
import os
import threading

import requests

_logger = logging.getLogger(__name__)


def _friendly_network_error(e):
    """把 requests 网络异常转成对用户友好的中文提示（不泄漏 host/port/WinError 等内部细节）。

    原始异常由调用方写入 client.log 供诊断；用户只见简明提示。
    """
    if isinstance(e, requests.exceptions.Timeout):
        return {'detail': '服务器响应超时，请稍后重试。', 'code': 'ERR_NETWORK_TIMEOUT'}
    if isinstance(e, requests.exceptions.SSLError):
        return {'detail': '安全连接失败，请检查网络或代理设置后重试。', 'code': 'ERR_NETWORK_SSL'}
    if isinstance(e, requests.exceptions.ConnectionError):
        # 拒绝连接 / DNS 失败 / 断网：服务可能未启动或正在维护
        return {'detail': '无法连接到服务器，服务可能正在维护或启动中，请稍后重试。', 'code': 'ERR_NETWORK_CONN'}
    return {'detail': '网络连接异常，请检查网络后重试。', 'code': 'ERR_NETWORK'}


class APIClient:
    """单例。封装 access/refresh token 与自动刷新。"""

    # 自动刷新时的状态锁
    _refresh_lock = threading.Lock()

    def __init__(self):
        self.base_url = ''
        self.session = requests.Session()
        self.access_token = ''
        self.refresh_token = ''

    # ── 生命周期 ──
    def configure(self, base_url: str, client_version: str = 'unknown', client_channel: str = 'stable'):
        self.base_url = (base_url or '').rstrip('/')
        # TLS 校验：优先证书 pinning（只信服务器这张自签证书，防公网 MITM 截 JWT）。
        #   1) SERVER_CA_BUNDLE 指定证书路径（相对本模块目录）→ pin 它；
        #   2) 否则若本模块旁有 server-ca.pem → 自动 pin；
        #   3) 否则按 API_VERIFY_TLS（默认 true；自签 IP 临时可 false）。
        #   仅影响 AlphaGPT 服务端会话；DeepSeek/Tavily 等出站走各自 client。
        _here = os.path.dirname(os.path.abspath(__file__))
        ca = os.getenv('SERVER_CA_BUNDLE', '').strip()
        if ca and not os.path.isabs(ca):
            ca = os.path.join(_here, ca)
        if not ca:
            bundled = os.path.join(_here, 'server-ca.pem')
            if os.path.exists(bundled):
                ca = bundled
        self.session.verify = ca or (os.getenv('API_VERIFY_TLS', 'true').lower() not in ('false', '0', 'no', 'off'))
        # X-Client-Version / X-Client-Channel 全局带上（登录与所有请求）；服务端登录闸门据此判定（docs/14 §4）
        self.session.headers['X-Client-Version'] = client_version or 'unknown'
        self.session.headers['X-Client-Channel'] = client_channel or 'stable'
        if self.base_url:
            self.session.headers.update({'Accept': 'application/json'})

    def has_server(self) -> bool:
        return bool(self.base_url)

    def set_tokens(self, access: str, refresh: str):
        self.access_token = access or ''
        self.refresh_token = refresh or ''
        if access:
            self.session.headers['Authorization'] = f'Bearer {access}'
        else:
            self.session.headers.pop('Authorization', None)

    def clear_tokens(self):
        self.set_tokens('', '')

    # ── 核心请求 ──
    def _url(self, path: str) -> str:
        return f'{self.base_url}{path}'

    def _auth_headers(self):
        h = {}
        if self.access_token:
            h['Authorization'] = f'Bearer {self.access_token}'
        return h

    def request(self, method: str, path: str, **kwargs):
        """发起请求，401 时自动 refresh 并重试一次。

        返回 (ok: bool, data: dict)。
        开发期无服务端：返回 (False, {'detail': 'no server configured'})。
        """
        if not self.has_server():
            return False, {'detail': 'no server configured', 'code': 'ERR_NO_SERVER'}

        kwargs.setdefault('timeout', 15)
        kwargs['headers'] = {**self._auth_headers(), **kwargs.get('headers', {})}
        try:
            resp = self.session.request(method, self._url(path), **kwargs)
        except requests.RequestException as e:
            _logger.warning('网络请求失败 %s %s: %r', method, path, e)
            return False, _friendly_network_error(e)

        # 401 → 尝试刷新
        if resp.status_code == 401 and self.refresh_token:
            if self._try_refresh():
                kwargs['headers'] = {**self._auth_headers(), **kwargs.get('headers', {})}
                try:
                    resp = self.session.request(method, self._url(path), **kwargs)
                except requests.RequestException as e:
                    _logger.warning('网络请求失败（刷新后重试）%s %s: %r', method, path, e)
                    return False, _friendly_network_error(e)

        ok = 200 <= resp.status_code < 300
        try:
            data = resp.json()
        except ValueError:
            data = {'detail': resp.text}
        if not ok and 'code' not in data:
            data.setdefault('code', f'ERR_HTTP_{resp.status_code}')
        return ok, data

    def _try_refresh(self) -> bool:
        """用 refresh_token 换新 access_token。"""
        if not self.has_server() or not self.refresh_token:
            return False
        with self._refresh_lock:
            try:
                resp = self.session.post(
                    self._url('/api/auth/refresh'),
                    json={'refresh': self.refresh_token}, timeout=15)
                if resp.status_code == 200:
                    self.access_token = resp.json().get('access', '')
                    if self.access_token:
                        self.session.headers['Authorization'] = f'Bearer {self.access_token}'
                        return True
            except requests.RequestException:
                pass
            return False


# ── 单例 ──
_client = APIClient()


def init(app, base_url: str, client_version: str = 'unknown', client_channel: str = 'stable'):
    """应用启动时配置（由 web/app.py 调用）。同时从 Flask session 恢复 token。"""
    _client.configure(base_url, client_version=client_version, client_channel=client_channel)
    # 延迟导入避免循环
    try:
        with app.app_context():
            from flask import session
            access = session.get('access_token', '')
            refresh = session.get('refresh_token', '')
            if access or refresh:
                _client.set_tokens(access, refresh)
    except Exception:
        pass


def get_client() -> APIClient:
    return _client

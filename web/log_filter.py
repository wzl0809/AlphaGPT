# -*- coding: utf-8 -*-
"""Werkzeug 请求日志降噪。

客户端用 Flask-SocketIO（threading / 长轮询）模式，Socket.IO 每 ~25s 产生一对
GET/POST /socket.io/ 请求，Werkzeug dev server 默认把每条 HTTP 请求以 INFO 记到
``werkzeug`` logger → 控制台被 200 刷屏，对终端用户毫无价值。

本模块挂一个 logging.Filter 到 ``werkzeug`` logger，丢弃：
  * 所有 /socket.io/ 请求（轮询 / 握手，无论状态码）；
  * 其余返回 200 的成功请求（用户在浏览器里看得见结果，无需控制台再报）。
保留 4xx / 5xx（排错需要）与非请求类日志（如启动横幅）。

仅依赖标准库，便于单测；不触碰 app/db 导入链。
werkzeug 实际消息格式（见 werkzeug.serving.WSGIRequestHandler.log）：
    "{address} - - [{date}] \"{command} {path} {version}\" {code} {size}"
"""
import logging
import re

__all__ = ["silence_request_log_noise"]


# 形如  ..."POST /socket.io/?... HTTP/1.1" 200 -  的请求行尾，捕获状态码
_REQ_STATUS = re.compile(r'"[A-Z]+\s+\S+\s+HTTP/[\d.]+"\s+(\d{3})\b')


class _QuietRequestLogFilter(logging.Filter):
    """丢弃 Werkzeug 成功请求日志中的轮询噪声。"""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003（logging 签名要求）
        try:
            msg = record.getMessage()
        except Exception:
            return True  # 解析失败则放行，绝不吞掉未知日志
        if "/socket.io/" in msg:
            return False
        m = _REQ_STATUS.search(msg)
        if m and m.group(1) == "200":
            return False
        return True


def silence_request_log_noise() -> None:
    """把降噪过滤器挂到 werkzeug logger（幂等，可重复调用）。"""
    wk = logging.getLogger("werkzeug")
    if not any(isinstance(f, _QuietRequestLogFilter) for f in wk.filters):
        wk.addFilter(_QuietRequestLogFilter())

# -*- coding: utf-8 -*-
"""日志/输出捕获 —— 把 AlphaGPT 的 logging 与 print() 转发到 WebSocket。

移植自 client/test_socketio/app.py，改用 emitter 统一出口。
"""
import io
import logging
from datetime import datetime

from . import emitter


class SocketIOLogHandler(logging.Handler):
    """日志 Handler：每条日志行通过 WebSocket 推送。"""

    def __init__(self, logger_name='AlphaGPT'):
        super().__init__()
        self.logger_name = logger_name
        self.setFormatter(logging.Formatter('%(asctime)s.%(msecs)03d', datefmt='%H:%M:%S'))

    def emit(self, record):
        try:
            level_tag = {'INFO': 'ℹ️', 'WARNING': '⚠️', 'ERROR': '❌',
                         'DEBUG': '🔍'}.get(record.levelname, '📝')
            msg = f'{datetime.now().strftime("%H:%M:%S")} {level_tag} {record.getMessage()}'
            emitter.emit('train_log', {'line': msg})
        except Exception:
            pass


class CaptureIO(io.TextIOWrapper):
    """捕获 sys.stdout/stderr，实时推送到 WebSocket。

    继承 TextIOWrapper 以正确处理编码；底层用一个真实 buffer。
    """

    def __init__(self):
        # 用一个真实 buffer 支持 readback；编码 UTF-8 与 AlphaGPT 一致
        self._buf = io.BytesIO()
        super().__init__(self._buf, encoding='utf-8', errors='replace', write_through=True)

    def write(self, s):
        ret = super().write(s)
        s_stripped = s.strip() if isinstance(s, str) else ''
        if s_stripped:
            emitter.emit('train_log', {'line': s_stripped})
        return ret

    def flush(self):
        try:
            super().flush()
        except Exception:
            pass


def attach_logger_handler(logger_name='AlphaGPT') -> SocketIOLogHandler:
    """给指定 logger 挂 WebSocket handler，返回 handler 供 finally 移除。"""
    logger = logging.getLogger(logger_name)
    handler = SocketIOLogHandler(logger_name)
    logger.addHandler(handler)
    return handler


def detach_logger_handler(handler: SocketIOLogHandler, logger_name='AlphaGPT'):
    logger = logging.getLogger(logger_name)
    try:
        logger.removeHandler(handler)
    except Exception:
        pass

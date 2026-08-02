# -*- coding: utf-8 -*-
"""SocketIOTqdm —— 替换系统 tqdm，把训练进度推送到 WebSocket。

移植自 client/test_socketio/app.py，改用 emitter 统一出口。
节流：每秒最多推送 4 次（避免高频刷新卡顿）。
"""
import time

from . import emitter

_last_emit_time = 0.0
_THROTTLE_SEC = 0.25   # 节流间隔


class SocketIOTqdm:
    """duck-type 替换 tqdm.tqdm：实现 __iter__/update/set_postfix/close。"""

    def __init__(self, iterable=None, initial=0, total=None, desc=None, **kwargs):
        self.iterable = iterable if iterable is not None else []
        self.total = total if total is not None else (
            len(self.iterable) if hasattr(self.iterable, '__len__') else 100)
        self.current = initial
        self.desc = desc or 'Training'
        self._postfix = {}

    def __iter__(self):
        for item in self.iterable:
            yield item
            self.update(1)

    def update(self, n=1):
        global _last_emit_time
        self.current += n
        now = time.time()
        if now - _last_emit_time < _THROTTLE_SEC:
            return
        _last_emit_time = now
        self._emit_progress()

    def set_postfix(self, info_dict=None, **kwargs):
        if info_dict:
            self._postfix.update(info_dict)
        elif kwargs:
            self._postfix.update(kwargs)
        self._emit_progress()

    def close(self):
        self._emit_progress(final=True)

    def _emit_progress(self, final=False):
        pct = min(self.current / self.total, 1.0) if self.total > 0 else 0
        postfix_str = ' | '.join(f'{k}: {v}' for k, v in self._postfix.items())
        emitter.emit('train_progress', {
            'epoch': self.current,
            'total': self.total,
            'percent': round(pct * 100, 1),
            'postfix': postfix_str,
            'desc': self.desc,
            'final': final,
        })

    # tqdm 兼容：其他可能被调用的方法
    def set_description(self, desc=None):
        self.desc = desc or self.desc

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

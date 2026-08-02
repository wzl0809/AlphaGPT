# -*- coding: utf-8 -*-
"""按用户分区的文件日志（[[client-per-user-data-partition]]）。

root logger 挂一个 RotatingFileHandler，写当前用户的 logs/<h>/client.log。
- install()         启动期挂载（写 _system 目录，登录前/未登录用）
- reconfigure(email) 登录切换到 logs/<h>/；登出传 None 回 _system

幂等（handler 打 _alphagpt_file 标记），切换时摘旧挂新。失败绝不阻断。
独立模块以避免 web.auth ↔ web.app 循环导入（两者均在请求期调用本模块）。
"""
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import storage

_FLAG = '_alphagpt_file'
_FORMATTER = logging.Formatter(
    '%(asctime)s [%(levelname).1s] %(name)s: %(message)s', datefmt='%H:%M:%S')


def _system_log_dir() -> Path:
    d = storage.client_root() / 'logs' / '_system'
    d.mkdir(parents=True, exist_ok=True)
    return d


def _new_handler(log_path: Path) -> RotatingFileHandler:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(str(log_path), maxBytes=2_000_000,
                             backupCount=3, encoding='utf-8')
    fh.setLevel(logging.INFO)
    fh.setFormatter(_FORMATTER)
    setattr(fh, _FLAG, True)
    return fh


def _remove_existing(root):
    for h in list(root.handlers):
        if getattr(h, _FLAG, False):
            try:
                root.removeHandler(h)
                h.close()
            except Exception:
                pass


def install():
    """启动期挂载文件日志（_system 目录）+ 全局 excepthook。"""
    try:
        root = logging.getLogger()
        if any(getattr(h, _FLAG, False) for h in root.handlers):
            return
        root.addHandler(_new_handler(_system_log_dir() / 'client.log'))
        sys.excepthook = lambda *a: logging.getLogger('web.app').error(
            '未处理异常', exc_info=a)
    except Exception as e:
        try:
            logging.getLogger('web.app').warning('文件日志初始化失败: %s', e)
        except Exception:
            pass


def reconfigure(email):
    """切换文件日志到指定用户目录（email=None → _system）。登录/登出调用。"""
    try:
        root = logging.getLogger()
        d = storage.user_logs_dir(email) if email else _system_log_dir()
        _remove_existing(root)
        root.addHandler(_new_handler(d / 'client.log'))
    except Exception:
        pass

# -*- coding: utf-8 -*-
"""Flask 扩展单例（避免循环导入）。

- socketio：Flask-SocketIO（threading async_mode，Windows 兼容）
- db：Flask-SQLAlchemy（本地 SQLite）
- api：APIClient 单例（连服务端 JWT，P08+ 联调）
"""
from flask_socketio import SocketIO
from flask_sqlalchemy import SQLAlchemy

socketio = SocketIO()
db = SQLAlchemy()

# api_client 延迟初始化（需 app 上下文读取 SERVER_BASE_URL）
api = None  # type: ignore[var-annotated]

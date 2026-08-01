# -*- coding: utf-8 -*-
"""客户端配置：dev / prod。

- dev：本地调试（Windows），DEBUG=True，SQLite 本地，DEV_BYPASS_AUTH 默认开。
- prod：生产，DEBUG=False，DEV_BYPASS_AUTH 必须关。
"""
import os
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

# client/.env
_CLIENT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(_CLIENT_DIR / '.env')


class Config:
    """公共配置。"""

    # Flask
    SECRET_KEY = os.getenv('FLASK_SECRET', 'dev-insecure-secret')

    # 会话安全策略（关闭浏览器 → 必须重新登录）：
    # - login_session 里 session.permanent=False → 会话级 cookie，浏览器关闭即丢弃
    #   （Set-Cookie 不带 Max-Age/Expires）。
    # - PERMANENT_SESSION_LIFETIME：session.permanent=False 时仅作为签名 cookie 的 decode
    #   max_age（Flask open_session 用它判定 cookie 是否仍可接受）。压短到 1 天可挡住浏览器
    #   「继续浏览上次会话」保留的 cookie、以及被复制的 cookie——超过即强制重登。
    #   SESSION_LIFETIME_HOURS 可调（生产建议 ≤ 24）。
    #   注：cookie 时间戳在「会话被写入」时刷新（登录 / flash / 各类动作），交互式使用中
    #   会持续滚动；纯只读浏览超过有效期才会触发重登。
    #   （勿用 SESSION_REFRESH_EACH_REQUEST——它只对 permanent=True 生效，本方案不适用。）
    PERMANENT_SESSION_LIFETIME = timedelta(
        hours=int(os.getenv('SESSION_LIFETIME_HOURS', '24'))
    )
    # 模板/静态目录相对 client/ 根
    _ROOT = _CLIENT_DIR
    TEMPLATE_FOLDER = str(_ROOT / 'templates')
    STATIC_FOLDER = str(_ROOT / 'static')

    # 客户端 Web 端口（高位避免与用户其他应用冲突）
    CLIENT_HOST = os.getenv('CLIENT_HOST', '0.0.0.0')
    CLIENT_PORT = int(os.getenv('CLIENT_PORT', '51888'))

    # 本地 SQLite
    SQLALCHEMY_DATABASE_URI = 'sqlite:///' + str(_ROOT / 'db' / 'alphagpt_local.db')
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # 并发写防护：训练持久化 + 训练结果同步 + 通知轮询可能并发写同一 SQLite。
    # busy_timeout 让写锁等待而非立即 'database is locked'；WAL 允许读写并发。
    SQLALCHEMY_ENGINE_OPTIONS = {
        'connect_args': {'timeout': 30},
        'pool_pre_ping': True,
    }

    # 服务端连接（留空=无服务端开发期）
    SERVER_BASE_URL = os.getenv('SERVER_BASE_URL', '').rstrip('/')

    # 开发旁路：True 时无 token 也放行并注入 mock 用户；生产必须 False
    DEV_BYPASS_AUTH = os.getenv('DEV_BYPASS_AUTH', 'true').lower() in ('true', '1', 'yes')

    # AI Key（系统设置页可覆盖，写本地 DB）
    DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY', '')
    ZHIPU_API_KEY = os.getenv('ZHIPU_API_KEY', '')
    TONGYI_API_KEY = os.getenv('TONGYI_API_KEY', '')
    TAVILY_API_KEY = os.getenv('TAVILY_API_KEY', '')
    TUSHARE_TOKEN = os.getenv('TUSHARE_TOKEN', '')
    # LLM 出网代理(可选, litellm/httpx 用; 留空=不走代理。国内访问 OpenAI/Anthropic 时可能需要)
    LLM_HTTP_PROXY = os.getenv('LLM_HTTP_PROXY', '')

    # 钉钉（可选）
    DINGTALK_WEBHOOK = os.getenv('DINGTALK_WEBHOOK', '')
    DINGTALK_SECRET = os.getenv('DINGTALK_SECRET', '')


class DevConfig(Config):
    DEBUG = True
    # 开发期显式开启旁路（除非 .env 明确 false）
    DEV_BYPASS_AUTH = os.getenv('DEV_BYPASS_AUTH', 'true').lower() in ('true', '1', 'yes')


class ProdConfig(Config):
    DEBUG = False
    # 生产强制关闭旁路
    DEV_BYPASS_AUTH = False


# 配置映射
CONFIG_MAP = {
    'dev': DevConfig,
    'prod': ProdConfig,
}

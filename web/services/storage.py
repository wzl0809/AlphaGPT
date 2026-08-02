# -*- coding: utf-8 -*-
"""按用户分区的本地存储路径解析（[[client-per-user-data-partition]]）。

客户端本地数据按登录账号分区：
- api_keys.enc → db/users/<h>/api_keys.enc
- reports      → reports/<h>/   （web 层写盘/服务；引擎仍写裸 'reports'，由 result_parser 迁入）
- logs         → logs/<h>/client.log（RotatingFileHandler 在登录时切换）
<h> = sha256(email)[:16]，避免邮箱明文落入文件系统路径。

DB 表（local_formula 等）通过 owner_email 列隔离，不走本模块。
保持全局（机器级，不分用户）：kline_cache/、db/hw_cache.json。
checkpoints/ 由引擎管理（transient），本模块仅提供目录供清理页索引归属。

属主 email 经 web.auth.active_email() 取（进程级，请求/后台线程均可用）。
"""
import hashlib
from pathlib import Path

_CLIENT_ROOT = Path(__file__).resolve().parent.parent.parent
_USERS_DIR = _CLIENT_ROOT / 'db' / 'users'


def user_hash(email: str) -> str:
    """email → 16 位 hex（sha256 截断）。空 email → 'anon'（dev/未登录兜底）。"""
    e = (email or '').strip().lower()
    if not e:
        return 'anon'
    return hashlib.sha256(e.encode('utf-8')).hexdigest()[:16]


def _ensure(p: Path) -> Path:
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return p


def user_reports_dir(email: str) -> Path:
    """当前用户的报告目录（训练/信号图迁入此；/reports-file 只从此服务）。"""
    return _ensure(_CLIENT_ROOT / 'reports' / user_hash(email))


def user_checkpoints_dir(email: str) -> Path:
    """当前用户的断点目录（清理页索引归属用；引擎本体仍写全局 checkpoints/）。"""
    return _CLIENT_ROOT / 'checkpoints' / user_hash(email)


def user_logs_dir(email: str) -> Path:
    """当前用户的日志目录。"""
    return _ensure(_CLIENT_ROOT / 'logs' / user_hash(email))


def user_api_keys_path(email: str) -> Path:
    """当前用户的加密 API Key 文件路径。"""
    return _ensure(_USERS_DIR / user_hash(email)) / 'api_keys.enc'


def legacy_api_keys_path() -> Path:
    """旧的全局 db/api_keys.enc（一次性迁移到首个登录用户目录用）。"""
    return _CLIENT_ROOT / 'db' / 'api_keys.enc'


def client_root() -> Path:
    return _CLIENT_ROOT


def backfill_email() -> str:
    """存量数据迁移目标 email：db/last_user.json 记录的上次登录邮箱，否则 admin 测试账号。

    仅本机迁移用（旧库的 ownerless 行与全局 reports 文件归此邮箱）；新装机库为空，无影响。
    """
    try:
        import json
        lu = _CLIENT_ROOT / 'db' / 'last_user.json'
        if lu.exists():
            e = (json.loads(lu.read_text(encoding='utf-8')).get('email') or '').strip().lower()
            if e:
                return e
    except Exception:
        pass
    return 'admin@alphagpt.local'

# -*- coding: utf-8 -*-
"""客户端版本信息本地缓存（SQLite 单例，docs/14 §5）。

零网络消费：
- 更新中心首屏用 outdated_payload（426 拒登时已持久化）渲染，避免拒登后再发一次请求（C16）。
- 关于页用 latest_payload（最近一次"检查更新"）。
所有访问 try/except → 失败返回 None，绝不阻塞请求（context_processor 与登录流程均依赖本模块）。
"""
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def _row():
    """取单例行（id=1），无则建空行。失败返回 None。"""
    try:
        from web.extensions import db
        from db.models import ReleaseInfoCache
        row = ReleaseInfoCache.query.filter_by(id=1).first()
        if row is None:
            row = ReleaseInfoCache(id=1)
            db.session.add(row)
            db.session.commit()
        return row
    except Exception as e:
        logger.warning('release_cache 读取失败: %s', e)
        try:
            from web.extensions import db
            db.session.rollback()
        except Exception:
            pass
        return None


def get_cached_release():
    row = _row()
    return (row.latest_payload if row else None) or None


def get_outdated_payload():
    row = _row()
    return (row.outdated_payload if row else None) or None


def set_cached_release(payload):
    if not isinstance(payload, dict):
        return
    try:
        from web.extensions import db
        row = _row()
        if row is None:
            return
        row.latest_payload = payload
        row.last_checked_at = datetime.utcnow()
        row.updated_at = datetime.utcnow()
        db.session.commit()
    except Exception as e:
        logger.warning('release_cache 写入失败: %s', e)
        try:
            from web.extensions import db
            db.session.rollback()
        except Exception:
            pass


def set_outdated_payload(payload):
    if not isinstance(payload, dict):
        return
    try:
        from web.extensions import db
        row = _row()
        if row is None:
            return
        row.outdated_payload = payload
        row.updated_at = datetime.utcnow()
        db.session.commit()
    except Exception as e:
        logger.warning('release_cache outdated 写入失败: %s', e)
        try:
            from web.extensions import db
            db.session.rollback()
        except Exception:
            pass


def clear_outdated_payload():
    """清空陈旧的 426 拒登负载（成功登录 / 检测通过后调用，避免更新中心首屏误渲染过期的"必须升级"）。"""
    try:
        from web.extensions import db
        row = _row()
        if row is None:
            return
        row.outdated_payload = None
        row.updated_at = datetime.utcnow()
        db.session.commit()
    except Exception as e:
        logger.warning('release_cache clear outdated 失败: %s', e)
        try:
            from web.extensions import db
            db.session.rollback()
        except Exception:
            pass

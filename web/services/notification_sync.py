# -*- coding: utf-8 -*-
"""通知轮询：定期从服务端拉取公告 / 个人通知，缓存到本地 SQLite。

- 开发期无服务端（api.has_server()=False）：轮询空转，不报错。
- 有服务端：每 10 分钟 调 /api/notifications/poll?since=<last_poll_ts>，
  增量 upsert 到 NotificationCache。
- 客户端首页/通知展示读本地缓存，与服务端解耦。
"""
import threading
import time
from datetime import datetime

_poller_started = False
_poller_lock = threading.Lock()
_INTERVAL = 600   # 秒（10 分钟——降服务端 poll 负载；登录后仍立即 poll_once 刷首屏，被动到账/公告/版本 ≤10min 感知，主动操作余额靠 refresh_user_profile 即时）
_FAIL_BACKOFF = (60, 120, 300, 600)   # 连续失败阶梯退避（秒）；成功归零回 _INTERVAL

# 通知轮询捎带的最新余额（进程内存）：后台轮询线程写，before_request 读。
# 用途：让「别人买你公式」等外部到账在 ≤10 分钟 内反映到侧栏，且不增加任何服务器请求
# （余额搭在每 10 分钟 本就要发的 poll 请求里，服务端 request.user 已加载故零额外查询）。
_balance_lock = threading.Lock()
_latest_balance = None   # None=未知（未取到/已清空）；否则为 poll 返回的余额


def latest_balance():
    """供 before_request 读取的最新余额（零服务器请求）。无数据返回 None。"""
    with _balance_lock:
        return _latest_balance


def clear_latest_balance():
    """登录/登出时清空，避免跨用户串号（切换账号后 ≤10 分钟 内不显示上一用户余额）。"""
    global _latest_balance
    with _balance_lock:
        _latest_balance = None


def _upsert_cache(app, kind, remote_id, row):
    """增量写入 NotificationCache（按 remote_id+kind 去重，属当前用户）。"""
    from web.extensions import db
    from db.models import NotificationCache
    from web.auth import active_email
    owner = active_email()
    existing = NotificationCache.owned().filter_by(kind=kind, remote_id=str(remote_id)).first()
    pub = row.get('publish_at') or row.get('created_at')
    pub_dt = None
    if pub:
        for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
            try:
                pub_dt = datetime.strptime(str(pub)[:19], fmt); break
            except ValueError:
                continue
    fields = dict(
        title=row.get('title', ''), content=row.get('content', ''),
        category=row.get('category', 'normal'),
        is_read=bool(row.get('is_read', False)),
        publish_at=pub_dt, cached_at=datetime.utcnow())
    if existing:
        for k, v in fields.items():
            setattr(existing, k, v)
    else:
        db.session.add(NotificationCache(owner_email=owner, kind=kind,
                                         remote_id=str(remote_id), **fields))
    db.session.commit()


def poll_once(app):
    """执行一次轮询。返回 True/False（成功/失败，供 _loop 退避）。

    公告：全量同步（服务端 PollView 始终回完整 active 集）→ upsert + 删除服务端已下架的，
          保证首页横幅反映服务端当前 active 公告（公告可下架，故必须全量比对）。
    个人通知：增量 upsert（since=上次 up_to，append-only 安全）；按 remote_id 去重。
    失败（无服务端/网络错）不推进游标，下次从上次成功点重拉（无丢失）。
    """
    import api_client.endpoints as ep
    from web.extensions import db
    from db.models import NotificationCache
    with app.app_context():
        ok, data = ep.poll_notifications(_last_ts(app) or '')   # 个人通知增量（since=上次 up_to）；首跑无游标则全量
        if not ok:
            return False   # 无服务端/网络错：不推进游标（下次重拉无丢失），_loop 据此退避
        # 捎带的余额 → 进程内存（before_request 会 patch 进 session，零额外请求）
        global _latest_balance
        b = data.get('balance')
        if b is not None:
            with _balance_lock:
                _latest_balance = b
        # 版本信息搭车（docs/14）：写本地缓存 → 顶栏角标/关于页 ≤10 分钟 感知新版（零额外请求）
        ri = data.get('release_info')
        if isinstance(ri, dict) and not ri.get('code'):
            try:
                from web.services.release_cache import set_cached_release
                set_cached_release(ri)
            except Exception:
                pass
        # 公告：全量同步
        srv_ids = set()
        for a in data.get('announcements', []):
            rid = a.get('id')
            if rid is None:
                continue
            srv_ids.add(str(rid))
            try:
                _upsert_cache(app, 'announcement', rid, a)
            except Exception:
                pass
        # 删除本地有、服务端已无（被下架/删除）的公告（仅当前用户的缓存）
        for row in NotificationCache.owned().filter_by(kind='announcement').all():
            if row.remote_id not in srv_ids:
                db.session.delete(row)
        # 个人通知：增量 upsert（仅 since 之后的新增；按 remote_id 去重）
        for p in data.get('personal', []):
            try:
                _upsert_cache(app, 'personal', p.get('id'), p)
            except Exception:
                pass
        db.session.commit()
        # 成功才推进增量游标（失败不推进 → 下次重拉无丢失）
        _set_last_ts(app, data.get('up_to') or '')
        # 重连对账：在线时把本地公式库与服务端库位全量同步（claim pending + 清孤儿 + 超额锁）
        try:
            from web.services.library_sync import reconcile_with_server
            reconcile_with_server()
        except Exception:
            pass
        return True


def _last_ts(app):
    with app.app_context():
        from db.models import UserProfileCache
        r = UserProfileCache.query.first()
        return r.last_poll_ts.isoformat() if (r and r.last_poll_ts) else ''


def _set_last_ts(app, ts):
    with app.app_context():
        from db.models import UserProfileCache
        from web.extensions import db
        r = UserProfileCache.query.first()
        if not r:
            r = UserProfileCache(id=1)
            db.session.add(r)
        try:
            r.last_poll_ts = datetime.strptime(str(ts)[:19], '%Y-%m-%dT%H:%M:%S')
        except ValueError:
            r.last_poll_ts = datetime.utcnow()
        db.session.commit()


def _loop(app):
    consec_fail = 0
    while True:
        try:
            ok = poll_once(app)
        except Exception:
            ok = False
        if ok:
            consec_fail = 0
        else:
            consec_fail += 1
        # 成功→10 分钟；连续失败→阶梯退避（让病态/承压服务端喘息，整支客户端集群随之降频），首成功即归零
        delay = _FAIL_BACKOFF[min(consec_fail, len(_FAIL_BACKOFF) - 1)] if consec_fail else _INTERVAL
        time.sleep(delay)


def start_poller(app):
    """启动后台轮询线程（进程内 daemon，启动一次）。"""
    global _poller_started
    with _poller_lock:
        if _poller_started:
            return
        _poller_started = True
    t = threading.Thread(target=_loop, args=(app,), daemon=True, name='notification-poller')
    t.start()

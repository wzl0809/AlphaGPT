# -*- coding: utf-8 -*-
"""登录态与开发旁路。

- DEV_BYPASS_AUTH=True（开发期，默认）：无 token 也放行，current_user() 返回 mock 用户。
- DEV_BYPASS_AUTH=False（生产）：无 token 重定向到登录页。

真实登录态：登录后 access/refresh token 与用户信息均写入 Flask 会话（客户端签名 cookie）。
会话安全策略（关闭浏览器 → 必须重新登录；见 login_session / config.PERMANENT_SESSION_LIFETIME）：
- session.permanent=False → 浏览器关闭即丢弃 cookie，重开客户端必须重新登录。
- 较短的 PERMANENT_SESSION_LIFETIME（默认 1 天）→ 即便浏览器「继续浏览上次会话」
  保留了 cookie、或 cookie 被复制，超过有效期同样强制重登。
⚠️ 注意：docs/04 §4 设想的「refresh_token 加密存本地配置」一旦实现，启动时会静默换发新 token、
重建会话，从而绕过本策略。如确需落盘，必须绑定 OS keyring/会话锁或显式「记住我」开关。
P08 联调真服务端前，开发期用旁路。
"""
from functools import wraps

from flask import session, redirect, url_for, flash, current_app


# ── 开发期 mock 用户（仅 DEV_BYPASS_AUTH 时使用）──
_MOCK_USER = {
    'id': 0,
    'email': 'dev@local',
    'username': '开发者(本地)',
    'level': 3,
    'level_title': '策略研究员',
    'nexus_balance': 1000.0,
    'is_subscriber': False,
    'subscribe_expire': None,
    'is_mock': True,
}


def current_user():
    """返回当前用户信息 dict。

    优先级：session 真实缓存 > 开发 mock > None。
    """
    # 真实登录态（登录后写入 session）
    profile = session.get('user_profile')
    if profile:
        return profile

    # 开发旁路
    if current_app.config.get('DEV_BYPASS_AUTH'):
        return dict(_MOCK_USER)

    return None


# ── 本地数据属主（账号隔离，[[client-per-user-data-partition]]）──
# 客户端单用户/进程：同一时刻只有一个登录会话。故用进程级 _active_email 作属主源，
# 在请求上下文（蓝图/socket）与后台线程（信号生成/训练结果同步/通知轮询）均可用，
# 无需把 email 往每个线程透传。login_session 置位、logout_session 清空。
_active_email = ''


def set_active_email(email):
    """登录时置位当前用户 email（小写）；登出清空（传 ''）。"""
    global _active_email
    _active_email = (email or '').strip().lower()


def _remember_last_user(email):
    """落 db/last_user.json：owner_email 列迁移时把存量行 backfill 到此邮箱（dev 机=admin 测试数据）。"""
    try:
        import json as _json
        from pathlib import Path
        p = Path(__file__).resolve().parent.parent / 'db' / 'last_user.json'
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_json.dumps({'email': (email or '').strip().lower()}, ensure_ascii=False),
                     encoding='utf-8')
    except Exception:
        pass


def active_email() -> str:
    """当前登录用户 email（小写）。进程级置位优先；未置位回落 current_user()['email']
    （dev mock → 'dev@local'）；皆无返回 ''。供 DB 属主过滤与用户目录解析共用。

    后台线程（通知轮询/训练结果同步/信号生成）无请求上下文 → current_user() 访问 session 会抛，
    此时回落 ''（.owned() 过滤空属主=空集，dev 后台轮询优雅空转，不崩）。"""
    if _active_email:
        return _active_email
    try:
        u = current_user()
        return ((u or {}).get('email') or '').strip().lower()
    except Exception:
        return ''


def get_owned(model_cls, pk):
    """按主键取记录并校验属主；不存在或不属当前用户一律返回 None。

    防止 B 通过猜 id 访问 A 的公式/跟踪（db.session.get 不带属主过滤）。
    """
    from web.extensions import db
    obj = db.session.get(model_cls, pk)
    if obj is None:
        return None
    if getattr(obj, 'owner_email', None) != active_email():
        return None
    return obj


def is_logged_in() -> bool:
    return current_user() is not None


def refresh_user_profile():
    """扣除算力/到账后刷新 session 中的用户信息缓存（余额/订阅/等级即时同步）。

    current_user() 优先读 session['user_profile']，该缓存在登录时写入、之后**不会自动更新**。
    因此任何改变服务端余额或订阅状态的操作（交易购买 / 转账 / 订阅 / 悬赏扣押 / 悬赏退款
    / 接力托管 / 接力汇总退款）成功后**必须**调用本函数，否则侧栏 base.html:72 与各页顶部
    的余额会停留在操作前的旧值——这正是「多次购买但余额一直显示不变」的根因。
    失败（无服务端 / 超时 / 401）静默忽略并保留旧缓存——离线/掉线时不清登录态。
    返回刷新后的 profile dict，或 None。

    附带：/users/me 响应携带最新 entitlement（额度凭证），此处同步刷新 → cap/会员态即时更新。
    """
    try:
        import api_client.endpoints as ep
        ok, prof = ep.get_profile()
        if ok and isinstance(prof, dict):
            ent = prof.pop('entitlement', None)
            if ent:
                try:
                    from . import entitlement
                    entitlement.set_token(ent)
                except Exception:
                    pass
            session['user_profile'] = prof
            return prof
    except Exception:
        pass
    return None


def register_balance_sync_hook(app):
    """注册 before_request：把通知轮询捎带的最新余额 patch 进 session（零服务器请求）。

    数据源是 notification_sync 后台线程每 10 分钟 拉取、缓存在进程内存的余额（搭在 poll 响应里，
    服务端 request.user 已为鉴权加载故零额外查询）。本 hook 只读进程内存 → session，不发任何请求，
    使「别人买你公式」等外部到账在 ≤10 分钟 内反映到侧栏。详见 [[client-balance-refresh-after-coin-mutation]]。
    """
    from flask import request

    @app.before_request
    def _patch_balance():
        # 静态资源 / SocketIO 长轮询跳过（避免给 CSS/JS/socketio 也跑一遍）
        if request.endpoint == 'static' or request.path.startswith('/socket.io/'):
            return
        profile = session.get('user_profile')
        if not profile:
            return  # 未登录（dev mock 不进 session）→ 跳过
        try:
            from web.services import notification_sync
            b = notification_sync.latest_balance()
        except Exception:
            return
        if b is None or profile.get('nexus_coin_balance') == b:
            return  # 未知 / 已是最新 → 不写 session（避免无谓 Set-Cookie）
        profile['nexus_coin_balance'] = b
        session['user_profile'] = profile


def login_required(view):
    """视图装饰器：未登录时重定向到登录页（开发旁路放行）。"""

    @wraps(view)
    def wrapped(*args, **kwargs):
        if is_logged_in():
            return view(*args, **kwargs)
        flash('请先登录', 'warning')
        return redirect(url_for('auth.login', next=request_next()))

    return wrapped


def request_next():
    """读取 ?next= 用于登录后回跳。"""
    from flask import request
    return request.args.get('next', '')


def login_session(profile: dict, access_token: str, refresh_token: str, entitlement: str = None):
    """登录成功后写入会话（P08 真登录调用） + 同步 api_client 单例 token + 缓存额度凭证。

    安全策略：会话为「浏览器会话 cookie」(session.permanent=False)——浏览器关闭即丢弃，
    再次打开客户端必须重新登录。配合 config.PERMANENT_SESSION_LIFETIME（默认 1 天），
    即便浏览器开了「继续浏览上次会话」保留了 cookie、或 cookie 被复制，超过有效期同样强制重登。

    entitlement：服务端签名的额度凭证（cap + 会员态）。有则缓存（进程内 + 落盘 db/entitlement.enc
    供离线 7 天内重启使用）；无（开发旁路 / 无服务端）则清空，回退到开发不受限模式。
    """
    # permanent=False → Set-Cookie 不带 Max-Age → 浏览器会话级 cookie（关闭即失效）。
    session.permanent = False
    session['user_profile'] = profile
    session['access_token'] = access_token
    session['refresh_token'] = refresh_token

    # ★ 账号隔离（[[client-per-user-data-partition]]）：置位进程级属主 email，
    #   后续 DB 查询/.owned() 与本地文件目录（api_keys/reports/logs）均按此分区。
    email = (profile or {}).get('email') or ''
    set_active_email(email)
    _remember_last_user(email)
    # 切换文件日志到该用户目录（登出/未登录写 _system）
    try:
        from .services import file_logging
        file_logging.reconfigure(email)
    except Exception:
        pass
    # 加载该用户的本地 API Key（迁旧全局密钥到首登录用户目录，再注入 app.config/os.environ）
    try:
        from .services import apikey_store
        from flask import current_app as _app
        apikey_store.migrate_legacy(email)
        apikey_store.apply_to_app(_app, email)
    except Exception:
        pass

    # 额度凭证（替代明文 FREE_FORMULA_CAP）：缓存签名 cap/会员态，离线亦可验
    try:
        from . import entitlement as _ent
        _ent.set_token(entitlement) if entitlement else _ent.clear_token()
    except Exception:
        pass
    # ★ 同步 api_client 单例：init 只在启动时读一次 session，登录后必须主动 set，
    # 否则后续 API 调用无 Authorization → 服务端 401（P12 联调发现的登录断点）。
    try:
        import api_client
        api_client.get_client().set_tokens(access_token, refresh_token)
    except Exception:
        pass
    # 登录后立即触发一次公告/通知拉取（不等 10 分钟 轮询，首页横幅即时刷新）
    try:
        import threading
        from web.services import notification_sync
        notification_sync.clear_latest_balance()  # 切换账号：先清旧用户余额缓存，防窗口期串号
        _app = current_app._get_current_object()
        threading.Thread(target=lambda: notification_sync.poll_once(_app),
                         daemon=True).start()
    except Exception:
        pass


def logout_session():
    session.pop('user_profile', None)
    session.pop('access_token', None)
    session.pop('refresh_token', None)
    # ★ 账号隔离：清属主置位 + 清当前用户 API Key 的进程内残留（app.config/os.environ）
    #   + 日志切回 _system。密钥文件本身保留（同用户重登复用），仅清进程内运行态防下一个
    #   登录者在未重载前读到上一个用户的密钥。
    set_active_email('')
    try:
        from .services import apikey_store
        from flask import current_app as _app
        apikey_store.clear_from_app(_app, list(apikey_store.KEY_MAP.keys()))
    except Exception:
        pass
    try:
        from .services import file_logging
        file_logging.reconfigure(None)
    except Exception:
        pass
    # 清额度凭证（防下一用户串号）
    try:
        from . import entitlement
        entitlement.clear_token()
    except Exception:
        pass
    try:
        import api_client
        api_client.get_client().clear_tokens()
    except Exception:
        pass
    # 清余额缓存，防下次登录前的窗口期显示旧值
    try:
        from web.services import notification_sync
        notification_sync.clear_latest_balance()
    except Exception:
        pass

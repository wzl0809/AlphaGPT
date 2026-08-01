# -*- coding: utf-8 -*-
"""Flask 应用工厂。

运行：
    cd client
    python -m web.app                 # 开发服务器
    # 或
    flask --app web.app run --port 5000
"""
import os
import sys
import logging
from pathlib import Path

from flask import Flask

from .config import CONFIG_MAP

# 把 core_engine + encrypted 加入 sys.path（hw_monitor / AlphaGPT / silent_uploader 导入用）
_CORE = Path(__file__).resolve().parent.parent / 'core_engine'
_ENC = Path(__file__).resolve().parent.parent / 'encrypted'
for _p in (str(_CORE), str(_ENC)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 国内数据源 / 国内 LLM 域名 —— 这些必须直连，绕过系统代理
# （requests 在 Windows 会读 IE/注册表代理；V2Ray/Xray 类客户端常把代理写进注册表，
#  把东财 push2 等国内源也塞进代理 → ProxyError → 大盘行情取不到、降级为「震荡」）
_DOMESTIC_NO_PROXY = (
    'eastmoney.com',     # 东财 push2/push2his/82./33. 等行情接口（akshare+efinance 同源）
    'sina.com', 'sinajs.cn',   # 新浪行情（akshare 备源）
    'tushare.pro',       # tushare 数据
    'deepseek.com',      # DeepSeek API（国内）
    'bigmodel.cn',       # 智谱 GLM（国内）
    'aliyuncs.com',      # 通义千问 dashscope（国内）
    'baostock.com',      # baostock（实际走裸 TCP，无害）
)


def _apply_no_proxy_domestic():
    """把国内域名合并进 NO_PROXY 环境变量，让 requests 对其直连。

    国际源（Tavily/OpenAI 等）不在列表 → 仍走系统代理，零回归。
    幂等：保留用户已有的 NO_PROXY 条目，去重。
    """
    cur = os.environ.get('NO_PROXY') or os.environ.get('no_proxy') or ''
    parts = [p.strip() for p in cur.split(',') if p.strip()]
    for d in _DOMESTIC_NO_PROXY:
        if d not in parts:
            parts.append(d)
    val = ','.join(parts)
    os.environ['NO_PROXY'] = val
    os.environ['no_proxy'] = val  # 大小写两个 requests/httpx 都要设


def _ensure_quant_tracking_columns(db):
    """SQLite 幂等加列：db.create_all() 不会给已存在的表补新增列。

    quant_tracking 新增列（量化信号新鲜度/命中率）：last_signal_basis_date / last_hit_rate /
    last_signal_fresh。旧库升级时 ALTER TABLE 补齐；新库 create_all 已建好，跳过。
    """
    try:
        from sqlalchemy import inspect, text
        insp = inspect(db.engine)
        if 'quant_tracking' not in insp.get_table_names():
            return
        existing = {c['name'] for c in insp.get_columns('quant_tracking')}
        additions = [
            ('last_signal_basis_date', 'DATE'),
            ('last_hit_rate', 'FLOAT'),
            ('last_confidence', 'FLOAT'),
            ('last_factor_value', 'FLOAT'),
            ('last_ai_score', 'FLOAT'),
            ('last_signal_fresh', 'BOOLEAN'),
        ]
        with db.engine.begin() as conn:
            for name, typ in additions:
                if name not in existing:
                    conn.execute(text(f'ALTER TABLE quant_tracking ADD COLUMN {name} {typ}'))
    except Exception as e:
        # 迁移失败不阻断启动（最坏情况：新列为 NULL，前端按"未知"渲染）
        import logging
        logging.getLogger(__name__).warning(f'quant_tracking 加列迁移跳过: {e}')


def _ensure_local_formula_columns(db):
    """SQLite 幂等加列：local_formula 新增 server_claimed（服务端库位确认标记，offline pending 用）。

    旧库升级时 ALTER TABLE 补齐；新库 create_all 已建好，跳过。
    """
    try:
        from sqlalchemy import inspect, text
        insp = inspect(db.engine)
        if 'local_formula' not in insp.get_table_names():
            return
        existing = {c['name'] for c in insp.get_columns('local_formula')}
        if 'server_claimed' not in existing:
            with db.engine.begin() as conn:
                conn.execute(text('ALTER TABLE local_formula ADD COLUMN server_claimed BOOLEAN'))
            # 旧数据：已 saved 的视为已确认（迁移前无服务端计数，按宽松处理，reconcile 会校正）
            with db.engine.begin() as conn:
                conn.execute(text('UPDATE local_formula SET server_claimed = 1 WHERE saved = 1'))
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f'local_formula 加列迁移跳过: {e}')


def _ensure_owner_columns(db):
    """SQLite 幂等加列：给 7 张「人属」表补 owner_email（账号隔离，[[client-per-user-data-partition]]）。

    db.create_all() 不给已存在表补列；旧库升级时 ALTER TABLE 补齐。新库 create_all 已建好跳过。
    存量行（迁移前无属主）backfill 到 db/last_user.json 记录的上次登录邮箱（dev 机=admin 测试数据），
    无记录则 'admin@alphagpt.local'（仅本机迁移用；新装机库为空，无影响）。
    """
    tables = ('local_formula', 'quant_tracking', 'ai_analysis_cache',
              'notification_cache', 'reports_index', 'checkpoints_index',
              'silent_upload_queue')
    try:
        from sqlalchemy import inspect, text
        from .services import storage
        insp = inspect(db.engine)
        # backfill 目标：上次登录邮箱（dev 机迁移保留测试数据归属），否则 admin 测试账号
        backfill_to = storage.backfill_email()
        with db.engine.begin() as conn:
            for t in tables:
                if t not in insp.get_table_names():
                    continue
                existing = {c['name'] for c in insp.get_columns(t)}
                if 'owner_email' not in existing:
                    conn.execute(text(f'ALTER TABLE {t} ADD COLUMN owner_email VARCHAR(128)'))
                    conn.execute(text(f"UPDATE {t} SET owner_email = :e WHERE owner_email IS NULL"),
                                 {'e': backfill_to})
                    conn.execute(text(f'CREATE INDEX IF NOT EXISTS ix_{t}_owner_email ON {t} (owner_email)'))
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f'owner_email 加列迁移跳过: {e}')


def _migrate_legacy_reports_once():
    """一次性：把旧全局 reports/ 文件迁入 backfill 属主目录（admin 测试数据基准图不失显）。

    引擎仍写裸 'reports'（CWD 相对），分区后由 result_parser 迁入用户目录；但升级前已存在
    的全局 reports/ 文件不会被回填。本函数在首次升级启动时把它们搬到 backfill 用户目录，
    使 /reports-file（按属主目录服务）仍能命中。marker 守卫只跑一次；之后全局 reports/ 由
    result_parser 管理。失败静默不阻断。
    """
    try:
        from pathlib import Path
        import shutil as _sh
        from .services import storage
        marker = storage.client_root() / 'db' / '.legacy_reports_migrated'
        if marker.exists():
            return
        global_reports = storage.client_root() / 'reports'
        if not global_reports.is_dir():
            marker.touch()
            return
        dst = storage.user_reports_dir(storage.backfill_email())
        moved = 0
        for p in list(global_reports.iterdir()):
            if p.is_file():
                try:
                    _sh.move(str(p), str(dst / p.name))
                    moved += 1
                except Exception:
                    pass
        marker.touch()
        if moved:
            import logging
            logging.getLogger(__name__).info('legacy reports 迁入属主目录: %s 个文件', moved)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning('legacy reports 迁移跳过: %s', e)


def _install_file_logging():
    """root logger → 按用户分区的 client.log（登录切换）+ 全局 excepthook。

    pythonw 无 console，崩溃/异常必须落盘才能诊断。委托 web.services.file_logging
    （独立模块，登录/登出在 web.auth 中调 reconfigure 切换用户目录，避免循环导入）。
    """
    try:
        from .services import file_logging
        file_logging.install()
    except Exception as e:
        try:
            logging.getLogger(__name__).warning('文件日志初始化失败: %s', e)
        except Exception:
            pass


def _port_in_use(host: str, port: int) -> bool:
    """TCP 探测端口是否已被监听（单实例守卫用，超时 0.5s）。"""
    import socket as _socket
    try:
        with _socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _find_chrome_or_edge():
    """Windows 找 Chrome/Edge 可执行路径（注册表 App Paths + 常见安装路径），供 --start-fullscreen 启动。
    找不到返回 None（非 Windows 或未装 Chromium 系浏览器）。"""
    if os.name != 'nt':
        return None
    try:
        import winreg
        for exe in ('chrome.exe', 'msedge.exe'):
            for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(
                        root, rf'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}') as k:
                        p, _ = winreg.QueryValueEx(k, '')
                        if p and os.path.isfile(p):
                            return p
                except OSError:
                    pass
    except ImportError:
        pass
    for p in (r'C:\Program Files\Google\Chrome\Application\chrome.exe',
              r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
              r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
              r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'):
        if os.path.isfile(p):
            return p
    return None


def _launch_fullscreen_browser(url: str):
    """打开默认浏览器到 url（普通窗口）。

    放弃 Chrome --app（实测会触发「系统设置页」native 崩——settings 加载必调 hw_monitor GPU 检测，
    --app 独立窗口与 GPU/socketio 交互疑似 native 段错误；全屏功能加入前用普通窗口 settings 正常）
    和 --start-fullscreen（JS 退不出全屏）。普通窗口最稳；全屏由用户按 F（JS Fullscreen API）手动控制。"""
    import webbrowser
    webbrowser.open(url)


def _open_browser_when_ready(port: int, timeout: float = 15.0):
    """轮询直到服务端口 listen，再用全屏模式打开浏览器到登录页（/ 未登录被重定向到 login）。"""
    import socket as _socket
    import time as _time
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        try:
            with _socket.create_connection(('127.0.0.1', port), timeout=0.5):
                break
        except OSError:
            _time.sleep(0.2)
    _launch_fullscreen_browser(f'http://127.0.0.1:{port}/')


def create_app(config_name: str = 'dev') -> Flask:
    """构建 Flask 应用：扩展 / 蓝图 / 过滤器 / 上下文 / SocketIO。"""
    # 国内数据源绕过系统代理（必须在任何 requests 取数前生效）
    _apply_no_proxy_domestic()

    config_name = os.getenv('FLASK_ENV', config_name)
    cfg_cls = CONFIG_MAP.get(config_name, CONFIG_MAP['dev'])

    app = Flask(
        __name__,
        template_folder=cfg_cls.TEMPLATE_FOLDER,
        static_folder=cfg_cls.STATIC_FOLDER,
    )
    app.config.from_object(cfg_cls)

    # 静默 Werkzeug 请求日志刷屏（Socket.IO 长轮询 + 200 成功请求；保留 4xx/5xx）
    from .log_filter import silence_request_log_noise
    silence_request_log_noise()

    # 无 console（pythonw 发布形态）下落盘日志，便于诊断
    _install_file_logging()

    # ── 初始化扩展 ──
    from .extensions import db, socketio
    db.init_app(app)

    # SQLite WAL + busy_timeout：允许读写并发，写锁等待而非立即 locked
    # （训练持久化 / 训练结果同步 / 通知轮询并发写防护）
    try:
        from sqlalchemy import event
        with app.app_context():
            @event.listens_for(db.engine, 'connect')
            def _set_sqlite_pragma(dbapi_conn, _conn_record):
                cur = dbapi_conn.cursor()
                cur.execute('PRAGMA journal_mode=WAL')
                cur.execute('PRAGMA busy_timeout=30000')
                cur.close()
    except Exception as e:
        app.logger.warning('SQLite PRAGMA 设置失败: %s', e)

    socketio.init_app(
        app,
        async_mode='threading',          # Windows 兼容性最好
        cors_allowed_origins='*',
        logger=False,
        engineio_logger=False,
    )

    # ── api_client 单例（带客户端版本号，供登录版本闸门 X-Client-Version）──
    import api_client
    from .version import CLIENT_VERSION, CLIENT_CHANNEL
    api_client.init(app, base_url=app.config['SERVER_BASE_URL'],
                    client_version=CLIENT_VERSION, client_channel=CLIENT_CHANNEL)

    # ── API Key：分区后按用户加载（login_session 负责）。dev 旁路无登录 → 启动期为 dev mock 加载。──
    try:
        from .services import apikey_store
        if app.config.get('DEV_BYPASS_AUTH'):
            # 开发旁路无真实登录：把旧全局密钥迁到 dev@local 用户目录并加载，保证 dev 即用
            apikey_store.migrate_legacy('dev@local')
            apikey_store.apply_to_app(app, 'dev@local')
    except Exception as e:
        app.logger.warning('API Key 加载失败: %s', e)

    # ── 训练性能/环境参数（数据源并发、CPU 线程数；从训练页挪到系统设置）──
    try:
        from .services import perf_store
        perf_store.apply_to_app(app)
    except Exception as e:
        app.logger.warning('性能参数加载失败: %s', e)

    # ── 注册 14 蓝图 ──
    from .blueprints import register_blueprints
    register_blueprints(app)

    # ── Jinja 过滤器 / 全局上下文 ──
    from .template_filters import register_filters
    from .context import register_context
    register_filters(app)
    register_context(app)

    # ── before_request：通知轮询捎带的余额 patch 进 session（零服务器请求）──
    from .auth import register_balance_sync_hook
    register_balance_sync_hook(app)

    # ── SocketIO 事件 ──
    from .sockets import register_sockets
    register_sockets(socketio, app)

    # ── 建表 ──
    with app.app_context():
        # 确保 db 目录存在
        db_path = Path(app.config['SQLALCHEMY_DATABASE_URI'].replace('sqlite:///', ''))
        db_path.parent.mkdir(parents=True, exist_ok=True)
        import db.models as _db_models  # noqa: F401  触发模型导入（定义表结构）；as 形式避免覆盖 extensions.db
        db.create_all()
        _ensure_quant_tracking_columns(db)   # create_all 不给已存在表补列 → 幂等 ALTER
        _ensure_local_formula_columns(db)    # server_claimed 列（服务端库位确认标记）
        _ensure_owner_columns(db)            # owner_email 列（账号隔离，7 张人属表）
        _migrate_legacy_reports_once()       # 旧全局 reports/ 迁入 backfill 属主目录（一次性）

    app.logger.info('AlphaGPT客户端启动 | env=%s | bypass=%s | server=%s',
                    config_name, app.config['DEV_BYPASS_AUTH'],
                    app.config['SERVER_BASE_URL'] or '(无服务端/mock)')
    return app


# 便于 `flask --app web.app` 与直接运行
def main():
    from .extensions import socketio
    from . import lifecycle
    app = create_app()
    host = app.config.get('CLIENT_HOST', '0.0.0.0')
    port = app.config.get('CLIENT_PORT', 51888)

    # 单实例守卫：端口已被既有实例占用 → 只开浏览器，不起第二进程（避免端口冲突/双进程）
    if _port_in_use('127.0.0.1', port):
        _launch_fullscreen_browser(f'http://127.0.0.1:{port}/')
        if sys.stdout:
            print(f'[AlphaGPT 客户端] 已在运行，打开浏览器: http://127.0.0.1:{port}')
        return

    if sys.stdout:
        print('=' * 56)
        print(f'[AlphaGPT 客户端] 访问: http://127.0.0.1:{port}')
        print(f'  env={app.config.get("DEBUG") and "dev" or "prod"} '
              f'| bypass_auth={app.config.get("DEV_BYPASS_AUTH")} '
              f'| server={app.config.get("SERVER_BASE_URL") or "(mock)"}')
        print('=' * 56)

    # 关闭机制：捕获 werkzeug server（供 srv.shutdown）/ 空闲兜底 watcher / 托盘
    lifecycle.capture_werkzeug_server()
    lifecycle.start_watcher()
    lifecycle.start_tray(port)

    # 服务就绪后自动开浏览器到登录页
    import threading
    threading.Thread(target=_open_browser_when_ready, args=(port,),
                     daemon=True, name='browser-launch').start()

    # Windows + watchdog reloader 无法正确交接 socket（HTTP 连上即被关闭），
    # 故显式关闭 reloader，单进程稳定运行；debug 错误页保留。
    socketio.run(app, host=host, port=port, debug=app.config['DEBUG'],
                 use_reloader=False, allow_unsafe_werkzeug=True)


if __name__ == '__main__':
    main()

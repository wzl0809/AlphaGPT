# -*- coding: utf-8 -*-
"""客户端进程生命周期：托盘 + 空闲兜底退出 + busy 守卫。

发布形态下服务端以 pythonw 无窗口后台运行（见 run.bat / start.vbs）。本模块负责：

1. **busy 守卫（绝不误杀底线）**：`is_busy()` 汇总训练 / 信号生成 / 同步引擎评估
   （regen 等）。任一在跑时，自动退出逻辑一律不触发——训练/回测/信号/基准图
   绝不会被空闲超时打断。
2. **空闲兜底退出**：watcher 线程仅当「至少连接过一次 + 当前无 SocketIO 客户端 ≥
   IDLE_GRACE_SEC 秒 + 无任务在跑」时才退出。覆盖"用户关浏览器走人"。
3. **托盘图标**（pystray，best-effort）：显运行状态、双击重开浏览器、右键退出。
   pystray/PIL 未装则自动降级为"仅空闲兜底"，不阻断。
4. **干净关闭**：Flask-SocketIO 的 `socketio.stop()` 在 Werkzeug 3 已废（依赖被移除的
   `werkzeug.server.shutdown` environ 钩子，且须在请求上下文）。这里改为 monkey-patch
   `werkzeug.serving.make_server` 捕获 server 实例，退出时调 `srv.shutdown()`——
   `socketserver.BaseServer.shutdown()` 本就是为"从其它线程终止 serve_forever()"设计，
   线程安全；`serve_forever` 的 finally 会 `server_close()`，socketio.run() 正常返回。

所有客户端后台线程均为 daemon（已核实），无既有 atexit/signal 钩子，故 srv.shutdown()
后 main 线程返回即整体退出，零阻塞。
"""
import logging
import os
import threading
import time
from contextlib import contextmanager

__all__ = [
    'mark_busy', 'is_busy',
    'note_connect', 'note_disconnect',
    'start_watcher', 'request_shutdown',
    'capture_werkzeug_server', 'start_tray',
]

# ── 可调参数 ──
IDLE_GRACE_SEC = 90      # 浏览器全部断开后，空闲多久自动退出（任务在跑时此值无效）
WATCH_INTERVAL = 5       # watcher 轮询间隔
FORCE_EXIT_AFTER = 3.0   # 请求退出后多久强退保险（仅 idle 路径，无任务在跑）

_log = logging.getLogger(__name__)

# ── 模块状态（ watcher / 托盘 / SocketIO handler 共享，_lock 保护计数类字段）──
_lock = threading.Lock()
_sync_busy = 0           # 同步引擎评估（evaluate_formula）在跑计数
_client_count = 0        # 当前 SocketIO 连接数
_idle_since = None       # monotonic ts：客户端数最后一次降到 0 的时刻
_ever_connected = False  # 至少有过一次连接（避免启动后浏览器还没开就被当闲置退掉）
_shutting_down = False   # 退出序列已启动（幂等守卫）

_server = None           # 捕获到的 werkzeug server 实例（srv.shutdown() 用）
_capture_installed = False
_icon = None             # pystray.Icon（None=无托盘）
_port = None


# ── busy 计数 ──────────────────────────────────────────────────────────────
@contextmanager
def mark_busy():
    """标记一段同步代码"在忙"。供 evaluate_formula 包裹，使无独立线程追踪的
    同步引擎调用（基准图 regen）也纳入 busy 守卫。可重入/嵌套（计数器）。"""
    global _sync_busy
    with _lock:
        _sync_busy += 1
    try:
        yield
    finally:
        with _lock:
            _sync_busy -= 1


def is_busy() -> bool:
    """是否有任何长任务在跑：训练 / 信号生成 / 同步引擎评估。"""
    # 懒导入避免循环依赖（本模块被 signal.py / app.py 早导入）
    try:
        from .train_bridge.runner import is_running as _tr
        if _tr():
            return True
    except Exception:
        pass
    try:
        from .blueprints import quant as _q
        if _q.any_signal_running():
            return True
    except Exception:
        pass
    with _lock:
        return _sync_busy > 0


# ── SocketIO 客户端计数（由 web/sockets.py 的 connect/disconnect 调用）──────
def note_connect():
    global _client_count, _idle_since, _ever_connected
    with _lock:
        _client_count += 1
        _ever_connected = True
        _idle_since = None


def note_disconnect():
    global _client_count, _idle_since
    with _lock:
        _client_count = max(0, _client_count - 1)
        if _client_count == 0:
            _idle_since = time.monotonic()


# ── watcher：空闲兜底退出 ──────────────────────────────────────────────────
def start_watcher():
    """启动空闲兜底守护线程（daemon，幂等）。仅 Windows：发布形态（pythonw 无窗口）
    才需要"关浏览器后自动退"；Linux/macOS 为前台终端运行，Ctrl+C 退出，不应被空闲
    超时打断（保持 [[client-linux-runnable]] 零行为变更）。"""
    if os.name != 'nt':
        _log.info('非 Windows 平台，跳过空闲兜底 watcher（前台运行，Ctrl+C 退出）')
        return
    t = threading.Thread(target=_watcher_loop, daemon=True, name='lifecycle-watcher')
    t.start()


def _watcher_loop():
    while True:
        time.sleep(WATCH_INTERVAL)
        try:
            if _shutting_down:
                return
            busy = is_busy()
            _update_tray(busy)
            now = time.monotonic()
            action = None
            with _lock:
                if not _ever_connected:
                    action = None                       # 浏览器还没连过，不退出
                elif _client_count > 0 or busy:
                    _idle_since = None                  # 有客户端或任务在跑，不计时
                elif _idle_since is None:
                    _idle_since = now                   # 刚进入空闲，开始计时
                elif now - _idle_since >= IDLE_GRACE_SEC:
                    action = 'shutdown'                 # 空闲超时
            if action == 'shutdown':
                request_shutdown('idle-timeout')
        except Exception:
            _log.exception('lifecycle watcher 迭代异常')


# ── 退出序列 ────────────────────────────────────────────────────────────────
def request_shutdown(reason: str = 'manual'):
    """请求关闭整个客户端进程（幂等）。托盘"退出"与 watcher 空闲超时共用。

    序列：通知前端 → 关托盘 → werkzeug srv.shutdown()（解除 main 线程阻塞）→
    daemon 定时器 FORCE_EXIT_AFTER 后 os._exit 强退保险。
    """
    global _shutting_down
    with _lock:
        if _shutting_down:
            return
        _shutting_down = True
    _log.info('客户端进程关闭中 (reason=%s)', reason)

    # 1) 通知浏览器（threading 模式下从任意线程 emit 安全）
    try:
        from .extensions import socketio as _sio
        _sio.emit('server_shutdown', {'reason': reason})
        # engine.io 长轮询：emit 会作为当前打开 long-poll 的响应立即送达；
        # 留 ~0.6s 让浏览器收到"正在退出"提示后再断开，避免无声掉线。
        # （idle-timeout 场景浏览器早已关闭 → 空 emit 无副作用。）
        time.sleep(0.6)
    except Exception:
        pass

    # 2) 托盘消失
    _stop_tray()

    # 3) 解除 main 线程 socketio.run() 阻塞
    srv = _server
    if srv is not None:
        try:
            srv.shutdown()
        except Exception:
            _log.exception('werkzeug server.shutdown 失败')

    # 4) 保险：3s 后强退（仅 idle 路径无任务在跑；正常路径 main 线程返回会更早退出）
    tmr = threading.Timer(FORCE_EXIT_AFTER, lambda: os._exit(0))
    tmr.daemon = True
    tmr.start()


# ── 捕获 werkzeug server（socketio.run 之前安装）──────────────────────────
def capture_werkzeug_server():
    """monkey-patch werkzeug.serving.make_server 捕获 server 实例，供退出时
    srv.shutdown()。socketio.stop() 在 Werkzeug3 已废，此为唯一干净退出路径。"""
    global _capture_installed
    if _capture_installed:
        return
    _capture_installed = True
    import werkzeug.serving as _ws
    _orig = _ws.make_server

    def _capturing_make_server(*args, **kwargs):
        global _server
        srv = _orig(*args, **kwargs)
        _server = srv
        return srv

    _ws.make_server = _capturing_make_server


# ── 托盘（best-effort，pystray/PIL 缺失则降级）──────────────────────────────
def start_tray(port: int):
    """启动系统托盘（仅 Windows：pystray Win32 后端可在 daemon 线程跑消息泵；
    macOS Cocoa 需主线程、Linux 需 GTK+系统包，均不稳，故非 Windows 跳过——
    Linux/macOS 前台终端运行，本就无托盘）。"""
    global _icon, _port
    if os.name != 'nt':
        _log.info('非 Windows 平台，跳过托盘（前台运行，无托盘）')
        return
    _port = port
    try:
        import pystray
    except ImportError:
        _log.info('pystray 未安装 → 托盘不可用，仅启用空闲兜底退出')
        return
    try:
        img = _make_tray_icon()
    except Exception:
        _log.exception('托盘图标生成失败 → 托盘不可用')
        return

    def _reopen(_icon, _item):
        import webbrowser
        webbrowser.open(f'http://127.0.0.1:{_port}/')

    def _quit(_icon, _item):
        request_shutdown('tray-quit')

    menu = pystray.Menu(
        pystray.MenuItem('打开浏览器', _reopen, default=True),   # default=双击触发
        pystray.Menu.SEPARATOR,
        pystray.MenuItem('退出 AlphaGPT 客户端', _quit),
    )
    icon = pystray.Icon('alphagpt-client', img, 'AlphaGPT 客户端 — 运行中', menu)
    _icon = icon
    # Win32 后端在调用线程跑消息泵；放 daemon 线程，主线程仍跑 socketio.run
    threading.Thread(target=icon.run, daemon=True, name='tray').start()


def _make_tray_icon():
    """现场生成托盘图标（蓝底 + 白色 K 线简笔），不依赖外部图片资源。"""
    from PIL import Image, ImageDraw
    s = 64
    img = Image.new('RGBA', (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([3, 3, s - 3, s - 3], radius=14, fill=(37, 99, 235, 255))
    white = (255, 255, 255, 255)
    # 三根 K 线简笔（白）：x 中心 / 上影顶 / 下影底 / 实体顶 / 实体底
    for cx, hi, lo, bt, bb in [(20, 14, 44, 20, 36),
                                (32, 10, 40, 16, 32),
                                (44, 20, 50, 26, 44)]:
        d.line([(cx, hi), (cx, lo)], fill=white, width=2)
        d.rectangle([cx - 4, bt, cx + 4, bb], fill=white)
    return img


def _update_tray(busy: bool):
    """watcher 每轮据 busy 更新托盘 tooltip（best-effort）。"""
    icon = _icon
    if icon is None:
        return
    try:
        icon.title = 'AlphaGPT 客户端 — ' + ('训练/评估中…' if busy else '运行中')
        icon.update_icon()
    except Exception:
        pass


def _stop_tray():
    icon = _icon
    if icon is None:
        return
    try:
        icon.stop()   # Win32：投递 WM_QUIT，icon.run() 返回，图标消失
    except Exception:
        pass

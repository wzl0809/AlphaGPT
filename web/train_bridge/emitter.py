# -*- coding: utf-8 -*-
"""后台线程向 WebSocket 推送的统一出口。

训练在后台线程运行，但 SocketIO.emit 需要 Flask app 上下文。
本模块在 init(app, socketio) 时持有引用，emit() 自动包 app_context。

客户端净化：所有下发给前端的 train_log / train_status 文本经 _sanitize_for_client()
清洗，屏蔽 本地文件路径 / Python 堆栈 / 异常类名 —— 分发产品不应让用户看到内部代码
结构（runner.py 已不再主动下发 traceback，本漏斗为统一兜底，覆盖所有日志源）。
"""
import re
import threading

_app = None
_sio = None
_lock = threading.Lock()

# ── 客户端净化（防内部路径 / 堆栈 / 异常结构泄露）──
# 整段 Python 堆栈 → 直接丢弃整行（runner 已不主动下发，此处为防御性兜底）
_TRACEBACK_HINT = 'Traceback (most recent call last)'
# File "D:\\...\\AlphaGPT.py"  →  File "<核心引擎>"
_FILE_FRAME_RE = re.compile(r'File\s+"[^"]*"')
# Windows 绝对路径 D:\\foo\\bar\\baz.py（前导不能是字母，避免误伤 http:// 里的 p:）
_WINPATH_RE = re.compile(r'(?<![A-Za-z])[A-Za-z]:[\\/](?:[^\s\'":]+[\\/])+[^\s\'":]+')
# 相对/Unix 路径含代码或产物后缀 core_engine/AlphaGPT.py、reports\run1\foo.pt、/home/u/x.so
_RELPATH_RE = re.compile(r'(?:[^\s\'":]+[/\\])+[^\s\'":]+\.(?:py|pyc|pyo|so|dll|pth|env|json|pt|pkl|ckpt|npz|csv|tsv|txt|log|db|sqlite|parquet|h5|feather)\b')
# CamelCase 异常类名 + (...)，如 RemoteDisconnected('...') / ConnectionError(...) / JSONDecodeError(...)
_EXC_REPR_RE = re.compile(r'\b[A-Z][A-Za-z]*(?:Error|Exception|Disconnected|Interrupted|Warning)\([^)]*\)')


def _sanitize_for_client(text):
    """净化下发文本：屏蔽路径 / 堆栈 / 异常类名。返回 None 表示整行丢弃。

    策略偏保守：只匹配明确的代码结构（File "..."、盘符路径、.py 后缀路径、
    大驼峰异常名+括号），避免误伤正常中文/行情日志。
    """
    if not isinstance(text, str) or not text:
        return text
    # 整段堆栈直接丢弃（多行 format_exc 字符串里会出现 Traceback 头 / 多个 File "）
    if _TRACEBACK_HINT in text or text.count('  File "') >= 2:
        return None
    text = _FILE_FRAME_RE.sub('File "<核心引擎>"', text)
    text = _WINPATH_RE.sub('<路径>', text)
    text = _RELPATH_RE.sub('<路径>', text)
    text = _EXC_REPR_RE.sub('<异常>', text)
    out = text.strip()
    return out or None


# ── 数据源取数过程日志：整行不下发到前端实时日志 ──
# 用户只关心训练进度，不关心 K 线/两融 的 缓存命中·未覆盖·抓取·重试·回退·限频·登录 等取数细节。
# 命中以下任一特征即丢弃整行（这些词均不出现在训练阶段日志：因子选定/训练/Sharpe/Checkpoint/IC筛选/报告）。
_DATA_SOURCE_NOISE = (
    "缓存命中", "缓存未覆盖", "缓存读取失败", "缓存已完整覆盖", "缓存损坏",
    "正在获取", "全量抓取", "Fetching Data of", "Data Ready",
    "数据源响应较慢",
    "baostock login", "tushare", "akshare 无", "szse 接口",
    "回退到 SSE", "默认 SSE", "两融",
    "adj_factor", "token 未配置", "触发分钟限频", "daily 返回空",
    "天无数据", "feat_data 构建完成",
)


def _is_data_source_noise(text):
    """是否数据源取数过程日志（缓存/抓取/回退/限频/登录/两融等）——这类不下发前端实时日志。"""
    if not isinstance(text, str):
        return False
    return any(p in text for p in _DATA_SOURCE_NOISE)


# ── 训练结果同步钩子自曝日志：整行不下发（同步要求用户无感）──
# 钩子挂载是内部机制，其状态对用户无价值；与数据源噪声同理，命中即丢弃整行。
# 如需屏蔽同源的其他自曝行（如 runner 内"模块未启用/挂载失败"），追加至此即可。
_SYNC_HOOK_NOISE = (
    "训练结果同步钩子已挂载",
    "训练结果同步模块未启用",
    "同步钩子挂载失败",
)


def init(app, socketio):
    """由 web/sockets.register_sockets 启动时调用。"""
    global _app, _sio
    with _lock:
        _app = app
        _sio = socketio


def get_app():
    """返回持有的 Flask app（后台线程取 app_context 用）。"""
    return _app


def emit(event: str, data: dict):
    """从任意线程安全推送（自动 app_context）。

    train_log.line / train_status.message 下发前经 _sanitize_for_client 净化，
    杜绝本地路径与堆栈暴露给终端用户。
    """
    if _app is None or _sio is None:
        return
    # 净化用户可见文本（防御性兜底；runner / data_source 已尽量发友好文案）
    try:
        if isinstance(data, dict):
            if event == 'train_log':
                clean = _sanitize_for_client(data.get('line'))
                if clean is None:
                    return  # 整行丢弃（如堆栈）
                if _is_data_source_noise(clean):
                    return  # 数据源取数过程日志：不下发实时日志（用户只关心训练进度）
                if any(p in clean for p in _SYNC_HOOK_NOISE):
                    return  # 同步钩子自曝日志：不下发（保持用户无感）
                data = dict(data, line=clean)
            elif event == 'train_status':
                clean = _sanitize_for_client(data.get('message'))
                if clean is not None:
                    data = dict(data, message=clean)
    except Exception:
        pass
    try:
        with _app.app_context():
            _sio.emit(event, data)
    except Exception:
        # 推送失败绝不影响训练
        pass

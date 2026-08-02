# -*- coding: utf-8 -*-
"""依赖检测：检查训练/运行所需 Python 包是否安装。"""
import importlib

# (模块名, 用途)
REQUIRED = [
    ('flask', 'Web 框架'),
    ('flask_socketio', 'WebSocket'),
    ('flask_sqlalchemy', '本地 SQLite ORM'),
    ('requests', 'HTTP / AI 调用'),
    ('torch', '训练核心（PyTorch）'),
    ('akshare', '数据源（akshare）'),
    ('baostock', '数据源（baostock）'),
    ('pandas', '数据处理'),
    ('numpy', '数值计算'),
    ('matplotlib', '图表生成'),
    ('tqdm', '进度条'),
    ('psutil', '硬件监控'),
]


# 进程内缓存：依赖安装状态在进程生命周期内不变，首次 import（torch 等大包慢）后复用，
# 避免每次开设置页都重 import 12 个包导致卡顿。force=True 强制重检（「刷新」按钮用）。
_CACHE = None


def check(force: bool = False):
    """返回 [{name, usage, ok}]。"""
    global _CACHE
    if _CACHE is not None and not force:
        return _CACHE
    out = []
    for name, usage in REQUIRED:
        try:
            importlib.import_module(name)
            out.append({'name': name, 'usage': usage, 'ok': True})
        except ImportError:
            out.append({'name': name, 'usage': usage, 'ok': False})
    _CACHE = out
    return out


def missing():
    return [m['name'] for m in check() if not m['ok']]

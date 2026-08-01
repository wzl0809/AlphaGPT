# -*- coding: utf-8 -*-
"""AlphaGPT 客户端启动器。

用法（在 client/ 目录下）：
    python run.py     # 开发：有控制台，UTF-8 包裹
    pythonw run.py    # 发布：无控制台后台运行（run.bat / start.vbs 走这条）

说明：本机 Python 为嵌入式配置（python312._pth），不会自动把当前目录加入
sys.path，因此需要本启动器显式插入项目目录后再导入 web.app。
"""
import os
import sys

# 显式把 client/ 目录加入 path（嵌入式 Python 不会自动加 cwd）
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
# 把 CWD 切到 client/：core_engine 的 reports/checkpoints/margin_balance 与 data_source 的
# kline_cache、ai/signal 的 reports 都用裸目录名（相对 CWD）写盘；而读取/索引方（common/cleanup/runner）
# 用 __file__ 锚定 <client>/。切到 _HERE 让写读双方落到同一物理目录，任意启动方式（直接 python run.py、
# 快捷方式、计划任务）与任意安装目录都能对齐。run.bat/start.vbs 本就把 CWD 设到 client/，此处不改变其行为。
os.chdir(_HERE)


class _NullStream:
    """丢弃式输出流：仅用于无法建文件时的兜底，避免 print 写 None 崩溃。"""
    def write(self, *a, **k):
        pass

    def flush(self, *a, **k):
        pass


def _redirect_stdio_to_file():
    """pythonw 无 console → 把 stdout/stderr 重定向到 logs/client_stdout.log。
    既避免第三方库 stray print 写 None 崩溃，也保留发布期可诊断性。"""
    try:
        logs_dir = os.path.join(_HERE, 'logs')
        os.makedirs(logs_dir, exist_ok=True)
        f = open(os.path.join(logs_dir, 'client_stdout.log'), 'a',
                 encoding='utf-8', buffering=1)
        sys.stdout = f
        sys.stderr = f
    except Exception:
        os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
        if sys.stdout is None:
            sys.stdout = _NullStream()
        if sys.stderr is None:
            sys.stderr = _NullStream()


def _setup_stdio():
    # 无 console（pythonw 发布形态）：重定向到文件
    if sys.stdout is None or sys.stderr is None:
        _redirect_stdio_to_file()
        return
    # Windows GBK 终端兼容：stdout/stderr 包 UTF-8（沿用 core_engine/AlphaGPT.py 做法）
    if sys.platform == 'win32':
        try:
            import io as _io
            sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
            sys.stderr = _io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
        except Exception:
            os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
    # Linux/macOS 终端：原样


_setup_stdio()

from web.app import main  # noqa: E402

if __name__ == '__main__':
    main()

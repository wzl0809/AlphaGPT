# -*- coding: utf-8 -*-
"""进程级 baostock 互斥锁。

baostock 是模块级单例 session（`baostock.login/logout/query_*` 共用全局连接），
多线程并发 login/logout 会互相打断、静默截断 K 线（数据污染且无报错）。

所有 baostock 取数区段（AlphaGPT.DataEngine.load 的 baostock 分支、
web.ai.market_breadth._fetch_indices_baostock）必须 `with baostock_lock:` 串行。

放在 core_engine（最底层、零重依赖），core 与 web 两层都能 import。
"""
import threading

baostock_lock = threading.RLock()

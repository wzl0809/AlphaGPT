# -*- coding: utf-8 -*-
"""
网络取数重试 / 超时 helper
==========================
全仓库原本在网络取数路径上「无重试、无代理、无超时」(deepseek/tavily/akshare 直调
requests.post)。本模块把 AlphaGPT.py:2399-2419 的指数退避闭包抽成通用装饰器,
并补一个 ThreadPoolExecutor 超时包装(efinance 自身无 timeout,东财断连会挂死几分钟)。

供 market_breadth / news_filter / deepseek 三处复用。
"""

import functools
import logging
import time
from typing import Callable, Tuple, Type, Any

logger = logging.getLogger(__name__)

_DEFAULT_RETRYABLE: Tuple[Type[BaseException], ...] = (Exception,)


def retry_on_network(
    max_retries: int = 2,
    base_delay: float = 1.0,
    retryable: Tuple[Type[BaseException], ...] = _DEFAULT_RETRYABLE,
    on_retry: Callable[[Exception, int], None] = None,
):
    """指数退避重试装饰器。delay = min(base_delay * 2^attempt, 60s)。

    用法::

        @retry_on_network(max_retries=3, base_delay=1.0)
        def fetch_spot(): ...
    """
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            last_exc: Exception = RuntimeError("unreached")
            for attempt in range(max_retries + 1):
                try:
                    return fn(*args, **kwargs)
                except retryable as e:  # noqa: PERF203
                    last_exc = e
                    if attempt < max_retries:
                        delay = min(base_delay * (2 ** attempt), 60.0)
                        if on_retry:
                            try:
                                on_retry(e, attempt + 1)
                            except Exception:  # noqa: BLE001
                                pass
                        else:
                            logger.warning("%s 第%d次重试(%.1fs后): %s",
                                           fn.__name__, attempt + 1, delay, e)
                        time.sleep(delay)
                    else:
                        logger.warning("%s 重试%d次仍失败: %s",
                                       fn.__name__, max_retries, e)
            raise last_exc
        return wrapper
    return decorator


def call_with_timeout(fn: Callable[..., Any], timeout: float, *args, **kwargs):
    """给同步调用加超时(用 ThreadPoolExecutor,直搬 DSA efinance_fetcher 的超时模式)。

    efinance 调东财接口自身不带 timeout,断连会挂死几分钟。本函数把它包一层,
    超时抛 ``TimeoutError``(注意是内置的,不是 concurrent.futures 的)。

    ⚠️ 必须用显式池 + ``shutdown(wait=False)``：旧版用 ``with ThreadPoolExecutor as ex``，
    超时 ``raise`` 后 ``with`` 退出会触发 ``shutdown(wait=True)``，**阻塞到挂死的 efinance
    调用真正返回**(可能 60s+)——等价于「假超时」，这正是首页大盘情绪卡顿被放大的主因。
    改为显式池 + ``wait=False``，超时即返回；挂死的 worker 是 daemon 线程(Py3.9+)会自回收。
    """
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import TimeoutError as FuturesTimeout

    ex = ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(fn, *args, **kwargs)
    try:
        return fut.result(timeout=timeout)
    except FuturesTimeout:
        name = getattr(fn, "__name__", repr(fn))
        raise TimeoutError(f"{name} 超时({timeout}s)") from None
    finally:
        ex.shutdown(wait=False)  # 不等挂死调用，否则超时形同虚设

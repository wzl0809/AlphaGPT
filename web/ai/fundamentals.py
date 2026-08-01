# -*- coding: utf-8 -*-
"""个股基本面富化（PE/PB/换手/主力资金/获利比例/筹码集中度 + 行业/ROE/毛利率）。

供 stock_context_builder.build() 合并进 ctx，喂 format_stock_prompt 的「💰 资金与基本面」表
（现 6 槽全 N/A → 本模块填真值）。fail-open：任一字段失败填 None + 标 missing，不阻断主分析。

数据源（全公开 HTTP，无逆向）：
- efinance get_base_info：pe(动)/pb/行业/ROE/毛利率/净利率/净利润/总市值（一次拿多）
- efinance get_latest_quote：换手率
- akshare stock_individual_fund_flow：主力净流入（东财，东财单点）
- akshare stock_cyq_em：获利比例/90集中度/平均成本（东财，东财单点）
- tushare daily_basic（有 token 且积分够）：pe_ttm/pb/turnover 精修（独立 token-bucket 1次/3600s）

⚠️ 诚实降级（对抗审查证伪，见 docs/14 §2）：efinance+akshare 同走东财端点，"逐字段降级"是
伪冗余——东财风控时 main_flow/profit_ratio/concentration（东财单点）同步 N/A，pe/pb/行业随
efinance 同源挂。无真独立兜底，靠 fund_missing 清单 + prompt 数据边界块诚实暴露（禁止编造）。
不 import AlphaGPT（与 stock_context_builder 同理，避免 _engine_lock 训练互斥）。
"""
import logging
import math
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 模块级 TTL 缓存：code -> (fetched_ts, ctx)。基本面慢变，30min 内复用，避免每次点击 4-5 路 HTTP。
_CACHE: Dict[str, tuple] = {}
_CACHE_TTL = 1800  # 30 分钟

# tushare daily_basic 独立 token-bucket（dev token 1次/小时积分墙，不抢 K线 tushare 兜底配额）
_TS_DAILY_BASIC_LAST = 0.0
_TS_DAILY_BASIC_GAP = 3600  # 1 次/3600s

# efinance get_base_info 列 → ctx 键（数值类）
_BASE_INFO_NUM = {"市盈率(动)": "pe", "市净率": "pb", "ROE": "roe",
                  "毛利率": "gross_margin", "净利率": "net_margin"}


def _to_float(v) -> Optional[float]:
    try:
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return None


def _retry(fn, *, timeout: float = 15.0, retries: int = 3, base_delay: float = 1.0):
    """带超时+重试调用（东财 push2 间歇 RemoteDisconnected，重试能扛过去）。"""
    import time
    from ._net import call_with_timeout
    last = None
    for attempt in range(retries):
        try:
            return call_with_timeout(fn, timeout)
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < retries - 1:
                time.sleep(base_delay * (attempt + 1))
    raise last if last else RuntimeError("_retry 无异常但失败")


def _market_code(code: str) -> str:
    return "sh" if str(code).startswith(("6", "51")) else "sz"


def _fetch_efinance_base(code: str, ctx: Dict, sources: Dict[str, str]):
    """efinance get_base_info：pe/pb/行业/ROE/毛利率/净利率（一次拿多，无 token 基线主源）。"""
    try:
        import efinance as ef
        bi = _retry(lambda: ef.stock.get_base_info([code]))
        if bi is None or len(bi) == 0:
            return
        row = bi.iloc[0]
        for src_col, ctx_key in _BASE_INFO_NUM.items():
            if ctx_key in ctx:   # quote 已填的(pe)不覆盖
                continue
            v = _to_float(row.get(src_col))
            if v is not None:
                ctx[ctx_key] = v
                sources[ctx_key] = "efinance"
        industry = row.get("所处行业")
        if industry:
            ctx["industry"] = str(industry)
            sources["industry"] = "efinance"
        if "pe" in ctx and "pe_basis" not in sources:
            sources["pe_basis"] = "动"   # efinance 给动态市盈率（非 TTM）
    except Exception as e:  # noqa: BLE001
        logger.warning("fundamentals get_base_info 失败 %s: %s", code, e)


def _fetch_efinance_quote(code: str, ctx: Dict, sources: Dict[str, str]):
    """efinance get_latest_quote（走 push2，比 push2his 稳）：PE(动态市盈率) + 换手率。主源（最稳）。

    push2his 风控挂 get_base_info/akshare 时，push2 这路常仍可用 → 保住 PE+换手率两槽。
    """
    try:
        import efinance as ef
        q = _retry(lambda: ef.stock.get_latest_quote([code]))
        if q is None or len(q) == 0:
            return
        row = q.iloc[-1]
        pe = _to_float(row.get("动态市盈率"))
        tr = _to_float(row.get("换手率"))
        if pe is not None and "pe" not in ctx:
            ctx["pe"] = pe
            sources["pe"] = "efinance"
            sources["pe_basis"] = "动"
        if tr is not None:
            ctx["turnover_rate"] = tr
            sources["turnover_rate"] = "efinance"
    except Exception as e:  # noqa: BLE001
        logger.warning("fundamentals get_latest_quote 失败 %s: %s", code, e)


def _fetch_akshare_mainflow(code: str, ctx: Dict, sources: Dict[str, str]):
    """akshare 主力净流入-净额（东财 push2his，东财单点；→ 亿元）。"""
    try:
        import akshare as ak
        ff = _retry(lambda: ak.stock_individual_fund_flow(stock=code, market=_market_code(code)))
        if ff is None or len(ff) == 0 or "主力净流入-净额" not in ff.columns:
            return
        mf = _to_float(ff.iloc[-1].get("主力净流入-净额"))
        if mf is not None:
            ctx["main_flow"] = round(mf / 1e8, 2)   # 元 → 亿
            sources["main_flow"] = "akshare_em_only"
    except Exception as e:  # noqa: BLE001
        logger.warning("fundamentals fund_flow 失败 %s: %s", code, e)


def _fetch_akshare_chip(code: str, ctx: Dict, sources: Dict[str, str]):
    """akshare 筹码分布（东财，东财单点）：获利比例/90集中度/平均成本（→ 百分比）。"""
    try:
        import akshare as ak
        cyq = _retry(lambda: ak.stock_cyq_em(symbol=code, adjust="qfq"))
        if cyq is None or len(cyq) == 0:
            return
        row = cyq.iloc[-1]
        pr = _to_float(row.get("获利比例"))
        cc = _to_float(row.get("90集中度"))
        ac = _to_float(row.get("平均成本"))
        if pr is not None:
            ctx["profit_ratio"] = round(pr * 100, 2)   # → %
            sources["profit_ratio"] = "akshare_em_only"
        if cc is not None:
            ctx["concentration"] = round(cc * 100, 2)   # → %
            sources["concentration"] = "akshare_em_only"
        if ac is not None:
            ctx["avg_cost"] = ac
    except Exception as e:  # noqa: BLE001
        logger.warning("fundamentals cyq 失败 %s: %s", code, e)


def _tushare_refine(code: str, ctx: Dict, sources: Dict[str, str]):
    """tushare daily_basic 精修 pe_ttm/pb/turnover（有 token 且过 token-bucket 才跑）。

    dev token daily_basic ~1次/小时积分墙；用独立 token-bucket 隔离，命中墙即静默 skip
    （efinance 基线已在），绝不 sleep 阻塞、绝不与 K线 tushare 兜底争配额。
    """
    global _TS_DAILY_BASIC_LAST
    import os
    token = os.environ.get("TUSHARE_TOKEN") or ""
    if not token:
        return
    if time.time() - _TS_DAILY_BASIC_LAST < _TS_DAILY_BASIC_GAP:
        return   # 积分墙内 skip
    try:
        import tushare as ts
        ts_code = code + (".SH" if str(code).startswith(("6", "51")) else ".SZ")
        from datetime import date
        df = ts.pro_api().daily_basic(ts_code=ts_code, trade_date=date.today().strftime("%Y%m%d"))
        _TS_DAILY_BASIC_LAST = time.time()
        if df is None or len(df) == 0:
            return   # 周末/节假日/积分不足空 → 保留 efinance 基线
        row = df.iloc[0]
        pe_ttm = _to_float(row.get("pe_ttm"))
        pb = _to_float(row.get("pb"))
        tr = _to_float(row.get("turnover_rate"))
        if pe_ttm is not None:
            ctx["pe"] = pe_ttm; sources["pe"] = "tushare"; sources["pe_basis"] = "TTM"
        if pb is not None:
            ctx["pb"] = pb; sources["pb"] = "tushare"
        if tr is not None:
            ctx["turnover_rate"] = tr; sources["turnover_rate"] = "tushare"
    except Exception as e:  # noqa: BLE001
        logger.info("tushare daily_basic 精修跳过 %s: %s", code, str(e)[:80])


def fetch(stock_code: str, stock_name: str = "") -> Dict[str, Any]:
    """取基本面，返回 ctx 增量 dict。

    keys: pe/pb/turnover_rate/main_flow/profit_ratio/concentration/industry/roe/gross_margin/
          net_margin/avg_cost + pe_basis(动/TTM/None) + fund_sources + fund_missing。
    任一字段失败不抛异常（fail-open）。
    """
    code = str(stock_code)
    cached = _CACHE.get(code)
    if cached and (time.time() - cached[0] < _CACHE_TTL):
        return dict(cached[1])   # 命中缓存，复用

    ctx: Dict[str, Any] = {}
    sources: Dict[str, str] = {}
    # 顺序取：quote(push2 最稳，先填 PE+换手率) → base(push2his 富化 PB/行业/ROE，pe 不覆盖) → 资金/筹码(东财单点)
    _fetch_efinance_quote(code, ctx, sources)
    _fetch_efinance_base(code, ctx, sources)
    _fetch_akshare_mainflow(code, ctx, sources)
    _fetch_akshare_chip(code, ctx, sources)
    _tushare_refine(code, ctx, sources)

    missing: List[str] = [k for k in ("pe", "pb", "turnover_rate", "main_flow",
                                      "profit_ratio", "concentration", "industry")
                          if k not in ctx]
    ctx["pe_basis"] = sources.get("pe_basis")
    ctx["fund_sources"] = sources
    ctx["fund_missing"] = missing
    # 仅当数据较全（≤3 缺失）才缓存，避免把一次东财抖动的半空结果缓存 30min
    if len(missing) <= 3:
        _CACHE[code] = (time.time(), dict(ctx))
    return ctx

# -*- coding: utf-8 -*-
"""个股上下文构建器(Phase 4)。

为量化页个股分析拼装 K线/均线/乖离率/量能,纯 numpy/pandas 实现
(不 import AlphaGPT,避免 _engine_lock 训练互斥 + 全局态耦合)。
喂给 prompts.stock_decision.format_stock_prompt。

数据缺失(akshare 被拦/代码错)时字段填 None + data_missing=True,
format_stock_prompt 会注入"数据缺失,严禁编造"警告。
"""

import logging
from datetime import date, timedelta
from typing import Any, Dict

logger = logging.getLogger(__name__)


def _fetch_kline(code: str, days: int = 60):
    """取近 N 日日K(qfq)，走本地缓存多源模块 data_source（命中缓存零网络，否则
    akshare→baostock→tushare 三级回退）。返回 DataFrame 或 None。

    data_source 是 core_engine 轻量模块（不 import AlphaGPT / torch），与本文件
    "避免 _engine_lock 训练互斥 + 全局态耦合" 的定位一致。"""
    from data_source import get_kline
    end = date.today().strftime("%Y%m%d")
    start = (date.today() - timedelta(days=days * 2)).strftime("%Y%m%d")  # 多取缓冲
    try:
        return get_kline(code, start, end, adjust='qfq')
    except Exception as e:  # noqa: BLE001
        logger.warning("kline 取数失败 %s: %s", code, e)
        return None


def build(stock_code: str, stock_name: str = "", days: int = 60) -> Dict[str, Any]:
    """构建个股上下文 dict。

    返回 {code, name, close, open, high, low, pct_chg, volume, amount,
          ma5, ma10, ma20, bias_ma5, data_missing,
          pe, pb, turnover_rate, main_flow, profit_ratio, concentration,  # 基本面(自 fund)
          industry, roe, gross_margin, net_margin, avg_cost,
          pe_basis, fund_sources, fund_missing}。失败字段填 None / fund_missing 标注。
    """
    import pandas as pd
    ctx: Dict[str, Any] = {"code": stock_code, "name": stock_name or stock_code}
    # 基本面富化（PE/PB/换手/主力资金/获利比例/筹码集中度 + 行业/ROE/毛利率；不依赖 K线，fail-open）。
    # 喂 format_stock_prompt 的「💰 资金与基本面」表（原 6 槽全 N/A）。东财同源降级见 fundamentals.py docstring。
    try:
        from . import fundamentals as _fund
        ctx.update(_fund.fetch(stock_code, stock_name))
    except Exception as e:  # noqa: BLE001
        logger.warning("fundamentals 富化失败 %s: %s", stock_code, e)
        ctx["fund_missing"] = ["pe", "pb", "turnover_rate", "main_flow",
                               "profit_ratio", "concentration", "industry"]
    df = _fetch_kline(stock_code, days)
    none_fields = ["close", "open", "high", "low", "pct_chg", "volume",
                   "amount", "ma5", "ma10", "ma20", "bias_ma5"]
    if df is None or len(df) == 0:
        ctx.update({k: None for k in none_fields})
        ctx["data_missing"] = True
        return ctx

    # 列名兼容(akshare 中文 / 通用英文)
    def _pick(*names):
        for n in names:
            if n in df.columns:
                return n
        return None

    close_c = _pick("收盘", "close")
    if close_c is None:
        ctx.update({k: None for k in none_fields})
        ctx["data_missing"] = True
        return ctx

    last = df.iloc[-1]
    closes = pd.to_numeric(df[close_c], errors="coerce")

    def _num(row, col):
        if col is None:
            return None
        v = pd.to_numeric(row.get(col), errors="coerce")
        return None if pd.isna(v) else float(v)

    ctx["close"] = _num(last, close_c)
    ctx["open"] = _num(last, _pick("开盘", "open"))
    ctx["high"] = _num(last, _pick("最高", "high"))
    ctx["low"] = _num(last, _pick("最低", "low"))
    ctx["pct_chg"] = _num(last, _pick("涨跌幅", "pct_chg"))
    ctx["volume"] = _num(last, _pick("成交量", "volume"))
    ctx["amount"] = _num(last, _pick("成交额", "amount"))

    # 均线 + 乖离率
    ma5 = closes.tail(5).mean()
    ma10 = closes.tail(10).mean()
    ma20 = closes.tail(20).mean()
    ctx["ma5"] = float(ma5) if pd.notna(ma5) else None
    ctx["ma10"] = float(ma10) if pd.notna(ma10) else None
    ctx["ma20"] = float(ma20) if pd.notna(ma20) else None
    if ctx["close"] and ctx["ma5"]:
        ctx["bias_ma5"] = round((ctx["close"] - ctx["ma5"]) / ctx["ma5"] * 100, 2)
    else:
        ctx["bias_ma5"] = None
    ctx["data_missing"] = False
    return ctx

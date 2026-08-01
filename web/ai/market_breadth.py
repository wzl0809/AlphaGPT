# -*- coding: utf-8 -*-
"""
大盘情绪三维度打分(market breadth)
====================================
从 daily_stock_analysis (src/market_analyzer.py:1248 _build_market_light_scores
+ efinance_fetcher.py:1012 涨跌停精确算法) 移植。

用真实行情指标替代旧的「tavily 新闻中文词频计数」(那是「一直平」的根因)。
三维度确定性打分,纯本地计算,不依赖 LLM/新闻:

    breadth = 上涨家数 / (上涨+下跌) × 100           # 市场广度
    index   = clamp(50 + 主要指数平均涨跌幅 × 12, 0, 100)  # 指数强度
    limit   = 涨停 / (涨停+跌停) × 100               # 涨跌停结构
    score   = round(breadth × 0.45 + index × 0.35 + limit × 0.20)

任一维取数失败 → 默认 50 分 + available=False + data_quality 降级。

数据源(2026-07-22 重构，解决东财 push2 ``clist/get`` 批量接口被拦 RemoteDisconnected)：
  breadth/limit: legu(乐股网,~0.65s 直取涨跌家数+涨跌停,非东财,主) → 失败回退
    spot 并发竞速[efinance + akshare_em(东财) + akshare_sina(新浪)],自算。
  index: akshare_sina(新浪指数,~1.2s,主) → akshare_em(东财) → baostock(EOD 兜底)。
efinance 与 akshare_em 同走东财(push2)会一起挂；真正异构兜底是 legu + 新浪(非东财)。
DSA(daily_stock_analysis) 的 AkshareFetcher 已内置东财→新浪 fallback，本重构借鉴之并加 legu 快路径。
"""

import logging
import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Optional

from . import _net

logger = logging.getLogger(__name__)

EFINANCE_TIMEOUT = 15.0  # 东财源(efinance/akshare_em)超时；东财被拦时 ~9s 即 RemoteDisconnected 快失败。
# 新浪 spot 慢(15-25s)，是东财全挂时的真兜底；仅 legu 快路径失败后才走 spot 竞速，故放宽。
SPOT_RACE_TIMEOUT = EFINANCE_TIMEOUT + 15  # 30s，足够新浪 spot 完成。
LEGU_TIMEOUT = 8.0  # legu 市场活跃度 + 新浪指数(均非东财，~0.65s/1.2s)。

# 主要指数(akshare/baostock 代码 → 显示名)
_INDEX_CODES = {
    "sh000001": "上证指数",
    "sz399001": "深证成指",
    "sz399006": "创业板指",
}


def is_beijing_trading_session() -> bool:
    """当前是否 A 股交易时段（北京时间 周一至五 9:30-11:30 / 13:00-15:00）。

    用 UTC+8 显式计算，不依赖机器时区。供 baostock EOD 兜底守卫与首页缓存 TTL 共用。
    """
    now_bj = datetime.utcnow() + timedelta(hours=8)
    if now_bj.weekday() >= 5:
        return False
    hm = now_bj.hour * 60 + now_bj.minute
    return (570 <= hm <= 690) or (780 <= hm <= 900)  # 9:30-11:30 / 13:00-15:00


# ---------- 板块涨跌停比例 ----------
def _limit_ratio_for_code(code: str) -> float:
    """根据 A 股代码判断涨跌停比例: 主板 10% / 创业板·科创 20% / 北证 30%。"""
    digits = "".join(c for c in str(code) if c.isdigit())
    if not digits:
        return 0.10
    if digits.startswith(("300", "301")):
        return 0.20  # 创业板
    if digits.startswith(("688", "689")):
        return 0.20  # 科创板
    if digits.startswith(("8", "43", "92")):
        return 0.30  # 北证(83/87/92/43 开头)
    return 0.10  # 主板


# ---------- 数据结构 ----------
@dataclass
class MarketSnapshot:
    """大盘情绪快照(build_snapshot 的返回)。"""

    score: int                                 # 0-100 总分
    status: str                                # green(>=60)/yellow(40-59)/red(<40)
    temperature_label: str                     # 强势(>=70)/偏暖(>=55)/震荡(>=40)/偏弱(<40)
    breadth_effect: str = ""                   # 赚钱效应扩散/亏钱效应较强/市场分化(广度解读标签)
    turnover_label: str = ""                   # 高活跃度/中等活跃/缩量观望(成交额解读标签)
    reasons: List[str] = field(default_factory=list)
    dimensions: Dict[str, Any] = field(default_factory=dict)  # {breadth/index/limit: {score, available}}
    data_quality: str = "unavailable"          # ok/partial/unavailable
    trade_date: str = ""
    up_count: int = 0
    down_count: int = 0
    flat_count: int = 0
    limit_up_count: int = 0
    limit_down_count: int = 0
    index_changes: Dict[str, float] = field(default_factory=dict)
    total_amount_yi: Optional[float] = None    # 两市成交额(亿元)
    index_table: List[Dict[str, Any]] = field(default_factory=list)   # P4:[{name,code,current,change_pct}]
    sector_rankings: Optional[Dict[str, Any]] = None                  # P4:{industry_top5/bottom5,concept_top5/bottom5}

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------- 列名容错 helper ----------
def _col(df, *names):
    """从 DataFrame 取第一个存在的列(兼容 efinance/akshare 中英文列名)。"""
    cols = set(df.columns)
    for n in names:
        if n in cols:
            return n
    return None


def _to_float(v) -> Optional[float]:
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


# ---------- 取数: legu 直取涨跌家数+涨跌停(快路径,非东财) ----------
def _fetch_breadth_legu() -> Optional[Dict[str, int]]:
    """legu 乐股网市场活跃度(``ak.stock_market_activity_legu``，~0.65s，**非东财、稳定**)。
    直接给涨跌家数+涨跌停，无需抓 5000 股自算 → 东财被拦时的**快路径主源**。
    用"真实涨停/真实跌停"(排除 ST/一字板)比按比例自算更准；无则回落"涨停/跌停"。
    返回 {up,down,flat,limit_up,limit_down} 或 None。
    """
    import akshare as ak
    try:
        df = _net.call_with_timeout(ak.stock_market_activity_legu, LEGU_TIMEOUT)
    except Exception as e:  # noqa: BLE001
        logger.warning("legu 市场活跃度取数失败: %s", e)
        return None
    if df is None or len(df) == 0:
        return None
    item_col = _col(df, "item", "项目")
    val_col = _col(df, "value", "值")
    if not (item_col and val_col):
        return None
    m: Dict[str, Optional[float]] = {}
    for _, row in df.iterrows():
        m[str(row[item_col]).strip()] = _to_float(row[val_col])

    def _gi(key: str) -> Optional[int]:
        v = m.get(key)
        return int(v) if v is not None else None

    up = _gi("上涨")
    down = _gi("下跌")
    if up is None or down is None:
        return None
    flat = _gi("平盘") or 0
    limit_up = _gi("真实涨停")
    if limit_up is None:
        limit_up = _gi("涨停") or 0
    limit_down = _gi("真实跌停")
    if limit_down is None:
        limit_down = _gi("跌停") or 0
    return {"up": up, "down": down, "flat": flat,
            "limit_up": limit_up, "limit_down": limit_down}


# ---------- 取数: 全市场 spot(回退路径,用于 breadth/limit/成交额) ----------
def _fetch_spot_efinance():
    """efinance 全市场实时(东财 push2，~1-9s，东财正常时最快)。"""
    import efinance as ef
    return _net.call_with_timeout(ef.stock.get_realtime_quotes, EFINANCE_TIMEOUT)


def _fetch_spot_em():
    """akshare 东财实时(``stock_zh_a_spot_em``，东财 push2)。efinance 的同源竞速伙伴。"""
    import akshare as ak
    return _net.call_with_timeout(ak.stock_zh_a_spot_em, EFINANCE_TIMEOUT)


def _fetch_spot_sina():
    """akshare 新浪实时(``stock_zh_a_spot``，~15-25s 慢但**非东财、稳定**)。
    列名(代码/涨跌幅/昨收/最新价/成交额)与东财一致，下游 _score_breadth/_count_limits/
    _total_amount_yi 的 _col() 模糊匹配**零改动可用**。东财被拦时的真兜底。
    """
    import akshare as ak
    return _net.call_with_timeout(ak.stock_zh_a_spot, SPOT_RACE_TIMEOUT)


def _fetch_spot():
    """全市场 spot 并发竞速: efinance + 东财akshare + **新浪**，首个非空 DataFrame 胜出。

    东财正常时 efinance/em ~1-9s 即胜(含成交额)；东财被拦时两东财源 ~9s RemoteDisconnected
    快失败，**新浪 ~15-25s 兜底胜出**。legu 快路径(直取涨跌家数)失败后才走这里，故慢点可接受。
    显式池 + ``shutdown(wait=False)``：落败/挂死的源不等它收尾(挂死 worker 是 daemon 自回收)。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from concurrent.futures import TimeoutError as FuturesTimeout

    ex = ThreadPoolExecutor(max_workers=3)
    futs = [ex.submit(_fetch_spot_efinance), ex.submit(_fetch_spot_em), ex.submit(_fetch_spot_sina)]
    try:
        for fut in as_completed(futs, timeout=SPOT_RACE_TIMEOUT + 2):
            try:
                df = fut.result()
            except Exception as e:  # noqa: BLE001
                logger.warning("spot 取数失败: %s", e)
                continue
            if df is not None and len(df) > 0:
                return df  # 首个非空胜出
    except FuturesTimeout:
        logger.warning("spot 并发取数整体超时(%.0fs)", SPOT_RACE_TIMEOUT)
    finally:
        ex.shutdown(wait=False)
    return None


# ---------- 取数: 主要指数涨跌幅 ----------
def _indices_from_name_rows(df) -> Optional[Dict[str, float]]:
    """从"名称/涨跌幅"两列的指数表里挑 上证指数/深证成指/创业板指。新浪/东财通用。"""
    name_col = "名称" if "名称" in df.columns else None
    pct_col = "涨跌幅" if "涨跌幅" in df.columns else None
    if not (name_col and pct_col) or len(df) == 0:
        return None
    want = {"上证指数", "深证成指", "创业板指"}
    out: Dict[str, float] = {}
    for _, row in df.iterrows():
        nm = str(row[name_col]).strip()
        if nm in want:
            pct = _to_float(row[pct_col])
            if pct is not None:
                out[nm] = pct
    return out if out else None


def _market_amount_from_rows(df) -> Optional[float]:
    """从指数表「成交额」列取两市成交额(亿元)：上证 sh000001(沪市) + 深证Ａ指 sz399107(深市全A)。

    新浪指数表的 成交额 字段=该指数对应市场的总成交额（实测 399001 深证成指 ≈ 399107 深证Ａ指
    ≈ 深市全市场——新浪把市场总额挂到指数上，故成指不会低估深市）。sh000001+sz399107 求和=两市。
    无 399107 时回落 sz399001。"""
    amt_col = _col(df, "成交额", "amount")
    code_col = _col(df, "代码", "code")
    if not (amt_col and code_col) or len(df) == 0:
        return None
    amt: Dict[str, float] = {}
    for _, row in df.iterrows():
        c = str(row[code_col]).strip().lower()
        if c in ("sh000001", "sz399107", "sz399001"):
            v = _to_float(row[amt_col])
            if v is not None:
                amt[c] = v
    sh = amt.get("sh000001")
    sz = amt.get("sz399107") or amt.get("sz399001")   # 深证Ａ指优先，回落深证成指(≈深市total)
    if sh is None or sz is None or (sh + sz) <= 0:
        return None
    return round((sh + sz) / 1e8, 0)   # 元 → 亿元


def _index_table_from_rows(df) -> List[Dict[str, Any]]:
    """从指数表建 index_table：[{name, code, current, change_pct}]（上证/深证成指/创业板指）。

    供 _market.html 渲染 DSA 式指数 emoji 表（🟢/🔴/⚪ + 最新价 + 涨跌幅）。
    """
    name_col = _col(df, "名称")
    code_col = _col(df, "代码")
    cur_col = _col(df, "最新价")
    pct_col = _col(df, "涨跌幅")
    if not (name_col and pct_col) or len(df) == 0:
        return []
    want = {"上证指数", "深证成指", "创业板指"}
    out: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        nm = str(row[name_col]).strip()
        if nm in want:
            out.append({"name": nm,
                        "code": str(row[code_col]).strip() if code_col else "",
                        "current": _to_float(row[cur_col]) if cur_col else None,
                        "change_pct": _to_float(row[pct_col])})
    return out


def _fetch_indices_sina() -> tuple:
    """新浪指数实时(``stock_zh_index_spot_sina``，~1.2s，**非东财、稳定**)：主源。

    返回 ``(index_changes_dict, amount_yi, index_table)``：顺带取两市成交额(上证+深证Ａ指 sum)
    + 指数表(最新价+涨跌幅)，供 legu 快路径补 total_amount 与 DSA 式指数表，零额外网络。
    """
    import akshare as ak
    try:
        df = _net.call_with_timeout(ak.stock_zh_index_spot_sina, LEGU_TIMEOUT)
    except Exception as e:  # noqa: BLE001
        logger.warning("stock_zh_index_spot_sina 失败: %s", e)
        return None, None, []
    return _indices_from_name_rows(df), _market_amount_from_rows(df), _index_table_from_rows(df)


def _fetch_indices_em() -> tuple:
    """东财指数实时(``stock_zh_index_spot_em``，东财 push2)：新浪失败时回退。

    返回 ``(index_changes_dict, None, index_table)``（amount 走 sina；em 也给 index_table）。
    """
    import akshare as ak
    try:
        df = ak.stock_zh_index_spot_em(symbol="沪深重要指数")
    except Exception as e:  # noqa: BLE001
        logger.warning("stock_zh_index_spot_em 失败,将走 baostock 兜底: %s", e)
        return None, None, []
    return _indices_from_name_rows(df), None, _index_table_from_rows(df)


def _fetch_indices_baostock() -> Optional[Dict[str, float]]:
    """baostock 兜底(真异构源),取指数近 30 日算当日涨跌幅。"""
    if is_beijing_trading_session():
        # 盘中 baostock 只有「昨日收盘」EOD，灌进评分会被当今日实时 → 误导（2026-07-21
        # 实盘事故：缓存的 上证+0.85% 实为 07-20 收盘 pctChg，被误判为今日震荡）。
        # 盘中禁用，让指数维度诚实降级；非交易时段 EOD==最新收盘==正确，保留兜底。
        logger.info("盘中时段，跳过 baostock EOD 指数兜底（避免昨日数据冒充今日）")
        return None
    try:
        import baostock as bs
    except ImportError:
        return None
    out: Dict[str, float] = {}
    bs_code_map = {"上证指数": "sh.000001", "深证成指": "sz.399001", "创业板指": "sz.399006"}
    today = date.today().strftime("%Y-%m-%d")
    start = (date.today() - timedelta(days=30)).strftime("%Y-%m-%d")
    from baostock_lock import baostock_lock
    baostock_lock.acquire()
    try:
        bs.login()
        for name, bscode in bs_code_map.items():
            rs = bs.query_history_k_data_plus(
                bscode, "date,code,preclose,close,pctChg",
                start_date=start, end_date=today, frequency="d")
            rows = []
            while (rs.error_code == "0") and rs.next():
                rows.append(rs.get_row_data())
            if rows:
                last = rows[-1]
                pct = _to_float(last[4]) if len(last) > 4 else None
                if pct is None and len(last) >= 4:
                    pre, cls = _to_float(last[2]), _to_float(last[3])
                    if pre and pre > 0 and cls is not None:
                        pct = (cls - pre) / pre * 100
                if pct is not None:
                    out[name] = pct
    except Exception as e:  # noqa: BLE001
        logger.warning("baostock 指数取数失败: %s", e)
    finally:
        try:
            bs.logout()
        except Exception:  # noqa: BLE001
            pass
        baostock_lock.release()
    return out if out else None


@_net.retry_on_network(max_retries=2, base_delay=1.0)
def _fetch_indices() -> tuple:
    """主要指数涨跌幅 + 两市成交额(亿元) + 指数表: 新浪(主,非东财) → 东财 → baostock(EOD 兜底)。

    返回 ``(index_changes_dict, amount_yi, index_table)``。amount/index_table 仅 df 源(sina/em)返回。
    """
    for fn in (_fetch_indices_sina, _fetch_indices_em, _fetch_indices_baostock):
        try:
            r = fn()
            if r is None:
                continue
            if isinstance(r, tuple):
                pct, amt, itbl = r          # sina/em 都 3-tuple
            else:
                pct, amt, itbl = r, None, []   # baostock 只返 dict
            if pct:
                return pct, amt, itbl
        except Exception as e:  # noqa: BLE001
            logger.warning("指数取数失败 %s: %s", getattr(fn, "__name__", fn), e)
    return None, None, []


def _fetch_sector_rankings() -> Optional[Dict[str, Any]]:
    """板块领涨/领跌 top5/bottom5（akshare 东财，fail-open，P4）。

    用 ``stock_board_industry_name_em`` / ``stock_board_concept_name_em``（板块涨跌幅排名，
    ~100 行直接给，**非** pytdx get_block_info 的 139 万静态成员关系——那是错误工具）。
    东财 push2 同源被拦则返 None，不影响 build_snapshot 评分/prompt。
    """
    import akshare as ak
    out: Dict[str, Any] = {}
    for key, fn_name in [("industry", "stock_board_industry_name_em"),
                         ("concept", "stock_board_concept_name_em")]:
        try:
            fn = getattr(ak, fn_name, None)
            if fn is None:
                continue
            df = _net.call_with_timeout(fn, LEGU_TIMEOUT)
            if df is None or len(df) == 0:
                continue
            pct_col = _col(df, "涨跌幅", "change_pct")
            name_col = _col(df, "板块名称", "名称", "name")
            if not (pct_col and name_col):
                continue
            import pandas as _pd
            sub = df[[name_col, pct_col]].copy()
            sub[pct_col] = sub[pct_col].apply(_to_float)
            sub = sub.dropna(subset=[pct_col]).sort_values(pct_col, ascending=False)
            top = [{"name": str(r[name_col]).strip(), "change_pct": r[pct_col]}
                   for _, r in sub.head(5).iterrows()]
            bot = [{"name": str(r[name_col]).strip(), "change_pct": r[pct_col]}
                   for _, r in sub.tail(5).sort_values(pct_col).iterrows()]
            out[f"{key}_top5"] = top
            out[f"{key}_bottom5"] = bot
        except Exception as e:  # noqa: BLE001
            logger.warning("板块排名取数失败 %s: %s", fn_name, e)
    return out if out else None


# ---------- 三维度打分(直搬 market_analyzer.py:1248) ----------
def _breadth_score(up: int, down: int) -> int:
    """上涨家数占比 → 0-100。up+down=0 → 50(不可用)。legu 路径与 spot 路径共用此公式。"""
    participants = up + down
    if participants <= 0:
        return 50
    return int(up / participants * 100)


def _score_breadth(df) -> tuple:
    """上涨家数占比 → 0-100。返回 (score, available, up, down, flat)。"""
    pct_col = _col(df, "涨跌幅", "change_pct", "pct_chg")
    if not pct_col:
        return 50, False, 0, 0, 0
    pcts = df[pct_col].apply(_to_float)
    pcts = pcts.dropna()
    up = int((pcts > 0).sum())
    down = int((pcts < 0).sum())
    flat = int((pcts == 0).sum())
    if up + down == 0:
        return 50, False, up, down, flat
    return _breadth_score(up, down), True, up, down, flat


def _count_limits(df) -> tuple:
    """精确统计涨停/跌停家数(向量化,直搬 efinance_fetcher.py:1012-1035 算法)。

    主板10%/创业科创20%/北证30%。limit_price=floor(昨收×(1±比例)×100+0.5)/100 + 容差。
    向量化避免 iterrows 5000 行 Python 循环(原版 20s+ → 毫秒级)。
    """
    import numpy as np
    import pandas as pd
    code_col = _col(df, "代码", "股票代码", "code")
    pct_col = _col(df, "涨跌幅", "change_pct", "pct_chg")
    pre_col = _col(df, "昨收", "昨日收盘", "pre_close")
    price_col = _col(df, "最新价", "现价", "close", "price")
    if not (code_col and pct_col and pre_col and price_col):
        return 0, 0, False
    pre = pd.to_numeric(df[pre_col], errors="coerce")
    cur = pd.to_numeric(df[price_col], errors="coerce")
    pct = pd.to_numeric(df[pct_col], errors="coerce")
    digits = df[code_col].astype(str).str.replace(r"\D", "", regex=True)
    # 按代码前缀算涨跌停比例(向量化: 创业板/科创20%, 北证30%, 其余主板10%)
    ratio = np.where(digits.str.startswith(("300", "301", "688", "689")), 0.20,
            np.where(digits.str.startswith(("8", "43", "92")), 0.30, 0.10))
    valid = ((pre > 0) & (cur > 0) & pct.notna()).values
    pre_v, cur_v, pct_v = pre.values, cur.values, pct.values
    limit_up_price = np.floor(pre_v * (1 + ratio) * 100 + 0.5) / 100.0
    limit_down_price = np.floor(pre_v * (1 - ratio) * 100 + 0.5) / 100.0
    tol = np.abs(pre_v * (1 + ratio) - limit_up_price) + 0.001
    is_up = valid & ((np.abs(cur_v - limit_up_price) <= tol) | (pct_v >= ratio * 100 * 0.985))
    is_down = valid & ((np.abs(cur_v - limit_down_price) <= tol) | (pct_v <= -ratio * 100 * 0.985))
    return int(is_up.sum()), int(is_down.sum()), True


def _score_index(index_changes: Optional[Dict[str, float]]) -> tuple:
    """主要指数平均涨跌幅 → clamp(50 + avg×12, 0, 100)。返回 (score, available)。"""
    if not index_changes:
        return 50, False
    vals = [v for v in index_changes.values() if v is not None]
    if not vals:
        return 50, False
    avg = sum(vals) / len(vals)
    return int(max(0, min(100, 50 + avg * 12))), True


def _total_amount_yi(df) -> Optional[float]:
    """两市成交额(亿元)。"""
    amt_col = _col(df, "成交额", "amount")
    if not amt_col:
        return None
    total = df[amt_col].apply(_to_float).sum()
    if total is None or total <= 0:
        return None
    return round(total / 1e8, 0)  # 元 → 亿元


# ---------- 文案(直搬 market_analyzer.py:1044 / 1239) ----------
def _describe_turnover(total_amount_yi: Optional[float]) -> str:
    if total_amount_yi is None:
        return "暂无数据"
    if total_amount_yi >= 15000:
        return "高活跃度"
    if total_amount_yi >= 9000:
        return "中等活跃"
    if total_amount_yi > 0:
        return "缩量观望"
    return "暂无数据"


def _breadth_effect(up: int, down: int) -> str:
    """广度效应标签(与 _build_reasons 同口径):赚钱效应扩散 / 亏钱效应较强 / 市场分化。"""
    participation = up + down
    if participation <= 0:
        return ""
    up_ratio = up / participation
    if up_ratio >= 0.6:
        return "赚钱效应扩散"
    if up_ratio <= 0.4:
        return "亏钱效应较强"
    return "市场分化"


def _build_reasons(up, down, limit_up, limit_down,
                   index_changes, total_amount_yi) -> List[str]:
    reasons: List[str] = []
    participation = up + down
    if participation > 0:
        up_ratio = up / participation
        reasons.append(f"上涨家数占比 {up_ratio:.0%},{_breadth_effect(up, down)}")
    if index_changes:
        vals = [v for v in index_changes.values() if v is not None]
        if vals:
            avg = sum(vals) / len(vals)
            reasons.append(f"主要指数平均涨跌幅 {avg:+.2f}%")
    if limit_up + limit_down > 0:
        reasons.append(f"涨停 {limit_up} 家 / 跌停 {limit_down} 家"
                       f"(净 {limit_up - limit_down:+d})")
    reasons.append(f"两市成交额 {total_amount_yi:.0f} 亿({_describe_turnover(total_amount_yi)})"
                   if total_amount_yi else "成交额暂无数据")
    return reasons[:4]


# ---------- 主入口 ----------
def build_snapshot() -> MarketSnapshot:
    """采集大盘行情并打分,返回 MarketSnapshot。任何取数失败都降级,不抛异常。"""
    trade_date = datetime.now().strftime("%Y%m%d")

    # breadth/limit：legu 快路径优先(~0.65s 直取涨跌家数+涨跌停，非东财)；
    # 失败再回退 spot 全量并发竞速(efinance/东财/新浪，自算)。成交额：spot 精确全市场 sum，
    # 或从新浪指数成交额列(上证+深证Ａ指)取——补 legu 快路径无成交额(见下 index_amount)。
    breadth_score, breadth_ok, up, down, flat = 50, False, 0, 0, 0
    limit_up, limit_down, limit_ok = 0, 0, False
    total_amount = None

    legu = None
    try:
        legu = _fetch_breadth_legu()
    except Exception as e:  # noqa: BLE001
        logger.warning("legu breadth 取数失败: %s", e)

    if legu is not None:
        up, down, flat = legu["up"], legu["down"], legu["flat"]
        limit_up, limit_down = legu["limit_up"], legu["limit_down"]
        breadth_score = _breadth_score(up, down)
        breadth_ok = (up + down) > 0
        limit_ok = True  # legu 已给计数(0 也是有效数据；dimension 可用性看 limit_total)
    else:
        spot = None
        try:
            spot = _fetch_spot()
        except Exception as e:  # noqa: BLE001
            logger.warning("全市场 spot 回退取数最终失败: %s", e)
        if spot is not None:
            breadth_score, breadth_ok, up, down, flat = _score_breadth(spot)
            limit_up, limit_down, limit_ok = _count_limits(spot)
            total_amount = _total_amount_yi(spot)

    # 指数 (+ 两市成交额 + 指数表：从新浪指数取 sh000001+sz399107 成交额 & 上证/深成/创业板 表)
    index_changes = None
    index_amount = None
    index_table: List[Dict[str, Any]] = []
    try:
        index_changes, index_amount, index_table = _fetch_indices()
    except Exception as e:  # noqa: BLE001
        logger.warning("指数取数最终失败: %s", e)
    index_score, index_ok = _score_index(index_changes)

    # 成交额：spot 路径已精确全市场 sum 则用之；否则用指数成交额(上证+深证Ａ指)——补 legu 快路径的无成交额
    if total_amount is None and index_amount is not None:
        total_amount = index_amount

    # P4: 板块领涨/领跌（akshare 东财，fail-open，失败不影响评分）
    sector_rankings = _fetch_sector_rankings()

    # limit 维度分数
    limit_total = limit_up + limit_down
    if limit_ok and limit_total > 0:
        limit_score = int(limit_up / limit_total * 100)
    else:
        limit_score = 50

    dimensions = {
        "breadth": {"score": breadth_score, "available": bool(breadth_ok)},
        "index": {"score": index_score, "available": bool(index_ok)},
        "limit": {"score": limit_score, "available": bool(limit_ok and limit_total > 0)},
    }

    # data_quality(直搬 L1276-1281)
    if not index_ok:
        data_quality = "unavailable"
    elif all(d["available"] for d in dimensions.values()):
        data_quality = "ok"
    else:
        data_quality = "partial"

    # 总分：按「可用维度」重归一权重
    # 旧版固定权重 + 缺数维度默认 50 → breadth(0.45 权重)取不到时综合分被两个幻影 50
    # 拉向中性「震荡」，掩盖真实涨跌（2026-07-21 实盘事故根因之一）。改为只对可用维度
    # 加权平均；三维全无 → 50 分，配合 data_quality=unavailable 由模板显式降级提示。
    _dim_weights = [('breadth', 0.45), ('index', 0.35), ('limit', 0.20)]
    _avail = [(dimensions[n]['score'], w) for n, w in _dim_weights if dimensions[n]['available']]
    if _avail:
        score = int(round(sum(s * w for s, w in _avail) / sum(w for _, w in _avail)))
    else:
        score = 50

    # 温度档(直搬 L1294)
    if score >= 70:
        temperature_label = "强势"
    elif score >= 55:
        temperature_label = "偏暖"
    elif score >= 40:
        temperature_label = "震荡"
    else:
        temperature_label = "偏弱"

    # status(直搬 L998)
    if score >= 60:
        status = "green"
    elif score >= 40:
        status = "yellow"
    else:
        status = "red"

    reasons = _build_reasons(up, down, limit_up, limit_down, index_changes, total_amount)
    breadth_effect = _breadth_effect(up, down)
    turnover_label = _describe_turnover(total_amount) if total_amount else ""

    return MarketSnapshot(
        score=score,
        status=status,
        temperature_label=temperature_label,
        breadth_effect=breadth_effect,
        turnover_label=turnover_label,
        reasons=reasons,
        dimensions=dimensions,
        data_quality=data_quality,
        trade_date=trade_date,
        up_count=up,
        down_count=down,
        flat_count=flat,
        limit_up_count=limit_up,
        limit_down_count=limit_down,
        index_changes=index_changes or {},
        total_amount_yi=total_amount,
        index_table=index_table or [],
        sector_rankings=sector_rankings,
    )

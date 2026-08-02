# -*- coding: utf-8 -*-
"""
新闻过滤 + 相关度评分
====================
从 daily_stock_analysis (src/search_service.py) 移植核心逻辑:
- _score_news_relevance (L2953): 标题命中股票代码 +55 / 摘要 +34 / 链接 +18,
  公司名 +45,公司事件词 +12,官方源 +8;输出 direct/sector/macro 分类 + 可读理由。
- _filter_news_response (L3401): strict 时间窗过滤(丢 old/unknown/future+1)。
- _normalize_news_publish_date (L3315): 统一多种日期格式。

Phase 0 建好地基,Phase 2 接 tavily 时用: 中文 query + include_domains 锁中文源 +
本模块过滤/评分,替代旧的「中文关键词计数」(那是情绪恒平的根因)。

item 结构兼容 tavily: {title, content/snippet, url, published_date, source}。
"""

import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

# ---------- 中文财经源白名单(Phase 2 接 tavily 时 include_domains 用) ----------
CHINESE_FINANCE_DOMAINS = [
    "sina.com.cn", "finance.sina.com.cn",
    "eastmoney.com", "finance.eastmoney.com",
    "10jqka.com.cn",  # 同花顺
    "cs.com.cn",      # 中证网
    "stcn.com",       # 证券时报
    "cnstock.com",    # 上海证券报
    "yicai.com",      # 第一财经
    "21jingji.com",   # 21世纪经济
    "cls.cn",         # 财联社
]

# 官方/可信源 host(相关度加分 +8)
_TRUSTED_HOSTS = (
    "cninfo.com.cn",  # 巨潮资讯(公告)
    "sse.com.cn", "szse.cn",  # 沪深交易所
    "hkexnews.hk", "sec.gov", "nasdaq.com", "nyse.com",
    "eastmoney.com", "sina.com.cn",
)

# 公司事件词(命中且 direct_signal>0 时 +12)
_COMPANY_EVENT_TERMS = (
    "公告", "披露", "发布", "收购", "回购", "减持", "增持", "增持",
    "诉讼", "处罚", "业绩", "财报", "营收", "净利润", "分红", "派息",
    "董事会", "股东大会", "订单", "中标", "合作", "签约", "停牌", "复牌",
    "预增", "预减", "预亏", "业绩预告", "业绩快报", "商誉减值", "问询", "立案",
    "earnings", "revenue", "profit", "dividend", "acquisition",
)

# 行业/板块背景词
_SECTOR_NEWS_TERMS = (
    "行业", "板块", "产业链", "上下游", "景气度", "产能", "需求", "供给",
    "政策", "规划", "补贴", "进口", "出口", "原材料", "涨价", "降价",
)

# 宏观/市场词
_MACRO_NEWS_TERMS = (
    "大盘", "沪指", "深成指", "创业板", "A股", "美股", "港股", "美联储",
    "央行", "降息", "加息", "GDP", "CPI", "PMI", "M2", "汇率", "北向",
)


# ---------- 日期归一 ----------
_DATE_FMTS = ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%Y-%m-%dT%H:%M:%S",
              "%Y-%m-%d %H:%M:%S", "%Y年%m月%d日", "%d %b %Y")


def normalize_publish_date(value: Any) -> Optional[date]:
    """统一 Unix/ISO/RFC/中文相对/英文 'N days ago' → date。失败返回 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value)).date()
        except (ValueError, OSError, OverflowError):
            return None
    s = str(value).strip()
    if not s:
        return None
    # Unix 数字串
    if re.fullmatch(r"\d{10}", s):
        try:
            return datetime.fromtimestamp(int(s)).date()
        except (ValueError, OSError):
            return None
    # 中文相对:"今天/昨天/前天/3天前/N小时前"
    today = date.today()
    if s in ("今天", "今日", "today"):
        return today
    if s in ("昨天", "昨日", "yesterday"):
        return today - timedelta(days=1)
    if s in ("前天",):
        return today - timedelta(days=2)
    m = re.search(r"(\d+)\s*天前", s)
    if m:
        return today - timedelta(days=int(m.group(1)))
    m = re.search(r"(\d+)\s*小时前", s)
    if m:
        return today - timedelta(days=int(m.group(1)) // 24)
    # 英文 "N days/hours ago"
    m = re.search(r"(\d+)\s*days?\s*ago", s, re.IGNORECASE)
    if m:
        return today - timedelta(days=int(m.group(1)))
    # 标准格式
    for fmt in _DATE_FMTS:
        try:
            return datetime.strptime(s[:19] if "T" in s or " " in s else s[:10], fmt).date()
        except ValueError:
            continue
    # 尝试提取 YYYY-MM-DD
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def filter_by_freshness(items: List[Dict[str, Any]], days: int = 7,
                        strict: bool = True, keep_unknown: bool = False,
                        future_tolerance_days: int = 1) -> List[Dict[str, Any]]:
    """时间窗过滤(直搬 _filter_news_response L3401 思路)。

    strict=True: 丢 old(<earliest)/unknown/future(>latest)。用于 latest_news/risk_check。
    strict=False + keep_unknown: 保留无日期项(用于分析类维度,lookback 较长)。
    """
    if days <= 0:
        return list(items)
    today = date.today()
    earliest = today - timedelta(days=max(0, days - 1))
    latest = today + timedelta(days=future_tolerance_days)
    out: List[Dict[str, Any]] = []
    for it in items:
        d = normalize_publish_date(it.get("published_date"))
        if d is None:
            if not strict or keep_unknown:
                out.append(it)
            continue
        if d < earliest or d > latest:
            continue
        out.append(it)
    return out


# ---------- 身份词匹配 ----------
def stock_code_identity_terms(stock_code: str) -> List[str]:
    """600519.SH → ['600519', '600519.SH']。用于精确匹配代码。"""
    code = str(stock_code or "").strip()
    if not code:
        return []
    digits = "".join(c for c in code if c.isdigit())
    terms = [code]
    if digits and digits != code:
        terms.append(digits)
    return list(dict.fromkeys(terms))  # 去重保序


def _contains_term(text: str, term: str) -> bool:
    """简单包含(用于中文公司名,中文无需词边界)。"""
    return term in text if term else False


def _contains_code_term(text: str, term: str) -> bool:
    """代码词边界匹配(数字代码前后不能是数字/字母,避免 600519 误命中 16005199)。"""
    if not term:
        return False
    pattern = r"(?<![0-9A-Za-z])" + re.escape(term) + r"(?![0-9A-Za-z])"
    return re.search(pattern, text) is not None


def _contains_any(text: str, terms) -> bool:
    return any(t in text for t in terms if t)


def _host_of(url: str) -> str:
    return (url or "").lower().replace("https://", "").replace("http://", "").split("/")[0]


def _is_trusted_source(item: Dict[str, Any]) -> bool:
    host = _host_of(item.get("url", ""))
    src = str(item.get("source", "") or "").lower()
    if any(h in host for h in _TRUSTED_HOSTS):
        return True
    if any(h.replace(".com", "").replace(".cn", "") in src for h in _TRUSTED_HOSTS):
        return True
    return False


# ---------- 相关度评分(直搬 _score_news_relevance L2953) ----------
def score_relevance(item: Dict[str, Any], stock_code: str = "",
                    stock_name: str = "") -> Dict[str, Any]:
    """给单条新闻打 0-100 相关度分 + 分类(direct/sector/macro) + 可读理由。

    评分规则(直搬 DSA):
        标题命中股票代码 +55 / 摘要 +34 / 链接 +18
        标题命中公司名 +45 / 摘要 +28
        公司事件词(且已有 direct_signal) +12
        官方可信源 +8
        分类: direct_signal>=38 → direct; 命中宏观且无 direct → macro(-12); else sector
    """
    title = str(item.get("title", "") or "")
    snippet = str(item.get("content", "") or item.get("snippet", "") or "")
    url = str(item.get("url", "") or "")
    full = " ".join([title, snippet, url])

    score = 0
    direct_signal = 0
    reasons: List[str] = []

    # 股票代码命中(标题>摘要>链接)
    code_hit = False
    for term in stock_code_identity_terms(stock_code):
        if _contains_code_term(title, term):
            score += 55; direct_signal += 55; code_hit = True
            reasons.append(f"标题命中股票代码 {term}")
            break
    if not code_hit:
        for term in stock_code_identity_terms(stock_code):
            if _contains_code_term(snippet, term):
                score += 34; direct_signal += 34; code_hit = True
                reasons.append(f"摘要命中股票代码 {term}")
                break
    if not code_hit:
        for term in stock_code_identity_terms(stock_code):
            if _contains_code_term(url, term):
                score += 18; direct_signal += 18; code_hit = True
                reasons.append(f"链接命中股票代码 {term}")
                break

    # 公司名命中(中文公司名直接包含匹配)
    name_hit = False
    if stock_name:
        clean_name = re.sub(r"(股份有限公司|有限公司|集团|Inc|Corp|Co\.?,?Ltd\.?)",
                            "", str(stock_name)).strip()
        for nm in (stock_name, clean_name):
            if nm and _contains_term(title, nm):
                score += 45; direct_signal += 45; name_hit = True
                reasons.append(f"标题命中公司名 {nm}")
                break
            if nm and _contains_term(snippet, nm):
                score += 28; direct_signal += 28; name_hit = True
                reasons.append(f"摘要命中公司名 {nm}")
                break

    # 公司事件词(需已有 direct_signal)
    has_event = _contains_any(full, _COMPANY_EVENT_TERMS)
    if has_event and direct_signal > 0:
        score += 12
        direct_signal += 12
        reasons.append("命中公告/财报/交易等公司事件词")

    # 官方可信源
    if _is_trusted_source(item):
        score += 8
        reasons.append("来源接近公告或交易所渠道")

    # 分类(直搬 L3051-3063)
    has_macro = _contains_any(full, _MACRO_NEWS_TERMS)
    has_sector = _contains_any(full, _SECTOR_NEWS_TERMS)
    if direct_signal >= 38:
        category = "direct"
    elif has_macro and direct_signal == 0:
        category = "macro"
        score = max(0, score - 12)
        reasons.append("未命中目标公司身份,归为宏观/市场新闻")
    else:
        category = "sector"
        if has_sector:
            score += 6
            reasons.append("仅命中行业或板块背景")
        else:
            reasons.append("未命中股票代码或公司全称,降级为背景新闻")

    score = max(0, min(100, score))
    return {
        "relevance_score": score,
        "relevance_category": category,
        "relevance_reasons": reasons[:5],
    }


def dedup_by_url(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按 URL 归一化去重(保留首个)。"""
    seen = set()
    out = []
    for it in items:
        u = (it.get("url") or "").strip().rstrip("/").lower()
        key = u or str(it.get("title", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def rank_news(items: List[Dict[str, Any]], stock_code: str = "",
              stock_name: str = "", max_results: int = 6) -> List[Dict[str, Any]]:
    """打分 + 按 direct>sector>macro / 分数降序 排序,取前 N。每条附 relevance_* 字段。"""
    scored = []
    for it in items:
        r = score_relevance(it, stock_code, stock_name)
        merged = dict(it)
        merged.update(r)
        scored.append(merged)
    cat_rank = {"direct": 0, "sector": 1, "macro": 2}
    scored.sort(key=lambda x: (cat_rank.get(x.get("relevance_category"), 9),
                               -x.get("relevance_score", 0)))
    return scored[:max_results]

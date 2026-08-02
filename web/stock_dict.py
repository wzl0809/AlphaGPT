# -*- coding: utf-8 -*-
"""A 股 / ETF 代码 ↔ 名称映射与查询。

- 内置常用标的（个股 + ETF）
- 可选：load_full_map() 用 akshare 拉全量并缓存到 db/stock_map.json（系统设置可触发）
- lookup_name(code) / search(q) 供模板与 result_parser 用
"""
import json
import os
from pathlib import Path

_CLIENT_ROOT = Path(__file__).resolve().parent.parent   # 文件在 client/web/，两级 parent=client/（同 config.py:14）
_CACHE_FILE = _CLIENT_ROOT / 'db' / 'stock_map.json'

# ── 内置常用标的（代码 → 名称）──
_BUILTIN = {
    # 上交所个股
    '601398': '工商银行', '601939': '建设银行', '601288': '农业银行', '601988': '中国银行',
    '601628': '中国人寿', '601318': '中国平安', '601633': '长城汽车', '601963': '重庆银行',
    '601857': '中国石油', '601988': '中国银行', '600519': '贵州茅台', '600036': '招商银行',
    '600900': '长江电力', '600276': '恒瑞医药', '600030': '中信证券', '600887': '伊利股份',
    '600031': '三一重工', '600009': '上海机场', '600585': '海螺水泥', '600690': '海尔智家',
    '601012': '隆基绿能', '601899': '紫金矿业', '601166': '兴业银行', '601328': '交通银行',
    '601398': '工商银行', '600436': '片仔癀', '600600': '青岛啤酒', '601888': '中国中免',
    # 深交所个股
    '000001': '平安银行', '000002': '万科A', '000333': '美的集团', '000651': '格力电器',
    '000858': '五粮液', '000725': '京东方A', '002594': '比亚迪', '002415': '海康威视',
    '002475': '立讯精密', '002241': '歌尔股份', '000568': '泸州老窖', '002714': '牧原股份',
    '000063': '中兴通讯', '002230': '科大讯飞', '000776': '广发证券',
    # 创业板
    '300750': '宁德时代', '300059': '东方财富', '300015': '爱尔眼科', '300760': '迈瑞医疗',
    '300124': '汇川技术',
    # 科创板
    '688981': '中芯国际', '688036': '传音控股', '688256': '寒武纪',
    # ETF
    '510050': '上证50ETF', '510300': '沪深300ETF', '510500': '中证500ETF',
    '588000': '科创50ETF', '159915': '创业板ETF', '512100': '中证1000ETF',
    '510310': '沪深300ETF易方达', '588050': '上证科创50ETF',
    '512760': '半导体ETF', '512010': '医药ETF', '512660': '军工ETF', '515030': '新能源车ETF',
}


def _load() -> dict:
    """优先用缓存（akshare 全量），否则内置。"""
    if _CACHE_FILE.exists():
        try:
            return json.loads(_CACHE_FILE.read_text(encoding='utf-8'))
        except Exception:
            pass
    return dict(_BUILTIN)


_MAP_CACHE = None


def get_map() -> dict:
    global _MAP_CACHE
    if _MAP_CACHE is None:
        _MAP_CACHE = _load()
    return _MAP_CACHE


def reload_map():
    """重新读缓存（refresh 后调用）。"""
    global _MAP_CACHE
    _MAP_CACHE = _load()
    return _MAP_CACHE


def lookup_name(code: str) -> str:
    if not code:
        return ''
    m = get_map()
    return m.get(str(code).strip(), '')


def search(q: str, limit: int = 20) -> list:
    """模糊查代码或名称，返回 [{code, name}]。"""
    q = (q or '').strip().lower()
    if not q:
        # 无关键字返回热门
        return [{'code': c, 'name': n} for c, n in list(get_map().items())[:limit]]
    out = []
    for code, name in get_map().items():
        if q in code.lower() or q in name.lower():
            out.append({'code': code, 'name': name})
        if len(out) >= limit:
            break
    return out


def refresh_from_akshare() -> int:
    """用 akshare 拉全量 A 股 + ETF，缓存到 db/stock_map.json。返回条数。

    网络失败时静默返回 0（保留内置）。
    """
    try:
        import akshare as ak
        m = {}
        # A 股
        try:
            df = ak.stock_info_a_code_name()
            for _, r in df.iterrows():
                m[str(r['code'])] = str(r['name'])
        except Exception:
            pass
        # ETF
        try:
            df = ak.fund_etf_category_sina(symbol='ETF基金')
            for _, r in df.iterrows():
                code = str(r.get('代码', r.get('code', '')))
                name = str(r.get('名称', r.get('name', '')))
                if code and name:
                    m[code] = name
        except Exception:
            pass
        if not m:
            return 0
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(json.dumps(m, ensure_ascii=False), encoding='utf-8')
        reload_map()
        return len(m)
    except Exception:
        return 0

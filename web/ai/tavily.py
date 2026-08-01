# -*- coding: utf-8 -*-
"""tavily 新闻搜索 + 情绪。

Key 来源：current_app.config['TAVILY_API_KEY']。无 Key 返回 None。
"""
import requests

_API = 'https://api.tavily.com/search'


def _key():
    """读 tavily key。优先读本地加密文件（权威源，避免运行进程 config 陈旧导致「未接入」），回退 config。"""
    try:
        from web.services import apikey_store
        k = apikey_store.load().get('tavily')
        if k:
            return k
    except Exception:
        pass
    try:
        from flask import current_app
        return current_app.config.get('TAVILY_API_KEY', '') or ''
    except Exception:
        return ''


def has_key() -> bool:
    return bool(_key())


def search(query: str, max_results: int = 5, timeout: int = 20, days: int = None,
           topic: str = 'general', include_domains=None):
    """返回结果列表 [{title, content, url}]；无 Key 返回 None。

    days/topic 用于 recency: topic='news' + days=N 只返回最近 N 天的新闻。
    include_domains: 锁定中文财经源(Phase 2,治"召回英文"),如 ['sina.com.cn','eastmoney.com']。
    """
    key = _key()
    if not key:
        return None
    try:
        payload = {'api_key': key, 'query': query,
                   'max_results': max_results, 'search_depth': 'basic', 'topic': topic}
        if days:
            payload['days'] = days
        if include_domains:
            payload['include_domains'] = include_domains
        r = requests.post(_API, json=payload, timeout=timeout)
        if r.status_code != 200:
            return None
        return r.json().get('results', [])
    except Exception:
        return None


def fetch_news_sentiment(code: str, name: str):
    """个股新闻 + 情绪(Phase 2 升级)。

    中文 query(去"利好 利空 业绩"污染词) + include_domains 锁中文源 + days=7 时间窗 +
    news_filter 相关度排名 + sentiment.score_v2(中英双语+标题加权)。
    """
    from . import sentiment, news_filter
    # 扩窗 7→14 天 + max 8→10 + 加「公告」提召回（P3：治"样本不足/信息少"；保留 licensed Tavily 不换源）
    results = search(f'{name} {code} 股票 最新消息 公告', max_results=10, topic='news', days=14,
                     include_domains=news_filter.CHINESE_FINANCE_DOMAINS)
    if results is None:
        return None
    filtered = news_filter.filter_by_freshness(results, days=14, strict=False, keep_unknown=True)
    ranked = news_filter.rank_news(filtered, stock_code=code, stock_name=name, max_results=5)
    return {
        'news': [{'title': x.get('title', ''), 'url': x.get('url', ''),
                  'content': (x.get('content', '') or '')[:160]} for x in ranked],
        'sentiment': sentiment.score_v2(ranked),
    }


def fetch_market_sentiment():
    """大盘新闻情绪(首页用,Phase 2 升级)。

    中文 query(去 ISO 日期前缀,治"召回英文") + include_domains 锁中文财经源 +
    news_filter 时间窗/相关度 + sentiment.score_v2。
    失败显式返回 error(旧版静默 None → UI 兜底"平",故障不可见)。
    """
    from datetime import date
    from . import sentiment, news_filter
    today = date.today().isoformat()
    results = search('A股 大盘 今日 行情 涨跌 收盘', max_results=10, topic='news', days=1,
                     include_domains=news_filter.CHINESE_FINANCE_DOMAINS)
    if results is None:
        return {'date': today, 'news': [],
                'sentiment': {'label': '数据获取失败', 'sample_size': 0},
                'error': 'tavily 无响应(Key/网络/配额)'}
    filtered = news_filter.filter_by_freshness(results, days=2, strict=False, keep_unknown=True)
    ranked = news_filter.rank_news(filtered, max_results=6)
    return {'date': today,
            'news': [{'title': x.get('title', ''), 'url': x.get('url', ''),
                      'content': (x.get('content', '') or '')[:160]} for x in ranked],
            'sentiment': sentiment.score_v2(ranked)}

# -*- coding: utf-8 -*-
"""首页（home）。P05：公告横幅 + 个人信息卡 + 大盘情绪仪表盘 + AI 分析。"""
import json
from datetime import datetime, timedelta

from flask import Blueprint, render_template

from ..auth import login_required, current_user, active_email
from ..extensions import db
from ..ai import deepseek, tavily, market_breadth
from db.models import NotificationCache, AIAnalysisCache

bp = Blueprint('home', __name__)

# 大盘分析缓存 TTL：盘中行情变化快，按 provider 分级短刷新；非交易时段行情静止，30 分钟。
def _market_ttl_minutes(provider: str) -> int:
    """breadth(取数快) 盘中 3 分钟；deepseek 评述(LLM 较重) 10 分钟；tavily 新闻 15 分钟。
    非交易时段统一 30 分钟。旧版三者都 30 分钟 → 盘中下跌时页面锁死「震荡」30 分钟（2026-07-21 实盘事故）。"""
    from web.ai.market_breadth import is_beijing_trading_session
    if not is_beijing_trading_session():
        return 30
    return {'breadth': 3, 'deepseek': 10, 'tavily': 15}.get(provider, 10)


def _latest_market_row(provider: str):
    """AIAnalysisCache(scope=market) 最新一行（不限新旧）。无则 None。"""
    return (AIAnalysisCache.owned()
            .filter_by(provider=provider, scope='market')
            .order_by(AIAnalysisCache.fetched_at.desc()).first())


def _decode_market(s):
    """缓存 content 还原：dict/list 以 JSON 存(避免 str(dict) 字面量泄漏给用户)，
    字符串原样返回，None→None。"""
    if s is None:
        return None
    try:
        return json.loads(s)
    except Exception:
        return s


def _read_cached_market(provider: str):
    """纯读最新缓存（**不看 TTL**，过期也返回），不触发任何网络。供首页瞬间渲染。

    打开首页零网络：有就显示（即便过期），无就 None（前端显占位 + 刷新按钮）。
    新鲜度交给用户点「刷新大盘」（走 /market-refresh 强拉）。
    """
    row = _latest_market_row(provider)
    return _decode_market(row.content) if row else None


def _get_cached_market(provider: str, fetch_fn, force: bool = False):
    """读 AIAnalysisCache（scope=market）；过期或 force=True 则调 fetch_fn 刷新。

    dict/list 结果以 JSON 存储；读取 json.loads 还原，字符串原样返回。
    返回 content（str/dict/None）。TTL 随交易时段/provider 动态变化。
    force=True 时无视 TTL 始终重拉（「刷新大盘」按钮用）；拉失败仍回落上一行且
    不更新 expires_at（避免把失败误当"新鲜"）。
    """
    row = _latest_market_row(provider)
    now = datetime.utcnow()
    ttl = _market_ttl_minutes(provider)

    if not force and row and row.expires_at and row.expires_at > now:
        return _decode_market(row.content)
    try:
        data = fetch_fn()
    except Exception:
        data = None
    if data is None:
        # 拉失败：保留上一行内容（陈旧但可见），不动 expires_at（不误"保鲜"）
        return _decode_market(row.content) if row else None
    stored = json.dumps(data, ensure_ascii=False) if isinstance(data, (dict, list)) else str(data)
    if row:
        row.content = stored
        row.fetched_at = now
        row.expires_at = now + timedelta(minutes=ttl)
    else:
        row = AIAnalysisCache(owner_email=active_email(), provider=provider, scope='market',
                              content=stored,
                              fetched_at=now, expires_at=now + timedelta(minutes=ttl))
        db.session.add(row)
    db.session.commit()
    return data


def _aggregate_market(market_tv_text):
    """从缓存的 tavily 大盘文本里提取情绪标签（简单解析）。"""
    if not market_tv_text:
        return None
    # content 以 '情绪：涨/平/跌' 开头（fetch_market_sentiment 存的是 repr，这里宽松匹配）
    for label in ('涨', '跌', '平'):
        if label in market_tv_text[:20]:
            return label
    return '平'


@bp.route('/')
@login_required
def index():
    # 公告：只读本地缓存（后台 notification-poller 线程每 10 分钟 异步拉取更新）。
    # 旧版在首页同步 poll_once → 服务端慢时整页阻塞卡顿；现去掉，首页零网络瞬间渲染。
    # 首次打开（poller 尚未拉到）缓存空 → 显欢迎条；poller 10 分钟 内拉到后下次首页显示真实公告。
    anns = (NotificationCache.owned()
            .filter_by(kind='announcement')
            .order_by(NotificationCache.cached_at.desc()).limit(5).all())

    profile = current_user()

    # 大盘三块（仪表盘 / deepseek 评述 / tavily 新闻）：**只读缓存，不触发任何网络**。
    # 打开首页瞬间返回；有无缓存都先渲染（无则前端显占位 + 刷新按钮）。
    # 新鲜度交给用户点「刷新大盘」(→ /market-refresh 强拉)。旧版同步拉取导致首页卡顿。
    has_ds = deepseek.has_key()
    has_tv = tavily.has_key()
    market_snapshot = _read_cached_market('breadth')
    market_ds = _read_cached_market('deepseek') if has_ds else None
    market_tv = _read_cached_market('tavily') if has_tv else None
    sentiment = (market_tv.get('sentiment') or {}).get('label') if isinstance(market_tv, dict) else None

    # 「更新于」时间戳：取 breadth 缓存行的 fetched_at（仪表盘主数据源；存的是 UTC→+8 北京时间）
    market_updated = None
    _breadth_row = _latest_market_row('breadth')
    if _breadth_row and _breadth_row.fetched_at:
        market_updated = (_breadth_row.fetched_at + timedelta(hours=8)).strftime('%m-%d %H:%M')

    return render_template('home/index.html',
                           announcements=anns, profile=profile,
                           market_snapshot=market_snapshot,
                           market_ds=market_ds, market_tv=market_tv,
                           sentiment=sentiment,
                           has_deepseek=has_ds, has_tavily=has_tv,
                           market_updated=market_updated)


@bp.route('/market-refresh')
@login_required
def market_refresh():
    """「刷新大盘」按钮 AJAX 端点：强制重拉三块大盘数据，返回 _market.html 片段。

    串行：breadth 先拉（喂给 deepseek 的 prompt）→ deepseek 评述 → tavily 新闻。
    全部 force=True（无视 TTL）。拉失败的维度回落上一行缓存（陈旧但可见）。
    服务端 threaded，一次长请求不阻塞其它路由。返回 HTML 片段，JS 直接 innerHTML 替换 #marketCards。
    """
    has_ds = deepseek.has_key()
    has_tv = tavily.has_key()

    def _fetch_breadth():
        try:
            return market_breadth.build_snapshot().to_dict()
        except Exception:
            return None
    market_snapshot = _get_cached_market('breadth', _fetch_breadth, force=True)

    def _fetch_market_ds():
        # 把刚拉的 breadth 快照注入大盘评述 prompt(真实涨跌家数/涨停跌停/指数涨跌幅)
        return deepseek.analyze_market(market_snapshot)
    market_ds = _get_cached_market('deepseek', _fetch_market_ds, force=True) if has_ds else None
    market_tv = _get_cached_market('tavily', tavily.fetch_market_sentiment, force=True) if has_tv else None
    sentiment = (market_tv.get('sentiment') or {}).get('label') if isinstance(market_tv, dict) else None

    # 「更新于」时间戳(#marketTs)在 swap 区外、由 home.js 用本地时间回填，故片段无需此变量。
    return render_template('home/_market.html',
                           market_snapshot=market_snapshot,
                           market_ds=market_ds, market_tv=market_tv,
                           sentiment=sentiment,
                           has_deepseek=has_ds, has_tavily=has_tv)

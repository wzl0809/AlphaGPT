# -*- coding: utf-8 -*-
"""新闻情绪打分（规则引擎，无需模型）。

返回 {score[-1,1], label(涨/平/跌), pos, neg}。
后期可替换为 deepseek/LLM 打分（预留接口）。
"""

# 利好 / 利空关键词表（可扩展）
_POS = ['利好', '增长', '上涨', '涨停', '盈利', '突破', '增持', '回购', '超预期',
        '阳线', '大单', '资金流入', '景气', '订单', '创新高', '复苏']
_NEG = ['利空', '下降', '下跌', '跌停', '亏损', '减持', '风险', '警示', '退市',
        '阴线', '暴跌', '资金流出', '业绩下滑', '商誉减值', '问询', '立案']

# 英文同义词(处理 Tavily 偶发返回的英文财经稿; Phase 2 治"英文回稿 count=0 → 恒平")
_POS_EN = ['surge', 'rally', 'jump', 'gain', 'rise', 'soar', 'bullish', 'beat', 'upgrade',
           'rebound', 'outperform', 'overweight', 'record high', 'strong', 'rally']
_NEG_EN = ['plunge', 'dive', 'selloff', 'drop', 'fall', 'crash', 'bearish', 'miss', 'downgrade',
           'slump', 'tumble', 'underperform', 'cut', 'weak', 'loss', 'default']


def score(results) -> dict:
    """对 tavily 结果列表打分(旧版,保留兼容)。"""
    text = ''
    for r in results:
        text += r.get('title', '') + ' ' + (r.get('content', '') or '') + ' '

    pos = sum(text.count(k) for k in _POS)
    neg = sum(text.count(k) for k in _NEG)
    total = pos + neg
    if total == 0:
        val = 0.0
    else:
        val = (pos - neg) / total

    if val > 0.15:
        label = '涨'
    elif val < -0.15:
        label = '跌'
    else:
        label = '平'
    return {'score': round(val, 2), 'label': label, 'pos': pos, 'neg': neg}


def score_v2(results) -> dict:
    """升级版打分(Phase 2): 中英双语词表 + 标题权重×3 + 样本不足显式标注。

    替代旧 score() 的"恒平"问题(全中文词表,英文回稿 count=0 → val=0 → 平)。
    label: 偏多/偏空/中性/样本不足(区别于旧 涨/跌/平)。
    """
    title_pos = title_neg = body_pos = body_neg = 0
    for r in results:
        title = r.get('title', '') or ''
        content = r.get('content', '') or r.get('snippet', '') or ''
        title_l, content_l = title.lower(), content.lower()
        # 中文词 + 英文词(小写匹配)
        title_pos += sum(title.count(k) for k in _POS) + sum(title_l.count(k) for k in _POS_EN)
        title_neg += sum(title.count(k) for k in _NEG) + sum(title_l.count(k) for k in _NEG_EN)
        body_pos += sum(content.count(k) for k in _POS) + sum(content_l.count(k) for k in _POS_EN)
        body_neg += sum(content.count(k) for k in _NEG) + sum(content_l.count(k) for k in _NEG_EN)
    # 标题权重 ×3(标题信号比正文强)
    pos = title_pos * 3 + body_pos
    neg = title_neg * 3 + body_neg
    total = pos + neg
    if total < 3:
        return {'score': 0.0, 'label': '样本不足', 'pos': pos, 'neg': neg,
                'confidence': 'low', 'sample_size': total}
    val = (pos - neg) / total
    if val > 0.15:
        label = '偏多'
    elif val < -0.15:
        label = '偏空'
    else:
        label = '中性'
    return {'score': round(val, 2), 'label': label, 'pos': pos, 'neg': neg,
            'confidence': 'high' if total >= 6 else 'medium', 'sample_size': total}

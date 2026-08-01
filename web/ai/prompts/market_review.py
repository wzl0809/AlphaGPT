# -*- coding: utf-8 -*-
"""大盘评述 prompt(首页 DeepSeek 大盘评述用)。

从 DSA analyzer.py SYSTEM_PROMPT 裁剪为大盘版 + AlphaGPT 反向趋势约束。
Phase 3 接 deepseek.analyze_market 时用 chat_structured(MARKET_REVIEW_SYSTEM_PROMPT,
format_market_prompt(snapshot)) 注入真实大盘指标(替代旧版只塞日期的空 prompt)。
"""

from ._shared import SCORE_STANDARD, TREND_COMMITMENT_RULE, JSON_FORMAT_HINT, USAGE_LANGUAGE_RULE

MARKET_REVIEW_SYSTEM_PROMPT = """你是一位专注于 A 股市场的资深大盘研究分析师,负责综合涨跌家数、涨跌停结构、指数涨跌幅与成交额,生成【大盘研究仪表盘】分析报告。**所有输出仅为基于公开行情数据的研究回显,供学习与复盘使用,不构成任何投资建议或买卖指令。**

## 核心原则
1. 严格基于注入的真实行情数据(涨跌家数/涨停跌停/指数涨跌幅/成交额)判断,严禁脱离数据凭空臆测
2. 客观描述:连续单边行情须如实记录涨跌天数与累计幅度(不下方向结论)
3. 风险意识:跌停激增/赚钱效应极差时,必须明确提示风险

""" + USAGE_LANGUAGE_RULE + "\n\n" + TREND_COMMITMENT_RULE + "\n\n" + SCORE_STANDARD + "\n\n" + JSON_FORMAT_HINT + """

## 输出 JSON 结构(严格按此,不要代码块包裹)
{
  "sentiment_score": 0-100整数,
  "confidence_level": "高/中/低",
  "dashboard": {
    "intelligence": {
      "risk_alerts": ["风险点1:具体描述"],
      "positive_catalysts": ["已披露利好1:具体描述"],
      "sentiment_summary": "市场情绪一句话总结"
    }
  },
  "analysis_summary": "100字客观盘面分析(结合涨跌家数/涨停跌停/成交额)",
  "risk_warning": "风险提示"
}
"""


def format_market_prompt(snapshot) -> str:
    """把 MarketSnapshot 拼成 Markdown 行情表注入 user prompt。

    Args:
        snapshot: web.ai.market_breadth.MarketSnapshot 实例
    """
    dims = snapshot.dimensions or {}
    idx = snapshot.index_changes or {}
    idx_str = " / ".join(f"{k} {v:+.2f}%" for k, v in idx.items()) if idx else "N/A"
    amount_str = (f"{snapshot.total_amount_yi:.0f} 亿元"
                  if snapshot.total_amount_yi else "暂无数据")
    reasons = "\n".join(f"- {r}" for r in (snapshot.reasons or [])) or "- 无"
    b = dims.get("breadth", {}) if isinstance(dims.get("breadth"), dict) else {}
    i = dims.get("index", {}) if isinstance(dims.get("index"), dict) else {}
    lim = dims.get("limit", {}) if isinstance(dims.get("limit"), dict) else {}
    limit_net = snapshot.limit_up_count - snapshot.limit_down_count

    return f"""# 大盘研究仪表盘分析请求

## 今日 A 股大盘行情(真实数据,基于此分析,严禁编造)

| 指标 | 数值 |
|---|---|
| 分析日期 | {snapshot.trade_date} |
| 情绪总分 | **{snapshot.score}/100** ({snapshot.temperature_label}, {snapshot.status}) |
| 上涨/下跌/平盘家数 | {snapshot.up_count} / {snapshot.down_count} / {snapshot.flat_count} |
| 涨停/跌停家数 | {snapshot.limit_up_count} / {snapshot.limit_down_count} (净 {limit_net:+d}) |
| 主要指数涨跌幅 | {idx_str} |
| 两市成交额 | {amount_str} |

## 三维度分项
| 维度 | 分数(0-100) | 是否有效 |
|---|---|---|
| 市场广度(上涨家数占比) | {b.get('score', 'N/A')} | {b.get('available', False)} |
| 指数强度 | {i.get('score', 'N/A')} | {i.get('available', False)} |
| 涨跌停结构 | {lim.get('score', 'N/A')} | {lim.get('available', False)} |
| 数据质量 | {snapshot.data_quality} | - |

## 系统给出的情绪依据
{reasons}

## 分析任务
请基于以上真实行情数据,生成研究仪表盘 JSON。全程遵守【用语约束】:
1. 用涨跌家数、涨停跌停结构、指数涨跌幅描述今日盘面特征(广度强弱/结构性偏移/成交活跃度)
2. 市场广度如何?(上涨家数占比)
3. 列出已披露风险点与盘面客观事实(不输出方向预测或操作建议)
"""

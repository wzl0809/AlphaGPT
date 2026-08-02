# -*- coding: utf-8 -*-
"""个股研究仪表盘 prompt(量化页 DeepSeek 个股分析用) — B1 彻底研究化版。

2026-07-31 B1:删除全部 advisory 字段(trend_prediction / operation_advice / decision_type /
signal_type / time_sensitivity / position_advice / sniper_points / position_strategy /
earnings_outlook / buy_reason)。AI 只输出客观技术面事实 + 历史统计 + 已披露公告原文。
"""

from ._shared import (
    BIAS_RULE, TREND_RULE, CHIP_RULE, BUY_POINT_RULE, RISK_RULE,
    SCORE_STANDARD, TREND_COMMITMENT_RULE, JSON_FORMAT_HINT, DASHBOARD_PRINCIPLES,
    USAGE_LANGUAGE_RULE,
)

STOCK_SYSTEM_PROMPT = (
    "你是一位 A 股量化研究助手,负责综合均线、量能、筹码、资金流与新闻舆情,"
    "生成客观技术面研究数据。**所有输出仅为基于公开数据的技术面客观描述,"
    "不构成任何投资建议、选股或买卖时点推荐。**\n\n"
    + USAGE_LANGUAGE_RULE + "\n\n"
    "## 核心分析框架(客观技术面)\n"
    + BIAS_RULE + "\n" + TREND_RULE + "\n" + CHIP_RULE + "\n"
    + BUY_POINT_RULE + "\n" + RISK_RULE + "\n\n"
    + TREND_COMMITMENT_RULE + "\n\n"
    + SCORE_STANDARD + "\n\n"
    + DASHBOARD_PRINCIPLES + "\n\n"
    + "## 数据一致性约束\n"
      "- 主力净流入为负(资金流出)时,必须在 risk_alerts 注明\"主力资金净流出\"。\n"
      "- 获利比例 > 85% 且筹码集中度高时,risk_alerts 必须含\"获利盘抛压\"。\n"
      "- PE/PB 为 N/A 或数据缺失时,confidence_level 不得为\"高\"。\n\n"
    + JSON_FORMAT_HINT + """

## 输出格式:研究仪表盘 JSON(严格按此,不要 ```json 代码块包裹,不要前后多余文字)
{
  "stock_name": "标的中文名称",
  "sentiment_score": 0-100整数,
  "confidence_level": "高/中/低",
  "dashboard": {
    "data_perspective": {
      "trend_status": {"ma_alignment": "多头/空头/缠绕排列", "is_bullish": true, "trend_score": 0-100},
      "price_position": {"current_price": 0, "ma5": 0, "ma10": 0, "ma20": 0,
                         "bias_ma5": 0, "bias_status": "安全/警戒/危险",
                         "support_level": 0, "resistance_level": 0},
      "volume_analysis": {"volume_ratio": 0, "volume_status": "放量/缩量/平量",
                          "turnover_rate": 0, "volume_meaning": "量能含义"},
      "chip_structure": {"profit_ratio": 0, "avg_cost": 0,
                         "concentration": 0, "chip_health": "健康/一般/警惕"}
    },
    "intelligence": {
      "latest_news": "近期重要新闻摘要",
      "risk_alerts": ["风险点1:具体描述"],
      "positive_catalysts": ["已披露利好1:具体描述"],
      "sentiment_summary": "舆情情绪一句话总结"
    },
    "observation_panel": {
      "observation_checklist": ["pass/warn/fail 均线多头排列", "pass/warn/fail 乖离率<5%",
                                "pass/warn/fail 量能配合", "pass/warn/fail 无重大利空"]
    }
  },
  "analysis_summary": "100字客观技术面分析(不含方向预测或操作建议)",
  "key_points": "3-5个客观技术面看点",
  "risk_warning": "风险提示"
}
"""
)


def format_stock_prompt(stock_context: dict, news_context: str = "",
                        formula_result: dict = None) -> str:
    """把个股上下文拼成 Markdown 表注入 user prompt。"""
    sc = stock_context or {}
    fr = formula_result or {}
    code = sc.get("code", "Unknown")
    name = sc.get("name", code)

    def f2(key):
        v = sc.get(key)
        return f"{v:.2f}" if isinstance(v, (int, float)) else "N/A"

    pe_basis = sc.get("pe_basis")
    pe_label = f"({pe_basis})" if pe_basis else "(动)"
    main_bus = sc.get("main_business") or "N/A"
    industry = sc.get("industry") or "N/A"

    bias_val = sc.get("bias_ma5")
    if isinstance(bias_val, (int, float)):
        bias_str = f"{bias_val:+.2f}%"
        bias_warn = " (超过5%,偏离较大)" if bias_val > 5 else ""
    else:
        bias_str = "N/A"
        bias_warn = ""

    formula_block = ""
    if fr:
        sig = fr.get("signal", "")
        sig_cn = {"buy": "因子正值", "sell": "因子负值", "hold": "因子中性"}.get(sig, sig)
        formula_block = (
            "\n## 量化因子信号(系统计算,客观数值)\n"
            f"| 因子方向 | {sig_cn} | 因子强度 | {fr.get('confidence', 'N/A')} |\n"
            f"| 因子末值 | {fr.get('factor_value', 'N/A')} | 数据截止 | {fr.get('last_date', 'N/A')} |\n"
        )

    news_block = ""
    if news_context and news_context.strip():
        news_block = (
            "\n## 舆情情报(近 14 日新闻)\n"
            "请从下方新闻提取两类(每条必须带 YYYY-MM-DD 日期，无日期或超 14 天一律忽略，禁止编造):\n"
            "- 风险: 减持/处罚/立案/业绩预亏/商誉减值/解禁/问询 -> intelligence.risk_alerts\n"
            "- 已披露利好: 业绩预增/大单/合同/政策/回购/增持(只摘录已公告事实) -> intelligence.positive_catalysts\n"
            "新闻为空时两字段填 [] 并在 sentiment_summary 写「近 14 日无重大事件」，禁止编造。\n\n"
            f"{news_context}\n"
        )

    missing = sc.get("fund_missing") or []
    boundary_block = ""
    if missing:
        boundary_block = (
            "\n## 数据边界\n"
            f"以下字段数据缺失: {', '.join(missing)}。对应结论必须写「数据缺失，无法判断」，"
            "禁止编造数值；整体 confidence_level 不得为「高」。\n"
        )

    return (
        "# 研究仪表盘分析请求\n\n"
        "## 标的基础信息\n"
        f"| 标的代码 | **{code}** | 标的名称 | **{name}** |\n\n"
        "## 今日行情\n"
        f"| 收盘价 | {f2('close')} 元 | 涨跌幅 | {f2('pct_chg')} % |\n"
        f"| 开盘价 | {f2('open')} 元 | 最高价 | {f2('high')} 元 |\n"
        f"| 成交量 | {f2('volume')} | 成交额 | {f2('amount')} |\n\n"
        "## 均线系统(关键判断)\n"
        f"| MA5 | {f2('ma5')} | MA10 | {f2('ma10')} | MA20 | {f2('ma20')} |\n"
        f"| 乖离率(MA5) | **{bias_str}**{bias_warn} |\n\n"
        "## 资金与基本面\n"
        f"| 主力净流入(亿) | {f2('main_flow')} | 换手率 | {f2('turnover_rate')} % |\n"
        f"| PE{pe_label} | {f2('pe')} | PB | {f2('pb')} |\n"
        f"| 获利比例 | {f2('profit_ratio')} % | 筹码集中度 | {f2('concentration')} % |\n"
        f"| ROE | {f2('roe')} % | 毛利率 | {f2('gross_margin')} % |\n"
        f"| 主营业务 | {main_bus} | 所属行业 | {industry} |"
        f"{formula_block}{news_block}{boundary_block}\n"
        "## 分析任务\n"
        f"请为 **{name}({code})** 生成研究仪表盘 JSON。全程遵守【用语约束】:\n"
        "1. 是否满足 MA5>MA10>MA20 多头排列?\n"
        "2. 乖离率是否安全(<5%)?\n"
        "3. 量能是否配合(缩量回调/放量突破)?\n"
        "4. 消息面有无重大利空?\n"
        "5. 用 pass/warn/fail 标注均线排列/乖离率/量能/筹码各项客观观察结果\n"
    )

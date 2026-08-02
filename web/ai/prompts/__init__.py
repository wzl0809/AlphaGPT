# -*- coding: utf-8 -*-
"""prompt 模板库:大盘研究仪表盘 + 个股研究仪表盘。

从 daily_stock_analysis (src/analyzer.py SYSTEM_PROMPT) 裁剪本地化:
- 去掉 DSA 的 skills 多策略系统 / 占位符组装({market_placeholder}/{skills_section}等)
- 保留研究框架(留意高位/趋势顺势/筹码效率/回踩留意位/风险排查)
- 加 AlphaGPT 定制:反向趋势约束(治"连续跌也判震荡") + 用语规范(柔和研究口吻)
"""

from .market_review import MARKET_REVIEW_SYSTEM_PROMPT, format_market_prompt
from .stock_decision import STOCK_SYSTEM_PROMPT, format_stock_prompt

__all__ = [
    "MARKET_REVIEW_SYSTEM_PROMPT",
    "format_market_prompt",
    "STOCK_SYSTEM_PROMPT",
    "format_stock_prompt",
]

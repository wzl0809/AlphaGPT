# -*- coding: utf-8 -*-
"""
===================================
研究仪表盘 Report Schema (Pydantic v2)
===================================
从 daily_stock_analysis (src/schemas/report_schema.py) 移植。
定义 AnalysisReportSchema,用于校验 LLM(DeepSeek/GLM)返回的「研究仪表盘」JSON。
与 client/web/ai/prompts/stock_decision.py 的 STOCK_SYSTEM_PROMPT 输出格式对齐。

设计要点(直搬 DSA,容忍 LLM 输出瑕疵):
- 所有字段 Optional + Union[int,float,str],容忍 LLM 返回 "N/A" 字符串
- model_config = ConfigDict(extra='allow'),容忍多余字段
- SignalAttribution 带 model_validator,自动把 4 项贡献度归一到 0-100 和为 100
"""

import math
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PositionAdvice(BaseModel):
    """持仓分类建议(空仓者 vs 持仓者)。"""

    no_position: Optional[str] = None
    has_position: Optional[str] = None


class CoreConclusion(BaseModel):
    """核心结论块。"""

    one_sentence: Optional[str] = None
    signal_type: Optional[str] = None          # 信号类型(研究参考)：🟢偏多/🟡观望/🔴偏空/⚠️风险提示
    time_sensitivity: Optional[str] = None     # 立即行动/今日内/本周内/不急
    position_advice: Optional[PositionAdvice] = None


class TrendStatus(BaseModel):
    """趋势状态。"""

    ma_alignment: Optional[str] = None
    is_bullish: Optional[bool] = None
    trend_score: Optional[Union[int, float, str]] = None


class PricePosition(BaseModel):
    """价格位置(可能含 N/A 字符串)。"""

    current_price: Optional[Union[int, float, str]] = None
    ma5: Optional[Union[int, float, str]] = None
    ma10: Optional[Union[int, float, str]] = None
    ma20: Optional[Union[int, float, str]] = None
    bias_ma5: Optional[Union[int, float, str]] = None
    bias_status: Optional[str] = None          # 安全/警戒/危险
    support_level: Optional[Union[int, float, str]] = None
    resistance_level: Optional[Union[int, float, str]] = None


class VolumeAnalysis(BaseModel):
    """量能分析。"""

    volume_ratio: Optional[Union[int, float, str]] = None
    volume_status: Optional[str] = None        # 放量/缩量/平量
    turnover_rate: Optional[Union[int, float, str]] = None
    volume_meaning: Optional[str] = None


class ChipStructure(BaseModel):
    """筹码结构。"""

    profit_ratio: Optional[Union[int, float, str]] = None
    avg_cost: Optional[Union[int, float, str]] = None
    concentration: Optional[Union[int, float, str]] = None
    chip_health: Optional[str] = None          # 健康/一般/警惕


class DataPerspective(BaseModel):
    """数据视角块。"""

    trend_status: Optional[TrendStatus] = None
    price_position: Optional[PricePosition] = None
    volume_analysis: Optional[VolumeAnalysis] = None
    chip_structure: Optional[ChipStructure] = None


class Intelligence(BaseModel):
    """舆情/风险/催化块。"""

    latest_news: Optional[str] = None
    risk_alerts: Optional[List[str]] = None
    positive_catalysts: Optional[List[str]] = None
    earnings_outlook: Optional[str] = None
    sentiment_summary: Optional[str] = None


class SniperPoints(BaseModel):
    """参考区间(研究观察：参考位/次参考位/下方风险位/上方参考位)。"""

    ideal_buy: Optional[Union[str, int, float]] = None
    secondary_buy: Optional[Union[str, int, float]] = None
    stop_loss: Optional[Union[str, int, float]] = None
    take_profit: Optional[Union[str, int, float]] = None


class PositionStrategy(BaseModel):
    """配置说明(研究观察)。"""

    suggested_position: Optional[str] = None
    entry_plan: Optional[str] = None
    risk_control: Optional[str] = None


class BattlePlan(BaseModel):
    """作战计划块。"""

    sniper_points: Optional[SniperPoints] = None
    position_strategy: Optional[PositionStrategy] = None
    action_checklist: Optional[List[str]] = None    # ✅⚠️❌ 检查项


class PhaseDecision(BaseModel):
    """盘中阶段决策(盘前/盘中/午休/收盘前/盘后/非交易日)。"""

    phase_context: Optional[Dict[str, Any]] = None
    action_window: Optional[str] = None
    immediate_action: Optional[str] = None
    watch_conditions: List[str] = Field(default_factory=list)
    next_check_time: Optional[str] = None
    confidence_reason: Optional[str] = None
    data_limitations: List[str] = Field(default_factory=list)


class SignalAttribution(BaseModel):
    """信号贡献归因 —— model_validator 自动把 4 项贡献度归一到 0-100 和为 100。

    容忍 LLM 返回 "N/A"/"70%"/负数/非数字等脏数据。
    """

    technical_indicators: Optional[Union[int, float, str]] = None
    news_sentiment: Optional[Union[int, float, str]] = None
    fundamentals: Optional[Union[int, float, str]] = None
    market_conditions: Optional[Union[int, float, str]] = None
    strongest_bullish_signal: Optional[str] = None
    strongest_bearish_signal: Optional[str] = None

    @model_validator(mode='after')
    def validate_and_normalize_contributions(self) -> 'SignalAttribution':
        """归一化贡献度:字符串→数字→clamp 0-100→非零和归一到 100。"""
        contrib_fields = ['technical_indicators', 'news_sentiment', 'fundamentals', 'market_conditions']
        values: Dict[str, Optional[float]] = {}

        for field in contrib_fields:
            val = getattr(self, field)
            if val is None:
                values[field] = None
                continue
            # 字符串先清洗
            if isinstance(val, str):
                if val.strip().upper() in ('N/A', 'NULL', 'NONE', ''):
                    values[field] = None
                    continue
                try:
                    cleaned = val.replace('%', '').strip()
                    val = float(cleaned)
                except (ValueError, AttributeError):
                    values[field] = None
                    continue
            # 确保是数字
            try:
                val = float(val)
            except (TypeError, ValueError):
                values[field] = None
                continue
            if not math.isfinite(val):
                values[field] = None
                continue
            # clamp 到 0-100
            if val < 0:
                val = 0
            if val > 100:
                val = 100
            values[field] = val

        # 四项都有效且非零时,归一到和为 100
        valid_values = {k: v for k, v in values.items() if v is not None}
        if len(valid_values) == 4:
            total = sum(valid_values.values())
            if total > 0:
                for field in contrib_fields:
                    if values[field] is not None:
                        values[field] = round(values[field] * 100 / total)
                # 修正取整误差,保证和为 100
                final_sum = sum(values[f] for f in contrib_fields)
                if final_sum != 100:
                    diff = 100 - final_sum
                    for field in contrib_fields:
                        if values[field] and values[field] > 0:
                            values[field] += diff
                            break

        for field in contrib_fields:
            setattr(self, field, values[field])
        return self


class Dashboard(BaseModel):
    """研究仪表盘块。"""

    core_conclusion: Optional[CoreConclusion] = None
    data_perspective: Optional[DataPerspective] = None
    intelligence: Optional[Intelligence] = None
    battle_plan: Optional[BattlePlan] = None
    phase_decision: Optional[PhaseDecision] = None
    signal_attribution: Optional[SignalAttribution] = None


class AnalysisReportSchema(BaseModel):
    """LLM 研究仪表盘 JSON 顶层契约,与 STOCK_SYSTEM_PROMPT 输出格式对齐。"""

    model_config = ConfigDict(extra="allow")  # 容忍 LLM 多吐字段

    stock_name: Optional[str] = None
    sentiment_score: Optional[int] = Field(None, ge=0, le=100)
    trend_prediction: Optional[str] = None    # 强烈看多/看多/震荡/看空/强烈看空
    operation_advice: Optional[str] = None    # 研究倾向(研究参考)
    decision_type: Optional[str] = None       # buy/hold/sell
    confidence_level: Optional[str] = None    # 高/中/低

    dashboard: Optional[Dashboard] = None

    analysis_summary: Optional[str] = None
    key_points: Optional[str] = None
    risk_warning: Optional[str] = None
    buy_reason: Optional[str] = None

    trend_analysis: Optional[str] = None
    short_term_outlook: Optional[str] = None
    medium_term_outlook: Optional[str] = None
    technical_analysis: Optional[str] = None
    ma_analysis: Optional[str] = None
    volume_analysis: Optional[str] = None
    pattern_analysis: Optional[str] = None
    fundamental_analysis: Optional[str] = None
    sector_position: Optional[str] = None
    company_highlights: Optional[str] = None
    news_summary: Optional[str] = None
    market_sentiment: Optional[str] = None
    hot_topics: Optional[str] = None

    search_performed: Optional[bool] = None
    data_sources: Optional[str] = None

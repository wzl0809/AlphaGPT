# -*- coding: utf-8 -*-
"""A 股交易日历 —— 静态节假日表 + 周末规则。

用于量化信号面板的「下一交易日」日期标注与「数据新鲜度」判断
（docs/P04：取得当日收盘后才能生成次日准确信号；signal.py / reports.py / quant.py 共用）。

规则（2026 年市场无调休交易日 —— 上交所公告明确"周末休市"）：

    交易日 = 周一~周五 且 不在 HOLIDAYS 表中

⚠️ 维护：每年 12 月交易所发布次年休市安排后，把新增节假日补进 HOLIDAYS。
   表外年份/日期自动回退为「仅周末」规则（不崩，仅节假日边缘可能差 1 天）。

数据来源：
  - 2026：上交所《关于2026年部分节假日休市安排的通知》上证公告〔2025〕45号（2025-12-22）
          https://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml
  - 2025：历史已发生，按当年休市安排核对
"""
import logging
from datetime import date, datetime, timedelta

_logger = logging.getLogger(__name__)


# 法定节假日中「落在工作日」的日期（周末由 weekday() 判断，不重复录入）。
HOLIDAYS = {
    # ── 2025 ──
    date(2025, 1, 1),                                              # 元旦
    date(2025, 1, 28), date(2025, 1, 29), date(2025, 1, 30),
    date(2025, 1, 31), date(2025, 2, 3), date(2025, 2, 4),         # 春节 1/28(二)-2/4(二)
    date(2025, 4, 4),                                              # 清明 4/4(五)-4/6(日)
    date(2025, 5, 1), date(2025, 5, 2), date(2025, 5, 5),          # 劳动节 5/1(四)-5/5(一)
    date(2025, 6, 2),                                              # 端午 5/31(六)-6/2(一)
    date(2025, 10, 1), date(2025, 10, 2), date(2025, 10, 3),
    date(2025, 10, 6), date(2025, 10, 7), date(2025, 10, 8),       # 国庆+中秋 10/1(三)-10/8(三)

    # ── 2026（上交所上证公告〔2025〕45号，已核实）──
    date(2026, 1, 1), date(2026, 1, 2),                            # 元旦 1/1(四)-1/3(六)
    date(2026, 2, 16), date(2026, 2, 17), date(2026, 2, 18),
    date(2026, 2, 19), date(2026, 2, 20), date(2026, 2, 23),       # 春节 2/15(日)-2/23(一)
    date(2026, 4, 6),                                              # 清明 4/4(六)-4/6(一)
    date(2026, 5, 1), date(2026, 5, 4), date(2026, 5, 5),          # 劳动节 5/1(五)-5/5(二)
    date(2026, 6, 19),                                             # 端午 6/19(五)-6/21(日)
    date(2026, 9, 25),                                             # 中秋 9/25(五)-9/27(日)
    date(2026, 10, 1), date(2026, 10, 2),
    date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7),       # 国庆 10/1(四)-10/7(三)
}


_HOLIDAY_YEARS = sorted({d.year for d in HOLIDAYS})
_year_warned: set = set()


def is_trading_day(d: date) -> bool:
    """d 是否为 A 股交易日（周一~周五 且 非节假日）。

    表外年份仅按周末近似——查到时 warn 一次，提示补表（否则节假日会被误当交易日，
    导致默认评估区间长度/新鲜度判断错算）。2027 年休市安排约 2026-12 由交易所发布后需补表。
    """
    if d.weekday() >= 5:
        return False
    if d.year < _HOLIDAY_YEARS[0] or d.year > _HOLIDAY_YEARS[-1]:
        if d.year not in _year_warned:
            _year_warned.add(d.year)
            _logger.warning(
                "trading_calendar: %d 年不在 HOLIDAYS 表（已覆盖 %s），按「仅周末」近似——"
                "节假日(春节/国庆等)会被误当交易日，请于次年休市安排发布后补表。",
                d.year, _HOLIDAY_YEARS)
    return d not in HOLIDAYS


def next_trading_day(d: date) -> date:
    """d 之后（不含 d）的下一个交易日（节假日感知）。"""
    nxt = d + timedelta(days=1)
    while not is_trading_day(nxt):
        nxt += timedelta(days=1)
    return nxt


def prev_trading_day(d: date) -> date:
    """d 之前（不含 d）的上一个交易日（节假日感知）。"""
    prv = d - timedelta(days=1)
    while not is_trading_day(prv):
        prv -= timedelta(days=1)
    return prv


def latest_trading_day_on_or_before(d: date) -> date:
    """<= d 的最近一个交易日（d 本身是交易日则返回 d）。"""
    cur = d
    while not is_trading_day(cur):
        cur -= timedelta(days=1)
    return cur


def trading_days_ago(d: date, n: int) -> date:
    """d 之前（不含 d）的第 n 个交易日。与原 quant._trading_days_ago 语义一致。"""
    cur = d
    for _ in range(n):
        cur = prev_trading_day(cur)
    return cur


def expected_latest_close(now: datetime = None) -> date:
    """当前「应已收盘」的最新交易日（数据新鲜度判断用）。

    A 股 15:00 收盘；交易日 15:05 之前视作「今日尚未收盘」，期望值回退到上一交易日
    （避免盘前/盘中取到当日未定型 bar 被当作收盘数据）。
    """
    now = now or datetime.now()
    today = now.date()
    if is_trading_day(today) and (now.hour * 60 + now.minute) < (15 * 60 + 5):
        return prev_trading_day(today)
    return latest_trading_day_on_or_before(today)

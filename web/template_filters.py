# -*- coding: utf-8 -*-
"""Jinja2 自定义过滤器。"""
from flask import Flask


def register_filters(app: Flask):

    @app.template_filter('pct')
    def _pct(value, digits=2):
        """0.185 -> '18.50%'；None -> '--'。"""
        if value is None:
            return '--'
        try:
            return f'{float(value) * 100:.{digits}f}%'
        except (TypeError, ValueError):
            return str(value)

    @app.template_filter('sharpe_color')
    def _sharpe_color(value):
        """夏普着色类：<=0.4 down / <1.5 neutral / >=1.5 up。"""
        if value is None:
            return 'neutral'
        try:
            v = float(value)
        except (TypeError, ValueError):
            return 'neutral'
        if v >= 1.5:
            return 'up'
        if v <= 0.4:
            return 'down'
        return 'neutral'

    @app.template_filter('fmt_date')
    def _fmt_date(value, fmt='%Y-%m-%d'):
        if value is None:
            return '--'
        try:
            return value.strftime(fmt)
        except AttributeError:
            return str(value)

    @app.template_filter('level_title')
    def _level_title(level):
        titles = {1: '量化新手', 2: '因子学徒', 3: '策略研究员',
                  4: '量化达人', 5: '阿尔法猎手', 6: '策略大师'}
        try:
            return titles.get(int(level), '未知')
        except (TypeError, ValueError):
            return '未知'

    @app.template_filter('source_label')
    def _source_label(src):
        labels = {'self': '自己训练', 'bought': '交易购买',
                  'relay': '接力获得', 'bounty': '悬赏获得'}
        return labels.get(src, src or '--')

    @app.template_filter('suggest_price')
    def _suggest_price(formula):
        """公式建议售价（算力）：劣质(<0.4)=0 无价值；普通(0.4-1.5)极低；优质(≥1.5)适中。区间 0-1000。

        <0.4：0（劣质，无价值，建议不上架）
        0.4-1.5（普通）：5 + 夏普×7.5（原方案 1/4——整体降半后普通再降半，普通价值不高）
        ≥1.5（优质）：50 + (夏普−1.5)×125（原方案 1/2——整体降半）
        加回撤 bonus：(30%−回撤)×50
        例：sharpe=0.3→0；sharpe=1.0/dd=25%→15；sharpe=1.5/dd=20%→55；sharpe=2.5/dd=10%→185；sharpe=3.5/dd=5%→312。
        """
        sharpe = formula.test_sharpe or 0
        max_dd = formula.max_dd if formula.max_dd is not None else 0.30
        if sharpe < 0.4:
            return 0
        if sharpe < 1.5:
            price = 5 + max(0, sharpe) * 7.5
        else:
            price = 50 + (sharpe - 1.5) * 125
        dd_bonus = max(0, 0.30 - max_dd) * 50 if max_dd >= 0 else 0
        return int(min(1000, max(0, price + dd_bonus)))

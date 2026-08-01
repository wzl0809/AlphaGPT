# -*- coding: utf-8 -*-
"""14 栏目蓝图注册。"""
from flask import Flask


def register_blueprints(app: Flask):
    """注册蓝图（home/settings/train/formula_lib/quant/donate/help/about/auth + common 共享）。

    2026-07-30 合规：trade_hall/bounty/relay 三件套（针对具体个股、算力结算、平台抽佣的策略
    买卖/悬赏/接力市场，触及非法经营证券业务/选股红线）由 FEATURE_FLAGS.markets_enabled 门控，
    默认 False=隐藏不注册；改 True 可恢复。community 栏目已整体移除（占位论坛+QQ群，运营连带风险）。
    三件套文件缺时静默跳过（发行包若排除其文件不致崩）。
    """
    from ..context import FEATURE_FLAGS
    from . import (
        common, home, settings, train, formula_lib, quant,
        donate,
        help, about, auth,
    )
    mods = [common, home, settings, train, formula_lib, quant,
            donate, help, about, auth]
    if FEATURE_FLAGS.get('markets_enabled'):
        try:
            from . import trade_hall, bounty, relay
            mods += [trade_hall, bounty, relay]
        except ImportError:
            pass
    for mod in mods:
        app.register_blueprint(mod.bp)

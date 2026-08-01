# -*- coding: utf-8 -*-
"""模板全局上下文注入。"""
import os

from flask import Flask

from .auth import current_user
from .membership import is_member_active
from .version import CLIENT_VERSION, CLIENT_CHANNEL


# ── 功能开关
# ai_dashboard_advisory=False：隐藏 AI 研究仪表盘的「方向/买卖点/投资建议」输出
#   （趋势预测/倾向/信号标/参考区间/研究配置/展望/分人群/最强多空/研究理由），仅保留
#   综合评分/数据视角/情报/信号归因占比/观察清单/综述——降低「针对具体证券输出买卖级
#   指引」的荐股定性。改 True 恢复完整输出。
# signal_directional=False：量化信号圆圈中心只显公式因子末值（纯数学返回值，合规默认）；
#   True=还原方向文字（▲看多 / ▬观望）。客户端持久化 last_factor_value 供首屏渲染。
# 注：训练高级/微调参数锁定【不在此处】——由签名凭证 entitlement(is_member) 判定，
#   仅定制用户开放（同公式库>5机制），见 train/index.html + docs/16。不可用户自调。
# donate_page_mode='research'（默认，合规研究分享页）。investor 旧支付页（支付宝/微信二维码 +
#   199/2999 会员定价）已于 2026-07-30 合规清理【删除】，不再支持（见 docs/04 §1.4）。
# markets_enabled=False（默认，合规）：隐藏「策略贴板/算力悬赏/公式接力」三件套——针对具体
#   个股、算力结算、平台抽佣的策略买卖/悬赏/接力市场，触及非法经营证券业务/选股红线，本次发行
#   不暴露。改 True 可恢复（蓝图注册 + nav + home/help 链接据此开关，见 blueprints/__init__.py）。
FEATURE_FLAGS = {
    'ai_dashboard_advisory': False,
    'signal_directional': False,
    'donate_page_mode': 'research',
    'markets_enabled': False,
}


def _safe_latest_release():
    """读最近一次版本检查结果（进程外 SQLite 单行；零网络）。失败→None。"""
    try:
        from .services.release_cache import get_cached_release
        return get_cached_release()
    except Exception:
        return None


def register_context(app: Flask):
    """向所有模板注入全局变量。"""

    @app.context_processor
    def inject_globals():
        nav_items = [
            {'endpoint': 'home.index', 'label': '首页', 'icon': 'home'},
            {'endpoint': 'settings.index', 'label': '系统设置', 'icon': 'settings'},
            {'endpoint': 'train.index', 'label': '训练系统', 'icon': 'train'},
            {'endpoint': 'formula_lib.index', 'label': '公式库', 'icon': 'formula'},
            {'endpoint': 'quant.index', 'label': '量化信号', 'icon': 'quant'},
            {'endpoint': 'donate.index', 'label': '研究分享', 'icon': 'donate'},
            {'endpoint': 'help.index', 'label': '帮助', 'icon': 'help'},
            {'endpoint': 'about.index', 'label': '关于', 'icon': 'about'},
        ]
        # 合规：markets_enabled=False 时隐藏三件套导航（与蓝图注册一致，见 blueprints/__init__.py）
        if not FEATURE_FLAGS.get('markets_enabled'):
            _hidden = {'trade_hall.index', 'bounty.index', 'relay.index'}
            nav_items = [n for n in nav_items if n['endpoint'] not in _hidden]
        user = current_user()
        return {
            'current_user': user,
            'is_member': is_member_active(user),
            'subscribe_expire': (user or {}).get('subscribe_expire'),
            'nav_items': nav_items,
            'app_name': 'AlphaGPT',
            'feature_flags': FEATURE_FLAGS,
            # 版本管理（docs/14）
            'client_version': CLIENT_VERSION,
            'client_channel': CLIENT_CHANNEL,
            'latest_release': _safe_latest_release(),
            'download_fallback_url': os.getenv('RELEASE_DOWNLOAD_FALLBACK_URL', ''),
        }

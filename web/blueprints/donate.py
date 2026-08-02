# -*- coding: utf-8 -*-
"""研究分享（donate）：合规研究分享页（免费平台 + 软件定制服务，无任何支付渠道）。

2026-07-30 合规清理：删除原投资人演示用的「捐助/会员订阅」旧页（donate/investor.html，
含支付宝/微信收款二维码 + 199/2999 算力会员定价 + 立即赞助表单）及 _INVESTOR_SINFO
定价常量；donate_page_mode=='investor' 分支同步移除——/donate 恒渲染研究分享页。
支付通道服务端早已下线；客户端不再架设任何收款入口（唯一联系入口 wzl0809@gmail.com）。
"""
from flask import Blueprint, render_template

from ..auth import login_required, current_user

bp = Blueprint('donate', __name__, url_prefix='/donate')


@bp.route('/')
@login_required
def index():
    """研究分享页（合规：无支付渠道）。"""
    profile = current_user() or {}
    return render_template('donate/index.html', profile=profile)

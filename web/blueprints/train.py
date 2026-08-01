# -*- coding: utf-8 -*-
"""训练系统（train）。P02 实装完整训练页。

reports 文件服务已移至共享蓝图 common（/reports-file），供全平台复用。
"""
from flask import Blueprint, render_template

from ..auth import login_required
from ..train_bridge.env_injector import PARAM_GROUPS, defaults_for_form
from ..train_bridge.seed import STRATEGY_LABELS

bp = Blueprint('train', __name__, url_prefix='/train')


@bp.route('/')
@login_required
def index():
    return render_template(
        'train/index.html',
        param_groups=PARAM_GROUPS,
        defaults=defaults_for_form(),
        seed_strategies=STRATEGY_LABELS,
    )

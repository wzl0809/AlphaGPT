# -*- coding: utf-8 -*-
"""帮助（help）。占位；后期实装使用手册。"""
from flask import Blueprint, render_template

from ..auth import login_required

bp = Blueprint('help', __name__, url_prefix='/help')


@bp.route('/')
@login_required
def index():
    return render_template('help/index.html')

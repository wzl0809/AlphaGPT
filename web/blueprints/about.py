# -*- coding: utf-8 -*-
"""关于（about）。占位。"""
from flask import Blueprint, render_template

from ..auth import login_required

bp = Blueprint('about', __name__, url_prefix='/about')


@bp.route('/')
@login_required
def index():
    return render_template('about/index.html')

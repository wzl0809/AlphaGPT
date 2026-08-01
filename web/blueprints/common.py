# -*- coding: utf-8 -*-
"""共享蓝图（无前缀）：reports 文件服务 + 股票代码搜索。

供训练页/公式库/量化信号等复用。
"""
import os

from flask import Blueprint, request, send_from_directory, abort, jsonify

from ..auth import login_required, active_email
from .. import stock_dict
from ..services import storage

bp = Blueprint('common', __name__)

_CLIENT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))


@bp.route('/reports-file')
@login_required
def reports_file():
    """安全提供【当前用户】reports/<h>/ 下单个文件（PNG/MD/TXT/HTML）。仅 basename，防穿越。

    账号隔离（[[client-per-user-data-partition]]）：从当前登录用户的报告目录服务，
    B 无法通过猜文件名取到 A 的报告（A 的文件不在 B 的目录里 → 404）。
    """
    name = os.path.basename(request.args.get('path', ''))
    if not name:
        abort(404)
    reports_dir = str(storage.user_reports_dir(active_email()))
    path = os.path.join(reports_dir, name)
    if not os.path.isfile(path):
        abort(404)
    return send_from_directory(reports_dir, name)


@bp.route('/common/stock-search')
@login_required
def stock_search():
    """股票代码/名称模糊查询，返回 [{code, name}]。自动补全用。"""
    q = request.args.get('q', '')
    return jsonify(stock_dict.search(q, limit=20))

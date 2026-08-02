# -*- coding: utf-8 -*-
"""策略贴板（trade_hall）。P10 实装：列表/详情/购买/下架/我的成交。

售前隐藏公式明文与 AI 名；购买后服务端返回明文 → 写本地公式库(source=bought)。
数据来自服务端 DRF 分页 {count,next,previous,results}（非本地 ORM），分页在视图层算。
"""
from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash

from ..auth import login_required, current_user, refresh_user_profile, active_email
from ..extensions import db
from ..quota import formula_save_allowed
from db.models import LocalFormula, SOURCE_BOUGHT
import api_client.endpoints as ep

bp = Blueprint('trade_hall', __name__, url_prefix='/trade')

PAGE_SIZE = 20

# 服务端 _filter_listings 允许的 order 值
_ORDER_OPTIONS = [
    ('-created_at', '最新上架'),
    ('-test_sharpe', '夏普降序'),
    ('-price_nexus', '价格降序'),
    ('price_nexus', '价格升序'),
]


def _parse_page(v):
    try:
        return max(1, int(v))
    except (TypeError, ValueError):
        return 1


def _is_dev(data):
    """区分「无服务端（dev）」与「真错误」：前者 info 提示，后者 error。"""
    return data.get('code') == 'ERR_NO_SERVER'


def _balance():
    u = current_user() or {}
    v = u.get('nexus_coin_balance')
    return v if v is not None else u.get('nexus_balance')


@bp.route('/')
@login_required
def index():
    page = _parse_page(request.args.get('page', 1))
    stock = (request.args.get('stock') or '').strip()
    sharpe_gt = (request.args.get('sharpe_gt') or '').strip()
    dd_lt = (request.args.get('dd_lt') or '').strip()
    order = request.args.get('order') or '-created_at'

    params = {'page': page, 'page_size': PAGE_SIZE, 'order': order}
    if stock:
        params['stock'] = stock
    if sharpe_gt:
        params['sharpe_gt'] = sharpe_gt
    if dd_lt:
        params['dd_lt'] = dd_lt

    ok, data = ep.get_listings(params)
    if not ok:
        if not _is_dev(data):
            flash(f'加载挂单失败：{data.get("detail", data.get("code"))}', 'error')
        results, count = [], 0
    else:
        results, count = data.get('results', []), data.get('count', 0)
    pages = max(1, (count + PAGE_SIZE - 1) // PAGE_SIZE)

    return render_template('trade_hall/index.html', items=results, count=count,
                           page=page, pages=pages, stock=stock, sharpe_gt=sharpe_gt,
                           dd_lt=dd_lt, order=order, order_options=_ORDER_OPTIONS,
                           balance=_balance())


@bp.route('/<int:lid>')
@login_required
def detail(lid):
    ok, data = ep.get_listing(lid)
    if not ok:
        if _is_dev(data):
            flash('未连接服务端，策略贴板不可用', 'info')
        else:
            flash(f'挂单不可查看：{data.get("detail", data.get("code"))}', 'error')
        return redirect(url_for('.index'))
    seller_id = (data.get('seller') or {}).get('id')
    me_id = (current_user() or {}).get('id')
    mine = bool(me_id and me_id == seller_id)
    return render_template('trade_hall/detail.html', l=data, mine=mine, balance=_balance())


@bp.route('/<int:lid>/buy', methods=['POST'])
@login_required
def buy(lid):
    # 公式库容量上限（免费用户）：扣除算力前查，到顶则不购买、引导删旧/升级
    qok, used, cap = formula_save_allowed(current_user())
    if not qok:
        if cap is None:
            flash('登录状态已过期，请重新登录后再购买。', 'error')
        else:
            flash(f'公式库已满（免费上限 {cap} 个），无法购买入库。请先删除旧公式；需要更大容量可联系开发者定制。', 'error')
        return redirect(url_for('.detail', lid=lid))
    ok, data = ep.create_order(lid)
    if not ok:
        if _is_dev(data):
            flash('未连接服务端，购买不可用', 'info')
        else:
            flash(f'购买失败：{data.get("detail", data.get("code"))}', 'error')
        return redirect(url_for('.detail', lid=lid))

    # 扣除算力成功 → 刷新本地 profile 缓存（侧栏 base.html:72 与大厅顶部余额即时更新，
    # 否则会停留在扣除算力前的旧值——这是「买了多次但余额一直不变」的根因）
    refresh_user_profile()

    # 服务端返回明文 + 公开水数据 → 写入本地公式库
    meta = data.get('formula_meta') or {}
    f = LocalFormula(
        owner_email=active_email(),
        stock_code=data.get('stock_code') or '',
        stock_name=data.get('stock_name') or '',
        formula_str=data.get('formula_str') or '',
        tokens=data.get('tokens') or [],
        ai_name=data.get('ai_name') or '',
        test_sharpe=float(data.get('test_sharpe') or 0.0),
        ann_ret=float(data['ann_ret']) if data.get('ann_ret') is not None else 0.0,
        max_dd=float(data['max_dd']) if data.get('max_dd') is not None else 0.0,
        source=SOURCE_BOUGHT,
        origin_id=str(data.get('order_id') or data.get('listing_id') or ''),
        train_params=meta,
        hardware_summary=meta.get('hardware'),
        saved=True,
        trained_at=datetime.utcnow(),
    )
    db.session.add(f)
    db.session.commit()
    # 在线硬校验：向服务端占库位（离线/竞态→pending，reconcile 兜底）
    try:
        from ..services.library_sync import stamp_server_claim
        stamp_server_claim(f)
    except Exception:
        pass
    flash(f'购买成功，已加入公式库（{f.stock_name or f.stock_code}）', 'success')
    return redirect(url_for('formula_lib.detail', fid=f.id))


@bp.route('/my')
@login_required
def my():
    """我的分享（卖家管理页：在售/已售/已下架，一键下架）。"""
    page = _parse_page(request.args.get('page', 1))
    status = (request.args.get('status') or '').strip()
    params = {'page': page, 'page_size': PAGE_SIZE}
    if status in ('active', 'sold', 'removed'):
        params['status'] = status
    ok, data = ep.get_my_listings(params)
    if not ok:
        if not _is_dev(data):
            flash(f'加载我的分享失败：{data.get("detail", data.get("code"))}', 'error')
        results, count = [], 0
    else:
        results, count = data.get('results', []), data.get('count', 0)
    pages = max(1, (count + PAGE_SIZE - 1) // PAGE_SIZE)
    status_options = [('', '全部'), ('active', '在售'), ('sold', '已售')]
    return render_template('trade_hall/my.html', items=results, count=count,
                           page=page, pages=pages, status=status,
                           status_options=status_options)


@bp.route('/<int:lid>/remove', methods=['POST'])
@login_required
def remove(lid):
    ok, data = ep.remove_listing(lid)
    dest = url_for('.my') if request.form.get('from_my') else url_for('.index')
    if ok:
        flash('挂单已下架', 'success')
    elif _is_dev(data):
        flash('未连接服务端，下架不可用', 'info')
    else:
        flash(f'下架失败：{data.get("detail", data.get("code"))}', 'error')
    return redirect(dest)


@bp.route('/orders')
@login_required
def orders():
    page = _parse_page(request.args.get('page', 1))
    ok, data = ep.get_my_orders({'page': page, 'page_size': PAGE_SIZE})
    if not ok:
        if not _is_dev(data):
            flash(f'加载成交记录失败：{data.get("detail", data.get("code"))}', 'error')
        results, count = [], 0
    else:
        results, count = data.get('results', []), data.get('count', 0)
    pages = max(1, (count + PAGE_SIZE - 1) // PAGE_SIZE)
    me_id = (current_user() or {}).get('id')
    return render_template('trade_hall/orders.html', items=results, count=count,
                           page=page, pages=pages, me_id=me_id)

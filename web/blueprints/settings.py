# -*- coding: utf-8 -*-
"""系统设置（settings）。P06 实装：硬件 / API Key / 依赖 / 清理。"""
from flask import Blueprint, render_template, request, redirect, url_for, flash, session

from ..auth import login_required, current_user, logout_session
from ..extensions import db
from ..services import hw_service, deps_check, apikey_store, cleanup, perf_store
from ..ai import deepseek, tavily
import api_client.endpoints as ep
from db.models import ReportsIndex, CheckpointsIndex

bp = Blueprint('settings', __name__, url_prefix='/settings')


@bp.route('/')
@login_required
def index():
    hw = hw_service.collect_static()
    deps = deps_check.check()
    keys = apikey_store.load()
    perf = perf_store.load()
    profile = current_user() or {}
    return render_template('settings/index.html', hw=hw, deps=deps,
                           keys=keys, enc_note=apikey_store.encryption_note(),
                           perf=perf, profile=profile)


@bp.route('/refresh-hw', methods=['POST'])
@login_required
def refresh_hw():
    """「刷新硬件信息」按钮：强制重采集硬件 + 重检依赖（绕过 7 天文件缓存 / 进程缓存），重定向回设置页。"""
    hw_service.collect_static(force=True)
    deps_check.check(force=True)
    flash('硬件信息与依赖检测已刷新', 'success')
    return redirect(url_for('.index'))


@bp.route('/profile', methods=['POST'])
@login_required
def save_profile():
    """保存个人信息（手机/QQ/微信）；成功后刷新本地 session 缓存即时生效。"""
    payload = {
        'phone': (request.form.get('phone') or '').strip(),
        'qq': (request.form.get('qq') or '').strip(),
        'wechat': (request.form.get('wechat') or '').strip(),
    }
    ok, data = ep.update_profile(payload)
    if not ok:
        flash('保存失败：' + str(data.get('detail', '')), 'error')
        return redirect(url_for('.index'))
    # 重新拉取 profile 刷新 session（首页/侧栏头像资料即时更新）
    ok2, prof = ep.get_profile()
    if ok2 and isinstance(prof, dict):
        session['user_profile'] = prof
    flash('个人信息已保存', 'success')
    return redirect(url_for('.index'))


@bp.route('/password', methods=['POST'])
@login_required
def save_password():
    """修改密码：客户端最小预校验 → 转服务端校验旧密码+新密码强度。

    错误经统一错误处理器扁平成 data.detail 中文串，直接 flash。
    成功后服务端已吊销所有会话；本地立即登出并跳登录页，强制用新密码重新登录。
    """
    old = request.form.get('old_password') or ''
    new = request.form.get('new_password') or ''
    confirm = request.form.get('confirm_password') or ''
    back = url_for('.index') + '#password'
    # 客户端预校验（与服务端一致的最小集），减少无谓往返
    if not old or not new:
        flash('请填写原密码与新密码', 'error')
        return redirect(back)
    if new != confirm:
        flash('两次输入的新密码不一致', 'error')
        return redirect(back)
    if len(new) < 8:
        flash('新密码至少 8 位', 'error')
        return redirect(back)
    ok, data = ep.change_password(old, new)
    if not ok:
        flash('修改失败：' + str(data.get('detail', '')), 'error')
        return redirect(back)
    # 安全：服务端已 blacklist 该用户全部会话；本地立即登出，强制用新密码重新登录
    logout_session()
    flash('密码已修改。为安全，请用新密码重新登录。', 'success')
    return redirect(url_for('auth.login'))


@bp.route('/apikeys', methods=['POST'])
@login_required
def save_apikeys():
    """保存 API Key（加密存本地，实时更新 config）。"""
    submitted = {
        'deepseek': (request.form.get('deepseek') or '').strip(),
        'tavily': (request.form.get('tavily') or '').strip(),
        'tushare': (request.form.get('tushare') or '').strip(),
        # 通用 OpenAI 兼容端点（三件套：key / base_url / model）
        'openai_compat': (request.form.get('openai_compat') or '').strip(),
        'openai_compat_base_url': (request.form.get('openai_compat_base_url') or '').strip(),
        'openai_compat_model': (request.form.get('openai_compat_model') or '').strip(),
    }
    # 语义：有值→写入/覆盖；清空→删除该 Key
    # （输入框预填当前 Key 掩码：不动=保留，清空=删除，故无需额外删除按钮）
    merged = apikey_store.load()
    cleared = []
    for k, v in submitted.items():
        if v:
            merged[k] = v
        else:
            merged.pop(k, None)
            cleared.append(k)
    apikey_store.save(merged)
    from flask import current_app
    apikey_store.apply_to_app(current_app)
    # 清空=删除：清除被删 Key 在 config / os.environ 的残留
    # （apply_to_app 只 set 不 clear；_all_keys() 会回退读 config/env，不清则删除无效）
    if cleared:
        apikey_store.clear_from_app(current_app, cleared)
    # 反馈各 Key 开通状态
    msgs = []
    has_any_llm = bool(merged.get('deepseek') or merged.get('openai_compat')
                       or merged.get('zhipu') or merged.get('tongyi'))
    msgs.append('AI 大模型: ' + ('✅ 已开通' if has_any_llm else '未配置'))
    msgs.append('tavily: ' + ('✅ 已开通' if merged.get('tavily') else '未配置'))
    msgs.append('tushare: ' + ('✅ 已配置' if merged.get('tushare') else '未配置'))
    flash('API Key 已保存（' + apikey_store.encryption_note() + '）：' + ' | '.join(msgs), 'success')
    return redirect(url_for('.index'))


@bp.route('/perf', methods=['POST'])
@login_required
def save_perf():
    """保存训练性能/环境参数（数据源并发、CPU 线程数）到本地，下次训练全局生效。"""
    raw = {
        'margin_workers': request.form.get('margin_workers'),
        'cpu_threads': request.form.get('cpu_threads'),
    }
    vals = perf_store.save(raw)             # clamp 到合法范围后写 perf.json
    from flask import current_app
    perf_store.apply_to_app(current_app)    # 实时写入 app.config，下次训练即生效
    flash(f'性能参数已保存：数据源并发 {vals["margin_workers"]} · CPU 线程数 {vals["cpu_threads"]}（下次训练生效）', 'success')
    return redirect(url_for('.index'))


@bp.route('/reports')
@login_required
def reports_cleanup():
    cleanup.sync_reports_index()
    files = (ReportsIndex.owned()
             .order_by(ReportsIndex.created_at.desc()).paginate(
                 page=max(1, int(request.args.get('page', 1))), per_page=20, error_out=False))
    return render_template('settings/reports_cleanup.html', items=files)


@bp.route('/checkpoints')
@login_required
def checkpoints_cleanup():
    cleanup.sync_checkpoints_index()
    files = (CheckpointsIndex.owned()
             .order_by(CheckpointsIndex.created_at.desc()).paginate(
                 page=max(1, int(request.args.get('page', 1))), per_page=20, error_out=False))
    return render_template('settings/checkpoints_cleanup.html', items=files)


@bp.route('/reports/delete', methods=['POST'])
@login_required
def delete_reports():
    fps = request.form.getlist('file_ids')
    n = cleanup.delete_files(fps)
    flash(f'已删除 {n} 个 reports 文件', 'success')
    return redirect(url_for('.reports_cleanup'))


@bp.route('/checkpoints/delete', methods=['POST'])
@login_required
def delete_checkpoints():
    fps = request.form.getlist('file_ids')
    n = cleanup.delete_files(fps)
    flash(f'已删除 {n} 个断点续训文件', 'success')
    return redirect(url_for('.checkpoints_cleanup'))

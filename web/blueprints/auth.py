# -*- coding: utf-8 -*-
"""鉴权（auth）：登录 / 注册页。

P01 骨架：渲染登录/注册页，POST 调 api_client（P08 真服务端联调）。
开发期 DEV_BYPASS_AUTH 已开，用户也可直接访问各页。
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify

from ..auth import login_required, login_session, logout_session, current_user
import api_client.endpoints as ep

bp = Blueprint('auth', __name__, url_prefix='/auth')


# register_info 拉取失败时的兜底（页面不致空白，与服务端 email_domains.py 保持一致）
_DEFAULT_REGISTER_INFO = {
    'groups': {
        '国内主流': ['qq.com', 'foxmail.com', 'vip.qq.com', '163.com', '126.com',
                  'yeah.net', '188.com', 'sina.com', 'sina.cn', 'sohu.com',
                  'aliyun.com', '21cn.com'],
        '国内运营商': ['139.com', '189.cn', 'wo.cn'],
        '国际主流': ['gmail.com', 'googlemail.com', 'outlook.com', 'hotmail.com',
                  'live.com', 'msn.com', 'yahoo.com', 'ymail.com', 'rocketmail.com',
                  'icloud.com', 'me.com', 'mac.com', 'aol.com', 'aim.com',
                  'gmx.com', 'mail.com', 'zoho.com'],
    },
    'hint': '请选择常用邮箱域名，或选「其他」输入完整邮箱（须为支持的主流邮箱）',
}


@bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user() and not current_user().get('is_mock'):
        return redirect(request.args.get('next') or url_for('home.index'))

    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        ok, data = ep.login(email, password)
        # 版本过低拒登（426 ERR_CLIENT_OUTDATED）：持久化 payload → 跳更新中心（docs/14 §5）
        if not ok and isinstance(data, dict) and data.get('code') == 'ERR_CLIENT_OUTDATED':
            try:
                from ..services.release_cache import set_outdated_payload
                set_outdated_payload(data)
            except Exception:
                pass
            return redirect(url_for('auth.update_center', mandatory=1))
        if ok:
            login_session(data.get('user', {}), data.get('access', ''), data.get('refresh', ''),
                          data.get('entitlement'))
            # 登录成功 → 清陈旧的拒登负载，避免更新中心首屏误渲染"必须升级"
            try:
                from ..services.release_cache import clear_outdated_payload
                clear_outdated_payload()
            except Exception:
                pass
            flash('登录成功', 'success')
            return redirect(request.args.get('next') or url_for('home.index'))
        flash(data.get('detail') or '登录失败，请稍后重试。', 'error')
        lock = 0
        if isinstance(data, dict):
            lock = data.get('locked_seconds') or 0
        return render_template('auth/login.html', lock_seconds=lock)
    return render_template('auth/login.html', lock_seconds=0)


@bp.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        # 组装邮箱：下拉选域名（name@domain）或「其他」手输完整邮箱
        if request.form.get('email_mode') == 'other':
            email = request.form.get('email_full', '').strip()
        else:
            local = request.form.get('email_local', '').strip()
            domain = request.form.get('email_domain', '').strip()
            email = f'{local}@{domain}' if local and domain else ''
        if request.form.get('confirm') == '1':
            # 协议闸门：未勾选「我已阅读并同意《用户协议与免责声明》」不得建账号
            # （前端 register.html 已 required + JS 拦截；此为绕过 JS 的兜底）
            if request.form.get('agree') != '1':
                flash('请先阅读并同意《用户协议与免责声明》后再注册', 'error')
                return redirect(url_for('auth.register'))
            # 确认注册：校验验证码 + 建账号（服务端 RegisterConfirmView：建用户+赠送算力+统计）
            code = request.form.get('code', '').strip()
            username = request.form.get('username', '').strip()
            password = request.form.get('password', '')
            ok, data = ep.register_confirm(email, code, username, password)
            if ok:
                flash(data.get('detail', '注册成功，请登录'), 'success')
                return redirect(url_for('auth.login'))
            flash(data.get('detail', '注册失败：验证码/用户名/密码/邮箱域名有误'), 'error')
            return redirect(url_for('auth.register'))
        # 发送验证码（前端已即时校验域名；email 为空时服务端自然拒绝）
        ok, data = ep.register_request(email)
        flash(data.get('detail', '验证码已发送（无服务端时不实际发送）'),
              'success' if ok else 'error')
        return redirect(url_for('auth.register'))
    # GET：拉取域名分组 + 引导文案，失败用兜底
    ok, info = ep.register_info()
    if not ok or not isinstance(info, dict):
        info = _DEFAULT_REGISTER_INFO
    return render_template('auth/register.html', reg_info=info)


@bp.route('/agreement')
def agreement():
    """用户协议与免责声明（注册页链接，公开访问，无需登录）。"""
    return render_template('auth/agreement.html')


@bp.route('/check-username')
def check_username():
    """昵称可用性实时检测（注册页 AJAX）：代理服务端，无服务端/限流时优雅降级（不阻断注册）。"""
    q = (request.args.get('username') or '').strip()
    if not q:
        return jsonify({'available': False, 'message': '请输入昵称'})
    ok, data = ep.check_username(q)
    if ok and isinstance(data, dict):
        return jsonify({'available': bool(data.get('available')),
                        'message': data.get('message', '')})
    # 无服务端 / 超时 / 被限流：不阻断（提交时服务端仍会强制校验），仅静默
    return jsonify({'available': None, 'message': ''})


@bp.route('/register/send-code', methods=['POST'])
def register_send_code():
    """注册发码 AJAX：JSON 返回 {detail, pow_required?, challenge?}；无服务端/限流时优雅降级。"""
    data = request.get_json(silent=True) or {}
    email = (data.get('email') or '').strip()
    if not email:
        return jsonify({'detail': '请输入邮箱'})
    ok, resp = ep.register_request(email, data.get('pow'))
    if isinstance(resp, dict):
        return jsonify(resp)
    return jsonify({'detail': '验证码服务暂不可用，请稍后再试'})


@bp.route('/reset/send-code', methods=['POST'])
def reset_send_code():
    """找回密码发码 AJAX：JSON 返回 {detail, pow_required?, challenge?}。"""
    data = request.get_json(silent=True) or {}
    email = (data.get('email') or '').strip()
    if not email:
        return jsonify({'detail': '请输入邮箱'})
    ok, resp = ep.password_reset_request(email, data.get('pow'))
    if isinstance(resp, dict):
        return jsonify(resp)
    return jsonify({'detail': '验证码服务暂不可用，请稍后再试'})


@bp.route('/reset-password', methods=['GET', 'POST'])
def reset_password():
    """找回密码：发重置码 → 凭码设新密码（与注册同构的双 submit 表单）。"""
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        if request.form.get('confirm') == '1':
            # 重置：校验码 + 设新密码（服务端 confirm_password_reset：set_password + 吊销会话）
            code = request.form.get('code', '').strip()
            new_password = request.form.get('new_password', '')
            ok, data = ep.password_reset_confirm(email, code, new_password)
            if ok:
                flash(data.get('detail', '密码已重置，请用新密码登录'), 'success')
                return redirect(url_for('auth.login'))
            flash(data.get('detail', '重置失败：重置码错误或已过期'), 'error')
            return redirect(url_for('auth.reset_password'))
        # 发送重置码（无论邮箱是否存在，服务端统一中性文案，不泄露注册状态）
        ok, data = ep.password_reset_request(email)
        flash(data.get('detail', '若该邮箱已注册，重置验证码已发送（无服务端时不实际发送）'),
              'success' if ok else 'error')
        return redirect(url_for('auth.reset_password'))
    return render_template('auth/reset_password.html')


@bp.route('/logout')
def logout():
    # 不再 flash：登录页本身即为「已退出」反馈，顶部提示条已移除
    logout_session()
    return redirect(url_for('auth.login'))


@bp.route('/mock')
def enter_mock():
    """开发旁路入口：主动进入 mock 模式（DEV_BYPASS_AUTH 开时仅占位跳转首页）。"""
    return redirect(url_for('home.index'))


@bp.route('/update')
def update_center():
    """更新中心（拒登后跳转 / 关于页手动进入）。

    零网络首屏：优先用持久化的 426 payload（拒登时已存），其次最近一次检查结果（C16）。
    无任何缓存（离线/首次）→ 降级提示 + 官方兜底地址 + "重新检测"按钮。
    """
    mandatory = request.args.get('mandatory') == '1'
    try:
        from ..services.release_cache import get_outdated_payload, get_cached_release
        payload = get_outdated_payload() or get_cached_release() or {}
    except Exception:
        payload = {}
    return render_template('auth/update_center.html', payload=payload, mandatory=mandatory)


@bp.route('/release-info')
def release_info_proxy():
    """版本检查 AJAX 代理（更新中心"重新检测" / 关于页"检查更新"共用）。

    公开（不要求登录）—— 更新中心在登录前就要用。无服务端/失败 → 错误负载，前端降级。
    """
    from ..version import CLIENT_VERSION, CLIENT_CHANNEL
    ok, data = ep.release_info(CLIENT_VERSION, CLIENT_CHANNEL)
    if ok and isinstance(data, dict):
        try:
            from ..services.release_cache import set_cached_release, clear_outdated_payload
            set_cached_release(data)
            if data.get('upgrade_required') is False:
                clear_outdated_payload()      # 检测通过/已是最新 → 清陈旧拒登负载
        except Exception:
            pass
        return jsonify(data)
    code = data.get('code') if isinstance(data, dict) else 'ERR_NETWORK'
    # 错误负载【不】带 upgrade_required，仅带 code —— 避免前端把"服务不可用"误判为"检测通过"
    return jsonify({
        'detail': '版本检查服务暂不可用，请稍后重试或访问官网下载',
        'code': code or 'ERR_NETWORK',
        'latest_version': '', 'assets': [],
    })

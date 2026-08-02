# -*- coding: utf-8 -*-
"""SocketIO 事件注册。

P01：connect / disconnect / request_hw_stats
P02：start_training / stop_training / reconnect / save_formula
       推送：train_progress / train_log / train_status / best_formula /
             train_classified / train_saved（由 train_bridge.emitter 发出）
"""
import os

from flask import request, current_app, send_from_directory, abort
from flask_socketio import SocketIO, emit

from .train_bridge import emitter, runner
from .train_bridge.env_injector import defaults_for_form
from . import lifecycle


def register_sockets(socketio: SocketIO, app):

    # emitter 需持有 app + socketio 引用（后台线程推送用）
    emitter.init(app, socketio)

    # 训练结果同步模块初始化（存 app + 拉起队列重传 worker）；模块缺失时跳过
    try:
        import silent_uploader
        silent_uploader.init(app)
    except Exception as e:
        app.logger.warning('silent_uploader 初始化失败: %s', e)

    # 通知轮询线程（每 10 分钟 拉取公告/个人通知缓存本地）
    try:
        from .services.notification_sync import start_poller
        start_poller(app)
    except Exception as e:
        app.logger.warning('通知轮询线程启动失败: %s', e)

    @socketio.on('connect')
    def _on_connect():
        lifecycle.note_connect()
        app.logger.info('[SocketIO] 客户端连接: %s', request.sid)
        emit('train_log', {'line': f'🔌 WebSocket 连接成功 (sid={request.sid[:8]}...)'})
        # 若训练进行中，通知前端
        if runner.is_running():
            emit('train_status', {'status': 'running', 'message': '训练后台运行中...'})

    @socketio.on('disconnect')
    def _on_disconnect():
        lifecycle.note_disconnect()
        app.logger.info('[SocketIO] 客户端断开: %s', request.sid)

    @socketio.on('request_hw_stats')
    def _on_request_hw_stats():
        try:
            import hw_monitor
            stats = hw_monitor.HWMonitor.collect()
            emit('hw_stats', stats)
        except ImportError:
            emit('hw_stats', {'error': 'hw_monitor 不可用'})
        except Exception as e:
            emit('hw_stats', {'error': str(e)})

    # ── 训练 ──
    @socketio.on('start_training')
    def _on_start_training(data):
        """接收前端参数，启动后台训练。data 为表单 dict。"""
        if runner.is_running():
            emit('train_status', {'status': 'error', 'message': '⚠️ 已有训练在运行，请等待或停止'})
            return

        # 信号评估与训练共用 AlphaGPT 全局态(FEATURES/VOCAB)，二者不可并发：
        # 任意一张卡的信号在跑则拒绝启动训练（对称于 quant.start_signal 拒绝训练中生成信号）。
        from .blueprints import quant as quant_bp
        if quant_bp.any_signal_running():
            emit('train_status', {'status': 'error',
                 'message': '⚠️ 量化信号生成进行中，与训练共用引擎，请等信号完成（约数秒）后再训练'})
            return

        # 用 spec 默认值补全缺失字段，保证 AlphaGPT.Config 完整
        params = dict(defaults_for_form())
        params.update({k: v for k, v in (data or {}).items() if v is not None})

        # 类型规整（前端可能传字符串）
        _coerce_numeric(params)

        # 身份 token（同步携带；开发期 mock 为空）
        from .auth import current_user
        token = ''
        collector = {'username': '', 'user_id': ''}
        try:
            from flask import session
            token = session.get('access_token', '')
            cu = current_user() or {}
            collector = {'username': cu.get('username', ''), 'user_id': cu.get('id', '')}
        except Exception:
            pass
        # 注入采集者信息（后台线程无 request context，无法取 session）
        params['_collector'] = collector
        # 更新同步重传 token（登录态刷新）
        try:
            import silent_uploader
            silent_uploader.set_retry_token(token)
        except Exception:
            pass

        ok = runner.start(params, token)
        if ok:
            emit('train_status', {'status': 'starting', 'message': '🔄 训练线程已启动...'})
        else:
            emit('train_status', {'status': 'error', 'message': '⚠️ 启动失败'})

    @socketio.on('stop_training')
    def _on_stop_training():
        runner.request_stop()
        # 不立即发 stopped 终态（旧版是空头承诺）：降级为日志；真正的 stopped 由 runner
        # 捕获引擎 StopTraining 后发出（训练循环每 ep/PPO-inner 检查，通常 ≤1 秒生效）
        emit('train_log', {'line': '⏹️ 已请求停止，正在当前 batch/epoch 完成后退出（通常 ≤1 秒）…'})

    @socketio.on('reconnect')
    def _on_reconnect(_data=None):
        if runner.is_running():
            emit('train_status', {'status': 'running', 'message': '🔄 训练后台运行中...'})
        else:
            emit('train_status', {'status': 'idle', 'message': '就绪'})

    @socketio.on('save_formula')
    def _on_save_formula(data):
        """「保存到公式库」确认：把 saved=False 的训练结果置为 saved=True。"""
        fid = (data or {}).get('formula_id')
        if fid is None:
            emit('train_saved', {'ok': False, 'detail': '缺少 formula_id'})
            return
        from web.extensions import db
        from db.models import LocalFormula
        from .auth import get_owned
        f = get_owned(LocalFormula, fid)
        if not f:
            emit('train_saved', {'ok': False, 'detail': '公式不存在'})
            return
        if f.saved:
            emit('train_saved', {'ok': True, 'formula_id': fid, 'already': True})
            return
        # 免费用户公式库容量上限：到顶则拒绝保存，引导删旧/升级
        from web.quota import formula_save_allowed
        from .auth import current_user
        from . import entitlement
        ok, used, cap = formula_save_allowed(current_user())
        if not ok:
            if not entitlement.is_valid() and not current_app.config.get('DEV_BYPASS_AUTH'):
                # 生产无有效凭证（过期/异常）→ 提示重登（绝不回落硬编码上限）
                emit('train_saved', {'ok': False, 'code': 'NO_ENTITLEMENT',
                     'detail': '登录状态已过期，请重新登录后再保存公式。'})
            else:
                emit('train_saved', {'ok': False, 'code': 'QUOTA_FULL', 'used': used, 'cap': cap,
                     'detail': f'公式库已满（免费上限 {cap} 个）。删除一个旧公式；需要更大容量可联系开发者定制扩展。'})
                emit('train_log', {'line': f'⚠️ 公式库已满（{used}/{cap}），未保存。删除旧公式后重试；需要更大容量可联系开发者定制。'})
            return
        f.saved = True
        db.session.commit()
        # 在线硬校验：向服务端占库位（离线/超额竞态→server_claimed=False 即 pending，reconcile 兜底）
        try:
            from .services.library_sync import stamp_server_claim
            stamp_server_claim(f)
        except Exception:
            pass
        emit('train_saved', {'ok': True, 'formula_id': fid, 'already': False})
        emit('train_log', {'line': f'💾 已保存到公式库：{f.ai_name}（id={fid}）'})

    # ── 量化信号生成 ──
    @socketio.on('generate_signal')
    def _on_generate_signal(data):
        """启动某跟踪公式的信号生成（后台线程）。

        训练进行中拒绝（AlphaGPT 全局态互斥）。
        """
        from .blueprints import quant as quant_bp
        data = data or {}
        tid = data.get('tracking_id')
        # 日期来自页面日期框（与 regen 共用同一组输入）；空则服务端回退默认窗口
        start = (data.get('start') or '').strip() or None
        end = (data.get('end') or '').strip() or None
        if tid is None:
            emit('quant_signal', {'error': '缺少 tracking_id'})
            return
        if runner.is_running():
            emit('quant_log', {'tracking_id': tid, 'line': '⚠️ 训练进行中，信号生成请稍后（共用引擎全局态）'})
            emit('quant_signal', {'tracking_id': tid, 'error': 'training_running'})
            return
        ok, reason = quant_bp.start_signal(int(tid), start, end)
        if not ok:
            msg = {'training_running': '训练进行中', 'already_running': '该公式正在评估中',
                   'formula_locked': '公式已锁定（公式库已满）'}.get(reason, reason)
            emit('quant_log', {'tracking_id': tid, 'line': f'⚠️ {msg}'})
            # 必须发 quant_signal error：否则前端徽标卡在"评估中"（genSignal 已置 loading）
            emit('quant_signal', {'tracking_id': tid, 'error': reason})
        else:
            emit('quant_log', {'tracking_id': tid, 'line': '🔄 信号生成已启动...'})


# ── 训练参数类型规整 ──
def _coerce_numeric(params):
    """前端表单数值字段可能为字符串，按 env_injector spec 转 int/float/bool。"""
    from .train_bridge.env_injector import _UI_INDEX, _cast
    for ui, val in list(params.items()):
        spec = _UI_INDEX.get(ui)
        if not spec or val is None or val == '':
            continue
        t = spec['type']
        try:
            if t == 'int':
                params[ui] = int(float(val))
            elif t == 'float':
                params[ui] = float(val)
            elif t == 'bool':
                if isinstance(val, str):
                    params[ui] = val.lower() in ('true', 'yes', '1')
            # str 不动
        except (TypeError, ValueError):
            pass

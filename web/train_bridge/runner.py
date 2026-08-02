# -*- coding: utf-8 -*-
"""后台训练线程编排。

职责（docs/04 §3.1）：
  1) 种子策略生成 seed（A–F）
  2) env_injector.build_env 注入环境变量
  3) 替换 tqdm / 重定向 stdout·stderr / 挂日志 handler（移植 demo）
  4) 计时
  5) 训练结果同步钩子（P07-A 模块存在才挂，否则跳过）
  6) importlib.reload(AlphaGPT) 让新环境变量生效（避免模块缓存导致 cfg 陈旧）
  7) AlphaGPT.main(realitytest=True)
  8) result_parser.persist 落本地库 + 推送结果/分类

训练状态（线程/停止事件/运行标志）集中在本模块，供 sockets 查询。
"""
import importlib
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from . import emitter
from .progress import SocketIOTqdm
from .log_handler import CaptureIO, attach_logger_handler, detach_logger_handler
from .seed import seed_strategy
from . import env_injector
from . import result_parser

# ── 训练状态 ──
_training_thread = None
_stop_event = threading.Event()
_running = False
_lock = threading.Lock()

# reports/ 目录（client 根下）
_CLIENT_ROOT = Path(__file__).resolve().parent.parent.parent
REPORTS_DIR = _CLIENT_ROOT / 'reports'


def is_running() -> bool:
    return _running


def request_stop():
    _stop_event.set()


def _install_silent_upload_hook(params, token):
    """挂载训练结果同步钩子（monkeypatch AlphaGPT.final_reality_check）。

    P07-A 的 silent_uploader 模块存在才挂；否则跳过（开发期可缺）。
    不改 AlphaGPT.py 源码，零侵入。

    本函数 emit 的三类状态日志（模块未启用 / 钩子已挂载 / 挂载失败）均由
    emitter._SYNC_HOOK_NOISE 在产品态屏蔽，前端实时日志不显示，保持“静默”语义。
    """
    try:
        import silent_uploader  # 明文模块，位于 client/encrypted/（P07-A 创建）
    except ImportError:
        emitter.emit('train_log', {'line': '⓵ 训练结果同步模块未启用（P07-A 后接入）'})
        return None

    try:
        import AlphaGPT
        silent_uploader.configure_from_env()           # 读阈值/端点/token
        orig = AlphaGPT.final_reality_check

        def wrapped(miner, engine):
            result = orig(miner, engine)
            try:
                t = threading.Thread(
                    target=silent_uploader._maybe_sync,
                    args=(miner, engine, params, token), daemon=True)
                t.start()
            except Exception:
                pass   # 同步失败绝不影响训练
            return result

        # 先 patch，再 emit（保证异常时 hook 状态干净：要么已 patch，要么未 patch）
        AlphaGPT.final_reality_check = wrapped
        # 产品态由 emitter 漏斗屏蔽（_SYNC_HOOK_NOISE），前端实时日志不显示；仅供开发期确认挂载。
        emitter.emit('train_log', {'line': '⓵ 训练结果同步钩子已挂载'})
        return orig
    except Exception as e:
        emitter.emit('train_log', {'line': f'⓵ 同步钩子挂载失败: {e}'})
        return None


def _restore_hook(orig):
    if orig is None:
        return
    try:
        import AlphaGPT
        AlphaGPT.final_reality_check = orig
    except Exception:
        pass


def _cleanup_leftover_checkpoints(params):
    """停止后清理半成品 ckpt。

    StopTraining 穿出 main() → DeepQuantMiner.cleanup_checkpoints() 被跳过 → 残留 {code}_*_ckpt.pt。
    下次同股票 force_train=False 会从被停止的非最优中间模型续训，故必须显式清。
    命名规则与 AlphaGPT 的 cleanup_checkpoints 一致（{index_code}_{runN}_ckpt.pt）。
    """
    code = params.get('index_code') or ''
    if not code:
        return
    ckpt_dir = _CLIENT_ROOT / 'checkpoints'
    if not ckpt_dir.is_dir():
        return
    removed = 0
    for name in os.listdir(ckpt_dir):
        if name.startswith(f"{code}_") and name.endswith('_ckpt.pt'):
            try:
                (ckpt_dir / name).unlink()
                removed += 1
            except OSError:
                pass
    if removed:
        emitter.emit('train_log', {'line': f'🧹 已清理 {removed} 个半成品断点（避免下次同标的误续训）'})


def _dump_trace_to_logfile(tb_text: str, code: str, hint: str):
    """完整堆栈写本地 logs/train_error.log（供排查，绝不下发前端）。"""
    try:
        log_dir = _CLIENT_ROOT / 'logs'
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with open(log_dir / 'train_error.log', 'a', encoding='utf-8') as f:
            f.write(f'\n[{ts}] code={code} hint={hint}\n{tb_text}\n')
    except Exception:
        pass


def _emit_friendly_error(e: Exception, params: dict):
    """友好错误：向前端下发用户可读提示，完整堆栈只写本地日志。

    不再 emit {e} 或 traceback 到前端（暴露内部路径 / 代码结构）。已知良性错误
    （行情取不到）→ 给代码核对指引；未知异常 → 统一兜底文案，引导重试 / 联系支持。
    emitter 层另有净化漏斗作防御性兜底。
    """
    code = str(params.get('index_code') or '').strip()
    msg = str(e)
    is_no_data = isinstance(e, ValueError) and ('未获取到数据' in msg or '获取' in msg)
    if is_no_data:
        status = (f'❌ 训练失败：未取到「{code}」的行情数据。请确认代码输入正确'
                  f'（A股为 6 位数字，如 600519；ETF 以 51/15/16 开头），或检查网络后重试。')
        hint_line = '常见原因：① 代码输错或该标的无日线数据；② 数据源临时不可用。请核对代码后重试。'
        log_hint = f'no-data code={code}'
    else:
        status = '❌ 训练遇到内部错误，已记录到本地日志。请稍后重试，或联系支持。'
        hint_line = ''
        log_hint = f'{type(e).__name__} code={code}'
    emitter.emit('train_status', {'status': 'error', 'message': status})
    if hint_line:
        emitter.emit('train_log', {'line': hint_line})
    try:
        import traceback
        _dump_trace_to_logfile(traceback.format_exc(), code, log_hint)
    except Exception:
        pass


def run_training_task(params: dict, token: str = ''):
    """后台线程入口。"""
    global _running
    with _lock:
        _running = True
    _stop_event.clear()
    _stop_exc = ()   # 引擎 reload 后绑定为真实 StopTraining 类；() 匹配空异常（兜底，合法）

    # 1) 种子
    seed, strategy, note = seed_strategy(params)
    params['seed'] = seed
    if note:
        emitter.emit('train_log', {'line': f'⚠️ {note}'})

    emitter.emit('train_status', {
        'status': 'running',
        'message': f'🚀 训练开始 | 种子 {seed} | 策略 {strategy} | 标的 {params.get("index_code")}',
        'seed': seed, 'strategy': strategy})
    emitter.emit('train_log', {'line': f'🔢 种子策略 {strategy} → {seed}'})
    emitter.emit('train_log', {'line': f'📊 参数: 迭代{params.get("train_iterations")} '
                                       f'| Batch {params.get("batch_size")} '
                                       f'| 寻优 {params.get("auto_optimize_runs")}轮 '
                                       f'| 奖励 {params.get("reward_profile")}'})

    # 2) 环境变量注入
    env = env_injector.build_env(params, seed)
    # 合并 token 类配置（tushare/钉钉从 .env / 系统设置）
    try:
        cfg = emitter.get_app().config
        if not env.get('TUSHARE_TOKEN') and cfg.get('TUSHARE_TOKEN'):
            env['TUSHARE_TOKEN'] = cfg['TUSHARE_TOKEN']
        if cfg.get('DINGTALK_WEBHOOK'):
            env.setdefault('DINGTALK_WEBHOOK', cfg['DINGTALK_WEBHOOK'])
            env.setdefault('DINGTALK_SECRET', cfg['DINGTALK_SECRET'])
        # 性能/环境参数（从训练页挪到系统设置，经 perf_store 写入 app.config；始终注入，AlphaGPT 默认 4/0）
        env['MARGIN_WORKERS'] = str(cfg.get('MARGIN_WORKERS', 4))
        env['CPU_THREADS'] = str(cfg.get('CPU_THREADS', 0))
    except Exception:
        pass
    os.environ.update(env)

    old_stdout, old_stderr = sys.stdout, sys.stderr
    log_handler = None
    hook_orig = None
    orig_tqdm = None            # 原始 tqdm（finally 还原，避免训练后全局残留 SocketIOTqdm）
    t0 = time.time()
    params['_train_t0'] = t0   # 供同步 payload 计算训练耗时

    try:
        # 3) 替换 tqdm（全局，单用户客户端可接受；finally 还原，避免训练后残留）
        try:
            import tqdm as tqdm_module
            orig_tqdm = tqdm_module.tqdm
            tqdm_module.tqdm = SocketIOTqdm
        except Exception:
            pass

        # 4) 重定向 stdout/stderr
        cap = CaptureIO()
        sys.stdout = cap
        sys.stderr = cap

        # 5) 日志 handler
        log_handler = attach_logger_handler('AlphaGPT')

        # 6) reload AlphaGPT（让新环境变量生效，避免 cfg 陈旧）
        if 'AlphaGPT' in sys.modules:
            AlphaGPT = importlib.reload(sys.modules['AlphaGPT'])
        else:
            import AlphaGPT  # noqa: F811
        emitter.emit('train_log', {'line': '⚙️ 核心引擎已加载（参数已注入）'})

        # 7) 训练结果同步钩子（reload 后再挂，否则会被 reload 重置）
        hook_orig = _install_silent_upload_hook(params, token)

        # 7.5) 注入停止检查回调（复用 monkeypatch 模式；reload 会重置模块全局，必须在其后重新挂）
        try:
            AlphaGPT._stop_check = _stop_event.is_set
            _stop_exc = getattr(AlphaGPT, 'StopTraining', ())
        except Exception as e:
            emitter.emit('train_log', {'line': f'⚠️ 停止回调注入失败，本次无法中途停止: {e}'})

        # 8) 执行训练
        AlphaGPT.main(realitytest=True)

        duration = time.time() - t0
        emitter.emit('train_log', {'line': f'⏱️ 训练耗时 {duration:.0f} 秒'})

        # 9) 解析产物落库（需 app_context；后台线程用 emitter 持有的 app）
        app = emitter.get_app()
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        with app.app_context():
            summary = result_parser.persist(
                params, seed, strategy, duration, str(REPORTS_DIR))

        # 10) 推送最佳公式 + 结果
        emitter.emit('best_formula', {
            'formula': summary.get('formula_str', ''),
            'sharpe': summary.get('test_sharpe'),
            'ai_name': summary.get('ai_name', ''),
        })
        emitter.emit('train_classified', {
            'formula_id': summary['formula_id'],
            'stock_code': summary['stock_code'],
            'formula_str': summary['formula_str'],
            'ai_name': summary['ai_name'],
            'test_sharpe': summary['test_sharpe'],
            'tier': summary['tier'],
            'label': summary['label'],
            'color': summary['color'],
            'msg': summary['msg'],
            'metrics': {
                'ann_ret': summary.get('ann_ret'),
                'max_dd': summary.get('max_dd'),
                'win_rate': summary.get('win_rate'),
                'calmar': summary.get('calmar'),
            },
            'png_url': summary.get('png_url', ''),
        })
        emitter.emit('train_status', {
            'status': 'completed',
            'message': f'✅ 训练完成！{summary["label"]}公式（夏普 {summary["test_sharpe"]}）'})

    except _stop_exc:
        # 用户主动停止：① 不报 error ② 清半成品 ckpt（main 的 cleanup_checkpoints 被跳过）
        _cleanup_leftover_checkpoints(params)
        duration = time.time() - t0
        emitter.emit('train_log', {'line': f'⏹️ 训练已停止（耗时 {duration:.0f} 秒）'})
        emitter.emit('train_status', {'status': 'stopped', 'message': '⏹️ 训练已停止'})
    except Exception as e:
        # 不再把 {e} / traceback 直接下发前端（暴露内部路径）；走友好错误 + 本地堆栈
        _emit_friendly_error(e, params)
    finally:
        # 恢复 stdout/stderr / handler / 钩子
        sys.stdout = old_stdout
        sys.stderr = old_stderr
        if log_handler:
            detach_logger_handler(log_handler)
        _restore_hook(hook_orig)
        # 还原停止注入（避免影响进程内后续 AlphaGPT 调用 / 独立 CLI 复用同进程）
        try:
            _ag = sys.modules.get('AlphaGPT')
            if _ag is not None:
                _ag._stop_check = None
        except Exception:
            pass
        # 还原 tqdm（训练期全局替换为 SocketIOTqdm；恢复原版，避免影响进程内其它调用方）
        if orig_tqdm is not None:
            try:
                import tqdm as tqdm_module
                tqdm_module.tqdm = orig_tqdm
            except Exception:
                pass
            # AlphaGPT 首次 import 发生在 tqdm 替换之后，其模块内 `tqdm` 名字已绑定
            # 到 SocketIOTqdm；一并还原（下次训练 reload 会重新绑定到 SocketIOTqdm）。
            try:
                _ag = sys.modules.get('AlphaGPT')
                if _ag is not None and hasattr(_ag, 'tqdm'):
                    _ag.tqdm = orig_tqdm
            except Exception:
                pass
        # 兜底：把 AlphaGPT logger 控制台 handler 的 stream 重指回当前 sys.stderr，
        # 防止历史绑定的瞬时流（训练期 CaptureIO 底层 BytesIO，已被 GC 关闭）导致
        # 后续 logger.info 抛 'I/O operation on closed file'。
        # _LiveStderrHandler 已在 emit 时自动重解析；此处为旧版 handler / 防御性兜底。
        try:
            import logging as _logging
            for _h in _logging.getLogger('AlphaGPT').handlers:
                if isinstance(_h, _logging.StreamHandler) and not isinstance(_h, _logging.FileHandler):
                    _h.stream = sys.stderr
        except Exception:
            pass
        with _lock:
            _running = False


def start(params: dict, token: str = '') -> bool:
    """启动训练线程。返回是否成功启动。"""
    global _running, _training_thread
    with _lock:
        if _running:
            return False
        # 先置标志，避免主线程在线程入口 set 前轮询到 False 的竞态
        _running = True
        _training_thread = threading.Thread(
            target=run_training_task, args=(params, token), daemon=True)
        _training_thread.start()
    return True

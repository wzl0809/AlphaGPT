# -*- coding: utf-8 -*-
"""信号 + 基准图生成 —— 复用 AlphaGPT 引擎评估公式，一次运行同时产出
「下一交易日看多/观望信号」与「策略净值回测图」，保证图与信号同源（不错位）。

核心（docs/P04 §1）：
  1) apply_selected_features(factors) 重建训练时的 FEATURES（token 索引依赖）
  2) DataEngine.load() 取数 + 算因子 + 归一化
  3) DeepQuantMiner.solve_one(tokens) 得全序列因子值 f_vals（单次求值）
  4) 由 f_vals 同时派生：
       - 信号：sign(tanh(f_last)) → only_long 下 1=看多 / 0=观望，日期为下一交易日
       - 基准图：sign(tanh(f_vals)) × target_oto_ret 累计净值 vs 持有基准

⚠️ 全局态：apply_selected_features 会改写 AlphaGPT.FEATURES/VOCAB，与训练共用
   全局变量。因此不得与训练并发——由调用方用 training_running() 拦截。
"""
import math
import threading

# 引擎评估与训练互斥（都动 AlphaGPT 全局态）
_engine_lock = threading.Lock()

# 近 N 日前向命中率窗口（真·准确度，区别于 confidence 的信号强度）
HIT_WINDOW = 20


def compute_hit_rate(pos_np, oto_np, window: int = HIT_WINDOW, unresolved_tail: int = 2):
    """近 `window` 日前向命中率：每日「买/空」预测方向 vs 次日实际涨跌方向。

    命中 = (pos==1 且 oto>0) 或 (pos==0 且 oto<=0)；即「看多且涨 / 看空且未涨」。
    末尾 `unresolved_tail` 根（持仓未到期 / 当前信号，收益尚未实现）不计入。

    Args:
        pos_np: 每日仓位 0/1（numpy）
        oto_np: 每日 OTO 收益（numpy，与 backtest 同源；oto[i] 为 bar i 信号的前向收益）
    Returns:
        (rate: float|None, total: int)  rate∈[0,1]，无足够样本时 None
    """
    import numpy as np
    m = min(len(pos_np), len(oto_np))
    end = max(0, m - unresolved_tail)          # 排除末尾未到期 bar
    w = min(window, end)
    if w <= 0:
        return None, 0
    sp = pos_np[end - w:end]
    sr = oto_np[end - w:end]
    mask = np.isfinite(sr)
    sp, sr = sp[mask], sr[mask]
    if len(sr) == 0:
        return None, 0
    hits = int(np.sum((sp > 0.5) == (sr > 0)))
    return round(hits / len(sr), 3), int(len(sr))


def evaluate_formula(formula, start, end, tracking_id=None) -> dict:
    """evaluate_formula 对外入口：包 lifecycle.mark_busy()，使同步调用（基准图 regen
    路由）与异步调用（信号生成线程）都被 busy 守卫覆盖——杜绝"用户在 regen/评估途中
    关闭浏览器→空闲兜底误杀引擎"的窗口。返回值原样透传。"""
    from .. import lifecycle   # 懒导入避免循环依赖
    with lifecycle.mark_busy():
        return _evaluate_formula_impl(formula, start, end, tracking_id)


def _evaluate_formula_impl(formula, start, end, tracking_id=None) -> dict:
    """一次引擎运行 → 同时产出策略净值图(png basename) + 下一交易日信号
    + （传 tracking_id 时）两份报告 HTML（Final Reality Check / 次日信号日志）。

    单趟：apply_selected_features → DataEngine.load → build_feat_data →
    DeepQuantMiner.solve_one → f_vals。由 f_vals 同时派生：
      (a) 全序列净值曲线（画图）
      (b) 末根 bar 的 sign(tanh(f_last)) → 看多/观望 + 下一交易日日期
      (c) 报告 HTML（整个窗口，与基准图同源）
    start/end 容忍 'YYYY-MM-DD' 或 'YYYYMMDD'（内部 strip 横线）。
    tracking_id 非空时，报告写入 reports/qt{tracking_id}_reality.html 与 _signal.html（覆写）。

    Returns:
      成功: {'png', 'signal', 'confidence', 'factor_value', 'last_date', 'factors_used',
             'fresh', 'basis_date', 'hit_rate', 'hit_total'}
             fresh=True 表示末根 bar=今日收盘（数据新鲜）；False 表示数据源滞后、
             信号基于 basis_date（上一交易日）收盘，下游须降级提示而非直接点亮圆圈。
      失败: {'error': ..., 'signal': 'hold'[, 'trace': ...]}   # 无 'png' 键，调用方先查 'error'
    """
    tokens = list(formula.tokens or [])
    if not tokens:
        return {'error': '公式无 tokens', 'signal': 'hold'}

    # 日期规整（容忍带横线：socket 路径传 YYYY-MM-DD，regen 路径已 strip）
    start = str(start).replace('-', '')
    end = str(end).replace('-', '')

    with _engine_lock:
        try:
            import os
            from datetime import datetime
            import numpy as np
            import matplotlib.pyplot as plt
            import torch

            import AlphaGPT
            from AlphaGPT import DataEngine, DeepQuantMiner, apply_selected_features

            # 1) 重建训练时因子顺序（token 索引对齐）
            feats = [x.strip() for x in (formula.factors or '').split(',') if x.strip()] \
                    or list(AlphaGPT.FEATURES)
            apply_selected_features(feats)

            # 2) 取数 + 因子（显式传 code/date，不依赖 cfg 全局）
            eng = DataEngine(index_code=str(formula.stock_code),
                             start_date=start, end_date=end,
                             only_long=True, use_fixed_hold=True, fixed_hold_days=2)
            eng.load()
            eng.build_feat_data(feats)

            # 3) 单次求值
            miner = DeepQuantMiner(eng)
            f_vals = miner.solve_one(tokens)
            if f_vals is None:
                return {'error': '公式无法求值（无效或常数）', 'signal': 'hold'}

            # 4a) 信号 —— 末根 bar 预测「下一交易日」仓位
            last = float(f_vals[-1].item())
            t = math.tanh(last)
            pos = 1 if t > 0 else 0
            signal = 'buy' if pos == 1 else 'hold'
            confidence = abs(t)

            # 数据新鲜度守卫（docs/P04：取得当日收盘后才能生成次日准确信号）。
            # 末根 bar 必须等于「当前应已收盘的最新交易日」，否则数据源滞后
            # （如 baostock 收盘后日终更新前只到昨天）→ 信号基于旧数据，下游须降级提示。
            from .trading_calendar import next_trading_day, expected_latest_close
            try:
                last_bar_date = eng.dates.iloc[-1].date()
            except Exception:
                last_bar_date = None
            expected = expected_latest_close()           # 当前应已收盘的最新交易日
            fresh = bool(last_bar_date is not None and last_bar_date == expected)
            basis_date = last_bar_date.strftime('%Y-%m-%d') if last_bar_date else None
            # 目标日 = 末根 bar 的下一交易日（节假日感知，不再只跳周末）
            last_date = next_trading_day(last_bar_date).strftime('%Y-%m-%d') \
                if last_bar_date else str(getattr(eng, 'dates', ['?'])[-1])

            # 4b) 基准图 —— 全序列净值（与 backtest 同源 target_oto_ret）
            sig = torch.tanh(f_vals)
            pos_t = torch.sign(sig)
            pos_t[pos_t < 0] = 0                                   # only_long → 0/1
            oto = eng.target_oto_ret                               # 每日 OTO 收益（与 backtest 同源）
            n = min(pos_t.shape[0], oto.shape[0])
            strat_ret = (pos_t[:n] * oto[:n]).detach().cpu().numpy()
            bench_ret = oto[:n].detach().cpu().numpy()
            strat_eq = np.cumprod(1.0 + strat_ret)
            bench_eq = np.cumprod(1.0 + bench_ret)

            # 近 N 日前向命中率（真·准确度）：每日「买/空」方向 vs 次日实际涨跌
            hit_rate, hit_total = compute_hit_rate(
                pos_t[:n].detach().cpu().numpy(), oto[:n].detach().cpu().numpy())
            try:
                dates = [d.strftime('%m-%d') for d in eng.dates.iloc[-n:]]
            except Exception:
                dates = list(range(n))

            ann = (strat_eq[-1] ** (252.0 / max(n, 1)) - 1.0) if strat_eq[-1] > 0 else 0.0
            sharpe = (strat_ret.mean() / (strat_ret.std() + 1e-8)) * (252.0 ** 0.5)

            # 中文字体（复用引擎 final_reality_check 的字体探测）
            import matplotlib.font_manager as fm
            preferred = ['SimHei', 'Microsoft YaHei', 'WenQuanYi Micro Hei',
                         'WenQuanYi Zen Hei', 'Noto Sans CJK SC', 'STHeiti']
            avail = {f.name for f in fm.fontManager.ttflist}
            cjk = next((fn for fn in preferred if fn in avail), None)
            if cjk:
                plt.rcParams['font.sans-serif'] = [cjk, 'DejaVu Sans']
                plt.rcParams['axes.unicode_minus'] = False

            x = range(n)
            fig, ax = plt.subplots(figsize=(12, 6))
            ax.plot(x, bench_eq, color='#f59e0b', linestyle='--', alpha=0.75, linewidth=1.2,
                    label='基准（持有）')                          # 基准：橙虚线
            ax.plot(x, strat_eq, color='#2563eb', linewidth=2, label='策略净值')   # 策略：蓝实线
            ax.fill_between(x, 1, strat_eq, where=(strat_eq >= 1), color='#10b981', alpha=0.12, interpolate=True)
            ax.fill_between(x, 1, strat_eq, where=(strat_eq < 1), color='#ef4444', alpha=0.10, interpolate=True)
            ax.axhline(1.0, color='#94a3b8', linewidth=0.7, linestyle=':')         # 净值=1 基线
            ax.set_title(f'{formula.stock_code} 策略回测  {start}~{end}\n'
                         f'年化 {ann:.1%}  ·  夏普 {sharpe:.3f}  ·  末值 {strat_eq[-1]:.2f}',
                         fontsize=12, fontweight='bold')
            ax.set_ylabel('净值')
            ax.legend(loc='upper left', framealpha=0.9)
            ax.grid(True, alpha=0.3)
            if n > 8:
                step = max(1, n // 8)
                ax.set_xticks(range(0, n, step))
                ax.set_xticklabels(dates[::step], rotation=30, fontsize=8)
            plt.tight_layout()
            # 基准图写公式属主的用户目录 reports/<h>/（账号隔离；/reports-file 按属主目录服务）
            from web.services import storage as _storage
            reports_dir = str(_storage.user_reports_dir(getattr(formula, 'owner_email', '') or ''))
            os.makedirs(reports_dir, exist_ok=True)
            fname = f"strategy_performance_{formula.stock_code}_{end}_{datetime.now().strftime('%H%M%S')}.png"
            plt.savefig(os.path.join(reports_dir, fname), dpi=120); plt.close()

            # 删除该公式上一张基准图（只保留最新一张，避免每日生成导致 reports/ 无限堆积）
            old = getattr(formula, 'png_path', None)
            if old and old != fname:
                try:
                    os.remove(os.path.join(reports_dir, old))
                except OSError:
                    pass

            # 顺带生成两份报告 HTML（与基准图同源同窗口）；确定性文件名覆写，不堆积。
            # 报告生成失败不阻断主流程（图+信号已成功）。
            if tracking_id is not None:
                try:
                    from .reports import build_reality_html, build_signal_log_html
                    reality_html = build_reality_html(eng, pos_t, oto, formula, feats)
                    signal_html = build_signal_log_html(eng, pos_t, oto, formula, feats, confidence=confidence)
                    with open(os.path.join(reports_dir, f'qt{tracking_id}_reality.html'), 'w', encoding='utf-8') as f:
                        f.write(reality_html)
                    with open(os.path.join(reports_dir, f'qt{tracking_id}_signal.html'), 'w', encoding='utf-8') as f:
                        f.write(signal_html)
                except Exception:
                    pass

            return {
                'png': fname,
                'signal': signal,
                'confidence': round(confidence, 3),
                'factor_value': round(last, 4),
                'last_date': last_date,
                'factors_used': feats,
                'fresh': fresh,                 # 末根 bar 是否=今日收盘（数据新鲜度）
                'basis_date': basis_date,       # 信号实际基于哪天收盘（未取得当日时为上一交易日）
                'hit_rate': hit_rate,           # 近 20 日前向命中率（真·准确度），无样本则 None
                'hit_total': hit_total,         # 命中率统计样本数
            }
        except Exception as e:
            import traceback
            return {'error': f'{e}', 'signal': 'hold',
                    'trace': traceback.format_exc()[-400:]}

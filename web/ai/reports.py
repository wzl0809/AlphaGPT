# -*- coding: utf-8 -*-
"""量化页报告生成（web 独立，不动 AlphaGPT.py）。

把 evaluate_formula 已算出的 pos / oto / eng 整理成两份 HTML，统计窗口为
**整个基准图区间**（不做测试集后 20% 切片），与基准图同源同窗口。

  1) build_reality_html     —— 样本外回测报告 Final Reality Check（核心指标 + 交易统计）
  2) build_signal_log_html  —— AlphaGPT-Evo 次日信号日志（逐日行 + 投资次数/胜率 Summary）

口径复刻 CLI 的 final_reality_check / show_latest_positions（含换手手续费、涨跌停过滤、
fixed_hold 出场回放），但窗口=整个 [start,end]。返回自包含 HTML 字符串（含 <style>），
由前端 fetch /reports-file 后直接 innerHTML 注入弹框。
"""
import html as _html


def _esc(s):
    return _html.escape(str(s)) if s is not None else ''


def _get_cost_rate():
    """取 AlphaGPT.COST_RATE；取不到（如隔离测试）回退默认 0.0004。"""
    try:
        from AlphaGPT import COST_RATE
        return COST_RATE
    except Exception:
        return 0.0004


_STYLE = """
<style scoped>
 .rpt{font-family:-apple-system,"Microsoft YaHei",sans-serif;color:#1f2937;font-size:13px;line-height:1.6;background:#fff;border-radius:6px}
 .rpt h2{font-size:16px;margin:0 0 8px;color:#111827}
 .rpt h3{font-size:13px;margin:14px 0 6px;color:#374151;border-left:3px solid #2563eb;padding-left:6px}
 .rpt .sub{color:#6b7280;margin-bottom:10px}
 .rpt table{border-collapse:collapse;width:100%;margin:4px 0}
 .rpt th,.rpt td{border:1px solid #e5e7eb;padding:4px 8px;text-align:left}
 .rpt th{background:#f9fafb;font-weight:600;width:38%}
 .rpt td.v{text-align:right;font-variant-numeric:tabular-nums}
 .rpt .callout{background:#eff6ff;border:1px solid #bfdbfe;border-radius:6px;padding:8px 10px;margin:8px 0}
 .rpt .pos-buy{color:#dc2626;font-weight:700}
 .rpt .pos-hold{color:#6b7280;font-weight:700}
 .rpt .scroll{max-height:46vh;overflow:auto;border:1px solid #e5e7eb;border-radius:6px}
 .rpt tr:last-child{background:#fef9c3}
 .rpt code{background:#f3f4f6;padding:1px 4px;border-radius:3px}
</style>
"""


def _metric_table(rows):
    """rows: list[(label, value_str)]. 返回两列表格 HTML。"""
    body = ''.join(f"<tr><th>{_esc(lab)}</th><td class='v'>{val}</td></tr>"
                   for lab, val in rows)
    return "<table><tbody>" + body + "</tbody></table>"


def _limit_thresholds(code: str):
    """按板块给涨跌停阈值（与 CLI final_reality_check 一致）。"""
    if code.startswith(('51', '15', '16')):
        return 0.50, -0.50            # ETF 宽阈值
    if code.startswith(('688', '30')):
        return 0.195, -0.195          # 科创/创业 ±20%
    return 0.095, -0.095              # 主板 ±10%


def build_reality_html(eng, pos_t, oto, formula, feats) -> str:
    """Final Reality Check —— 核心指标 + 交易统计（整个窗口）。"""
    import numpy as np
    COST_RATE = _get_cost_rate()

    pos = pos_t.detach().cpu().numpy().astype(float)
    ret = oto.detach().cpu().numpy().astype(float)
    raw_open = eng.raw_open.detach().cpu().numpy().astype(float)
    raw_close = eng.raw_close.detach().cpu().numpy().astype(float)
    dates = list(eng.dates)
    n = min(len(pos), len(ret), len(raw_open), len(raw_close), len(dates))
    pos, ret = pos[:n], ret[:n]
    raw_open, raw_close = raw_open[:n], raw_close[:n]
    dates_w = dates[:n]

    # 涨跌停过滤：次日开盘相对前收的跳空超阈值 → 当日不可成交，收益置 0
    up, dn = _limit_thresholds(eng.index_code)
    valid = np.ones(n, dtype=bool)
    for t in range(n - 1):
        co = raw_close[t]
        if co != 0:
            gap = (raw_open[t + 1] - co) / co
            if gap > up or gap < dn:
                valid[t] = False
    ret_f = ret * valid

    # 换手 + 手续费
    turnover = np.abs(pos - np.concatenate([[0.0], pos[:-1]]))
    daily_ret = pos * ret_f - turnover * COST_RATE
    equity = np.cumprod(1.0 + daily_ret)
    total_ret = equity[-1] - 1
    ann_ret = equity[-1] ** (252.0 / len(equity)) - 1
    vol = np.std(daily_ret) * np.sqrt(252.0)
    sharpe = ann_ret / (vol + 1e-6)
    dd = 1 - equity / np.maximum.accumulate(equity)
    max_dd = float(np.max(dd)) if len(dd) else 0.0
    calmar = ann_ret / (max_dd + 1e-6)

    pos_mask = (pos == 1)
    total_positions = int(np.sum(pos_mask))
    if total_positions > 0:
        success_count = int(np.sum(pos_mask & (ret_f > 0)))
        success_rate = success_count / total_positions
    else:
        success_count, success_rate = 0, 0.0
    trade_idx = np.where(pos_mask)[0]
    wins = [float(ret_f[i]) for i in trade_idx if ret_f[i] > 0]
    losses = [float(ret_f[i]) for i in trade_idx if ret_f[i] <= 0]
    avg_win = float(np.mean(wins)) if wins else 0.0
    avg_loss = float(np.mean(losses)) if losses else 0.0
    pl_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else float('inf')

    d0 = dates_w[0].date() if len(dates_w) else None
    d1 = dates_w[-1].date() if len(dates_w) else None
    title = f"{_esc(eng.index_code)}"

    core = [
        ('回测周期', f"{d0} ~ {d1}"),
        ('交易天数', f"{len(equity)} 天"),
        ('总收益率', f"{total_ret:.2%}"),
        ('年化收益率', f"{ann_ret:.2%}"),
        ('年化波动率', f"{vol:.2%}"),
        ('夏普比率', f"{sharpe:.3f}"),
        ('最大回撤', f"{max_dd:.2%}"),
        ('Calmar 比率', f"{calmar:.3f}"),
    ]
    trade = [
        ('看多信号次数', f"{total_positions}"),
        ('盈利次数', f"{success_count}"),
        ('亏损次数', f"{total_positions - success_count}"),
        ('样本外胜率', f"{success_rate:.1%}"),
        ('平均盈利', f"{avg_win:.2%}"),
        ('平均亏损', f"{avg_loss:.2%}"),
        ('盈亏比', f"{pl_ratio:.2f}"),
    ]
    return _STYLE + f"""
<div class="rpt">
  <h2>📊 样本外回测报告 Final Reality Check [{_esc(eng.index_code)}]</h2>
  <div class="sub">{title}</div>
  <h3>📈 核心指标</h3>
  {_metric_table(core)}
  <h3>🎯 交易统计</h3>
  {_metric_table(trade)}
  <div class="sub" style="margin-top:10px">口径：整个基准图区间；含换手手续费；涨跌停日不可成交（收益置 0）。</div>
</div>
""".strip()


def _next_trading_day(last_date):
    """从最后一根 bar 前进到下一交易日（节假日感知，委托 trading_calendar）。"""
    from .trading_calendar import next_trading_day
    return next_trading_day(last_date)


def build_signal_log_html(eng, pos_t, oto, formula, feats, confidence=None) -> str:
    """AlphaGPT-Evo 次日信号日志 —— 逐日行 + Summary（整个窗口）。"""
    import numpy as np
    COST_RATE = _get_cost_rate()

    pos = pos_t.detach().cpu().numpy().astype(float)
    ret = oto.detach().cpu().numpy().astype(float)
    all_open = eng.raw_open.detach().cpu().numpy().astype(float)
    all_close = eng.raw_close.detach().cpu().numpy().astype(float)
    dates = list(eng.dates)
    n = min(len(pos), len(ret), len(all_open), len(all_close), len(dates))
    pos, ret = pos[:n], ret[:n]
    dates_w = dates[:n]
    k = int(getattr(eng, 'fixed_hold_days', 2))

    # 涨跌停过滤（与 build_reality_html 同口径）：次日开盘跳空超阈值 → 当日不可成交，收益置 0。
    # 修复：旧版逐日收益不过滤涨跌停，与回测报告同窗口同数据给出矛盾的总收益。
    up_lim, dn_lim = _limit_thresholds(eng.index_code)
    valid = np.ones(n, dtype=bool)
    for t in range(n - 1):
        co = all_close[t]
        if co != 0:
            gap = (all_open[t + 1] - co) / co
            if gap > up_lim or gap < dn_lim:
                valid[t] = False

    turnover = np.abs(pos - np.concatenate([[0.0], pos[:-1]]))
    simple_sum = 0.0
    compound = 1.0
    inv_count = 0
    profit_count = 0
    rows_html = []

    for i in range(n):
        date_str = dates_w[i].strftime('%Y-%m-%d')
        pos_value = pos[i]
        # 固定持仓期出场回放：买 open_{t+1}，卖 open_{t+k}
        if i + k < len(all_open) and i + 1 < len(all_open):
            buy_open = all_open[i + 1]
            sell_open = all_open[i + k]
            if buy_open != 0:
                ret_value = float(ret[i]) * valid[i]   # 涨跌停日 valid[i]=0 → 收益置 0（与回测报告同口径）
                d1_open = f"{buy_open:.3f}"
                exit_open = f"{sell_open:.3f}"
                exit_idx = i + k
                exit_date = dates_w[exit_idx].strftime('%Y-%m-%d') if exit_idx < len(dates_w) else 'N/A'
            else:
                d1_open = exit_open = 'N/A'; exit_date = 'N/A'
                ret_value = 0.0; pos_value = 0.0
        else:
            # 未来数据不足 → 持仓尚未到期，不纳入统计
            d1_open = exit_open = 'N/A'; exit_date = 'N/A'
            ret_value = 0.0; pos_value = 0.0

        simple_sum += pos_value * ret_value
        compound *= (1.0 + (pos_value * ret_value - turnover[i] * COST_RATE))
        if pos_value == 1:
            inv_count += 1
            if ret_value > 0:
                profit_count += 1

        if ret_value > 0:
            emoji, ret_disp = '🔴', f"+{ret_value:.2%}"
        elif ret_value < 0:
            emoji, ret_disp = '🟢', f"{ret_value:.2%}"
        else:
            emoji, ret_disp = '⚪', '0.00%'
        pos_info = '+' if int(pos_value) == 1 else '−'
        pos_cls = 'pos-buy' if int(pos_value) == 1 else 'pos-hold'
        rows_html.append(
            f"<tr><td>{_esc(date_str)}</td><td class='{pos_cls}'>{pos_info}</td>"
            f"<td class='v'>{emoji} {ret_disp}</td><td class='v'>{_esc(d1_open)}</td>"
            f"<td class='v'>{_esc(exit_open)}</td><td>{_esc(exit_date)}</td></tr>"
        )

    win_rate = (profit_count / inv_count) if inv_count > 0 else 0.0

    # 末行 = 下一交易日信号（日期前进到下一交易日，节假日感知）
    last_pos = int(pos[n - 1]) if n > 0 else 0
    last_bar_date = dates_w[-1].date() if len(dates_w) else None
    next_date = _next_trading_day(last_bar_date) if last_bar_date is not None else None
    if confidence is not None:
        import math
        _fv = math.atanh(min(confidence, 0.999)) * (1 if last_pos else -1)
        next_label = f'因子值 {_fv:+.2f}（强度 {confidence*100:.0f}%）'
    else:
        next_label = '因子值 —'
    next_cls = 'pos-buy' if last_pos == 1 else 'pos-hold'

    # 数据新鲜度 + 近 20 日前向命中率（真·准确度）
    from .trading_calendar import expected_latest_close
    from .signal import compute_hit_rate
    expected = expected_latest_close()
    fresh = bool(last_bar_date is not None and last_bar_date == expected)
    hit_rate, hit_total = compute_hit_rate(pos, ret)
    basis_str = last_bar_date.strftime('%Y-%m-%d') if last_bar_date else '—'
    fresh_str = '✅ 数据新鲜' if fresh else '⚠️ 当日数据未取得（基于上日收盘）'

    title = f"{_esc(eng.index_code)}"
    summary = [
        ('投资次数', f"{inv_count}"),
        ('盈利次数', f"{profit_count}"),
        ('胜率', f"{win_rate:.2%}"),
        ('信号强度', f"{confidence*100:.0f}%（|tanh|，强度非概率）" if confidence is not None else '—'),
        ('近20日命中率', f"{hit_rate:.0%}（{hit_total} 样本）" if hit_rate is not None else '—'),
        ('简单收益', f"{simple_sum:.2%}"),
        ('复合收益', f"{compound - 1:.2%}"),
    ]

    return _STYLE + f"""
<div class="rpt">
  <h2>📋 AlphaGPT-Evo 策略信号日志 [{_esc(eng.index_code)}]</h2>
  <div class="sub">{title}</div>
  <div class="callout">📌 <b>下一交易日（{next_date}）信号：</b>
      <span class='{next_cls}'>{next_label}</span><br>
      <span class="sub">信号基于 {_esc(basis_str)} 收盘 · {fresh_str}</span></div>
  <h3>📈 Summary</h3>
  {_metric_table(summary)}
  <h3>📅 逐日明细（末行高亮 = 当前信号）</h3>
  <div class="scroll">
    <table><thead><tr>
      <th>日期</th><th>方向</th><th>收益</th><th>入场价</th><th>出场价</th><th>出场日</th>
    </tr></thead><tbody>
      {''.join(rows_html)}
    </tbody></table>
  </div>
  <div class="sub" style="margin-top:8px">口径：整个基准图区间；🔴涨 🟢跌 ⚪平；涨跌停日不可成交收益置 0（与回测报告同口径）；末尾持仓未到期者标 [N/A] 不计入胜率。</div>
</div>
""".strip()

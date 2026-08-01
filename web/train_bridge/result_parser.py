# -*- coding: utf-8 -*-
"""训练产物解析 → 本地 SQLite LocalFormula + 结果分类。

解析 docs/02 §6 字段映射的 reports/ 文件：
  {code}_train_metrics_{ts}.txt   → BestScore/Tokens/Formula
  {code}_oos_metrics_{ts}.txt     → OOS 夏普/年化/回撤/Calmar
  {code}_OOS_{ts}.md             → 预测成功率(胜率)
  strategy_performance_{code}_*   → PNG 基准图

test_sharpe 优先取 OOS 夏普，回退训练期 BestScore。
分类阈值（docs/06 §5）：≤0.4 劣质 / <1.5 普通 / ≥1.5 优质。

注意：persist() 必须在 Flask app_context 内调用（写 DB）。
"""
import glob
import os
import re
from datetime import datetime


def _latest(patterns, reports_dir):
    """返回匹配 patterns 的最新文件路径（按 mtime），无则 None。"""
    files = []
    for pat in patterns:
        files.extend(glob.glob(os.path.join(reports_dir, pat)))
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def _relocate_to_user(path, email):
    """把引擎写到全局 reports/ 的产物迁入当前用户目录 reports/<h>/（账号隔离）。

    引擎 core_engine 保持 app 无关、写裸 'reports'（CWD 相对）；web 层在落库时把该次训练
    的产物搬到属主目录，使 /reports-file 只按属主目录服务 → 跨账号不可见。返回新路径或原路径。
    """
    if not path or not os.path.isfile(path):
        return path
    try:
        from web.services import storage
        import shutil as _sh
        dst_dir = storage.user_reports_dir(email)
        dst = os.path.join(str(dst_dir), os.path.basename(path))
        if os.path.abspath(path) != os.path.abspath(dst):
            _sh.move(path, dst)
        return dst
    except Exception:
        return path


def _relocate_code_glob(reports_dir, code, email):
    """兜底迁移：把全局 reports/ 中该 code 的全部产物（{code}* 与 strategy_performance_{code}_*）
    迁入属主目录，杜绝含公式/指标的明文残片留在全局目录。code 为空或失败静默。"""
    if not code:
        return
    try:
        from web.services import storage
        import shutil as _sh
        dst_dir = storage.user_reports_dir(email)
        for pat in (f'{code}*', f'strategy_performance_{code}_*'):
            for path in glob.glob(os.path.join(reports_dir, pat)):
                if not os.path.isfile(path):
                    continue
                dst = os.path.join(str(dst_dir), os.path.basename(path))
                if os.path.abspath(path) != os.path.abspath(dst):
                    try:
                        _sh.move(path, dst)
                    except Exception:
                        pass
    except Exception:
        pass


def _parse_train_metrics(path):
    """解析 {code}_train_metrics_*.txt。"""
    out = {'best_score': None, 'tokens': None, 'formula_str': ''}
    if not path or not os.path.exists(path):
        return out
    try:
        txt = open(path, encoding='utf-8').read()
        m = re.search(r'BestScore:\s*([-\d.]+)', txt)
        if m:
            out['best_score'] = float(m.group(1))
        m = re.search(r'Tokens:\s*\[([^\]]*)\]', txt)
        if m:
            out['tokens'] = [int(x) for x in re.findall(r'-?\d+', m.group(1))]
        m = re.search(r'Formula:\s*(.+)', txt)
        if m:
            out['formula_str'] = m.group(1).strip()
    except Exception:
        pass
    return out


def _pct_to_float(s):
    """'18.50%' -> 0.185；'1.234' -> 1.234。先判 % 再替换（修复原 bug）。"""
    if s is None:
        return None
    had_pct = '%' in s
    s = s.strip().replace('%', '').strip()
    try:
        v = float(s)
    except ValueError:
        return None
    return v / 100.0 if had_pct else v


def _parse_oos_metrics(path):
    """解析 {code}_oos_metrics_*.txt → OOS 指标。"""
    out = {'sharpe': None, 'ann_ret': None, 'vol': None, 'max_dd': None, 'calmar': None}
    if not path or not os.path.exists(path):
        return out
    try:
        txt = open(path, encoding='utf-8').read()
        mapping = {
            'sharpe': r'夏普指数\s*:\s*([-\d.%]+)',
            'ann_ret': r'年回报率\s*:\s*([-\d.%]+)',
            'vol': r'年波动率\s*:\s*([-\d.%]+)',
            'max_dd': r'最大回撤\s*:\s*([-\d.%]+)',
            'calmar': r'回撤调整收益率\s*:\s*([-\d.%]+)',
        }
        for k, pat in mapping.items():
            m = re.search(pat, txt)
            if m:
                out[k] = _pct_to_float(m.group(1))
    except Exception:
        pass
    return out


def _parse_win_rate(md_path):
    """从 {code}_OOS_*.md 解析样本外胜率（兼容旧"预测成功率"标签）。"""
    if not md_path or not os.path.exists(md_path):
        return None
    try:
        txt = open(md_path, encoding='utf-8').read()
        m = re.search(r'(?:预测成功率|样本外胜率)\s*\|\s*([-\d.%N/A]+)', txt)
        if m:
            return _pct_to_float(m.group(1))
    except Exception:
        pass
    return None


def _parse_factors(oos_path):
    """从 oos_metrics 文件解析 Factors 行。"""
    if not oos_path or not os.path.exists(oos_path):
        return ''
    try:
        txt = open(oos_path, encoding='utf-8').read()
        m = re.search(r'Factors:\s*([^\n\r]*)', txt)
        return m.group(1).strip() if m else ''
    except Exception:
        return ''


# ── AI 取名（规则引擎，明文；deepseek 版 P05 实装）──
_FACTOR_THEME = {
    'WR_DIFF': '威廉动量', 'RSI14': '相对强弱', 'RSV': '随机突破', 'MFI': '资金流量',
    'RET': '日动量', 'RET5': '周动量', 'RET20': '月动量', 'ROC': '变动率',
    'VOL_CHG': '量能异动', 'V_RET': '量价共振', 'VWAP_DEV': '均价偏离',
    'VOL_RET_CORR': '量价相关', 'TREND': '趋势偏离', 'ATR14': '波幅',
    'VOL20': '波动率', 'BB_WIDTH': '布林带宽', 'F_BUY_F_REPLAY': '融资动量',
    'OBV_CHG': 'OBV能量', 'BIAS': '乖离',
}
_OP_THEME = {
    'DECAY': '衰减增强', 'GATE': '条件过滤', 'JUMP': '尖峰捕捉', 'MAX3': '三日极值',
    'DELAY1': '滞后确认', 'ABS': '绝对', 'SIGN': '方向', 'NEG': '反向',
}


def guess_name(formula_str, factors):
    """规则引擎为公式起一个能体现自然意思的名字。"""
    if not formula_str:
        return '未命名公式'
    hits = [name for f, name in _FACTOR_THEME.items() if f in formula_str]
    ops = [name for op, name in _OP_THEME.items() if op in formula_str]
    theme = hits[0] if hits else '复合'
    suffix = ops[0] if ops else ''
    # 夏普类语义提示（粗略）
    if 'ADD' in formula_str or 'MUL' in formula_str:
        suffix = suffix or '增强'
    name = (theme + suffix).strip()
    return name or '复合因子公式'


# ── 分类 ──
def classify(test_sharpe):
    if test_sharpe is None:
        return {'tier': 'unknown', 'label': '未知', 'color': 'neutral',
                'msg': '未取到 test_sharpe，请查看日志'}
    if test_sharpe <= 0.4:
        return {'tier': 'bad', 'label': '劣质', 'color': 'down',
                'msg': '夏普 ≤0.4，建议放弃保存，重新训练'}
    if test_sharpe < 1.5:
        return {'tier': 'normal', 'label': '普通', 'color': 'neutral',
                'msg': '0.4<夏普<1.5，根据需求选择性保存'}
    return {'tier': 'premium', 'label': '优质', 'color': 'up',
            'msg': '夏普 ≥1.5，优质公式，建议保存'}


def persist(params: dict, seed: int, strategy: str, duration: float,
            reports_dir: str, stock_name: str = '') -> dict:
    """扫描 reports/ 最新产物，写 LocalFormula(saved=False)，返回结果摘要。

    必须在 app_context 内调用。返回 dict 含 formula_id / tier / 各指标，供 emit。
    """
    from web.extensions import db
    from db.models import LocalFormula
    from web.auth import active_email

    email = active_email()
    code = str(params.get('index_code', ''))
    tm = _latest([f'{code}*train_metrics*.txt'], reports_dir)
    om = _latest([f'{code}*oos_metrics*.txt'], reports_dir)
    md = _latest([f'{code}_OOS_*.md'], reports_dir)
    png = _latest([f'strategy_performance_{code}_*.png',
                   f'{code}*strategy*.png'], reports_dir)

    # 引擎产物从全局 reports/ 迁入当前用户目录（账号隔离；/reports-file 按属主目录服务）
    tm = _relocate_to_user(tm, email)
    om = _relocate_to_user(om, email)
    md = _relocate_to_user(md, email)
    png = _relocate_to_user(png, email)
    # 兜底：把该 code 在全局 reports/ 的其余产物（如主训练报告 {code}_*.md）一并迁走，
    # 不留含公式/指标的明文残片在全局目录供跨账号浏览
    _relocate_code_glob(reports_dir, code, email)

    # 股票名称（stock_dict 内置 + akshare 缓存）
    try:
        from web.stock_dict import lookup_name
        stock_name = lookup_name(code)
    except Exception:
        stock_name = ''

    tm_data = _parse_train_metrics(tm)
    oos = _parse_oos_metrics(om)
    win_rate = _parse_win_rate(md)
    factors = _parse_factors(om)

    # test_sharpe：OOS 优先，回退训练期
    test_sharpe = oos['sharpe']
    if test_sharpe is None:
        test_sharpe = tm_data['best_score']

    formula_str = tm_data['formula_str']
    ai_name = guess_name(formula_str, factors)

    f = LocalFormula(
        owner_email=email,
        stock_code=code,
        stock_name=stock_name or '',
        formula_str=formula_str,
        tokens=tm_data['tokens'],
        factors=factors,
        ai_name=ai_name,
        test_sharpe=test_sharpe or 0.0,
        ann_ret=oos.get('ann_ret'),
        max_dd=oos.get('max_dd'),
        win_rate=win_rate,
        calmar=oos.get('calmar'),
        source='self',
        origin_id='',
        png_path=os.path.basename(png) if png else '',
        oos_md_path=os.path.basename(md) if md else '',
        train_metrics_path=os.path.basename(tm) if tm else '',
        seed=seed,
        seed_strategy=strategy,
        train_params=dict(params),
        train_duration_sec=duration,
        trained_at=datetime.utcnow(),
        in_quant=False,
        saved=False,   # 待用户点「保存到公式库」确认
    )
    db.session.add(f)
    db.session.commit()

    tier = classify(test_sharpe)
    return {
        'formula_id': f.id,
        'stock_code': code,
        'formula_str': formula_str,
        'ai_name': ai_name,
        'test_sharpe': round(test_sharpe, 4) if test_sharpe is not None else None,
        'tier': tier['tier'],
        'label': tier['label'],
        'color': tier['color'],
        'msg': tier['msg'],
        'ann_ret': oos.get('ann_ret'),
        'max_dd': oos.get('max_dd'),
        'win_rate': win_rate,
        'calmar': oos.get('calmar'),
        'png_url': ('/reports-file?path=' + os.path.basename(png)) if png else '',
    }

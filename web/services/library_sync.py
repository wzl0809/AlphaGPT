# -*- coding: utf-8 -*-
"""公式库与服务端的「库位」同步：占位/对账。

设计见 docs/14 §公式库额度 与 [[formula-limit-protection-recon]]：
- 在线保存/获取公式 → stamp_server_claim 向服务端占一个库位（硬校验，超额 402）；
- 离线时占位失败 → 公式留 server_claimed=False（pending），本地按签名 cap 计数，
  超 cap 的由 quota.formula_locked_ids 动态锁定；
- 重连（通知轮询成功）→ reconcile_with_server 全量上报 saved 列表：
    服务端补占 pending + 清孤儿（离线删的）+ 超额返 rejected（本地动态锁）。
"""
from web.extensions import db
from db.models import LocalFormula
import api_client.endpoints as ep


def stamp_server_claim(formula) -> bool:
    """新建/获取公式后向服务端占位。返回 True=已确认；False=离线/超额/失败（pending）。

    幂等：服务端 (user, local_ref) 唯一约束 → 重复占位不重复计数。
    不阻断本地保存：失败仅置 server_claimed=False，交由 reconcile 对账。
    """
    try:
        ok, data = ep.claim_formula_slot(str(formula.id), formula.source or 'self')
    except Exception:
        ok = False
    formula.server_claimed = bool(ok)
    try:
        db.session.commit()
    except Exception:
        pass
    return bool(ok)


def reconcile_with_server():
    """重连对账：把本地全部 saved 公式（按 sharpe desc）上报服务端做全量同步。

    由通知轮询成功后触发（在线）。服务端：补占 pending + 清孤儿 + 超额返 rejected。
    rejected 的公式本地保持 saved=True，但会被 quota.formula_locked_ids 按 (count > cap) 动态锁定
    （上报顺序为 sharpe desc，与服务端锁逻辑同源 → 最低 sharpe 的自然进锁）。
    claimed 的公式标记 server_claimed=True。
    """
    rows = (LocalFormula.owned().filter_by(saved=True)
            .order_by(LocalFormula.test_sharpe.desc(),
                      LocalFormula.created_at.desc(),
                      LocalFormula.id.asc())
            .with_entities(LocalFormula.id, LocalFormula.source).all())
    if not rows:
        return
    claims = [{'local_ref': str(r.id), 'source': r.source or 'self'} for r in rows]
    try:
        ok, data = ep.reconcile_library(claims)
    except Exception:
        return
    if not ok or not isinstance(data, dict):
        return
    claimed = {str(x) for x in (data.get('claimed') or [])}
    changed = False
    for r in rows:
        if str(r.id) in claimed:
            lf = db.session.get(LocalFormula, r.id)
            if lf and not lf.server_claimed:
                lf.server_claimed = True
                changed = True
    if changed:
        try:
            db.session.commit()
        except Exception:
            pass

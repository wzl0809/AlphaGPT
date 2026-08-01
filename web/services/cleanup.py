# -*- coding: utf-8 -*-
"""reports / checkpoints 文件清理服务（账号隔离，[[client-per-user-data-partition]]）。

reports：扫描当前用户目录 reports/<h>/，写 ReportsIndex（按 filepath 去重，带 owner_email）。
checkpoints：引擎写全局 checkpoints/（transient），扫描并按当前用户打属主标签（app 级隔离）。
删除仅删文件 + 索引行；公式库引用的 png 删除后详情页回退空状态（不崩）。
"""
import glob
import os
from datetime import datetime
from pathlib import Path

from web.auth import active_email
from . import storage

_CLIENT_ROOT = Path(__file__).resolve().parent.parent.parent
CHECKPOINTS_DIR = _CLIENT_ROOT / 'checkpoints'   # 引擎全局写盘点（transient）


def _reports_dir() -> Path:
    """当前用户的报告目录（清理页只看自己的）。"""
    return storage.user_reports_dir(active_email())


def _ext_kind(name: str) -> str:
    e = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    return e if e in ('txt', 'md', 'png', 'html') else 'other'


def sync_reports_index():
    """扫描当前用户 reports/<h>/，upsert ReportsIndex（按 filepath 去重，带属主）。"""
    from web.extensions import db
    from db.models import ReportsIndex
    owner = active_email()
    reports_dir = _reports_dir()
    reports_dir.mkdir(parents=True, exist_ok=True)
    existing = {r.filepath: r for r in ReportsIndex.owned().all()}
    for path in glob.glob(str(reports_dir / '*')):
        if not os.path.isfile(path):
            continue
        name = os.path.basename(path)
        if path in existing:
            r = existing[path]
            r.size_bytes = os.path.getsize(path)
            continue
        # 从文件名提取股票代码（如 600519_xxx 或 strategy_performance_600519_xxx）
        code = _guess_code(name)
        db.session.add(ReportsIndex(
            owner_email=owner, filepath=path, kind=_ext_kind(name), stock_code=code,
            size_bytes=os.path.getsize(path),
            created_at=datetime.fromtimestamp(os.path.getmtime(path))))
    # 移除已不存在的记录
    for fp, r in existing.items():
        if not os.path.isfile(fp):
            db.session.delete(r)
    db.session.commit()


def sync_checkpoints_index():
    """扫描全局 checkpoints/*.ckpt.pt，upsert CheckpointsIndex（按当前用户打属主）。

    引擎写全局 checkpoints/（transient，训练成功/停止时自清），这里只做 app 级归属标签，
    供清理页按属主过滤；跨账号残留（中断未清）会被首个扫描者接管，仅清理性影响。
    """
    from web.extensions import db
    from db.models import CheckpointsIndex
    owner = active_email()
    CHECKPOINTS_DIR.mkdir(parents=True, exist_ok=True)
    existing = {r.filepath: r for r in CheckpointsIndex.owned().all()}
    for path in glob.glob(str(CHECKPOINTS_DIR / '*ckpt.pt')):
        if not os.path.isfile(path):
            continue
        name = os.path.basename(path)
        code = _guess_code(name)
        run_id = _guess_run(name)
        if path in existing:
            continue
        db.session.add(CheckpointsIndex(
            owner_email=owner, filepath=path, stock_code=code, run_id=run_id,
            created_at=datetime.fromtimestamp(os.path.getmtime(path))))
    for fp, r in existing.items():
        if not os.path.isfile(fp):
            db.session.delete(r)
    db.session.commit()


def delete_files(filepaths):
    """删除文件 + 对应索引行（仅当前用户的）。返回删除数。"""
    from web.extensions import db
    from db.models import ReportsIndex, CheckpointsIndex
    reports_root = str(_reports_dir())
    ckpt_root = str(CHECKPOINTS_DIR)
    n = 0
    for fp in filepaths:
        # 防穿越：必须在当前用户 reports/ 或全局 checkpoints/ 下
        fp = os.path.abspath(fp)
        if not (fp.startswith(reports_root) or fp.startswith(ckpt_root)):
            continue
        try:
            if os.path.isfile(fp):
                os.remove(fp)
                n += 1
        except OSError:
            pass
        # 删索引（仅当前属主）
        for M in (ReportsIndex, CheckpointsIndex):
            for r in M.owned().filter_by(filepath=fp).all():
                db.session.delete(r)
    db.session.commit()
    return n


def _guess_code(name: str) -> str:
    import re
    m = re.search(r'(\d{6})', name)
    return m.group(1) if m else ''


def _guess_run(name: str) -> int:
    import re
    m = re.search(r'run(\d+)', name)
    return int(m.group(1)) if m else 0

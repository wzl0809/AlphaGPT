# -*- coding: utf-8 -*-
"""训练性能/环境参数本地存储（明文 JSON，非敏感）。

margin_workers（数据源并发）与 cpu_threads（CPU 核心数）原在训练配置页的"数据/早停"卡，
但它们是客户端环境调优（网络/硬件），与策略无关、配一次全局生效——故挪到系统设置。
启动时 apply_to_app() 写入 app.config；runner 注入训练环境变量
（AlphaGPT 读 MARGIN_WORKERS / CPU_THREADS，引擎零改动）。
文件：client/db/perf.json
"""
import json
from pathlib import Path

_STORE = Path(__file__).resolve().parent.parent.parent / 'db' / 'perf.json'

DEFAULTS = {
    'margin_workers': 4,   # 两融数据获取并发线程数；被限速时改 1~2
    'cpu_threads': 0,       # 0=自动；建议设为 CPU 物理核心数
}

# app.config 键名（runner 从 app.config 读取注入训练 env）
CFG_KEYS = {
    'margin_workers': 'MARGIN_WORKERS',
    'cpu_threads': 'CPU_THREADS',
}

RANGES = {
    'margin_workers': (1, 16),
    'cpu_threads': (0, 64),
}


def load() -> dict:
    """读取（合并默认值，保证两键都在）。"""
    vals = dict(DEFAULTS)
    if _STORE.exists():
        try:
            data = json.loads(_STORE.read_text(encoding='utf-8'))
            for k in DEFAULTS:
                if k in data:
                    vals[k] = int(data[k])
        except Exception:
            pass
    return vals


def save(values: dict) -> dict:
    """保存（仅这两键，clamp 到合法范围，明文）。返回保存后的值。"""
    vals = load()
    for k in DEFAULTS:
        if k in values and values[k] is not None:
            try:
                lo, hi = RANGES[k]
                vals[k] = max(lo, min(hi, int(values[k])))
            except (TypeError, ValueError):
                continue
    _STORE.parent.mkdir(parents=True, exist_ok=True)
    _STORE.write_text(json.dumps(vals, ensure_ascii=False), encoding='utf-8')
    return vals


def apply_to_app(app):
    """启动时把值写入 app.config（runner 从这里读注入训练 env）。"""
    vals = load()
    for k, cfg_key in CFG_KEYS.items():
        app.config[cfg_key] = vals[k]

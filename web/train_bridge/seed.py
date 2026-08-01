# -*- coding: utf-8 -*-
"""种子策略 A–F（docs/04 §3.3 / docs/02 训练系统）。

  A 混沌探索（默认）：hash(uid + code + timestamp)
  B 网格探索：base + step * i
  C 黄金分割：斐波那契序列映射
  D 极致复现：用户手输
  E 区间扫描：本地递增，确保不重复
  F AI 探索：deepseek 先验知识（无 key 回退 A）
"""
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

# 区间扫描的持久化文件（记录每个 uid+code 上次分配的 seed）
_SEED_FILE = Path(__file__).resolve().parent.parent.parent / 'db' / 'last_seeds.json'


def _fib(n: int) -> int:
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a


def _read_last_seeds() -> dict:
    if _SEED_FILE.exists():
        try:
            return json.loads(_SEED_FILE.read_text(encoding='utf-8'))
        except Exception:
            return {}
    return {}


def _write_last_seeds(data: dict):
    try:
        _SEED_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SEED_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    except Exception:
        pass


def _deepseek_available() -> bool:
    """是否配置了 deepseek key（系统设置页 P06 写本地，此处先查环境/config）。"""
    try:
        from flask import current_app
        return bool(current_app.config.get('DEEPSEEK_API_KEY'))
    except Exception:
        return bool(os.getenv('DEEPSEEK_API_KEY'))


def _ai_seed_via_deepseek(uid: str, code: str) -> int:
    """用 LLM 先验知识生成种子(走 web.ai.deepseek.chat 统一调用路径,Phase 0 迁移自旧版直调)。"""
    try:
        from web.ai import deepseek
        if not deepseek.has_key():
            return _chaos(uid, code)
        text = deepseek.chat(
            f'为A股{code}生成一个1到9999999之间的幸运整数种子,只返回数字。', max_tokens=16)
        if not text:
            return _chaos(uid, code)
        import re
        m = re.search(r'\d+', text)
        return max(1, min(int(m.group()), 9999999)) if m else _chaos(uid, code)
    except Exception:
        return _chaos(uid, code)


def _chaos(uid: str, code: str) -> int:
    raw = f'{uid}{code}{datetime.now().strftime("%Y%m%d%H%M%S%f")}'
    h = int(hashlib.md5(raw.encode('utf-8')).hexdigest(), 16)
    return h % 9999900 + 10000


def _grid(params: dict) -> int:
    base = int(params.get('grid_base', 10000))
    step = int(params.get('grid_step', 137))
    i = int(params.get('run_idx', 0))
    return abs(base + step * i) % 9999900 + 10000


def _golden(uid: str, code: str, run_idx: int = 0) -> int:
    """斐波那契序列映射到种子区间。"""
    # 用 uid/code 哈希作偏移，fib 作主部，乘黄金比例 0.618 放大
    offset = int(hashlib.md5(f'{uid}{code}'.encode()).hexdigest()[:8], 16) % 100000
    f = _fib(20 + (run_idx % 30))   # fib(20..49)
    return int((f * 0.6180339 + offset)) % 9999900 + 10000


def _manual(params: dict) -> int:
    return max(1, int(params.get('manual_seed', 42)))


def _scanning(uid: str, code: str) -> int:
    """区间扫描：本地记录上次值，本次 +1 起步，确保不重复。"""
    data = _read_last_seeds()
    key = f'{uid}:{code}'
    last = data.get(key, 9999)            # 从 10000 起
    nxt = last + 1
    if nxt > 9999900:
        nxt = 10000
    data[key] = nxt
    _write_last_seeds(data)
    return nxt


STRATEGY_LABELS = {
    'A': '混沌探索（hash 用户×股票×时间）',
    'B': '网格探索（base + step×i）',
    'C': '黄金分割（斐波那契序列）',
    'D': '极致复现（手动种子）',
    'E': '区间扫描（系统递增不重复）',
    'F': 'AI 探索（deepseek 先验知识）',
}


def seed_strategy(params: dict) -> tuple:
    """根据 params['seed_mode'] 返回 (seed, strategy_code, fallback_note)。

    fallback_note：F 无 deepseek 回退 A 时返回提示，否则 ''。
    """
    mode = (params.get('seed_mode') or 'A').upper()
    uid = str(params.get('user_id', 0))
    code = str(params.get('index_code', '000000'))
    run_idx = int(params.get('run_idx', 0))
    note = ''

    if mode == 'A':
        seed = _chaos(uid, code)
    elif mode == 'B':
        seed = _grid(params)
    elif mode == 'C':
        seed = _golden(uid, code, run_idx)
    elif mode == 'D':
        seed = _manual(params)
    elif mode == 'E':
        seed = _scanning(uid, code)
    elif mode == 'F':
        if not _deepseek_available():
            seed = _chaos(uid, code)
            note = '未配置 deepseek key，AI 探索回退为混沌探索(A)'
            mode = 'A'
        else:
            seed = _ai_seed_via_deepseek(uid, code)
    else:
        seed = _chaos(uid, code)
        mode = 'A'

    return seed, mode, note

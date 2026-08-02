# -*- coding: utf-8 -*-
"""客户端版本号：单一真相源（docs/14 §1）。

优先读 build 烘焙的 version.txt（JSON），回退 .env APP_VERSION，再回退 'unknown'。
绝不伪造数字版本（如 '0.0.0-dev'）—— 'unknown' 让服务端走恩期，避免误锁（C4）。
parse_version 与服务端 apps/releases/services.py 保持完全一致（C5）。
"""
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_VERSION_TXT = Path(__file__).resolve().parent.parent / 'version.txt'   # client/version.txt


def _read_version_meta():
    """读 version.txt（build 烘焙）。UTF-8（容忍 BOM）。失败→None，绝不抛。"""
    try:
        if _VERSION_TXT.exists():
            raw = _VERSION_TXT.read_text(encoding='utf-8-sig')
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
    except Exception as e:
        logger.warning('version.txt 读取失败（将回退 APP_VERSION / unknown）: %s', e)
    return None


_meta = _read_version_meta()
CLIENT_VERSION = (
    (_meta or {}).get('version')
    or os.getenv('APP_VERSION')
    or 'unknown'
)
CLIENT_CHANNEL = (_meta or {}).get('channel') or os.getenv('CLIENT_CHANNEL') or 'preview'
CLIENT_BUILD_ID = (_meta or {}).get('build_id') or ''


def parse_version(s):
    """'0.88' / '0.88.0' / 'V0.88' → (0,88,0)。不可解析抛 ValueError。与服务端一致。"""
    s = (s or '').strip()
    if s[:1] in ('v', 'V'):
        s = s[1:]
    parts = s.split('.')
    nums = []
    for seg in parts[:3]:
        digits = ''
        for ch in seg:
            if ch.isdigit():
                digits += ch
            else:
                break
        if digits == '':
            raise ValueError(f'不可解析的版本段: {seg!r} (in {s!r})')
        nums.append(int(digits))
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums[:3])


def is_unknown():
    return (not CLIENT_VERSION) or CLIENT_VERSION.lower() == 'unknown'


def get_version_header():
    """供 X-Client-Version 头使用（unknown 时原样发 'unknown'，服务端走恩期）。"""
    return CLIENT_VERSION if CLIENT_VERSION else 'unknown'

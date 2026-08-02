# -*- coding: utf-8 -*-
"""硬件信息服务（封装 hw_monitor）。"""
import json
import time
from pathlib import Path

import hw_monitor

# 持久缓存：静态硬件信息（CPU/GPU/内存/OS）极少变化，缓存到文件 7 天，避免每次开设置页都
# PowerShell/subprocess 采集卡顿。force=True（「刷新硬件」按钮）无视缓存重采集。
_HW_CACHE_FILE = Path(__file__).resolve().parent.parent.parent / 'db' / 'hw_cache.json'
_HW_CACHE_TTL = 7 * 24 * 3600   # 7 天


def collect_static(force: bool = False):
    """静态硬件信息（设置页）。优先读持久缓存（7 天 TTL）；force=True 无视缓存重采集。

    hw_monitor 不可用时回退 {'error': ...}。
    """
    if not force:
        try:
            if _HW_CACHE_FILE.exists():
                data = json.loads(_HW_CACHE_FILE.read_text(encoding='utf-8'))
                if time.time() - data.get('ts', 0) < _HW_CACHE_TTL:
                    return data.get('hw') or {}
        except Exception:
            pass
    try:
        hw = hw_monitor.HWMonitor.static_info()
    except Exception as e:
        return {'error': str(e)}
    try:
        _HW_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _HW_CACHE_FILE.write_text(json.dumps({'ts': time.time(), 'hw': hw}, ensure_ascii=False),
                                  encoding='utf-8')
    except Exception:
        pass
    return hw


def collect_summary():
    """精简摘要（训练记录 hardware_summary 用）。"""
    try:
        return hw_monitor.HWMonitor.summary()
    except Exception:
        return {}

# -*- coding: utf-8 -*-
"""硬件监控（跨平台、多厂商显卡兼容）。

采集策略按「平台 + 显卡厂商」选最优路径，互为兜底：
- CPU 型号/核心/内存/OS：platform + psutil（进程内，安全）。
- CPU 负载/内存占用：psutil（collect 用 interval=None，须在 app 启动期 seed 基线，否则首次返回 0.0）。
- CPU 温度：Linux 用 psutil.sensors_temperatures；Windows 用 WMI MSAcpi_ThermalZoneTemperature（subprocess+超时）。
- GPU（按优先级）：
  1) NVIDIA：pynvml（负载/温度/显存，最详细）。
  2) Windows 任意显卡（AMD/Intel/NVIDIA/集显）：GPU 性能计数器 Get-Counter（任务管理器同源），
     自动选最活跃的 phys（双卡选在干活的那张），集显共享内存如实标 N/A。
  3) Linux AMD：sysfs（/sys/class/drm/card*/device：gpu_busy_percent / mem_info_vram_* / hwmon 温度）。
  4) 兜底：仅静态型号。

⚠️ 不在进程内用 WMI/COM（本机会假死）。Windows 外部查询一律走 subprocess + timeout，可被 kill。
"""
import glob
import json
import os
import platform
import subprocess
import threading

_CACHE = {'gpu_static': None}
_lock = threading.Lock()
_IS_WINDOWS = platform.system() == 'Windows'
_IS_LINUX = platform.system() == 'Linux'

# 集成显卡名特征（共享系统内存，无独立显存）
_INTEGRATED_MARKERS = ('intel', 'uhd', 'iris', 'arc graphics', 'radeon(tm) graphics',
                      'radeon graphics', 'vega', 'integrated', 'graphics 5', 'graphics 6',
                      'graphics 7', 'adl', 'graphics 4600', 'graphics 5500')


def _run_ps(script: str, timeout: int = 5):
    """安全跑 PowerShell（subprocess + 超时；绝不 in-process COM）。失败返回 ''。

    creationflags=CREATE_NO_WINDOW：pythonw（无控制台）发布形态下，子进程默认会被
    分配新控制台 → 每 3s 弹一个 PowerShell 黑窗（训练页轮询 hw_stats 时尤为密集）。
    CREATE_NO_WINDOW 根治弹窗；常量仅 Windows 存在，故用 _IS_WINDOWS 守卫。
    """
    try:
        kwargs = dict(capture_output=True, text=True, timeout=timeout)
        if _IS_WINDOWS:
            kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
        r = subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
            **kwargs)
        return (r.stdout or '').strip()
    except Exception:
        return ''


def _gpu_vendor(name: str) -> str:
    n = (name or '').lower()
    if any(k in n for k in ('nvidia', 'geforce', 'rtx', 'gtx', 'quadro', 'tesla')):
        return 'nvidia'
    if any(k in n for k in ('amd', 'radeon', 'firepro', 'instinct')):
        return 'amd'
    if any(k in n for k in ('intel', 'arc', 'iris', 'uhd')):
        return 'intel'
    return 'unknown'


def _is_integrated(name: str) -> bool:
    n = (name or '').lower()
    return any(k in n for k in _INTEGRATED_MARKERS)


# ── NVIDIA（pynvml）──
def _nvml_handles():
    try:
        import pynvml
        try:
            pynvml.nvmlInit()
        except Exception:
            return None
        n = pynvml.nvmlDeviceGetCount()
        return [pynvml.nvmlDeviceGetHandleByIndex(i) for i in range(n)]
    except Exception:
        return None


def _gpu_live_nvml():
    """NVIDIA 实时（pynvml）。无 pynvml/失败返回 []。"""
    nv = _nvml_handles()
    if nv is None:
        return []
    out = []
    try:
        import pynvml
        for h in nv:
            name = pynvml.nvmlDeviceGetName(h)
            name = name.decode() if isinstance(name, bytes) else str(name)
            util = pynvml.nvmlDeviceGetUtilizationRates(h)
            mem = pynvml.nvmlDeviceGetMemoryInfo(h)
            try:
                temp = pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU)
            except Exception:
                temp = None
            total_mb = mem.total / 1024 / 1024
            used_mb = mem.used / 1024 / 1024
            out.append({
                'name': name, 'vendor': 'nvidia', 'integrated': False,
                'load_pct': float(util.gpu),
                'temp_c': float(temp) if temp is not None else None,
                'mem_used_mb': used_mb, 'mem_total_mb': total_mb,
                'mem_percent': round(used_mb / total_mb * 100, 1) if total_mb else None,
            })
    except Exception:
        pass
    return out


# ── Windows（GPU 性能计数器，兼容任意显卡）──
def _win_static():
    """Windows 静态 GPU 列表（Win32_VideoController，过滤虚拟/远程，标注集显）。"""
    out = _run_ps(
        "Get-CimInstance Win32_VideoController -EA SilentlyContinue | "
        "Where-Object { $_.Name -and $_.Name -notmatch 'Virtual|Oray|Remote|DisplayLink|Basic|Microsoft|SPSS' } | "
        "ForEach-Object { [pscustomobject]@{ name=$_.Name; ram=$_.AdapterRAM } } | ConvertTo-Json -Compress")
    if not out:
        return []
    try:
        data = json.loads(out)
        if isinstance(data, dict):
            data = [data]
        gpus = []
        for g in data:
            nm = g.get('name', '')
            if not nm:
                continue
            gpus.append({'name': nm, 'vendor': _gpu_vendor(nm), 'integrated': _is_integrated(nm),
                         'vram_mb': int(g.get('ram') or 0) // (1024 * 1024)})
        return gpus
    except Exception:
        return []


def _win_live():
    """Windows 一次 subprocess：最活跃 GPU 的负载/显存占用。

    多卡时按 Dedicated Usage 选占用最大的 phys（= 在干活的那张，通常是独显）。
    （原 CPU 热区温度查询已移除：ACPI MSAcpi_ThermalZoneTemperature 在桌面机返回静态阈值，不可靠。）
    """
    out = _run_ps(
        "$o=@{load=$null; mem_used=$null};"
        "try{"
        "  $top=(Get-Counter '\\GPU Adapter Memory(*)\\Dedicated Usage' -EA SilentlyContinue).CounterSamples"
        "    | Where-Object{ $_.InstanceName -like '*phys_*' } | Sort-Object CookedValue -Descending | Select-Object -First 1;"
        "  if($top){ $o.mem_used=$top.CookedValue; $phys=($top.InstanceName -replace '.*phys_(\\d+).*','$1');"
        "    $sum=((Get-Counter '\\GPU Engine(*)\\Utilization Percentage' -EA SilentlyContinue).CounterSamples"
        "      | Where-Object{ $_.InstanceName -like \"*phys_$phys*\" -and $_.CookedValue -gt 0 } | Measure-Object CookedValue -Sum).Sum;"
        "    if($sum){ $o.load=[math]::Round([math]::Min(100.0,$sum),1) } }"
        "}catch{};"
        "$o | ConvertTo-Json -Compress")
    if not out:
        return {}
    try:
        d = json.loads(out)
        mem = d.get('mem_used')
        return {'gpu_load_pct': d.get('load'),
                'gpu_mem_used_mb': round(mem / (1024 * 1024), 1) if mem else None}
    except Exception:
        return {}


def _win_gpu_live():
    """Windows GPU 实时：静态型号 + 性能计数器负载/显存。选独显（非集显优先）。"""
    static = _gpu_static_once()
    if not static:
        return []
    # 优先选独显（非集显）；全为集显则取显存最大者
    discrete = [g for g in static if not g.get('integrated')] or static
    g0 = max(discrete, key=lambda g: g.get('vram_mb') or 0)
    w = _win_live()
    used = w.get('gpu_mem_used_mb')
    total = g0.get('vram_mb') or None
    integrated = g0.get('integrated')
    return [{
        'name': g0.get('name'), 'vendor': g0.get('vendor', 'unknown'),
        'integrated': bool(integrated),
        'load_pct': w.get('gpu_load_pct'),
        'temp_c': None,   # 非 NVIDIA 在 Windows 无 GPU 温度源（pynvml 才有）
        'mem_used_mb': used,
        'mem_total_mb': total,
        # 集显共享内存：显存百分比无意义 → None（前端显示 N/A）
        'mem_percent': None if integrated else (round(used / total * 100, 1) if (used and total) else None),
    }]


# ── Linux AMD（sysfs）──
def _linux_amd_gpu():
    """Linux AMD 实时（sysfs gpu_busy_percent / VRAM / hwmon 温度）。非 AMD/失败返回 []。"""
    out = []
    for card in glob.glob('/sys/class/drm/card*/device/gpu_busy_percent'):
        try:
            base = os.path.dirname(card)
            with open(card) as f:
                load = float(f.read().strip())
            vram_total = vram_used = None
            for fn, key in (('mem_info_vram_total', 'total'), ('mem_info_vram_used', 'used')):
                p = os.path.join(base, fn)
                if os.path.exists(p):
                    with open(p) as f:
                        val = int(f.read().strip())
                    if key == 'total':
                        vram_total = val // (1024 * 1024)
                    else:
                        vram_used = val // (1024 * 1024)
            # 温度：hwmon
            temp = None
            for tf in glob.glob(base + '/hwmon/*/temp1_input'):
                try:
                    with open(tf) as f:
                        temp = round(int(f.read().strip()) / 1000.0, 1)
                    break
                except Exception:
                    pass
            name = ''
            np = os.path.join(base, 'uevent')
            # 取 DRM 设备名（如 radeon/amdgpu + PCI id）；简化用 driver
            out.append({
                'name': 'AMD GPU (amdgpu/radeon)', 'vendor': 'amd', 'integrated': False,
                'load_pct': load, 'temp_c': temp,
                'mem_used_mb': vram_used, 'mem_total_mb': vram_total,
                'mem_percent': round(vram_used / vram_total * 100, 1) if (vram_used and vram_total) else None,
            })
        except Exception:
            continue
    return out


def _gpu_static_once():
    """静态 GPU 列表 [{name, vendor, integrated, vram_mb}]，缓存。"""
    with _lock:
        if _CACHE['gpu_static'] is not None:
            return _CACHE['gpu_static']
        gpus = []
        nv = _nvml_handles()
        if nv is not None:
            try:
                import pynvml
                for h in nv:
                    name = pynvml.nvmlDeviceGetName(h)
                    name = name.decode() if isinstance(name, bytes) else str(name)
                    mem = pynvml.nvmlDeviceGetMemoryInfo(h)
                    gpus.append({'name': name, 'vendor': 'nvidia', 'integrated': False,
                                 'vram_mb': int(mem.total / 1024 / 1024)})
            except Exception:
                gpus = []
        if not gpus and _IS_WINDOWS:
            gpus = _win_static()
        if not gpus and _IS_LINUX:
            for card in glob.glob('/sys/class/drm/card*/device/mem_info_vram_total'):
                try:
                    base = os.path.dirname(card)
                    with open(card) as f:
                        total = int(f.read().strip()) // (1024 * 1024)
                    gpus.append({'name': 'GPU (sysfs)', 'vendor': 'amd', 'integrated': False, 'vram_mb': total})
                except Exception:
                    pass
        _CACHE['gpu_static'] = gpus
        return gpus


def _gpu_live():
    """实时 GPU：NVIDIA(pynvml) → Windows(计数器) → Linux AMD(sysfs) → 静态兜底。"""
    nvml = _gpu_live_nvml()
    if nvml:
        return nvml
    if _IS_WINDOWS:
        gpus = _win_gpu_live()
        if gpus:
            return gpus
    if _IS_LINUX:
        amd = _linux_amd_gpu()
        if amd:
            return amd
    # 兜底：仅静态名
    return [{'name': g.get('name'), 'vendor': g.get('vendor', 'unknown'),
             'integrated': g.get('integrated'), 'load_pct': None, 'temp_c': None,
             'mem_used_mb': None, 'mem_total_mb': g.get('vram_mb'), 'mem_percent': None}
            for g in _gpu_static_once()]


class HWMonitor:
    @staticmethod
    def collect():
        """实时负载（训练页轮询）。

        cpu_percent 用 interval=0.1（实测 0.1s 窗口，不依赖基线）——训练时引擎紧握 GIL，
        socketio 工作线程会被饿成簇触发，interval=None 的"距上次调用 delta"常因相邻调用过近返回 0.0；
        interval=0.1 每次独立测真实窗口，根治 0.0 闪烁。
        CPU 温度已移除：Windows ACPI 热区在桌面机返回静态阈值（非核心温度），不可靠。
        """
        import psutil
        return {
            'cpu_percent': psutil.cpu_percent(interval=0.1),
            'ram_percent': psutil.virtual_memory().percent,
            'gpus': _gpu_live(),
        }

    @staticmethod
    def static_info():
        import psutil
        return {
            'cpu_model': platform.processor() or platform.uname().machine,
            'cpu_cores': psutil.cpu_count(logical=True),
            'cpu_cores_physical': psutil.cpu_count(logical=False),
            'ram_gb': round(psutil.virtual_memory().total / 1024 ** 3, 1),
            'gpus': _gpu_static_once(),
            'os': platform.platform(),
            'python_ver': platform.python_version(),
            'gpu_note': '' if _gpu_static_once() else '未检测到 GPU',
        }

    @staticmethod
    def summary():
        s = HWMonitor.static_info()
        real = [g for g in (s.get('gpus') or [])
                if g.get('name') and not any(k in (g['name'] or '') for k in ('Virtual', 'Oray', 'Remote', 'DisplayLink'))]
        gpu0 = (real or s.get('gpus') or [{}])[0]
        return {
            'cpu_model': s['cpu_model'],
            'cpu_cores': s['cpu_cores'],
            'ram_gb': s['ram_gb'],
            'gpu_model': gpu0.get('name', ''),
            'gpu_vram_gb': round(gpu0['vram_mb'] / 1024, 1) if gpu0.get('vram_mb') else None,
            'os': s['os'],
            'python_ver': s['python_ver'],
        }

# -*- coding: utf-8 -*-
"""TorchScript 算子（明文保留，不随 AlphaGPT.py 一起 Cython 编译）。

为何独立成明文模块：
  @torch.jit.script 与 Cython 编译冲突——Cython 编译后函数类型变为
  cython_function_or_method（inspect.isfunction=False、inspect.getsource 失败），
  torch.jit.script 在类型分派层即抛 TypeError「... is a built-in class」，
  导致含 @torch.jit.script 的 .pyd 在【导入期】装饰器触发时直接崩溃。
  2026-07-30 实测确认（机制 + 端到端 .pyd 编译复现），见 docs/07 §3.4。

解法（docs/07 §3.4 既定兜底）：把这 4 个 jit 函数留在【明文】本模块，
AlphaGPT.py（编译为 .pyd）`from _jit_ops import ...` 导入之。jit 装饰器在本明文
模块执行 → 面对真 PyFunction + 有源码 → script 正常；.pyd 只消费产出的 ScriptFunction，
不再触发冲突。

代价：4 个琐碎张量 helper（时延/门控/跳变/衰减）以明文随包，属可接受的最小外泄
（核心 IP——19 因子定义、Transformer 策略网、PPO 训练、回测——仍在编译保护的 AlphaGPT.pyd 内）。

发布注意（build_release / setup_cython）：
  - 本文件【必须明文随包】，不得进入 cythonize 清单；
  - AlphaGPT.py 的 .py/.pyx 源必须从包中排除（只发 .pyd），否则加密形同虚设。
"""
import torch


@torch.jit.script
def _ts_delay(x: torch.Tensor, d: int) -> torch.Tensor:
    if d == 0: return x
    pad = torch.zeros((x.shape[0], d), device=x.device)
    return torch.cat([pad, x[:, :-d]], dim=1)


@torch.jit.script
def _op_gate(condition: torch.Tensor, x: torch.Tensor, y: torch.Tensor) -> torch.Tensor:
    mask = (condition > 0).float()
    return mask * x + (1.0 - mask) * y


@torch.jit.script
def _op_jump(x: torch.Tensor) -> torch.Tensor:
    mean = x.mean(dim=1, keepdim=True)
    std = x.std(dim=1, keepdim=True) + 1e-6
    z = (x - mean) / std
    return torch.relu(z - 3.0)


@torch.jit.script
def _op_decay(x: torch.Tensor) -> torch.Tensor:
    return x + 0.8 * _ts_delay(x, 1) + 0.6 * _ts_delay(x, 2)

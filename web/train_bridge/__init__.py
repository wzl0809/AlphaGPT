# -*- coding: utf-8 -*-
"""训练桥接：把核心引擎 AlphaGPT.py 接入 Flask+SocketIO。

模块：
  emitter       后台线程向 WebSocket 推送的统一出口（app_context 包装）
  progress      SocketIOTqdm（替换系统 tqdm，移植自 demo）
  log_handler   SocketIOLogHandler + CaptureIO（移植自 demo）
  seed          种子策略 A–F
  env_injector  UI 参数 → 环境变量（对接 AlphaGPT.Config 覆盖机制）
  result_parser 训练产物 reports/ 解析 → 本地 SQLite LocalFormula + 分类
  runner        后台训练线程编排（reload/重定向/钩子）
"""

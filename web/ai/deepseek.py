# -*- coding: utf-8 -*-
"""LLM 调用(DeepSeek / GLM / 通义,个股分析 / 大盘分析 / 结构化输出)。

重构自旧版纯 requests 直调 DeepSeek,升级为:
- LiteLLM 多 provider 路由(DeepSeek → GLM → 通义,按可用 key 优先级 + 指数退避)
- chat_structured(): 返回经 report_schema 校验的结构化对象(替代纯文本)
- 旧 chat()/analyze_market()/analyze_stock() 保留签名兼容(home/quant 旧调用方不破)
- 失败返回 None(不再返回 "[deepseek 调用失败 xxx]" 错误字符串,避免被当结果渲染)
- litellm 不可用时自动降级 requests 直连 DeepSeek(plan 降级路径)

Key 来源: web.services.apikey_store(本地加密 db/api_keys.enc,权威源) → config 回退。
LiteLLM 通过显式 api_key= 参数传 key,apply_to_app 也会写 os.environ 兜底。
"""
import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

# 旧版 DeepSeek 直连端点(降级路径用)
_API = 'https://api.deepseek.com/v1/chat/completions'

# LiteLLM 可用性(启动时探测,失败则全程走 requests 降级)
try:
    # 跳过启动时拉 GitHub 成本表：litellm.__init__ 会 httpx.get raw.githubusercontent.com
    # 国内常超时(读注册表代理/墙)，打出吓人的 "Failed to fetch remote model cost map" 警告。
    # 设 LITELLM_LOCAL_MODEL_COST_MAP=True → 直接用包内内置表，零网络、零延迟、零告警。
    # 必须在 import litellm 之前设(get_model_cost_map 在 __init__ 期执行)。
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    import litellm  # noqa: F401
    _HAS_LITELLM = True
    try:
        litellm.suppress_debug_info = True  # 静音噪声日志
    except Exception:  # noqa: BLE001
        pass
except ImportError:
    _HAS_LITELLM = False

# provider 配置: (store_name, default_litellm_model, litellm_env, base_url_env)
#   - 前 3 个是命名 provider（固定 model 前缀，LiteLLM 自动路由）
#   - openai_compat 是通用 OpenAI 兼容端点：model 与 base_url 由用户在「系统设置」填，
#     覆盖 OpenAI / Kimi(月之暗面) / 零一Yi / OpenRouter / 硅基流动 / Ollama / 本地 vLLM 等。
# 优先级: 按列表顺序遍历，首个有 key 且调用成功的胜出。
_PROVIDERS = [
    ("deepseek", "deepseek/deepseek-v4-flash", "DEEPSEEK_API_KEY", None),   # V4：原 deepseek-chat 已下线（官方仅支持 deepseek-v4-pro/flash）
    ("zhipu",    "zhipu/glm-4-flash",     "ZHIPU_API_KEY", None),
    ("tongyi",   "dashscope/qwen-turbo",  "DASHSCOPE_API_KEY", None),
    ("openai_compat", None, "OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL"),
]


# ---------- Key 读取 ----------
def _all_keys() -> dict:
    """读全部 provider key(明文)。返回 {store_name: key}。"""
    out = {}
    try:
        from web.services import apikey_store
        stored = apikey_store.load()
    except Exception:  # noqa: BLE001
        stored = {}
    # config env 名与 store_name 的映射(apikey_store.KEY_MAP)
    env_for = {"deepseek": "DEEPSEEK_API_KEY",
               "zhipu": "ZHIPU_API_KEY",
               "tongyi": "TONGYI_API_KEY",
               "openai_compat": "OPENAI_COMPAT_API_KEY"}
    for entry in _PROVIDERS:
        store_name = entry[0]
        k = stored.get(store_name)
        if not k:
            cfg_key = env_for.get(store_name, "")
            if cfg_key:
                try:
                    from flask import current_app
                    k = current_app.config.get(cfg_key, "") or ""
                except Exception:  # noqa: BLE001
                    k = os.getenv(cfg_key, "") or ""
        if k:
            out[store_name] = k
    return out


def _key() -> str:
    """旧版兼容: 返回 deepseek key(优先)。"""
    return _all_keys().get("deepseek", "")


def has_key() -> bool:
    """是否有任意可用 provider key(home/quant 据此显隐 AI 卡)。"""
    return bool(_all_keys())


# ---------- LiteLLM 多 provider 调用 ----------
def _call_litellm(prompt: str, system_prompt: str = None,
                  max_tokens: int = 512, temperature: float = 0.5,
                  timeout: int = 40) -> str | None:
    """遍历可用 provider,每个指数退避重试。返回文本或 None。"""
    if not _HAS_LITELLM:
        return None
    keys = _all_keys()
    if not keys:
        return None
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    for store_name, default_model, litellm_env, base_env in _PROVIDERS:
        api_key = keys.get(store_name)
        if not api_key:
            continue
        # 解析 model / api_base：openai_compat 用用户填的 model+base_url；其余用命名默认
        if store_name == "openai_compat":
            model = os.getenv("OPENAI_COMPAT_MODEL", "").strip()
            if not model:
                logger.warning("openai_compat 配了 key 但未配 model 名(OPENAI_COMPAT_MODEL)，跳过该 provider")
                continue
            litellm_model = model if "/" in model else f"openai/{model}"
            api_base = (os.getenv(base_env, "").strip() if base_env else "") or None
        else:
            litellm_model = default_model
            api_base = None
        # 兜底写 os.environ(部分 provider 内部读 env)
        if litellm_env:
            os.environ[litellm_env] = api_key
        kwargs = dict(model=litellm_model, messages=messages,
                      max_tokens=max_tokens, temperature=temperature,
                      api_key=api_key, timeout=timeout)
        if api_base:
            kwargs["api_base"] = api_base
        for attempt in range(3):
            try:
                resp = litellm.completion(**kwargs)
                return resp.choices[0].message.content.strip()
            except Exception as e:  # noqa: BLE001
                msg = str(e).lower()
                is_rate_limit = any(k in msg for k in ("429", "rate", "quota", "limit"))
                if is_rate_limit and attempt < 2:
                    time.sleep(min(2 ** attempt, 60))
                    continue
                logger.warning("litellm %s 失败(尝试%d/3): %s",
                               litellm_model, attempt + 1, e)
                break  # 该 provider 失败,换下一个
    return None


def _chat_requests_deepseek(prompt: str, max_tokens: int = 512,
                            timeout: int = 40) -> str | None:
    """requests 直连 DeepSeek(litellm 不可用或全部 provider 失败时降级)。"""
    key = _all_keys().get("deepseek")
    if not key:
        return None
    try:
        r = requests.post(_API,
            headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
            json={'model': 'deepseek-v4-flash',   # V4：原 deepseek-chat 已下线
                  'messages': [{'role': 'user', 'content': prompt}],
                  'max_tokens': max_tokens, 'temperature': 0.5},
            timeout=timeout)
        if r.status_code != 200:
            logger.warning("deepseek requests 调用失败 status=%s", r.status_code)
            return None
        return r.json()['choices'][0]['message']['content'].strip()
    except Exception as e:  # noqa: BLE001
        logger.warning("deepseek requests 异常: %s", e)
        return None


# ---------- 对外接口 ----------
def chat(prompt: str, max_tokens: int = 512, timeout: int = 40) -> str | None:
    """通用对话(旧版兼容,home/quant 旧调用方用)。

    返回 str|None。无 key→None;失败→None(不再返回错误字符串)。
    优先 litellm 多 provider,降级 requests 直连 deepseek。
    """
    if not has_key():
        return None
    if _HAS_LITELLM:
        text = _call_litellm(prompt, max_tokens=max_tokens, timeout=timeout)
        if text is not None:
            return text
    return _chat_requests_deepseek(prompt, max_tokens=max_tokens, timeout=timeout)


def chat_structured(system_prompt: str, user_prompt: str, schema_cls=None,
                    max_tokens: int = 2000, temperature: float = 0.3,
                    timeout: int = 60):
    """结构化对话: 返回 schema 实例 / dict / None。

    优先 litellm(多 provider,带 system role); 降级 requests 直连 deepseek
    (system_prompt 拼进 user,因 requests 路径无独立 system role)。
    用 web.ai.llm_json.parse_llm_json 鲁棒解析 + 可选 schema 校验。

    Args:
        system_prompt: 系统提示(角色/规则/输出格式)
        user_prompt: 用户内容(行情数据/分析任务)
        schema_cls: 可选 Pydantic 模型(如 AnalysisReportSchema)
    """
    if not has_key():
        return None
    text = None
    if _HAS_LITELLM:
        text = _call_litellm(user_prompt, system_prompt=system_prompt,
                             max_tokens=max_tokens, temperature=temperature,
                             timeout=timeout)
    if text is None:
        # 降级: requests 直连 deepseek, system 拼进 user
        combined = (f"{system_prompt}\n\n{user_prompt}"
                    if system_prompt else user_prompt)
        text = _chat_requests_deepseek(combined, max_tokens=max_tokens, timeout=timeout)
    if not text:
        return None
    from web.ai.llm_json import parse_llm_json
    return parse_llm_json(text, schema_cls=schema_cls)


# ---------- 旧版业务封装(保留签名,Phase 3/4 升级为 chat_structured) ----------
def analyze_stock(code: str, name: str, signal: str = None, formula: str = None,
                  stock_context=None, news_context=None, formula_result=None) -> dict | None:
    """个股研究仪表盘分析(Phase 4 升级)。

    注入 K线/均线/乖离率/新闻/AlphaGPT信号 → chat_structured(STOCK_SYSTEM_PROMPT,...)
    → AnalysisReportSchema(评分/趋势/参考区间/检查清单 + 反向趋势约束)。返回 dict 或 None。

    保留旧位置签名(code/name/signal/formula)兼容;新增 stock_context/news_context/formula_result。
    无 stock_context 时自动调 stock_context_builder.build(取数+算指标)。
    """
    if not has_key():
        return None
    from .prompts.stock_decision import STOCK_SYSTEM_PROMPT, format_stock_prompt
    from .schemas.report_schema import AnalysisReportSchema
    # 自动取上下文(调用方没传时)
    if stock_context is None:
        try:
            from . import stock_context_builder
            stock_context = stock_context_builder.build(code, name)
        except Exception:  # noqa: BLE001
            stock_context = {"code": code, "name": name, "data_missing": True}
    # 合并 formula_result(信号 + 量化置信度)
    fr = dict(formula_result or {})
    if signal and "signal" not in fr:
        fr["signal"] = signal
    user_prompt = format_stock_prompt(stock_context or {}, news_context or "", fr)
    result = chat_structured(STOCK_SYSTEM_PROMPT, user_prompt,
                             schema_cls=AnalysisReportSchema, max_tokens=2000, timeout=90)
    if result is None:
        return None
    return result.model_dump() if hasattr(result, "model_dump") else result


def analyze_market(market_snapshot=None) -> dict | None:
    """大盘评述(首页用)。注入真实行情指标,返回结构化 dict。

    Phase 3 升级: 旧版只注入日期让 LLM 凭记忆猜(导致"连跌也判震荡");
    新版注入 market_breadth 的真实涨跌家数/涨停跌停/指数涨跌幅 + 反向趋势约束,
    返回 {sentiment_score, trend_prediction, operation_advice, dashboard{core_conclusion,
    intelligence{risk_alerts,positive_catalysts,...}}, analysis_summary, ...}。

    Args:
        market_snapshot: market_breadth.build_snapshot().to_dict()。None 时自动采集(慢)。
    Returns:
        结构化 dict 或 None(无 key/调用失败)。
    """
    if not has_key():
        return None
    from .prompts.market_review import MARKET_REVIEW_SYSTEM_PROMPT, format_market_prompt
    from .schemas.report_schema import AnalysisReportSchema
    from .market_breadth import MarketSnapshot
    # dict → MarketSnapshot(format_market_prompt 读属性)
    snap = None
    if market_snapshot is not None:
        if isinstance(market_snapshot, dict):
            try:
                snap = MarketSnapshot(**market_snapshot)
            except Exception:  # noqa: BLE001
                snap = None
        else:
            snap = market_snapshot
    if snap is None:
        try:
            from . import market_breadth
            snap = market_breadth.build_snapshot()
        except Exception:  # noqa: BLE001
            snap = None
    user_prompt = format_market_prompt(snap) if snap else "今日 A 股大盘分析(无实时行情数据,请谨慎判断)"
    result = chat_structured(MARKET_REVIEW_SYSTEM_PROMPT, user_prompt,
                             schema_cls=AnalysisReportSchema, max_tokens=1500, timeout=90)
    if result is None:
        return None
    return result.model_dump() if hasattr(result, "model_dump") else result

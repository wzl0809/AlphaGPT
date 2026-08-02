# -*- coding: utf-8 -*-
"""
LLM JSON 鲁棒解析
==================
从 daily_stock_analysis (src/analyzer.py 的 _fix_json_string / _parse_response) 移植。
处理 LLM(DeepSeek/GLM)返回文本的常见瑕疵:```json 代码块包裹、// 注释、尾随逗号、
True/False 大写、坏转义、引号缺失等。

所有调 LLM 拿结构化输出的地方(web/ai/deepseek.py 的 chat_structured)统一走这里。
失败一律返回 None,不抛异常,不返回错误字符串 —— 避免上层把错误提示当结果渲染
(这是旧 deepseek.py「[deepseek 调用失败 xxx]」被当个股分析显示的根因之一)。
"""

import json
import re
from typing import Any, Optional, Type, TypeVar

try:
    from json_repair import repair_json
    _HAS_REPAIR = True
except ImportError:  # json-repair 未装时降级为纯正则
    _HAS_REPAIR = False

try:
    from pydantic import BaseModel, ValidationError
    _HAS_PYDANTIC = True
except ImportError:
    _HAS_PYDANTIC = False
    BaseModel = None  # type: ignore
    ValidationError = Exception  # type: ignore

T = TypeVar("T")

_CODE_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*", re.IGNORECASE)


def _strip_code_fence(text: str) -> str:
    """剥 ```json ... ``` 代码块标记。"""
    if "```" not in text:
        return text
    cleaned = _CODE_FENCE_RE.sub("", text)
    cleaned = cleaned.replace("```", "")
    return cleaned


def fix_json_string(json_str: str) -> str:
    """修复常见 JSON 格式问题(直搬 DSA _fix_json_string, L4606)。

    顺序: 去 // 行注 → 去 /* */ 块注 → 去尾逗号(,} / ,]) →
          布尔/None 小写(True→true/False→false/None→null) → json_repair 兜底。
    """
    # 移除行注释
    json_str = re.sub(r"//[^\n]*", "\n", json_str)
    # 移除块注释
    json_str = re.sub(r"/\*.*?\*/", "", json_str, flags=re.DOTALL)
    # 修复尾随逗号
    json_str = re.sub(r",\s*}", "}", json_str)
    json_str = re.sub(r",\s*]", "]", json_str)
    # 布尔/None 小写
    json_str = json_str.replace("True", "true").replace("False", "false")
    json_str = json_str.replace("None", "null")
    # json-repair 兜底(处理引号缺失/转义错误等更复杂问题)
    if _HAS_REPAIR:
        try:
            json_str = repair_json(json_str, return_objects=False)
        except Exception:
            pass
    return json_str


def extract_json_object(text: str) -> Optional[str]:
    """从文本中截取第一个完整的 {...} JSON 对象。

    剥代码块后,用括号配平(忽略字符串内的括号)找匹配的 },避免 rfind 误取无关尾花括号。
    """
    cleaned = _strip_code_fence(text)
    start = cleaned.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(cleaned)):
        ch = cleaned[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return cleaned[start:i + 1]
    # 括号不平,fallback 到 find/rfind
    end = cleaned.rfind("}")
    if end > start:
        return cleaned[start:end + 1]
    return None


def parse_llm_json(text: str, schema_cls: Optional[Type[T]] = None) -> Optional[Any]:
    """解析 LLM 返回文本为 JSON dict,可选 schema 校验。

    Args:
        text: LLM 原始返回(可能含 ```json 代码块/注释/尾逗号/坏转义)
        schema_cls: 可选 Pydantic 模型类(如 AnalysisReportSchema)。

    Returns:
        - schema_cls 提供:校验通过返回 schema 实例;校验失败 fallback 返回原始 dict
          (不丢 LLM 已产出的内容,上层可降级使用字段)
        - schema_cls 不提供:返回 dict
        - 解析失败(空文本/无 JSON/json.loads 报错):返回 None
    """
    if not text or not text.strip():
        return None
    json_str = extract_json_object(text)
    if not json_str:
        return None
    json_str = fix_json_string(json_str)
    try:
        data = json.loads(json_str)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if schema_cls is not None and _HAS_PYDANTIC:
        try:
            if isinstance(schema_cls, type) and issubclass(schema_cls, BaseModel):
                return schema_cls.model_validate(data)
        except ValidationError:
            # schema 校验失败,fallback 返回原始 dict(上层可降级)
            return data
    return data


"""鲁棒解析 LLM 返回的决策/计划 JSON。

要点：模型输出本身往往就是合法 JSON，其字符串值里又可能内嵌 ```json 代码块。
必须**先直接解析原文**，绝不能在解析前做代码围栏抽取——否则会把字符串内层的 ```
误当成最外层围栏，把整段 JSON 截断成碎片，导致 tool_calls 丢失、空决策反复重试而死循环。

解析顺序：
  1) 直接 json.loads 原文（最常见，且能正确处理字符串里内嵌的代码块）；
  2) 仅当整段被 ``` 围栏包裹时，剥掉**最外层**围栏后再解析（保留字符串内部的围栏）；
  3) 容错恢复：json_repair / 抽取首个配平 {...} 片段，对原文与去围栏体分别尝试。
"""

from __future__ import annotations

import json

try:
    import json_repair  # type: ignore
except ImportError:  # 未安装时降级为 None，严格解析路径不受影响
    json_repair = None


def extract_first_json_object(text: str) -> "str | None":
    """配平括号扫描，跟踪字符串字面量与反斜杠转义，返回首个配平的顶层 {...} 子串。

    因为跟踪了字符串状态，字符串值里内嵌的 { } 和 ``` 都会被正确跳过。"""
    in_string = False
    escape_next = False
    depth = 0
    start = None
    for i, ch in enumerate(text):
        if escape_next:
            escape_next = False
            continue
        if in_string:
            if ch == "\\":
                escape_next = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    return text[start:i + 1]
    return None


def strip_outer_code_fence(text: str) -> "str | None":
    """仅当整段文本被 ```（可带语言名）代码围栏包裹时，剥掉最外层围栏并返回内体；
    否则返回 None。只移除最外层（首行围栏 + 最后一个 ```），保留字符串内部的围栏，
    避免把模型 content 里内嵌的 ```json 代码块误当成外层围栏。"""
    s = text.strip()
    if not s.startswith("```"):
        return None
    newline = s.find("\n")
    if newline == -1:
        return None
    body = s[newline + 1:]
    end = body.rfind("```")
    if end != -1:
        body = body[:end]
    body = body.strip()
    return body or None


def loads_tolerant(text: str) -> "dict | None":
    """容错解析，返回首个得到的 dict，否则 None：
    (a) 严格 json.loads (b) json_repair.loads（已安装时） (c) 抽取首个配平 {...} 后重试 (a)(b)。"""
    # (a) 严格解析
    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    # (b) json_repair 修复整段
    if json_repair is not None:
        try:
            result = json_repair.loads(text)
            if isinstance(result, dict):
                return result
        except Exception:
            pass

    # (c) 抽取首个配平 {...} 再试
    fragment = extract_first_json_object(text)
    if fragment is not None and fragment != text:
        try:
            result = json.loads(fragment)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass
        if json_repair is not None:
            try:
                result = json_repair.loads(fragment)
                if isinstance(result, dict):
                    return result
            except Exception:
                pass

    return None


def robust_json_load(text: str) -> "dict | None":
    """按正确顺序鲁棒解析模型决策 JSON；无法恢复为 dict 时返回 None。"""
    text = (text or "").strip()
    if not text:
        return None

    # 1) 原文直接解析（最常见，且能正确处理字符串内嵌的代码块）
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    # 2) 整体被代码围栏包裹时，仅剥最外层围栏后再解析
    body = strip_outer_code_fence(text)
    if body is not None:
        try:
            data = json.loads(body)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

    # 3) 容错恢复：对原文与去围栏体分别尝试
    for candidate in (text, body):
        if not candidate:
            continue
        data = loads_tolerant(candidate)
        if isinstance(data, dict):
            return data
    return None

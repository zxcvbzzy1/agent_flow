#!/usr/bin/env python3
"""零依赖的 stdio MCP server：把 domain.skill 的检索暴露成 recall_skill 工具。

为什么手写：避免引入 `mcp` 依赖；MCP stdio 传输就是按行分隔(newline-delimited)的
JSON-RPC 2.0，自己实现 initialize / tools/list / tools/call 即可。

由 coding agent 的 CLI 作为子进程拉起（claude --mcp-config / codex -c mcp_servers）。
它从同一个 agent_flow/skills 目录加载技能，检索结果直接作为工具返回值进 CLI 的工具循环
（跨进程，不写 agent_flow 的 memory —— 这正是“工具循环内同步调用”的语义）。

stdout 纪律（关键）：stdout 只能出现 JSON-RPC 响应。因此启动时把 sys.stdout 改指向
stderr，任何意外 print（如技能加载日志）都不会污染协议流；响应只通过保存的真实 stdout 写出。
"""

from __future__ import annotations

import json
import os
import sys

# stdout 只留给 JSON-RPC；其余一切（含 import/加载期的 print）改走 stderr。
_REAL_STDOUT = sys.stdout
sys.stdout = sys.stderr

# 把 agent_flow 根加入 sys.path：mcp_server.py 位于 agent_flow/infra/skill/ → 上溯三级。
_AGENT_FLOW_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _AGENT_FLOW_ROOT not in sys.path:
    sys.path.insert(0, _AGENT_FLOW_ROOT)

from domain.skill import (  # noqa: E402
    default_registry,
    default_retriever,
    load_skills_from_dir,
)

SERVER_NAME = "skill"
SERVER_VERSION = "0.1.0"
TOOL_NAME = "recall_skill"
DEFAULT_PROTOCOL = "2024-11-05"

TOOL_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "用于检索技能的自然语言查询（任务或问题的描述）",
        },
        "k": {"type": "integer", "description": "返回条数，默认 5"},
    },
    "required": ["query"],
}
TOOL_DESCRIPTION = (
    "从技能库（可检索的记忆）召回与查询最相关的技能/方法，返回其步骤与要点。"
    "当你不确定某类任务的最佳做法、或想复用既有经验时调用。"
)


def _load_skills() -> None:
    try:
        default_registry.add_many(load_skills_from_dir())
    except Exception as exc:  # noqa: BLE001 —— 加载失败不致命，工具退化为“无召回”
        print(f"[skill-mcp] 技能加载失败: {exc}", file=sys.stderr)


def _retrieve_text(query: str, k: int) -> str:
    hits = default_retriever.retrieve(query or "", k=k, threshold=0.0)
    if not hits:
        return "未召回到相关技能。"
    out = [f"召回 {len(hits)} 条技能（按相关度排序）："]
    for hit in hits:
        skill = hit.skill
        block = f"\n### {skill.name}（相关度 {float(getattr(hit, 'score', 0.0)):.3f}）"
        if skill.description:
            block += f"\n{skill.description}"
        if skill.content:
            block += f"\n{skill.content}"
        out.append(block.rstrip())
    return "\n".join(out)


def _ok(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _err(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _handle(req: dict):
    method = req.get("method")
    rid = req.get("id")
    params = req.get("params") or {}

    if method == "initialize":
        proto = params.get("protocolVersion") or DEFAULT_PROTOCOL
        return _ok(rid, {
            "protocolVersion": proto,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })

    if method == "tools/list":
        return _ok(rid, {"tools": [{
            "name": TOOL_NAME,
            "description": TOOL_DESCRIPTION,
            "inputSchema": TOOL_INPUT_SCHEMA,
        }]})

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if name != TOOL_NAME:
            return _err(rid, -32602, f"未知工具: {name}")
        query = str(args.get("query", "")).strip()
        try:
            k = int(args.get("k") or 5)
        except (TypeError, ValueError):
            k = 5
        text = _retrieve_text(query, k)
        return _ok(rid, {"content": [{"type": "text", "text": text}], "isError": False})

    if method == "ping":
        return _ok(rid, {})

    # 通知（无 id）一律不回；其余未知方法返回 method not found。
    if rid is None:
        return None
    return _err(rid, -32601, f"方法未实现: {method}")


def main() -> None:
    _load_skills()
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(req, dict):
            continue
        try:
            resp = _handle(req)
        except Exception as exc:  # noqa: BLE001
            resp = _err(req.get("id"), -32603, f"内部错误: {exc}")
        if resp is not None:
            _REAL_STDOUT.write(json.dumps(resp, ensure_ascii=False) + "\n")
            _REAL_STDOUT.flush()


if __name__ == "__main__":
    main()

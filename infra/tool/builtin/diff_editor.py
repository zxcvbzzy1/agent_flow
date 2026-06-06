"""协同文件编辑工具（diff_editor）。

与 `file` 工具的区别：`file` 是 Agent 单方面读写；`diff_editor` 面向"人机协同改某个文件"——
Agent 每次提出修改都先生成一份 **Diff 产物**（artifacts.diff）展示给用户，由用户在卡片上
**一键应用**（apply）后才真正落盘；落盘时记录一版快照，支持**版本历史**与**回退**。

为什么 apply 走"服务端待应用记录"（pending）：
  Diff 卡片里的 after 内容在前端可见，如果让前端把 after 传回来落盘，等于开放了任意内容/路径
  写入。这里改为：propose_edit 时把 {abs_path, after, before_sha} 存进进程内 `_PENDING`，前端
  apply 只回传 edit_id，落盘的内容与路径**完全由服务端记录决定**，前端无法注入。

存储边界（与用户确认过）：
  版本历史用**工作区内快照**（work_path/.diff_editor_history/），无数据库、随工作区迁移，
  够当前协作 run 内使用；进程重启后 pending 失效（apply 返回 expired，让用户重新生成）。

跨进程：im_backend 与 agent_flow 同进程（im_backend import agent_flow），因此 REST 接口可直接
import 本模块的 apply_pending_edit / save_document / list_history / revert_version 调用，
共享同一个 `_PENDING` 字典与工作区快照。
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any

from domain.event import Event
from domain.tool import Tool, Tool_respond
from infra.config import agent_dict, bus, factory
from infra.event_bind import On_bind
from infra.tool.builtin.artifacts import InlineArtifactTool
from infra.tool.builtin.file_tools import _resolve, _work_path, _rel_display


HISTORY_DIRNAME = ".diff_editor_history"
# 进程内待应用记录：edit_id -> {agent_id, abs_path, file_path, after, before_sha, language, work_path, ...}
_PENDING: dict[str, dict[str, Any]] = {}
# agent_id -> work_path 缓存（propose/apply 时写入），用于 run 结束后 history/revert 仍能定位历史目录
_WORK_PATHS: dict[str, str] = {}


DIFF_EDITOR = Tool(
    name="diff_editor",
    description=(
        "与用户协同修改某个文件的工具，通过 operation 选择操作：\n"
        "- propose_edit：读取目标文件，基于你给出的 new_content 生成一份 Diff 卡片展示给用户，"
        "由用户在卡片上一键应用后才真正落盘（不要用 file.write 直接覆盖需要协同确认的文件）。"
        "可选 instruction（本次修改说明）、selection（用户选中的局部代码，用于局部修改）。\n"
        "- list_history：列出某文件的版本历史。\n"
        "- revert：把某文件回退到指定版本。\n"
        "- open_document：把某文件以可编辑文档卡片打开，用户可在卡片内编辑并回写原文件。\n"
        "路径处理：绝对路径直接使用，相对路径相对于工作目录解析。"
    ),
    field="system",
    input_schema={
        "type": "object",
        "required": ["operation"],
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["propose_edit", "list_history", "revert", "open_document"],
                "description": "要执行的协同编辑操作",
            },
            "propose_edit": {
                "type": "object",
                "description": "提出一处文件修改并生成 Diff 卡片的参数。必填 path、new_content（修改后的完整文件内容）。",
                "properties": {
                    "path": {"type": "string", "description": "目标文件路径（绝对或相对工作目录）"},
                    "new_content": {"type": "string", "description": "修改后的完整文件内容"},
                    "instruction": {"type": "string", "description": "本次修改说明（可选，展示在卡片标题/元信息）"},
                    "selection": {
                        "type": "object",
                        "description": "用户选中的局部代码（局部修改场景，可选）",
                        "properties": {
                            "start": {"type": "integer", "description": "选区起始偏移（可选）"},
                            "end": {"type": "integer", "description": "选区结束偏移（可选）"},
                            "text": {"type": "string", "description": "选中的原文本（可选）"},
                        },
                    },
                },
            },
            "list_history": {
                "type": "object",
                "description": "列出某文件版本历史的参数。必填 path。",
                "properties": {
                    "path": {"type": "string", "description": "目标文件路径"},
                },
            },
            "revert": {
                "type": "object",
                "description": "把某文件回退到指定版本的参数。必填 path、version。",
                "properties": {
                    "path": {"type": "string", "description": "目标文件路径"},
                    "version": {"type": "integer", "description": "要回退到的版本号"},
                },
            },
            "open_document": {
                "type": "object",
                "description": "把某文件以可编辑文档卡片打开的参数。必填 path。",
                "properties": {
                    "path": {"type": "string", "description": "目标文件路径"},
                    "title": {"type": "string", "description": "卡片标题（可选）"},
                },
            },
        },
    },
)


# ---------------------------------------------------------------------------
# 公共辅助
# ---------------------------------------------------------------------------

def _sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", errors="replace")).hexdigest()


def _read_raw(target: Path) -> str:
    """读取文件全文用于 diff；不存在返回空串（视作新文件）。"""
    if target.exists() and target.is_file():
        return target.read_text(encoding="utf-8", errors="replace")
    return ""


def _ext_language(path: str) -> str:
    ext = Path(path).suffix.lstrip(".").lower()
    return InlineArtifactTool.LANGUAGE_BY_FORMAT.get(ext, ext or "text")


def _ext_format(path: str) -> str:
    return Path(path).suffix.lstrip(".").lower() or "txt"


def _cache_work_path(agent_id: str) -> Path:
    wp = _work_path(agent_id)
    _WORK_PATHS[agent_id] = str(wp)
    return wp


def _resolve_work_path_strict(agent_id: str) -> Path | None:
    """解析该 agent 的工作目录；**只认已注册的实例或缓存，绝不退回进程 cwd**。

    安全要点：客户端驱动的写回（save_document / revert）必须 fail-closed —— 若 agent 已回收
    且无缓存（如进程重启或伪造的 agent_id），返回 None 让上层拒绝，而不是把 cwd 当成工作区根，
    否则路径包含检查会被锚定到任意 cwd 上，等于失去边界。
    """
    agent = agent_dict.get(agent_id) if hasattr(agent_dict, "get") else None
    if agent is not None and getattr(agent, "work_path", None):
        wp = str(Path(agent.work_path).expanduser())
        _WORK_PATHS[agent_id] = wp
        return Path(wp)
    cached = _WORK_PATHS.get(agent_id)
    if cached:
        return Path(cached)
    return None


def _abs_under(work_path: Path, file_path: str) -> Path:
    """相对路径一律相对 work_path 解析（不走 _work_path 的 cwd 回退）；绝对路径原样。"""
    p = Path(file_path).expanduser()
    return p if p.is_absolute() else (work_path / p)


def _file_key(abs_path: Path) -> str:
    return hashlib.sha1(str(abs_path).encode("utf-8")).hexdigest()[:16]


def _hist_dir(work_path: Path, abs_path: Path) -> Path:
    return work_path / HISTORY_DIRNAME / "files" / _file_key(abs_path)


def _record_version(
    work_path: Path,
    abs_path: Path,
    file_path: str,
    content: str,
    base_sha: str,
    edit_id: str,
    note: str = "",
) -> int:
    """写一版快照并更新索引，返回新版本号。失败不抛（历史是尽力而为，不应阻断落盘）。"""
    try:
        hist = _hist_dir(work_path, abs_path)
        hist.mkdir(parents=True, exist_ok=True)
        index_path = hist / "index.json"
        index: dict[str, Any] = {"file_path": file_path, "versions": []}
        if index_path.exists():
            try:
                index = json.loads(index_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                index = {"file_path": file_path, "versions": []}
        versions = index.get("versions") or []
        version = (max((v.get("version", 0) for v in versions), default=0)) + 1
        (hist / f"v{version}.snapshot").write_text(content, encoding="utf-8")
        versions.append(
            {
                "version": version,
                "edit_id": edit_id,
                "created_at": time.time(),
                "base_sha": base_sha,
                "result_sha": _sha256(content),
                "note": note,
                "bytes": len(content.encode("utf-8")),
            }
        )
        index["versions"] = versions
        index["file_path"] = file_path
        index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
        return version
    except OSError:
        return 0


def _ensure_within(work_path: Path, target: Path) -> bool:
    """REST 写回路径必须落在 work_path 内（比 file 工具更严，防客户端越界写）。"""
    try:
        target.resolve().relative_to(work_path.resolve())
        return True
    except (ValueError, OSError):
        return False


# ---------------------------------------------------------------------------
# 供 REST 接口与工具共用的核心动作
# ---------------------------------------------------------------------------

def apply_pending_edit(edit_id: str) -> dict[str, Any]:
    """落盘一条待应用 Diff。内容与路径完全取自服务端 pending 记录。

    返回 {status: applied|conflict|expired, ...}。
    """
    rec = _PENDING.get(edit_id)
    if rec is None:
        return {"status": "expired", "message": "该修改提案已失效（可能后端已重启），请让 Agent 重新生成。"}

    abs_path = Path(rec["abs_path"])
    file_path = rec.get("file_path", str(abs_path))
    current = _read_raw(abs_path)
    if _sha256(current) != rec["before_sha"]:
        return {
            "status": "conflict",
            "file_path": file_path,
            "message": "文件自生成该 Diff 后已发生变化，请让 Agent 基于最新内容重新生成。",
        }

    after = rec["after"]
    try:
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(after, encoding="utf-8")
    except OSError as exc:
        return {"status": "error", "file_path": file_path, "message": f"写入失败: {exc}"}

    wp_str = rec.get("work_path") or ""
    if not wp_str:
        sp = _resolve_work_path_strict(rec.get("agent_id", ""))
        wp_str = str(sp) if sp else ""
    version = (
        _record_version(
            Path(wp_str), abs_path, file_path, after, rec["before_sha"], edit_id, note=rec.get("instruction", "")
        )
        if wp_str
        else 0
    )
    _PENDING.pop(edit_id, None)
    return {
        "status": "applied",
        "file_path": file_path,
        "version": version,
        "applied_sha": _sha256(after),
        "bytes": len(after.encode("utf-8")),
    }


def save_document(agent_id: str, file_path: str, content: str, base_sha: str | None = None) -> dict[str, Any]:
    """文档卡片编辑回写原文件。内容来自用户（这是预期的），但路径限制在 work_path 内。"""
    work_path = _resolve_work_path_strict(agent_id)
    if work_path is None:
        return {"status": "error", "message": "无法定位该 agent 的工作目录（可能已回收），请在活动会话中操作。"}
    abs_path = _abs_under(work_path, file_path)
    if not _ensure_within(work_path, abs_path):
        return {"status": "error", "message": f"路径越界，拒绝写回：{file_path}"}

    current = _read_raw(abs_path)
    if base_sha and _sha256(current) != base_sha:
        return {
            "status": "conflict",
            "file_path": file_path,
            "current_content": current,
            "message": "文件已被改动，回写前请确认最新内容。",
        }
    try:
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(content, encoding="utf-8")
    except OSError as exc:
        return {"status": "error", "file_path": file_path, "message": f"写入失败: {exc}"}

    version = _record_version(
        work_path, abs_path, file_path, content, _sha256(current), uuid.uuid4().hex, note="文档编辑回写"
    )
    return {
        "status": "applied",
        "file_path": file_path,
        "version": version,
        "applied_sha": _sha256(content),
    }


def list_history(agent_id: str, file_path: str) -> dict[str, Any]:
    work_path = _resolve_work_path_strict(agent_id)
    if work_path is None:
        return {"file_path": file_path, "versions": []}
    abs_path = _abs_under(work_path, file_path)
    index_path = _hist_dir(work_path, abs_path) / "index.json"
    if not index_path.exists():
        return {"file_path": file_path, "versions": []}
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"file_path": file_path, "versions": []}
    versions = sorted(index.get("versions") or [], key=lambda v: v.get("version", 0))
    # 不回传 result_sha 之外的敏感信息；快照内容按需通过 revert 获取
    return {"file_path": file_path, "versions": versions}


def revert_version(agent_id: str, file_path: str, version: int) -> dict[str, Any]:
    work_path = _resolve_work_path_strict(agent_id)
    if work_path is None:
        return {"status": "error", "message": "无法定位该 agent 的工作目录（可能已回收），请在活动会话中操作。"}
    abs_path = _abs_under(work_path, file_path)
    if not _ensure_within(work_path, abs_path):
        return {"status": "error", "message": f"路径越界，拒绝写回：{file_path}"}
    snapshot = _hist_dir(work_path, abs_path) / f"v{int(version)}.snapshot"
    if not snapshot.exists():
        return {"status": "error", "message": f"版本 v{version} 不存在"}
    try:
        content = snapshot.read_text(encoding="utf-8")
        current = _read_raw(abs_path)
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(content, encoding="utf-8")
    except OSError as exc:
        return {"status": "error", "message": f"回退失败: {exc}"}
    new_version = _record_version(
        work_path, abs_path, file_path, content, _sha256(current), uuid.uuid4().hex, note=f"回退到 v{version}"
    )
    # 回传回退后的内容，前端据此刷新卡片正文（props.artifact 是静态 SSE 快照，不会自动更新）
    return {
        "status": "applied",
        "file_path": file_path,
        "version": new_version,
        "reverted_to": int(version),
        "content": content,
        "applied_sha": _sha256(content),
    }


# ---------------------------------------------------------------------------
# 注册与事件处理
# ---------------------------------------------------------------------------

on_tool = On_bind()
factory._build_and_register_list([DIFF_EDITOR], bus)


def _succeed(agent_id: str, respond) -> Event:
    if isinstance(respond, str) and not respond.strip():
        respond = "diff_editor 执行成功"
    return factory.tool("diff_editor").succeeded(
        Tool_respond(agent_id=agent_id, name="diff_editor", success=True, respond=respond)
    )


def _fail(agent_id: str, message: str) -> Event:
    return factory.tool("diff_editor").failed(
        Tool_respond(agent_id=agent_id, name="diff_editor", success=False, respond=message)
    )


async def _emit_artifact(artifact_type: str, agent_id: str, section: dict[str, Any]) -> None:
    payload = InlineArtifactTool().build_event_payload(
        {"artifact_type": artifact_type, "agent_id": agent_id, artifact_type: section}
    )
    await bus.publish(Event(payload["event_name"], payload=payload))


async def _do_propose_edit(agent_id: str, params: dict[str, Any]) -> Event:
    path = params.get("path")
    if not path:
        return _fail(agent_id, "propose_edit 需要提供 path")
    if "new_content" not in params:
        return _fail(agent_id, "propose_edit 需要提供 new_content（修改后的完整文件内容）")

    abs_path = _resolve(path, agent_id)
    work_path = _cache_work_path(agent_id)
    before = _read_raw(abs_path)
    after = str(params.get("new_content", ""))
    if before == after:
        return _succeed(agent_id, f"{path}: 新内容与当前文件一致，无需修改。")

    before_sha = _sha256(before)
    edit_id = uuid.uuid4().hex
    instruction = str(params.get("instruction", "") or "")
    display = _rel_display(abs_path, work_path)
    language = _ext_language(path)

    _PENDING[edit_id] = {
        "agent_id": agent_id,
        "abs_path": str(abs_path),
        "file_path": display,
        "after": after,
        "before_sha": before_sha,
        "language": language,
        "work_path": str(work_path),
        "instruction": instruction,
        "created_at": time.time(),
    }

    title = f"编辑 {display}" if not instruction else f"编辑 {display} · {instruction[:40]}"
    await _emit_artifact(
        "diff",
        agent_id,
        {
            "title": title,
            "before": before,
            "after": after,
            "file_path": display,
            "language": language,
            "metadata": {
                "edit_id": edit_id,
                "base_sha": before_sha,
                "agent_id": agent_id,
                "kind": "diff_editor",
                "instruction": instruction,
                "selection": params.get("selection") or None,
                "is_new_file": before == "",
            },
        },
    )
    return _succeed(
        agent_id,
        f"已生成 Diff（edit_id={edit_id}）展示给用户，待用户一键应用后落盘。文件: {display}",
    )


async def _do_open_document(agent_id: str, params: dict[str, Any]) -> Event:
    path = params.get("path")
    if not path:
        return _fail(agent_id, "open_document 需要提供 path")
    abs_path = _resolve(path, agent_id)
    work_path = _cache_work_path(agent_id)
    if not abs_path.exists():
        return _fail(agent_id, f"文件不存在: {abs_path}")
    content = _read_raw(abs_path)
    display = _rel_display(abs_path, work_path)
    fmt = _ext_format(path)
    await _emit_artifact(
        "document",
        agent_id,
        {
            "title": params.get("title") or display,
            "content": content,
            "format": fmt,
            "language": _ext_language(path),
            "editable": True,
            "metadata": {
                "file_path": display,
                "agent_id": agent_id,
                "base_sha": _sha256(content),
                "kind": "diff_editor",
            },
        },
    )
    return _succeed(agent_id, f"已打开可编辑文档卡片: {display}")


def _do_list_history(agent_id: str, params: dict[str, Any]) -> Event:
    path = params.get("path")
    if not path:
        return _fail(agent_id, "list_history 需要提供 path")
    result = list_history(agent_id, path)
    versions = result.get("versions") or []
    if not versions:
        return _succeed(agent_id, f"{path}: 暂无版本历史。")
    lines = [f"{result['file_path']} 版本历史（共 {len(versions)} 版）:"]
    for v in versions:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(v.get("created_at", 0)))
        note = f" - {v['note']}" if v.get("note") else ""
        lines.append(f"  v{v['version']}  {ts}  {v.get('bytes', 0)}B{note}")
    return _succeed(agent_id, "\n".join(lines))


def _do_revert(agent_id: str, params: dict[str, Any]) -> Event:
    path = params.get("path")
    version = params.get("version")
    if not path or version is None:
        return _fail(agent_id, "revert 需要提供 path 和 version")
    result = revert_version(agent_id, path, int(version))
    if result.get("status") != "applied":
        return _fail(agent_id, result.get("message", "回退失败"))
    return _succeed(agent_id, f"{result['file_path']} 已回退到 v{result['reverted_to']}（记为 v{result['version']}）。")


@on_tool.on(factory.tool("diff_editor").called())
async def on_diff_editor(**kwargs) -> Event:
    agent_id = kwargs.get("agent_id", "")
    operation = str(kwargs.get("operation", "")).strip().lower()

    if operation == "propose_edit":
        params = kwargs.get("propose_edit")
        if not isinstance(params, dict):
            return _fail(agent_id, "操作 propose_edit 需要提供 `propose_edit` 参数对象")
        try:
            return await _do_propose_edit(agent_id, params)
        except Exception as exc:  # noqa: BLE001
            return _fail(agent_id, f"生成 Diff 失败: {exc}")

    if operation == "open_document":
        params = kwargs.get("open_document")
        if not isinstance(params, dict):
            return _fail(agent_id, "操作 open_document 需要提供 `open_document` 参数对象")
        try:
            return await _do_open_document(agent_id, params)
        except Exception as exc:  # noqa: BLE001
            return _fail(agent_id, f"打开文档失败: {exc}")

    if operation == "list_history":
        params = kwargs.get("list_history")
        if not isinstance(params, dict):
            return _fail(agent_id, "操作 list_history 需要提供 `list_history` 参数对象")
        try:
            return _do_list_history(agent_id, params)
        except Exception as exc:  # noqa: BLE001
            return _fail(agent_id, f"读取历史失败: {exc}")

    if operation == "revert":
        params = kwargs.get("revert")
        if not isinstance(params, dict):
            return _fail(agent_id, "操作 revert 需要提供 `revert` 参数对象")
        try:
            return _do_revert(agent_id, params)
        except Exception as exc:  # noqa: BLE001
            return _fail(agent_id, f"回退失败: {exc}")

    return _fail(agent_id, f"不支持的 operation: {operation}")

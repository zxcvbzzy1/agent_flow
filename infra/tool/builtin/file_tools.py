"""文件操作工具集。

为什么需要这些工具：模型有时会把一大坨 shell + markdown + JSON 塞进 bash 工具的单个
"command" 参数里，导致外层工具调用 JSON 解析失败。这里提供一组参数清晰、互相隔离的专用
文件工具，让模型用干净的参数做文件操作，而不是 bash heredoc。

路径处理（重要设计决策）：不限制在工作目录内 —— 与现有 bash 工具同样自由。给定 "path"：
绝对路径直接使用；否则相对于 work_path = agent_dict[agent_id].work_path 解析。永远不会
因为路径在 work_path 之外而拒绝。
"""

from __future__ import annotations

import glob as glob_module
import os
from pathlib import Path

from domain.event import Event
from domain.tool import Tool, Tool_respond
from infra.event_bind import On_bind
from infra.config import factory, agent_dict, bus


# 输出截断阈值（字符）
MAX_OUTPUT_CHARS = 50000
# glob / search 结果上限
GLOB_RESULT_CAP = 1000
SEARCH_DEFAULT_MAX = 200
# search_text 跳过过大文件的阈值（字节）
SEARCH_MAX_FILE_BYTES = 5 * 1024 * 1024


# ---------------------------------------------------------------------------
# 工具定义
# ---------------------------------------------------------------------------

FILE = Tool(
    name="file",
    description=(
        "统一文件操作工具，通过 operation 字段选择操作类型，支持 read / write / append / "
        "edit / apply_patch / list_dir / glob / search_text 等操作。\n"
        "为什么使用此工具而非 bash：避免把大段内容塞进单个 shell 参数造成 JSON 解析失败，"
        "参数清晰隔离。\n"
        "路径处理：绝对路径直接使用；相对路径相对于 agent 工作目录解析；"
        "永远不会因为路径在工作目录之外而拒绝。"
    ),
    field="system",
    input_schema={
        "type": "object",
        "required": ["operation"],
        "properties": {
            "operation": {
                "type": "string",
                "enum": [
                    "read",
                    "write",
                    "append",
                    "edit",
                    "apply_patch",
                    "list_dir",
                    "glob",
                    "search_text",
                ],
                "description": "要执行的文件操作",
            },
            "read": {
                "type": "object",
                "description": "读取文本文件内容的参数。可选 start_line / end_line（1 起始、闭区间）只读取某一行范围。超大输出会截断。路径可为绝对路径或相对于工作目录的相对路径。",
                "properties": {
                    "path": {"type": "string", "description": "文件路径（绝对或相对工作目录）"},
                    "start_line": {"type": "integer", "description": "起始行，1 起始、闭区间（可选）"},
                    "end_line": {"type": "integer", "description": "结束行，1 起始、闭区间（可选）"},
                },
            },
            "write": {
                "type": "object",
                "description": "写入文本文件（utf-8）的参数，会自动创建父目录并覆盖原内容。路径可为绝对路径或相对于工作目录的相对路径。必填：path、content。",
                "properties": {
                    "path": {"type": "string", "description": "文件路径（绝对或相对工作目录）"},
                    "content": {"type": "string", "description": "要写入的完整内容"},
                },
            },
            "append": {
                "type": "object",
                "description": "向文本文件追加内容（utf-8）的参数，文件不存在时会创建（含父目录）。路径可为绝对路径或相对于工作目录的相对路径。必填：path、content。",
                "properties": {
                    "path": {"type": "string", "description": "文件路径（绝对或相对工作目录）"},
                    "content": {"type": "string", "description": "要追加的内容"},
                },
            },
            "edit": {
                "type": "object",
                "description": "在文件中把 old_string 替换为 new_string 的参数。默认只替换一处，若 old_string 出现多次且未设置 replace_all 会报错并要求更唯一的字符串或开启 replace_all。路径可为绝对路径或相对于工作目录的相对路径。必填：path、old_string、new_string。",
                "properties": {
                    "path": {"type": "string", "description": "文件路径（绝对或相对工作目录）"},
                    "old_string": {"type": "string", "description": "要被替换的原文本"},
                    "new_string": {"type": "string", "description": "替换后的新文本"},
                    "replace_all": {
                        "type": "boolean",
                        "description": "是否替换全部匹配，默认 false",
                    },
                },
            },
            "apply_patch": {
                "type": "object",
                "description": "应用一个统一格式（unified diff，git 风格）补丁的参数，可一次包含多个文件。纯 Python 实现，按文件原子应用。支持修改已有文件和创建新文件（源为 /dev/null）。必填：patch。",
                "properties": {
                    "patch": {
                        "type": "string",
                        "description": "统一格式 diff 文本，含 --- a/path、+++ b/path 头和 @@ hunk",
                    },
                },
            },
            "list_dir": {
                "type": "object",
                "description": "列出目录下的条目的参数，包含名称、类型（file/dir）和文件大小。可选 recursive 递归列出。路径可为绝对路径或相对于工作目录的相对路径，默认 '.'。",
                "properties": {
                    "path": {"type": "string", "description": "目录路径，默认 '.'（相对工作目录）"},
                    "recursive": {"type": "boolean", "description": "是否递归，默认 false"},
                },
            },
            "glob": {
                "type": "object",
                "description": "按 glob 模式匹配文件的参数（支持 ** 递归）。可选 path 作为基准目录，默认工作目录。返回匹配到的路径列表。必填：pattern。",
                "properties": {
                    "pattern": {"type": "string", "description": "glob 模式，如 '**/*.py'"},
                    "path": {"type": "string", "description": "基准目录，默认工作目录"},
                },
            },
            "search_text": {
                "type": "object",
                "description": "在文件中按行搜索文本的参数（regex=true 时为正则，否则为字面子串）。可选 glob 过滤文件（如 '**/*.py'），max_results 限制结果数。返回 'relpath:lineno: line' 列表。必填：pattern。",
                "properties": {
                    "pattern": {"type": "string", "description": "搜索模式"},
                    "path": {"type": "string", "description": "基准目录，默认工作目录"},
                    "regex": {"type": "boolean", "description": "是否按正则匹配，默认 false"},
                    "glob": {"type": "string", "description": "文件过滤 glob，如 '**/*.py'（可选）"},
                    "max_results": {"type": "integer", "description": "结果上限，默认 200"},
                },
            },
        },
    },
)


# ---------------------------------------------------------------------------
# 公共辅助
# ---------------------------------------------------------------------------

def _work_path(agent_id: str) -> Path:
    """取得 agent 的工作目录；找不到则退回当前进程工作目录。"""
    agent = agent_dict.get(agent_id) if hasattr(agent_dict, "get") else agent_dict[agent_id]
    if agent is not None and getattr(agent, "work_path", None):
        return Path(agent.work_path).expanduser()
    return Path(os.getcwd())


def _resolve(path_str: str, agent_id: str) -> Path:
    """绝对路径直接用；相对路径相对于 work_path 解析。绝不因越界而拒绝。"""
    p = Path(path_str).expanduser()
    if p.is_absolute():
        return p
    return (_work_path(agent_id) / p)


def _rel_display(p: Path, base: Path) -> str:
    """尽量给出相对 base 的可读路径，失败则用绝对路径。"""
    try:
        return str(p.relative_to(base))
    except ValueError:
        return str(p)


def _looks_binary(sample: bytes) -> bool:
    if b"\x00" in sample:
        return True
    return False


# ---------------------------------------------------------------------------
# 统一格式 diff 应用器（纯 Python）
# ---------------------------------------------------------------------------

class PatchError(Exception):
    """补丁解析或应用失败。"""


def _strip_diff_prefix(path: str) -> str:
    path = path.strip()
    # 去掉时间戳（"\t" 后内容）
    if "\t" in path:
        path = path.split("\t", 1)[0]
    if path in ("/dev/null",):
        return path
    if path.startswith("a/") or path.startswith("b/"):
        return path[2:]
    return path


def _parse_patch(patch_text: str) -> list[dict]:
    """把 unified diff 解析成按文件分组的 section 列表。

    每个 section: {old_path, new_path, is_new, is_delete, hunks}
    每个 hunk:    {old_start, old_count, new_start, new_count, lines}
    line 形如 (' '|'-'|'+', content)。
    """
    lines = patch_text.splitlines()
    sections: list[dict] = []
    i = 0
    n = len(lines)
    current: dict | None = None
    current_hunk: dict | None = None

    while i < n:
        line = lines[i]

        if line.startswith("--- "):
            # 新文件 section 开始
            old_path = _strip_diff_prefix(line[4:])
            if i + 1 >= n or not lines[i + 1].startswith("+++ "):
                raise PatchError(f"'--- ' 行后缺少 '+++ ' 行 (第 {i + 1} 行)")
            new_path = _strip_diff_prefix(lines[i + 1][4:])
            current = {
                "old_path": old_path,
                "new_path": new_path,
                "is_new": old_path == "/dev/null",
                "is_delete": new_path == "/dev/null",
                "hunks": [],
            }
            current_hunk = None
            sections.append(current)
            i += 2
            continue

        if line.startswith("@@"):
            if current is None:
                raise PatchError(f"@@ hunk 出现在任何文件头之前 (第 {i + 1} 行)")
            current_hunk = _parse_hunk_header(line, i + 1)
            current["hunks"].append(current_hunk)
            i += 1
            continue

        if current_hunk is not None and (
            line.startswith(" ")
            or line.startswith("+")
            or line.startswith("-")
            or line == ""
        ):
            # 空行视为上下文空行（内容为空）
            tag = line[0] if line else " "
            content = line[1:] if line else ""
            if tag in (" ", "+", "-"):
                current_hunk["lines"].append((tag, content))
            i += 1
            continue

        if line == r"\ No newline at end of file":
            i += 1
            continue

        # 其它行（diff --git、index、mode、二进制提示等）直接忽略
        i += 1

    return sections


def _parse_hunk_header(header: str, lineno: int) -> dict:
    # 形如 @@ -l,s +l,s @@ optional
    try:
        body = header[2:]
        at_idx = body.index("@@")
        spec = body[:at_idx].strip()
        old_spec, new_spec = spec.split(" ")
        old_start, old_count = _parse_range(old_spec, expect_minus=True)
        new_start, new_count = _parse_range(new_spec, expect_minus=False)
    except Exception as exc:  # noqa: BLE001
        raise PatchError(f"无法解析 hunk 头 '{header}' (第 {lineno} 行): {exc}") from exc
    return {
        "old_start": old_start,
        "old_count": old_count,
        "new_start": new_start,
        "new_count": new_count,
        "lines": [],
    }


def _parse_range(spec: str, expect_minus: bool) -> tuple[int, int]:
    sign = "-" if expect_minus else "+"
    if spec.startswith(sign):
        spec = spec[1:]
    if "," in spec:
        start_s, count_s = spec.split(",", 1)
        return int(start_s), int(count_s)
    return int(spec), 1


def _apply_hunks_to_lines(original: list[str], hunks: list[dict], file_label: str) -> list[str]:
    """把一组 hunk 应用到原文件行列表，返回新的行列表。

    采用基于上下文/删除行匹配的方式：以 hunk 头给出的 old_start 为提示，但允许在附近
    搜索匹配，提高对行号轻微漂移的鲁棒性。任一 hunk 匹配失败抛 PatchError。
    """
    result: list[str] = []
    src_index = 0  # 已消费到原文件的位置（0 起始）

    for h_num, hunk in enumerate(hunks, start=1):
        # hunk 期望匹配的"旧侧"行（上下文 + 删除）
        old_lines = [content for tag, content in hunk["lines"] if tag in (" ", "-")]
        hint = max(hunk["old_start"] - 1, 0)  # 转 0 起始
        match_at = _find_hunk_position(original, old_lines, hint, src_index)
        if match_at is None:
            raise PatchError(
                f"文件 {file_label} 的第 {h_num} 个 hunk（@@ -{hunk['old_start']} 处）"
                f"无法匹配上下文，补丁未应用"
            )

        # 把匹配点之前未处理的原始行原样拷贝
        result.extend(original[src_index:match_at])
        cursor = match_at
        for tag, content in hunk["lines"]:
            if tag == " ":
                cursor += 1
                result.append(content)
            elif tag == "-":
                cursor += 1  # 跳过被删除的原始行
            elif tag == "+":
                result.append(content)
        src_index = cursor

    # 拷贝剩余尾部
    result.extend(original[src_index:])
    return result


def _find_hunk_position(
    original: list[str],
    old_lines: list[str],
    hint: int,
    min_index: int,
) -> int | None:
    """在 original 中找到与 old_lines 完全相等的连续片段起点。

    先试 hint 位置，再以 hint 为中心向外扩展搜索，最后全量扫描。返回 0 起始下标或 None。
    纯上下文（无 old_lines）的 hunk 直接用 hint（但不早于 min_index）。
    """
    if not old_lines:
        return max(hint, min_index)

    n = len(original)
    m = len(old_lines)

    def matches(at: int) -> bool:
        if at < 0 or at + m > n:
            return False
        return original[at:at + m] == old_lines

    # 1) hint 精确命中
    if hint >= min_index and matches(hint):
        return hint

    # 2) 以 hint 为中心向外搜索
    max_offset = n
    for offset in range(1, max_offset + 1):
        for cand in (hint - offset, hint + offset):
            if cand >= min_index and matches(cand):
                return cand
        if hint - offset < min_index and hint + offset > n:
            break

    # 3) 兜底全量扫描（从 min_index 开始）
    for at in range(min_index, n - m + 1):
        if matches(at):
            return at
    return None


def _apply_patch_text(patch_text: str, agent_id: str) -> list[str]:
    """应用补丁，返回被改动文件的展示路径列表。按文件原子写入。"""
    sections = _parse_patch(patch_text)
    if not sections:
        raise PatchError("补丁中未找到任何文件区块（缺少 '--- '/'+++ ' 头）")

    base = _work_path(agent_id)
    # 先全部在内存中计算，全部成功后再统一写盘，保证多文件也不会半途留下脏数据
    pending: list[tuple[Path, str | None, str]] = []  # (target, new_content or None=delete, label)

    for sec in sections:
        is_new = sec["is_new"]
        is_delete = sec["is_delete"]
        target_str = sec["old_path"] if (is_delete and not is_new) else sec["new_path"]
        if target_str == "/dev/null":
            target_str = sec["old_path"]
        target = _resolve(target_str, agent_id)
        label = _rel_display(target, base)

        if is_delete:
            pending.append((target, None, label))
            continue

        if is_new:
            original_lines: list[str] = []
        else:
            if not target.exists():
                # 源不是 /dev/null 但文件不存在：当作空文件起步以增强鲁棒性
                original_lines = []
            else:
                text = target.read_text(encoding="utf-8")
                original_lines = text.splitlines()

        new_lines = _apply_hunks_to_lines(original_lines, sec["hunks"], label)
        new_content = "\n".join(new_lines)
        # 若原文件以换行结尾或为新文件且有内容，补一个结尾换行（贴近常见行为）
        if new_lines:
            new_content += "\n"
        pending.append((target, new_content, label))

    changed: list[str] = []
    for target, content, label in pending:
        if content is None:
            if target.exists():
                target.unlink()
            changed.append(f"{label} (deleted)")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            changed.append(label)
    return changed


# ---------------------------------------------------------------------------
# 工具核心逻辑
# ---------------------------------------------------------------------------

def _do_read_file(path: str, agent_id: str, start_line=None, end_line=None) -> str:
    target = _resolve(path, agent_id)
    if not target.exists():
        raise FileNotFoundError(f"文件不存在: {target}")
    if target.is_dir():
        raise IsADirectoryError(f"路径是目录而非文件: {target}")
    text = target.read_text(encoding="utf-8", errors="replace")

    if start_line is not None or end_line is not None:
        lines = text.splitlines()
        s = (int(start_line) - 1) if start_line is not None else 0
        e = int(end_line) if end_line is not None else len(lines)
        s = max(s, 0)
        e = min(e, len(lines))
        text = "\n".join(lines[s:e])

    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS] + "\n...[truncated]"
    return text


def _do_write_file(path: str, content: str, agent_id: str) -> str:
    target = _resolve(path, agent_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = content if isinstance(content, str) else str(content)
    target.write_text(data, encoding="utf-8")
    n = len(data.encode("utf-8"))
    return f"已写入 {target} ({n} bytes)"


def _do_append_file(path: str, content: str, agent_id: str) -> str:
    target = _resolve(path, agent_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = content if isinstance(content, str) else str(content)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(data)
    n = len(data.encode("utf-8"))
    return f"已追加到 {target} ({n} bytes)"


def _do_edit_file(path: str, old_string: str, new_string: str, agent_id: str, replace_all=False) -> str:
    target = _resolve(path, agent_id)
    if not target.exists():
        raise FileNotFoundError(f"文件不存在: {target}")
    text = target.read_text(encoding="utf-8")
    count = text.count(old_string)
    if count == 0:
        raise ValueError(f"在 {target} 中未找到 old_string")
    if count > 1 and not replace_all:
        raise ValueError(
            f"old_string 在 {target} 中出现 {count} 次。请提供更唯一的 old_string，"
            f"或设置 replace_all=true 替换全部。"
        )
    if replace_all:
        new_text = text.replace(old_string, new_string)
        replaced = count
    else:
        new_text = text.replace(old_string, new_string, 1)
        replaced = 1
    target.write_text(new_text, encoding="utf-8")
    return f"已在 {target} 中替换 {replaced} 处"


def _do_list_dir(path: str, agent_id: str, recursive=False) -> str:
    base = _resolve(path or ".", agent_id)
    if not base.exists():
        raise FileNotFoundError(f"目录不存在: {base}")
    if not base.is_dir():
        raise NotADirectoryError(f"路径不是目录: {base}")

    entries: list[str] = []
    if recursive:
        for root, dirs, files in os.walk(base):
            dirs.sort()
            root_path = Path(root)
            for d in sorted(dirs):
                dp = root_path / d
                entries.append(f"[dir]  {_rel_display(dp, base)}/")
            for f in sorted(files):
                fp = root_path / f
                try:
                    size = fp.stat().st_size
                except OSError:
                    size = -1
                entries.append(f"[file] {_rel_display(fp, base)} ({size} bytes)")
    else:
        for child in sorted(base.iterdir(), key=lambda p: (p.is_file(), p.name)):
            if child.is_dir():
                entries.append(f"[dir]  {child.name}/")
            else:
                try:
                    size = child.stat().st_size
                except OSError:
                    size = -1
                entries.append(f"[file] {child.name} ({size} bytes)")

    if not entries:
        return f"{base} 为空目录"
    header = f"{base} ({len(entries)} 项):"
    return header + "\n" + "\n".join(entries)


def _do_glob(pattern: str, agent_id: str, path=None) -> str:
    base = _resolve(path, agent_id) if path else _work_path(agent_id)
    # 让相对模式相对 base 解析；绝对模式直接用
    if os.path.isabs(pattern):
        full_pattern = pattern
    else:
        full_pattern = str(base / pattern)
    matches = glob_module.glob(full_pattern, recursive=True)
    matches.sort()
    capped = False
    if len(matches) > GLOB_RESULT_CAP:
        matches = matches[:GLOB_RESULT_CAP]
        capped = True
    rel_matches = [_rel_display(Path(m), base) for m in matches]
    if not rel_matches:
        return f"未匹配到任何文件: {pattern}"
    out = f"匹配到 {len(rel_matches)} 项（基准目录 {base}）:\n" + "\n".join(rel_matches)
    if capped:
        out += f"\n...[已截断，最多显示 {GLOB_RESULT_CAP} 项]"
    return out


def _do_search_text(
    pattern: str,
    agent_id: str,
    path=None,
    regex=False,
    glob=None,
    max_results=SEARCH_DEFAULT_MAX,
) -> str:
    import re as _re

    base = _resolve(path, agent_id) if path else _work_path(agent_id)
    if not base.exists():
        raise FileNotFoundError(f"搜索目录不存在: {base}")

    max_results = int(max_results) if max_results else SEARCH_DEFAULT_MAX

    # 收集候选文件
    if glob:
        if os.path.isabs(glob):
            glob_pattern = glob
        else:
            glob_pattern = str(base / glob)
        candidates = [Path(p) for p in glob_module.glob(glob_pattern, recursive=True)]
        candidates = [p for p in candidates if p.is_file()]
    elif base.is_file():
        candidates = [base]
    else:
        candidates = [p for p in base.rglob("*") if p.is_file()]

    candidates.sort()

    matcher = None
    if regex:
        matcher = _re.compile(pattern)

    results: list[str] = []
    capped = False
    for fp in candidates:
        if len(results) >= max_results:
            capped = True
            break
        try:
            if fp.stat().st_size > SEARCH_MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        try:
            with fp.open("rb") as fh:
                sample = fh.read(4096)
            if _looks_binary(sample):
                continue
            text = fp.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeError):
            continue

        rel = _rel_display(fp, base)
        for lineno, line in enumerate(text.splitlines(), start=1):
            hit = bool(matcher.search(line)) if regex else (pattern in line)
            if hit:
                results.append(f"{rel}:{lineno}: {line}")
                if len(results) >= max_results:
                    capped = True
                    break

    if not results:
        return f"未找到匹配 '{pattern}'（基准目录 {base}）"
    out = f"找到 {len(results)} 处匹配:\n" + "\n".join(results)
    if capped:
        out += f"\n...[已截断，最多 {max_results} 条]"
    return out


# ---------------------------------------------------------------------------
# 注册与事件处理
# ---------------------------------------------------------------------------

on_tool = On_bind()
factory._build_and_register_list([FILE], bus)


def _succeed(agent_id: str, name: str, respond: str) -> Event:
    # 兜底成非空成功提示，保证 succeeded 负载非空，避免模型看不到结果而重复调用。
    if not (isinstance(respond, str) and respond.strip()):
        respond = f"{name} 执行成功"
    return factory.tool(name).succeeded(
        Tool_respond(agent_id=agent_id, name=name, success=True, respond=respond)
    )


def _fail(agent_id: str, name: str, message: str) -> Event:
    return factory.tool(name).failed(
        Tool_respond(agent_id=agent_id, name=name, success=False, respond=message)
    )


@on_tool.on(factory.tool("file").called())
def on_file(**kwargs) -> Event:
    agent_id = kwargs.get("agent_id", "")
    name = "file"
    operation = str(kwargs.get("operation", "")).strip().lower()

    if operation == "read":
        params = kwargs.get("read")
        if not isinstance(params, dict):
            return _fail(agent_id, name, "操作 read 需要提供 `read` 参数对象")
        try:
            respond = _do_read_file(
                params["path"],
                agent_id,
                start_line=params.get("start_line"),
                end_line=params.get("end_line"),
            )
            return _succeed(agent_id, name, respond)
        except Exception as exc:  # noqa: BLE001
            return _fail(agent_id, name, f"读取失败: {exc}")

    elif operation == "write":
        params = kwargs.get("write")
        if not isinstance(params, dict):
            return _fail(agent_id, name, "操作 write 需要提供 `write` 参数对象")
        try:
            respond = _do_write_file(params["path"], params["content"], agent_id)
            return _succeed(agent_id, name, respond)
        except Exception as exc:  # noqa: BLE001
            return _fail(agent_id, name, f"写入失败: {exc}")

    elif operation == "append":
        params = kwargs.get("append")
        if not isinstance(params, dict):
            return _fail(agent_id, name, "操作 append 需要提供 `append` 参数对象")
        try:
            respond = _do_append_file(params["path"], params["content"], agent_id)
            return _succeed(agent_id, name, respond)
        except Exception as exc:  # noqa: BLE001
            return _fail(agent_id, name, f"追加失败: {exc}")

    elif operation == "edit":
        params = kwargs.get("edit")
        if not isinstance(params, dict):
            return _fail(agent_id, name, "操作 edit 需要提供 `edit` 参数对象")
        try:
            respond = _do_edit_file(
                params["path"],
                params["old_string"],
                params["new_string"],
                agent_id,
                replace_all=bool(params.get("replace_all", False)),
            )
            return _succeed(agent_id, name, respond)
        except Exception as exc:  # noqa: BLE001
            return _fail(agent_id, name, f"编辑失败: {exc}")

    elif operation == "apply_patch":
        params = kwargs.get("apply_patch")
        if not isinstance(params, dict):
            return _fail(agent_id, name, "操作 apply_patch 需要提供 `apply_patch` 参数对象")
        try:
            changed = _apply_patch_text(params["patch"], agent_id)
            respond = "已应用补丁，改动文件:\n" + "\n".join(changed)
            return _succeed(agent_id, name, respond)
        except Exception as exc:  # noqa: BLE001
            return _fail(agent_id, name, f"应用补丁失败: {exc}")

    elif operation == "list_dir":
        params = kwargs.get("list_dir")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            params = {}
        try:
            respond = _do_list_dir(
                params.get("path", "."),
                agent_id,
                recursive=bool(params.get("recursive", False)),
            )
            return _succeed(agent_id, name, respond)
        except Exception as exc:  # noqa: BLE001
            return _fail(agent_id, name, f"列目录失败: {exc}")

    elif operation == "glob":
        params = kwargs.get("glob")
        if not isinstance(params, dict):
            return _fail(agent_id, name, "操作 glob 需要提供 `glob` 参数对象")
        try:
            respond = _do_glob(params["pattern"], agent_id, path=params.get("path"))
            return _succeed(agent_id, name, respond)
        except Exception as exc:  # noqa: BLE001
            return _fail(agent_id, name, f"glob 失败: {exc}")

    elif operation == "search_text":
        params = kwargs.get("search_text")
        if not isinstance(params, dict):
            return _fail(agent_id, name, "操作 search_text 需要提供 `search_text` 参数对象")
        try:
            respond = _do_search_text(
                params["pattern"],
                agent_id,
                path=params.get("path"),
                regex=bool(params.get("regex", False)),
                glob=params.get("glob"),
                max_results=params.get("max_results", SEARCH_DEFAULT_MAX),
            )
            return _succeed(agent_id, name, respond)
        except Exception as exc:  # noqa: BLE001
            return _fail(agent_id, name, f"搜索失败: {exc}")

    else:
        return _fail(agent_id, name, f"不支持的 operation: {operation}")

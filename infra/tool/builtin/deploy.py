from __future__ import annotations

from pathlib import Path

from domain.event import Event
from domain.runtime_hooks import get_run_context_provider
from domain.tool import Tool, Tool_respond
from infra.config import agent_dict, bus, factory
from infra.deploy.manager import build_deploy_event_payload, deployment_manager
from infra.event_bind import On_bind


DEPLOY = Tool(
    name="deploy",
    field="system",
    description=(
        "后台部署：当用户说'部署'时调用。把一组文件作为静态网页托管，"
        "或用一条常驻命令(如 uvicorn/flask/node)启动一个后端，分配 0.0.0.0 端口"
        "并返回可预览地址；前端会出现可一键关闭端口的部署卡片。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["static", "command"],
                "description": "static=托管静态文件; command=运行常驻启动命令",
            },
            "title": {"type": "string", "description": "部署卡片标题"},
            "source_dir": {
                "type": "string",
                "description": (
                    "要托管/运行的**已存在目录**，相对你的工作目录或绝对路径。"
                    "当源码已用文件工具写到工作目录时填这里(如 '.' 或 'dist')，"
                    "static 会托管它、command 会在其中运行启动命令。不填则按 files 落地。"
                ),
            },
            "files": {
                "type": "object",
                "description": (
                    "path->content 映射。content 为字符串=新建该文件并落地；"
                    "content 为 null=仅引用工作目录里已存在的文件(据此自动推断 source_dir，不会重写)。"
                    "static 内联内容时必填；command 落地少量启动文件时可选。"
                ),
            },
            "entry": {
                "type": "string",
                "description": "static 入口文件，默认 index.html",
            },
            "command": {
                "type": "string",
                "description": (
                    "command 模式启动命令，用 $PORT 占位监听端口，"
                    "例: uvicorn app:app --host 0.0.0.0 --port $PORT"
                ),
            },
            "env": {"type": "object", "description": "附加环境变量"},
        },
        "required": ["kind", "title"],
    },
)


on_tool = On_bind()
factory._build_and_register_list([DEPLOY], bus)


def _agent_workdir(agent_id: str) -> str | None:
    """取 agent 的工作目录(work_path)作为 source_dir 解析与越界判断的根。"""
    if not agent_id:
        return None
    try:
        agent = agent_dict.get(agent_id) if hasattr(agent_dict, "get") else agent_dict[agent_id]
    except Exception:
        agent = None
    work_path = getattr(agent, "work_path", None) if agent is not None else None
    if not work_path:
        return None
    return str(Path(str(work_path)).expanduser())


def _is_within(child: str, parent: str) -> bool:
    try:
        Path(child).resolve().relative_to(Path(parent).resolve())
        return True
    except (ValueError, OSError):
        return False


def _derive_source_dir(files, base_dir: str | None) -> str | None:
    """从“引用已存在文件”的 files 项推断要托管/运行的目录(绝对路径)。

    只看 content 为 None 或写了绝对路径的项——这类是 agent 已在工作目录里建好的
    现成文件。取它们的公共父目录；无法唯一判定则返回 None(退回 files 落地模式)。
    """
    if not isinstance(files, dict):
        return None
    base = Path(base_dir).expanduser() if base_dir else None
    parents: set[Path] = set()
    for raw, content in files.items():
        if not isinstance(raw, str) or not raw.strip():
            continue
        p = Path(raw.strip()).expanduser()
        # 相对路径 + 有内容 = 要新建的文件，不参与目录推断
        if content is not None and not p.is_absolute():
            continue
        if p.is_absolute():
            abs_p = p
        elif base is not None:
            abs_p = base / p
        else:
            continue
        try:
            parents.add(abs_p.parent.resolve())
        except OSError:
            continue
    if len(parents) == 1:
        return str(next(iter(parents)))
    return None


@on_tool.on(factory.tool("deploy").called())
async def deploy(**kwargs) -> Event:
    agent_id = str(kwargs.get("agent_id", "")).strip()
    run_id = str(kwargs.get("run_id", "")).strip()
    if not run_id and agent_id:
        provider = get_run_context_provider()
        if provider is not None:
            run_id = provider.run_id_for_agent(agent_id)

    files = kwargs.get("files")
    base_dir = _agent_workdir(agent_id)

    # source_dir：显式优先，否则从“引用已存在文件”的 files 项推断；统一解析成绝对路径。
    explicit = str(kwargs.get("source_dir") or "").strip()
    if explicit:
        sp = Path(explicit).expanduser()
        if sp.is_absolute():
            source_dir = str(sp)
        elif base_dir:
            source_dir = str(Path(base_dir).expanduser() / sp)
        else:
            source_dir = None
    else:
        source_dir = _derive_source_dir(files, base_dir)

    # work_path 未知或不是 source_dir 的祖先时，放宽越界根为该目录本身，
    # 与文件工具“不限制工作目录”的策略保持一致，避免误报越界。
    if source_dir and (not base_dir or not _is_within(source_dir, base_dir)):
        base_dir = source_dir

    try:
        deployment = await deployment_manager.create(
            kind=str(kwargs.get("kind", "")).strip().lower(),
            title=str(kwargs.get("title", "")).strip(),
            files=files,
            command=kwargs.get("command"),
            entry=kwargs.get("entry"),
            env=kwargs.get("env"),
            agent_id=agent_id,
            run_id=run_id,
            source_dir=source_dir,
            base_dir=base_dir,
        )
    except Exception as exc:
        tool_respond = Tool_respond(
            agent_id=agent_id,
            name="deploy",
            success=False,
            respond=f"部署失败: {exc}",
        )
        return factory.tool("deploy").failed(tool_respond)

    if deployment.status == "running":
        payload = build_deploy_event_payload(deployment, agent_id, run_id)
        await bus.publish(Event("artifacts.deploy", payload=payload))
        respond = (
            f"部署成功: {deployment.url} "
            f"(deployment_id={deployment.deployment_id}, kind={deployment.kind})。"
            f"可在卡片上一键关闭。"
        )
        tool_respond = Tool_respond(
            agent_id=agent_id,
            name="deploy",
            success=True,
            respond=respond,
        )
        return factory.tool("deploy").succeeded(tool_respond)

    # 失败：也发一个 status="failed" 的 artifacts.deploy 便于前端显示
    error = deployment.error or "未知错误"
    payload = build_deploy_event_payload(deployment, agent_id, run_id)
    try:
        await bus.publish(Event("artifacts.deploy", payload=payload))
    except Exception:
        pass
    tool_respond = Tool_respond(
        agent_id=agent_id,
        name="deploy",
        success=False,
        respond=f"部署失败: {error}",
    )
    return factory.tool("deploy").failed(tool_respond)

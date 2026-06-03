from __future__ import annotations

from domain.event import Event
from domain.runtime_hooks import get_run_context_provider
from domain.tool import Tool, Tool_respond
from infra.config import bus, factory
from infra.deploy.manager import build_deploy_event_payload, deployment_manager
from infra.event_bind import On_bind


DEPLOY = Tool(
    name="deploy",
    field="system",
    description=(
        "后台部署：当用户说'部署'时调用。把一组文件作为静态网页托管，"
        "或用一条常驻命令(如 uvicorn/flask/node)启动一个后端，分配 127.0.0.1 端口"
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
            "files": {
                "type": "object",
                "description": (
                    "path->content 映射，写入部署工作目录。"
                    "static 必填；command 可选(落地后端源码)"
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
                    "例: uvicorn app:app --host 127.0.0.1 --port $PORT"
                ),
            },
            "env": {"type": "object", "description": "附加环境变量"},
        },
        "required": ["kind", "title"],
    },
)


on_tool = On_bind()
factory._build_and_register_list([DEPLOY], bus)


@on_tool.on(factory.tool("deploy").called())
async def deploy(**kwargs) -> Event:
    agent_id = str(kwargs.get("agent_id", "")).strip()
    run_id = str(kwargs.get("run_id", "")).strip()
    if not run_id and agent_id:
        provider = get_run_context_provider()
        if provider is not None:
            run_id = provider.run_id_for_agent(agent_id)

    try:
        deployment = await deployment_manager.create(
            kind=str(kwargs.get("kind", "")).strip().lower(),
            title=str(kwargs.get("title", "")).strip(),
            files=kwargs.get("files"),
            command=kwargs.get("command"),
            entry=kwargs.get("entry"),
            env=kwargs.get("env"),
            agent_id=agent_id,
            run_id=run_id,
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

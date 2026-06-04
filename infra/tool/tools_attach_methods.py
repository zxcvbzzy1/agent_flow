import inspect

from domain.event import EventBusReturn, Event
from domain.run_context import current_agent
from domain.runtime_hooks import get_tool_event_observer
from infra.event_bind import On_bind
from infra.config import factory, agent_dict, bus
from infra.tool.common_func import HumanCollaborationAuditor


# factory = StaticToolEventFactory(prefix="infra") #编写时方便
on_tool = On_bind()
human_auditor = HumanCollaborationAuditor(factory, bus)


# 中间件

# logging middleware
@on_tool.use()
async def logging_middleware(event: Event, call_next):
    event_payload = event.unpack()
    agent_id = event_payload.get("agent_id", "Unknown")
    print(f"[LOG] {agent_id} 事件触发: {event.name}")
    # print(f"{event.name}负载为:\n {event.payload}")

    try:
        result = await call_next()
        print(f"[LOG]  {agent_id}  事件完成: {event.name}")
        return result

    except Exception as e:
        print(f"[ERROR]  {agent_id}  事件异常: {event.name} -> {e}")
        raise   # ⚠️ 一定要 rethrow


# frontend_sse_bridge middleware
@on_tool.use()
async def frontend_sse_bridge_middleware(event: Event, call_next):
    try:
        observer = get_tool_event_observer()
        if observer is not None:
            result = observer.on_tool_event(event)
            if inspect.isawaitable(result):
                await result
    except Exception as exc:
        print(f"[SSE-BRIDGE] 工具事件镜像失败: {event.name} -> {exc}")
    return await call_next()


# human_collaboration middleware
@on_tool.use()
async def human_collaboration_middleware(event: Event, call_next):
    return await human_auditor.handle(event, call_next)


# 成功/失败返回事件
@on_tool.on_pattern("*.succeeded")
async def on_tool_success(**kwargs):
    agent_id = kwargs.get("agent_id")
    event = kwargs.get("_event")
    # per-run 隔离：优先用 contextvar 里的“本实例”，回退到全局 agent_dict[agent_id]（向后兼容）。
    agent = current_agent.get() or agent_dict.get(agent_id)
    if agent is None:
        # 无法定位 agent（contextvar 未设且全局映射缺失）：优雅跳过，避免 AttributeError。
        print(f"[tool] 无法定位 agent，跳过 succeeded 回调: agent_id={agent_id}")
        return EventBusReturn(agent_id=agent_id, src_object=event, results=kwargs.get("respond"), success=True)
    await agent.on_tool_call(
        tool_name=kwargs.get("name"),
        success=True,
        respond=kwargs.get("respond"),
    )
    return EventBusReturn(agent_id=agent_id,src_object=event, results=kwargs.get("respond"), success=True)

@on_tool.on_pattern("*.failed")
async def on_tool_fail(**kwargs):  # event.playload为Tool_respond类，kwargs为Tool_respond类解构可字典调用
    agent_id = kwargs.get("agent_id")
    # per-run 隔离：优先用 contextvar 里的“本实例”，回退到全局 agent_dict[agent_id]（向后兼容）。
    agent = current_agent.get() or agent_dict.get(agent_id)
    event = kwargs.get("_event")
    if agent is None:
        print(f"[tool] 无法定位 agent，跳过 failed 回调: agent_id={agent_id}")
        return EventBusReturn(agent_id=agent_id, src_object=event, results="工具调用失败事件处理完成", success=False)
    await agent.on_tool_call(
        tool_name=kwargs.get("name"),
        success=False,
        respond=kwargs.get("respond"),
    )
    return EventBusReturn(agent_id=agent_id, src_object=event, results="工具调用失败事件处理完成", success=False)
# @on_tool.on(factory.tool("query_tool_respond").succeeded({}))
# async def on_query_tool_respond_tool_successed(**kwargs):  
#     agent_id = kwargs.get("agent_id")
#     agent:AgentBase = agent_dict.get(agent_id)
#     full_respond = kwargs.get("respond")
#     def callBack() -> str:
#         summary = f"{full_respond}" 
#         return summary           
#     await agent.on_tool_call(
#         tool_name=kwargs.get("name"),
#         success=True,
#         respond=kwargs.get("respond"),
#         callBack=callBack
#     )

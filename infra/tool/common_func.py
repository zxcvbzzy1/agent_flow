import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from domain.event import Event, EventBusPort
from domain.runtime_hooks import get_human_approval_provider, get_run_context_provider
from domain.tool import Tool_respond


async def ask_human_input(prompt: str) -> str:
    """异步包装 input，避免在 async 事件链路里直接阻塞事件循环。"""
    return await asyncio.to_thread(input, prompt)


@dataclass
class HumanAuditResult:
    approved: bool
    reason: str


@dataclass
class HumanInputContext:
    tool_name: str
    called_event_name: str
    arguments: dict[str, Any]


_human_input_context: ContextVar[HumanInputContext | None] = ContextVar(
    "human_input_context",
    default=None,
)


def parse_human_results(human_results: Any) -> HumanAuditResult:
    if not human_results:
        return HumanAuditResult(False, "未找到人机协作确认处理器")

    results = human_results if isinstance(human_results, list) else [human_results]

    for result in results:
        if isinstance(result, Exception):
            return HumanAuditResult(False, f"人机协作确认异常: {result}")
        if isinstance(result, bool):
            reason = "用户已确认" if result else "用户拒绝执行"
            return HumanAuditResult(result, reason)
        if isinstance(result, Event):
            data = result.unpack()
            approved = bool(data.get("approved", False))
            reason = data.get("reason") or ("用户已确认" if approved else "用户拒绝执行")
            return HumanAuditResult(approved, reason)
        if isinstance(result, dict):
            approved = bool(result.get("approved", False))
            reason = result.get("reason") or ("用户已确认" if approved else "用户拒绝执行")
            return HumanAuditResult(approved, reason)

    return HumanAuditResult(False, "人机协作确认处理器返回格式无效")


async def ask_web_human_input(prompt: str) -> str:
    """通过网页端等待人类输入；没有网页上下文时回落到终端 input。"""
    context = _human_input_context.get()
    if context is None:
        return await ask_human_input(prompt)

    approval_provider = get_human_approval_provider()
    run_context_provider = get_run_context_provider()
    agent_id = context.arguments.get("agent_id", "")
    run_id = ""
    if run_context_provider is not None and agent_id:
        run_id = run_context_provider.run_id_for_agent(agent_id)
    if approval_provider is None or not run_id:
        return await ask_human_input(prompt)

    result = await approval_provider.request_approval(
        run_id=run_id,
        agent_id=agent_id,
        tool_name=context.tool_name,
        called_event_name=context.called_event_name,
        arguments=context.arguments,
    )
    approved = bool(result.get("approved"))
    reason = result.get("reason", "")
    if approved:
        return "yes"
    return f"no {reason}".strip()


class HumanApprovalService:
    """人类确认服务；替换 input_func 即可切换确认策略。"""

    def __init__(
        self,
        input_func=ask_web_human_input,
    ) -> None:
        self.input_func = input_func
        self._lock = asyncio.Lock()

    async def input(self, prompt: str) -> str:
        async with self._lock:
            return await self.input_func(prompt)

# 外部调用的确认服务实例
human_approval_service = HumanApprovalService()



# 中间件逻辑，发送人机协作事件并等待确认结果；如果未通过审核则发送工具执行失败事件
class HumanCollaborationAuditor:
    """工具执行前的人机协作审核器。"""

    def __init__(self, factory, bus: EventBusPort | None) -> None:
        self.factory = factory
        self.bus = bus

    async def handle(self, event: Event, call_next):
        """执行完整的人机协作审核 middleware 流程。"""
        audit_result = await self.audit(event)
        if audit_result.approved:
            return await call_next()

        return await self.reject(event, audit_result.reason)

    async def audit(self, event: Event) -> HumanAuditResult:
        arguments = event.unpack()
        if not event.name.endswith(".called"):
            return HumanAuditResult(True, "无需人机协作审核")

        try:
            spec = self.factory.get_spec(event.name)
        except KeyError:
            return HumanAuditResult(True, "未找到工具描述，跳过人机协作审核")
        tool_name = spec.tool_name

        if not spec.tool_metadata.get("require_human_confirm"):
            return HumanAuditResult(True, "工具未要求人机协作审核")
        if self.bus is None:
            return HumanAuditResult(False, "工具要求人机协作审核，但 EventBus 未注入")

        human_event_name = f"human.{tool_name}"
        print(f"[HUMAN] 正在启动人工审核 {human_event_name}")
        token = _human_input_context.set(
            HumanInputContext(
                tool_name=tool_name,
                called_event_name=event.name,
                arguments=arguments,
            )
        )
        try:
            human_results = await self.bus.publish_one(
                Event(
                    name=human_event_name,
                    payload={
                        "tool_name": tool_name,
                        "called_event_name": event.name,
                        "arguments": arguments,
                    },
                )
            )
        finally:
            _human_input_context.reset(token)

        return parse_human_results(human_results)

    async def reject(self, event: Event, reason: str):
        arguments = event.unpack()
        try:
            spec = self.factory.get_spec(event.name)
        except KeyError:
            return Event(
                name=f"{event.name}.human_rejected",
                payload={"approved": False, "reason": reason},
            )
        tool_name = spec.tool_name
        respond = Tool_respond(
            agent_id=arguments.get("agent_id", ""),
            name=tool_name,
            success=False,
            respond=reason,
        )
        return await self.factory.tool(tool_name).emit_failed(respond)

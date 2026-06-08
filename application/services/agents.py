from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from domain.agent.plan.planAgent import PlanAgent
from domain.agent_base import AgentBase
from domain.context.context import ContextEngine
from infra.LLM.LLM_infra import LLM_Client
from infra.db.mongodb import DocumentStore

from application.services.contexts import ContextService
from application.services.events import EventStreamService
from application.services.llm_streaming import StreamingObservableLLMClient


class APIExecutorAgent(AgentBase):
    def __init__(self, *args, role_prompt: str = "", work_path: str='/Users/zxcvbzzy1/Desktop/项目/agent_full_stack/agent_flow/temp', **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.work_path = work_path
        self._role_prompt = role_prompt + f"""
## 工作目录
        
当前工作目录为：{self.work_path},请在此目录里创建文件

## 目标
根据用户需求，自主决定调用组合合适的工具完成任务。

## 输出格式
用 JSON 严格按以下格式回复：
{{
  "think": "你的思考过程",
  "tool_calls": [
    {{
      "tool_name": "工具名",
      "arguments": {{"参数名": "参数值"}},
      "reasoning": "为什么调用这个工具"
    }}
  ],
  "is_finished": false
}}

## 任务完成时输出
{{
  "think": "...",
  "tool_calls": [],
  "is_finished": true,
  "finish_reason": "完成原因",
  "final": "最终结果(string)"
}}     
"""
        

    def _build_agent_prompt(self) -> str:
        if self._role_prompt:
            return self._role_prompt
        return super()._build_agent_prompt()


class AgentFactoryService:
    PROTECTED_AGENT_IDS = {"default_planner", "default_executor"}

    def __init__(
        self,
        store: DocumentStore,
        context_service: ContextService,
        llm_client: LLM_Client,
        events: EventStreamService | None = None,
        external_executor_builder: Callable[[dict[str, Any]], AgentBase] | None = None,
    ) -> None:
        self._store = store
        self._contexts = context_service
        self._llm = llm_client
        self._events = events
        self._external_executor_builder = external_executor_builder
        self._agents: dict[str, AgentBase | PlanAgent] = {}
        self.ensure_default_agents()

    def ensure_default_agents(self) -> None:
        if self._store.find_one("agents", {"agent_id": "default_planner"}) is None:
            self.create_agent(
                agent_id="default_planner",
                name="默认任务编排者",
                agent_type="planner",
                context_id="default_planner",
            )
        if self._store.find_one("agents", {"agent_id": "default_executor"}) is None:
            self.create_agent(
                agent_id="default_executor",
                name="默认执行者",
                agent_type="executor",
                context_id="default_executor",
                role_prompt="""
你是一个通用 ReACT 执行者，请根据上下文选择工具完成任务。

## 输出格式
用 JSON 严格按以下格式回复：
{
  "think": "你的思考",
  "tool_calls": [
    {
      "tool_name": "工具名",
      "arguments": {"参数名": "参数值"},
      "reasoning": "为什么调用这个工具"
    }
  ],
  "is_finished": false
}

## 任务完成时输出
{
  "think": "...",
  "tool_calls": [],
  "is_finished": true,
  "finish_reason": "完成原因",
  "final": "最终结果"
}
""",
            )

    def create_agent(
        self,
        name: str,
        agent_type: str,
        context_id: str,
        role_prompt: str = "",
        agent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if agent_type not in {"planner", "executor"}:
            raise ValueError("agent_type 必须是 planner 或 executor")
        self._contexts.get_engine(context_id)
        agent_id = agent_id or str(uuid.uuid4())
        record = {
            "agent_id": agent_id,
            "name": name,
            "agent_type": agent_type,
            "context_id": context_id,
            "role_prompt": role_prompt,
            "metadata": metadata or {},
        }
        self._store.update_one("agents", {"agent_id": agent_id}, record, upsert=True)
        self._agents[agent_id] = self._build_agent(record)
        return record

    def create_agent_from_instance(
        self,
        agent: AgentBase | PlanAgent,
        agent_type: str | None = None,
        context_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        agent_type = agent_type or ("planner" if isinstance(agent, PlanAgent) else "executor")
        if agent_type not in {"planner", "executor"}:
            raise ValueError("agent_type 必须是 planner 或 executor")

        context_id = context_id or f"{agent.id}_context"
        context_kind = "planner" if agent_type == "planner" else "executor"
        self._contexts.create_context_from_engine(
            kind=context_kind,
            name=f"{agent.name} Context",
            engine=agent.context_engine,
            context_id=context_id,
        )

        agent_metadata = {
            "description": getattr(agent, "description", ""),
            "imported_agent_class": type(agent).__name__,
            **(metadata or {}),
        }
        record = {
            "agent_id": agent.id,
            "name": agent.name,
            "agent_type": agent_type,
            "context_id": context_id,
            "role_prompt": rp if isinstance(rp := getattr(agent, "_role_prompt", ""), str) else "",
            "metadata": agent_metadata,
        }
        self._store.update_one("agents", {"agent_id": agent.id}, record, upsert=True)
        agent._llm = self._build_llm(record)
        self._agents[agent.id] = agent
        return record

    def list_agents(self) -> list[dict[str, Any]]:
        return self._store.find_many("agents", sort=[("created_at", 1)])

    def get_agent_record(self, agent_id: str) -> dict[str, Any] | None:
        return self._store.find_one("agents", {"agent_id": agent_id})

    def get_agent(self, agent_id: str) -> AgentBase | PlanAgent:
        if agent_id not in self._agents:
            record = self.get_agent_record(agent_id)
            if record is None:
                raise KeyError(f"Agent 不存在: {agent_id}")
            self._agents[agent_id] = self._build_agent(record)
        return self._agents[agent_id]

    def build_run_agent(self, agent_id: str) -> AgentBase | PlanAgent:
        """为单次 run 构造一个全新的 agent 实例（全新 engine + memory + states），不缓存。

        per-run 隔离的入口：planner 与每个 executor 在 run 内都用独立实例，互不共享 states/memory，
        从而修复 executor 旧状态泄漏、planner 跨会话残留、以及同一 agent_id 并发 run 的相互污染。
        与 get_agent 一致地在 agent 不存在时抛 KeyError。
        注意：emit 的 agent.id 仍是逻辑 id（record["agent_id"]），前端按逻辑 id 关联 trace 不受影响。
        """
        record = self.get_agent_record(agent_id)
        if record is None:
            raise KeyError(f"Agent 不存在: {agent_id}")
        engine = self._contexts.build_engine(record["context_id"])
        return self._construct_agent(record, engine)

    def delete_agent(self, agent_id: str) -> dict[str, Any]:
        if agent_id in self.PROTECTED_AGENT_IDS:
            raise ValueError("默认 Agent 不允许删除")
        record = self.get_agent_record(agent_id)
        if record is None:
            raise KeyError(f"Agent 不存在: {agent_id}")

        planner_runs = self._store.find_many("runs", {"planner_agent_id": agent_id})
        executor_runs = [
            run for run in self._store.find_many("runs")
            if agent_id in (run.get("executor_agent_ids") or [])
        ]
        run_ids = {
            run.get("run_id")
            for run in [*planner_runs, *executor_runs]
            if run.get("run_id")
        }

        stats = {
            "agents": self._store.delete_one("agents", {"agent_id": agent_id}),
            "runs": 0,
            "events": 0,
            "contexts": 0,
        }
        self._agents.pop(agent_id, None)

        for run_id in run_ids:
            stats["runs"] += self._store.delete_many("runs", {"run_id": run_id})
            stats["events"] += self._store.delete_many("events", {"run_id": run_id})

        # 删除该 Agent 独占的上下文（创建时按模版克隆，与 Agent 1:1 绑定），避免删 Agent 后 contexts 残留。
        # 先删 Agent 记录与其 runs 再删上下文：delete_context 自带保护/引用校验——默认上下文、
        # 仍被其它 Agent 或 run 引用的上下文会抛错，这里吞掉以不阻断 Agent 删除（共享/受保护上下文不应随单个 Agent 删除）。
        context_id = record.get("context_id", "")
        if context_id:
            try:
                ctx_result = self._contexts.delete_context(context_id)
                stats["contexts"] += int((ctx_result.get("stats") or {}).get("contexts", 0))
            except (KeyError, ValueError):
                pass

        return {"deleted": True, "agent_id": agent_id, "stats": stats}

    def _build_agent(self, record: dict[str, Any]) -> AgentBase | PlanAgent:
        # 缓存路径（get_agent / create_agent 用）：复用 ContextService 缓存的 engine。
        context = self._contexts.get_engine(record["context_id"])
        return self._construct_agent(record, context)

    def _construct_agent(
        self,
        record: dict[str, Any],
        engine: ContextEngine,
    ) -> AgentBase | PlanAgent:
        """根据 record + 给定 engine 构造 agent 实例。

        缓存路径（_build_agent）传入 ContextService 缓存的共享 engine；
        per-run 路径（build_run_agent）传入 build_engine 产出的全新独立 engine。
        agent.id 始终为逻辑 id（record["agent_id"]）。
        """
        llm = self._build_llm(record)
        if record.get("agent_type") == "planner":
            agent = PlanAgent(
                id=record["agent_id"],
                name=record["name"],
                llm=llm,
                context=engine,
            )
        elif (record.get("metadata") or {}).get("agent_kind") in {"claude_code", "codex"}:
            if self._external_executor_builder is None:
                raise ValueError("缺少第三方 executor builder")
            agent = self._external_executor_builder(record)
        else:
            agent = APIExecutorAgent(
                id=record["agent_id"],
                name=record["name"],
                llm=llm,
                context=engine,
                role_prompt=record.get("role_prompt", ""),
                work_path=(record.get("metadata") or {}).get("workdir", ""),
            )

        metadata = record.get("metadata") or {}
        description = metadata.get("description")
        if description:
            agent.inject_attribute(description=description)
        return agent

    def _build_llm(self, record: dict[str, Any]):
        if self._events is None:
            return self._llm
        return StreamingObservableLLMClient(
            self._llm,
            self._events,
            agent_id=record["agent_id"],
            agent_name=record["name"],
            agent_type=record.get("agent_type", "executor"),
        )

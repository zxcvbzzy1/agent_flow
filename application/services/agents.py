from __future__ import annotations

import uuid
from typing import Any

from domain.agent.plan.planAgent import PlanAgent
from domain.agent_base import AgentBase
from infra.LLM.LLM_infra import LLM_Client
from infra.db.mongodb import DocumentStore

from application.services.contexts import ContextService
from application.services.events import EventStreamService
from application.services.llm_streaming import StreamingObservableLLMClient


class APIExecutorAgent(AgentBase):
    def __init__(self, *args, role_prompt: str = "", **kwargs) -> None:
        self._role_prompt = role_prompt
        super().__init__(*args, **kwargs)

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
    ) -> None:
        self._store = store
        self._contexts = context_service
        self._llm = llm_client
        self._events = events
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
        }
        self._agents.pop(agent_id, None)

        for run_id in run_ids:
            stats["runs"] += self._store.delete_many("runs", {"run_id": run_id})
            stats["events"] += self._store.delete_many("events", {"run_id": run_id})

        return {"deleted": True, "agent_id": agent_id, "stats": stats}

    def _build_agent(self, record: dict[str, Any]) -> AgentBase | PlanAgent:
        context = self._contexts.get_engine(record["context_id"])
        llm = self._build_llm(record)
        if record.get("agent_type") == "planner":
            agent = PlanAgent(
                id=record["agent_id"],
                name=record["name"],
                llm=llm,
                context=context,
            )
        else:
            agent = APIExecutorAgent(
                id=record["agent_id"],
                name=record["name"],
                llm=llm,
                context=context,
                role_prompt=record.get("role_prompt", ""),
            )

        description = (record.get("metadata") or {}).get("description")
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

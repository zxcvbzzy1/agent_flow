from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from domain.agent.plan.orchestrator import OrchestratorState, PlanOrchestrator
from domain.agent.plan.planAgent import PlanAgent
from domain.agent_base import AgentBase
from domain.context.context import ContextEngine
from domain.event import Event, EventBusPort
from infra.db.mongodb import DocumentStore

from application.services.agents import AgentFactoryService
from application.services.contexts import ContextService
from application.events.bridge import FrontendEventBridge
from application.events.schemas import step_failed_payload
from application.services.events import EventStreamService


class RecordingEventBus(EventBusPort):
    def __init__(self, run_id: str, streams: EventStreamService) -> None:
        self._run_id = run_id
        self._streams = streams

    async def publish_one(self, event: Event) -> Any:
        events = await self.publish(event)
        return events[0] if events else None

    async def publish(self, event: Event) -> list[Any]:
        self._streams.publish(self._run_id, event.name, event.unpack())
        return [event]

    def subscribe(self, event_name: str, handler) -> None:
        return None


class ObservablePlanOrchestrator(PlanOrchestrator):
    def __init__(self, *args, run_id: str, streams: EventStreamService, **kwargs) -> None:
        self._run_id = run_id
        self._streams = streams
        self._reported_failed_steps: set[str] = set()
        super().__init__(*args, **kwargs)

    def _dispatch(self, action: dict) -> None:
        super()._dispatch(action)
        event_name = action.get("event_dispatch", "workflow.event")
        payload = self._normalize_payload(action.get("playload", {}))
        self._streams.publish(self._run_id, event_name, payload)
        self._publish_failed_steps(payload)

    def _normalize_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = {}
        for key, value in payload.items():
            if hasattr(value, "to_dict"):
                normalized[key] = value.to_dict()
            else:
                normalized[key] = value
        return normalized

    async def _run_plan_step(self, step, plan) -> None:
        await super()._run_plan_step(step, plan)
        if step.status == "failed":
            self._streams.publish(
                self._run_id,
                "agent.failed",
                step_failed_payload(
                    run_id=self._run_id,
                    executor_id=step.executor_id,
                    step=step.to_dict(),
                ),
            )

    def _publish_failed_steps(self, payload: dict[str, Any]) -> None:
        steps = payload.get("plan", {}).get("steps", [])
        for step in steps:
            if step.get("status") != "failed":
                continue
            step_id = step.get("step_id", "")
            if not step_id or step_id in self._reported_failed_steps:
                continue
            self._reported_failed_steps.add(step_id)
            self._streams.publish(
                self._run_id,
                "plan.step.failed",
                step_failed_payload(
                    run_id=self._run_id,
                    executor_id=step.get("executor_id", ""),
                    step=step,
                ),
            )


class RunOrchestrationService:
    def __init__(
        self,
        store: DocumentStore,
        agent_service: AgentFactoryService,
        context_service: ContextService,
        streams: EventStreamService,
        frontend_bridge: FrontendEventBridge,
    ) -> None:
        self._store = store
        self._agents = agent_service
        self._contexts = context_service
        self._streams = streams
        self._frontend_bridge = frontend_bridge
        self._tasks: dict[str, asyncio.Task] = {}

    def create_run(
        self,
        prompt: str,
        planner_agent_id: str = "default_planner",
        executor_agent_ids: list[str] | None = None,
        context_id: str = "default_step",
        max_replan_rounds: int = 3,
        conversation_id: str | None = None,
        message_id: str | None = None,
        auto_start: bool = True,
    ) -> dict[str, Any]:
        run_id = str(uuid.uuid4())
        executor_agent_ids = executor_agent_ids or ["default_executor"]
        record = {
            "run_id": run_id,
            "prompt": prompt,
            "planner_agent_id": planner_agent_id,
            "executor_agent_ids": executor_agent_ids,
            "context_id": context_id,
            "max_replan_rounds": max_replan_rounds,
            "conversation_id": conversation_id,
            "message_id": message_id,
            "status": "pending",
            "plan": {},
            "final": "",
            "started_at": None,
            "finished_at": None,
        }
        self._store.insert_one("runs", record)
        if auto_start:
            self._tasks[run_id] = asyncio.create_task(self._execute_run(record))
        return record

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return self._store.find_one("runs", {"run_id": run_id})

    async def _execute_run(self, record: dict[str, Any]) -> None:
        run_id = record["run_id"]
        self._store.update_one(
            "runs",
            {"run_id": run_id},
            {"status": "running", "started_at": time.time()},
        )
        try:
            planner = self._agents.get_agent(record["planner_agent_id"])
            if not isinstance(planner, PlanAgent):
                raise TypeError("planner_agent_id 必须指向 planner agent")
            self._frontend_bridge.register_agent_run(planner.id, run_id)

            executors: dict[str, AgentBase] = {}
            for executor_id in record["executor_agent_ids"]:
                executor = self._agents.get_agent(executor_id)
                if not isinstance(executor, AgentBase):
                    raise TypeError(f"executor_agent_id 不是执行型 agent: {executor_id}")
                self._frontend_bridge.register_agent_run(executor.id, run_id)
                executors[executor_id] = executor

            step_context = self._contexts.get_engine(record["context_id"])
            if not isinstance(step_context, ContextEngine):
                raise TypeError("context_id 必须指向可用上下文")

            orchestrator = ObservablePlanOrchestrator(
                planner=planner,
                executors=executors,
                step_context_engine=step_context,
                event_bus=RecordingEventBus(run_id, self._streams),
                state=OrchestratorState(),
                max_replan_rounds=record["max_replan_rounds"],
                run_id=run_id,
                streams=self._streams,
            )
            await orchestrator.start(record["prompt"])
            self._store.update_one(
                "runs",
                {"run_id": run_id},
                {
                    "status": "finished",
                    "plan": orchestrator.state.plan,
                    "final": orchestrator.state.final,
                    "finish_reason": orchestrator.state.finish_reason,
                    "finished_at": time.time(),
                },
            )
            self._complete_conversation_queue(
                record=record,
                status="done",
                final=orchestrator.state.final,
            )
        except Exception as exc:
            self._store.update_one(
                "runs",
                {"run_id": run_id},
                {"status": "failed", "error": str(exc), "finished_at": time.time()},
            )
            self._complete_conversation_queue(record=record, status="failed", final=str(exc))
            self._streams.publish(run_id, "workflow.failed", {"error": str(exc)})
        finally:
            self._frontend_bridge.unregister_agent_run(record.get("planner_agent_id", ""), run_id)
            for executor_id in record.get("executor_agent_ids", []):
                self._frontend_bridge.unregister_agent_run(executor_id, run_id)

    def _complete_conversation_queue(self, record: dict[str, Any], status: str, final: str) -> None:
        conversation_id = record.get("conversation_id")
        message_id = record.get("message_id")
        if not conversation_id or not message_id:
            return

        queue_items = self._store.find_many(
            "message_queue",
            {"conversation_id": conversation_id},
            sort=[("created_at", 1)],
        )
        for item in reversed(queue_items):
            if item.get("message_id") == message_id:
                self._store.update_one(
                    "message_queue",
                    {"queue_id": item["queue_id"]},
                    {
                        "status": status,
                        "run_id": record["run_id"],
                        "processed_at": time.time(),
                    },
                )
                break

        if status == "done" and final:
            self._store.insert_one(
                "messages",
                {
                    "message_id": str(uuid.uuid4()),
                    "conversation_id": conversation_id,
                    "role": "assistant",
                    "content": final,
                    "metadata": {"source": "run"},
                    "run_id": record["run_id"],
                },
            )

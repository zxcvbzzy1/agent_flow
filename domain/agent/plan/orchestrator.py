from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Literal

from domain.agent.plan.planAgent import PlanAgent
from domain.agent_base import AgentBase
from domain.context.context import ContextEngine
from domain.event import Event, EventBusPort
from domain.state import Plan, PlanStep, _dict_to_plan



@dataclass
class OrchestratorState:
    prompt: str = ""
    executors: dict[str, dict] = field(default_factory=dict)
    plan: dict = field(default_factory=dict)
    current_step: dict | None = None
    final: str = ""
    is_finished: bool = False
    finish_reason: str = ""

    def to_context_dict(self) -> dict:
        state = {
            "prompt": self.prompt,
            "executors": self.executors,
            "plan": self.plan,
            "final": self.final,
            "is_finished": self.is_finished,
            "finish_reason": self.finish_reason,
        }
        if self.current_step is not None:
            state["current_step"] = self.current_step
        return state

    def get(self, key: str, default=None):
        """兼容 provider / notebook 中少量 dict 风格读取。"""
        return self.to_context_dict().get(key, default)

event_dispatch = Literal[
    "workflow.started",
    "plan.generated",
    "wave.completed",
    "plan.replanned",
    "plan.step.observed",
    "workflow.finished"
]



class PlanOrchestrator:
    """Plan workflow 的依赖调度、执行和 observation 编排器。"""

    def __init__(
        self,
        planner: PlanAgent,
        executors: dict[str, AgentBase],
        state: OrchestratorState,
        step_context_engine: ContextEngine,
        event_bus: EventBusPort | None = None,
        max_replan_rounds: int = 5,
    ) -> None:
        self.planner = planner
        self.executors = executors
        self.state = state
        self.step_context_engine = step_context_engine
        self.event_bus = event_bus
        self.max_replan_rounds = max_replan_rounds
        self._replan_rounds = 0

    async def start(self, prompt: str) -> None:
        self._dispatch({
            "event_dispatch": "workflow.started",
            "playload": {"prompt": prompt},
        })

        plan = await self.planner.generate_plan(
            self.state.to_context_dict(),
            list(self.executors.keys()),
        )
        self._dispatch({
            "event_dispatch": "plan.generated",
            "playload": {"plan": plan},
        })

        await self.execute(plan)

        final = await self.planner.summarize_result(self.state.to_context_dict())
        self._dispatch({
            "event_dispatch": "workflow.finished",
            "playload": {
                "final": final,
                "finish_reason": "计划编排执行完成",
            },
        })

    async def execute(self, plan: Plan) -> None:
        while True:
            self._fail_steps_with_unknown_executor(plan)
            self._dispatch({
                "event_dispatch": "wave.completed",
                "playload": {"plan": plan},
            })

            pending_steps = [step for step in plan.steps if step.status == "pending"]
            if not pending_steps:
                return

            ready_steps = [
                step for step in pending_steps
                if self._dependencies_done(step, plan)
            ]

            if not ready_steps:
                self._fail_blocked_steps(pending_steps, plan)
                self._dispatch({
                    "event_dispatch": "wave.completed",
                    "playload": {"plan": plan},
                })
                return

            await asyncio.gather(*[
                self._run_plan_step(step, plan)
                for step in ready_steps
            ])

            self._dispatch({
                "event_dispatch": "wave.completed",
                "playload": {"plan": plan},
            })
            await self._publish_event(
                "plan.wave.completed",
                {
                    "planner_id": self.planner.id,
                    "completed_step_ids": [step.step_id for step in ready_steps],
                    "plan": plan.to_dict(),
                },
            )

            should_finish = await self._replan_after_observation(plan)
            if should_finish:
                return
            
    # 内部函数
    def _fail_steps_with_unknown_executor(self, plan: Plan) -> None:
        for step in plan.steps:
            if step.status == "pending" and step.executor_id not in self.executors:
                step.status = "failed"
                step.status_reason = f"未知 executor_id: {step.executor_id}"
                step.result_observation = step.status_reason

    def _dispatch(self, action: dict) -> None:
        action_type = action.get("event_dispatch")
        playload = action.get("playload", {})
        

        if action_type == "workflow.started":
            self.state.prompt = playload.get("prompt", "")
            self.state.executors = self._executor_status()
            self.state.current_step = None
            return

        if action_type in {"plan.generated", "wave.completed", "plan.replanned"}:
            plan = playload["plan"]
            self.state.plan = plan.to_dict()
            self.state.executors = self._executor_status()
            self.state.current_step = None
            return

        if action_type == "workflow.finished":
            self.state.executors = self._executor_status()
            self.state.final = playload.get("final", "")
            self.state.is_finished = True
            self.state.finish_reason = playload.get("finish_reason", "")
            self.state.current_step = None

    def _executor_status(self) -> dict[str, dict]:
        status = {}
        for executor_id, executor in self.executors.items():
            state = getattr(executor, "states", {})
            status[executor_id] = {
                "name": getattr(executor, "name", executor_id),
                "is_finished": state.get("is_finished", False),
                "final": state.get("final", ""),
                "finish_reason": state.get("finish_reason", ""),
            }
        return status

    def _dependencies_done(self, step: PlanStep, plan: Plan) -> bool:
        done_step_ids = {
            item.step_id for item in plan.steps
            if item.status == "done"
        }
        return all(dependency in done_step_ids for dependency in step.depends_on)

    async def _run_plan_step(self, step: PlanStep, plan: Plan) -> None:
        executor = self.executors[step.executor_id]
        step.status = "in_progress"

        step_prompt = self.step_context_engine.build(
            {
            **self.state.to_context_dict(),
            "plan": plan.to_dict(),
            "executors": self._executor_status(),
            "current_step": step.to_dict(),
        }
        )
        try:
            executor.states["is_finished"] = False
            executor.states["finish_reason"] = ""
            executor.states["final"] = ""
            await executor.start(step_prompt)
            if executor.states.get("is_finished", True):
                step.status = "done"
                step.status_reason = executor.states.get("finish_reason", "执行完成")
            else:
                step.status = "failed"
                step.status_reason = executor.states.get("finish_reason", "执行失败")
        except Exception as exc:
            step.status = "failed"
            step.status_reason = f"执行异常: {exc}"
        finally:
            step.result_observation = self._build_step_observation(step, executor)
            await self._publish_event(
                "plan.step.observed",
                {
                    "planner_id": self.planner.id,
                    "step": step.to_dict(),
                },
            )

    def _build_step_observation(self, step: PlanStep, executor: AgentBase) -> str:
        state = getattr(executor, "states", {})
        return "\n".join([
            f"executor_id={step.executor_id}",
            f"status={step.status}",
            f"finish_reason={state.get('finish_reason', '')}",
            f"last_tool_ok={state.get('last_tool_ok', True)}",
            f"final={state.get('final', '')}",
        ])


    async def _publish_event(self, name: str, payload: dict) -> None:
        if self.event_bus is None:
            return
        await self.event_bus.publish(Event(name=name, payload=payload))



    async def _replan_after_observation(self, plan: Plan) -> bool:
        if self._replan_rounds >= self.max_replan_rounds:
            return False

        self._replan_rounds += 1
        decision = await self.planner.replan_after_observation(
            plan,
            self.state.to_context_dict(),
        )
        action = decision.get("action", "continue")

        if action == "finish":
            for step in plan.steps:
                if step.status == "pending":
                    step.status = "skipped"
                    step.status_reason = decision.get("reason", "replan 决定提前结束")
                    step.result_observation = step.status_reason
            self._dispatch({
                "event_dispatch": "plan.replanned",
                "playload": {"plan": plan},
            })
            return True

        if action == "replan":
            self._apply_replan_steps(plan, decision.get("steps", []))
            self._dispatch({
                "event_dispatch": "plan.replanned",
                "playload": {"plan": plan},
            })

        return False



    def _fail_blocked_steps(self, pending_steps: list[PlanStep], plan: Plan) -> None:
        existing_step_ids = {step.step_id for step in plan.steps}
        done_step_ids = {
            step.step_id for step in plan.steps
            if step.status == "done"
        }
        failed_step_ids = {
            step.step_id for step in plan.steps
            if step.status == "failed"
        }

        for step in pending_steps:
            missing = [dep for dep in step.depends_on if dep not in existing_step_ids]
            failed = [dep for dep in step.depends_on if dep in failed_step_ids]
            blocked = [dep for dep in step.depends_on if dep not in done_step_ids]
            step.status = "failed"
            if missing:
                step.status_reason = f"依赖不存在: {missing}"
            elif failed:
                step.status_reason = f"依赖已失败: {failed}"
            else:
                step.status_reason = f"依赖阻塞或循环依赖: {blocked}"
            step.result_observation = step.status_reason


    def _apply_replan_steps(self, plan: Plan, raw_steps: list[dict]) -> None:
        history_steps = [
            step for step in plan.steps
            if step.status != "pending"
        ]
        replacement = _dict_to_plan({"steps": raw_steps})
        plan.steps = history_steps + replacement.steps

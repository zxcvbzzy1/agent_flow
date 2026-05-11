from __future__ import annotations

import asyncio
import json
import re
from collections import defaultdict
from typing import Any

from domain.agent_base import AgentBase
from domain.agent.plan.providers import PlanStepPromptProvider
from domain.context.context import ContextEngine
from domain.state import Plan


class PlanAgent(AgentBase):
    """规划和编排多个 ReACT executor 的 Agent。"""

    def __init__(
        self,
        id: str,
        name: str,
        llm,
        context: ContextEngine,
        executors: dict[str, AgentBase],
    ) -> None:
        super().__init__(id, name, llm, context)
        self.executors = executors
        self.step_context_engine = ContextEngine(
            providers=[PlanStepPromptProvider()],
            memory=context.get_memory(),
        )
        self.states["executors"] = self._executor_status()

    async def start(self, prompt: str) -> None:
        self.states["prompt"] = prompt
        self.states["executors"] = self._executor_status()

        plan = await self._generate_plan()
        self.states["plan"] = plan.to_dict()

        await self._execute_plan(plan)
        self.states["executors"] = self._executor_status()

        self.states["final"] = await self._summarize_result()
        self.states["is_finished"] = True
        self.states["finish_reason"] = "计划编排执行完成"

    async def _generate_plan(self) -> Plan:
        context = self.context_engine.build(self.states)
        messages = [
            {"role": "system", "content": self._build_plan_prompt()},
            {"role": "user", "content": context},
        ]
        raw = await self._llm.chat(messages)
        data = self._parse_json(raw)
        raw_steps = data.get("steps", [])

        plan = Plan()
        plan.add_steps(raw_steps)
        return plan

    async def _execute_plan(self, plan: Plan) -> None:
        grouped_steps: dict[str, list] = defaultdict(list)

        for step in plan.steps:
            if step.executor_id not in self.executors:
                step.status = "failed"
                step.note = f"未知 executor_id: {step.executor_id}"
                continue
            grouped_steps[step.executor_id].append(step)

        self.states["plan"] = plan.to_dict()
        await asyncio.gather(
            *[
                self._run_executor_steps(executor_id, steps, plan)
                for executor_id, steps in grouped_steps.items()
            ]
        )

    async def _run_executor_steps(self, executor_id: str, steps: list, plan: Plan) -> None:
        executor = self.executors[executor_id]

        for step in steps:
            step.status = "in_progress"
            self.states["current_step"] = step.to_dict()
            self.states["plan"] = plan.to_dict()

            step_prompt = self.step_context_engine.build(self.states)
            try:
                executor.states["is_finished"] = False
                executor.states["finish_reason"] = ""
                await executor.start(step_prompt)
                if executor.states.get("last_tool_ok", True):
                    step.status = "done"
                    step.note = executor.states.get("finish_reason", "执行完成")
                else:
                    step.status = "failed"
                    step.note = executor.states.get("finish_reason", "执行失败")
            except Exception as exc:
                step.status = "failed"
                step.note = f"执行异常: {exc}"
            finally:
                self.states["current_step"] = {}
                self.states["plan"] = plan.to_dict()
                self.states["executors"] = self._executor_status()

    async def _summarize_result(self) -> str:
        context = self.context_engine.build(self.states)
        messages = [
            {"role": "system", "content": self._build_summary_prompt()},
            {"role": "user", "content": context},
        ]
        return await self._llm.chat(messages)

    def _executor_status(self) -> dict[str, dict[str, Any]]:
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

    def _parse_json(self, raw: str) -> dict[str, Any]:
        text = raw.strip()
        match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        if match:
            text = match.group(1).strip()
        return json.loads(text)

    def _build_plan_prompt(self) -> str:
        executor_ids = ", ".join(self.executors.keys())
        return f"""
你是一个任务编排型 PlanAgent。

你的职责：
- 根据用户需求生成结构化计划
- 为每个步骤选择一个 executor_id
- 只输出 JSON，不要输出其他文本

可用 executor_id：
{executor_ids}

严格输出格式：
{{
  "steps": [
    {{
      "step_id": "1",
      "title": "步骤标题",
      "detail": "步骤说明",
      "executor_id": "可用 executor_id"
    }}
  ]
}}
"""

    def _build_summary_prompt(self) -> str:
        return """
你是一个任务编排型 PlanAgent。
请根据计划状态、执行者状态和工具反馈，总结本次任务的最终结果。
直接输出最终总结文本。
"""



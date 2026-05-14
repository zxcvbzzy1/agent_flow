from __future__ import annotations

import json
import re
from typing import Any

from domain.agent_base import AgentBase
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
    ) -> None:
        super().__init__(id, name, llm, context)

    async def start(self, prompt: str) -> None:
        self.states["prompt"] = prompt
        await self.run()

    async def generate_plan(self, state: dict, executor_ids: list[str]) -> Plan:
        context = self.context_engine.build(state)
        messages = [
            {"role": "system", "content": self._build_plan_prompt(executor_ids)},
            {"role": "user", "content": context},
        ]
        raw = await self._llm.chat(messages)
        data = self._parse_json(raw)
        raw_steps = data.get("steps", [])

        plan = Plan()
        plan.add_steps(raw_steps)
        return plan

    async def replan_after_observation(self, plan: Plan, state: dict) -> dict[str, Any]:
        context = self.context_engine.build(state)
        messages = [
            {"role": "system", "content": self._build_replan_prompt()},
            {"role": "user", "content": context},
        ]
        raw = await self._llm.chat(messages)
        try:
            return self._parse_json(raw)
        except json.JSONDecodeError:
            return {"action": "continue", "reason": "replan JSON 解析失败"}

    async def summarize_result(self, state: dict) -> str:
        context = self.context_engine.build(state)
        messages = [
            {"role": "system", "content": self._build_summary_prompt()},
            {"role": "user", "content": context},
        ]
        return await self._llm.chat(messages)

    def _parse_json(self, raw: str) -> dict[str, Any]:
        text = raw.strip()
        match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        if match:
            text = match.group(1).strip()
        return json.loads(text)

    def _build_plan_prompt(self, executor_ids: list[str]) -> str:
        executor_names = ", ".join(executor_ids)
        return f"""
你是一个任务编排型 PlanAgent。

你的职责：
- 根据用户需求生成结构化计划
- 为每个步骤选择一个 executor_id
- 只输出 JSON，不要输出其他文本

可用 executor_id：
{executor_names}

严格输出格式：
{{
  "steps": [
    {{
      "step_id": "1",
      "title": "步骤标题",
      "instruction": "步骤执行说明",
      "executor_id": "可用 executor_id",
      "depends_on": []
    }}
  ]
}}
"""

    def _build_replan_prompt(self) -> str:
        return """
你是一个任务编排型 PlanAgent。
请根据当前计划状态、执行观察和执行者状态判断下一步。

只输出 JSON，不要输出其他文本。

可选 action：
- continue：计划无需调整，继续执行
- replan：追加或替换未开始的 pending 步骤
- finish：提前结束剩余 pending 步骤

输出格式：
{
  "action": "continue",
  "reason": "无需调整"
}

replan 时输出：
{
  "action": "replan",
  "reason": "需要补充步骤",
  "steps": [
    {
      "step_id": "E",
      "title": "补充步骤",
      "instruction": "步骤执行说明",
      "executor_id": "可用 executor_id",
      "depends_on": ["依赖的step_id"] 
    }
  ]
}
"""

    def _build_summary_prompt(self) -> str:
        return """
你是一个任务编排型 PlanAgent。
请根据计划状态、执行者状态和工具反馈，总结本次任务的最终结果。
直接输出最终总结文本。
"""

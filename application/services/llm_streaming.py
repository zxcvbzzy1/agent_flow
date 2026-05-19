from __future__ import annotations

import json
import re
from typing import Any

from domain.runtime_hooks import get_run_context_provider
from application.services.events import EventStreamService


class StreamingObservableLLMClient:
    """Mirrors LLM streaming chunks into the run SSE stream."""

    def __init__(
        self,
        base_llm,
        streams: EventStreamService,
        *,
        agent_id: str,
        agent_name: str,
        agent_type: str,
    ) -> None:
        self._base = base_llm
        self._streams = streams
        self.agent_id = agent_id
        self.agent_name = agent_name
        self.agent_type = agent_type
        self.model = getattr(base_llm, "model", "")
        self.max_tokens = getattr(base_llm, "max_tokens", None)

    async def chat(self, messages: list) -> str:
        call_role = self._detect_call_role(messages)
        run_id = self._run_id()
        sequence = 0
        full_response = ""

        if run_id:
            self._publish(
                run_id,
                "llm.started",
                {
                    "call_role": call_role,
                    "model": self.model,
                },
            )

        async for delta in self._base.stream_chat(messages, model=self.model):
            print(delta, end="", flush=True)
            full_response += delta
            sequence += 1
            if run_id:
                self._publish(
                    run_id,
                    "llm.delta",
                    {
                        "call_role": call_role,
                        "delta": delta,
                        "sequence": sequence,
                    },
                )

        if run_id:
            self._publish(
                run_id,
                "llm.completed",
                {
                    "call_role": call_role,
                    "content": full_response,
                    "token_chunks": sequence,
                },
            )
            self._publish_structured(run_id, call_role, full_response)

        return full_response

    def _run_id(self) -> str:
        provider = get_run_context_provider()
        if provider is None:
            return ""
        return provider.run_id_for_agent(self.agent_id)

    def _publish(self, run_id: str, name: str, payload: dict[str, Any]) -> None:
        self._streams.publish(
            run_id,
            name,
            {
                "run_id": run_id,
                "agent_id": self.agent_id,
                "agent_name": self.agent_name,
                "agent_type": self.agent_type,
                **payload,
            },
        )

    def _publish_structured(self, run_id: str, call_role: str, raw: str) -> None:
        if self.agent_type == "planner":
            self._publish_planner_structured(run_id, call_role, raw)
            return
        self._publish_executor_structured(run_id, raw)

    def _publish_executor_structured(self, run_id: str, raw: str) -> None:
        data = self._parse_json(raw)
        if data is None:
            self._publish(run_id, "agent.think", {"think": raw})
            return

        think = data.get("think", "")
        if think:
            self._publish(run_id, "agent.think", {"think": think})

        for tool_call in data.get("tool_calls", []) or []:
            reasoning = tool_call.get("reasoning", "")
            if not reasoning:
                continue
            self._publish(
                run_id,
                "agent.tool.reasoning",
                {
                    "tool_name": tool_call.get("tool_name", ""),
                    "reasoning": reasoning,
                    "arguments": tool_call.get("arguments", {}),
                },
            )

        if data.get("is_finished"):
            self._publish(
                run_id,
                "agent.final",
                {
                    "finish_reason": data.get("finish_reason", ""),
                    "final": data.get("final", "") or data.get("finish_reason", ""),
                },
            )

    def _publish_planner_structured(self, run_id: str, call_role: str, raw: str) -> None:
        data = self._parse_json(raw)
        if call_role == "plan_summary":
            self._publish(run_id, "planner.final", {"planner_id": self.agent_id, "final": raw})
            return
        if data is None:
            return

        if call_role == "plan_replan":
            self._publish(
                run_id,
                "planner.replan.reasoning",
                {
                    "planner_id": self.agent_id,
                    "action": data.get("action", ""),
                    "reason": data.get("reason", ""),
                    "steps": data.get("steps", []),
                    "raw": data,
                },
            )
            return

        if "steps" in data:
            self._publish(
                run_id,
                "planner.plan.generated",
                {
                    "planner_id": self.agent_id,
                    "steps": data.get("steps", []),
                    "raw": data,
                },
            )

    def _detect_call_role(self, messages: list) -> str:
        if self.agent_type == "executor":
            return "executor_think"

        system = ""
        if messages:
            system = str(messages[0].get("content", ""))
        lowered = system.lower()

        if "replan" in lowered or "重新规划" in system or "判断下一步" in system:
            return "plan_replan"
        if "总结本次任务" in system or "最终结果" in system:
            return "plan_summary"
        if "生成结构化计划" in system or '"steps"' in system or "steps" in lowered:
            return "plan_generate"
        return "unknown"

    def _parse_json(self, raw: str) -> dict[str, Any] | None:
        text = raw.strip()
        match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        if match:
            text = match.group(1).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None

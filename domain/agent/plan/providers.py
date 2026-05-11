from __future__ import annotations

from domain.context.providers import ContextProvider


class ExecutorStatusProvider(ContextProvider):
    """当前可用执行者状态，供 PlanAgent 规划和编排。"""
    name = "executors"

    def get(self, state: dict) -> list[str]:
        executors = state.get("executors", {})
        if not executors:
            return ["## 执行者状态\n当前没有可用执行者。"]

        parts = ["## 执行者状态"]
        for executor_id, executor in executors.items():
            if isinstance(executor, dict):
                name = executor.get("name", executor_id)
                is_finished = executor.get("is_finished", False)
                recent_result = (
                    executor.get("final")
                    or executor.get("finish_reason")
                    or executor.get("last_result")
                    or ""
                )
            else:
                name = getattr(executor, "name", executor_id)
                executor_state = getattr(executor, "states", {})
                is_finished = executor_state.get("is_finished", False)
                recent_result = (
                    executor_state.get("final")
                    or executor_state.get("finish_reason")
                    or ""
                )

            status = "finished" if is_finished else "available"
            line = f"- executor_id: {executor_id}; name: {name}; status: {status}"
            if recent_result:
                line += f"; recent_result: {recent_result}"
            parts.append(line)

        return ["\n".join(parts)]


class PlanStepPromptProvider(ContextProvider):
    """构造发送给 ReACT executor 的单步任务上下文。"""
    name = "plan_step_prompt"

    def get(self, state: dict) -> list[str]:
        step = state.get("current_step")
        if not step:
            return []

        if isinstance(step, dict):
            step_id = step.get("step_id", "")
            title = step.get("title", "")
            detail = step.get("detail", "")
        else:
            step_id = getattr(step, "step_id", "")
            title = getattr(step, "title", "")
            detail = getattr(step, "detail", "")

        return [
            "\n".join([
                f"原始用户需求：\n{state.get('prompt', '')}",
                "",
                "当前计划步骤：",
                f"- step_id: {step_id}",
                f"- title: {title}",
                f"- detail: {detail}",
                "",
                "请只完成当前步骤，并在完成时输出 is_finished=true。",
            ])
        ]


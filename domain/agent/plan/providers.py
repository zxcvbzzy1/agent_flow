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
            depends_on = step.get("depends_on", [])
        else:
            step_id = getattr(step, "step_id", "")
            title = getattr(step, "title", "")
            detail = getattr(step, "detail", "")
            depends_on = getattr(step, "depends_on", [])

        dependency_observations = []
        for plan_step in state.get("plan", {}).get("steps", []):
            if plan_step.get("step_id") in depends_on and plan_step.get("observation"):
                dependency_observations.append(
                    f"- [{plan_step.get('step_id')}] {plan_step.get('title')}: "
                    f"{plan_step.get('observation')}"
                )

        return [
            "\n".join([
                f"原始用户需求：\n{state.get('prompt', '')}",
                "",
                "当前计划步骤：",
                f"- step_id: {step_id}",
                f"- title: {title}",
                f"- detail: {detail}",
                f"- depends_on: {depends_on}",
                "",
                "依赖步骤观察结果：",
                "\n".join(dependency_observations) if dependency_observations else "无",
                "",
                "请严格只完成当前步骤，禁止完成其他步骤，并在完成时输出 is_finished=true。",
            ])
        ]


class PlanObservationProvider(ContextProvider):
    """输出已完成或失败计划步骤的 observation，供 replan 和 summary 使用。"""
    name = "plan_observations"

    def get(self, state: dict) -> list[str]:
        steps = state.get("plan", {}).get("steps", [])
        observed_steps = [
            step for step in steps
            if step.get("status") in {"done", "failed", "skipped"}
        ]
        if not observed_steps:
            return []

        parts = ["## 计划执行观察"]
        for step in observed_steps:
            observation = step.get("observation") or step.get("note") or ""
            parts.append(
                f"- [{step.get('step_id')}] {step.get('title')} "
                f"status={step.get('status')} executor_id={step.get('executor_id')}\n"
                f"  observation: {observation}"
            )
        return ["\n".join(parts)]

from __future__ import annotations
from typing import Any,Literal
from enum import Enum, auto
from dataclasses import dataclass, field
import uuid
import time

PlanStatus = Literal["pending", "in_progress", "done", "failed", "skipped"]

@dataclass
class PlanStep:
    # 步骤唯一标识，用于依赖引用、replan 定位和状态追踪。
    step_id: str
    # 步骤短标题，用于人类阅读和 prompt 摘要。
    title: str
    # 执行前的任务说明，即这个步骤要让 executor 做什么。
    instruction: str = ""
    # 负责执行该步骤的 executor id。
    executor_id: str = ""
    # 依赖的 step_id 列表；依赖步骤 done 后本步骤才可调度。
    depends_on: list[str] = field(default_factory=list)
    # 执行后的完整观察结果，供后续步骤、replan 和 summary 使用。
    result_observation: str = ""
    # 调度状态：pending/in_progress/done/failed/skipped。
    status: PlanStatus = "pending"
    # 当前状态的简短原因，例如失败原因、跳过原因、执行完成原因。
    status_reason: str = ""
    # 步骤创建时间。
    created_at: float = field(default_factory=time.time)
    # 步骤最后更新时间。
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "step_id":    self.step_id,
            "title":      self.title,
            "instruction": self.instruction,
            "executor_id": self.executor_id,
            "depends_on":  self.depends_on,
            "result_observation": self.result_observation,
            "status":     self.status,
            "status_reason": self.status_reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class Plan:
    steps:     list[PlanStep] = field(default_factory=list)
    finished:  bool           = False
    summary:   str            = ""
    created_at: float         = field(default_factory=time.time)

    # ── 操作 ──────────────────────────────────────────────────────

    def add_steps(self, step_dicts: list[dict]) -> None:
        for s in step_dicts:
            self.steps.append(PlanStep(
                step_id=s.get("step_id", str(uuid.uuid4())[:8]),
                title=s.get("title", ""),
                instruction=s.get("instruction", s.get("detail", "")),
                executor_id=s.get("executor_id", ""),
                depends_on=s.get("depends_on", []),
                result_observation=s.get("result_observation", s.get("observation", "")),
            ))

    def update_step(self, step_id: str, title: str | None = None,
                    instruction: str | None = None, status: PlanStatus | None = None,
                    status_reason: str | None = None,
                    executor_id: str | None = None,
                    depends_on: list[str] | None = None,
                    result_observation: str | None = None,
                    **legacy_fields) -> PlanStep | None:
        if instruction is None:
            instruction = legacy_fields.get("detail")
        if status_reason is None:
            status_reason = legacy_fields.get("note")
        if result_observation is None:
            result_observation = legacy_fields.get("observation")

        for step in self.steps:
            if step.step_id == step_id:
                if title  is not None: step.title  = title
                if instruction is not None: step.instruction = instruction
                if status is not None: step.status = status
                if status_reason is not None: step.status_reason = status_reason
                if executor_id is not None: step.executor_id = executor_id
                if depends_on is not None: step.depends_on = depends_on
                if result_observation is not None: step.result_observation = result_observation
                step.updated_at = time.time()
                return step
        return None

    def get_steps(self, filter_status: str = "") -> list[PlanStep]:
        if not filter_status:
            return self.steps
        return [s for s in self.steps if s.status == filter_status]

    def finish(self, summary: str) -> None:
        self.finished = True
        self.summary  = summary

    def to_dict(self) -> dict:
        return {
            "steps":    [s.to_dict() for s in self.steps],
            "finished": self.finished,
            "summary":  self.summary,
        }

    def next_pending(self) -> PlanStep | None:
        return next((s for s in self.steps if s.status == "pending"), None)

class Agent_state():

    def __init__(self, genre: str = "通用", session_id: str = "") -> None:
        self._data: dict[str, Any] = {
            # 内容
            "prompt":         "",
            "genre":         genre,
            "final":"",
            "think":"",
            "history":       [],
            # tool 
            "tool_history":  [],   # list[str] 执行过的工具名
            "last_tool_ok": True,
            "tool_retry":     0,   
            # 控制
            "plan":{},
            "current_state":"",
            "session_id":    session_id,
            "retry":         0,
            "is_finished":   False,
            
        }
        self._version:int = 0

    def get_state(self):
        return self._data



def _dict_to_plan(plan_dict: dict) -> Plan:
    plan = Plan()
    for s in plan_dict.get("steps", []):
        step = PlanStep(
            step_id=s["step_id"],
            title=s["title"],
            instruction=s.get("instruction", s.get("detail", "")),
            executor_id=s.get("executor_id", ""),
            depends_on=s.get("depends_on", []),
            result_observation=s.get("result_observation", s.get("observation", "")),
            status=s.get("status", "pending"),
            status_reason=s.get("status_reason", s.get("note", "")),
            created_at=s.get("created_at", 0),
            updated_at=s.get("updated_at", 0),
        )
        plan.steps.append(step)
    plan.finished = plan_dict.get("finished", False)
    plan.summary  = plan_dict.get("summary", "")
    return plan

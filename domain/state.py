from __future__ import annotations
from typing import Any,Literal
from enum import Enum, auto
from dataclasses import dataclass, field
import uuid
import time

PlanStatus = Literal["pending", "in_progress", "done", "failed", "skipped"]

@dataclass
class PlanStep:
    step_id:    str
    title:      str
    detail:     str         = ""
    status:     PlanStatus  = "pending"
    note:       str         = ""
    created_at: float       = field(default_factory=time.time)
    updated_at: float       = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "step_id":    self.step_id,
            "title":      self.title,
            "detail":     self.detail,
            "status":     self.status,
            "note":       self.note,
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
                detail=s.get("detail", ""),
            ))

    def update_step(self, step_id: str, title: str | None = None,
                    detail: str | None = None, status: PlanStatus | None = None,
                    note: str | None = None) -> PlanStep | None:
        for step in self.steps:
            if step.step_id == step_id:
                if title  is not None: step.title  = title
                if detail is not None: step.detail = detail
                if status is not None: step.status = status
                if note   is not None: step.note   = note
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
            detail=s.get("detail", ""),
            status=s.get("status", "pending"),
            note=s.get("note", ""),
            created_at=s.get("created_at", 0),
            updated_at=s.get("updated_at", 0),
        )
        plan.steps.append(step)
    plan.finished = plan_dict.get("finished", False)
    plan.summary  = plan_dict.get("summary", "")
    return plan


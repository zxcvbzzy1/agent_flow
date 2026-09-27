"""Contracts and validation for the replaceable, batch-oriented L3 → L2 pipeline."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


@dataclass(frozen=True)
class ProcessingConfig:
    mode: str = "jev"
    run_batch_size: int = 1
    noul_threshold: float = 0.7

    def __post_init__(self):
        if self.mode not in {"rules", "jev"}:
            raise ValueError("mode 必须为 rules 或 jev")
        if type(self.run_batch_size) is not int or not 1 <= self.run_batch_size <= 100:
            raise ValueError("每批 run 数须为 1–100 的整数")
        if not 0 <= self.noul_threshold <= 1:
            raise ValueError("Noul 阈值须为 0–1")

    def to_dict(self):
        return asdict(self)


class Structure(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class MemoryCandidate(Structure):
    """A full-line L3 slice; routing results are persisted alongside this record."""
    candidate_id: str
    batch_id: str
    source_id: str
    user_id: str
    room_id: str
    conversation_id: str
    run_id: str
    run_status: str
    message_id: str
    file_path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    content: str
    section_kind: str
    event: dict = Field(default_factory=dict)
    created_at: float
    status: str = "pending"


class UserPreference(Structure):
    topic: str | None = None
    preference: str | None = None
    scope: str | None = None
    conditions: str | None = None


class ProjectState(Structure):
    project: str | None = None
    object: str | None = None
    status: str | None = None
    progress: str | None = None
    blockers: str | None = None
    next_steps: str | None = None
    observed_at: str | None = None


class UserFact(Structure):
    subject: str | None = None
    attribute: str | None = None
    value: str | None = None
    scope: str | None = None
    valid_time: str | None = None


class Decision(Structure):
    decision: str | None = None
    actor: str | None = None
    status: str | None = None
    rationale: str | None = None
    constraints: str | None = None
    decided_at: str | None = None


class ReusableConclusion(Structure):
    problem: str | None = None
    experimental_conditions: str | None = None
    observations: str | None = None
    conclusion: str | None = None
    applicability: str | None = None
    limitations: str | None = None


STRUCTURES = {"user_preference": UserPreference, "project_state": ProjectState,
              "user_fact": UserFact, "decision": Decision, "reusable_conclusion": ReusableConclusion}
CATEGORIES = tuple(STRUCTURES)


class ExtractedMemory(Structure):
    content: str = Field(min_length=1)
    structure: dict
    tags: list[str] = Field(default_factory=list)
    evidence_candidate_ids: list[str] = Field(min_length=1)


def validate_memories(rows: list[dict], category: str, candidates: list[dict]) -> list[dict]:
    if not isinstance(rows, list):
        raise ValueError("提取结果必须为列表")
    allowed = {c["candidate_id"] for c in candidates}
    result = []
    for row in rows:
        item = ExtractedMemory.model_validate(row).model_dump()
        if not item["content"].strip() or not set(item["evidence_candidate_ids"]) <= allowed:
            raise ValueError("提取正文为空或引用了本类别子批之外的候选")
        item["structure"] = STRUCTURES[category].model_validate(item["structure"]).model_dump()
        item["evidence_candidate_ids"] = list(dict.fromkeys(item["evidence_candidate_ids"]))
        result.append(item)
    return result


class NoulResult(Structure):
    noul: float = Field(ge=0, le=1, allow_inf_nan=False)
    model: str
    prompt_version: str


class ChoiceResult(Structure):
    probabilities: dict[str, float]
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    model: str
    prompt_version: str

    def validated(self) -> dict:
        p = self.probabilities
        if set(p) != set(CATEGORIES) or any(not 0 <= v <= 1 for v in p.values()) or abs(sum(p.values()) - 1) > .01:
            raise ValueError("Choice 必须返回五类的完整概率分布")
        return {**self.model_dump(), "category": max(CATEGORIES, key=lambda k: p[k])}


class MemoryClassifier(Protocol):
    async def noul(self, candidate: dict) -> dict: ...
    async def choice(self, candidate: dict) -> dict: ...


class MemoryExtractor(Protocol):
    async def extract(self, category: str, candidates: list[dict]) -> dict: ...
    async def equivalent(self, category: str, left: dict, right: dict) -> bool: ...


class ProcessingRepository(Protocol):
    def settings(self, user_id: str) -> dict: ...
    def batches(self, query: dict) -> list[dict]: ...
    def put_batch(self, batch: dict) -> dict: ...
    def update_batch(self, batch_id: str, owner: str, changes: dict) -> dict: ...
    def candidates(self, batch_id: str) -> list[dict]: ...
    def put_candidate(self, candidate: dict) -> dict: ...
    def update_candidate(self, candidate_id: str, changes: dict) -> None: ...
    def acquire(self, user_id: str, owner: str) -> bool: ...
    def renew(self, user_id: str, owner: str) -> bool: ...
    def release(self, user_id: str, owner: str) -> None: ...
    def claim_batch(self, batch_id: str, owner: str) -> dict: ...


class ProcessingLeaseLost(RuntimeError):
    """Another worker owns this user's batch; stop without changing its state."""

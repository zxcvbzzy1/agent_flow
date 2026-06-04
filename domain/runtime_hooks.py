from __future__ import annotations

from typing import Any, Protocol

from domain.event import Event


class ToolEventObserverPort(Protocol):
    """Optional observer for mirroring internal tool events."""

    def on_tool_event(self, event: Event) -> Any:
        ...


class HumanApprovalProviderPort(Protocol):
    """Optional provider for non-terminal human approval flows."""

    async def request_approval(
        self,
        *,
        run_id: str,
        agent_id: str,
        tool_name: str,
        called_event_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        ...


class RunContextProviderPort(Protocol):
    """Optional provider for resolving application run context."""

    def run_id_for_agent(self, agent_id: str) -> str:
        ...


class SkillRetrieverPort(Protocol):
    """Optional retriever for recalling skills (system recall + recall_skill tool).

    返回元素需带 .skill 与 .score（见 domain.skill.retriever.SkillHit）。
    实现可以是简单向量匹配，也可以后续替换为 RAG 检索器。
    """

    def retrieve(self, query: str, k: int = 5, threshold: float = 0.0) -> Any:
        ...


_tool_event_observer: ToolEventObserverPort | None = None
_human_approval_provider: HumanApprovalProviderPort | None = None
_run_context_provider: RunContextProviderPort | None = None
_skill_retriever: SkillRetrieverPort | None = None


def register_tool_event_observer(observer: ToolEventObserverPort | None) -> None:
    global _tool_event_observer
    _tool_event_observer = observer


def get_tool_event_observer() -> ToolEventObserverPort | None:
    return _tool_event_observer


def register_human_approval_provider(provider: HumanApprovalProviderPort | None) -> None:
    global _human_approval_provider
    _human_approval_provider = provider


def get_human_approval_provider() -> HumanApprovalProviderPort | None:
    return _human_approval_provider


def register_run_context_provider(provider: RunContextProviderPort | None) -> None:
    global _run_context_provider
    _run_context_provider = provider


def get_run_context_provider() -> RunContextProviderPort | None:
    return _run_context_provider


def register_skill_retriever(retriever: SkillRetrieverPort | None) -> None:
    global _skill_retriever
    _skill_retriever = retriever


def get_skill_retriever() -> SkillRetrieverPort | None:
    return _skill_retriever

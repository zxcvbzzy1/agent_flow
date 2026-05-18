from __future__ import annotations

import uuid
from typing import Any

from domain.agent.plan.providers import (
    ExecutorStatusProvider,
    PlanObservationProvider,
    PlanStepPromptProvider,
)
from domain.context.context import ContextEngine
from domain.context.providers import (
    AvailableToolsProvider,
    HistoryProvider,
    StateProvider,
    ToolOutputProvider,
    UserPromptProvider,
)
from domain.context.strategy import FullHistoryStrategy, RecencyStrategy, TokenBudgetStrategy
from domain.memory.short.default_short_term_memory import DefaultShortTermMemory
from infra.db.mongodb import DocumentStore


class ContextService:
    def __init__(self, store: DocumentStore) -> None:
        self._store = store
        self._engines: dict[str, ContextEngine] = {}
        self.ensure_default_contexts()

    def ensure_default_contexts(self) -> None:
        for kind in ("executor", "planner", "step"):
            context_id = f"default_{kind}"
            if self._store.find_one("contexts", {"context_id": context_id}) is None:
                self.create_context(kind=kind, name=f"Default {kind}", context_id=context_id)

    def create_context(
        self,
        kind: str,
        name: str,
        provider_config: list[dict[str, Any]] | None = None,
        strategy_config: dict[str, Any] | None = None,
        available_fields: list[str] | None = None,
        context_id: str | None = None,
    ) -> dict[str, Any]:
        context_id = context_id or str(uuid.uuid4())
        record = {
            "context_id": context_id,
            "kind": kind,
            "name": name,
            "provider_config": provider_config or [],
            "strategy_config": strategy_config or {"type": "full_history"},
            "available_fields": available_fields or ["system", "search", "memory", "write_agent", "human"],
        }
        self._store.update_one("contexts", {"context_id": context_id}, record, upsert=True)
        self._engines[context_id] = self._build_engine(record)
        return record

    def get_context(self, context_id: str) -> dict[str, Any] | None:
        return self._store.find_one("contexts", {"context_id": context_id})

    def get_engine(self, context_id: str) -> ContextEngine:
        if context_id not in self._engines:
            record = self.get_context(context_id)
            if record is None:
                raise KeyError(f"上下文不存在: {context_id}")
            self._engines[context_id] = self._build_engine(record)
        return self._engines[context_id]

    def _build_engine(self, record: dict[str, Any]) -> ContextEngine:
        memory = DefaultShortTermMemory(["tool_respond", "agent_history"])
        kind = record.get("kind", "executor")
        strategy = self._build_strategy(record.get("strategy_config", {}))

        if kind == "planner":
            providers = [
                UserPromptProvider(),
                StateProvider(),
                ExecutorStatusProvider(),
                PlanObservationProvider(),
                HistoryProvider(memory, "agent_history", strategy),
                ToolOutputProvider(memory, "tool_respond", strategy),
            ]
        elif kind == "step":
            providers = [PlanStepPromptProvider()]
        else:
            providers = [
                UserPromptProvider(),
                StateProvider(),
                AvailableToolsProvider(record.get("available_fields", ["system"])),
                HistoryProvider(memory, "agent_history", strategy),
                ToolOutputProvider(memory, "tool_respond", strategy),
            ]
        return ContextEngine(providers=providers, memory=memory)

    def _build_strategy(self, config: dict[str, Any]):
        strategy = FullHistoryStrategy()
        if config.get("keep_last"):
            strategy = strategy | RecencyStrategy(int(config["keep_last"]))
        if config.get("token_limit"):
            strategy = strategy | TokenBudgetStrategy(int(config["token_limit"]))
        return strategy


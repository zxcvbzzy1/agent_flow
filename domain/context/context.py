# domain/context/engine.py
from __future__ import annotations
from domain.context.providers import ContextProvider
from domain.memory.short.default_short_term_memory import ShortTermMemory


class ContextEngine:

    def __init__(
        self,
        providers: list[ContextProvider],
        memory:    ShortTermMemory,
    ) -> None:
        self._providers = providers
        self._memory    = memory

    def build(self, state: dict) -> str:
        pieces: list[str] = []
        for p in self._providers:
            if not p.enabled:
                continue
            try:
                pieces.extend(p.get(state))
            except Exception as e:
                name = p.name if hasattr(p, "name") else type(p).__name__
                print(f"[ContextEngine] provider '{name}' error: {e}")
        return "\n\n".join(p for p in pieces if p.strip())

    def get_memory(self) -> ShortTermMemory:
        return self._memory
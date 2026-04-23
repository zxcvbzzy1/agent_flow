# domain/context/context_engine.py

from abc import ABC, abstractmethod
from dataclasses import dataclass
from domain.context.strategy import DefaultComposeStrategy


class ContextProvider(ABC):
    @abstractmethod
    def get(self, state: dict) -> str:
        ...

@dataclass(frozen=True)
class ProviderRespond():
    agent_id:str
    respond:str 
    


class ComposeStrategy(ABC):
    @abstractmethod
    def compose(self, pieces: list[str]) -> str:
        ...



class ContextEngine:
    def __init__(self, providers: list[ContextProvider],
                 strategy: ComposeStrategy | None = None):
        self._providers = providers
        self._strategy  = strategy or DefaultComposeStrategy()

    def build(self, state: dict) -> str:
        pieces = []
        for provider in self._providers:
            try:
                content = provider.get(state)
                if content:
                    pieces.append(content)
            except Exception as e:
                print(f"[ContextEngine] provider error: {e}")
        return self._strategy.compose(pieces)
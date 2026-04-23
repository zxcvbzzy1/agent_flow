# domain/context/strategy.py

from abc import ABC, abstractmethod

class ComposeStrategy(ABC):
    @abstractmethod
    def compose(self, pieces: list[str]) -> str: ...

class DefaultComposeStrategy(ComposeStrategy):
    def compose(self, pieces: list[str]) -> str:
        return "\n\n".join(p for p in pieces if p)
"""Memory adapter over the generic JEV client: versioned memory prompts and the "memory" question key."""
import os

from domain.memory.long.prompts import (CATEGORY_DESCRIPTIONS, CHOICE_PROMPT, CHOICE_VERSION,
                                       GATE_PROMPT, GATE_VERSION)
from infra.jev import JevClient


class JevClassifier:
    def __init__(self, model=None):
        self.client = JevClient(model or os.getenv("MEMORY_JEV_MODEL"))

    @property
    def model(self):
        return self.client.model

    def ready(self):
        return self.client.ready()

    async def noul(self, candidate):
        # Include provenance and run outcome as well as the unmodified text.
        result = await self.client.noul(candidate, GATE_PROMPT, key="memory")
        return {**result, "prompt_version": GATE_VERSION}

    async def choice(self, candidate):
        result = await self.client.choice(candidate, CHOICE_PROMPT, CATEGORY_DESCRIPTIONS, key="memory")
        return {**result, "prompt_version": CHOICE_VERSION}

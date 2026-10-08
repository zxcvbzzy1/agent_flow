"""Generic TypeSafe JEV client (official SDK); callers supply prompts, criteria and question keys.

Reusable for any judgement over a structured state: memory gating/classification,
"should we recall?" decisions, etc. No generative calls and no application dependencies.
"""
from __future__ import annotations

import os


class JevClient:
    def __init__(self, model: str | None = None, *, timeout: float = 90, max_retries: int = 0):
        self.model = model or os.getenv("JEV_MODEL", "jev-latest")
        self.timeout, self.max_retries = timeout, max_retries

    def ready(self) -> bool:
        return bool(os.getenv("TYPESAFE_API_KEY", "").strip())

    async def ask(self, state, questions: dict):
        """One system_one call; several questions share the same state and request."""
        from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy
        if not self.ready():
            raise ValueError("未配置 TYPESAFE_API_KEY")
        async with AsyncTypeSafeClient(api_key=os.environ["TYPESAFE_API_KEY"], timeout=self.timeout,
                                       retry=RetryPolicy(max_retries=self.max_retries)) as client:
            return await client.system_one(state=state, questions=questions, model=self.model)

    async def noul(self, state, instructions: str, *, key: str = "default") -> dict:
        from typesafe_sdk import Noul
        response = await self.ask(state, {key: Noul(instructions=instructions)})
        return {"noul": response.nouls[key].noul, "model": response.model}

    async def choice(self, state, instructions: str, criteria: dict, *, key: str = "default") -> dict:
        from typesafe_sdk import Choice
        response = await self.ask(state, {key: Choice(instructions=instructions, criteria=criteria)})
        answer = response.choices[key]
        return {"probabilities": answer.probabilities, "confidence": answer.confidence, "model": response.model}

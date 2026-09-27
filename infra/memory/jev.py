"""Official TypeSafe SDK adapter; no generative calls or application dependencies."""
import os

from domain.memory.long.prompts import (CATEGORY_DESCRIPTIONS, CHOICE_PROMPT, CHOICE_VERSION,
                                       GATE_PROMPT, GATE_VERSION)


class JevClassifier:
    def __init__(self, model=None):
        self.model = model or os.getenv("MEMORY_JEV_MODEL", "jev-latest")

    def ready(self):
        return bool(os.getenv("TYPESAFE_API_KEY", "").strip())

    async def _call(self, candidate, question):
        from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy
        if not self.ready():
            raise ValueError("未配置 TYPESAFE_API_KEY")
        async with AsyncTypeSafeClient(api_key=os.environ["TYPESAFE_API_KEY"], timeout=90,
                                       retry=RetryPolicy(max_retries=0)) as client:
            # Include provenance and run outcome as well as the unmodified text.
            return await client.system_one(state=candidate, questions={"memory": question}, model=self.model)

    async def noul(self, candidate):
        from typesafe_sdk import Noul
        response = await self._call(candidate, Noul(instructions=GATE_PROMPT))
        return {"noul": response.nouls["memory"].noul, "model": response.model, "prompt_version": GATE_VERSION}

    async def choice(self, candidate):
        from typesafe_sdk import Choice
        response = await self._call(candidate, Choice(instructions=CHOICE_PROMPT, criteria=CATEGORY_DESCRIPTIONS))
        answer = response.choices["memory"]
        return {"probabilities": answer.probabilities, "confidence": answer.confidence,
                "model": response.model, "prompt_version": CHOICE_VERSION}

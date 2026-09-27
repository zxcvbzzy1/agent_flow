"""Structured extraction over the existing OpenAI-compatible LLM connection."""
import json
import os

from domain.memory.long.processing import STRUCTURES
from domain.memory.long.prompts import (COMMON_EXTRACTION, EXTRACTION_PROMPTS, EXTRACTION_VERSION,
                                       MERGE_PROMPT, MERGE_VERSION)


class LLMExtractionAdapter:
    def __init__(self, llm):
        self.llm = llm
        self.model = os.getenv("MEMORY_EXTRACTION_MODEL") or llm.model
        self.base_url = os.getenv("MEMORY_EXTRACTION_BASE_URL") or llm.url

    def _key(self):
        return os.getenv("MEMORY_EXTRACTION_API_KEY", "").strip() or self.llm._api_key()

    def ready(self):
        try:
            return bool(self._key())
        except RuntimeError:
            return False

    async def _json(self, prompt, payload):
        from openai import AsyncOpenAI
        if not self.ready():
            raise ValueError("未配置提取模型凭据：请设置 MEMORY_EXTRACTION_API_KEY 或现有 LLM API key")
        # A per-request client avoids sharing clients across API event loops.
        async with AsyncOpenAI(api_key=self._key(), base_url=self.base_url, timeout=120, max_retries=0) as client:
            response = await client.chat.completions.create(model=self.model, stream=False,
                max_tokens=min(self.llm.max_tokens, 16000), response_format={"type": "json_object"},
                messages=[{"role": "system", "content": prompt},
                          {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}])
        if response.choices[0].finish_reason != "stop":
            raise ValueError("提取响应不完整")
        value = json.loads(response.choices[0].message.content or "")
        if not isinstance(value, dict):
            raise ValueError("提取响应须为 JSON 对象")
        return value, response.model

    async def extract(self, category, candidates):
        prompt = COMMON_EXTRACTION + "\n" + EXTRACTION_PROMPTS[category]
        prompt += "\nstructure JSON Schema: " + json.dumps(STRUCTURES[category].model_json_schema(), ensure_ascii=False)
        value, model = await self._json(prompt, {"category": category, "candidates": candidates})
        return {"memories": value["memories"], "model": model, "prompt_version": EXTRACTION_VERSION}

    async def equivalent(self, category, left, right):
        value, _ = await self._json(MERGE_PROMPT, {"category": category, "left": left, "right": right})
        if type(value.get("equivalent")) is not bool:
            raise ValueError("归并判断必须返回布尔值")
        return value["equivalent"]

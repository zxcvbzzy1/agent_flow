
import asyncio
import codecs
import errno
import json
from enum import Enum
import os
from pathlib import Path
import shlex
import tempfile
from dotenv import load_dotenv
from openai import AsyncOpenAI

for env_path in (
    Path.cwd() / ".env",
    Path(__file__).resolve().parents[2] / ".env",
    Path(__file__).resolve().parents[3] / ".env",
):
    load_dotenv(env_path, override=False)

class LLM_Model_Provider(Enum):
    """LLM模型提供者"""
    GLM = "GLM_API"
    DEEPSEEK = "DEEPSEEK_API"
    MINMAX = "MINIMAX_API"



class LLM_Client:
    def __init__(self,url:str,model_class:str,model_provider:LLM_Model_Provider,max_tokens:int):
        self.url = url
        self.model_provider = model_provider
        self._client: AsyncOpenAI | None = None
        self.model = model_class
        self.max_tokens =max_tokens

    def _api_key(self) -> str:
        key = os.getenv(self.model_provider.value, "").strip()
        if not key or key == "im-backend-placeholder":
            raise RuntimeError(
                f"LLM API key 未配置：请在环境变量或 .env 中设置 {self.model_provider.value}。"
                f"当前 base_url={self.url}, model={self.model}"
            )
        return key

    @property
    def client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=self._api_key(),
                base_url=self.url,
            )
        return self._client

    async def default_call(self, messages: list, model: str):
        async for delta in self.stream_chat(messages, model=model):
            yield delta

    async def stream_chat(self, messages: list, model: str | None = None):
        stream = await self.client.chat.completions.create(
            model=model or self.model,
            messages=messages,
            max_tokens=self.max_tokens,
            stream=True,
            extra_body={"reasoning_split": True},
            response_format={
                'type': 'json_object'
            }
        )

        async for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta
    
    async def chat(self, messages: list):
        full_response = ""
        async for delta in self.stream_chat(messages, model=self.model):
            print(delta, end="", flush=True)
            full_response += delta
        return full_response

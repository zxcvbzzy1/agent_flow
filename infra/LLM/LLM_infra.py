

from enum import Enum
import os
from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv() 

class LLM_Model_Provider(Enum):
    """LLM模型提供者"""
    GLM = "GLM_API"
    DEEPSEEK = "DEEPSEEK_API"
    MINMAX = "MINIMAX_API"



class LLM_Client:
    def __init__(self,url:str,model_class:str,model_provider:LLM_Model_Provider,max_tokens:int):
        self.url = url
        self.client = AsyncOpenAI(
            api_key=os.getenv(f"{model_provider.value}"), 
            base_url=self.url,
        )
        self.model = model_class
        self.max_tokens =max_tokens

    async def default_call(self, messages: list, model: str):
        stream =await self.client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=self.max_tokens,
            stream=True,
            extra_body={"reasoning_split": True},
        )

        async for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta
    
    async def chat(self, messages: list):
        full_response = ""
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            max_tokens=self.max_tokens,
            stream=True,
            extra_body={"reasoning_split": True},
        )

        async for chunk in response:
            # 获取 delta 内容
            delta = chunk.choices[0].delta.content
            if delta:
                print(delta, end="", flush=True)
                full_response += delta
        return full_response

    



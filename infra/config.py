import os

from domain.agent_base import AgentBase
from domain.context.context import ContextEngine
from domain.context.providers import *
from domain.event import ToolEventFactory
from infra.LLM.LLM_infra import LLM_Client, LLM_Model_Provider
from infra.eventbus import EventBus


# 事件总线
bus = EventBus()
# agent注册表
agent_dict = AgentBase.get_instance_dict()
# 工具工厂
factory = ToolEventFactory(prefix="infra")._build()._resigister_bus(bus)
# 默认工具使用模型
llm_client = LLM_Client(
    url=os.getenv("LLM_BASE_URL", "https://api.minimaxi.com/v1"),
    # model_class="glm-5.1",
    model_class="MiniMax-M2.7",
    model_provider=LLM_Model_Provider.MINMAX,
    max_tokens=131072,
)


# ── LLM 摘要函数 ──────────────────────────────────────────────────

_OUTLINE_SYSTEM_PROMPT = """你是一个内容分析专家。你的任务是为给定文本生成结构化大纲。
要求：
- 文本已被分为 {chunk_count} 个片段，编号 0-{last_chunk}
- 每个要点对应一个片段，在末尾标注 [片段N]
- 提取核心内容和关键信息
- 用中文回答
- 格式：编号. 内容概述 [片段N]
"""


def make_outline_fn(llm_client):
    async def outline_fn(source_key: str, raw: str, chunk_count: int) -> str:
        user_prompt = f"请为以下内容生成结构化大纲，来源标识：[{source_key}]\n\n{raw}"
        system_prompt = _OUTLINE_SYSTEM_PROMPT.format(
            chunk_count=chunk_count,
            last_chunk=chunk_count - 1,
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ]
        try:
            chunks = []
            async for chunk in llm_client.default_call(
                messages=messages,
                model=llm_client.model,
            ):
                chunks.append(chunk)
            return "".join(chunks).strip()
        except Exception as e:
            raise RuntimeError(f"outline_fn LLM call failed: {e}") from e

    return outline_fn


# ── ContextStore ────────────────────────────────────────────────────

from domain.context.store.store import ContextStore

store = ContextStore(
    token_limit=100000,
    storage_dir="./ctx_storage",
    outline_fn=make_outline_fn(llm_client),
)

# 上下文管理
providers = [
    UserPromptProvider(),
    StateProvider(),
    HistoryProvider(store),
    ToolRespondProvider(store),
    StoredContextProvider(store),
    AvailableToolsProvider(["system", "search", "memory", "write_agent"]),
]

# ── ContextEngine ──────────────────────────────────────────────────
engine = ContextEngine(
    providers=providers,
    context_store=store,
)

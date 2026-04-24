import os

from domain.agent_base import AgentBase
from domain.context.context import ContextEngine
from domain.context.processor import DocumentProcessor, HistoryProcessor, ToolOutputProcessor
from domain.context.providers import *
from domain.event import ToolEventFactory
from domain.memory.short.default_short_term_memory import DefaultShortTermMemory
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

# ---------------------------------------------------------------------------
# 大纲生成 prompt
# ---------------------------------------------------------------------------

_OUTLINE_SYSTEM_PROMPT = """你是一个内容分析专家。你的任务是为给定文本生成结构化大纲。
要求：
- 提取文本的核心结构和主要内容点
- 用层级列表表示（- 或数字编号）
- 每个要点简洁，不超过一行
- 保留关键数据、结论、重要细节
- 不添加原文中没有的内容
- 末尾注明总字符数和可读取的片段数
- 用中文回答
"""


def make_outline_fn(llm_client):
    async def outline_fn(source_key: str, raw: str) -> str:
        user_prompt = f"请为以下内容生成结构化大纲，来源标识：[{source_key}]\n\n{raw}"
        messages = [
            {"role": "system", "content": _OUTLINE_SYSTEM_PROMPT},
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


# ── Memory 实例 ─────────────────────────────────────

memory = DefaultShortTermMemory()

store = ContextStore(token_limit=100000, storage_dir="./ctx_storage")
store.register_processor("memory",   ToolOutputProcessor(outline_fn=make_outline_fn(llm_client)))
store.register_processor("document", DocumentProcessor(outline_fn=make_outline_fn(llm_client)))
store.register_processor("history",  HistoryProcessor())
# 上下文管理
providers = [
        UserPromptProvider(),
        StateProvider(),
        HistoryProvider(store),
        ToolRespondProvider(store),
        # ExploredContextProvider(store),
        AvailableToolsProvider(["system", "search", "memory","write_agent"]),
    ]
 
    # ── 4. ContextEngine ──────────────────────────────────────────
engine = ContextEngine(
    providers=providers,
    context_store=store
)
import os

from domain.agent_base import AgentBase
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
    url=os.getenv("LLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/"),
    # model_class="glm-5.1",
    model_class="MiniMax-M2.7",
    model_provider=LLM_Model_Provider.MINMAX,
    max_tokens=131072,
)


# ── LLM 摘要函数 ──────────────────────────────────────────────────

_SUMMARY_SYSTEM_PROMPT = """你是一个内容总结专家。你的任务是将工具返回的长文本进行简洁准确的总结，保留关键信息。
要求：
- 用1-3句话概括核心内容
- 保留关键数据、结论和重要细节
- 不要添加原文中没有的信息
- 用中文回答
"""


async def llm_summarize(tool_name: str, raw: str, call_index: int) -> str:
    """供 DefaultShortTermMemory 使用的 LLM 摘要函数"""
    user_prompt = f"工具 [{tool_name}] 第{call_index}次调用返回了以下内容，请总结：\n\n{raw}"
    messages = [
        {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
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
        return f"(LLM 总结失败: {e}) 原文前{200}字: {raw[:200]}..."


# ── 带摘要能力的 Memory 实例 ─────────────────────────────────────

memory = DefaultShortTermMemory(summarize_fn=llm_summarize)

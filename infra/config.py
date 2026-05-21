import os

from domain.agent_base import AgentBase
from domain.agent.plan.providers import ExecutorStatusProvider, PlanObservationProvider
from domain.context.context import ContextEngine
from domain.context.providers import *
from domain.context.strategy import FullHistoryStrategy, LatestOnlyStrategy, RecencyStrategy, TokenBudgetStrategy
from domain.event import ToolEventFactory
from domain.memory.short.default_short_term_memory import DefaultShortTermMemory
from infra.LLM.LLM_infra import LLM_Client, LLM_Model_Provider
from infra.context.db_strategy import ChunkToFileStrategy
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


# 个性化设置
# 记忆
memory = DefaultShortTermMemory(["tool_respond", "agent_history"])
memory2 = DefaultShortTermMemory(["tool_respond", "agent_history"])

# ReACT执行者上下文提供类
providers = [
    UserPromptProvider(),
    StateProvider(),
    AvailableToolsProvider(["system", "search", "memory", "write_agent"]),
    HistoryProvider(memory, "agent_history", FullHistoryStrategy()),
    ToolOutputProvider(memory, "tool_respond", FullHistoryStrategy() | RecencyStrategy(10) | ChunkToFileStrategy("./mid",4000,4000)),
]

# PlanAgent编排上下文提供类
plan_providers = [
    UserPromptProvider(),
    StateProvider(),
    ExecutorStatusProvider(),
    PlanObservationProvider(),
    HistoryProvider(memory2, "agent_history", FullHistoryStrategy()),
    ToolOutputProvider(memory2, "tool_respond", FullHistoryStrategy() | RecencyStrategy(10) | ChunkToFileStrategy("./mid",4000,4000)),
]

# ── 上下文管理类 ──────────────────────────────────────────────────
engine = ContextEngine(
    providers=providers,
    memory=memory,
)

plan_engine = ContextEngine(
    providers=plan_providers,
    memory=memory2,
)

import asyncio
import os
from domain.agent.plan.orchestrator import OrchestratorState, PlanOrchestrator
from domain.agent.plan.planAgent import PlanAgent
from domain.agent.plan.providers import (
    ExecutorStatusProvider,
    PlanObservationProvider,
    PlanStepPromptProvider,
)
from domain.agent.write.writeAgent import WriteAgent
from domain.context.context import ContextEngine
from domain.context.providers import (
    AvailableToolsProvider,
    HistoryProvider,
    StateProvider,
    ToolOutputProvider,
    UserPromptProvider,
)
from domain.context.strategy import FullHistoryStrategy, RecencyStrategy
from domain.memory.short.default_short_term_memory import DefaultShortTermMemory
from infra.LLM.LLM_infra import LLM_Client, LLM_Model_Provider
from infra.eventbus import EventBus


# LLM 客户端配置
llm_client = LLM_Client(
    url=os.getenv("LLM_BASE_URL", "https://api.minimaxi.com/v1"),
    model_class="MiniMax-M2.7",
    model_provider=LLM_Model_Provider.MINMAX,
    max_tokens=131072,
)


# 共享记忆：让 planner 与 executors 能看到工具反馈和历史结果。
workflow_memory = DefaultShortTermMemory(["tool_respond", "agent_history"])

# plan agent 上下文，提供给 planner 用于决策和编排
planner_context = ContextEngine(
    providers=[
        UserPromptProvider(),
        StateProvider(),
        PlanObservationProvider(),
        ExecutorStatusProvider(),
    ],
    memory=workflow_memory,
)


# 编排者上下文
step_context = ContextEngine(
    providers=[
        PlanStepPromptProvider(),
    ],
    memory=workflow_memory,
)


# 组装 planner 和 executors
planner = PlanAgent(
    id="plan_agent",
    name="任务编排者",
    llm=llm_client,
    context=planner_context,
)

executors ={}
orchestrator = PlanOrchestrator(
    planner=planner,
    executors=executors,
    step_context_engine=step_context,
    event_bus=EventBus(),
    state=OrchestratorState(),
    max_replan_rounds=3,
)


import os

from domain.agent_base import AgentBase
from domain.context.context import ContextEngine
from domain.context.providers import AvailableToolsProvider, HistoryProvider, StateProvider, ToolOutputProvider, UserPromptProvider
from domain.context.strategy import FullHistoryStrategy, RecencyStrategy
from domain.memory.short.default_short_term_memory import DefaultShortTermMemory
from infra.LLM.LLM_infra import LLM_Client, LLM_Model_Provider
from infra.tool.builtin import system

# LLM 客户端配置
llm_client = LLM_Client(
    url=os.getenv("LLM_BASE_URL", "https://api.minimaxi.com/v1"),
    model_class="MiniMax-M2.7",
    model_provider=LLM_Model_Provider.MINMAX,
    max_tokens=131072,
)


# CLI agent 操作主体
class OperatorExecutor(AgentBase):
    """负责系统操作步骤的 ReACT executor"""

    def _build_agent_prompt(self) -> str:
        return f"""
你是一个系统操作执行者，当前工作目录为：{self.work_path}
你可以使用 bash 工具执行命令来完成任务。

## 输出格式
用 JSON 严格按以下格式回复：
{{
  "think": "你的思考过程",
  "tool_calls": [
    {{
      "tool_name": "工具名",
      "arguments": {{"参数名": "参数值"}},
      "reasoning": "为什么调用这个工具"
    }}
  ],
  "is_finished": false
}}

## 任务完成时输出
{{
  "think": "...",
  "tool_calls": [],
  "is_finished": true,
  "finish_reason": "完成原因",
  "final": "最终结果"
}}
"""
    
# CLI agent上下文管理
operator_memory = DefaultShortTermMemory(["tool_respond", "agent_history"])
operator_context = ContextEngine(
    providers=[
        UserPromptProvider(),
        StateProvider(),
        AvailableToolsProvider(["system"]),
        HistoryProvider(operator_memory, "agent_history", FullHistoryStrategy()),
        ToolOutputProvider(operator_memory, "tool_respond", FullHistoryStrategy() | RecencyStrategy(5)),
    ],
    memory=operator_memory,
)

# 组装
operator = OperatorExecutor(
    id="operator",
    name="系统操作执行者",
    llm=llm_client,
    context=operator_context,
)
import asyncio
import os

# 先导入写作工具内置模块，再导入通用回调绑定。
from domain.agent_base import AgentBase
import infra.tool.builtin.story_write  # noqa: F401
import infra.tool.tools_attach_methods  # noqa: F401

from domain.context.context import ContextEngine
from domain.context.providers import (
    AvailableToolsProvider,
    ErrorProvider,
    HistoryProvider,
    StateProvider,
    ToolOutputProvider,
    UserPromptProvider,
)
from domain.context.strategy import FullHistoryStrategy, RecencyStrategy
from domain.memory.short.default_short_term_memory import DefaultShortTermMemory
from infra.LLM.LLM_infra import LLM_Client, LLM_Model_Provider
from infra.tool.builtin import story_write

# LLM 客户端配置
llm_client = LLM_Client(
    url=os.getenv("LLM_BASE_URL", "https://api.minimaxi.com/v1"),
    model_class="MiniMax-M2.7",
    model_provider=LLM_Model_Provider.MINMAX,
    max_tokens=131072,
)


class StoryWriterAgent(AgentBase):
    """故事写作 ReACT agent。"""

    def _build_agent_prompt(self) -> str:
        return """
你是一个专业的故事写作 Agent。

## 目标
根据用户的故事创作需求，自主选择写作工具完成从需求分析、大纲、初稿、检查、重写到润色的完整流程。

## 工作原则
- 优先理解题材、角色、冲突、叙事视角、篇幅和风格要求。
- 复杂任务先做需求分析和大纲，再写初稿。
- 写完后根据需要调用需求检查、重写或润色工具。
- 每轮只调用当前最有必要的工具，不要重复调用同一类工具。
- 当故事已经满足需求时，输出 is_finished=true 和最终文本。

## 输出格式
用 JSON 严格按以下格式回复：
{
  "think": "你的思考过程",
  "tool_calls": [
    {
      "tool_name": "工具名",
      "arguments": {"参数名": "参数值"},
      "reasoning": "为什么调用这个工具"
    }
  ],
  "is_finished": false
}

## 任务完成时输出
{
  "think": "...",
  "tool_calls": [],
  "is_finished": true,
  "finish_reason": "完成原因",
  "final": "最终故事文本或交付说明"
}
"""


# 故事写作 agent 上下文管理
story_memory = DefaultShortTermMemory(["tool_respond", "agent_history", "error"])
story_context = ContextEngine(
    providers=[
        UserPromptProvider(),
        StateProvider(),
        ErrorProvider(story_memory),
        AvailableToolsProvider(["write_agent", "human"]),
        HistoryProvider(story_memory, "agent_history", FullHistoryStrategy()),
        ToolOutputProvider(
            story_memory,
            "tool_respond",
            FullHistoryStrategy() | RecencyStrategy(8),
        ),
    ],
    memory=story_memory,
)


# 组装
story_writer = StoryWriterAgent(
    id="story_writer",
    name="故事写作执行者",
    llm=llm_client,
    context=story_context,
)

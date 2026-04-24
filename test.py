"""
集成测试文件：测试 WriteAgent、ToolEventFactory 和 ContextEngine 的核心功能
"""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock
from domain.agent.write.writeAgent import WriteAgent
from domain.state import Agent_state
from domain.tool import Tool
from domain.event import ToolEventFactory
from domain.context.context import ContextEngine
from domain.context.providers import (
    UserPromptProvider, StateProvider, ToolRespondProvider,
    AvailableToolsProvider, HistoryProvider,
)
from domain.context.store.store import ContextStore
from domain.context.store.node import ContextNode
from domain.context.processor import ToolOutputProcessor, HistoryProcessor
from domain.memory.short.default_short_term_memory import DefaultShortTermMemory


class TestContextEngine:
    """ContextEngine 核心链路测试：store.write → window → provider.get → engine.build"""

    def setup_method(self):
        self.store = ContextStore(token_limit=100000)
        self.store.register_processor("memory", ToolOutputProcessor(outline_fn=None))
        self.store.register_processor("history", HistoryProcessor())

        self.providers = [
            UserPromptProvider(),
            StateProvider(),
            HistoryProvider(self.store),
            ToolRespondProvider(self.store),
            AvailableToolsProvider(["write_agent"]),
        ]
        self.engine = ContextEngine(providers=self.providers, context_store=self.store)

    def _base_state(self, **overrides) -> dict:
        state = {
            "prompt": "写一篇科幻小说",
            "retry": 0,
            "last_tool_ok": True,
            "tool_history": [],
        }
        state.update(overrides)
        return state

    # ── 基础 build ───────────────────────────────────────────────

    def test_build_returns_nonempty_prompt(self):
        """最简单场景：只有 prompt + state，无工具反馈"""
        result = self.engine.build(self._base_state())
        assert "写一篇科幻小说" in result
        assert "当前执行状态" in result

    # ── store.write → ToolRespondProvider ──────────────────────────

    def test_tool_respond_appears_in_prompt(self):
        """工具输出写入 store 后，build 结果中包含工具反馈"""
        nodes = asyncio.run(
            self.store.write(
                source_key="tool:outline_generation#1",
                raw="第一章：深海之谜",
                scope="memory",
                metadata={"tool_name": "outline_generation"},
            )
        )
        assert len(nodes) > 0, "write 应返回节点列表"

        result = self.engine.build(self._base_state(
            tool_history=["outline_generation"],
        ))
        assert "工具反馈" in result
        assert "outline_generation" in result

    def test_short_output_creates_full_node(self):
        """短工具输出（≤800字符）应生成 promoted 的 full 节点"""
        short_text = "简单的工具输出结果"
        nodes = asyncio.run(
            self.store.write("tool:short_tool#1", short_text, scope="memory")
        )
        assert len(nodes) == 1
        assert nodes[0].granularity == "full"
        assert nodes[0].promoted is True

        window = self.store.window(scope="memory")
        assert len(window) == 1
        assert window[0].content == short_text

    def test_long_output_creates_skeleton_and_chunks(self):
        """长工具输出应生成 skeleton(promoted) + chunks(unpromoted)"""
        long_text = "这是一段很长的文本。" * 200  # ~1800 字符
        nodes = asyncio.run(
            self.store.write("tool:long_tool#1", long_text, scope="memory")
        )
        granularities = {n.granularity for n in nodes}
        assert "skeleton" in granularities
        assert "chunk" in granularities

        promoted = [n for n in nodes if n.promoted]
        assert any(n.granularity == "skeleton" for n in promoted)

    # ── 多工具输出 ───────────────────────────────────────────────

    def test_multiple_tool_responds(self):
        """多次工具调用结果都在 prompt 中"""
        asyncio.run(self.store.write(
            "tool:requirements_analysis#1",
            "需求：科幻悬疑，30000字",
            scope="memory",
            metadata={"tool_name": "requirements_analysis"},
        ))
        asyncio.run(self.store.write(
            "tool:outline_generation#1",
            "大纲：第一章…",
            scope="memory",
            metadata={"tool_name": "outline_generation"},
        ))
        result = self.engine.build(self._base_state(
            tool_history=["requirements_analysis", "outline_generation"],
        ))
        assert "工具反馈" in result
        assert "2 条" in result

    # ── HistoryProvider ───────────────────────────────────────────

    def test_history_appears_in_prompt(self):
        """写入 history scope 后，对话历史出现在 prompt"""
        asyncio.run(self.store.write(
            "history:turn_1",
            "用户：写一篇小说",
            scope="history",
        ))
        result = self.engine.build(self._base_state())
        assert "对话历史" in result
        assert "写一篇小说" in result

    # ── Slot enable/disable ──────────────────────────────────────

    def test_disabled_slot_is_skipped(self):
        """禁用 slot 后对应 provider 不注入"""
        ToolRespondProvider.slot.disable()
        try:
            asyncio.run(self.store.write(
                "tool:outline_generation#1",
                "大纲内容",
                scope="memory",
            ))
            result = self.engine.build(self._base_state(
                tool_history=["outline_generation"],
            ))
            assert "工具反馈" not in result
        finally:
            ToolRespondProvider.slot.enable()

    # ── on_tool_call 模拟 ────────────────────────────────────────

    def test_on_tool_call_writes_to_store(self):
        """模拟 on_tool_call 的 store.write 流程，验证数据流完整"""
        memory = DefaultShortTermMemory()
        memory.begin_round()

        # 模拟 store
        tool_name = "requirements_analysis"
        respond = "分析结果：科幻小说，30000字"

        memory.store(tool_name, respond)
        source_key = f"tool:{tool_name}#{memory.count(tool_name)}"
        asyncio.run(self.store.write(
            source_key=source_key,
            raw=respond,
            scope="memory",
            metadata={"tool_name": tool_name},
        ))

        # 验证 prompt 中有工具反馈
        result = self.engine.build(self._base_state(
            tool_history=[tool_name],
        ))
        assert "工具反馈" in result
        assert "requirements_analysis" in result

    # ── token budget / demote ────────────────────────────────────

    def test_demote_on_budget_exceeded(self):
        """token 超限时自动 demote 低优先级节点"""
        small_store = ContextStore(token_limit=50)  # 极小预算
        small_store.register_processor("memory", ToolOutputProcessor(outline_fn=None))

        # 写入一段超过 50 token 的内容
        text = "x" * 400  # ~100 tokens
        asyncio.run(small_store.write("tool:big#1", text, scope="memory"))

        # 短输出是 full 节点，超限后会被 demote
        window = small_store.window(scope="memory")
        # token_limit=50, 内容~100 tokens, 应该被 demote
        assert len(window) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
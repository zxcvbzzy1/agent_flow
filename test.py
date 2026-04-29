"""
集成测试文件：测试 ToolEventFactory 和 ContextEngine 的核心功能
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
    UserPromptProvider, StateProvider, ToolOutputProvider,
    AvailableToolsProvider, HistoryProvider,
)
from domain.memory.short.default_short_term_memory import DefaultShortTermMemory


class TestToolEventFactory:
    """ToolEventFactory 单元测试"""

    def setup_method(self):
        """清理并重新注册一些测试用的工具"""
        # 清除之前的注册，确保测试环境干净（实际项目中可能需要更复杂的隔离）
        Tool._registry.clear()

        # 注册两个测试工具
        self.search_tool = Tool(
            name="rag_search",
            description="搜索知识库",
            input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
            field="search"
        )
        self.outline_tool = Tool(
            name="outline_generator",
            description="生成大纲",
            input_schema={"type": "object", "properties": {"topic": {"type": "string"}}},
            field="write_agent"
        )

        self.factory = ToolEventFactory(prefix="test")
        self.factory._build()

    def test_factory_builds_specs(self):
        """测试工厂是否正确构建了所有注册工具的规格"""
        assert "rag_search" in self.factory._specs
        assert "outline_generator" in self.factory._specs

    def test_tool_event_names(self):
        """测试生成的事件名是否符合规范"""
        spec = self.factory.tool("rag_search")
        expected_called = "test.search.rag.search.called"
        assert spec._events["called"] == expected_called

        spec2 = self.factory.tool("outline_generator")
        expected_succeeded = "test.write.agent.outline.generator.succeeded"
        assert spec2._events["succeeded"] == expected_succeeded

    def test_emit_called_event(self):
        """测试触发 called 事件"""
        spec = self.factory.tool("rag_search")
        event = spec.called({"query": "AI"})
        assert event.name == "test.search.rag.search.called"
        assert event.payload == {"query": "AI"}

    def test_by_field_filtering(self):
        """测试按 field 分组获取工具"""
        search_tools = self.factory.by_field("search")
        write_tools = self.factory.by_field("write_agent")

        assert len(search_tools) == 1
        assert search_tools[0].tool_name == "rag_search"
        assert len(write_tools) == 1
        assert write_tools[0].tool_name == "outline_generator"

    def test_all_events_list(self):
        """测试获取所有事件名称列表"""
        events = self.factory.all_events()
        # 每个工具有 4 个标准事件 (called, succeeded, failed, retrying)
        assert len(events) == 8
        assert "test.search.rag.search.failed" in events



class TestWriteAgentCore:
    """WriteAgent 核心逻辑单元测试"""

    def setup_method(self):
        Tool._registry.clear()
        self.mock_llm = AsyncMock()
        # 创建最简 context engine
        self.memory = DefaultShortTermMemory(["tool_respond", "agent_history", "plan"])
        self.engine = ContextEngine(
            providers=[UserPromptProvider(), StateProvider()],
            memory=self.memory,
        )
        self.agent = WriteAgent(
            id="wa_001",
            name="NovelWriter",
            llm=self.mock_llm,
            context=self.engine,
        )

    def test_agent_initialization(self):
        """测试 Agent 初始化状态"""
        assert self.agent.id == "wa_001"
        assert "write_agent" in self.agent.states
        assert self.agent.states["write_agent"]["score"] == 1.0

    def test_parse_decision_json(self):
        """测试从 JSON 字符串解析决策"""
        raw = json.dumps({
            "think": "先写大纲",
            "tool_calls": [{"tool_name": "outline", "arguments": {}}],
            "is_finished": False
        })
        decision = self.agent._parse_decision(raw)

        assert decision.think == "先写大纲"
        assert len(decision.tool_calls) == 1
        assert decision.is_finished is False

    def test_parse_decision_markdown_wrapped(self):
        """测试解析被 Markdown 代码块包裹的 JSON"""
        raw = "```json\n{\"think\": \"ok\", \"tool_calls\": [], \"is_finished\": true}\n```"
        decision = self.agent._parse_decision(raw)

        assert decision.is_finished is True
        assert decision.think == "ok"

    def test_parse_decision_invalid_json(self):
        """测试解析无效 JSON 时的容错"""
        decision = self.agent._parse_decision("not a json")
        assert len(decision.tool_calls) == 0
        assert decision.think == "not a json"

    def test_step_logic_finished(self):
        """测试 _step 在任务完成时的返回逻辑"""
        self.mock_llm.chat.return_value = json.dumps({
            "think": "完成了",
            "tool_calls": [],
            "is_finished": True,
            "finish_reason": "质量达标"
        })

        result = asyncio.run(self.agent._step())

        assert result is True
        assert self.agent.states["is_finished"] is True


class TestContextEngine:
    """ContextEngine 核心链路测试：memory.store → provider.get → engine.build"""

    def setup_method(self):
        self.memory = DefaultShortTermMemory(["tool_respond", "agent_history", "plan"])

        self.providers = [
            UserPromptProvider(),
            StateProvider(),
            HistoryProvider(self.memory, "agent_history"),
            ToolOutputProvider(self.memory, "tool_respond"),
            AvailableToolsProvider(["write_agent"]),
        ]
        self.engine = ContextEngine(providers=self.providers, memory=self.memory)

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

    # ── memory.store → ToolOutputProvider ──────────────────────────

    def test_tool_output_appears_in_prompt(self):
        """工具输出写入 memory 后，build 结果中包含工具反馈"""
        self.memory.store("tool_respond", "outline_generation", "第一章：深海之谜")

        result = self.engine.build(self._base_state(
            tool_history=["outline_generation"],
        ))
        assert "工具反馈" in result
        assert "outline_generation" in result

    # ── 多工具输出 ───────────────────────────────────────────────

    def test_multiple_tool_outputs(self):
        """多次工具调用结果都在 prompt 中"""
        self.memory.store("tool_respond", "requirements_analysis", "需求：科幻悬疑，30000字")
        self.memory.store("tool_respond", "outline_generation", "大纲：第一章…")
        result = self.engine.build(self._base_state(
            tool_history=["requirements_analysis", "outline_generation"],
        ))
        assert "工具反馈" in result
        assert "2 条" in result

    # ── HistoryProvider ───────────────────────────────────────────

    def test_history_appears_in_prompt(self):
        """写入 history 后，对话历史出现在 prompt"""
        self.memory.store("agent_history", "turn_1", "用户：写一篇小说")
        result = self.engine.build(self._base_state())
        assert "对话历史" in result
        assert "写一篇小说" in result

    # ── Slot enable/disable ──────────────────────────────────────

    def test_disabled_slot_is_skipped(self):
        """禁用 slot 后对应 provider 不注入"""
        ToolOutputProvider.disable()
        try:
            self.memory.store("tool_respond", "outline_generation", "大纲内容")
            result = self.engine.build(self._base_state(
                tool_history=["outline_generation"],
            ))
            assert "工具反馈" not in result
        finally:
            ToolOutputProvider.enable()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

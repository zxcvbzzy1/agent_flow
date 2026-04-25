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
    UserPromptProvider, StateProvider, ToolRespondProvider,
    StoredContextProvider, AvailableToolsProvider, HistoryProvider,
)
from domain.context.store.store import ContextStore
from domain.context.store.node import ContextNode


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

    def test_export_class_generation(self, tmp_path):
        """测试静态代码导出功能"""
        export_path = tmp_path / "static_events.py"
        self.factory.export_class(str(export_path))

        assert export_path.exists()
        content = export_path.read_text(encoding="utf-8")
        assert "StaticToolEventFactory" in content
        assert "rag_search" in content


class TestWriteAgentCore:
    """WriteAgent 核心逻辑单元测试"""

    def setup_method(self):
        Tool._registry.clear()
        self.mock_llm = AsyncMock()
        # 创建最简 context engine
        self.store = ContextStore(token_limit=100000)
        self.engine = ContextEngine(
            providers=[UserPromptProvider(), StateProvider()],
            context_store=self.store,
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
    """ContextEngine 核心链路测试：store.write → window → provider.get → engine.build"""

    def setup_method(self):
        self.store = ContextStore(token_limit=100000)

        self.providers = [
            UserPromptProvider(),
            StateProvider(),
            HistoryProvider(self.store),
            ToolRespondProvider(self.store),
            StoredContextProvider(self.store),
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

    def test_short_output_creates_summary_node(self):
        """短工具输出（≤800字符）应生成 promoted 的 summary 节点"""
        short_text = "简单的工具输出结果"
        nodes = asyncio.run(
            self.store.write("tool:short_tool#1", short_text, scope="memory")
        )
        assert len(nodes) == 1
        assert nodes[0].node_type == "summary"
        assert nodes[0].promoted is True

        window = self.store.window(scope="memory")
        assert len(window) == 1
        assert window[0].content == short_text

    def test_long_output_creates_summary_and_chunks(self):
        """长工具输出应生成 summary(promoted) + chunks(stored)"""
        long_text = "这是一段很长的文本。" * 200  # ~1800 字符
        nodes = asyncio.run(
            self.store.write("tool:long_tool#1", long_text, scope="memory")
        )
        types = {n.node_type for n in nodes}
        assert "summary" in types
        assert "chunk" in types

        # summary 应该 promoted，chunk 不应该
        summary = next(n for n in nodes if n.node_type == "summary")
        assert summary.promoted is True
        chunks = [n for n in nodes if n.node_type == "chunk"]
        assert all(not c.promoted for c in chunks)

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
        ToolRespondProvider.disable()
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
            ToolRespondProvider.enable()

    # ── on_tool_call 模拟 ────────────────────────────────────────

    def test_on_tool_call_writes_to_store(self):
        """模拟 on_tool_call 的 store.write 流程，验证数据流完整"""
        tool_name = "requirements_analysis"
        respond = "分析结果：科幻小说，30000字"

        # 模拟 on_tool_call 中的 store.write 流程
        store = self.store
        source_key = f"tool:{tool_name}#{store.count(f'tool:{tool_name}#')}"
        asyncio.run(store.write(
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

    # ── query（替代 ShortTermMemory）──────────────────────────────

    def test_query_returns_full_content(self):
        """query() 返回 source_key 的完整内容"""
        asyncio.run(self.store.write(
            "tool:my_tool#1",
            "完整输出内容",
            scope="memory",
        ))
        result = self.store.query("tool:my_tool#1")
        assert result == "完整输出内容"

    def test_query_long_content_chunks_concatenated(self):
        """query() 对长内容返回 chunk 拼接"""
        long_text = "这是一段很长的文本。" * 200
        asyncio.run(self.store.write(
            "tool:long_tool#1",
            long_text,
            scope="memory",
        ))
        result = self.store.query("tool:long_tool#1")
        assert result == long_text

    def test_latest_source_key(self):
        """latest_source_key 返回最近写入的 source_key"""
        asyncio.run(self.store.write("tool:my_tool#1", "a", scope="memory"))
        asyncio.run(self.store.write("tool:my_tool#2", "b", scope="memory"))
        assert self.store.latest_source_key("my_tool") == "tool:my_tool#2"

    # ── explore / dismiss ────────────────────────────────────────

    def test_explore_promotes_chunk(self):
        """explore() 将暂存的 chunk promote 进窗口"""
        long_text = "这是一段很长的文本。" * 200
        asyncio.run(self.store.write("tool:long_tool#1", long_text, scope="memory"))

        # 初始状态：chunk 不在窗口中
        window_before = self.store.window(scope="memory")
        chunk_nodes = [n for n in window_before if n.node_type == "chunk"]
        assert len(chunk_nodes) == 0

        # explore 后：chunk 进入窗口
        self.store.explore("tool:long_tool#1", chunk_index=0)
        window_after = self.store.window(scope="memory")
        chunk_nodes = [n for n in window_after if n.node_type == "chunk"]
        assert len(chunk_nodes) == 1
        assert chunk_nodes[0].chunk_index == 0

    def test_dismiss_removes_from_window(self):
        """dismiss() 将指定 source_key 从窗口移到暂存池"""
        asyncio.run(self.store.write("tool:my_tool#1", "内容", scope="memory"))

        # 初始在窗口中
        nodes_before = self.store.window(scope="memory")
        my_tool_in_window = any(n.source_key == "tool:my_tool#1" for n in nodes_before)
        assert my_tool_in_window

        self.store.dismiss("tool:my_tool#1")
        nodes_after = self.store.window(scope="memory")
        my_tool_in_window = any(n.source_key == "tool:my_tool#1" for n in nodes_after)
        assert not my_tool_in_window
        # 暂存池有内容
        assert len(self.store.list_stored(scope="memory")) > 0

    # ── list_stored ──────────────────────────────────────────────

    def test_list_stored_shows_stored_content(self):
        """list_stored() 返回暂存池目录"""
        long_text = "这是一段很长的文本。" * 200
        asyncio.run(self.store.write("tool:long_tool#1", long_text, scope="memory"))

        stored = self.store.list_stored(scope="memory")
        assert len(stored) == 1
        assert stored[0]["source_key"] == "tool:long_tool#1"
        assert stored[0]["total_chunks"] > 0

    # ── StoredContextProvider ─────────────────────────────────────

    def test_stored_provider_shows_stored_content(self):
        """StoredContextProvider 展示暂存池目录"""
        long_text = "这是一段很长的文本。" * 200
        asyncio.run(self.store.write("tool:long_tool#1", long_text, scope="memory"))

        result = self.engine.build(self._base_state())
        assert "暂存内容" in result

    # ── token budget / demote ────────────────────────────────────

    def test_demote_on_budget_exceeded(self):
        """token 超限时自动 demote 低优先级节点"""
        small_store = ContextStore(token_limit=50)
        text = "x" * 400  # ~100 tokens
        asyncio.run(small_store.write("tool:big#1", text, scope="memory"))

        # 短输出是 summary 节点，超限后会被 demote
        window = small_store.window(scope="memory")
        assert len(window) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

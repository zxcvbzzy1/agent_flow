"""
集成测试文件：测试 WriteAgent 和 ToolEventFactory 的核心功能
"""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock
from domain.agent.write.writeAgent import WriteAgent
from domain.state import Agent_state
from domain.tool import Tool
from domain.event import ToolEventFactory, WriteAgentEvent


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
        self.mock_llm = AsyncMock()
        self.agent = WriteAgent(
            id="wa_001",
            name="NovelWriter",
            llm=self.mock_llm
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

    def test_build_prompts_components(self):
        """测试各个 Prompt 组件的构建"""
        self.agent.states["prompt"] = "写一篇科幻"
        
        agent_prompt = self.agent._build_agent_prompt()
        assert "写作 Agent" in agent_prompt
        
        user_msg = self.agent._build_user_message()
        assert "写一篇科幻" in user_msg
        
        summary = self.agent._build_state_summary()
        assert "当前执行状态" in summary

    def test_step_logic_finished(self):
        """测试 _step 在任务完成时的返回逻辑"""
        self.mock_llm.chat.return_value = json.dumps({
            "think": "完成了",
            "tool_calls": [],
            "is_finished": True,
            "finish_reason": "质量达标"
        })
        
        # 注意：_step 是 async 的
        result = asyncio.run(self.agent._step())
        
        assert result is True
        assert self.agent.states["is_finished"] is True

    def test_step_logic_tool_call(self):
        """测试 _step 在需要调用工具时的返回逻辑"""
        self.mock_llm.chat.return_value = json.dumps({
            "think": "调用工具",
            "tool_calls": [{"tool_name": "test", "arguments": {}}],
            "is_finished": False
        })
        
        result = asyncio.run(self.agent._step())
        
        assert result is False


class TestWriteAgentEvents:
    """WriteAgentEvent 静态事件测试"""

    def test_process_start_event(self):
        """测试流程开始事件"""
        event = WriteAgentEvent.ProcessStart(session_id="123")
        assert event.name == "write.process.start"
        assert event.payload["session_id"] == "123"

    def test_outline_events(self):
        """测试大纲相关事件"""
        req_event = WriteAgentEvent.OutlineRequested(topic="悬疑")
        done_event = WriteAgentEvent.OutlineDone(outline_text="...")
        
        assert req_event.name == "outline.requested"
        assert done_event.name == "outline.done"

    def test_judge_decision_events(self):
        """测试评判后的路由事件"""
        polish_event = WriteAgentEvent.StylePolishRequested(text="...")
        rewrite_event = WriteAgentEvent.RewriteRequested(reason="分数太低")
        
        assert polish_event.name == "style.polish.requested"
        assert rewrite_event.name == "rewrite.requested"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
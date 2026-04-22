# ---------------------------------------------------------------------------
# 自动生成的工具事件工厂代码，请勿手动修改
# ---------------------------------------------------------------------------
from typing import Any, Literal, overload
from domain.event import ToolEventFactory, ToolEventSpec, Event, EventBusPort

class StaticToolEventFactory(ToolEventFactory):
    _bus: EventBusPort = None

    @classmethod
    def _resigister_bus(self, bus: EventBusPort):
        """注入全局事件总线实例"""
        for spec in self._specs.values():
            spec.set_bus(bus)

    def __init__(self, prefix: str = "") -> None:
        super().__init__(prefix=prefix)
        self._specs = {
            "read_files": ToolEventSpec(
                tool_name="read_files",
                tool_field="system",
                tool_input_schema={"type": "object", "properties": {"file_path": {"type": "array", "description": "要读取的文件路径列表", "items": {"type": "string", "description": "单个文件的路径"}}}, "required": ["file_path"]},
                _events={"called": "infra.system.read.files.called", "succeeded": "infra.system.read.files.succeeded", "failed": "infra.system.read.files.failed", "retrying": "infra.system.read.files.retrying"}
            ),
            "write_files": ToolEventSpec(
                tool_name="write_files",
                tool_field="system",
                tool_input_schema={"type": "object", "properties": {"file_path": {"type": "string", "description": "要写入的文件路径"}, "content": {"type": "string", "description": "要写入的文件内容"}}},
                _events={"called": "infra.system.write.files.called", "succeeded": "infra.system.write.files.succeeded", "failed": "infra.system.write.files.failed", "retrying": "infra.system.write.files.retrying"}
            ),
            "rag_search": ToolEventSpec(
                tool_name="rag_search",
                tool_field="search",
                tool_input_schema={"type": "object", "properties": {"query": {"type": "string", "description": "用户问题的概述，保留问题里与文档要求相关的部分"}}, "required": ["query"]},
                _events={"called": "infra.search.rag.search.called", "succeeded": "infra.search.rag.search.succeeded", "failed": "infra.search.rag.search.failed", "retrying": "infra.search.rag.search.retrying"}
            ),
            "memory_query": ToolEventSpec(
                tool_name="memory_query",
                tool_field="memory",
                tool_input_schema={"type": "object", "properties": {"query": {"type": "string", "description": "检索关键词或问题描述，用于在记忆库中匹配相关信息"}}, "required": ["query"]},
                _events={"called": "infra.memory.memory.query.called", "succeeded": "infra.memory.memory.query.succeeded", "failed": "infra.memory.memory.query.failed", "retrying": "infra.memory.memory.query.retrying"}
            ),
            "save_short_term_memory": ToolEventSpec(
                tool_name="save_short_term_memory",
                tool_field="memory",
                tool_input_schema={"type": "object", "properties": {"content": {"type": "string", "description": "需要临时序列化存储的内容或数据字符串"}}, "required": ["content"]},
                _events={"called": "infra.memory.save.short.term.memory.called", "succeeded": "infra.memory.save.short.term.memory.succeeded", "failed": "infra.memory.save.short.term.memory.failed", "retrying": "infra.memory.save.short.term.memory.retrying"}
            ),
            "save_long_term_memory": ToolEventSpec(
                tool_name="save_long_term_memory",
                tool_field="memory",
                tool_input_schema={"type": "object", "properties": {"category": {"type": "string", "description": "记忆的分类，例如 'user_preference', 'project_knowledge'等"}, "information": {"type": "string", "description": "需要永久记住的具体信息内容"}}, "required": ["category", "information"]},
                _events={"called": "infra.memory.save.long.term.memory.called", "succeeded": "infra.memory.save.long.term.memory.succeeded", "failed": "infra.memory.save.long.term.memory.failed", "retrying": "infra.memory.save.long.term.memory.retrying"}
            ),
            "confirm_human": ToolEventSpec(
                tool_name="confirm_human",
                tool_field="human",
                tool_input_schema={"type": "object", "properties": {"query": {"type": "string", "description": "向用户确认的问题"}}, "required": ["query"]},
                _events={"called": "infra.human.confirm.human.called", "succeeded": "infra.human.confirm.human.succeeded", "failed": "infra.human.confirm.human.failed", "retrying": "infra.human.confirm.human.retrying"}
            ),
            "summary": ToolEventSpec(
                tool_name="summary",
                tool_field="summary",
                tool_input_schema={"type": "object", "properties": {"text": {"type": "string", "description": "代总结的文本信息"}}, "required": ["text"]},
                _events={"called": "infra.summary.summary.called", "succeeded": "infra.summary.summary.succeeded", "failed": "infra.summary.summary.failed", "retrying": "infra.summary.summary.retrying"}
            ),
        }

    @overload
    def tool(self, tool_name: Literal["read_files"]) -> ToolEventSpec:
        """
        获取工具 `read_files` 的事件描述符。
        
        **输入参数 Schema 提示**:
        ```json
        {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "array",
                    "description": "要读取的文件路径列表",
                    "items": {
                        "type": "string",
                        "description": "单个文件的路径"
                    }
                }
            },
            "required": [
                "file_path"
            ]
        }
        ```
        """
        ...

    @overload
    def tool(self, tool_name: Literal["write_files"]) -> ToolEventSpec:
        """
        获取工具 `write_files` 的事件描述符。
        
        **输入参数 Schema 提示**:
        ```json
        {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "要写入的文件路径"
                },
                "content": {
                    "type": "string",
                    "description": "要写入的文件内容"
                }
            }
        }
        ```
        """
        ...

    @overload
    def tool(self, tool_name: Literal["rag_search"]) -> ToolEventSpec:
        """
        获取工具 `rag_search` 的事件描述符。
        
        **输入参数 Schema 提示**:
        ```json
        {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "用户问题的概述，保留问题里与文档要求相关的部分"
                }
            },
            "required": [
                "query"
            ]
        }
        ```
        """
        ...

    @overload
    def tool(self, tool_name: Literal["memory_query"]) -> ToolEventSpec:
        """
        获取工具 `memory_query` 的事件描述符。
        
        **输入参数 Schema 提示**:
        ```json
        {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "检索关键词或问题描述，用于在记忆库中匹配相关信息"
                }
            },
            "required": [
                "query"
            ]
        }
        ```
        """
        ...

    @overload
    def tool(self, tool_name: Literal["save_short_term_memory"]) -> ToolEventSpec:
        """
        获取工具 `save_short_term_memory` 的事件描述符。
        
        **输入参数 Schema 提示**:
        ```json
        {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "需要临时序列化存储的内容或数据字符串"
                }
            },
            "required": [
                "content"
            ]
        }
        ```
        """
        ...

    @overload
    def tool(self, tool_name: Literal["save_long_term_memory"]) -> ToolEventSpec:
        """
        获取工具 `save_long_term_memory` 的事件描述符。
        
        **输入参数 Schema 提示**:
        ```json
        {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "记忆的分类，例如 'user_preference', 'project_knowledge'等"
                },
                "information": {
                    "type": "string",
                    "description": "需要永久记住的具体信息内容"
                }
            },
            "required": [
                "category",
                "information"
            ]
        }
        ```
        """
        ...

    @overload
    def tool(self, tool_name: Literal["confirm_human"]) -> ToolEventSpec:
        """
        获取工具 `confirm_human` 的事件描述符。
        
        **输入参数 Schema 提示**:
        ```json
        {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "向用户确认的问题"
                }
            },
            "required": [
                "query"
            ]
        }
        ```
        """
        ...

    @overload
    def tool(self, tool_name: Literal["summary"]) -> ToolEventSpec:
        """
        获取工具 `summary` 的事件描述符。
        
        **输入参数 Schema 提示**:
        ```json
        {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "代总结的文本信息"
                }
            },
            "required": [
                "text"
            ]
        }
        ```
        """
        ...

    @overload
    def tool(self, tool_name: str) -> ToolEventSpec: 
        """
        不支持的工具名称。
        """
        ...

    def tool(self, tool_name: str) -> ToolEventSpec:
        """运行时的实际调用逻辑"""
        return super().tool(tool_name)

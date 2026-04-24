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
        return self

    def __init__(self, prefix: str = "") -> None:
        super().__init__(prefix=prefix)
        self._specs = {
            "rag_search": ToolEventSpec(
                tool_name="rag_search",
                tool_field="search",
                tool_input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
                _events={"called": "test.search.rag.search.called", "succeeded": "test.search.rag.search.succeeded", "failed": "test.search.rag.search.failed", "retrying": "test.search.rag.search.retrying"}
            ),
            "outline_generator": ToolEventSpec(
                tool_name="outline_generator",
                tool_field="write_agent",
                tool_input_schema={"type": "object", "properties": {"topic": {"type": "string"}}},
                _events={"called": "test.write.agent.outline.generator.called", "succeeded": "test.write.agent.outline.generator.succeeded", "failed": "test.write.agent.outline.generator.failed", "retrying": "test.write.agent.outline.generator.retrying"}
            ),
        }

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
                    "type": "string"
                }
            }
        }
        ```
        """
        ...

    @overload
    def tool(self, tool_name: Literal["outline_generator"]) -> ToolEventSpec:
        """
        获取工具 `outline_generator` 的事件描述符。
        
        **输入参数 Schema 提示**:
        ```json
        {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string"
                }
            }
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

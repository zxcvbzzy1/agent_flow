

from dataclasses import asdict, dataclass, field
import importlib
import json
import os
import re
import sys
from typing import Any, ClassVar, Dict, List

from domain.state import Agent_state
from domain.tool import Tool, Tool_respond

from abc import ABC, abstractmethod


# ---------------------------------------------------------------------------
# 基本事件定义格式
# ---------------------------------------------------------------------------
@dataclass
class Event:

    name: str
    payload: Dict[str, Any] = field(default_factory=dict)

    def unpack(self) -> dict:
        """
        统一解构 payload，无论是 dict、dataclass 还是其他对象。
        """
        p = self.payload

        if isinstance(p, dict):
            return p

        # dataclass 对象（Tool_respond 等）
        if hasattr(p, '__dataclass_fields__'):
            return asdict(p)

        # 普通对象，取公开属性
        if hasattr(p, '__dict__'):
            return {k: v for k, v in p.__dict__.items() if not k.startswith('_')}

        # 其他（str、int 等基础类型）
        return {"value": p}

# ---------------------------------------------------------------------------
# 事件总线抽象接口
# ---------------------------------------------------------------------------

class EventBusPort(ABC):
    """
    事件总线的抽象接口（端口）。
    domain 层只依赖这个抽象，不知道任何具体实现。
    """

    @abstractmethod
    async def publish_one(self, event: "Event") -> Any:
        ...

    @abstractmethod
    async def publish(self, event: "Event") -> list[Any]:
        ...

    @abstractmethod
    def subscribe(self, event_name: str, handler) -> None:
        ...


# ---------------------------------------------------------------------------
# 单工具事件定义格式
# ---------------------------------------------------------------------------
@dataclass
class ToolEventSpec:
    """
    一个工具对应的全部执行事件。
    事件名格式：{prefix}.{field}.{tool_dot_name}.{suffix}
    """
    tool_name:  str
    tool_field: str | None
    tool_input_schema: dict[str, Any]
    _events: dict[str, str] = field(default_factory=dict)  # suffix -> event_name
    _bus: "EventBusPort | None" = field(
        default=None,
        init=False,
        repr=False,
        compare=False
    )
 
    def set_bus(self, bus: EventBusPort) -> None:
        """由外部注入，domain 层自己不创建 bus。"""
        self._bus = bus

    def _get_bus(self) -> EventBusPort:
        if self._bus is None:
            raise RuntimeError(
                f"工具 '{self.tool_name}' 的 EventBus 未注入，"
                "请在初始化时调用 spec.set_bus(bus)"
            )
        return self._bus

    # ── 调用接口 ─────────────────────────────────────────────────────
    # ── 事件生成 ─────────────────────────────────────────────────────
    
    def called(self, arguments: dict | None = None) -> Event:
        """EDA 收到工具调用请求。"""
        return self._emit("called", arguments)
 
    def succeeded(self, respond: "Tool_respond") -> Event:
        """工具执行成功。"""
        return self._emit("succeeded",respond)
 
    def failed(self, respond: "Tool_respond") -> Event:
        """工具执行失败。"""
        return self._emit("failed", respond)
 
    def retrying(self, arguments: dict | None = None) -> Event:
        """工具失败后重试。"""
        return self._emit("retrying", arguments)
 
    # ── 事件发布 ─────────────────────────────────────────────────────
    async def emit_called(self, arguments: dict | None = None):
        return await self._get_bus().publish_one(self._emit("called", arguments or {}))

    async def emit_succeeded(self, respond: "Tool_respond"):
        return await self._get_bus().publish_one(self._emit("succeeded", respond))

    async def emit_failed(self, respond: "Tool_respond"):
        return await self._get_bus().publish_one(self._emit("failed", respond))

    async def emit_retrying(self, arguments: dict | None = None):
        return await self._get_bus().publish_one(self._emit("retrying", arguments or {}))


    # ── 内部 ─────────────────────────────────────────────────────────
 
    def _emit(self, suffix: str, payload: dict) -> Event:
        event_name = self._events.get(suffix)
        if event_name is None:
            raise AttributeError(
                f"工具 '{self.tool_name}' 没有 '{suffix}' 事件。"
                f"可用: {list(self._events.keys())}"
            )
        return Event(name=event_name, payload=payload)
 
    def all_event_names(self) -> list[str]:
        return sorted(self._events.values())
 
    def get_tool_input_schema(self) -> dict[str, Any]:
        return self.tool_input_schema[self.tool_name]

 
# ---------------------------------------------------------------------------
# 工具事件工厂
# ---------------------------------------------------------------------------
 
class ToolEventFactory:
    """
    从 Tool._registry 自动构建所有工具的事件描述符。
 
    用法：
        动态构建
            factory= ToolEventFactory(prefix="infra")._build()
        静态构建
            factory = ToolEventFactory(prefix="infra").export_class("./domain/tools/static_tools.py")
        
        event   = factory.tool("rag_search").called({"query": "AI趋势"})
        factory.export_class("generated_tool_events.py")
    """
 
    # 标准四类事件后缀，对所有工具统一
    _SUFFIXES = ["called", "succeeded", "failed", "retrying"]
    _instance = None
    _instance_bool = False
    
    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, prefix: str = "") -> None:
        if self.__class__._instance_bool:
            return
        self._prefix = prefix.rstrip(".") + "." if prefix else ""
        self._specs: dict[str, ToolEventSpec] = {}
        self.__class__._instance_bool = True
        # self._build()
 
    # ── 外部接口 ─────────────────────────────────────────────────────
 
    def tool(self, tool_name: str) -> ToolEventSpec:
        """返回指定工具的事件描述符。"""
        if tool_name not in self._specs:
            raise KeyError(
                f"工具 '{tool_name}' 未注册。"
                f"已知工具: {list(self._specs.keys())}"
            )
        return self._specs[tool_name]
 
    def by_field(self, field_name: str) -> list[ToolEventSpec]:
        """返回指定 field 下的所有工具事件描述符，方便按分组订阅。"""
        return [s for s in self._specs.values() if s.tool_field == field_name]
 
    def all_events(self) -> list[str]:
        """全部事件名，用于批量注册到事件总线。"""
        names: set[str] = set()
        for spec in self._specs.values():
            names.update(spec.all_event_names())
        return sorted(names)
 
    def events_by_field(self) -> dict[str, list[str]]:
        """按 field 分组的事件名，方便 EDA 分组订阅。"""
        result: dict[str, list[str]] = {}
        for spec in self._specs.values():
            key = spec.tool_field or "unknown"
            result.setdefault(key, []).extend(spec.all_event_names())
        for v in result.values():
            v.sort()
        return result
  


    # ── 构建注册工具的ToolEventSpec事件类型─────────────────────────────────────────────────────────
    def _build(self) -> None:
        for tool in Tool.get_all_tools():
            field_seg = _normalize(tool.field) if tool.field else "unknown"
            tool_seg  = _to_dot_name(tool.name)
            base      = f"{self._prefix}{field_seg}.{tool_seg}"
            
            spec = ToolEventSpec(
                tool_name=tool.name,
                tool_field=tool.field,
                tool_input_schema=tool.input_schema,
            )
            for suffix in self._SUFFIXES:
                spec._events[suffix] = f"{base}.{suffix}"
 
            self._specs[tool.name] = spec
        return self
            
    def export_class(self,bus:EventBusPort, filepath: str = "generated_tool_events.py") -> None:
        """
        静态导出当前所有注册工具的事件工厂类，包含基于 Literal 的代码提示和 Schema 文档。
        """
        if not self._specs:
            self._build()

        lines = [
            '# ---------------------------------------------------------------------------',
            '# 自动生成的工具事件工厂代码，请勿手动修改',
            '# ---------------------------------------------------------------------------',
            'from typing import Any, Literal, overload',
            'from domain.event import ToolEventFactory, ToolEventSpec, Event, EventBusPort',
            '',
            'class StaticToolEventFactory(ToolEventFactory):',
            '    _bus: EventBusPort = None',
            '',
            '    @classmethod',
            '    def _resigister_bus(self, bus: EventBusPort):',
            '        """注入全局事件总线实例"""',
            '        for spec in self._specs.values():',
            '            spec.set_bus(bus)',
            '        return self',
            '',
            '    def __init__(self, prefix: str = "") -> None:',
            '        super().__init__(prefix=prefix)',
            '        self._specs = {'
        ]

        # 1. 静态构建字典，避免运行时反射
        for tool_name, spec in self._specs.items():
            schema_str = json.dumps(spec.tool_input_schema, ensure_ascii=False)
            events_str = json.dumps(spec._events, ensure_ascii=False)
            
            lines.append(f'            "{tool_name}": ToolEventSpec(')
            lines.append(f'                tool_name="{spec.tool_name}",')
            lines.append(f'                tool_field="{spec.tool_field}",')
            lines.append(f'                tool_input_schema={schema_str},')
            lines.append(f'                _events={events_str}')
            lines.append( '            ),')
            
        lines.append('        }')
        lines.append('')

        # 2. 生成带 Schema 提示的 @overload
        for tool_name, spec in self._specs.items():
            # 将 Schema 格式化为漂亮的 JSON 字符串，方便在 IDE 悬停时阅读
            schema_pretty = json.dumps(spec.tool_input_schema, ensure_ascii=False, indent=4)
            # 处理缩进以适应 docstring
            schema_indented = "\n".join(f"        {line}" for line in schema_pretty.split("\n"))

            lines.append('    @overload')
            lines.append(f'    def tool(self, tool_name: Literal["{tool_name}"]) -> ToolEventSpec:')
            lines.append('        """')
            lines.append(f'        获取工具 `{tool_name}` 的事件描述符。')
            lines.append('        ')
            lines.append('        **输入参数 Schema 提示**:')
            lines.append('        ```json')
            lines.append(f'{schema_indented}')
            lines.append('        ```')
            lines.append('        """')
            lines.append('        ...')
            lines.append('')

        # 3. 添加默认重载和实际实现
        lines.append('    @overload')
        lines.append('    def tool(self, tool_name: str) -> ToolEventSpec: ')
        lines.append('        """')
        lines.append('        不支持的工具名称。')
        lines.append('        """')
        lines.append('        ...')
        lines.append('')
        lines.append('    def tool(self, tool_name: str) -> ToolEventSpec:')
        lines.append('        """运行时的实际调用逻辑"""')
        lines.append('        return super().tool(tool_name)')
        lines.append('')

        with open(filepath, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        
        print(f"✅ 静态事件工厂类已成功导出至: {filepath}")

        module_name = os.path.splitext(os.path.basename(filepath))[0]
        # --- 核心改进：清理旧缓存 ---
        if module_name in sys.modules:
            del sys.modules[module_name]
        # --------------------------
        spec = importlib.util.spec_from_file_location(module_name, filepath)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        # 返回生成的类
        return module.StaticToolEventFactory(prefix=self._prefix)

    def _resigister_bus(self, bus: EventBusPort):
        for spec in self._specs.values():
            spec.set_bus(bus)
        return self
    

def _normalize(s: str) -> str:
    """field 名标准化：write_agent -> write.agent"""
    return s.replace("_", ".")
 
 
def _to_dot_name(tool_name: str) -> str:
    """tool name -> 点分名：rag_search -> rag.search"""
    return tool_name.replace("_", ".")
 
def _to_pascal(s: str) -> str:
    return "".join(w.capitalize() for w in re.split(r"[_\s]+", s))


from dataclasses import dataclass,field
import json
from typing import Any, Callable, ClassVar, Dict, List, Literal, Optional

ToolField = Literal[
    "system",  # 系统"
    "search",      # 查询
    "memory",  # 记忆
    "human",       # 人机协作
    "write_agent"   # 代理特有工具
]

# 工具返回格式
@dataclass(frozen=True)
class Tool_respond:
    agent_id:str
    name:str
    success:bool
    respond:Optional[any]
    

# 工具LLM 决策定义格式
@dataclass(frozen=True)
class Tool:
    _registry: ClassVar[List['Tool']] = []
    _registry_dict: ClassVar[Dict[str, 'Tool']] = {}

    name:        str
    description: str
    field:Optional[ToolField]
    input_schema: dict[str, Any]  = field(default_factory=dict)   
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        Tool._registry.append(self)
        Tool._registry_dict[self.name] = self

    @classmethod
    def get_all_tools(cls) -> List['Tool']:
        """获取所有已实例化的工具"""
        return cls._registry
    
    @classmethod
    def get_tools_by_metadata(cls, key: str, value: Any) -> List['Tool']:
        """根据元数据筛选工具"""
        return [tool for tool in cls._registry if tool.metadata.get(key) == value]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "metadata": self.metadata,
            "field": self.field
        }

    def to_cypher_props(self) -> str:
        """序列化为 Neo4j 属性字符串（嵌套在节点属性内）。"""
        return json.dumps(self.to_dict(), ensure_ascii=False)



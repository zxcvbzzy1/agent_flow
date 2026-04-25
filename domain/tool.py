

from dataclasses import dataclass,field
import json
from typing import Any, Callable, ClassVar, Dict, List, Literal, Optional

ToolField = Literal[
    "system",  # 系统"
    "search",      # 查询
    "memory",  # 记忆
    "human",       # 人机协作
    "summary",  # 总结
    "plan",    # 计划
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



# 系统
READ_FILES = Tool(
    name        = "read_files",
    description = "读取系统目录中的文件（包括用户上传文件）,返回文件内容",
    input_schema = {
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
        "required": ["file_path"]
    },
    field="system"
)

WRITE_FILES = Tool(
    name        = "write_files",
    description = "将内容写入系统目录中的文件,返回文件目录。",
    input_schema = {
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
    },
    field="system"
)


# 搜索
RAG_SEARCH = Tool( 
    name        = "rag_search",
    description = "检索存储到知识库里的文档。当用户提及根据上传的文档时使用，一般不使用",
    input_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type":        "string",
                "description": "用户问题的概述，保留问题里与文档要求相关的部分"
            }
        },
        "required": ["query"]
    },
    field="search"
    )





# 记忆

# QUERY_TOOL_RESPOND = Tool(
#     name="query_tool_respond",
#     description="查询本次任务中历史工具调用的完整反馈内容。当需要回溯某个工具的执行结果时，或者查看被截断的工具执行结果时使用。",
#     input_schema={
#         "type": "object",
#         "properties": {
#             "tool_name": {
#                 "type": "string",
#                 "description": "要查询的工具名称"
#             },
#             "index": {
#                 "type": "integer",
#                 "description": "第几次调用的结果，从1开始，不填则返回最后一次",
#             }
#         },
#         "required": ["tool_name","index"]
#     },
#     field="memory"
# )


# MEMORY_QUERY = Tool(
#     name        = "memory_query",
#     description = "查询用户的历史偏好、重要事实、过往对话记录、工具调用的具体反馈。当需要回顾之前提到的信息时使用。",
#     input_schema = {
#         "type": "object",
#         "properties": {
#             "query": {
#                 "type":        "string",
#                 "description": "检索关键词或问题描述，用于在记忆库中匹配相关信息"
#             }
#         },
#         "required": ["query"]
#     },
#     field = "memory"
# )

# # 短期记忆存储工具：存储到临时文件（Session级别）
# SAVE_SHORT_TERM_MEMORY = Tool(
#     name        = "save_short_term_memory",
#     description = "将当前任务的中间状态、工具反馈存入缓存文件。这些信息仅在当前会话有效，适合存储复杂或者文本过长的工具反馈内容，可跟其他工具一同调用。",
#     input_schema = {
#         "type": "object",
#         "properties": {
#             "content": {
#                 "type":        "string",
#                 "description": "需要临时序列化存储的内容或数据字符串"
#             }
#         },
#         "required": ["content"]
#     },
#     field = "memory"
# )

# # 3长期记忆存储工具：存储到数据库（持久化）
# SAVE_LONG_TERM_MEMORY = Tool(
#     name        = "save_long_term_memory",
#     description = "将重要的用户偏好、长期事实或项目背景信息持久化保存到数据库。即使会话结束，这些信息也会被保留。",
#     input_schema = {
#         "type": "object",
#         "properties": {
#             "category": {
#                 "type":        "string",
#                 "description": "记忆的分类，例如 'user_preference', 'project_knowledge'等"
#             },
#             "information": {
#                 "type":        "string",
#                 "description": "需要永久记住的具体信息内容"
#             }
#         },
#         "required": ["category", "information"]
#     },
#     field = "memory"
# )


#人工
CONFIRM_HUNMAN = Tool(
    name        = "confirm_human",
    description = "用户问题指向不确定时，向用户进行确认进一步的细节。",
    input_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type":        "string",
                "description": "向用户确认的问题"
            },
        },
        "required": ["query"]
    },
    field = "human"
)

# 总结
SUMMARY = Tool(
    name        = "summary",
    description = "当字数过多时，将内容进行总结",
    input_schema = {
        "type": "object",
        "properties": {
            "text": {
                "type":        "string",
                "description": "代总结的文本信息"
            },
        },
        "required": ["text"]
    },
    field = "summary"
)

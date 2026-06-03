"""
所有 ContextProvider 的具体实现。

分两类：
  静态 Provider  —— 数据来自 state dict，不依赖上下文管理
  动态 Provider  —— 数据来自记忆，由上下文管理

Provider 只格式化，不做任何存储或管理决策。
"""

from __future__ import annotations
from abc import ABC, abstractmethod

from domain.context.strategy import ContextStrategy, FullHistoryStrategy, ConsumeOnceStrategy, ContextItem
from domain.memory.short.default_short_term_memory import ShortTermMemory
from domain.memory.short.short_term_memory import memory_field
import json
from domain.tool import Tool

# ── Provider 基类 ────────────────────────────────────────────────────

class ContextProvider(ABC):
    name:    str
    enabled: bool = True

    @classmethod
    def disable(cls): cls.enabled = False
    @classmethod
    def enable(cls):  cls.enabled = True

    @abstractmethod
    def get(self, state: dict) -> list[str]:
        ...


# ── 动态需要 memory 的 Provider 基类 ────────────────────────────────

class MemoryProvider(ContextProvider, ABC):
    """从 ShortTermMemory 经 Strategy 取 items，再格式化。"""

    def __init__(
        self,
        memory:   ShortTermMemory,
        field:    memory_field,
        strategy: ContextStrategy | None = None,
    ) -> None:
        self._memory   = memory
        self._field    = field
        self._strategy = strategy or FullHistoryStrategy()

    def _get_items(self, state: dict) -> list[ContextItem]:
        return self._strategy.apply(self._memory, self._field, state)


# ── 具体 Provider ─────────────────────────────────────────────────

# 静态的provider

class UserPromptProvider(ContextProvider):
    """任务入口，注入用户原始需求。"""
    name = "user"

    def get(self, state: dict) -> list[str]:
        text = (
            f"请开始处理以下需求：\n"
            f"用户需求：{state.get('prompt', '')}\n"
        )
        return [text]


class StateProvider(ContextProvider):
    """当前执行状态：重试次数、工具调用历史、失败提示。"""
    name = "task"

    def get(self, state: dict) -> list[str]:
        parts = ["## 当前执行状态"]
        if state.get("retry", 0) > 0:
            parts.append(f"- 已重试：{state['retry']} 次")
        if not state.get("last_tool_ok", True):
            parts.append("- 上一个工具执行失败，请决定是否重试或换其他工具")
        if state.get("tool_history"):
            parts.append(f"- 已调用工具：{' -> '.join(state['tool_history'])}")
        return ["\n".join(parts)]


class AvailableToolsProvider(ContextProvider):
    name = "available_tools"

    def __init__(
        self,
        available_fields: list[str] | None = None,
        available_tools: list[str] | None = None,
    ) -> None:
        # 按 field 分组粗选，或按具体工具名细选；二者取并集。
        self._fields = list(available_fields or [])
        self._tools = list(available_tools or [])

    def get(self, state: dict) -> list[str]:

        lines = ["当前可用工具："]
        for tool in Tool.get_all_tools():
            if tool.field in self._fields or tool.name in self._tools:
                lines.append(
                    tool.name + "\n"
                    + tool.description + "\n"
                    + json.dumps(tool.input_schema, ensure_ascii=False) + "\n"
                )
        return ["\n".join(lines)] if len(lines) > 1 else []


class PinnedContextProvider(ContextProvider):
    """用户收藏的关键信息，作为长期固定上下文注入。

    数据来自 state["pinned_context"]（list[str]），由应用层在每次运行前写入。
    与历史/工具反馈不同，这里是"固定写入"，不参与裁剪/摘要策略。
    """
    name = "pinned_context"

    def get(self, state: dict) -> list[str]:
        items = state.get("pinned_context") or []
        cleaned = [str(item).strip() for item in items if str(item).strip()]
        if not cleaned:
            return []
        parts = ["## 固定上下文（用户收藏，长期有效）"]
        parts += [f"- {item}" for item in cleaned]
        return ["\n".join(parts)]


# 内联产物协议：coding agent（Claude Code / Codex）无法直接调用 inline_artifact 工具，
# 只能在正文里输出标记块，由 im_backend 侧解析成产物事件。这里只注入“协议说明文本”，
# 解析逻辑（ArtifactStreamParser）留在 im_backend。常量放在 agent_flow，使 ContextService
# 能按 provider_id="artifact_protocol" 构建该 provider，而无需反向依赖 im_backend。
ARTIFACT_BEGIN = "@@ARTIFACT_BEGIN@@"
ARTIFACT_END = "@@ARTIFACT_END@@"

ARTIFACT_PROTOCOL_INSTRUCTION = (
    "## 内联产物协议\n"
    "当你需要向用户展示一个“产物”（代码改动对比、可预览文档/文件、图片、网页预览，"
    "或一条结构化消息）时，在正文中单独输出一个产物标记块。系统会捕获该标记块、"
    "渲染成内联卡片，并自动把标记本身从展示文本里移除。\n\n"
    "重要：你不能直接调用 agent_flow 的 inline_artifact 工具；Claude Code / Codex 的"
    "shell 命令、文件读取、command_execution 输出也不会自动变成产物。若要让前端出现"
    "产物卡片，必须由你在最终回复文本中原样输出下面的标记块。\n\n"
    "标记块格式（起止标记各占一行，中间是一个合法 JSON 对象）：\n\n"
    f"{ARTIFACT_BEGIN}\n"
    '{"artifact_type": "<类型>", "<类型>": { ...该类型字段... }}\n'
    f"{ARTIFACT_END}\n\n"
    "artifact_type 取值与对应字段：\n"
    '- message  普通消息：{"title","content","mime_type"?,"metadata"?}\n'
    '- image    图片：{"title","url","alt"?,"mime_type"?,"metadata"?}\n'
    '- diff     代码改动对比：{"title","before","after","file_path"?,"language"?,"metadata"?}\n'
    '- document 可预览文档/文件：{"title","content","format"?(md/py/js/json/txt...),"language"?,"editable"?,"metadata"?}\n'
    '- web      网页预览：{"title","url"?,"html"?,"preview_title"?,"metadata"?}\n'
    '- deploy   后台真实部署（系统会在 127.0.0.1 真起一个端口，前端出现带实时预览且可一键关闭端口的部署卡片）：\n'
    '           {"kind":"static"|"command","title","source_dir"?,"command"?,"entry"?,"files"?,"env"?}\n'
    '           · kind=static：把一个**已存在的目录**作为静态网页托管，source_dir 填该目录相对你工作目录的路径（如 "dist" 或 "."），entry 默认 index.html；\n'
    '           · kind=command：在 source_dir 目录下运行一条常驻启动命令，command 用 $PORT 占位监听端口（如 "uvicorn app:app --host 127.0.0.1 --port $PORT"）；\n'
    '           · files 可选：path->content，把少量启动文件补写进 source_dir（路径不得越界）。\n\n'
    "规则：\n"
    "- JSON 必须合法、可一次性解析；顶层只放 artifact_type 与同名的类型对象；"
    "content/before/after/html 内的换行必须写成 JSON 字符串里的 \\n 转义，不能放未转义的真实换行。\n"
    "- 一次只放一个产物；要展示多个产物就输出多个标记块。\n"
    "- 用户要求展示、预览、生成、创建、打开某个文件内容时，读取/生成内容后必须输出 "
    "document 产物标记块；title 使用文件名或简短标题，content 放完整可展示内容，"
    "format/language 按文件扩展名填写，editable 通常设为 true。\n"
    "- 用户要求展示代码修改前后对比时，必须输出 diff 产物标记块。\n"
    "- 用户要求“部署/上线/运行起来看效果”时：先用你的文件工具在工作目录把要部署的文件或后端代码真实写盘，"
    "再输出 deploy 标记块（kind=static 用 source_dir 指向静态目录，kind=command 给出含 $PORT 的启动命令并用 source_dir 指向代码目录）。"
    "不要用 web 预览冒充部署——只有 deploy 标记块才会真正开端口并生成可关闭的部署卡片。\n"
    "- 不要把 shell 命令输出当作已经完成产物展示；命令只用于获取内容，产物展示必须靠标记块。\n"
    "- 仅在确有产物要展示时使用；普通解释照常直接输出，不要包进标记。\n"
    "- 标记块之外的正文会照常流式展示给用户。\n"
)


class ArtifactProtocolProvider(ContextProvider):
    """把内联产物协议说明注入 coding agent 的上下文。

    只注入说明文本——产物事件由 executor 侧解析标记后发出。
    """

    name = "artifact_protocol"

    def get(self, state: dict) -> list[str]:
        return [ARTIFACT_PROTOCOL_INSTRUCTION]


# 动态provider

class ReActToolFeedbackProvider(MemoryProvider):
    """ReACT 工具反馈：逐条渲染已在 memory 里拼好的完整 ReACT 块
    （Thought→Action→Args→Observation），让模型在下一轮看到自己当初的
    思考与行动，而不只是孤立的 Observation —— 从根上避免重复调用同一工具。
    """
    name = "tool_output"

    def get(self, state: dict) -> list[str]:
        items = self._get_items(state)
        if not items:
            return []
        parts = [f"## ReACT 工具反馈（{len(items)} 条）：思考→行动→观察"]
        for item in items:
            # item.content 已是完整 ReACT 块（由 AgentBase._build_react_block 生成）
            parts.append(f"### {item.source}\n{item.content}")
            if item.metadata.get("summarized"):
                parts.append(
                    f'（内容已压缩）'
                )
        return ["\n\n".join(parts)]


# 别名：保留旧名以兼容大量已有 import / isinstance 检查（指向同一类对象，
# isinstance(provider, ToolOutputProvider) 仍正确）。
ToolOutputProvider = ReActToolFeedbackProvider


class HistoryProvider(MemoryProvider):
    name = "history"

    def get(self, state: dict) -> list[str]:
        items = self._get_items(state)
        if not items:
            return []
        parts = ["## 对话历史"]
        parts += [item.content for item in items]
        return ["\n".join(parts)]


class ErrorProvider(MemoryProvider):
    """错误回灌：注入上一轮解析失败/不合规输出等错误信息，提醒模型纠正。

    默认使用 ConsumeOnceStrategy —— 错误只注入一次，注入后即从 memory 删除，
    因此下一轮不再出现（除非又产生了新错误）。无错误时输出为空，无副作用。
    """
    name = "error"

    def __init__(
        self,
        memory:   ShortTermMemory,
        field:    memory_field = "error",
        strategy: ContextStrategy | None = None,
    ) -> None:
        super().__init__(memory, field, strategy or ConsumeOnceStrategy())

    def get(self, state: dict) -> list[str]:
        items = self._get_items(state)
        if not items:
            return []
        parts = ["## 上一轮错误（请修正后重试，本提示仅出现一次）"]
        parts += [item.content for item in items]
        return ["\n\n".join(parts)]



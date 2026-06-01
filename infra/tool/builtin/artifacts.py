from __future__ import annotations

import time
from typing import Any

from domain.event import Event
from domain.runtime_hooks import get_run_context_provider
from domain.tool import Tool, Tool_respond
from infra.config import bus, factory
from infra.event_bind import On_bind


INLINE_ARTIFACT = Tool(
    name="inline_artifact",
    description=(
        "创建消息内联产物，支持普通消息、图片消息、diff 卡片、"
        "可编辑文档预览和网页预览。"
    ),
    field="system",
    input_schema={
        "type": "object",
        "properties": {
            "artifact_type": {
                "type": "string",
                "enum": ["message", "image", "diff", "document", "web"],
                "description": "产物类型",
            },
            "message": {
                "type": "object",
                "description": "普通消息产物参数",
                "properties": {
                    "title": {"type": "string", "description": "消息标题"},
                    "content": {"type": "string", "description": "消息内容"},
                    "mime_type": {"type": "string", "description": "内容 MIME 类型"},
                    "metadata": {"type": "object", "description": "前端渲染附加信息"},
                },
            },
            "image": {
                "type": "object",
                "description": "图片消息产物参数",
                "properties": {
                    "title": {"type": "string", "description": "图片标题"},
                    "url": {"type": "string", "description": "图片 URL 或 data URL"},
                    "alt": {"type": "string", "description": "图片替代文本"},
                    "mime_type": {"type": "string", "description": "图片 MIME 类型"},
                    "metadata": {"type": "object", "description": "前端渲染附加信息"},
                },
            },
            "diff": {
                "type": "object",
                "description": "diff 卡片产物参数",
                "properties": {
                    "title": {"type": "string", "description": "diff 标题"},
                    "before": {"type": "string", "description": "修改前内容"},
                    "after": {"type": "string", "description": "修改后内容"},
                    "file_path": {"type": "string", "description": "对应文件路径"},
                    "language": {"type": "string", "description": "代码语言"},
                    "metadata": {"type": "object", "description": "前端渲染附加信息"},
                },
            },
            "document": {
                "type": "object",
                "description": "文档预览产物参数",
                "properties": {
                    "title": {"type": "string", "description": "文档标题"},
                    "content": {"type": "string", "description": "文档内容"},
                    "format": {
                        "type": "string",
                        "description": "文档格式，如 md、py、js、txt、json",
                    },
                    "language": {
                        "type": "string",
                        "description": "代码或文档语言，用于高亮",
                    },
                    "mime_type": {"type": "string", "description": "内容 MIME 类型"},
                    "editable": {"type": "boolean", "description": "是否支持编辑"},
                    "metadata": {"type": "object", "description": "前端渲染附加信息"},
                },
            },
            "web": {
                "type": "object",
                "description": "网页预览产物参数",
                "properties": {
                    "title": {"type": "string", "description": "网页产物标题"},
                    "url": {"type": "string", "description": "网页 URL"},
                    "html": {"type": "string", "description": "网页预览 HTML 内容"},
                    "preview_title": {"type": "string", "description": "网页预览标题"},
                    "metadata": {"type": "object", "description": "前端渲染附加信息"},
                },
            },
        },
        "required": ["artifact_type"],
    },
)


class InlineArtifactTool:
    VALID_TYPES = {"message", "image", "diff", "document", "web"}
    MIME_BY_FORMAT = {
        "md": "text/markdown",
        "markdown": "text/markdown",
        "py": "text/x-python",
        "js": "text/javascript",
        "ts": "text/typescript",
        "tsx": "text/tsx",
        "jsx": "text/jsx",
        "json": "application/json",
        "html": "text/html",
        "css": "text/css",
        "txt": "text/plain",
    }
    LANGUAGE_BY_FORMAT = {
        "md": "markdown",
        "markdown": "markdown",
        "py": "python",
        "js": "javascript",
        "ts": "typescript",
        "tsx": "tsx",
        "jsx": "jsx",
        "json": "json",
        "html": "html",
        "css": "css",
        "txt": "text",
    }

    def build_event_payload(self, arguments: dict[str, Any]) -> dict[str, Any]:
        artifact_type = str(arguments.get("artifact_type", "")).strip().lower()
        if artifact_type not in self.VALID_TYPES:
            raise ValueError(f"不支持的 artifact_type: {artifact_type}")

        agent_id = str(arguments.get("agent_id", "")).strip()
        run_id = str(arguments.get("run_id", "")).strip()
        if not run_id and agent_id:
            provider = get_run_context_provider()
            if provider is not None:
                run_id = provider.run_id_for_agent(agent_id)

        event_name = f"artifacts.{artifact_type}"
        artifact = self._build_artifact(arguments, artifact_type)
        return {
            "run_id": run_id,
            "agent_id": agent_id,
            "event_name": event_name,
            "frontend_event_name": event_name,
            "artifact_type": artifact_type,
            "artifact": artifact,
            "created_at": time.time(),
        }

    def _build_artifact(
        self,
        arguments: dict[str, Any],
        artifact_type: str,
    ) -> dict[str, Any]:
        builders = {
            "message": self._build_message,
            "image": self._build_image,
            "diff": self._build_diff,
            "document": self._build_document,
            "web": self._build_web,
        }
        return builders[artifact_type](self._require_section(arguments, artifact_type))

    def _require_section(self, arguments: dict[str, Any], artifact_type: str) -> dict[str, Any]:
        section = arguments.get(artifact_type)
        if not isinstance(section, dict):
            raise ValueError(f"{artifact_type} 产物必须提供 `{artifact_type}` 对象参数")
        return section

    def _build_message(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "message",
            "title": params.get("title", ""),
            "content": params.get("content", ""),
            "mime_type": params.get("mime_type") or "text/plain",
            "metadata": params.get("metadata") or {},
            "editable": False,
        }

    def _build_image(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "image",
            "title": params.get("title", ""),
            "url": params.get("url", ""),
            "alt": params.get("alt", ""),
            "mime_type": params.get("mime_type") or "image/*",
            "metadata": params.get("metadata") or {},
            "editable": False,
        }

    def _build_diff(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "diff",
            "title": params.get("title", ""),
            "before": params.get("before", ""),
            "after": params.get("after", ""),
            "file_path": params.get("file_path", ""),
            "language": params.get("language", ""),
            "metadata": params.get("metadata") or {},
            "editable": False,
        }

    def _build_document(self, params: dict[str, Any]) -> dict[str, Any]:
        doc_format = str(params.get("format", "") or "md").strip().lower()
        return {
            "type": "document",
            "title": params.get("title", ""),
            "content": params.get("content", ""),
            "format": doc_format,
            "language": params.get("language")
            or self.LANGUAGE_BY_FORMAT.get(doc_format, doc_format),
            "mime_type": params.get("mime_type")
            or self.MIME_BY_FORMAT.get(doc_format, "text/plain"),
            "metadata": params.get("metadata") or {},
            "editable": bool(params.get("editable", True)),
        }

    def _build_web(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "web",
            "title": params.get("title", ""),
            "url": params.get("url", ""),
            "html": params.get("html", ""),
            "preview_title": params.get("preview_title", ""),
            "mime_type": "text/html",
            "metadata": params.get("metadata") or {},
            "editable": False,
        }


on_tool = On_bind()
factory._build_and_register_list([INLINE_ARTIFACT], bus)


@on_tool.on(factory.tool("inline_artifact").called())
async def inline_artifact(**kwargs) -> Event:
    agent_id = kwargs.get("agent_id", "")
    tool = InlineArtifactTool()
    try:
        payload = tool.build_event_payload(kwargs)
        await bus.publish(Event(payload["event_name"], payload=payload))
    except Exception as exc:
        tool_respond = Tool_respond(
            agent_id=agent_id,
            name="inline_artifact",
            success=False,
            respond=f"内联产物创建失败: {exc}",
        )
        return factory.tool("inline_artifact").failed(tool_respond)

    tool_respond = Tool_respond(
        agent_id=agent_id,
        name="inline_artifact",
        success=True,
        respond=payload,
    )
    return factory.tool("inline_artifact").succeeded(tool_respond)


@on_tool.on_pattern("artifacts.*")
async def on_artifacts(**kwargs) -> None:
    return None

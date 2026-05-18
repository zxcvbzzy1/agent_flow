from __future__ import annotations

import importlib
import importlib.util
import re
import sys
import uuid
from pathlib import Path
from typing import Any

from domain.tool import Tool
from infra.config import bus, factory
from infra.db.mongodb import DocumentStore


class ToolRegistryService:
    def __init__(self, store: DocumentStore, root_dir: Path) -> None:
        self._store = store
        self._upload_dir = root_dir / "infra" / "tool" / "uploaded"
        self._upload_dir.mkdir(parents=True, exist_ok=True)
        self.load_builtin_tools()
        self._persist_registered_tools()

    def load_builtin_tools(self) -> None:
        import infra.tool.builtin.system  # noqa: F401
        import infra.tool.builtin.story_write  # noqa: F401
        import infra.tool.tools_attach_methods  # noqa: F401

    def list_tools(self) -> list[dict[str, Any]]:
        return [self._tool_to_dict(tool) for tool in Tool.get_all_tools()]

    def upload_tool(
        self,
        name: str,
        description: str,
        field: str | None,
        input_schema: dict[str, Any],
        metadata: dict[str, Any] | None,
        source_code: str,
    ) -> dict[str, Any]:
        safe_name = self._safe_module_name(name)
        module_path = self._upload_dir / f"{safe_name}_{uuid.uuid4().hex[:8]}.py"
        module_path.write_text(source_code or "\n", encoding="utf-8")

        tool = self._ensure_tool(
            name=name,
            description=description,
            field=field,
            input_schema=input_schema,
            metadata=metadata or {},
        )
        factory._build_and_register_list([tool], bus)

        if source_code.strip():
            self._import_file(module_path)

        record = {
            **self._tool_to_dict(tool),
            "tool_id": name,
            "source_path": str(module_path),
            "uploaded": True,
        }
        self._store.update_one("tools", {"tool_id": name}, record, upsert=True)
        return record

    def _persist_registered_tools(self) -> None:
        for tool in Tool.get_all_tools():
            record = {**self._tool_to_dict(tool), "tool_id": tool.name, "uploaded": False}
            self._store.update_one("tools", {"tool_id": tool.name}, record, upsert=True)

    def _ensure_tool(
        self,
        name: str,
        description: str,
        field: str | None,
        input_schema: dict[str, Any],
        metadata: dict[str, Any],
    ) -> Tool:
        existing = Tool._registry_dict.get(name)
        if existing:
            Tool._registry = [item for item in Tool._registry if item.name != name]
            Tool._registry_dict.pop(name, None)
        return Tool(
            name=name,
            description=description,
            field=field,
            input_schema=input_schema,
            metadata=metadata,
        )

    def _tool_to_dict(self, tool: Tool) -> dict[str, Any]:
        event_names: list[str] = []
        try:
            event_names = factory.tool(tool.name).all_event_names()
        except Exception:
            pass
        return {
            "name": tool.name,
            "description": tool.description,
            "field": tool.field,
            "input_schema": tool.input_schema,
            "metadata": tool.metadata,
            "events": event_names,
        }

    def _safe_module_name(self, name: str) -> str:
        value = re.sub(r"[^a-zA-Z0-9_]+", "_", name).strip("_")
        return value or "uploaded_tool"

    def _import_file(self, path: Path) -> None:
        module_name = f"infra.tool.uploaded.{path.stem}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"无法加载工具实现: {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        importlib.invalidate_caches()


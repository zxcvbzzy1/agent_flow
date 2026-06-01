from __future__ import annotations

import time
from typing import Any

from domain.event import Event

from application.events.schemas import tool_event_payload
from application.services.events import EventStreamService


class FrontendEventBridge:
    """
    实现ToolEventObserverPort接口，监听工具事件并转换成前端事件发送到EventStreamService中。
    实现RunContextProviderPort接口，维护agent_id到run_id的映射关系，供工具事件中缺失run_id时使用。
     - register_agent_run方法用于注册agent_id和run_id的关系，通常在agent开始执行时调用。
     - unregister_agent_run方法用于取消注册关系，通常在agent结束执行时调用。
     - on_tool_event方法监听工具事件，调用mirror_tool_event进行转换和发布。
     - mirror_tool_event方法根据事件名称确定前端事件类型，构造事件负载，并发布到EventStreamService中。
     - _frontend_tool_event_name方法根据内部事件名称的后缀确定对应的前端事件名称，目前支持tool.called、tool
    """

    _TOOL_SUFFIX_TO_EVENT = {
        "called": "tool.called",
        "succeeded": "tool.succeeded",
        "failed": "tool.failed",
        "retrying": "tool.retrying",
    }

    def __init__(self, streams: EventStreamService, factory) -> None:
        self._streams = streams
        self._factory = factory
        self._agent_runs: dict[str, str] = {}

    def register_agent_run(self, agent_id: str, run_id: str) -> None:
        self._agent_runs[agent_id] = run_id

    def unregister_agent_run(self, agent_id: str, run_id: str | None = None) -> None:
        if run_id is not None and self._agent_runs.get(agent_id) != run_id:
            return
        self._agent_runs.pop(agent_id, None)

    def run_id_for_agent(self, agent_id: str) -> str:
        return self._agent_runs.get(agent_id, "")

    def on_tool_event(self, event: Event) -> None:
        self.mirror_tool_event(event)

    def mirror_tool_event(self, event: Event) -> None:
        frontend_event_name = self._frontend_tool_event_name(event.name)
        if not frontend_event_name:
            return

        payload = event.unpack()
        agent_id = payload.get("agent_id", "")
        run_id = payload.get("run_id") or self.run_id_for_agent(agent_id)
        if not run_id:
            return

        if frontend_event_name.startswith("artifacts."):
            mirrored = {
                "run_id": run_id,
                "agent_id": agent_id,
                "event_name": event.name,
                "frontend_event_name": frontend_event_name,
                "artifact_type": payload.get("artifact_type"),
                "artifact": payload.get("artifact", {}),
                "created_at": payload.get("created_at", time.time()),
            }
            self._streams.publish(run_id, frontend_event_name, mirrored)
            return

        tool_name = payload.get("tool_name", "")
        tool_field = None
        try:
            spec = self._factory.get_spec(event.name)
            tool_name = tool_name or spec.tool_name
            tool_field = spec.tool_field
        except Exception:
            pass

        mirrored = tool_event_payload(
            run_id=run_id,
            agent_id=agent_id,
            tool_name=tool_name,
            tool_field=tool_field,
            internal_event_name=event.name,
            frontend_event_name=frontend_event_name,
            payload=payload,
        )
        self._streams.publish(run_id, frontend_event_name, mirrored)

    def _frontend_tool_event_name(self, internal_name: str) -> str:
        if internal_name.startswith("artifacts."):
            return internal_name
        suffix = internal_name.rsplit(".", 1)[-1]
        return self._TOOL_SUFFIX_TO_EVENT.get(suffix, "")

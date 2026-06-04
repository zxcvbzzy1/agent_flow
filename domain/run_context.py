from __future__ import annotations

from contextvars import ContextVar
from typing import Any

# 当前 run 的 run_id（per-run 隔离）。在 RunOrchestrationService._execute_run 顶部 set，
# finally 中 reset。FrontendEventBridge.run_id_for_agent 优先读它，避免并发 run 下
# agent_id -> run_id 全局映射的 last-writer-wins 串扰。
current_run_id: ContextVar[str] = ContextVar("current_run_id", default="")

# 当前正在 emit 工具事件的 agent 实例（per-run 隔离）。在 AgentBase._run_one 围绕 emit
# set/reset，工具回调（on_tool_success/on_tool_fail/recall_skill）优先读它，避免并发 run 下
# AgentBase._instance_list[agent_id] 全局映射的 last-writer-wins 路由到错误实例。
current_agent: ContextVar[Any] = ContextVar("current_agent", default=None)

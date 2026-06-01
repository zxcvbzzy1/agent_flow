from __future__ import annotations

import copy
import uuid
from typing import Any

from domain.agent.plan.providers import (
    AvailableExecutorsProvider,
    ExecutorStatusProvider,
    PlanObservationProvider,
    PlanStepPromptProvider,
)
from domain.context.context import ContextEngine
from domain.context.providers import (
    AvailableToolsProvider,
    ErrorProvider,
    HistoryProvider,
    MemoryProvider,
    PinnedContextProvider,
    StateProvider,
    ToolOutputProvider,
    UserPromptProvider,
)
from domain.context.strategy import (
    ContextStrategy,
    FullHistoryStrategy,
    RecencyStrategy,
    StrategyPipeline,
    TokenBudgetStrategy,
)
from domain.context.strategy import FilterByToolStrategy, LatestOnlyStrategy, SummarizeStrategy
from domain.memory.short.default_short_term_memory import DefaultShortTermMemory
from infra.db.mongodb import DocumentStore


class ContextService:
    DEFAULT_FIELDS = ["system", "search", "memory", "write_agent", "human"]
    PROTECTED_CONTEXT_IDS = {"default_executor", "default_planner", "default_step"}

    def __init__(self, store: DocumentStore) -> None:
        self._store = store
        self._engines: dict[str, ContextEngine] = {}
        self.ensure_default_contexts()

    def ensure_default_contexts(self) -> None:
        for kind in ("executor", "planner", "step"):
            context_id = f"default_{kind}"
            if self._store.find_one("contexts", {"context_id": context_id}) is None:
                self.create_context(
                    kind=kind,
                    name=f"Default {kind}",
                    provider_config=self.default_template(kind),
                    context_id=context_id,
                )

    def catalog(self) -> dict[str, Any]:
        return {
            "providers": [
                {"provider_id": "user_prompt", "name": "用户需求", "params": []},
                {"provider_id": "state", "name": "执行状态", "params": []},
                {"provider_id": "error", "name": "错误回灌(一次性)", "params": ["memory_field"]},
                {"provider_id": "pinned_context", "name": "固定上下文(收藏)", "params": []},
                {"provider_id": "available_tools", "name": "可用工具", "params": ["available_fields", "available_tools"]},
                {"provider_id": "history", "name": "对话历史", "params": ["memory_field", "strategy_config"]},
                {"provider_id": "tool_output", "name": "工具反馈", "params": ["memory_field", "strategy_config"]},
                {"provider_id": "available_executors", "name": "可用执行者", "params": []},
                {"provider_id": "executor_status", "name": "执行者状态", "params": []},
                {"provider_id": "plan_observations", "name": "计划观察", "params": []},
                {"provider_id": "plan_step_prompt", "name": "计划步骤 Prompt", "params": []},
            ],
            "strategies": [
                {"type": "full_history", "name": "完整历史", "params": []},
                {"type": "latest_only", "name": "仅最新", "params": []},
                {"type": "recency", "name": "最近 N 条", "params": ["keep_last"]},
                {"type": "token_budget", "name": "Token 预算", "params": ["token_limit"]},
                {"type": "summarize", "name": "超长摘要/截断", "params": ["threshold"]},
                {"type": "filter_by_tool", "name": "按工具过滤", "params": ["tool_names"]},
            ],
            "templates": {
                "executor": self.default_template("executor"),
                "planner": self.default_template("planner"),
                "step": self.default_template("step"),
            },
        }

    def default_template(self, kind: str) -> list[dict[str, Any]]:
        memory_strategy = {"pipeline": [{"type": "full_history"}, {"type": "recency", "keep_last": 10}]}
        templates = {
            "executor": [
                {"provider_id": "user_prompt", "enabled": True, "params": {}},
                {"provider_id": "pinned_context", "enabled": True, "params": {}},
                {"provider_id": "state", "enabled": True, "params": {}},
                {"provider_id": "error", "enabled": True, "params": {}},
                {"provider_id": "available_tools", "enabled": True, "params": {"available_fields": self.DEFAULT_FIELDS}},
                {
                    "provider_id": "history",
                    "enabled": True,
                    "params": {"memory_field": "agent_history", "strategy_config": memory_strategy},
                },
                {
                    "provider_id": "tool_output",
                    "enabled": True,
                    "params": {"memory_field": "tool_respond", "strategy_config": memory_strategy},
                },
            ],
            "planner": [
                {"provider_id": "user_prompt", "enabled": True, "params": {}},
                {"provider_id": "pinned_context", "enabled": True, "params": {}},
                {"provider_id": "state", "enabled": True, "params": {}},
                {"provider_id": "error", "enabled": True, "params": {}},
                {"provider_id": "available_executors", "enabled": True, "params": {}},
                {"provider_id": "executor_status", "enabled": True, "params": {}},
                {"provider_id": "plan_observations", "enabled": True, "params": {}},
                {
                    "provider_id": "history",
                    "enabled": True,
                    "params": {"memory_field": "agent_history", "strategy_config": memory_strategy},
                },
                {
                    "provider_id": "tool_output",
                    "enabled": True,
                    "params": {"memory_field": "tool_respond", "strategy_config": memory_strategy},
                },
            ],
            "step": [
                {"provider_id": "plan_step_prompt", "enabled": True, "params": {}},
            ],
        }
        if kind not in templates:
            raise ValueError(f"未知上下文类型: {kind}")
        return copy.deepcopy(templates[kind])

    def create_context(
        self,
        kind: str,
        name: str,
        provider_config: list[dict[str, Any]] | None = None,
        context_id: str | None = None,
    ) -> dict[str, Any]:
        context_id = context_id or str(uuid.uuid4())
        if not provider_config:
            raise ValueError("provider_config 不能为空")
        record = {
            "context_id": context_id,
            "kind": kind,
            "name": name,
            "provider_config": copy.deepcopy(provider_config),
        }
        engine = self._build_engine(record)
        self._store.update_one("contexts", {"context_id": context_id}, record, upsert=True)
        self._engines[context_id] = engine
        return self._with_overview(record)

    def create_context_from_engine(
        self,
        kind: str,
        name: str,
        engine: ContextEngine,
        context_id: str | None = None,
    ) -> dict[str, Any]:
        """Register an existing ContextEngine and persist its provider config."""
        context_id = context_id or str(uuid.uuid4())
        provider_config = [
            self._provider_to_config(provider)
            for provider in getattr(engine, "_providers", [])
        ]
        if not provider_config:
            raise ValueError("ContextEngine 至少需要一个 provider")

        record = {
            "context_id": context_id,
            "kind": kind,
            "name": name,
            "provider_config": provider_config,
        }
        self._store.update_one("contexts", {"context_id": context_id}, record, upsert=True)
        self._engines[context_id] = engine
        return self._with_overview(record)

    def list_contexts(self) -> list[dict[str, Any]]:
        return [
            self._with_overview(record)
            for record in self._store.find_many("contexts", sort=[("created_at", 1)])
        ]

    def get_context(self, context_id: str) -> dict[str, Any] | None:
        record = self._store.find_one("contexts", {"context_id": context_id})
        return self._with_overview(record) if record else None

    def get_engine(self, context_id: str) -> ContextEngine:
        if context_id not in self._engines:
            record = self.get_context(context_id)
            if record is None:
                raise KeyError(f"上下文不存在: {context_id}")
            self._engines[context_id] = self._build_engine(record)
        return self._engines[context_id]

    def delete_context(self, context_id: str) -> dict[str, Any]:
        if context_id in self.PROTECTED_CONTEXT_IDS:
            raise ValueError("默认 ContextEngine 不允许删除")

        record = self._store.find_one("contexts", {"context_id": context_id})
        if record is None:
            raise KeyError(f"上下文不存在: {context_id}")

        agent_ref = self._store.find_one("agents", {"context_id": context_id})
        if agent_ref is not None:
            raise ValueError(f"ContextEngine 已被 Agent 引用: {agent_ref.get('agent_id', '')}")

        run_ref = self._store.find_one("runs", {"context_id": context_id})
        if run_ref is not None:
            raise ValueError(f"ContextEngine 已被 Run 引用: {run_ref.get('run_id', '')}")

        stats = {
            "contexts": self._store.delete_one("contexts", {"context_id": context_id}),
        }
        self._engines.pop(context_id, None)
        return {"deleted": True, "context_id": context_id, "stats": stats}

    def _build_engine(self, record: dict[str, Any]) -> ContextEngine:
        memory = DefaultShortTermMemory(["tool_respond", "agent_history", "error"])
        providers = [
            provider
            for provider in (
                self._build_provider(config, memory)
                for config in record.get("provider_config", [])
            )
            if provider is not None
        ]
        if not providers:
            raise ValueError("至少需要一个启用的 provider")
        # 固定上下文（收藏）注入对所有 context 生效，即使旧配置未声明也自动补一个；
        # state 中没有 pinned_context 时该 provider 输出为空，无副作用。
        if not any(isinstance(provider, PinnedContextProvider) for provider in providers):
            providers.append(PinnedContextProvider())
        # 错误回灌同样对所有 context 生效：解析失败时下一轮注入一次提醒，无错误时输出为空。
        if not any(isinstance(provider, ErrorProvider) for provider in providers):
            providers.append(ErrorProvider(memory))
        return ContextEngine(providers=providers, memory=memory)

    def _build_provider(
        self,
        config: dict[str, Any],
        memory: DefaultShortTermMemory,
    ):
        if config.get("enabled", True) is False:
            return None

        provider_id = config.get("provider_id")
        params = config.get("params") or {}
        if provider_id == "user_prompt":
            return UserPromptProvider()
        if provider_id == "state":
            return StateProvider()
        if provider_id == "pinned_context":
            return PinnedContextProvider()
        if provider_id == "error":
            # 一次性错误回灌，固定使用 ConsumeOnceStrategy，不暴露 strategy 配置。
            return ErrorProvider(memory, params.get("memory_field", "error"))
        if provider_id == "available_tools":
            available_tools = params.get("available_tools") or []
            available_fields = params.get("available_fields")
            if available_fields is None and not available_tools:
                available_fields = self.DEFAULT_FIELDS
            return AvailableToolsProvider(available_fields or [], available_tools)
        if provider_id == "history":
            return HistoryProvider(
                memory,
                params.get("memory_field", "agent_history"),
                self._build_strategy_pipeline(params.get("strategy_config")),
            )
        if provider_id == "tool_output":
            return ToolOutputProvider(
                memory,
                params.get("memory_field", "tool_respond"),
                self._build_strategy_pipeline(params.get("strategy_config")),
            )
        if provider_id == "available_executors":
            return AvailableExecutorsProvider()
        if provider_id == "executor_status":
            return ExecutorStatusProvider()
        if provider_id == "plan_observations":
            return PlanObservationProvider()
        if provider_id == "plan_step_prompt":
            return PlanStepPromptProvider()
        raise ValueError(f"未知 provider: {provider_id}")

    def _provider_to_config(self, provider) -> dict[str, Any]:
        enabled = getattr(provider, "enabled", True)
        params: dict[str, Any] = {}

        if isinstance(provider, UserPromptProvider):
            provider_id = "user_prompt"
        elif isinstance(provider, StateProvider):
            provider_id = "state"
        elif isinstance(provider, PinnedContextProvider):
            provider_id = "pinned_context"
        elif isinstance(provider, ErrorProvider):
            # ErrorProvider 是 MemoryProvider，但策略固定（ConsumeOnce），只回填 memory_field。
            provider_id = "error"
            params["memory_field"] = getattr(provider, "_field", "error")
        elif isinstance(provider, AvailableToolsProvider):
            provider_id = "available_tools"
            params["available_fields"] = list(getattr(provider, "_fields", self.DEFAULT_FIELDS))
            params["available_tools"] = list(getattr(provider, "_tools", []))
        elif isinstance(provider, HistoryProvider):
            provider_id = "history"
            params = self._memory_provider_params(provider)
        elif isinstance(provider, ToolOutputProvider):
            provider_id = "tool_output"
            params = self._memory_provider_params(provider)
        elif isinstance(provider, AvailableExecutorsProvider):
            provider_id = "available_executors"
        elif isinstance(provider, ExecutorStatusProvider):
            provider_id = "executor_status"
        elif isinstance(provider, PlanObservationProvider):
            provider_id = "plan_observations"
        elif isinstance(provider, PlanStepPromptProvider):
            provider_id = "plan_step_prompt"
        else:
            raise ValueError(f"无法转换 provider: {type(provider).__name__}")

        return {
            "provider_id": provider_id,
            "enabled": enabled,
            "params": params,
        }

    def _memory_provider_params(self, provider: MemoryProvider) -> dict[str, Any]:
        return {
            "memory_field": getattr(provider, "_field", "agent_history"),
            "strategy_config": self._strategy_to_config(getattr(provider, "_strategy", FullHistoryStrategy())),
        }

    def _build_strategy_pipeline(self, config: dict[str, Any] | None):
        if not isinstance(config, dict) or not isinstance(config.get("pipeline"), list) or not config["pipeline"]:
            raise ValueError("strategy_config 必须包含非空 pipeline")

        strategies = [self._build_strategy_item(item) for item in config["pipeline"]]
        strategy = strategies[0]
        for next_strategy in strategies[1:]:
            strategy = strategy | next_strategy
        return strategy

    def _build_strategy_item(self, config: dict[str, Any]):
        strategy_type = config.get("type") if isinstance(config, dict) else None
        if strategy_type == "full_history":
            return FullHistoryStrategy()
        if strategy_type == "latest_only":
            return LatestOnlyStrategy()
        if strategy_type == "recency":
            return RecencyStrategy(int(config["keep_last"]))
        if strategy_type == "token_budget":
            return TokenBudgetStrategy(int(config["token_limit"]))
        if strategy_type == "summarize":
            return SummarizeStrategy(int(config["threshold"]))
        if strategy_type == "filter_by_tool":
            tool_names = config.get("tool_names")
            if not isinstance(tool_names, list):
                raise ValueError("filter_by_tool.tool_names 必须是列表")
            return FilterByToolStrategy(tool_names)
        raise ValueError(f"未知 strategy: {strategy_type}")

    def _strategy_to_config(self, strategy: ContextStrategy) -> dict[str, Any]:
        strategies = getattr(strategy, "_strategies", None)
        if isinstance(strategy, StrategyPipeline):
            strategies = strategy._strategies
        if not isinstance(strategies, list):
            strategies = [strategy]
        return {
            "pipeline": [
                self._strategy_item_to_config(item)
                for item in strategies
            ]
        }

    def _strategy_item_to_config(self, strategy: ContextStrategy) -> dict[str, Any]:
        if isinstance(strategy, FullHistoryStrategy):
            return {"type": "full_history"}
        if isinstance(strategy, LatestOnlyStrategy):
            return {"type": "latest_only"}
        if isinstance(strategy, RecencyStrategy):
            return {"type": "recency", "keep_last": getattr(strategy, "_n")}
        if isinstance(strategy, TokenBudgetStrategy):
            return {"type": "token_budget", "token_limit": getattr(strategy, "_limit")}
        if isinstance(strategy, SummarizeStrategy):
            return {"type": "summarize", "threshold": getattr(strategy, "_threshold")}
        if isinstance(strategy, FilterByToolStrategy):
            return {"type": "filter_by_tool", "tool_names": sorted(getattr(strategy, "_names"))}
        raise ValueError(f"无法转换 strategy: {type(strategy).__name__}")

    def _with_overview(self, record: dict[str, Any]) -> dict[str, Any]:
        provider_config = record.get("provider_config", [])
        enabled = [item for item in provider_config if item.get("enabled", True) is not False]
        return {
            **record,
            "engine_loaded": record.get("context_id") in self._engines,
            "provider_count": len(enabled),
            "provider_names": [item.get("provider_id", "") for item in enabled],
        }

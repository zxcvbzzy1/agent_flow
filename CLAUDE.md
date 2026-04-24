# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Novel Agent — a multi-agent novel writing system built on an EDA (Event-Driven Architecture) with a DDD-inspired layered structure. Two agents collaborate: **WriteAgent** (content creation) and **CriticAgent** (quality evaluation), orchestrated through a BusinessGraph workflow engine.

## Architecture

Three-layer architecture: `domain/` → `infra/` → `application/`

### Domain Layer (`domain/`)
Pure business logic, no infrastructure dependencies.

- **`agent_base.py`** — `AgentBase` abstract class: the core think→tool→state loop. Maintains `Agent_state`, builds prompts via `ContextEngine`, parses LLM JSON decisions (`AgentDecision`/`ToolCall`), executes tools via `ToolEventFactory`, supports `$ref:tool_name#index` cross-tool result referencing.
- **`tool.py`** — `Tool` dataclass with class-level `_registry` (auto-registers on instantiation). Defines tool semantics for LLM prompt injection. Includes `Tool_respond` return type. All tool instances are module-level globals.
- **`event.py`** — `ToolEventFactory` (singleton) auto-builds `ToolEventSpec` per registered `Tool`. Event naming: `{prefix}.{field}.{tool_dot_name}.{suffix}` where suffix ∈ {called, succeeded, failed, retrying}. Also contains `WriteAgentEvent`/`CriticAgentEvent` static event factories for workflow-level events. Supports static code export via `export_class()`.
- **`graph.py`** — `BusinessGraph` + `BuilderGraph`: DAG workflow engine with `Node`/`Edge` types (START, END, ACTION, DECISION, PARALLEL, CHECKPOINT, AGENT, TOOL). Supports conditional edges, loop-back, topological sort. `build_content_creation_graph()` defines the writer→judge→human review pipeline; `build_critic_ai_graph()` defines the critic pipeline. Exports: PyVis visualization (HTML), Neo4j Cypher.
- **`state.py`** — `Agent_state`: dict-based state container tracking prompt, tool history, tool responses (summary + full), retry counts, session info.
- **`agent/write/`** — `WriteAgent(AgentBase)`: adds `write_agent` field tools (requirements_analysis, outline_generation, draft_writing, rewrite, style_polish, requirements_check).
- **`agent/critic/`** — `CriticAgent(AgentBase)`: evaluation agent (currently minimal).

### Context System (`domain/context/`)
Manages what content gets injected into the LLM prompt with token budgeting and multi-granularity content.

Data flow: `raw content → GranularityProcessor → ContextNode(s) → ContextStore.write() → ContextStore.window() → ContextProvider.get() → ContextEngine.build() → LLM prompt`

- **`store/node.py`** — `ContextNode`: content unit with three granularity levels: `skeleton` (LLM-generated summary, low tokens, default promoted), `chunk` (fixed-size fragments, on-demand), `full` (original content, rarely promoted). Token count auto-estimated as `len(content)//4`.
- **`store/store.py`** — `ContextStore`: manages a "window" of promoted nodes with token budgeting. `write()` splits content via registered processors; `explore()` promotes specific nodes into the window; `window()` returns all promoted nodes for providers. When token budget exceeded, demotes nodes by priority: full → chunk → skeleton (oldest first). Supports file persistence (JSONL) for demoted nodes.
- **`processor.py`** — `GranularityProcessor` implementations: `DocumentProcessor` (long docs: skeleton+chunks+full), `ToolOutputProcessor` (short output→full only; long→skeleton+chunks), `HistoryProcessor` (each turn as a chunk, promoted by default). All accept optional `outline_fn` for LLM-generated skeletons.
- **`context.py`** — `ContextEngine`: iterates providers, calls `get()`, composes pieces into final prompt string. `ContextSlot` describes each provider's scope and enables/disables injection. `ComposeStrategy` controls concatenation (default: double-newline join, skip empty). `SlotScope` types: task, state, memory, tool, history, child.
- **`providers.py`** — Static providers (read from state dict): `UserPromptProvider`, `StateProvider`, `AvailableToolsProvider`. Dynamic providers (read from `ContextStore.window()`): `ToolRespondProvider`, `ExploredContextProvider`, `HistoryProvider`. Providers only format — they never store or make management decisions.

### Memory System (`domain/memory/`)
Stores tool execution outputs for the current session.

- **`short/short_term_memory.py`** — `ShortTermMemory` abstract interface: `store()`, `get()`, `get_summary_list()`, round-based caching (`begin_round`/`store_round`/`get_round`).
- **`short/default_short_term_memory.py`** — `DefaultShortTermMemory`: in-memory dict-based implementation. `_store` holds raw outputs per tool, `_round_cache` holds current-round outputs for `$ref:tool_name#0` resolution. No truncation or compression.

### Infrastructure Layer (`infra/`)
Concrete implementations and external integrations.

- **`eventbus.py`** — `EventBus` (singleton): async pub/sub with middleware chain and glob-pattern subscriptions (`fnmatch`). `publish_one()` for single-handler scenarios.
- **`config.py`** — Central wiring: creates `EventBus`, `ToolEventFactory`, `LLM_Client`, `DefaultShortTermMemory`, `ContextStore` (with registered processors), `ContextEngine` (with all providers), and `agent_dict`. Also defines `make_outline_fn()` for LLM-based skeleton generation. All singletons are imported from here.
- **`LLM/LLM_infra.py`** — `LLM_Client`: thin wrapper over `AsyncOpenAI` supporting GLM, DeepSeek, MiniMax providers. Streaming by default.
- **`tool/tool_bind.py`** — `Tool_bind` (singleton): decorator-based event↔handler binding. `@on_tool.on(event)` for exact match, `@on_tool.on_pattern("*.suffix")` for glob match, `@on_tool.use()` for middleware.
- **`tool/tools_attach_methods.py`** — All tool implementations registered via `@on_tool.on()`. Includes logging middleware, success/fail pattern handlers that call back to `AgentBase.on_tool_call()`, and concrete tool functions.

### Application Layer (`application/`)
Currently empty (`application/agent/` exists but is unused).

## Key Patterns

### Adding a new tool
1. Define a `Tool(...)` instance in `domain/agent/write/tools.py` (or `domain/tool.py` for system tools), setting `field` for grouping.
2. Rebuild factory: `ToolEventFactory(prefix="infra")._build()` picks it up automatically (or re-export static class).
3. Implement the handler in `infra/tool/tools_attach_methods.py` using `@on_tool.on(factory.tool("tool_name").called())`.
4. Handler receives `kwargs` (unpacked from `Event.payload`), returns an `Event` via `factory.tool("name").succeeded(respond)` or `.failed(respond)`.

### Adding a new context provider
1. Define a `ContextSlot` in `domain/context/providers.py` with name, description, and scope.
2. Implement a `ContextProvider` subclass with the slot as class attribute and `get(state) -> list[str]`.
3. Register the provider in `infra/config.py` `providers` list (order = injection order).

### Event flow for tool execution
1. Agent `_think()` → `ContextEngine.build(state)` constructs prompt → LLM decision with `tool_calls`
2. `_execute_tools()` → resolves `$ref` dependencies, runs no-dep calls in parallel first → `factory.tool(name).emit_called(arguments)` → EventBus publishes `*.called`
3. Handler executes, returns `*.succeeded` or `*.failed` event
4. Pattern-matched handlers (`*.succeeded`, `*.failed`) call `agent.on_tool_call()` to update state
5. `on_tool_call()` stores output in `ShortTermMemory`, writes to `ContextStore` (scope="memory"), updates `tool_history`
6. `_pending_tools` decremented; `_tool_done` event set when all complete
7. Agent loop continues — next `_think()` sees updated context via `ContextEngine`

### `$ref` dependency resolution
- LLM outputs `$ref:tool_name#index` as argument values to reference prior tool outputs
- `index=0` → current round output (from `memory.get_round()`), falls back to latest historical
- `index≥1` → specific historical call (from `memory.get()`)
- Tool calls with `$ref` args execute after their dependencies finish

### Singleton pattern
`EventBus`, `Tool_bind`, `ToolEventFactory` are all singletons. They are instantiated in `infra/config.py` and imported from there.

## Running Tests

```bash
pytest test.py -v
```

Single test class:
```bash
pytest test.py::TestToolEventFactory -v
```

Single test:
```bash
pytest test.py::TestWriteAgentCore::test_parse_decision_json -v
```

## Environment

Requires `.env` with API keys: `GLM_API`, `DEEPSEEK_API`, `MINIMAX_API`, `LLM_BASE_URL`. Uses `python-dotenv` to load.

## Dependencies

Key packages: `openai` (async), `pyvis` (graph visualization), `pytest`, `python-dotenv`, `neo4j` (Cypher export support).

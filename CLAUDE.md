# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Novel Agent — a multi-agent novel writing system built on an EDA (Event-Driven Architecture) with a DDD-inspired layered structure. Two agents collaborate: **WriteAgent** (content creation) and **CriticAgent** (quality evaluation), orchestrated through a BusinessGraph workflow engine.

## Architecture

Three-layer architecture: `domain/` → `infra/` → `application/`

### Domain Layer (`domain/`)
Pure business logic, no infrastructure dependencies.

- **`agent_base.py`** — `AgentBase` abstract class: the core think→tool→state loop. Maintains `Agent_state`, builds prompts, parses LLM JSON decisions (`AgentDecision`/`ToolCall`), executes tools via `ToolEventFactory`, supports `$ref:tool_name#index` cross-tool result referencing.
- **`tool.py`** — `Tool` dataclass with class-level `_registry` (auto-registers on instantiation). Defines tool semantics for LLM prompt injection. Includes `Tool_respond` return type. All tool instances are module-level globals.
- **`event.py`** — `ToolEventFactory` (singleton) auto-builds `ToolEventSpec` per registered `Tool`. Event naming: `{prefix}.{field}.{tool_dot_name}.{suffix}` where suffix ∈ {called, succeeded, failed, retrying}. Also contains `WriteAgentEvent`/`CriticAgentEvent` static event factories for workflow-level events. Supports static code export via `export_class()`.
- **`graph.py`** — `BusinessGraph` + `BuilderGraph`: DAG workflow engine with `Node`/`Edge` types (START, END, ACTION, DECISION, PARALLEL, CHECKPOINT, AGENT). Supports conditional edges, loop-back, topological sort, PyVis visualization. `build_content_creation_graph()` defines the writer→judge→human review pipeline; `build_critic_ai_graph()` defines the critic pipeline.
- **`state.py`** — `Agent_state`: dict-based state container tracking prompt, tool history, tool responses (summary + full), retry counts, session info.
- **`agent/write/`** — `WriteAgent(AgentBase)`: adds `write_agent` field tools (requirements_analysis, outline_generation, draft_writing, rewrite, style_polish, requirements_check).
- **`agent/critic/`** — `CriticAgent(AgentBase)`: evaluation agent (currently minimal).

### Infrastructure Layer (`infra/`)
Concrete implementations and external integrations.

- **`eventbus.py`** — `EventBus` (singleton): async pub/sub with middleware chain and glob-pattern subscriptions (`fnmatch`). `publish_one()` for single-handler scenarios.
- **`config.py`** — Wiring: creates `EventBus`, `ToolEventFactory`, `LLM_Client`, and `agent_dict`. All singletons are imported from here.
- **`LLM/LLM_infra.py`** — `LLM_Client`: thin wrapper over `AsyncOpenAI` supporting GLM, DeepSeek, MiniMax providers. Streaming by default.
- **`tool/tool_bind.py`** — `Tool_bind` (singleton): decorator-based event↔handler binding. `@on_tool.on(event)` for exact match, `@on_tool.on_pattern("*.suffix")` for glob match, `@on_tool.use()` for middleware.
- **`tool/tools_attach_methods.py`** — All tool implementations registered via `@on_tool.on()`. Includes logging middleware, success/fail pattern handlers that call back to `AgentBase.on_tool_call()`, and concrete tool functions (read_files, write_files, query_tool_respond, requirements_analysis, outline_generation, draft_writing, rewrite, style_polish, requirements_check).

### Application Layer (`application/`)
Currently empty (`application/agent/` exists but is unused).

## Key Patterns

### Adding a new tool
1. Define a `Tool(...)` instance in `domain/agent/write/tools.py` (or `domain/tool.py` for system tools), setting `field` for grouping.
2. Rebuild factory: `ToolEventFactory(prefix="infra")._build()` picks it up automatically (or re-export static class).
3. Implement the handler in `infra/tool/tools_attach_methods.py` using `@on_tool.on(factory.tool("tool_name").called())`.
4. Handler receives `kwargs` (unpacked from `Event.payload`), returns an `Event` via `factory.tool("name").succeeded(respond)` or `.failed(respond)`.

### Event flow for tool execution
1. Agent `_think()` → LLM decision with `tool_calls`
2. `_execute_tools()` → `factory.tool(name).emit_called(arguments)` → EventBus publishes `*.called`
3. Handler executes, returns `*.succeeded` or `*.failed` event
4. Pattern-matched handlers (`*.succeeded`, `*.failed`) call `agent.on_tool_call()` to update state
5. `on_tool_call()` decrements `_pending_tools`, sets `_tool_done` event when all complete
6. Agent loop continues with updated state

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

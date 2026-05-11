# Repository Guidelines

## Project Structure & Module Organization

Agent FLOW is a Python multi-agent writing system organized around a DDD-style layered architecture. Core business logic lives in `domain/`: agents, state, tool definitions, event factories, workflow graphs, context providers, and short-term memory. Infrastructure integrations live in `infra/`: event bus, tool bindings, LLM client, configuration wiring, database/RAG support, and built-in tool implementations. `application/`, `api/`, and `mid/` are currently light placeholders for higher-level orchestration and interfaces. Tests are top-level Python files such as `test.py` and `test_update_plan.py`; exploratory work may appear in `test.ipynb`. Architecture notes are in `README.md` and `CLAUDE.md`.
本项目的python环境为/Users/zxcvbzzy1/miniconda3/envs/MY_env/bin

## Build, Test, and Development Commands

Use a local virtual environment and install the project dependencies manually; this repo currently has no pinned requirements file.

```bash
pytest test.py -v
pytest test_update_plan.py -v
pytest test.py::TestToolEventFactory -v
```

Run `pytest ... -v` for focused validation. The LLM-backed runtime expects `.env` values such as `GLM_API`, `DEEPSEEK_API`, `MINIMAX_API`, and `LLM_BASE_URL`; unit tests should avoid real network calls unless explicitly intended.

## Coding Style & Naming Conventions

Follow the existing Python style: 4-space indentation, type hints where useful, dataclasses for structured domain objects, and small classes around clear responsibilities. Keep domain code independent of infrastructure imports. Use snake_case for functions, variables, modules, and test names; use PascalCase for classes such as `BusinessGraph`, `ContextEngine`, and `DefaultShortTermMemory`. Tool instances are registered as module-level globals, so name new tools clearly and keep their `field` grouping consistent.

## Testing Guidelines

Tests use `pytest`. Prefer focused unit tests for domain behavior, event naming, context strategy pipelines, memory behavior, and tool handler core logic. Name files `test_*.py` and test methods `test_*`. When testing infrastructure-facing code, isolate pure logic where possible and avoid importing modules that trigger full tool registration unless the test needs that behavior.

## Commit & Pull Request Guidelines

Recent commits use short lowercase identifiers such as `context_2`, `plan_1`, and `add context`. Keep commits concise and scoped to one change. For pull requests, include a brief summary, affected modules, test commands run, and any required `.env` or service setup. Add screenshots or generated graph artifacts only when changing visual exports such as PyVis output.

## Agent-Specific Instructions

When adding tools, update both the domain `Tool(...)` definition and the matching handler registration in `infra/tool/tools_attach_methods.py`. When adding context behavior, register providers in `infra/config.py`; provider order controls prompt injection order.

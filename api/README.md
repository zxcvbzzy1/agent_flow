# Agent Flow API

`api/` 是 FastAPI 的 HTTP/SSE 适配层，只负责请求解析、响应返回、跨域和路由挂载。业务用例放在 `application/services/`，领域能力仍由 `domain/` 和 `infra/` 提供。

项目总览与领域/基础设施说明见：[../README.md](../README.md)。

## 启动方式

项目 Python 环境：

```bash
/Users/zxcvbzzy1/miniconda3/envs/MY_env/bin
```

从 `agent_flow/` 目录启动：

```bash
/Users/zxcvbzzy1/miniconda3/envs/MY_env/bin/python -m uvicorn api.index:app --host 127.0.0.1 --port 8000
```

默认 MongoDB：

```text
mongodb://localhost:27017/
database: agent_flow
```

如果 MongoDB 不可用，`DocumentStore` 会降级到内存存储，方便本地测试。

## 目录职责

当前 `api/` 目录结构：

```text
api/
  index.py                         # FastAPI app、CORS、/health、业务路由挂载
  README.md                        # 当前 API 文档
  core/
    config.py                      # API 配置：app、MongoDB、CORS origins
    dependencies.py                # ServiceContainer、依赖构建与运行时 hook 注册
  tools/
    router.py                      # 工具列表、上传、删除
    schemas.py                     # 工具上传请求体
  contexts/
    router.py                      # ContextEngine 列表、catalog、创建、查询、删除
    schemas.py                     # Context 创建请求体
  agents/
    router.py                      # Agent 列表、创建、删除
    schemas.py                     # Agent 创建请求体
  runs/
    router.py                      # Run 列表、创建、查询、SSE、确认、中断
    schemas.py                     # Run 创建请求体
  conversations/
    router.py                      # 会话、消息、从消息启动 run、删除会话
    schemas.py                     # 会话、消息、会话 run 请求体
```

| 文件/目录 | 职责 |
|---|---|
| `api/index.py` | 创建 FastAPI app，注册 CORS，挂载所有业务路由，提供 `/health`。 |
| `api/core/config.py` | API 配置，包括 app 名称、MongoDB 地址、数据库名、CORS origins。 |
| `api/core/dependencies.py` | 构建并缓存 `ServiceContainer`，统一注入应用服务，并注册前端事件桥、人类确认、run context hook。 |
| `api/tools/router.py` | 工具注册、查询与删除接口。 |
| `api/tools/schemas.py` | 工具上传请求体。 |
| `api/contexts/router.py` | 上下文配置 catalog、创建、查询与删除接口。 |
| `api/contexts/schemas.py` | 上下文创建请求体。 |
| `api/agents/router.py` | Agent 创建、查询与删除接口。 |
| `api/agents/schemas.py` | Agent 创建请求体。 |
| `api/runs/router.py` | React/Plan run 创建、查询、取消、SSE 事件流、人类确认接口。 |
| `api/runs/schemas.py` | Run 创建请求体。 |
| `api/conversations/router.py` | 会话、消息、从会话消息创建 run、删除会话的接口。 |
| `api/conversations/schemas.py` | 会话、消息、会话 run 请求体。 |

对应应用层服务：

| 服务 | 文件 | 职责 |
|---|---|---|
| `ToolRegistryService` | `application/services/tools.py` | 加载内置工具，上传工具声明和实现源码，注册工具事件，删除上传工具。 |
| `ContextService` | `application/services/contexts.py` | 创建 `ContextEngine`，管理默认 executor/planner/step 上下文。 |
| `AgentFactoryService` | `application/services/agents.py` | 创建 planner 或 executor agent，删除非默认 Agent 并清理关联 run/event。 |
| `RunOrchestrationService` | `application/services/runs.py` | 创建 react/plan run，后台执行 Agent 或 `PlanOrchestrator`，写回 assistant 消息。 |
| `EventStreamService` | `application/services/events.py` | 写入事件日志并提供 SSE 输出。 |
| `StreamingObservableLLMClient` | `application/services/llm_streaming.py` | 包装 LLM 流式输出，发布 `llm.*` 与结构化 Agent 输出事件。 |
| `ConversationService` | `application/services/conversations.py` | 创建会话、保存消息，删除会话及关联消息、run/event。 |
| `FrontendEventBridge` | `application/events/bridge.py` | 将内部工具/Agent/Workflow 事件镜像为前端 SSE 事件。 |
| `HumanConfirmationService` | `application/events/human_confirmation.py` | 管理网页人类确认请求与确认结果。 |

## 基础接口

### `GET /health`

健康检查。

响应示例：

```json
{
  "status": "ok",
  "mongo": "mongodb"
}
```

`mongo` 可能是：

- `mongodb`：当前使用本地 MongoDB。
- `memory`：MongoDB 不可用，已降级到内存存储。

## 工具接口

文件：

- `api/tools/router.py`
- `api/tools/schemas.py`
- `application/services/tools.py`

### `GET /api/tools`

返回当前已注册工具列表，包括内置工具和上传工具。

响应结构：

```json
{
  "items": [
    {
      "name": "bash",
      "description": "执行 bash 命令...",
      "field": "system",
      "input_schema": {},
      "metadata": {},
      "events": [
        "infra.system.bash.called",
        "infra.system.bash.failed",
        "infra.system.bash.retrying",
        "infra.system.bash.succeeded"
      ]
    }
  ]
}
```

### `POST /api/tools/upload`

上传工具声明和工具实现源码。本阶段不做源码审查、沙箱隔离或鉴权。

请求体：

```json
{
  "name": "unit_test_tool",
  "description": "工具说明",
  "field": "system",
  "input_schema": {
    "type": "object",
    "properties": {}
  },
  "metadata": {
    "require_human_confirm": false
  },
  "source_code": ""
}
```

### `DELETE /api/tools/{tool_id}`

删除上传工具，并从运行时工具 registry 中移除。内置工具会被后端保护，返回 `400`。

响应结构：

```json
{
  "item": {
    "deleted": true,
    "tool_id": "unit_test_tool",
    "stats": {
      "tools": 1,
      "source_deleted": true
    }
  }
}
```

响应结构：

```json
{
  "item": {
    "name": "unit_test_tool",
    "description": "工具说明",
    "field": "system",
    "input_schema": {},
    "metadata": {},
    "events": [],
    "tool_id": "unit_test_tool",
    "source_path": "...",
    "uploaded": true
  }
}
```

## 上下文接口

文件：

- `api/contexts/router.py`
- `api/contexts/schemas.py`
- `application/services/contexts.py`

### `GET /api/contexts`

查询所有上下文配置，并返回当前进程内 engine 加载状态。

### `GET /api/contexts/catalog`

查询可用的内置 provider、strategy 和默认模板。

### `POST /api/contexts`

按新版 `provider_config` 创建上下文配置，并在服务内构建对应 `ContextEngine`。`provider_config` 不能为空。

请求体：

```json
{
  "name": "自定义执行者上下文",
  "kind": "executor",
  "provider_config": [
    {"provider_id": "user_prompt", "enabled": true, "params": {}},
    {"provider_id": "available_tools", "enabled": true, "params": {"available_fields": ["system", "human"]}},
    {
      "provider_id": "tool_output",
      "enabled": true,
      "params": {
        "memory_field": "tool_respond",
        "strategy_config": {
          "pipeline": [
            {"type": "full_history"},
            {"type": "recency", "keep_last": 5}
          ]
        }
      }
    }
  ]
}
```

`kind` 当前支持：

- `executor`：执行型 Agent 上下文。
- `planner`：PlanAgent 编排上下文。
- `step`：Orchestrator 生成单步骤 prompt 的上下文。

响应结构：

```json
{
  "item": {
    "context_id": "uuid",
    "kind": "executor",
    "name": "自定义执行者上下文",
    "provider_config": [],
    "provider_count": 3,
    "provider_names": ["user_prompt", "available_tools", "tool_output"],
    "engine_loaded": true
  }
}
```

### `GET /api/contexts/{context_id}`

查询单个上下文配置。

### `DELETE /api/contexts/{context_id}`

删除未被引用的自定义 ContextEngine。默认 ContextEngine（`default_executor`、`default_planner`、`default_step`）不可删除；已被 Agent 或 Run 引用的 ContextEngine 会返回 `400`。

成功响应：

```json
{
  "item": {
    "deleted": true,
    "context_id": "uuid",
    "stats": {
      "contexts": 1
    }
  }
}
```

## Agent 接口

文件：

- `api/agents/router.py`
- `api/agents/schemas.py`
- `application/services/agents.py`

### `GET /api/agents`

返回已创建 Agent 列表。

### `POST /api/agents`

创建 planner 或 executor agent。

请求体：

```json
{
  "name": "默认执行者",
  "agent_type": "executor",
  "context_id": "default_executor",
  "role_prompt": "",
  "metadata": {}
}
```

`agent_type` 当前支持：

- `planner`：创建 `PlanAgent`。
- `executor`：创建执行型 `AgentBase` 子类。

服务启动时会创建默认 Agent：

- `default_planner`
- `default_executor`

### `DELETE /api/agents/{agent_id}`

删除非默认 Agent，移除运行时实例，并清理该 Agent 关联的 runs 和 events。默认 Agent 会被后端保护，返回 `400`。

响应结构：

```json
{
  "item": {
    "deleted": true,
    "agent_id": "uuid",
    "stats": {
      "agents": 1,
      "runs": 1,
      "events": 3
    }
  }
}
```

## Run 编排接口

文件：

- `api/runs/router.py`
- `api/runs/schemas.py`
- `application/services/runs.py`
- `application/services/events.py`

### `GET /api/runs`

查询已创建 run 列表，按创建时间倒序返回。

响应结构：

```json
{
  "items": [
    {
      "run_id": "uuid",
      "status": "pending",
      "prompt": "...",
      "planner_agent_id": "default_planner",
      "executor_agent_ids": ["default_executor"],
      "context_id": "default_step",
      "created_at": 1710000000.0,
      "started_at": null,
      "finished_at": null
    }
  ]
}
```

### `POST /api/runs`

创建一次 Agent run。`mode=react` 调用单一 executor，`mode=plan` 使用 planner + executors 编排。默认会立即后台启动。

React 请求体：

```json
{
  "mode": "react",
  "prompt": "你好",
  "executor_agent_id": "default_executor",
  "conversation_id": null,
  "message_id": null,
  "auto_start": true
}
```

Plan 请求体：

```json
{
  "mode": "plan",
  "prompt": "请分析项目并生成总结",
  "planner_agent_id": "default_planner",
  "executor_agent_ids": ["default_executor"],
  "context_id": "default_step",
  "max_replan_rounds": 3,
  "conversation_id": null,
  "message_id": null,
  "auto_start": true
}
```

字段说明：

| 字段 | 说明 |
|---|---|
| `mode` | 运行模式：`react` 或 `plan`。 |
| `prompt` | 用户任务输入。 |
| `executor_agent_id` | React 模式下使用的执行者 Agent id。 |
| `planner_agent_id` | 编排者 Agent id，默认 `default_planner`。 |
| `executor_agent_ids` | 执行者 Agent id 列表。 |
| `context_id` | step prompt 上下文，默认 `default_step`。 |
| `max_replan_rounds` | 最大 replan 次数。 |
| `conversation_id` | 可选，会话关联。 |
| `message_id` | 可选，消息关联。 |
| `auto_start` | 是否创建后立即后台执行，测试时可设为 `false`。 |

响应结构：

```json
{
  "item": {
    "run_id": "uuid",
    "prompt": "...",
    "status": "pending",
    "plan": {},
    "final": ""
  }
}
```

### `GET /api/runs/{run_id}`

查询 run 当前状态。

常见状态：

- `pending`
- `running`
- `finished`
- `failed`
- `cancelled`

### `POST /api/runs/{run_id}/cancel`

中断当前 run。中断后 run 状态会更新为 `cancelled`，并复用 `workflow.failed` SSE 终止事件，payload 中包含 `"cancelled": true`。

响应结构：

```json
{
  "item": {
    "run_id": "uuid",
    "status": "cancelled",
    "cancel_reason": "用户中断",
    "finished_at": 1710000000.0
  }
}
```

### `GET /api/runs/{run_id}/events`

SSE 事件流。

响应 `Content-Type`：

```text
text/event-stream
```

事件格式：

```text
event: workflow.started
data: {"event_id":"...","run_id":"...","name":"workflow.started","payload":{}}
```

常见事件：

- `workflow.started`
- `plan.generated`
- `wave.completed`
- `plan.replanned`
- `plan.step.observed`
- `plan.wave.completed`
- `workflow.finished`
- `workflow.failed`
- `tool.called`
- `tool.succeeded`
- `tool.failed`
- `tool.retrying`
- `artifacts.*`
- `agent.failed`
- `plan.step.failed`
- `human.confirmation.requested`
- `human.confirmation.resolved`

`tool.*`、`artifacts.*`、`agent.failed`、`plan.step.failed` 来自内部 `infra.eventbus` 到前端 SSE 的应用层桥接。它们只用于前端观察，不改变内部工具执行流程。

用户中断 run 时也会发送 `workflow.failed`，通过 `payload.cancelled === true` 区分普通失败。

### `GET /api/runs/{run_id}/confirmations`

查询当前 run 等待网页处理的人类确认请求。

响应结构：

```json
{
  "items": [
    {
      "confirmation_id": "uuid",
      "run_id": "uuid",
      "agent_id": "default_executor",
      "tool_name": "bash",
      "called_event_name": "infra.system.bash.called",
      "arguments": {
        "command": "pwd"
      },
      "status": "pending",
      "created_at": 1710000000.0
    }
  ]
}
```

### `POST /api/runs/{run_id}/confirmations/{confirmation_id}`

批准或拒绝网页人类确认请求。该接口会唤醒等待中的工具调用协程，并向 SSE 推送 `human.confirmation.resolved`。

请求体：

```json
{
  "approved": true,
  "reason": "允许执行"
}
```

响应结构：

```json
{
  "item": {
    "confirmation_id": "uuid",
    "run_id": "uuid",
    "status": "resolved",
    "approved": true,
    "reason": "允许执行"
  }
}
```

## 会话、消息接口

文件：

- `api/conversations/router.py`
- `api/conversations/schemas.py`
- `application/services/conversations.py`

### `POST /api/conversations`

创建会话。

请求体：

```json
{
  "title": "项目分析对话",
  "metadata": {}
}
```

### `GET /api/conversations`

查询会话列表。

### `GET /api/conversations/{conversation_id}`

查询单个会话。

### `DELETE /api/conversations/{conversation_id}`

删除会话，并级联删除该会话下的 messages、runs，以及这些 run 对应的 events。

响应结构：

```json
{
  "item": {
    "deleted": true,
    "conversation_id": "uuid",
    "stats": {
      "conversations": 1,
      "messages": 2,
      "runs": 1,
      "events": 5
    }
  }
}
```

### `GET /api/conversations/{conversation_id}/messages`

查询会话消息列表，按创建时间升序返回。

### `POST /api/conversations/{conversation_id}/messages`

写入用户或 assistant 消息。

请求体：

```json
{
  "role": "user",
  "content": "请帮我分析这个项目",
  "metadata": {},
  "run_id": null
}
```

### `POST /api/conversations/{conversation_id}/runs`

从指定用户消息直接创建 run，并把 `conversation_id`、`message_id` 关联到 run。

请求体：

```json
{
  "mode": "react",
  "message_id": "uuid",
  "executor_agent_id": "default_executor",
  "auto_start": true
}
```

或：

```json
{
  "mode": "plan",
  "message_id": "uuid",
  "planner_agent_id": "default_planner",
  "executor_agent_ids": ["default_executor"],
  "context_id": "default_step",
  "max_replan_rounds": 3,
  "auto_start": true
}
```

当 run 完成后：

- 成功时会把 Agent 最终结果写入一条 `assistant` 消息。
- 如果 run 失败或取消，错误通过 run 状态和 SSE 事件展示，不写入 assistant 消息。

## 典型调用流

### 直接创建编排任务

1. `GET /api/tools` 查看可用工具。
2. `POST /api/contexts` 可选，创建自定义上下文。
3. `POST /api/agents` 可选，创建自定义 planner/executor。
4. `POST /api/runs` 创建编排任务。
5. `GET /api/runs/{run_id}/events` 监听 SSE。
6. `GET /api/runs/{run_id}` 查询最终结果。

### 从聊天消息创建任务

1. `POST /api/conversations` 创建会话。
2. `POST /api/conversations/{conversation_id}/messages` 写入用户消息。
3. `POST /api/conversations/{conversation_id}/runs` 从该消息创建 react 或 plan run。
4. `GET /api/runs/{run_id}/events` 监听执行事件。
5. `GET /api/conversations/{conversation_id}/messages` 查看用户消息和 assistant 回复。

## 跨域配置

CORS 在 `api/index.py` 中注册，默认允许：

```text
http://localhost:5173
http://127.0.0.1:5173
```

可通过环境变量扩展：

```bash
export AGENT_FLOW_CORS_ORIGINS="http://localhost:5173,http://127.0.0.1:5173,http://localhost:3000"
```

## 测试

从 `agent_flow/` 目录运行：

```bash
/Users/zxcvbzzy1/miniconda3/envs/MY_env/bin/python -m pytest -q
```

当前 API 测试文件：

```text
api_services_test.py
```

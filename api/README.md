# Agent Flow API

`api/` 是 FastAPI 的 HTTP/SSE 适配层，只负责请求解析、响应返回、跨域和路由挂载。业务用例放在 `application/services/`，领域能力仍由 `domain/` 和 `infra/` 提供。

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

| 文件/目录 | 职责 |
|---|---|
| `api/index.py` | 创建 FastAPI app，注册 CORS，挂载所有业务路由，提供 `/health`。 |
| `api/core/config.py` | API 配置，包括 app 名称、MongoDB 地址、数据库名、CORS origins。 |
| `api/core/dependencies.py` | 构建并缓存 `ServiceContainer`，统一注入应用服务。 |
| `api/tools/router.py` | 工具注册与查询接口。 |
| `api/tools/schemas.py` | 工具上传请求体。 |
| `api/contexts/router.py` | 上下文配置创建与查询接口。 |
| `api/contexts/schemas.py` | 上下文创建请求体。 |
| `api/agents/router.py` | Agent 创建与查询接口。 |
| `api/agents/schemas.py` | Agent 创建请求体。 |
| `api/runs/router.py` | Agent 编排 run 创建、查询、SSE 事件流接口。 |
| `api/runs/schemas.py` | Run 创建请求体。 |
| `api/conversations/router.py` | 会话、消息、对话队列、从会话创建 run 的接口。 |
| `api/conversations/schemas.py` | 会话、消息、队列请求体。 |

对应应用层服务：

| 服务 | 文件 | 职责 |
|---|---|---|
| `ToolRegistryService` | `application/services/tools.py` | 加载内置工具，上传工具声明和实现源码，注册工具事件。 |
| `ContextService` | `application/services/contexts.py` | 创建 `ContextEngine`，管理默认 executor/planner/step 上下文。 |
| `AgentFactoryService` | `application/services/agents.py` | 创建 planner 或 executor agent。 |
| `RunOrchestrationService` | `application/services/runs.py` | 创建 run，后台执行 `PlanOrchestrator`，更新 run 和对话队列状态。 |
| `EventStreamService` | `application/services/events.py` | 写入事件日志并提供 SSE 输出。 |
| `ConversationService` | `application/services/conversations.py` | 创建会话、保存消息、维护 message queue。 |

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

### `POST /api/contexts`

创建上下文配置，并在服务内构建对应 `ContextEngine`。

请求体：

```json
{
  "name": "Default Executor",
  "kind": "executor",
  "provider_config": [],
  "strategy_config": {
    "type": "full_history",
    "keep_last": 10,
    "token_limit": 4000
  },
  "available_fields": ["system", "write_agent", "human"]
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
    "name": "Default Executor",
    "provider_config": [],
    "strategy_config": {},
    "available_fields": []
  }
}
```

### `GET /api/contexts/{context_id}`

查询单个上下文配置。

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

## Run 编排接口

文件：

- `api/runs/router.py`
- `api/runs/schemas.py`
- `application/services/runs.py`
- `application/services/events.py`

### `POST /api/runs`

创建一次编排任务。默认会立即后台启动。

请求体：

```json
{
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
| `prompt` | 用户任务输入。 |
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
- `agent.failed`
- `plan.step.failed`
- `human.confirmation.requested`
- `human.confirmation.resolved`

`tool.*`、`agent.failed`、`plan.step.failed` 来自内部 `infra.eventbus` 到前端 SSE 的应用层桥接。它们只用于前端观察，不改变内部工具执行流程。

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

## 会话、消息与队列接口

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

### `POST /api/conversations/{conversation_id}/queue`

将指定消息加入对话队列。如果不传 `message_id`，默认取最新一条用户消息。

请求体：

```json
{
  "message_id": "uuid",
  "metadata": {}
}
```

队列状态：

- `pending`
- `processing`
- `done`
- `failed`
- `cancelled`

### `GET /api/conversations/{conversation_id}/queue`

查询会话队列。

### `POST /api/conversations/{conversation_id}/runs`

从最新 pending 用户消息创建 run，并把 `conversation_id`、`message_id` 关联到 run。创建成功后，对应队列项会标记为 `processing`。

请求体：

```json
{
  "planner_agent_id": "default_planner",
  "executor_agent_ids": ["default_executor"],
  "context_id": "default_step",
  "max_replan_rounds": 3
}
```

当 run 完成后：

- 队列项更新为 `done`。
- 如果 run 失败，队列项更新为 `failed`。
- 成功时会把 Agent 最终结果写入一条 `assistant` 消息。

## 典型调用流

### 直接创建编排任务

1. `GET /api/tools` 查看可用工具。
2. `POST /api/contexts` 可选，创建自定义上下文。
3. `POST /api/agents` 可选，创建自定义 planner/executor。
4. `POST /api/runs` 创建编排任务。
5. `GET /api/runs/{run_id}/events` 监听 SSE。
6. `GET /api/runs/{run_id}` 查询最终结果。

### 从聊天消息创建编排任务

1. `POST /api/conversations` 创建会话。
2. `POST /api/conversations/{conversation_id}/messages` 写入用户消息。
3. `POST /api/conversations/{conversation_id}/queue` 将消息入队。
4. `POST /api/conversations/{conversation_id}/runs` 从队列消息创建 run。
5. `GET /api/runs/{run_id}/events` 监听编排事件。
6. `GET /api/conversations/{conversation_id}/messages` 查看用户消息和 assistant 回复。

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

# 长期记忆

## 分层与接入

- **L3**：`agent_flow/store/{user_id}/{room_id}/{conversation_id}/runs/{run_id}.md`，不可变 UTF-8 原文。更新、聚合写入同级 `derived/{source_id}.md`。私聊房间标识为 `direct`。
- **L2**：MongoDB `long_memory_sources` 和 `long_memory_blocks`；结构见 `models.py`。文件路径相对 store；行号从 1 开始，首尾均包含；块内容等于文件对应完整行。
- **L1**：`LongTermMemoryProvider` 格式化 `state["long_term_memory"]`，不查询数据库、不调用模型、不修改使用次数。

领域模块定义模型与可替换策略，`infra/memory/` 实现 MongoDB 和文件系统适配器，`application/services/long_memory.py` 提供业务操作。API 与 IM 容器启动时创建目录和索引，并暴露 `long_memory` 服务。没有新增 HTTP 管理接口。

IM 群聊派发、私聊回复和重新生成均以**登录用户**构造 `MemoryScope`。旧调用未提供可信身份时跳过长期记忆，不使用公共默认用户。内部 `create_run` 可传 `memory_scope` 和 `user_question`；后者是完整用户原问题，独立于执行用的拼接 prompt。

ReAct、Plan、私聊在运行前召回一次。Plan 的 planner 和 executors 共用结果，默认 step prompt 不重复附加长期记忆。上下文中声明 `{"provider_id": "long_term_memory", "enabled": false, "params": {}}` 可禁用注入。仅启用的 Provider 注入计入使用次数，每块每 run 一次。

终态归档位于 `RunStateService.finish_control`，在释放运行控制之前执行，覆盖成功、失败、取消与失联收尾。它从事件历史接口收集当前 run 的 `agent.think`，保留事件、执行和智能体 ID；不归档其他 reasoning 或流式增量事件。归档失败保留 run 结果，将 `memory_status=failed` 和 `memory_error` 写入 run；无身份写 `memory_status=skipped`。

## 使用服务

以下调用为内部 Python 接口；调用方负责提供可信用户身份。服务操作为同步存储操作，异步运行接入已通过 `asyncio.to_thread` 调用。

```python
from domain.memory.long import MemoryScope

memory = bridge.long_memory  # 或 ServiceContainer.long_memory
scope = MemoryScope(user_id, room_id, conversation_id)
hits = memory.recall(scope, "之前如何配置 Python 项目？", run_id=run_id)

# 内容更新生成新版本，返回新块；标签/metadata 可单独修改。
updated = memory.update_memory(user_id, block_id, content="新的记忆内容", tags=["python"])
memory.update_memory(user_id, updated["block_id"], expires_at=None)  # 清除过期时间

# 聚合需要同一用户至少两个不同的有效块；内容由调用方提供。
merged = memory.aggregate_memories(user_id, block_ids, "调用方整理后的内容", scope)
memory.expire_memory(user_id, merged["block_id"])
memory.delete_memory(user_id, block_id)

# L3 已存在、L2 索引失败时，按来源 ID 显式重试。
memory.reindex_source(source_id)
# 整体归档失败时，运行记录仍存在可重试终态处理。
await bridge.runs.states.finish_control(run_id)
```

## 生命周期与默认策略

- `block_id` 对应一个内容版本，`memory_id + version` 关联更新历史。更新后的旧块为 `superseded`，聚合原块为 `merged`，均退出召回。派生块通过 `derived_from_block_ids` 追溯来源。
- 删除、显式过期会沿派生关系停用后续记忆。删除消息、会话或房间同步停用其来源和派生块，混合来源聚合块整体停用。MD 和数据库历史保留，不使用 MongoDB TTL 物理删除。
- `expires_at` 为 UTC Unix 时间戳，默认 `None`。查询时即刻排除过期块；聚合继承原块最早过期时间。重新索引是插入缺失块，不覆盖已存在块的状态、版本、标签或使用次数。
- 源文档保存分段行号，不通过重新解析用户 Markdown 推断边界；正常分块约 1,000 个估算 token，无重叠，超长单行完整保留。
- `TokenCounter` 默认 `cjk-char-latin-4-v1`：中文按字符、其余内容按每 4 字符估算，结果不是模型 tokenizer 的精确 token 数。
- `MemoryRouter` 默认全部接纳且标记 `unclassified`。可替换接口返回是否收录、类型、标签、过期时间；本次没有 JEV 集成或自动摘要。
- `MemoryRetriever` 默认 BM25，`k1=1.5`、`b=0.75`，非负 IDF。英文按小写词项，中文按连续片段单字和双字词项；只在当前用户有效块中计算语料统计。默认最多 6 块、格式化后约 4,000 token，无匹配则为空。
- MongoDB 以内部 `_usage_run_ids` 数组实现每块每 run 的原子计数去重，不返回给召回方。本版采用用户有效块的内存 BM25 排序；大规模索引和使用记录压缩可后续替换仓储/检索实现。

## 验证

```bash
/Users/zxcvbzzy1/miniconda3/envs/MY_env/bin/python -m pytest agent_flow/long_memory_test.py -v
```

测试使用临时目录、内存持久层和伪事件/LLM，无需 MongoDB、Redis 或真实模型。不会回填历史 run。

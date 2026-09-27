# 长期记忆

## 分层与接入

- **L3**：`agent_flow/store/{user_id}/{room_id}/{conversation_id}/runs/{run_id}.md`，不可变 UTF-8 原文。更新、聚合写入同级 `derived/{source_id}.md`。私聊房间标识为 `direct`。
- **L2**：MongoDB `long_memory_sources` 和 `long_memory_blocks`；结构见 `models.py`。文件路径相对 store；行号从 1 开始，首尾均包含。规则块正文等于文件对应完整行；JEV 提取块正文是提取结果，行号仅定位原文证据，不要求逐字相等。
- **L1**：`LongTermMemoryProvider` 格式化 `state["long_term_memory"]`，不查询数据库、不调用模型、不修改使用次数。

领域模块定义模型、运行时选项、模型接口和版本化提示词，`infra/memory/` 实现 MongoDB、文件系统、JEV SDK 和提取 LLM 适配器，`application/services/long_memory.py` 提供归档、生命周期、运行时召回及 `build_long_memory()` 组装函数；`memory_processing.py` 编排后台批次。`infra` 不导入 `application`，不新增 bootstrap。IM 个人检索配置与管理分页位于 `im_backend`，通过 `settings_loader` 将检索设置传给下一次运行。API 与 IM lifespan 启停记忆处理服务；`/memory` 提供记忆查询、检索测试和记忆生成管理。

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

# 规则模式 L3 已存在、L2 索引失败时，按来源 ID 显式重试。
memory.reindex_source(source_id)
# JEV 失败批次使用管理接口重试；reindex 不会绕过筛选将原文发布到 L2。
memory.processing.repository.retry(user_id, batch_id)
# 整体归档失败时，运行记录仍存在可重试终态处理。
await bridge.runs.states.finish_control(run_id)
```

## 生命周期与默认策略

- `block_id` 对应一个内容版本，`memory_id + version` 关联更新历史。更新后的旧块为 `superseded`，聚合原块为 `merged`，均退出召回。派生块通过 `derived_from_block_ids` 追溯来源。
- 删除、显式过期会沿派生关系停用后续记忆。删除消息、会话或房间同步停用其来源和派生块，混合来源聚合块整体停用。MD 和数据库历史保留，不使用 MongoDB TTL 物理删除。
- `expires_at` 为 UTC Unix 时间戳，默认 `None`。查询时即刻排除过期块；聚合继承原块最早过期时间。重新索引是插入缺失块，不覆盖已存在块的状态、版本、标签或使用次数。
- 源文档保存分段行号，不通过重新解析用户 Markdown 推断边界；正常分块约 1,000 个估算 token，无重叠，超长单行完整保留。
- `TokenCounter` 默认 `cjk-char-latin-4-v1`：中文按字符、其余内容按每 4 字符估算，结果不是模型 tokenizer 的精确 token 数。
- `rules` 模式的 `MemoryRouter` 默认全部接纳且标记 `unclassified`，接口返回是否收录、类型、标签、过期时间。生产组装默认启用 JEV，底层 `LongTermMemoryService` 可单独用于规则模式和测试。
- `MemoryRetriever` 默认 BM25，`k1=1.5`、`b=0.75`，非负 IDF。英文按小写词项，中文按连续片段单字和双字词项；只在当前用户有效块中计算语料统计。默认最多 6 块、格式化后约 4,000 token，无匹配则为空。
- MongoDB 以内部 `_usage_run_ids` 数组实现每块每 run 的原子计数去重，不返回给召回方。本版采用用户有效块的内存 BM25 排序；大规模索引和使用记录压缩可后续替换仓储/检索实现。

## 记忆管理页面与接口

顶部导航“记忆”分为“记忆查询”“检索配置与测试”和“记忆生成”。记忆列表支持关键词（正文/标签字面匹配）、状态、房间和会话筛选，多来源记忆可按任一证据的房间或会话筛选；详情区分提取正文、结构、分类概率、所有原文证据和版本关系。过期时间到达后，页面直接显示已过期，无需后台扫描更新状态。页面不提供编辑、删除、合并或重新索引功能。

下列接口均需登录，作用域由服务端认证确定，不能通过请求指定其他用户。无法访问的块或来源返回 404。

| `/api/im/memory` 下的接口 | 功能 |
| --- | --- |
| `GET /blocks` | 参数 `q/status/room_id/conversation_id/page/page_size`，默认有效状态、每页 20 条，最多 100 条 |
| `GET /blocks/{block_id}` | 正文、来源摘要及版本/派生关联 |
| `GET /sources/{source_id}` | 只读来源文件，路径来自已授权来源记录 |
| `GET /scopes` | 当前用户记忆涉及的房间与会话选项 |
| `GET /settings` | 当前配置 `item` 与系统默认值 `defaults` |
| `PUT /settings` | 保存个人检索配置 |
| `POST /recall-test` | `{query, config?}`，不传 config 时使用个人已保存配置 |
| `GET /processing-settings` | 个人生成配置、默认值、固定 24 小时规则及模型凭据就绪状态（不返回密钥） |
| `PUT /processing-settings` | 保存 `mode/run_batch_size/noul_threshold`；生成独立配置版本 |
| `GET /batches` | 当前账号的批次分页，默认 20、最多 100 条 |
| `GET /batches/{batch_id}` | 阶段、计数、失败原因、各类别进度和前 100 条候选诊断 |
| `POST /batches/{batch_id}/retry` | 仅重试自己的失败批次，保留检查点；非失败状态返回 409，外部用户返回 404 |

检索配置由 IM 后端保存到 `long_memory_settings`，`user_id` 唯一。默认 `limit=6`、`token_budget=4000`、`k1=1.5`、`b=0.75`；可设置范围分别为 1–50、128–32000、(0,5]、[0,1]。未配置用户直接读取系统默认值，不做历史迁移。

保存从当前用户**下一次 run**开始生效，已运行的任务继续使用启动时选中的记忆。ReAct、Plan 和私聊共用配置。页面的“恢复默认”只改变草稿，“测试召回”使用草稿，“保存配置”才影响实际运行。查询页筛选不会改变召回范围，同账号仍跨房间召回。

`recall_result(scope, query, config=...)` 接受运行时 `RecallOptions` 并返回 `RecallResult`，与 `recall(...)` 共用排序、预算选择和 Provider。IM 后端先校验 `RetrievalSettings`，再转换为运行时选项。结果包含有效语料数、匹配数、选中块、前 100 条候选诊断、最终 L1 文本及 token 估算；超出 100 条有截断标识，所有已选块仍完整返回。诊断区分 `selected`、`limit`、`token_budget`；空结果区分 `no_memory`、`no_match`、`token_budget`。BM25 分数不是概率。

测试是只读操作：不会生成 run、增加使用次数、保存配置或更新记忆。每次默认检索按本次配置创建独立 BM25 实例，不修改共享参数。自定义检索器可通过 `retriever_factory(config)` 扩展，原有显式 `retriever` 注入仍兼容。

## JEV 分类和批量提取

未配置账号的默认值为 `mode=jev`、`run_batch_size=1`、`noul_threshold=0.7`，分别可设置 `jev/rules`、1–100、0–1。生成配置单独保存在 `long_memory_processing_settings`；只对之后归档的 run 生效，已入队来源带有不可变配置快照和版本，旧记忆不迁移。

L3 归档不等待模型。新来源以 `index_status=queued` 持久化；后台按用户和配置版本组批，跨房间与会话。未满 N 个 run 时等待，最早来源满 24 小时自动触发。批次开始后 run 清单固定，后到的来源进入下一批。

`long_memory_candidates` 保存规则分块及其 Noul、Choice 结果；Noul ≥ 阈值才分类，否则保留拒绝记录而不进入 L2。Choice 保存五类完整概率、整体置信度和响应实际模型版本，以最高概率选唯一主类别，低置信度不额外拒绝。候选保留 run 状态及 think 事件关联。**所有候选分类完成后才允许任何提取调用**。

五类各有独立提示词和结构，定义于 `processing.py` 与 `prompts.py`：

| 类别 | 结构字段 |
| --- | --- |
| user_preference | topic / preference / scope / conditions |
| project_state | project / object / status / progress / blockers / next_steps / observed_at |
| user_fact | subject / attribute / value / scope / valid_time |
| decision | decision / actor / status / rationale / constraints / decided_at |
| reusable_conclusion | problem / experimental_conditions / observations / conclusion / applicability / limitations |

字段使用字符串或 null，未知内容不补全。每条输出必须有 `content`、`structure`、`tags` 和本类别子批中的 `evidence_candidate_ids`。代码校验后映射真实文件和行号，拒绝模型捏造的候选。提取明确区分事实、建议、假设和 think 猜测，允许返回空列表。

按类别汇总，再按完整候选和服务端输入预算划分子批。子批结果通过 LLM 等价判断归并，同义内容合并证据，冲突和不同主体、项目、时间、条件分别保留。所有提取与归并检查点保存于 `long_memory_batches`，重试复用已完成结果。默认每个模型调用最多尝试 3 次；失败批次不自动重试，用户修复配置后显式重试。所有候选被拒绝会正常完成。

提取结果直接进入 L2，不创建派生 MD。新增 `section_kind=extracted`、`batch_id`、`evidence_refs` 和 `evidence_source_ids`；单来源字段兼容指向首条证据。元数据保存结构、模型/提示词版本和每条证据的独立 Noul/Choice 结果，不计算混合置信度。整批完成标记是发布开关，未完成输出不参与有效查询或召回。删除任何证据来源会整条停用，并沿手动更新、聚合关系继续传播；读取时再次检查全部证据状态，防止并发删除或重试恢复已停用内容。

两个应用的 lifespan 在启动时扫描恢复，每 30 秒检查数量与等待时间。MongoDB 用户租约有效期 60 秒、15 秒续约，发布检查租约所有者；同账号不能由多个 worker 同时发布。应用退出取消后台任务并释放租约，下一次启动复用阶段结果。

服务端依赖 `typesafe-sdk==0.7.1`、`pydantic>=2.12,<3`。JEV 使用 [官方 Python SDK](https://docs.typesafe.ai/sdk/python)。配置 `TYPESAFE_API_KEY`，可通过 `MEMORY_JEV_MODEL` 覆盖默认 `jev-latest`。提取默认复用现有 LLM，独立连接可设置 `MEMORY_EXTRACTION_MODEL`、`MEMORY_EXTRACTION_BASE_URL` 和 `MEMORY_EXTRACTION_API_KEY`。`MEMORY_EXTRACTION_INPUT_BUDGET` 默认 12000，按候选 JSON 估算，需为提示词和输出预留模型上下文；单候选超限不截断，而是记录错误等待调整预算。浏览器只看到模型名及凭据是否存在，不做自动连通性请求。

缺少凭据保留 L3 和失败批次，展示配置错误，不回退规则模式。`rules` 模式继续使用原有即时规则索引。切换模式不转换旧数据，也不改变已排队配置。

## 验证

```bash
/Users/zxcvbzzy1/miniconda3/envs/MY_env/bin/python -m pytest agent_flow/tests/test_long_memory.py agent_flow/tests/test_memory_processing.py agent_flow/tests/test_memory_model_adapters.py agent_flow/tests/test_layer_dependencies.py im_backend/tests/test_memory_management.py im_backend/tests/test_memory_processing_api.py -v
cd IM_front
npm run build
```

测试使用临时目录、内存持久层和伪事件/LLM，无需 MongoDB、Redis 或真实模型。不会回填历史 run。

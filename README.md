### 项目架构
    采用应用层-领域层-基础设施层架构





### 工具注入-工具事件调用注册/发送

#### 涉及文件

1. `domain/tool.py`定义工具的LLM决策定义类、返回类型、工具领域，该类用于定义工具的语义信息注入
2. `domain/event.py`定义工具事件工厂类，该类用于自动生成工具事件名，并支持函数链发送事件
3. `infra/eventbus.py`定义事件总线，该类用于注册工具中间件、发布事件等
4. `infra/tool/tool_bind.py`定义工具事件绑定修饰器，用于将事件绑定到工具函数实现中
5. `infra/tool/tools_attach_methods.py`定义工具的具体实现



#### 添加使用工具流程

1. 完成`Tool`类的实现，自动完成LLM的工具语义注入，注入prompt为
    ```
    prompt = "当前可用工具、简介和其参数："
                + tool.name
                + tool.description 
                + tool.input_schema
    ```
    
    以及自动生成相应的用于执行层的EDA事件，事件格式为
    ```
    {prefix}.{field}.{tool_dot_name}.{suffix}

    suffix = 
    ["called", "succeeded", "failed", "retrying"]
    ```

2. 通过事件工厂函数，函数调用直接发送事件
    ```
    动态构建
    factory= 
    ToolEventFactory(prefix="infra")
    ._build()
    
    静态构建
    factory = 
    ToolEventFactory(prefix="infra")
    .export_class("./domain/tools/static_tools.py")

    事件发送    
    factory
    .tool("tool.name")
    .called(tool.input_schema) -> Event

    事件发布
    factory
    .tool("tool.name")
    .called(tool.input_schema) -> Coroutine

    ```

3. 添加工具具体实现，并完成工具实现-事件注册
   
   `infra/tool/tools_attach_methods.py`中
   
   ---
   单事件注册


   ```
    @on_tool.on(factory.tool(tool.name).called())
    def tool_called(**kwargs) -> Event:...
   ```

   将`tool.name`的`called`事件绑定到`tool_called`中
   `tool.input_schema`的参数可以直接从`kwargs`中获取

   ---
   匹配注册


   ```
    @on_tool.on_pattern("*.failed")
    async def on_tool_fail(event,**kwargs):
   ```
   将`.failed`后缀的所有事件绑定到`on_tool_fail`中，事件参数同样`kwargs`中获取（`Tool_respond`类统一解构为`dict`）
   
   ---
    ***单事件注册可以覆盖匹配注册***

#### 事件完成后回调


### Agent默认策略

#### 涉及文件 


#### 工具回调策略


### 上下文工程

#### 涉及文件

1. `domain/context/strategy.py`定义上下文管理策略基类与内置策略。
   
   `ContextStrategy`从记忆中读取原始数据并产出`ContextItem[]`
   
    `ItemStrategy`对已有`ContextItem[]`做变换
    
    `|`运算符串联成`StrategyPipeline`

2. `domain/context/providers.py`定义上下文提供类。
   
   静态Provider（数据来自`state dict`）
   
   动态Provider（数据来自`ShortTermMemory`）
   
   动态Provider获得经`Strategy`处理后数据

3. `domain/context/context.py`定义上下文引擎`ContextEngine`，按Provider注册顺序依次拼接为最终LLM prompt
4. `domain/memory/short/short_term_memory.py`定义`ShortTermMemory`抽象接口与记忆存储字段类型



#### 内置Provider 分类

---
静态Provider（数据来自`state dict`，不依赖记忆）

| Provider | 注入内容 |
|---|---|
| `UserPromptProvider` | 用户原始需求 |
| `StateProvider` | 当前执行状态（重试次数、工具历史、失败提示） |
| `AvailableToolsProvider` | 当前可用工具列表及参数定义 |

---
动态Provider（数据来自`ShortTermMemory`，经Strategy处理后格式化）

| Provider | 记忆字段 | 默认策略 |
|---|---|---|
| `ToolOutputProvider` | `tool_respond` | `FullHistoryStrategy()` |
| `HistoryProvider` | `agent_history` | `FullHistoryStrategy()` |

#### 内置策略

| 策略 | 类型 | 说明 |
|---|---|---|
| `FullHistoryStrategy` | `ContextStrategy` | 透传全部原文 |
| `LatestOnlyStrategy` | `ContextStrategy` | 每个工具只取最新一次输出 |
| `RecencyStrategy(keep_last)` | `ItemStrategy` | 只保留最近N条item |
| `TokenBudgetStrategy(token_limit)` | `ItemStrategy` | 超出token上限时从最旧item开始丢弃 |
| `SummarizeStrategy(threshold, outline_fn)` | `ItemStrategy` | 超长item替换为摘要，无`outline_fn`时降级为截断 |
| `FilterByToolStrategy(tool_names)` | `ItemStrategy` | 只保留指定工具名的item |
| `ChunkToFileStrategy(storage_dir,token_limit)`  | `ItemStrategy` | 超过token限制的item持久化分块存储到磁盘上 |




#### 添加自定义Provider

1. 静态Provider：继承`ContextProvider`，实现`get(state)`
2. 动态Provider：继承`MemoryProvider`，实现`get(state)`，内部通过`self._get_items(state)`获取经Strategy处理后的`ContextItem[]`


#### 添加自定义Strategy

1. 从数据源开始的策略：继承`ContextStrategy`，实现`apply`
2. 中间过程检验的策略：继承`ItemStrategy`，实现`transform`

#### 策略组合

策略通过`|`运算符串联成Pipeline，第一个策略从`ShortTermMemory`读取产出初始`ContextItem[]`，后续`ItemStrategy`依次变换

```
# 透传全部 → 只保留最近10条 → token预算10000
FullHistoryStrategy() | RecencyStrategy(10) | TokenBudgetStrategy(10000)

# 只取最新 → 超长摘要
LatestOnlyStrategy() | SummarizeStrategy(threshold=800, outline_fn=llm_summarize)

...

```

#### 整体流程

1. 定义Provider实例。
   
    ```
    # 静态 — 数据来自 state dict
    user_provider = UserPromptProvider()
  
    # 动态 — 数据来自 ShortTermMemory
    tool_output_provider = ToolOutputProvider(
        memory,
        "tool_respond",
        FullHistoryStrategy() | 
        RecencyStrategy(10) | 
        TokenBudgetStrategy(10000),
    )
    ```

2. 组装Provider列表并实例化`ContextEngine`，Provider注册顺序即prompt注入顺序

    ```
    providers = [
        user_provider,        # 1. 用户需求
        tool_output_provider, # 2. 工具反馈
    ]

    engine = ContextEngine(providers=providers, memory=memory)
    ```

3. 在Agent中使用，`ContextEngine.build(state)`返回拼接好的prompt字符串

    ```
    prompt = engine.build(state)

    prompt 按providers顺序拼接，形如：
    # "请开始处理以下需求：用户需求：...\n\n## 当前执行状态\n...\n\n## 对话历史\n...\n\n## 工具反馈\n...\n\n当前可用工具：..."
    ```


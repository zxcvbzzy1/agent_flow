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


#### 状态总结策略

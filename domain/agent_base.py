

from abc import ABC, abstractmethod
import asyncio
from dataclasses import dataclass, field
import json
from typing import Any, Callable
import re
from domain.event import ToolEventFactory
from domain.state import Agent_state
from domain.tool import Tool


@dataclass
class ToolCall:
    """LLM 决定调用的一个工具。"""
    tool_name: str
    arguments:  dict[str, Any]
    reasoning:  str = ""   # LLM 的思考链，用于调试

@dataclass
class AgentDecision:
    """LLM 单次推理的完整决策。"""
    tool_calls:  list[str] = field(default_factory=list)          # 本轮要调用的工具（可多个）
    think:       str = ""                # <thinking> 内容
    is_finished: bool = False            # 是否认为整个任务完成
    finish_reason: str = ""              # 完成原因


def run_async(coro):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # 已在事件循环中
    return loop.create_task(coro)

# 默认回调_截断策略
def default_callback(respond, s) -> str:
    tool_name = respond["tool_name"]
    raw = respond["respond"]
    # 统一转字符串
    if not isinstance(raw, str):
        raw = json.dumps(raw, ensure_ascii=False)
    call_index = len(s["tool_respond_full"].get(tool_name, []))
    if len(raw) > 700:
        # 待优化
        summary = (
            f"[{tool_name} 第{call_index}次] "
            f"{raw[:700]}... "
            f"(文本过长已截断，可用 query_tool_respond 查询完整内容)"
        )
    else:
        summary = f"[{tool_name} 第{call_index}次] {raw}"
    return summary        


class AgentBase(ABC):
    """
    基于 LLM 的通用 Agent 抽象基类。

    【核心功能】
    - 维护 Agent 运行状态（state）
    - 构建多轮推理 Prompt（角色 / 工具 / 历史 / 状态）
    - 调用 LLM 进行决策（生成 tool_calls 或结束信号）
    - 解析 LLM 输出为结构化决策（AgentDecision）
    - 驱动 “思考 → 工具调用 → 状态更新 → 再思考” 的循环执行

    【核心方法】
    - start(prompt): 初始化任务并启动 Agent
    - run(): Agent 主循环，不断执行 _step() 直到结束
    - _step(): 单步推理（构造 prompt → LLM 推理 → 决策分支）
    - on_tool_call(): 工具执行后的回调，用于更新状态，再执行run()
    - _parse_decision(): 将 LLM 输出解析为结构化决策

    【Prompt 构造】
    - _build_agent_prompt(): Agent 角色与行为定义
    - _build_user_message(): 当前用户任务
    - _build_state_summary(): 当前执行状态总结
    - _build_mutil_user_message(): 多轮对话历史
    - _get_avilable_tools_str(): 可用工具及参数说明

    【子类重写】
    - _build_agent_prompt(): Agent 角色与行为定义
    - _build_user_message(): 当前用户任务
    - _build_state_summary(): 当前执行状态总结
    - _build_mutil_user_message(): 多轮对话历史
    
    - on_tool_call(): 工具执行完成后的回调，用于更新状态
    - _step(): 单步推理，返回是否结束

    【执行流程】
    1. start() 初始化任务
    2. run() 进入循环
    3. 每轮调用 _step():
        - 构造 Prompt
        - 调用 LLM 获取决策
        - 若有 tool_calls → 等待工具执行
        - 工具执行完成后，通过 on_tool_call() 更新状态并进入下一轮
        - 若 is_finished=True → 结束循环
    4. 循环结束后，返回结果
    """
    _instance_list = {}

    def __init__(self,id,name,llm) -> None:
        self.id = id
        self.name = name
        self._llm = llm
        self.states_manage = Agent_state()
        self.states=self.states_manage.get_state()
        # self.avilable_tools = ["search", "memory", "human", "summary","system"]
        self.avilable_tools = ["system", "memory"]
        self.tool_factory:ToolEventFactory = ToolEventFactory(prefix="infra")
        AgentBase._instance_list[self.id] = self

        # ✅ 用于等待工具完成的信号
        self._tool_done = asyncio.Event()
        # ✅ 本轮待完成的工具数量
        self._pending_tools = 0
        # ✅ 缓存最近几条工具回调结果
        self.RECENT_COUNT = 5

        self.work_path = "./temp/"

    @classmethod
    def get_instance_dict(cls) ->dict:
        return cls._instance_list

    async def start(self,prompt):
        s = self.states_manage.get_state()
        s["prompt"] = prompt
        print("🔥 start 设置 prompt:", s["prompt"])
        await self.run()
    
    async def run(self) -> None:
        while True:
            agent_break =await self._step()
            if agent_break:
                break

    # 单步推理

    async def _step(self) -> bool:
        # 1. 构造 prompt，调用 LLM，解析决策
        decision = await self._think()
        
        # 2. 执行工具调用
        if decision.tool_calls:
            await self._execute_tools(decision.tool_calls)
            return False
        
        # 3. 结束判断
        if decision.is_finished:
            self.states["is_finished"] = True
            self.states["finish_reason"] = decision.finish_reason
            return True 
        return False


    async def _think(self) -> AgentDecision:
        """构造 prompt → 调用 LLM → 解析决策"""
        prompt = "\n".join([
            self._get_avilable_tools_str(),
            self._build_state_summary(),
            self._build_mutil_user_message(),
            self._build_user_message(),
        ])
        messages = [
            {"role": "system", "content": self._build_agent_prompt()},
            {"role": "user",   "content": prompt},
        ]
        print(prompt)
        response = await self._llm.chat(messages)
        decision = self._parse_decision(response)
        
        if decision.think:
            self.states["think"] += decision.think
        
        return decision


    async def _execute_tools(self, tool_calls: list[ToolCall]) -> None:
        """执行本轮所有工具调用，处理 $ref 引用，等待全部完成"""
        self._tool_done.clear()
        self._pending_tools = len(tool_calls)
        
        # 本轮结果暂存，供 $ref 引用
        round_results: dict[str, str] = {}
        
        no_dep  = [tc for tc in tool_calls if not self._has_ref(tc)]
        has_dep = [tc for tc in tool_calls if self._has_ref(tc)]
        
        await asyncio.gather(*[self._run_one(tc, round_results) for tc in no_dep])
        for tc in has_dep:
            await self._run_one(tc, round_results)
        
        await self._tool_done.wait()


    async def _run_one(self, tc: ToolCall, round_results: dict[str, str]) -> None:
        """解析单个工具调用的 $ref 参数，执行工具，更新状态"""
        resolved_args = self._resolve_refs(tc.arguments, round_results)
        
        result_event = await self.tool_factory.tool(tc.tool_name).emit_called(
            arguments={**resolved_args, "agent_id": self.id}
        )
        if result_event:
            respond = result_event.payload
            raw = str(respond.respond)
            round_results[tc.tool_name] = raw
          

    def _resolve_refs(self, arguments: dict, round_results: dict[str, str]) -> dict:
        """将参数里的 $ref:tool_name#次数 替换为实际值"""
        resolved = {}
        for k, v in arguments.items():
            if isinstance(v, str) and re.fullmatch(r"\$ref:[^#]+#\d+", v):
                # 解析 tool_name 和 index
                ref_part = v[5:]                          # 去掉 "$ref:"
                tool_name, index_str = ref_part.split("#")
                index = int(index_str)                    # 从1开始

                if tool_name in round_results and index == 0:
                    resolved[k] = round_results[tool_name]
                else:
                    history = self.states.get("tool_respond_full", {}).get(tool_name, [])
                    if history and 1 <= index <= len(history):
                        resolved[k] = history[index - 1]  # index 从1开始转0开始
                    else:
                        resolved[k] = ""
                        print(f"[WARN] $ref:{tool_name}#{index} 未找到，"
                            f"该工具共调用 {len(history)} 次")
            else:
                resolved[k] = v
        return resolved


    def _has_ref(self, tc: ToolCall) -> bool:
        return any(
            isinstance(v, str) and bool(re.fullmatch(r"\$ref:[^#]+#\d+", v))
            for v in tc.arguments.values()
        )
    
    # async def _step(self) -> None:
    #     s = self.states_manage.get_state()
    #     prompts = []
    #     # prompts.append(self._build_agent_prompt())
    #     prompts.append(self._get_avilable_tools_str())
    #     prompts.append(self._build_state_summary())
    #     prompts.append(self._build_mutil_user_message())
    #     prompts.append(self._build_user_message())
    #     prompt = "\n".join(prompts)
        
    #     print(prompt)
    #     messages = [
    #     {"role": "system", "content": self._build_agent_prompt()},
    #     {"role": "user",   "content": prompt},
    #     ]

    #     response = await self._llm.chat(messages)
    #     decision = self._parse_decision(response)
    #     if decision.think:
    #         self.states["think"] += decision.think
        
    #     if decision.tool_calls:
    #         self._tool_done.clear()
    #         self._pending_tools = len(decision.tool_calls)
    #         for tool_call in decision.tool_calls:
    #             run_async(self.tool_factory.tool(tool_call.tool_name).emit_called(arguments={
    #                 **tool_call.arguments,
    #                 "agent_id": self.id,
    #                 }))
                
    #         await self._tool_done.wait()
    #         return False
    #     if decision.is_finished:
    #         self.states["is_finished"] = True
    #         self.states["finish_reason"] = decision.finish_reason
    #         return True
    #     else:
    #         return False

    # prompt构造
    # 单次对话
    def _build_agent_prompt(self) -> str:
        s = self.states
        return f"""
你是一个专业 Agent,当前的工作目录为：{self.work_path}
可以自主读取创建工作目录下的文件

## 你的目标
根据用户需求，自主决定调用组合合适的工具完成任务。
 
 ## 输出格式
用 JSON 严格按以下格式回复：
{{
  "think": "你的思考过程",
  "tool_calls": [
    {{
      "tool_name": "工具名",
      "arguments": {{ 
        "参数名": "参数值"
         }},
      "reasoning": "为什么调用这个工具"
    }}
  ],
  "is_finished": false
}}
 
## 工具结果引用
如果某个工具的参数需要使用先前工具的输出，用 "$ref:工具名#次数" 作为参数值,次数从1开始,
框架会自动将 "$ref:工具名#次数" 替换为 ‘工具名’ 的调用次数实际输出。
如$ref:query_tool_respond#1,表示引用query_tool_respond工具第1次调用的输出。
引用本轮刚执行的工具时,次数填0表示本轮结果如
$ref:query_tool_respond#0"   → 本轮 query_tool_respond 的输出
有引用依赖的工具会在被引用工具完成后自动执行。


如果任务完成，输出：
{{
  "think": "...",
  "tool_calls": [],
  "is_finished": true,
  "finish_reason": "完成原因"
}}
"""        


    def _build_user_message(self) -> str:
        s = self.states
        return f"""
\n
请开始处理以下需求： 
用户需求：{s['prompt']}
根据需求，决定下一步调用哪个工具。"""
    

    def _build_state_summary(self) -> str:
        s = self.states
        parts = ["## 当前执行状态"]
        if s["retry"] > 0:
            parts.append(f"- 已重试：{s['retry']} 次")
        if not s["last_tool_ok"]:
            parts.append("- ⚠️ 上一个工具执行失败，请决定是否重试或换其他工具")       
        if s["tool_history"]:
            parts.append(f"- 已调用工具：\n{' -> '.join(s['tool_history'])}")
        
        responds = s.get("tool_respond", [])
        if responds:
            parts.append(f"- 工具反馈（共 {len(responds)} 条）：")
            for i, item in enumerate(responds):
                tool_name = item["tool_name"]
                respond   = item["respond"]
                is_recent = i >= len(responds) - self.RECENT_COUNT
                if is_recent:
                    parts.append(f"  [{tool_name}]反馈\n{respond}")
                else:
                    # 较早的条目：只展示工具名，不展示内容
                    parts.append(
                        f"  [{tool_name}] (内容已折叠,可调用query_tool_respond 查看)"
                    )        
        # if s["tool_respond"]:
        #     parts.append(f"- 已调用工具的反馈：\n{' \n-> \n'.join(s['tool_respond'])}")
        parts.append("\n请决定下一步调用哪个工具，或输出 is_finished=true。")
        return "\n".join(parts)

    # 多轮会话

    def _build_mutil_user_message(self) -> str:
        s = self.states_manage.get_state()
        return f"""
## 历史记录
历史对话记录：{"\n".join(s['history'])}
"""
        pass

    # 工具调用返回
    async def on_tool_call(self, tool_name: str, success: bool,respond:str,callBack:Callable[[dict,dict],str]=default_callback) -> None:
        """

        agent调用工具回调，用来更新agent状态

        Args:
            tool_name (str): 工具名称
            success (bool): 是否成功
            respond (str): 工具返回结果

            callBack (Callable[[dict,dict],str]): 回调函数
                args:
                    respond (dict): 
                        tool_name(str): 工具名称
                        respond(str): 工具返回结果
                    state (dict): agent状态
                return: str 经过处理后的工具返回结果文本
        """
        s = self.states
        if success:
            try:
                print(f"{tool_name}召回成功，开始处理结果\n\n")
                s["tool_history"].append(tool_name)
                s["last_tool_ok"] = True
                s["retry"] = 0
                # 原文始终完整保存
                full_store = s["tool_respond_full"]
                if tool_name not in full_store:
                    full_store[tool_name] = []
                full_store[tool_name].append(respond)
                # 工具特殊处理
                responds = {
                    "tool_name": tool_name,
                    "respond": respond
                }
                tool_hander = callBack(responds,s)
                responds["respond"] = tool_hander
                s["tool_respond"].append(responds)
            except Exception as e:
                print(f"tool {tool_name} error: {e}")
        else:
            s["last_tool_ok"] = False
            s["retry"] += 1
        self._pending_tools -= 1
        if self._pending_tools <= 0:
            self._tool_done.set()

    def _parse_decision(self, raw: str) -> AgentDecision:
        """
        解析 LLM 返回的 JSON 决策。
        容错：LLM 有时会在 JSON 外包一层 markdown 代码块。
        """
        text = raw.strip()
        # 去掉 ```json ... ``` 包装
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(
                l for l in lines
                if not l.strip().startswith("```")
            )
 
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return AgentDecision(tool_calls=[], think=raw)
 
        tool_calls = [
            ToolCall(
                tool_name=tc["tool_name"],
                arguments=tc.get("arguments", {}),
                reasoning=tc.get("reasoning", ""),
            )
            for tc in data.get("tool_calls", [])
        ]
 
        return AgentDecision(
            tool_calls=    tool_calls,
            think=         data.get("think", ""),
            is_finished=   data.get("is_finished", False),
            finish_reason= data.get("finish_reason", ""),
        )

    def _get_avilable_tools_str(self)->str:
        return "\n当前可用工具、简介和其参数：\n"+"\n".join([
            tool.name+'\n'+tool.description +'\n'+json.dumps(tool.input_schema,ensure_ascii=False) 
            for tool in Tool.get_all_tools() 
            if tool.field in self.avilable_tools
            ])


    

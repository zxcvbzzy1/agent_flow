from abc import ABC, abstractmethod
import asyncio
from dataclasses import dataclass, field
import json
import re
from typing import Any
from domain.context.context import ContextEngine
from domain.event import ToolEventFactory
from domain.state import Agent_state
from domain.tool import Tool


@dataclass
class ToolCall:
    """LLM 决定调用的一个工具。"""
    tool_name: str
    arguments: dict[str, Any]
    reasoning: str = ""


@dataclass
class AgentDecision:
    """LLM 单次推理的完整决策。"""
    tool_calls:    list[str] = field(default_factory=list)
    think:         str = ""
    is_finished:   bool = False
    finish_reason: str = ""
    final:         str = ""


class AgentBase(ABC):
    """
    基于 LLM 的通用 Agent 抽象基类。

    【核心功能】
    - 维护 Agent 运行状态（state）
    - 通过注入的 ContextEngine 构建多轮推理 Prompt
    - 调用 LLM 进行决策（生成 tool_calls 或结束信号）
    - 解析 LLM 输出为结构化决策（AgentDecision）
    - 驱动 "思考 → 工具调用 → 状态更新 → 再思考" 的循环执行

    【核心方法】
    - start(prompt):      初始化任务并启动 Agent
    - run():              Agent 主循环
    - _step():            单步推理
    - on_tool_call():     工具执行后的状态回调
    - _parse_decision():  将 LLM 输出解析为结构化决策

    【子类重写】
    - _build_agent_prompt(): Agent 角色与系统指令（system message）

    【执行流程】
    1. start() 初始化任务
    2. run() 进入循环
    3. 每轮调用 _step():
        - ContextEngine.build() 构造 user message
        - 调用 LLM 获取决策
        - 若有 tool_calls → 执行工具，等待完成
        - 若 is_finished=True → 结束循环
    """

    _instance_list = {}

    def __init__(
        self,
        id:      str,
        name:    str,
        llm,
        context: ContextEngine,
    ) -> None:
        self.id             = id
        self.name           = name
        self._llm           = llm
        self.states_manage  = Agent_state()
        self.states         = self.states_manage.get_state()
        self.tool_factory   = ToolEventFactory(prefix="infra")
        self.work_path      = "/Users/zxcvbzzy1/Desktop/项目/agent_flow/temp"  # 默认工作目录
        self.context_engine = context
        self._tool_done     = asyncio.Event()
        self._pending_tools = 0
        AgentBase._instance_list[self.id] = self

    @classmethod
    def get_instance_dict(cls) -> dict:
        return cls._instance_list

    # ── 生命周期 ──────────────────────────────────────────────────

    async def start(self, prompt: str) -> None:
        self.states["prompt"] = prompt
        self.states["final"] = ""
        self.states["is_finished"] = False
        self.states["finish_reason"] = ""
        await self.run()

    async def run(self) -> None:
        while True:
            if await self._step():
                break

    # ── 单步推理 ──────────────────────────────────────────────────

    async def _step(self) -> bool:
        decision = await self._think()

        if decision.tool_calls:
            await self._execute_tools(decision.tool_calls)
            return False

        if decision.is_finished:
            self.states["is_finished"]   = True
            self.states["finish_reason"] = decision.finish_reason
            self.states["final"]         = decision.final or decision.finish_reason
            return True
        
        

        return False

    async def _think(self) -> AgentDecision:
        """ContextEngine 构造 user message → 调用 LLM → 解析决策"""
        state    = self.states_manage.get_state()
        context  = self.context_engine.build(state)
        messages = [
            {"role": "system", "content": self._build_agent_prompt()},
            {"role": "user",   "content": context},
        ]
        # print(context)
        print(f"[LLM] {self.id} 正在进行推理")
        response = await self._llm.chat(messages)
        decision = self._parse_decision(response)

        if decision.think:
            self.states["think"] += decision.think + "\n"

        return decision

    # ── 工具执行 ──────────────────────────────────────────────────

    async def _execute_tools(self, tool_calls: list[ToolCall]) -> None:
        """执行本轮所有工具，等待全部完成"""
        self._tool_done.clear()
        self._pending_tools = len(tool_calls)
        no_dep  = [tc for tc in tool_calls ]
        await asyncio.gather(*[self._run_one(tc) for tc in no_dep])
        await self._tool_done.wait()

    async def _run_one(self, tc: ToolCall) -> None:
        await self.tool_factory.tool(tc.tool_name).emit_called(
            {**tc.arguments, "agent_id": self.id}
        )

    # ── 工具回调 ──────────────────────────────────────────────────

    async def on_tool_call(
        self,
        tool_name: str,
        success:   bool,
        respond:   str,
    ) -> None:
        s = self.states
        if success:
            try:
                memory = self.context_engine.get_memory()
                memory.store("tool_respond", tool_name, respond)
                s["tool_history"].append(tool_name)
                s["last_tool_ok"] = True
                s["retry"]        = 0
            except Exception as e:
                print(f"[tool:{tool_name}] memory error: {e}")
        else:
            s["last_tool_ok"] = False
            s["retry"]       += 1
            memory = self.context_engine.get_memory()
            memory.store("tool_respond", tool_name, respond)
            s["tool_history"].append(tool_name)


        self._pending_tools -= 1
        if self._pending_tools <= 0:
            self._tool_done.set()

    # ── 系统指令（子类重写）───────────────────────────────────────

    def _build_agent_prompt(self) -> str:
        return f"""
你是一个专业 Agent，当前工作目录为：{self.work_path}
可以自主读取、创建工作目录下的文件。

## 目标
根据用户需求，自主决定调用组合合适的工具完成任务。

## 输出格式
用 JSON 严格按以下格式回复：
{{
  "think": "你的思考过程",
  "tool_calls": [
    {{
      "tool_name": "工具名",
      "arguments": {{"参数名": "参数值"}},
      "reasoning": "为什么调用这个工具"
    }}
  ],
  "is_finished": false
}}

## 任务完成时输出
{{
  "think": "...",
  "tool_calls": [],
  "is_finished": true,
  "finish_reason": "完成原因",
  "final": "最终结果"
}}
"""


    # ── 决策解析 ──────────────────────────────────────────────────

    def _parse_decision(self, raw: str) -> AgentDecision:
        text = raw.strip()

        match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        if match:
            text = match.group(1).strip()
        else:
            pass

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
            tool_calls=tool_calls,
            think=data.get("think", ""),
            is_finished=data.get("is_finished", False),
            finish_reason=data.get("finish_reason", ""),
            final=data.get("final", ""),
        )

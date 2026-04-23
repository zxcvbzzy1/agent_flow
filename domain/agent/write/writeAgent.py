from dataclasses import dataclass, field
import json
from typing import Any

from domain.agent_base import AgentBase
from domain.state import Agent_state
from domain.tool import Tool



class WriteAgent(AgentBase):
    def __init__(self,id,name,llm,memory,context) -> None:
        super().__init__(id,name,llm,memory,context)
        self.states["write_agent"] = {
            "score":    1.0,
            "feedback": "",
            "passed":   True,
        }
        
    # 单步推理
    async def _step(self) -> None:
        return await super()._step()
        
    # prompt构造
    # 单次对话
    def _build_agent_prompt(self) -> str:
        s = self.states
        return f"""
你是一个专业的写作 Agent。

## 你的目标
根据用户需求，自主决定调用合适的工具完成写作任务。
 
## 工作原则
- 根据任务复杂度决定是否需要先生成大纲
- 每次只调用最必要的工具，不要冗余调用
- 如果上一步工具执行失败，优先分析原因再决定是否重试
- 当文本质量已满足需求（score >= 0.8 或 passed=True）时，结束输出
- 最多重试 {3} 次，超过后输出finish并说明原因
 
 
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
 
如果任务完成，输出：
{{
  "think": "...",
  "tool_calls": [],
  "is_finished": true,
  "finish_reason": "完成原因"
}}
"""        





    


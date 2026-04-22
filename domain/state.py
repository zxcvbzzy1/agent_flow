from typing import Any
from enum import Enum, auto

class Agent_state():

    def __init__(self, genre: str = "通用", session_id: str = "") -> None:
        self._data: dict[str, Any] = {
            # 内容
            "prompt":         "",
            "genre":         genre,
            "final":"",
            "think":"",
            "history":       [],
            # tool memory
            "tool_history":  [],   # list[str] 执行过的工具名
            "tool_respond":  [],   # list[str] 执行过的工具的反馈摘要
            "tool_respond_full": {},     # 原文，key=tool_name，value=list（支持多次调用）
            "last_tool_ok": True,
            "tool_retry":     0,   
            # 控制
            "current_state":"",
            "session_id":    session_id,
            "retry":         0,
            "is_finished":   False,
            
        }
        self._version:int = 0

    def get_state(self):
        return self._data


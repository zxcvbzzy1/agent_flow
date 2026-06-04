"""infra.skill：技能子系统的传输/进程适配层。

目前只有 mcp_server —— 一个零依赖的 stdio MCP server，把 domain.skill 的检索能力
暴露成 coding agent（Claude Code / Codex）可在其工具循环内同步调用的 recall_skill 工具。
"""

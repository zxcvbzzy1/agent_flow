"""recall_skill 工具：智能体主动召回技能（两层召回中的“工具召回”）。

与系统检索召回共用 runtime_hooks 暴露的检索器（默认简单向量匹配，可换 RAG）。
命中的技能会：1) 作为工具返回直接给本轮模型看到；2) 并入 agent.states["skills"]，
经 SkillProvider 在后续轮次持续注入上下文。
"""

from __future__ import annotations

from domain.agent_base import AgentBase
from domain.event import Event
from domain.runtime_hooks import get_skill_retriever
from domain.tool import Tool, Tool_respond
from infra.config import bus, factory
from infra.event_bind import On_bind


RECALL_SKILL = Tool(
    name="recall_skill",
    field="system",
    description=(
        "从技能库（可检索的记忆）召回与查询最相关的技能/方法，返回其步骤与要点，"
        "并注入后续上下文。当你不确定某类任务的最佳做法、或想复用既有经验时调用。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "用于检索技能的自然语言查询（任务或问题的描述）",
            },
            "k": {"type": "integer", "description": "返回条数，默认 5"},
        },
        "required": ["query"],
    },
)


on_tool = On_bind()
factory._build_and_register_list([RECALL_SKILL], bus)


def _format_hits(hits) -> str:
    if not hits:
        return "未召回到相关技能。"
    lines = [f"召回 {len(hits)} 条技能（按相关度排序）："]
    for hit in hits:
        skill = hit.skill
        score = float(getattr(hit, "score", 0.0))
        block = f"\n### {skill.name}（相关度 {score:.3f}）"
        if skill.description:
            block += f"\n{skill.description}"
        if skill.content:
            block += f"\n{skill.content}"
        lines.append(block.rstrip())
    return "\n".join(lines)


@on_tool.on(factory.tool("recall_skill").called())
def recall_skill(**kwargs) -> Event:
    agent_id = kwargs.get("agent_id", "")
    name = "recall_skill"
    query = str(kwargs.get("query", "")).strip()
    try:
        k = int(kwargs.get("k") or 5)
    except (TypeError, ValueError):
        k = 5

    if not query:
        return factory.tool(name).failed(
            Tool_respond(agent_id=agent_id, name=name, success=False, respond="query 不能为空")
        )

    retriever = get_skill_retriever()
    if retriever is None:
        return factory.tool(name).succeeded(
            Tool_respond(agent_id=agent_id, name=name, success=True, respond="技能库未启用。")
        )

    try:
        hits = retriever.retrieve(query, k=k, threshold=0.0)
    except Exception as exc:  # noqa: BLE001
        return factory.tool(name).failed(
            Tool_respond(agent_id=agent_id, name=name, success=False, respond=f"技能召回失败: {exc}")
        )

    # 注入到 agent 状态，供 SkillProvider 在后续轮次持续注入
    agent = AgentBase.get_instance_dict().get(agent_id)
    if agent is not None and hasattr(agent, "merge_recalled_skills"):
        try:
            agent.merge_recalled_skills(hits, source="tool")
        except Exception as e:  # noqa: BLE001
            print(f"[skill] 工具召回注入失败: {e}")

    return factory.tool(name).succeeded(
        Tool_respond(agent_id=agent_id, name=name, success=True, respond=_format_hits(hits))
    )

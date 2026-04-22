from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
import json
from typing import Any, Callable, Optional
from domain.tool import Tool
from pyvis.network import Network

# ──────────────────────────────────────────
# 阈值常量（集中管理）
# ──────────────────────────────────────────

# ---------------------------------------------------------------------------
# 枚举：节点类型 / 边类型 / 节点状态
# ---------------------------------------------------------------------------

class NodeType(str, Enum):
    START       = "start"        # 流程入口，唯一
    END         = "end"          # 流程出口，可多个
    ACTION      = "action"       # 普通业务动作
    DECISION    = "decision"     # 条件判断节点（本身不执行业务，只路由）
    PARALLEL    = "parallel"     # 并行分叉点
    JOIN        = "join"         # 并行汇合点
    AGENT  = "agent"   # 代理节点
    # 智能体构造
    CHECKPOINT  = "checkpoint"   # 需要人工/外部确认的节点
    TOOL  = "tool"   # 工具节点
    


class EdgeType(str, Enum):
    DEFAULT     = "default"      # 无条件流转
    CONDITIONAL = "conditional"  # 带条件的流转
    FALLBACK    = "fallback"     # 失败/降级路径
    LOOP_BACK   = "loop_back"    # 循环回退（如评判不通过重写）
    PARALLEL    = "parallel"     # 并行分叉


class NodeStatus(str, Enum):
    PENDING     = "pending"
    AVAILABLE   = "available"
    RUNNING     = "running"
    DONE        = "done"
    SKIPPED     = "skipped"
    FAILED      = "failed"



# ---------------------------------------------------------------------------
# 节点（Node）
# ---------------------------------------------------------------------------

@dataclass
class Node:
    """
    业务节点。

    Attributes:
        node_id:      唯一标识
        name:         可读名称
        node_type:    节点类型（见 NodeType）
        description:  业务语义描述，注入 Agent system prompt
        tools:        该节点可调用的工具列表
        optional:     是否可被 Agent 跳过
        max_retries:  最大重试次数（失败后）
        timeout_sec:  执行超时秒数，None 表示不限
        tags:         标签，用于分组/过滤（如 "writing", "review"）
        metadata:     扩展属性
        status:       运行时状态（不持久化进 Neo4j 属性）
    """
    node_id: str
    name: str
    node_type: NodeType = NodeType.ACTION
    description: str = ""
    tools: list[Tool] = field(default_factory=list)
    optional: bool = False
    max_retries: int = 1
    timeout_sec: int | None = None
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    # 运行时字段，不导出到 Neo4j
    status: NodeStatus = field(default=NodeStatus.PENDING, repr=False)

    def add_tool(self, tool: Tool) -> Node:
        self.tools.append(tool)
        return self

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "name": self.name,
            "node_type": self.node_type.value,
            "description": self.description,
            "tools": [t.to_dict() for t in self.tools],
            "optional": self.optional,
            "max_retries": self.max_retries,
            "timeout_sec": self.timeout_sec,
            "tags": self.tags,
            "metadata": self.metadata,
        }

    def to_cypher(self) -> str:
        """生成 Neo4j MERGE 语句（节点部分）。"""
        props = {
            "node_id": self.node_id,
            "name": self.name,
            "node_type": self.node_type.value,
            "description": self.description,
            "optional": self.optional,
            "max_retries": self.max_retries,
            "timeout_sec": self.timeout_sec if self.timeout_sec is not None else -1,
            "tags": json.dumps(self.tags, ensure_ascii=False),
            "tools": json.dumps([t.to_dict() for t in self.tools], ensure_ascii=False),
            **{f"meta_{k}": v for k, v in self.metadata.items()},
        }
        props_str = _cypher_props(props)
        label = self.node_type.value.capitalize()
        return f"MERGE (n:Node:{label} {{{props_str}}});"

# ---------------------------------------------------------------------------
# 边（Edge）
# ---------------------------------------------------------------------------

@dataclass
class Edge:
    """
    有向边，描述两个节点之间的可达关系。

    Attributes:
        source_id:    起始节点 ID
        target_id:    目标节点 ID
        edge_type:    边的类型（见 EdgeType）
        condition:    可达条件，自然语言描述（注入 Agent prompt 使用）
        condition_fn: 运行时条件函数，接收 state dict，返回 bool
                      None 表示无条件可达
        priority:     同源多出边时的优先级，数字越小越优先
        label:        可读标签，用于可视化和调试
        metadata:     扩展属性（SLA、权重、版本等）
    """
    source_id: str
    target_id: str
    edge_type: EdgeType = EdgeType.DEFAULT
    condition: str = ""
    condition_fn: Callable[[dict], bool] | None = None
    priority: int = 0
    label: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def edge_id(self) -> str:
        return f"{self.source_id}__{self.target_id}"

    def is_reachable(self, state: dict) -> bool:
        """运行时判断该边是否可达。"""
        if self.condition_fn is None:
            return True
        return self.condition_fn(state)

    def to_dict(self) -> dict:
        return {
            "edge_id": self.edge_id,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "edge_type": self.edge_type.value,
            "condition": self.condition,
            "priority": self.priority,
            "label": self.label,
            "metadata": self.metadata,
        }

    def to_cypher(self) -> str:
        """生成 Neo4j MERGE 语句（关系部分）。"""
        props = {
            "edge_id": self.edge_id,
            "edge_type": self.edge_type.value,
            "condition": self.condition,
            "priority": self.priority,
            "label": self.label,
            **{f"meta_{k}": v for k, v in self.metadata.items()},
        }
        props_str = _cypher_props(props)
        return (
            f"MATCH (src:Node {{node_id: '{self.source_id}'}})\n"
            f"MATCH (tgt:Node {{node_id: '{self.target_id}'}})\n"
            f"MERGE (src)-[r:EDGE {{{props_str}}}]->(tgt);"
        )

# ---------------------------------------------------------------------------
# 图（BusinessGraph）
# ---------------------------------------------------------------------------

class BusinessGraph:
    """
    业务流程图。

    - 节点用 node_id 索引。
    - 边用 (source_id, target_id) 索引，允许同源多出边（不同条件）。
    - 提供运行时查询：可达节点、可用节点、拓扑排序。
    - 提供导出：JSON / Cypher。
    """

    def __init__(self, graph_id: str, name: str, description: str = ""):
        self.graph_id = graph_id
        self.name = name
        self.description = description
        self._nodes: dict[str, Node] = {}
        self._edges: dict[str, Edge] = {}          # key: edge_id
        self._out_edges: dict[str, list[str]] = {} # node_id -> [edge_id]
        self._in_edges: dict[str, list[str]] = {}  # node_id -> [edge_id]

    # ------------------------------------------------------------------
    # 构造 API
    # ------------------------------------------------------------------

    def add_node(self, node: Node) -> BusinessGraph:
        if node.node_id in self._nodes:
            raise ValueError(f"节点 '{node.node_id}' 已存在")
        self._nodes[node.node_id] = node
        self._out_edges.setdefault(node.node_id, [])
        self._in_edges.setdefault(node.node_id, [])
        return self

    def add_edge(self, edge: Edge) -> BusinessGraph:
        if edge.source_id not in self._nodes:
            raise ValueError(f"源节点 '{edge.source_id}' 不存在")
        if edge.target_id not in self._nodes:
            raise ValueError(f"目标节点 '{edge.target_id}' 不存在")
        if edge.edge_id in self._edges:
            raise ValueError(f"边 '{edge.edge_id}' 已存在，同向重复边请用不同 label 区分")
        self._edges[edge.edge_id] = edge
        self._out_edges[edge.source_id].append(edge.edge_id)
        self._in_edges[edge.target_id].append(edge.edge_id)
        return self

    def connect(
        self,
        source_id: str,
        target_id: str,
        *,
        edge_type: EdgeType = EdgeType.DEFAULT,
        condition: str = "",
        condition_fn: Callable[[dict], bool] | None = None,
        priority: int = 0,
        label: str = "",
        metadata: dict | None = None,
    ) -> BusinessGraph:
        """快捷方法：直接用 ID 连边，省去手动构造 Edge 对象。"""
        edge = Edge(
            source_id=source_id,
            target_id=target_id,
            edge_type=edge_type,
            condition=condition,
            condition_fn=condition_fn,
            priority=priority,
            label=label or f"{source_id} -> {target_id}",
            metadata=metadata or {},
        )
        return self.add_edge(edge)

    # ------------------------------------------------------------------
    # 运行时查询
    # ------------------------------------------------------------------

    def node(self, node_id: str) -> Node:
        if node_id not in self._nodes:
            raise KeyError(f"节点 '{node_id}' 不存在")
        return self._nodes[node_id]

    def reachable_edges(self, node_id: str, state: dict) -> list[Edge]:
        """返回从指定节点出发、当前 state 下可达的边，按 priority 排序。"""
        edges = [self._edges[eid] for eid in self._out_edges.get(node_id, [])]
        reachable = [e for e in edges if e.is_reachable(state)]
        return sorted(reachable, key=lambda e: e.priority)

    def available_nodes(self, state: dict) -> list[Node]:
        """
        返回当前所有"可执行"节点：
        - 状态为 PENDING
        - 所有入边的源节点已 DONE 或 SKIPPED（或本节点为 START）
        """
        result = []
        for node in self._nodes.values():
            if node.status != NodeStatus.PENDING:
                continue
            in_edge_ids = self._in_edges.get(node.node_id, [])
            if not in_edge_ids:
                # 无入边 = 入口节点，直接可用
                result.append(node)
                continue
            # 所有入边的源节点均已完成
            all_done = all(
                self._nodes[self._edges[eid].source_id].status
                in (NodeStatus.DONE, NodeStatus.SKIPPED)
                for eid in in_edge_ids
                if self._edges[eid].is_reachable(state)
            )
            if all_done:
                result.append(node)
        return result

    def topological_order(self) -> list[str]:
        """Kahn 算法拓扑排序，返回节点 ID 列表（含环检测）。"""
        in_degree = {nid: len(eids) for nid, eids in self._in_edges.items()}
        queue = [nid for nid, deg in in_degree.items() if deg == 0]
        order = []
        while queue:
            nid = queue.pop(0)
            order.append(nid)
            for eid in self._out_edges.get(nid, []):
                target = self._edges[eid].target_id
                in_degree[target] -= 1
                if in_degree[target] == 0:
                    queue.append(target)
        if len(order) != len(self._nodes):
            raise RuntimeError("图中存在环，无法拓扑排序（loop_back 边请在 metadata 中标注跳过排序）")
        return order

    def list_target_nodes(self, current_node_id: str,state:dict) -> list[str]:
        """返回指定节点的直接目标节点 ID 列表。"""
        out_edges = self._out_edges.get(current_node_id, [])
        nodes_id = [self._edges[eid].target_id for eid in out_edges if self._edges[eid].is_reachable(state)]
        return [self._nodes[item] for item in nodes_id]
    def build_agent_prompt(self, state: dict, current_node_id: str | None = None) -> str:
        """
        根据当前状态动态生成注入 Agent system prompt 的业务描述段落。
        只暴露当前可用节点及其可达条件。
        """
        # available = self.available_nodes(state)
        available = self.list_target_nodes(current_node_id,state)
        lines = [f"== 业务图：{self.name} ==", self.description, ""]
        lines.append("当前可执行的业务动作：")
        for node in available:
            skip_hint = "（可跳过）" if node.optional else ""
            lines.append(f"\n[{node.node_id}] {node.name} {skip_hint}")
            lines.append(f"  描述：{node.description}")
            if node.tools:
                tool_names = ", ".join([ 
                    "\n"+
                    t.name +
                    "\n工具描述：\n"+ 
                    t.self.description   
                    for t in node.tools])
                lines.append(f"  可用工具：{tool_names}")
            # 列出从本节点出发的边条件（帮 Agent 理解后续路径）
            out_edges = [self._edges[eid] for eid in self._out_edges.get(node.node_id, [])]
            if out_edges:
                lines.append("  后续路径：")
                for e in sorted(out_edges, key=lambda x: x.priority):
                    cond = f"（条件：{e.condition}）" if e.condition else ""
                    lines.append(f"    -> {e.target_id} {cond}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "graph_id": self.graph_id,
            "name": self.name,
            "description": self.description,
            "nodes": [n.to_dict() for n in self._nodes.values()],
            "edges": [e.to_dict() for e in self._edges.values()],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def to_cypher(self) -> str:
        """
        生成完整的 Neo4j Cypher 导入脚本。
        可直接粘贴到 Neo4j Browser 或通过 neo4j-driver 执行。
        """
        lines = [
            "// ============================================================",
            f"// 业务图：{self.name}  (graph_id: {self.graph_id})",
            "// ============================================================",
            "",
            "// 1. 图元数据节点",
            f"MERGE (g:Graph {{graph_id: '{self.graph_id}', name: '{self.name}', "
            f"description: '{self.description}'}});",
            "",
            "// 2. 节点",
        ]
        for node in self._nodes.values():
            lines.append(node.to_cypher())
        lines += ["", "// 3. 边"]
        for edge in self._edges.values():
            lines.append(edge.to_cypher())
        lines += [
            "",
            "// 4. 节点归属图",
            f"MATCH (g:Graph {{graph_id: '{self.graph_id}'}})",
            "MATCH (n:Node)",
            "MERGE (g)-[:CONTAINS]->(n);",
        ]
        return "\n".join(lines)

    def summary(self) -> str:
        node_counts = {}
        for n in self._nodes.values():
            node_counts[n.node_type.value] = node_counts.get(n.node_type.value, 0) + 1
        edge_counts = {}
        for e in self._edges.values():
            edge_counts[e.edge_type.value] = edge_counts.get(e.edge_type.value, 0) + 1
        lines = [
            f"图: {self.name} ({self.graph_id})",
            f"节点: {len(self._nodes)} 个  {node_counts}",
            f"边:   {len(self._edges)} 条  {edge_counts}",
        ]
        return "\n".join(lines)

def _cypher_props(props: dict) -> str:
    """将 Python dict 序列化为 Cypher 属性字符串。"""
    parts = []
    for k, v in props.items():
        if isinstance(v, bool):
            parts.append(f"{k}: {str(v).lower()}")
        elif isinstance(v, (int, float)):
            parts.append(f"{k}: {v}")
        else:
            escaped = str(v).replace("'", "\\'").replace("\n", "\\n")
            parts.append(f"{k}: '{escaped}'")
    return ", ".join(parts)

class BuilderGraph():
    def __init__(self, graph_id: str, name: str, description: str) -> None:
        self._graph = BusinessGraph(graph_id, name, description)
    
    
    def get_graph(self) -> BusinessGraph:
        return self._graph
    

    def build_content_creation_graph(self) -> BusinessGraph:
        """
        内容创作完整流程图，包含：
        - writer agent → critic agent 
        - 评判通过 → 人工审核 → 发布
        - 评判不通过 → 重写（最多循环）→ 再评判
        - 任意阶段可触发紧急中止
        """
        g = self._graph
        
        # ---- 节点定义 ----
        
        g.add_node(Node(
            node_id="start",
            name="流程入口",
            node_type=NodeType.START,
            description="接收用户的创作需求，解析主题、受众、字数、风格等约束",
            tags=["entry"],
            metadata={"version": "1.0"},
        ))

    
        g.add_node(Node(
            node_id="writer_agent",
            name="自动续写",
            node_type=NodeType.AGENT,
            description="自动续写功能，根据主题和用户要求，生成文本",
            tools=[],
            optional=True,
            max_retries=1,
            tags=["writing", "draft"]
        ))

        g.add_node(Node(
            node_id="auto_judge",
            name="自动评判",
            node_type=NodeType.AGENT,
            description="多维度自动评估稿件质量：逻辑性、事实准确性、语言质量、字数合规、主题切合度",
            tools=[],
            optional=True,
            max_retries=1,
            tags=["evaluation", "auto"],
            metadata={"score_threshold": 75, "dimensions": ["logic", "accuracy", "language", "length", "relevance"]},
        ))

        g.add_node(Node(
            node_id="judge_decision",
            name="评判结果路由",
            node_type=NodeType.DECISION,
            description="根据自动评判分数决定后续路径：高分进入人工审核，低分触发重写",
            optional=True,
            tags=["decision", "routing"],
        ))

        g.add_node(Node(
            node_id="human_review",
            name="人工审核",
            node_type=NodeType.CHECKPOINT,
            description="发送给人工审核员确认，审核内容包括：合规性、品牌一致性、事实核查",
            tools=[],
            optional=False,
            timeout_sec=86400,
            tags=["human_in_loop", "review"],
            metadata={"reviewer_role": "editor", "sla_hours": 24},
        ))

        g.add_node(Node(
            node_id="human_decision",
            name="人工审核结果路由",
            node_type=NodeType.DECISION,
            description="根据人工审核结果路由：通过则发布，打回则按意见修改",
            optional=True,
            tags=["decision", "routing"],
        ))

        g.add_node(Node(
            node_id="human_revision",
            name="按人工意见修改",
            node_type=NodeType.ACTION,
            description="严格按照审核员的具体意见进行修改，完成后重新提交审核",
            tools=[Tool.get_all_tools()[0]] if len(Tool.get_all_tools()) > 0 else [],
            optional=False,
            max_retries=3,
            tags=["writing", "revision", "human_in_loop"],
        ))

        g.add_node(Node(
            node_id="publish",
            name="发布",
            node_type=NodeType.ACTION,
            description="将最终稿件发布到目标平台，记录发布日志",
            tools=[],
            optional=False,
            max_retries=3,
            timeout_sec=60,
            tags=["output", "publish"],
            metadata={"targets": ["cms", "newsletter", "social"]},
        ))

        g.add_node(Node(
            node_id="abort",
            name="紧急中止",
            node_type=NodeType.END,
            description="出现不可恢复错误或违规内容时终止流程，记录原因",
            tags=["end", "error"],
            metadata={"notify_slack": True},
        ))

        g.add_node(Node(
            node_id="end",
            name="流程结束",
            node_type=NodeType.END,
            description="内容成功发布，流程正常结束",
            tags=["end", "success"],
        ))

        # ---- 边定义 ----

        g.connect("start", "writer_agent",
                  edge_type=EdgeType.DEFAULT,
                  label="调用agent 生成内容")


        # 自动评判
        g.connect("writer_agent", "human_review",
                  edge_type=EdgeType.CONDITIONAL,
                  condition="任务不需要自动评判时，直接跳过",
                  condition_fn=lambda s: s.get("passed", False),
                  priority=0,
                  label="无需评判")

        g.connect("writer_agent", "auto_judge",
                  label="初稿完成，进入自动评判")

        g.connect("auto_judge", "judge_decision",
                  label="评判结果出炉")


        g.connect("judge_decision", "human_review",
                  edge_type=EdgeType.CONDITIONAL,
                  condition="评判总分 >= 75 且不需要润色，直接进入人工审核",
                  condition_fn=lambda s: s.get("judge_score", 0) >= 75 and not s.get("enable_polish", True),
                  priority=1,
                  label="高分 -> 人工审核")

        # 重写
        g.connect("judge_decision", "writer_agent",
                  edge_type=EdgeType.LOOP_BACK,
                  condition="评判总分 < 75 且重写次数未超上限（max 2）",
                  condition_fn=lambda s: s.get("judge_score", 0) < 75 and s.get("retry", 0) < 2,
                  priority=2,
                  label="低分 -> 重写")

        g.connect("judge_decision", "abort",
                  edge_type=EdgeType.FALLBACK,
                  condition="评判总分 < 75 且重写次数已达上限，触发中止",
                  condition_fn=lambda s: s.get("judge_score", 0) < 75 and s.get("retry", 0) >= 2,
                  priority=3,
                  label="重写耗尽 -> 中止")

        
        # 人工审核

        g.connect("human_review", "human_decision",
                  label="人工审核完成")

        g.connect("human_decision", "publish",
                  edge_type=EdgeType.CONDITIONAL,
                  condition="人工审核通过",
                  condition_fn=lambda s: s.get("human_approved", False),
                  priority=0,
                  label="审核通过 -> 发布")

        g.connect("human_decision", "human_revision",
                  edge_type=EdgeType.LOOP_BACK,
                  condition="人工审核打回，需按意见修改",
                  priority=1,
                  label="打回 -> 修改")
        
        g.connect("human_revision", "writer_agent",
                  edge_type=EdgeType.LOOP_BACK,
                  condition="修改完成，重新续写",
                  priority=1,
                  label="修改 -> 续写")

        g.connect("publish", "end",
                  label="发布成功，流程结束")

        return g
    
    def build_critic_ai_graph(self) -> BusinessGraph:
        g = self._graph

        g.add_node(Node(
            node_id="start",
            name="开始",
            node_type=NodeType.START,
            description="流程开始，等待用户输入需求",
            tags=["entry"],
        ))

        g.add_node(Node(
            node_id="summary_user",
            name="用户需求拆分",
            node_type=NodeType.ACTION,
            description="对用户输入的需求进行拆分",
            optional=True,
            tags=["requirements_analysis"],
        ))

        g.add_node(Node(
            node_id="summary_writer",
            name="总结writer模型输出",
            node_type=NodeType.ACTION,
            description="对writer agent输出的前文进行总结",
            optional=True,
            tags=["requirements_analysis"],
        ))

        g.add_node(Node(
            node_id="merge_summary",
            name="总结汇总",
            node_type=NodeType.ACTION,
            description="将writer agent输出和用户需求汇总",
            tags=["requirements_analysis"],
        ))

        g.add_node(Node(
            node_id="critic_process",
            name="评价",
            node_type=NodeType.ACTION,
            description="使用critic agent对writer agent的返回内容是否符合用户需求进行评价[0-100]",
            tags=["requirements_analysis"],
        ))

        g.add_node(Node(
            node_id="end",
            name="结束",
            node_type=NodeType.END,
            description="流程结束，返回结果",
            tags=["exit"],
        ))

        g.connect("start", "summary_user",
                  edge_type=EdgeType.PARALLEL,
                  condition="用户输入需求，需要拆分",
                  priority=0,
                  label="开始 -> 用户需求拆分")
        
        g.connect("start", "summary_writer",
                  edge_type=EdgeType.PARALLEL,
                  condition="模型输出的文本，需要总结",
                  priority=1,
                  label="开始 -> 总结writer模型输出")
        
        g.connect("start", "merge_summary",
                  edge_type=EdgeType.CONDITIONAL,
                  condition="模型输出的文本和用户需求不多时直接汇总",
                  label="开始 -> 汇总")
        
        g.connect("summary_writer", "merge_summary",
                  edge_type=EdgeType.DEFAULT,
                  label="总结writer模型输出 -> 汇总")
        
        g.connect("summary_user", "merge_summary",
                  edge_type=EdgeType.DEFAULT,
                  label="用户需求拆分 -> 汇总")

        g.connect("merge_summary", "critic_process",
                  edge_type=EdgeType.DEFAULT,
                  label="汇总 -> 评价")

        g.connect("critic_process", "end",
                  edge_type=EdgeType.DEFAULT,
                  label="评价 -> 结束")
        
        return g


    @classmethod
    def visualize_pyvis(cls, graph:BusinessGraph,output_path: str = "./graph_visualization.html", 
                        notebook_mode: bool = False, show_details: bool = True) -> str:
        """
        使用 pyvis 生成交互式网络图
        
        Args:
            output_path: 输出 HTML 文件路径
            notebook_mode: 是否在 Jupyter Notebook 中显示
            show_details: 是否显示详细信息（工具、重试次数等）
            
        Returns:
            生成的 HTML 文件路径
        """
        
        # 创建网络图
        net = Network(
            height='800px',
            width='100%',
            bgcolor='#ffffff',
            font_color='#333333',
            directed=True,  # 有向图
            notebook=notebook_mode
        )
        
        # 配置物理引擎
        net.set_options("""
        var options = {
            "nodes": {
                "font": {
                    "size": 14,
                    "face": "Microsoft YaHei"
                },
                "borderWidth": 2,
                "shadow": true
            },
            "edges": {
                "font": {
                    "size": 10,
                    "face": "Microsoft YaHei"
                },
                "smooth": {
                    "type": "curvedCW",
                    "roundness": 0.2
                },
                "shadow": true
            },
            "physics": {
                "enabled": true,
                "stabilization": {
                    "iterations": 100
                },
                "barnesHut": {
                    "gravitationalConstant": -3000,
                    "centralGravity": 0.3,
                    "springLength": 150,
                    "springConstant": 0.05,
                    "damping": 0.09
                }
            },
            "interaction": {
                "hover": true,
                "tooltipDelay": 200,
                "hideEdgesOnDrag": false
            }
        }
        """)
        
        # 节点颜色映射
        color_map = {
            NodeType.START: {'background': '#90EE90', 'border': '#228B22'},
            NodeType.END: {'background': '#FFB6C1', 'border': '#DC143C'},
            NodeType.ACTION: {'background': '#87CEEB', 'border': '#4682B4'},
            NodeType.DECISION: {'background': '#FFD700', 'border': '#DAA520'},
            NodeType.PARALLEL: {'background': '#DDA0DD', 'border': '#9370DB'},
            NodeType.JOIN: {'background': '#DDA0DD', 'border': '#9370DB'},
            NodeType.CHECKPOINT: {'background': '#FFA07A', 'border': '#FF6347'},
            NodeType.AGENT: {'background': "#BB0101", 'border': "#5B0202"},
        }
        
        # 节点形状映射
        shape_map = {
            NodeType.START: 'ellipse',
            NodeType.END: 'dot',
            NodeType.ACTION: 'box',
            NodeType.DECISION: 'diamond',
            NodeType.PARALLEL: 'parallelogram',
            NodeType.JOIN: 'triangleDown',
            NodeType.CHECKPOINT: 'hexagon',
            NodeType.AGENT: 'square'
        }
        
        # 添加节点
        for node_id, node in graph._nodes.items():
            colors = color_map.get(node.node_type, {'background': '#FFFFFF', 'border': '#000000'})
            shape = shape_map.get(node.node_type, 'box')
            
            # 构建标题和详细信息
            title_parts = [f"<b>{node.name}</b>", f"类型: {node.node_type.value}"]
            
            if node.description:
                title_parts.append(f"描述: {node.description}")
            
            if show_details:
                if node.tools:
                    tool_names = [t.name for t in node.tools]
                    title_parts.append(f"工具: {', '.join(tool_names)}")
                
                if node.optional:
                    title_parts.append("⚠️ 可选节点")
                
                if node.max_retries > 1:
                    title_parts.append(f"🔄 最大重试: {node.max_retries}")
                
                if node.timeout_sec:
                    title_parts.append(f"⏱️ 超时: {node.timeout_sec}秒")
                
                if node.tags:
                    title_parts.append(f"标签: {', '.join(node.tags)}")
            
            title = '<br>'.join(title_parts)
            
            # 确定节点大小
            size = 25
            if node.node_type in [NodeType.START, NodeType.END]:
                size = 35
            elif node.node_type == NodeType.DECISION:
                size = 30
            
            net.add_node(
                node_id,
                label=node.name,
                title=title,
                shape=shape,
                color=colors['background'],
                borderColor=colors['border'],
                size=size,
                font={'size': 14, 'color': '#000000'}
            )
        
        # 添加边
        for edge_id, edge in graph._edges.items():
            # 边的颜色和样式
            color = '#000000'
            dashes = False
            width = 1
            arrows = 'to'
            
            if edge.edge_type == EdgeType.CONDITIONAL:
                color = '#4169E1'  # 蓝色
                dashes = [5, 5]
                width = 2
            elif edge.edge_type == EdgeType.FALLBACK:
                color = '#DC143C'  # 红色
                dashes = False
                width = 3
                arrows = 'to, middle'
            elif edge.edge_type == EdgeType.LOOP_BACK:
                color = '#FF8C00'  # 橙色
                dashes = [2, 2]
                width = 2
            elif edge.edge_type == EdgeType.PARALLEL:
                color = '#9932CC'  # 紫色
                dashes = [10, 5]
                width = 2
            
            # 边的标签
            label = edge.label
            if show_details and edge.condition:
                label += f'\n条件: {edge.condition[:60]}'
            
            net.add_edge(
                edge.source_id,
                edge.target_id,
                label=label,
                color=color,
                width=width,
                dashes=dashes,
                arrows=arrows,
                font={'size': 10, 'color': color, 'align': 'middle'}
            )
        
        # 保存文件
        net.save_graph(output_path)
        print(f"✅ 交互式流程图已保存到: {output_path}")
        print(f"💡 在浏览器中打开该文件即可查看和交互")
        
        # 如果在 notebook 模式中，返回 HTML 对象
        if notebook_mode:
            from IPython.display import HTML
            return HTML(filename=output_path)
        
        return output_path
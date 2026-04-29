"""
测试 update_plan 工具：验证计划步骤更新逻辑

由于 tools_attach_methods.py 模块级装饰器依赖完整 Tool 注册，
这里直接测试 Plan.update_step 和 handler 的核心逻辑（提取为独立函数）。
"""

import json
import pytest

from domain.state import Plan, PlanStep, _dict_to_plan
from domain.tool import Tool, Tool_respond


# ── 复制 handler 核心逻辑，避免模块导入问题 ────────────────────────

def _update_plan_core(agent_states: dict, step_id: str, title: str = "",
                      detail: str = "", status: str = "", note: str = "") -> dict:
    """
    从 tools_attach_methods.update_plan 提取的核心逻辑，
    返回 {"success": bool, "respond": ...} 而非 Event。
    """
    plan_dict = agent_states.get("plan", {})

    if not plan_dict:
        return {"success": False, "respond": "当前没有进行中的计划，请先调用 write_plan"}

    plan = _dict_to_plan(plan_dict)
    step = plan.update_step(step_id, status, note)

    if step is None:
        return {"success": False, "respond": f"步骤 '{step_id}' 不存在"}

    # 回写
    agent_states["plan"] = plan.to_dict()

    return {
        "success": True,
        "respond": {
            "message":      f"步骤 '{step_id}' 已更新为 {status}",
            "updated_step": step.to_dict(),
            "next_pending": plan.next_pending().to_dict() if plan.next_pending() else None,
        }
    }


# ── 1. Plan.update_step 单元测试 ──────────────────────────────────

class TestPlanUpdateStep:
    """直接测试 Plan.update_step 方法"""

    def _make_plan(self) -> Plan:
        plan = Plan()
        plan.add_steps([
            {"step_id": "1", "title": "需求分析", "detail": "分析用户需求"},
            {"step_id": "2", "title": "大纲生成", "detail": "生成小说大纲"},
            {"step_id": "3", "title": "初稿撰写", "detail": "写初稿"},
        ])
        return plan

    def test_update_status_and_note(self):
        """正常更新步骤的 status 和 note"""
        plan = self._make_plan()
        step = plan.update_step("1", "in_progress", "开始分析")

        assert step is not None
        assert step.status == "in_progress"
        assert step.note == "开始分析"

    def test_update_nonexistent_step(self):
        """更新不存在的步骤应返回 None"""
        plan = self._make_plan()
        step = plan.update_step("999", "done", "")
        assert step is None

    def test_update_only_note_clears_status(self):
        """BUG: 只想更新 note 时，空 status 会覆盖原值"""
        plan = self._make_plan()
        # 先把步骤设为 in_progress
        plan.update_step("1", "in_progress", "")
        # 再想只更新 note，传空 status
        step = plan.update_step("1", "", "添加备注")
        assert step.note == "添加备注"
        assert step.status == ""  # BUG: status 被覆盖为空字符串！

    def test_cannot_update_title(self):
        """Plan.update_step 不支持更新 title"""
        plan = self._make_plan()
        step = plan.update_step("1", "in_progress", "")
        assert step.title == "需求分析"  # title 不变

    def test_cannot_update_detail(self):
        """Plan.update_step 不支持更新 detail"""
        plan = self._make_plan()
        step = plan.update_step("1", "in_progress", "")
        assert step.detail == "分析用户需求"  # detail 不变


# ── 2. Handler 核心逻辑测试 ───────────────────────────────────────

class TestUpdatePlanHandlerCore:
    """测试 update_plan handler 的核心逻辑"""

    def setup_method(self):
        self.plan = Plan()
        self.plan.add_steps([
            {"step_id": "s1", "title": "需求分析", "detail": "分析需求"},
            {"step_id": "s2", "title": "写初稿", "detail": "撰写初稿"},
        ])
        self.agent_states = {"plan": self.plan.to_dict()}

    def test_update_status_success(self):
        """正常更新 status"""
        result = _update_plan_core(self.agent_states, "s1", status="in_progress", note="开始")

        assert result["success"] is True
        assert result["respond"]["updated_step"]["status"] == "in_progress"
        assert result["respond"]["updated_step"]["note"] == "开始"

        # 验证 plan 被回写
        updated_plan = _dict_to_plan(self.agent_states["plan"])
        assert updated_plan.steps[0].status == "in_progress"

    def test_update_nonexistent_step(self):
        """更新不存在的步骤应失败"""
        result = _update_plan_core(self.agent_states, "nonexistent", status="done")
        assert result["success"] is False
        assert "不存在" in result["respond"]

    def test_update_empty_plan(self):
        """plan 为空时应失败"""
        self.agent_states["plan"] = {}
        result = _update_plan_core(self.agent_states, "s1", status="done")
        assert result["success"] is False
        assert "请先调用 write_plan" in result["respond"]

    def test_next_pending_after_update(self):
        """更新一个步骤为 done 后，next_pending 应指向下一个"""
        result = _update_plan_core(self.agent_states, "s1", status="done")

        assert result["success"] is True
        next_pending = result["respond"]["next_pending"]
        assert next_pending is not None
        assert next_pending["step_id"] == "s2"

    def test_no_next_pending_when_all_done(self):
        """所有步骤都完成后，next_pending 应为 None"""
        _update_plan_core(self.agent_states, "s1", status="done")
        result = _update_plan_core(self.agent_states, "s2", status="done")

        assert result["success"] is True
        assert result["respond"]["next_pending"] is None

    # ── BUG 验证测试 ───────────────────────────────────────────────

    def test_bug_title_param_ignored(self):
        """BUG: Tool schema 定义了 title 参数，但 handler 和 Plan.update_step 都不使用它"""
        result = _update_plan_core(self.agent_states, "s1", title="新标题", status="in_progress")

        assert result["success"] is True
        # title 没有被更新
        assert result["respond"]["updated_step"]["title"] == "需求分析"

    def test_bug_detail_param_ignored(self):
        """BUG: Tool schema 定义了 detail 参数，但 handler 和 Plan.update_step 都不使用它"""
        result = _update_plan_core(self.agent_states, "s1", detail="新详情", status="in_progress")

        assert result["success"] is True
        # detail 没有被更新
        assert result["respond"]["updated_step"]["detail"] == "分析需求"

    def test_bug_status_not_in_schema(self):
        """BUG: handler 使用 status 参数，但 Tool schema 没有定义 status"""
        # 检查 Tool schema 中是否缺少 status
        update_plan_tool = Tool._registry_dict.get("update_plan")
        if update_plan_tool:
            schema_props = update_plan_tool.input_schema.get("properties", {})
            assert "status" not in schema_props, (
                "status 不应在 schema 中（当前实现说明文档说 '不可修改执行状态'），"
                "但 handler 又需要 status 来更新"
            )

    def test_bug_empty_status_overwrites(self):
        """BUG: 不传 status 时空字符串会覆盖原有状态"""
        # 先设置 status 为 in_progress
        _update_plan_core(self.agent_states, "s1", status="in_progress", note="开始")

        # 只想更新 note，不传 status
        result = _update_plan_core(self.agent_states, "s1", note="添加备注")

        # status 被覆盖为空字符串！
        updated_step = result["respond"]["updated_step"]
        assert updated_step["status"] == ""  # BUG: 应该还是 "in_progress"


# ── 3. Schema 一致性报告 ─────────────────────────────────────────

class TestSchemaConsistencyReport:
    """输出 Schema 与 Handler 的不一致报告"""

    def test_report_schema_vs_handler(self):
        """打印 Schema 一致性报告"""
        update_plan_tool = Tool._registry_dict.get("update_plan")
        if not update_plan_tool:
            pytest.skip("update_plan Tool 未注册，需先导入 tools_attach_methods")

        schema_props = set(update_plan_tool.input_schema.get("properties", {}).keys())
        handler_fields = {"step_id", "status", "note"}  # handler 实际使用（排除 agent_id）
        schema_desc = update_plan_tool.description

        print(f"\n{'='*60}")
        print(f"[Schema 一致性报告] update_plan")
        print(f"{'='*60}")
        print(f"  Tool description: {schema_desc}")
        print(f"  Schema 定义字段:  {schema_props}")
        print(f"  Handler 使用字段: {handler_fields}")
        print(f"")
        print(f"  Schema 有但 Handler 不用: {schema_props - handler_fields}")
        print(f"  Handler 用但 Schema 没定义: {handler_fields - schema_props}")
        print(f"")
        print(f"  BUG 1: schema 定义了 title/detail，但 Plan.update_step() 不支持修改")
        print(f"  BUG 2: handler 使用 status，但 schema 未定义（description 说'不可修改执行状态'）")
        print(f"  BUG 3: 传空 status 会覆盖原有状态值（缺少 Optional 语义）")
        print(f"{'='*60}")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])

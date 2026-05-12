import asyncio
import time

import pytest

from domain.agent.plan.orchestrator import PlanOrchestrator
from domain.agent.plan.planAgent import PlanAgent
from domain.agent.plan.providers import (
    ExecutorStatusProvider,
    PlanObservationProvider,
    PlanStepPromptProvider,
)
from domain.agent_base import AgentBase
from domain.context.context import ContextEngine
from domain.event import EventBusPort
from domain.memory.short.default_short_term_memory import DefaultShortTermMemory
from domain.tool import Tool
from infra.tool.builtin import declare  # noqa: F401


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)

    async def chat(self, messages):
        return self.responses.pop(0)


class FakeExecutor(AgentBase):
    def __init__(self, id, delay=0.0):
        memory = DefaultShortTermMemory(["tool_respond", "agent_history"])
        context = ContextEngine(providers=[], memory=memory)
        super().__init__(id=id, name=id, llm=FakeLLM([]), context=context)
        self.delay = delay
        self.prompts = []

    async def start(self, prompt: str) -> None:
        self.prompts.append(prompt)
        await asyncio.sleep(self.delay)
        self.states["is_finished"] = True
        self.states["finish_reason"] = f"{self.id} done"
        self.states["final"] = f"{self.id} final"


class FakeEventBus(EventBusPort):
    def __init__(self):
        self.events = []
        self.subscriptions = {}

    async def publish_one(self, event):
        self.events.append(event)
        return None

    async def publish(self, event):
        self.events.append(event)
        return []

    def subscribe(self, event_name: str, handler) -> None:
        self.subscriptions.setdefault(event_name, []).append(handler)


def make_context():
    memory = DefaultShortTermMemory(["tool_respond", "agent_history"])
    return ContextEngine(
        providers=[ExecutorStatusProvider(), PlanObservationProvider()],
        memory=memory,
    )


def make_step_context(context):
    return ContextEngine(
        providers=[PlanStepPromptProvider()],
        memory=context.get_memory(),
    )


def make_orchestrator(planner, executors, event_bus=None):
    return PlanOrchestrator(
        planner=planner,
        executors=executors,
        state=planner.states,
        step_context_engine=make_step_context(planner.context_engine),
        event_bus=event_bus,
    )


def test_plan_tools_are_not_registered():
    names = {tool.name for tool in Tool.get_all_tools()}
    assert "write_plan" not in names
    assert "update_plan" not in names
    assert "finish_plan" not in names


def test_executor_status_provider_formats_executors():
    provider = ExecutorStatusProvider()
    output = provider.get({
        "executors": {
            "writer_001": {
                "name": "Writer",
                "is_finished": True,
                "finish_reason": "done",
            }
        }
    })

    assert "writer_001" in output[0]
    assert "Writer" in output[0]
    assert "finished" in output[0]


def test_executor_status_provider_handles_empty_executors():
    provider = ExecutorStatusProvider()
    output = provider.get({})
    assert "当前没有可用执行者" in output[0]


def test_plan_step_prompt_provider_formats_current_step():
    provider = PlanStepPromptProvider()
    output = provider.get({
        "prompt": "写一个故事",
        "current_step": {
            "step_id": "1",
            "title": "需求分析",
            "detail": "分析用户需求",
            "depends_on": ["A"],
        }
    })

    assert "写一个故事" in output[0]
    assert "step_id: 1" in output[0]
    assert "需求分析" in output[0]
    assert "分析用户需求" in output[0]
    assert "depends_on: ['A']" in output[0]


def test_plan_step_prompt_provider_returns_empty_without_current_step():
    provider = PlanStepPromptProvider()
    assert provider.get({"prompt": "写一个故事"}) == []


def test_plan_observation_provider_formats_observations():
    provider = PlanObservationProvider()
    output = provider.get({
        "plan": {
            "steps": [
                {
                    "step_id": "A",
                    "title": "读 README",
                    "executor_id": "writer_001",
                    "status": "done",
                    "observation": "README 已读取",
                }
            ]
        }
    })

    assert "读 README" in output[0]
    assert "README 已读取" in output[0]


def test_plan_step_roundtrip_keeps_dependencies_and_observation():
    from domain.state import Plan, _dict_to_plan

    plan = Plan()
    plan.add_steps([
        {
            "step_id": "D",
            "title": "总结项目结构",
            "detail": "总结",
            "executor_id": "writer_001",
            "depends_on": ["A", "B"],
            "observation": "已完成",
        }
    ])
    restored = _dict_to_plan(plan.to_dict())
    step = restored.steps[0]

    assert step.depends_on == ["A", "B"]
    assert step.observation == "已完成"


@pytest.mark.asyncio
async def test_plan_agent_generate_plan_uses_executor_ids():
    planner = PlanAgent(
        id="planner_capability",
        name="Planner",
        llm=FakeLLM(['{"steps": [{"step_id": "A", "title": "A", "executor_id": "writer_001"}]}']),
        context=make_context(),
    )

    plan = await planner.generate_plan(
        {"prompt": "分析项目", "executors": {}},
        ["writer_001"],
    )

    assert plan.steps[0].step_id == "A"
    assert plan.steps[0].executor_id == "writer_001"


@pytest.mark.asyncio
async def test_plan_agent_replan_and_summary_capabilities():
    from domain.state import Plan

    planner = PlanAgent(
        id="planner_capability_replan",
        name="Planner",
        llm=FakeLLM(['{"action": "continue", "reason": "ok"}', "最终总结"]),
        context=make_context(),
    )

    decision = await planner.replan_after_observation(Plan(), {"plan": {"steps": []}})
    summary = await planner.summarize_result({"plan": {"steps": []}})

    assert decision["action"] == "continue"
    assert summary == "最终总结"


@pytest.mark.asyncio
async def test_plan_agent_executes_plan_and_writes_final():
    plan_json = """
    {
      "steps": [
        {
          "step_id": "1",
          "title": "需求分析",
          "detail": "分析需求",
          "executor_id": "writer_001"
        }
      ]
    }
    """
    planner = PlanAgent(
        id="planner",
        name="Planner",
        llm=FakeLLM([plan_json, '{"action": "continue"}', "最终总结"]),
        context=make_context(),
    )
    orchestrator = make_orchestrator(
        planner,
        {"writer_001": FakeExecutor("writer_001")},
    )

    await orchestrator.start("写一个故事")

    assert planner.states["plan"]["steps"][0]["status"] == "done"
    assert "executor_id=writer_001" in planner.states["plan"]["steps"][0]["observation"]
    assert planner.states["final"] == "最终总结"
    assert planner.states["is_finished"] is True


@pytest.mark.asyncio
async def test_plan_agent_marks_unknown_executor_failed():
    plan_json = """
    {
      "steps": [
        {
          "step_id": "1",
          "title": "未知执行者步骤",
          "detail": "测试",
          "executor_id": "missing"
        }
      ]
    }
    """
    planner = PlanAgent(
        id="planner_unknown",
        name="Planner",
        llm=FakeLLM([plan_json, "最终总结"]),
        context=make_context(),
    )
    orchestrator = make_orchestrator(
        planner,
        {"writer_001": FakeExecutor("writer_001")},
    )

    await orchestrator.start("写一个故事")

    step = planner.states["plan"]["steps"][0]
    assert step["status"] == "failed"
    assert "未知 executor_id" in step["note"]


@pytest.mark.asyncio
async def test_plan_agent_runs_different_executors_concurrently():
    plan_json = """
    {
      "steps": [
        {
          "step_id": "1",
          "title": "步骤一",
          "detail": "交给一号",
          "executor_id": "writer_001"
        },
        {
          "step_id": "2",
          "title": "步骤二",
          "detail": "交给二号",
          "executor_id": "writer_002"
        }
      ]
    }
    """
    planner = PlanAgent(
        id="planner_concurrent",
        name="Planner",
        llm=FakeLLM([plan_json, '{"action": "continue"}', "最终总结"]),
        context=make_context(),
    )
    orchestrator = make_orchestrator(
        planner,
        {
            "writer_001": FakeExecutor("writer_001", delay=0.05),
            "writer_002": FakeExecutor("writer_002", delay=0.05),
        },
    )

    start = time.perf_counter()
    await orchestrator.start("写一个故事")
    elapsed = time.perf_counter() - start

    assert elapsed < 0.09
    assert [step["status"] for step in planner.states["plan"]["steps"]] == ["done", "done"]


@pytest.mark.asyncio
async def test_plan_agent_respects_dependencies_by_wave():
    plan_json = """
    {
      "steps": [
        {"step_id": "A", "title": "读 README", "detail": "读 A", "executor_id": "writer_001", "depends_on": []},
        {"step_id": "B", "title": "读 pyproject", "detail": "读 B", "executor_id": "writer_002", "depends_on": []},
        {"step_id": "D", "title": "总结项目结构", "detail": "总结", "executor_id": "writer_003", "depends_on": ["A", "B"]}
      ]
    }
    """
    planner = PlanAgent(
        id="planner_deps",
        name="Planner",
        llm=FakeLLM([
            plan_json,
            '{"action": "continue"}',
            '{"action": "continue"}',
            "最终总结",
        ]),
        context=make_context(),
    )
    executors = {
            "writer_001": FakeExecutor("writer_001", delay=0.05),
            "writer_002": FakeExecutor("writer_002", delay=0.05),
            "writer_003": FakeExecutor("writer_003", delay=0.0),
    }
    orchestrator = make_orchestrator(
        planner,
        executors,
    )

    start = time.perf_counter()
    await orchestrator.start("分析项目")
    elapsed = time.perf_counter() - start

    assert elapsed >= 0.05
    assert elapsed < 0.12
    assert [step["status"] for step in planner.states["plan"]["steps"]] == ["done", "done", "done"]
    assert "writer_001 done" in executors["writer_003"].prompts[0]
    assert "writer_002 done" in executors["writer_003"].prompts[0]


@pytest.mark.asyncio
async def test_plan_agent_fails_blocked_or_cyclic_dependencies():
    plan_json = """
    {
      "steps": [
        {"step_id": "A", "title": "循环 A", "detail": "A", "executor_id": "writer_001", "depends_on": ["B"]},
        {"step_id": "B", "title": "循环 B", "detail": "B", "executor_id": "writer_001", "depends_on": ["A"]},
        {"step_id": "C", "title": "缺失 C", "detail": "C", "executor_id": "writer_001", "depends_on": ["missing"]}
      ]
    }
    """
    planner = PlanAgent(
        id="planner_blocked",
        name="Planner",
        llm=FakeLLM([plan_json, "最终总结"]),
        context=make_context(),
    )
    orchestrator = make_orchestrator(
        planner,
        {"writer_001": FakeExecutor("writer_001")},
    )

    await orchestrator.start("分析项目")

    assert [step["status"] for step in planner.states["plan"]["steps"]] == ["failed", "failed", "failed"]
    assert "循环依赖" in planner.states["plan"]["steps"][0]["note"]
    assert "依赖不存在" in planner.states["plan"]["steps"][2]["note"]


@pytest.mark.asyncio
async def test_plan_agent_replan_updates_existing_pending_step():
    plan_json = """
    {
      "steps": [
        {"step_id": "A", "title": "初始步骤", "detail": "A", "executor_id": "writer_001", "depends_on": []},
        {"step_id": "B", "title": "待更新步骤", "detail": "old", "executor_id": "writer_001", "depends_on": ["A"]}
      ]
    }
    """
    replan_json = """
    {
      "action": "replan",
      "reason": "需要补充",
      "steps": [
        {"step_id": "B", "title": "更新后的步骤", "detail": "new", "executor_id": "writer_001", "depends_on": ["A"]},
        {"step_id": "C", "title": "不应新增", "detail": "C", "executor_id": "writer_001", "depends_on": ["A"]}
      ]
    }
    """
    planner = PlanAgent(
        id="planner_replan",
        name="Planner",
        llm=FakeLLM([plan_json, replan_json, '{"action": "continue"}', "最终总结"]),
        context=make_context(),
    )
    orchestrator = make_orchestrator(
        planner,
        {"writer_001": FakeExecutor("writer_001")},
    )

    await orchestrator.start("分析项目")

    assert [step["step_id"] for step in planner.states["plan"]["steps"]] == ["A", "B"]
    assert [step["status"] for step in planner.states["plan"]["steps"]] == ["done", "done"]
    assert planner.states["plan"]["steps"][1]["title"] == "更新后的步骤"
    assert planner.states["plan"]["steps"][1]["detail"] == "new"


@pytest.mark.asyncio
async def test_plan_agent_replan_finish_skips_pending_steps():
    plan_json = """
    {
      "steps": [
        {"step_id": "A", "title": "初始步骤", "detail": "A", "executor_id": "writer_001", "depends_on": []},
        {"step_id": "B", "title": "后续步骤", "detail": "B", "executor_id": "writer_001", "depends_on": ["A"]}
      ]
    }
    """
    planner = PlanAgent(
        id="planner_finish",
        name="Planner",
        llm=FakeLLM([plan_json, '{"action": "finish", "reason": "足够了"}', "最终总结"]),
        context=make_context(),
    )
    orchestrator = make_orchestrator(
        planner,
        {"writer_001": FakeExecutor("writer_001")},
    )

    await orchestrator.start("分析项目")

    assert [step["status"] for step in planner.states["plan"]["steps"]] == ["done", "skipped"]
    assert planner.states["plan"]["steps"][1]["note"] == "足够了"


@pytest.mark.asyncio
async def test_plan_agent_publishes_step_and_wave_events():
    event_bus = FakeEventBus()
    plan_json = """
    {
      "steps": [
        {"step_id": "A", "title": "初始步骤", "detail": "A", "executor_id": "writer_001", "depends_on": []}
      ]
    }
    """
    planner = PlanAgent(
        id="planner_events",
        name="Planner",
        llm=FakeLLM([plan_json, '{"action": "continue"}', "最终总结"]),
        context=make_context(),
    )
    orchestrator = make_orchestrator(
        planner,
        {"writer_001": FakeExecutor("writer_001")},
        event_bus=event_bus,
    )

    await orchestrator.start("分析项目")

    event_names = [event.name for event in event_bus.events]
    assert "plan.step.observed" in event_names
    assert "plan.wave.completed" in event_names


@pytest.mark.asyncio
async def test_plan_orchestrator_runs_without_event_bus():
    plan_json = """
    {
      "steps": [
        {"step_id": "A", "title": "初始步骤", "detail": "A", "executor_id": "writer_001", "depends_on": []}
      ]
    }
    """
    planner = PlanAgent(
        id="planner_no_events",
        name="Planner",
        llm=FakeLLM([plan_json, '{"action": "continue"}', "最终总结"]),
        context=make_context(),
    )
    orchestrator = make_orchestrator(
        planner,
        {"writer_001": FakeExecutor("writer_001")},
    )

    await orchestrator.start("分析项目")

    assert planner.states["is_finished"] is True
    assert planner.states["final"] == "最终总结"

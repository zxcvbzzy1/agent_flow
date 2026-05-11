import asyncio
import time

import pytest

from domain.agent.plan.planAgent import PlanAgent
from domain.agent.plan.providers import ExecutorStatusProvider, PlanStepPromptProvider
from domain.agent_base import AgentBase
from domain.context.context import ContextEngine
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


def make_context():
    memory = DefaultShortTermMemory(["tool_respond", "agent_history"])
    return ContextEngine(
        providers=[ExecutorStatusProvider()],
        memory=memory,
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
        }
    })

    assert "写一个故事" in output[0]
    assert "step_id: 1" in output[0]
    assert "需求分析" in output[0]
    assert "分析用户需求" in output[0]


def test_plan_step_prompt_provider_returns_empty_without_current_step():
    provider = PlanStepPromptProvider()
    assert provider.get({"prompt": "写一个故事"}) == []


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
    agent = PlanAgent(
        id="planner",
        name="Planner",
        llm=FakeLLM([plan_json, "最终总结"]),
        context=make_context(),
        executors={"writer_001": FakeExecutor("writer_001")},
    )

    await agent.start("写一个故事")

    assert agent.states["plan"]["steps"][0]["status"] == "done"
    assert agent.states["final"] == "最终总结"
    assert agent.states["is_finished"] is True


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
    agent = PlanAgent(
        id="planner_unknown",
        name="Planner",
        llm=FakeLLM([plan_json, "最终总结"]),
        context=make_context(),
        executors={"writer_001": FakeExecutor("writer_001")},
    )

    await agent.start("写一个故事")

    step = agent.states["plan"]["steps"][0]
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
    agent = PlanAgent(
        id="planner_concurrent",
        name="Planner",
        llm=FakeLLM([plan_json, "最终总结"]),
        context=make_context(),
        executors={
            "writer_001": FakeExecutor("writer_001", delay=0.05),
            "writer_002": FakeExecutor("writer_002", delay=0.05),
        },
    )

    start = time.perf_counter()
    await agent.start("写一个故事")
    elapsed = time.perf_counter() - start

    assert elapsed < 0.09
    assert [step["status"] for step in agent.states["plan"]["steps"]] == ["done", "done"]

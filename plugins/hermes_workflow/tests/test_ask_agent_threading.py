"""Tests for WorkflowRuntime.ask_agent structured-output kwargs."""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

from plugins.hermes_workflow.agent_bridge import AgentResponse, JournalingBridge
from plugins.hermes_workflow.runtime import WorkflowRuntime


class _CaptureBridge(JournalingBridge):
    """Wraps a stub inner bridge and records the kwargs it sees."""

    def __init__(self):
        super().__init__(inner=None)
        self.received_kwargs: dict | None = None

    async def invoke(self, **kwargs):
        self.received_kwargs = kwargs
        return AgentResponse(text="ok")


class TestAskAgentThreading:
    def test_ask_agent_accepts_json_schema_kwarg(self):
        sig = inspect.signature(WorkflowRuntime.ask_agent)
        assert "json_schema" in sig.parameters

    def test_ask_agent_accepts_schema_name_kwarg(self):
        sig = inspect.signature(WorkflowRuntime.ask_agent)
        assert "schema_name" in sig.parameters

    def test_ask_agent_threads_kwargs_to_bridge(self):
        rt = WorkflowRuntime(
            journal_root=Path("/tmp/_test_rt_ask"),
            default_max_concurrent=2,
        )
        capture = _CaptureBridge()
        rt.set_agent_bridge(capture)
        asyncio.run(rt.ask_agent(
            prompt="hi", model=None, max_tokens=None,
            json_schema={"type": "object"}, schema_name="MySchema",
        ))
        assert capture.received_kwargs is not None
        assert capture.received_kwargs["json_schema"] == {"type": "object"}
        assert capture.received_kwargs["schema_name"] == "MySchema"

    def test_ask_agent_threads_none_when_no_schema(self):
        rt = WorkflowRuntime(
            journal_root=Path("/tmp/_test_rt_ask2"),
            default_max_concurrent=2,
        )
        capture = _CaptureBridge()
        rt.set_agent_bridge(capture)
        asyncio.run(rt.ask_agent(prompt="hi", model=None, max_tokens=None))
        assert capture.received_kwargs is not None
        assert capture.received_kwargs["json_schema"] is None
        assert capture.received_kwargs["schema_name"] is None
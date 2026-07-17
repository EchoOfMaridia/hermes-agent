"""Tests for JournalingBridge structured-output journal events."""

from __future__ import annotations

import asyncio

from plugins.hermes_workflow.agent_bridge import AgentBridge, AgentResponse, JournalingBridge
from plugins.hermes_workflow.dsl.primitives import set_current_run
from plugins.hermes_workflow.journal import Journal


class _MemJournal:
    """In-memory journal capturing events for assertions."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def append(self, event: dict) -> None:
        self.events.append(event)


class _FakeRun:
    """Minimal duck-typed run for JournalingBridge.invoke."""

    def __init__(self, run_id: str = "r_test") -> None:
        self.run_id = run_id
        self.current_step_name = "test_step"
        self.journal = _MemJournal()
        self._call_index = 0

    def next_agent_call_index(self) -> int:
        self._call_index += 1
        return self._call_index

    def touch(self) -> None:
        pass


class _StubBridge(AgentBridge):
    async def invoke(self, **kwargs):
        return AgentResponse(text="ok")


class _StubWithParsed(AgentBridge):
    async def invoke(self, **kwargs):
        return AgentResponse(
            text='{"intent": "refund", "confidence": 0.92}',
            parsed={"intent": "refund", "confidence": 0.92},
            content_type="json",
        )


class _StubWithList(AgentBridge):
    async def invoke(self, **kwargs):
        return AgentResponse(
            text="[1, 2, 3]",
            parsed=[1, 2, 3],
            content_type="json",
        )


class _StubWithScalar(AgentBridge):
    async def invoke(self, **kwargs):
        return AgentResponse(
            text='"hello"',
            parsed="hello",
            content_type="json",
        )


class TestJournalStructuredOutputEvents:
    def test_agent_call_records_schema_name_and_has_json_schema(self):
        fake_run = _FakeRun()
        token = set_current_run(fake_run)
        try:
            jb = JournalingBridge(inner=_StubBridge())
            asyncio.run(jb.invoke(
                prompt="hi", model=None, max_tokens=None,
                json_schema={"type": "object"}, schema_name="MyResponse",
            ))
        finally:
            set_current_run(None)

        agent_call_events = [
            e for e in fake_run.journal.events if e["kind"] == "agent_call"
        ]
        assert len(agent_call_events) == 1
        assert agent_call_events[0]["schema_name"] == "MyResponse"
        assert agent_call_events[0]["has_json_schema"] is True

    def test_agent_call_records_no_schema_when_unset(self):
        fake_run = _FakeRun()
        token = set_current_run(fake_run)
        try:
            jb = JournalingBridge(inner=_StubBridge())
            asyncio.run(jb.invoke(prompt="hi", model=None, max_tokens=None))
        finally:
            set_current_run(None)

        agent_call_events = [
            e for e in fake_run.journal.events if e["kind"] == "agent_call"
        ]
        assert agent_call_events[0]["schema_name"] is None
        assert agent_call_events[0]["has_json_schema"] is False

    def test_agent_response_records_content_type_and_parsed_shape_for_dict(self):
        fake_run = _FakeRun()
        token = set_current_run(fake_run)
        try:
            jb = JournalingBridge(inner=_StubWithParsed())
            asyncio.run(jb.invoke(prompt="hi", model=None, max_tokens=None))
        finally:
            set_current_run(None)

        agent_response_events = [
            e for e in fake_run.journal.events if e["kind"] == "agent_response"
        ]
        assert len(agent_response_events) == 1
        assert agent_response_events[0]["content_type"] == "json"
        # Shape = top-level keys, NEVER values.
        assert agent_response_events[0]["parsed_shape"] == ["intent", "confidence"]

    def test_agent_response_records_parsed_shape_for_list(self):
        fake_run = _FakeRun()
        token = set_current_run(fake_run)
        try:
            jb = JournalingBridge(inner=_StubWithList())
            asyncio.run(jb.invoke(prompt="hi", model=None, max_tokens=None))
        finally:
            set_current_run(None)

        agent_response_events = [
            e for e in fake_run.journal.events if e["kind"] == "agent_response"
        ]
        assert agent_response_events[0]["parsed_shape"] == ["<list>"]

    def test_agent_response_records_parsed_shape_for_scalar(self):
        fake_run = _FakeRun()
        token = set_current_run(fake_run)
        try:
            jb = JournalingBridge(inner=_StubWithScalar())
            asyncio.run(jb.invoke(prompt="hi", model=None, max_tokens=None))
        finally:
            set_current_run(None)

        agent_response_events = [
            e for e in fake_run.journal.events if e["kind"] == "agent_response"
        ]
        assert agent_response_events[0]["parsed_shape"] == ["str"]

    def test_agent_response_records_empty_shape_when_no_parse(self):
        fake_run = _FakeRun()
        token = set_current_run(fake_run)
        try:
            jb = JournalingBridge(inner=_StubBridge())
            asyncio.run(jb.invoke(prompt="hi", model=None, max_tokens=None))
        finally:
            set_current_run(None)

        agent_response_events = [
            e for e in fake_run.journal.events if e["kind"] == "agent_response"
        ]
        # No parsed -> default content_type "text", empty parsed_shape.
        assert agent_response_events[0]["content_type"] == "text"
        assert agent_response_events[0]["parsed_shape"] == []
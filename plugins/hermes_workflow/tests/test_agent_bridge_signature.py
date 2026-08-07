"""Tests for AgentBridge + AgentResponse + JournalingBridge structured-output kwargs."""

from __future__ import annotations

import asyncio
import inspect

from plugins.hermes_workflow.agent_bridge import AgentBridge, AgentResponse, JournalingBridge


# ---------------------------------------------------------------------------
# Signature checks
# ---------------------------------------------------------------------------


class TestAgentBridgeInvokeSignature:
    def test_invoke_accepts_json_schema_kwarg(self):
        sig = inspect.signature(AgentBridge.invoke)
        assert "json_schema" in sig.parameters

    def test_invoke_accepts_schema_name_kwarg(self):
        sig = inspect.signature(AgentBridge.invoke)
        assert "schema_name" in sig.parameters

    def test_invoke_json_schema_defaults_none(self):
        sig = inspect.signature(AgentBridge.invoke)
        assert sig.parameters["json_schema"].default is None

    def test_invoke_schema_name_defaults_none(self):
        sig = inspect.signature(AgentBridge.invoke)
        assert sig.parameters["schema_name"].default is None


class TestAgentResponseFields:
    def test_response_has_parsed_field(self):
        sig = inspect.signature(AgentResponse)
        assert "parsed" in sig.parameters
        assert sig.parameters["parsed"].default is None

    def test_response_has_content_type_field(self):
        sig = inspect.signature(AgentResponse)
        assert "content_type" in sig.parameters
        assert sig.parameters["content_type"].default == "text"


# ---------------------------------------------------------------------------
# JournalingBridge threading
# ---------------------------------------------------------------------------


class _CapturingBridge:
    """Stub bridge that records the kwargs it was invoked with."""
    def __init__(self):
        self.received_kwargs = None

    async def invoke(self, **kwargs):
        self.received_kwargs = kwargs
        return AgentResponse(text="ok")


class TestJournalingBridgeInvoke:
    def test_journaling_bridge_accepts_json_schema(self):
        sig = inspect.signature(JournalingBridge.invoke)
        assert "json_schema" in sig.parameters
        assert "schema_name" in sig.parameters

    def test_journaling_bridge_threads_json_schema_to_inner(self):
        stub = _CapturingBridge()
        jb = JournalingBridge(inner=stub)
        asyncio.run(jb.invoke(
            prompt="hi", model=None, max_tokens=None,
            json_schema={"type": "object"}, schema_name="MySchema",
        ))
        assert stub.received_kwargs["json_schema"] == {"type": "object"}
        assert stub.received_kwargs["schema_name"] == "MySchema"

    def test_journaling_bridge_threads_no_schema(self):
        stub = _CapturingBridge()
        jb = JournalingBridge(inner=stub)
        asyncio.run(jb.invoke(prompt="hi", model=None, max_tokens=None))
        assert stub.received_kwargs["json_schema"] is None
        assert stub.received_kwargs["schema_name"] is None
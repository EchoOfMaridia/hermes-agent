"""Tests for PluginLlmBridge — in-process bridge wrapping ctx.llm.acomplete_structured."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from plugins.hermes_workflow.agent_bridge import AgentResponse
from plugins.hermes_workflow.plugin_llm_bridge import PluginLlmBridge


@dataclass
class _FakeUsage:
    input_tokens: int = 10
    output_tokens: int = 20
    total_tokens: int = 30


@dataclass
class _FakeStructuredResult:
    text: str = ""
    parsed: Any = None
    content_type: str = "text"
    usage: _FakeUsage = None  # type: ignore[assignment]
    provider: str = "fake"
    model: str = "fake-model"
    agent_id: str = "default"
    audit: dict = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.usage is None:
            self.usage = _FakeUsage()
        if self.audit is None:
            self.audit = {}


class _FakePluginLlm:
    """Mimics the PluginLlm.acomplete_structured + acomplete surface."""

    def __init__(
        self,
        *,
        structured_response: _FakeStructuredResult | None = None,
        text_response=None,
    ):
        self._structured = structured_response
        self._text = text_response
        self.received_kwargs: dict | None = None
        self.received_messages: list | None = None

    async def acomplete_structured(self, **kwargs):
        self.received_kwargs = kwargs
        return self._structured

    async def acomplete(self, messages, **kwargs):
        self.received_messages = messages
        if self._text is not None:
            return self._text
        # Default text response when none provided.
        return _FakeTextResult(text="plain text response")

    async def complete(self, messages, **kwargs):
        # Synchronous-style fallback for bridges that only expose .complete.
        self.received_messages = messages
        if self._text is not None:
            return self._text
        return _FakeTextResult(text="plain text response")


@dataclass
class _FakeTextResult:
    text: str = ""
    usage: _FakeUsage = None  # type: ignore[assignment]
    provider: str = "fake"
    model: str = "fake-model"
    agent_id: str = "default"

    def __post_init__(self):
        if self.usage is None:
            self.usage = _FakeUsage()


class TestPluginLlmBridgeStructuredPath:
    def test_bridge_calls_acomplete_structured_when_json_schema_set(self):
        fake = _FakePluginLlm(
            structured_response=_FakeStructuredResult(
                text='{"a": 1}',
                parsed={"a": 1},
                content_type="json",
            )
        )
        bridge = PluginLlmBridge(llm=fake)
        schema = {"type": "object", "properties": {"a": {"type": "number"}}}
        response = asyncio.run(bridge.invoke(
            prompt="classify", model=None, max_tokens=None,
            json_schema=schema, schema_name="MySchema",
        ))
        assert response.parsed == {"a": 1}
        assert response.content_type == "json"
        assert fake.received_kwargs is not None
        assert fake.received_kwargs["json_schema"] == schema
        assert fake.received_kwargs["schema_name"] == "MySchema"

    def test_bridge_threads_tokens_from_usage(self):
        fake = _FakePluginLlm(
            structured_response=_FakeStructuredResult(
                text="x",
                parsed=None,
                content_type="text",
                usage=_FakeUsage(input_tokens=42, output_tokens=7, total_tokens=49),
            )
        )
        bridge = PluginLlmBridge(llm=fake)
        response = asyncio.run(bridge.invoke(
            prompt="hi", model=None, max_tokens=None,
            json_schema={"type": "object"},
        ))
        assert response.tokens_in == 42
        assert response.tokens_out == 7

    def test_bridge_passes_model_override_through(self):
        fake = _FakePluginLlm(
            structured_response=_FakeStructuredResult(text="x", parsed=None)
        )
        bridge = PluginLlmBridge(llm=fake)
        asyncio.run(bridge.invoke(
            prompt="hi",
            model="anthropic/claude-sonnet-4",
            max_tokens=None,
            json_schema={"type": "object"},
        ))
        assert fake.received_kwargs["model"] == "anthropic/claude-sonnet-4"


class TestPluginLlmBridgeTextFallback:
    def test_bridge_falls_back_to_acomplete_when_no_json_schema(self):
        fake = _FakePluginLlm(
            text_response=_FakeTextResult(
                text="plain text",
                usage=_FakeUsage(input_tokens=5, output_tokens=8, total_tokens=13),
            )
        )
        bridge = PluginLlmBridge(llm=fake)
        response = asyncio.run(bridge.invoke(
            prompt="hi", model=None, max_tokens=None,
        ))
        assert response.text == "plain text"
        assert response.parsed is None
        assert response.content_type == "text"
        assert response.tokens_in == 5
        assert response.tokens_out == 8

    def test_bridge_passes_system_prompt(self):
        fake = _FakePluginLlm(
            text_response=_FakeTextResult(text="ok"),
        )
        bridge = PluginLlmBridge(llm=fake)
        asyncio.run(bridge.invoke(
            prompt="user prompt",
            model=None,
            max_tokens=None,
            system_prompt="be terse",
        ))
        # System prompt should appear in the messages list.
        assert fake.received_messages is not None
        assert any(
            m.get("role") == "system" and "be terse" in m.get("content", "")
            for m in fake.received_messages
        )
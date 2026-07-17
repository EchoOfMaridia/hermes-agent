"""Tests for Step 5: agent bridge.

What we verify:

AgentResponse:
- Construction with text only
- Construction with tool_calls, tokens, duration

JournalingBridge:
- Without an inner bridge, raises NotImplementedError
- Calls inner.invoke() and returns its response
- set_inner() swaps the bridge

WorkflowRuntime.ask_agent:
- Returns the bridge's response
- A workflow step that calls ask_agent completes successfully when a
  stub bridge is installed
- The agent_call + agent_response events appear in the journal
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from plugins.hermes_workflow import (
    Evidence,
    step,
    workflow,
)
from plugins.hermes_workflow.agent_bridge import (
    AgentBridge,
    AgentResponse,
    JournalingBridge,
)
from plugins.hermes_workflow.journal import Journal
from plugins.hermes_workflow.runtime import WorkflowRuntime

from plugins.hermes_workflow.tests._runtime_helpers import (
    import_workflow,
    submit_and_wait,
    write_workflow_module,
)


# ---------------------------------------------------------------------------
# AgentResponse
# ---------------------------------------------------------------------------

class TestAgentResponse:
    def test_text_only(self):
        r = AgentResponse(text="hello")
        assert r.text == "hello"
        assert r.tool_calls == ()
        assert r.tokens_in == 0
        assert r.tokens_out == 0
        assert r.duration == 0.0

    def test_full_construction(self):
        r = AgentResponse(
            text="response",
            tool_calls=("Read", "Grep"),
            tokens_in=100,
            tokens_out=50,
            duration=2.5,
        )
        assert r.tool_calls == ("Read", "Grep")
        assert r.tokens_in == 100
        assert r.tokens_out == 50
        assert r.duration == 2.5


# ---------------------------------------------------------------------------
# JournalingBridge
# ---------------------------------------------------------------------------

class TestJournalingBridge:
    def test_no_inner_raises(self):
        bridge = JournalingBridge()
        with pytest.raises(NotImplementedError):
            asyncio.run(bridge.invoke(prompt="hi", model=None, max_tokens=None))

    def test_delegates_to_inner(self):
        class StubBridge(AgentBridge):
            async def invoke(self, *, prompt, model, max_tokens,
                             tools=None, session_key=None,
                             system_prompt=None,
                 json_schema=None, schema_name=None):
                return AgentResponse(text=f"echo: {prompt}",
                                     tokens_in=10, tokens_out=5)

        bridge = JournalingBridge(inner=StubBridge())
        result = asyncio.run(bridge.invoke(
            prompt="hello", model="sonnet", max_tokens=100,
        ))
        assert result.text == "echo: hello"
        assert result.tokens_in == 10

    def test_set_inner_swaps(self):
        class FirstBridge(AgentBridge):
            async def invoke(self, *, prompt, model, max_tokens,
                             tools=None, session_key=None,
                             system_prompt=None,
                 json_schema=None, schema_name=None):
                return AgentResponse(text="first")

        class SecondBridge(AgentBridge):
            async def invoke(self, *, prompt, model, max_tokens,
                             tools=None, session_key=None,
                             system_prompt=None,
                 json_schema=None, schema_name=None):
                return AgentResponse(text="second")

        bridge = JournalingBridge(inner=FirstBridge())
        r1 = asyncio.run(bridge.invoke(
            prompt="x", model=None, max_tokens=None,
        ))
        assert r1.text == "first"

        bridge.set_inner(SecondBridge())
        r2 = asyncio.run(bridge.invoke(
            prompt="x", model=None, max_tokens=None,
        ))
        assert r2.text == "second"


# ---------------------------------------------------------------------------
# Runtime.ask_agent + journal integration
# ---------------------------------------------------------------------------

class TestRuntimeAskAgent:
    def test_ask_agent_returns_response(self, tmp_path):
        class StubBridge(AgentBridge):
            async def invoke(self, *, prompt, model, max_tokens,
                             tools=None, session_key=None,
                             system_prompt=None,
                 json_schema=None, schema_name=None):
                return AgentResponse(text=f"got: {prompt}",
                                     tool_calls=("Read",))

        rt = WorkflowRuntime(journal_root=tmp_path)
        rt.set_agent_bridge(StubBridge())
        result = asyncio.run(rt.ask_agent(
            prompt="hi", model="sonnet", max_tokens=50,
        ))
        assert result.text == "got: hi"
        assert result.tool_calls == ("Read",)

    def test_workflow_step_calls_ask_agent(self, tmp_path):
        """End-to-end: a step calls ctx.runtime.ask_agent and the call
        appears in the journal as agent_call + agent_response."""
        class StubBridge(AgentBridge):
            async def invoke(self, *, prompt, model, max_tokens,
                             tools=None, session_key=None,
                             system_prompt=None,
                 json_schema=None, schema_name=None):
                return AgentResponse(text="analysis result",
                                     tool_calls=("Read", "Grep"),
                                     tokens_in=42, tokens_out=17)

        mod_path = write_workflow_module(tmp_path, """
            from plugins.hermes_workflow import step, workflow, Evidence

            def _empty_ev():
                return Evidence(files_changed=(), commands_run=(), exit_codes=(),
                                tests_run=0, tests_passed=0, duration_seconds=0.0)

            @step(name="ask")
            async def ask(ctx) -> Evidence:
                resp = await ctx.runtime.ask_agent(
                    prompt="analyze this",
                    model="sonnet",
                )
                return Evidence(
                    files_changed=("analysis.md",),
                    commands_run=(),
                    exit_codes=(),
                    tests_run=0,
                    tests_passed=0,
                    duration_seconds=0.5,
                )

            @workflow(name="ask_wf")
            async def ask_wf(ctx) -> dict:
                await ask(ctx)
                return {}
        """)

        async def _go():
            rt = WorkflowRuntime(journal_root=tmp_path)
            rt.set_agent_bridge(StubBridge())
            module = import_workflow(mod_path)
            run_id, run = await submit_and_wait(rt, module["ask_wf"], {})
            journal = Journal.replay(run_id, tmp_path)
            calls = journal.agent_calls()
            response_events = [
                e for e in journal.events
                if e.get("kind") == "agent_response"
            ]
            return (run.state.value, len(calls), calls[0]["prompt_chars"],
                    calls[0]["model"],
                    len(response_events),
                    response_events[0]["tool_calls"])

        (state, n_calls, prompt_chars, model,
         n_responses, tool_calls) = asyncio.run(_go())
        assert state == "done"
        assert n_calls == 1
        assert prompt_chars == len("analyze this")
        assert model == "sonnet"
        assert n_responses == 1
        # Legacy string-only tool_calls ("Read", "Grep") are journaled
        # as minimal structured records ({"name": ..., "args": {},
        # "result_chars": 0}) so legacy callers don't break the journal
        # write. See agent_bridge.py:JournalingBridge.invoke for the
        # conversion logic.
        assert tool_calls == [
            {"name": "Read", "args": {}, "result_chars": 0},
            {"name": "Grep", "args": {}, "result_chars": 0},
        ]

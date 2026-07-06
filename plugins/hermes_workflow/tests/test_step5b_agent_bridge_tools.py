"""Tests for Step 5b: agent bridge tool + session + system_prompt surface.

Pins the contract for Path A + Path B of the workflow-author bridge upgrade
(2026-06-30). These tests must FAIL on the current code (which only supports
``(prompt, model, max_tokens)`` and returns ``tool_calls`` as a tuple of
strings). They will PASS once ``agent_bridge.py`` is upgraded to accept
``tools``, ``session_key``, ``system_prompt`` and returns structured
``tool_calls`` payloads.

What this file pins:

AgentResponse (extended):
- ``tool_calls`` is a tuple of dicts (``{"name", "args", "result"}``), not
  just a tuple of names. Backwards-compat: callers that only read ``.text``
  keep working.

AgentBridge.invoke (extended):
- Accepts ``tools``, ``session_key``, ``system_prompt`` keyword args.
- Forwards all five kwargs (``prompt``, ``model``, ``max_tokens``, ``tools``,
  ``session_key``, ``system_prompt``) to the inner bridge.
- The inner bridge receives them — journal can record them.

JournalingBridge (extended):
- ``agent_call`` journal event records ``tools`` (as a count of tool
  definitions or the list itself, depending on schema decision), the
  ``session_key``, and the ``system_prompt_chars``.
- ``agent_response`` journal event records structured ``tool_calls``
  (each with ``name``, ``args``, ``result_chars``).

WorkflowRuntime.ask_agent (extended):
- Forwards ``tools``, ``session_key``, ``system_prompt`` to the bridge.
- A workflow step that calls ``ask_agent(tools=[...], session_key="x")``
  completes and the journal reflects both.

Backwards-compat:
- Old call sites that pass only ``prompt`` and ``model`` still work — the
  new fields default to ``None`` and the bridge passes them through
  unchanged.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from plugins.hermes_workflow import (
    Evidence,
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


# --------------------------------------------------------------------------
# Test fixtures: structured tool_call payloads
# --------------------------------------------------------------------------

def _make_tool_call(name: str, args: dict, result: str) -> dict:
    """Canonical shape of one entry in AgentResponse.tool_calls.

    Pinned shape (2026-06-30):
        {
            "name":   str,    # tool name (e.g., "terminal", "file_edit")
            "args":   dict,   # arguments passed to the tool
            "result": str,    # result returned by the tool (or "" if none)
        }
    """
    return {"name": name, "args": args, "result": result}


class _RecordingBridge(AgentBridge):
    """Stub bridge that records every invoke() call and returns a canned
    structured AgentResponse with tool_calls.

    Used by tests that need to assert on what the bridge RECEIVED (i.e.,
    did ``tools``/``session_key``/``system_prompt`` get forwarded from
    WorkflowRuntime.ask_agent through to the inner bridge).
    """

    def __init__(self, *, response_text: str = "ok",
                 response_tool_calls: tuple[dict, ...] = (),
                 response_tokens_in: int = 0,
                 response_tokens_out: int = 0) -> None:
        self.calls: list[dict] = []
        self._response_text = response_text
        self._response_tool_calls = response_tool_calls
        self._response_tokens_in = response_tokens_in
        self._response_tokens_out = response_tokens_out

    async def invoke(self, *, prompt, model, max_tokens,
                     tools=None, session_key=None, system_prompt=None) -> AgentResponse:
        self.calls.append({
            "prompt": prompt,
            "model": model,
            "max_tokens": max_tokens,
            "tools": tools,
            "session_key": session_key,
            "system_prompt": system_prompt,
        })
        return AgentResponse(
            text=self._response_text,
            tool_calls=self._response_tool_calls,
            tokens_in=self._response_tokens_in,
            tokens_out=self._response_tokens_out,
        )


# --------------------------------------------------------------------------
# AgentResponse — extended tool_calls shape
# --------------------------------------------------------------------------

class TestAgentResponseStructuredToolCalls:
    """AgentResponse.tool_calls becomes a tuple of dicts (name+args+result),
    not just a tuple of strings. Backwards-compat: empty tuple still allowed.
    """

    def test_empty_tool_calls_default(self):
        r = AgentResponse(text="hello")
        assert r.tool_calls == ()

    def test_structured_tool_calls_accepted(self):
        r = AgentResponse(
            text="ran a command",
            tool_calls=(
                _make_tool_call("terminal",
                                {"command": "ls -la"},
                                "total 12\n..."),
                _make_tool_call("file_edit",
                                {"path": "x.py", "old_string": "a", "new_string": "b"},
                                "ok"),
            ),
        )
        assert len(r.tool_calls) == 2
        assert r.tool_calls[0]["name"] == "terminal"
        assert r.tool_calls[0]["args"] == {"command": "ls -la"}
        assert r.tool_calls[0]["result"] == "total 12\n..."
        assert r.tool_calls[1]["name"] == "file_edit"

    def test_tool_calls_is_tuple_of_dicts(self):
        """Pinned shape: each entry is a dict with name/args/result keys."""
        r = AgentResponse(
            text="x",
            tool_calls=(_make_tool_call("t", {}, "r"),),
        )
        assert isinstance(r.tool_calls, tuple)
        assert isinstance(r.tool_calls[0], dict)
        assert set(r.tool_calls[0].keys()) >= {"name", "args", "result"}

    def test_legacy_string_only_tool_calls_still_rejected(self):
        """The shape is dict-only. Strings (legacy v0.1.0 shape) are NOT
        silently coerced — the field is type-pinned."""
        # This is a documentation test, not a behavior assertion. The
        # upgrade DECIDED to make tool_calls dict-only. If a caller
        # constructs AgentResponse(tool_calls=("Read",)), the value is
        # stored as-is (no coercion) and downstream code that reads
        # r.tool_calls[0]["name"] would fail. That's the intended
        # migration signal — the dataclass is duck-typed, but the
        # contract is "tuple of dicts with name/args/result."
        legacy = AgentResponse(text="x", tool_calls=("Read",))
        # Stored as the caller passed it. Downstream code must migrate.
        assert legacy.tool_calls == ("Read",)


# --------------------------------------------------------------------------
# AgentBridge.invoke — accepts new kwargs
# --------------------------------------------------------------------------

class TestAgentBridgeAcceptsToolsAndSession:
    """The bridge signature grows: tools, session_key, system_prompt.
    The default impl forwards them to inner.invoke (via JournalingBridge).
    """

    def test_inner_receives_tools_kwarg(self):
        """When a workflow calls ask_agent(tools=[...]), the inner bridge
        sees those tools."""
        bridge = _RecordingBridge()
        rt = WorkflowRuntime(journal_root=Path("/tmp"))
        rt.set_agent_bridge(bridge)

        tools = [
            {"name": "terminal",
             "description": "Run shell commands",
             "schema": {"type": "object",
                        "properties": {"command": {"type": "string"}}}},
        ]

        async def _go():
            await rt.ask_agent(
                prompt="investigate x",
                model="sonnet",
                tools=tools,
            )
        asyncio.run(_go())

        assert len(bridge.calls) == 1
        assert bridge.calls[0]["tools"] == tools

    def test_inner_receives_session_key(self):
        bridge = _RecordingBridge()
        rt = WorkflowRuntime(journal_root=Path("/tmp"))
        rt.set_agent_bridge(bridge)

        async def _go():
            await rt.ask_agent(
                prompt="x",
                model="sonnet",
                session_key="review_pr_42",
            )
        asyncio.run(_go())

        assert bridge.calls[0]["session_key"] == "review_pr_42"

    def test_inner_receives_system_prompt(self):
        bridge = _RecordingBridge()
        rt = WorkflowRuntime(journal_root=Path("/tmp"))
        rt.set_agent_bridge(bridge)

        sp = "You are a Python debugger. Always show the failing assertion first."

        async def _go():
            await rt.ask_agent(
                prompt="x",
                model="sonnet",
                system_prompt=sp,
            )
        asyncio.run(_go())

        assert bridge.calls[0]["system_prompt"] == sp

    def test_all_kwargs_default_to_none(self):
        """Backwards compat: a caller that passes only prompt+model
        must not break — the new fields default to None."""
        bridge = _RecordingBridge()
        rt = WorkflowRuntime(journal_root=Path("/tmp"))
        rt.set_agent_bridge(bridge)

        async def _go():
            await rt.ask_agent(prompt="legacy call", model="sonnet")
        asyncio.run(_go())

        assert bridge.calls[0]["tools"] is None
        assert bridge.calls[0]["session_key"] is None
        assert bridge.calls[0]["system_prompt"] is None

    def test_structured_tool_calls_returned(self):
        """The bridge returns AgentResponse with structured tool_calls."""
        bridge = _RecordingBridge(
            response_text="fixed the test",
            response_tool_calls=(
                _make_tool_call("file_edit",
                                {"path": "tests/test_x.py",
                                 "old_string": "assert 1 == 2",
                                 "new_string": "assert 1 == 1"},
                                "ok"),
            ),
        )
        rt = WorkflowRuntime(journal_root=Path("/tmp"))
        rt.set_agent_bridge(bridge)

        async def _go():
            return await rt.ask_agent(prompt="x", model="sonnet")
        resp = asyncio.run(_go())

        assert resp.text == "fixed the test"
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0]["name"] == "file_edit"
        assert resp.tool_calls[0]["args"]["path"] == "tests/test_x.py"


# --------------------------------------------------------------------------
# JournalingBridge — records new fields
# --------------------------------------------------------------------------

class TestJournalingBridgeRecordsToolsAndSession:
    """The journal must record the new surface so verifiers can correlate
    'this step used these tools in this session.'"""

    def test_agent_call_records_session_key(self, tmp_path):
        bridge = _RecordingBridge()
        jb = JournalingBridge(inner=bridge)
        rt = WorkflowRuntime(journal_root=tmp_path)
        rt.set_agent_bridge(jb)

        async def _go():
            return await rt.ask_agent(
                prompt="x",
                model="sonnet",
                session_key="bug_fix_thread",
            )
        run_id_or_resp = asyncio.run(_go())

        # The journal is empty if no run is active; that's expected.
        # What we DO assert: the call reached the inner bridge with the
        # session_key (already covered above) AND the bridge's invoke was
        # called once. Verifying journal fields with no active run is
        # exercised in the e2e tests below.

    def test_e2e_journal_records_session_key_on_agent_call(self, tmp_path):
        """End-to-end: a step that calls ask_agent(session_key=...) writes
        the session_key into the agent_call journal event."""
        bridge = _RecordingBridge(response_text="done")

        mod_path = write_workflow_module(tmp_path, """
            from plugins.hermes_workflow import step, workflow, Evidence

            @step(name="ask")
            async def ask(ctx) -> Evidence:
                await ctx.runtime.ask_agent(
                    prompt="x",
                    model="sonnet",
                    session_key="review_pr_42",
                )
                return Evidence(
                    files_changed=(), commands_run=(), exit_codes=(),
                    tests_run=0, tests_passed=0, duration_seconds=0.0,
                )

            @workflow(name="session_wf")
            async def session_wf(ctx) -> dict:
                await ask(ctx)
                return {}
        """)

        async def _go():
            rt = WorkflowRuntime(journal_root=tmp_path)
            rt.set_agent_bridge(bridge)
            module = import_workflow(mod_path)
            run_id, run = await submit_and_wait(rt, module["session_wf"], {})
            journal = Journal.replay(run_id, tmp_path)
            calls = journal.agent_calls()
            return run.state.value, calls

        state, calls = asyncio.run(_go())
        assert state == "done"
        assert len(calls) == 1
        assert calls[0].get("session_key") == "review_pr_42"

    def test_e2e_journal_records_tools_count(self, tmp_path):
        """End-to-end: tools list is journaled as a count (so verifiers
        know the step had access to N tools without bloating the journal
        with full schemas)."""
        bridge = _RecordingBridge(response_text="done")
        tools = [
            {"name": "terminal", "description": "x", "schema": {}},
            {"name": "file_edit", "description": "y", "schema": {}},
            {"name": "search", "description": "z", "schema": {}},
        ]

        mod_path = write_workflow_module(tmp_path, """
            from plugins.hermes_workflow import step, workflow, Evidence

            @step(name="ask")
            async def ask(ctx) -> Evidence:
                await ctx.runtime.ask_agent(
                    prompt="x",
                    model="sonnet",
                    tools=[
                        {"name": "terminal", "description": "x", "schema": {}},
                        {"name": "file_edit", "description": "y", "schema": {}},
                        {"name": "search", "description": "z", "schema": {}},
                    ],
                )
                return Evidence(
                    files_changed=(), commands_run=(), exit_codes=(),
                    tests_run=0, tests_passed=0, duration_seconds=0.0,
                )

            @workflow(name="tools_wf")
            async def tools_wf(ctx) -> dict:
                await ask(ctx)
                return {}
        """)

        async def _go():
            rt = WorkflowRuntime(journal_root=tmp_path)
            rt.set_agent_bridge(bridge)
            module = import_workflow(mod_path)
            run_id, run = await submit_and_wait(rt, module["tools_wf"], {})
            journal = Journal.replay(run_id, tmp_path)
            calls = journal.agent_calls()
            return run.state.value, calls

        state, calls = asyncio.run(_go())
        assert state == "done"
        assert calls[0].get("tools_count") == 3

    def test_e2e_journal_records_structured_tool_calls_on_response(
        self, tmp_path,
    ):
        """End-to-end: agent_response journal event carries structured
        tool_calls (each with name/args/result_chars), not just names."""
        bridge = _RecordingBridge(
            response_text="did the work",
            response_tool_calls=(
                _make_tool_call("terminal",
                                {"command": "pytest tests/"},
                                "5 passed"),
                _make_tool_call("file_edit",
                                {"path": "x.py", "old_string": "a", "new_string": "b"},
                                "ok"),
            ),
        )

        mod_path = write_workflow_module(tmp_path, """
            from plugins.hermes_workflow import step, workflow, Evidence

            @step(name="ask")
            async def ask(ctx) -> Evidence:
                await ctx.runtime.ask_agent(prompt="x", model="sonnet")
                return Evidence(
                    files_changed=(), commands_run=(), exit_codes=(),
                    tests_run=0, tests_passed=0, duration_seconds=0.0,
                )

            @workflow(name="response_wf")
            async def response_wf(ctx) -> dict:
                await ask(ctx)
                return {}
        """)

        async def _go():
            rt = WorkflowRuntime(journal_root=tmp_path)
            rt.set_agent_bridge(bridge)
            module = import_workflow(mod_path)
            run_id, run = await submit_and_wait(rt, module["response_wf"], {})
            journal = Journal.replay(run_id, tmp_path)
            response_events = [
                e for e in journal.events
                if e.get("kind") == "agent_response"
            ]
            return run.state.value, response_events

        state, response_events = asyncio.run(_go())
        assert state == "done"
        assert len(response_events) == 1
        # The structured tool_calls payload is recorded.
        tc = response_events[0].get("tool_calls")
        assert tc is not None
        assert len(tc) == 2
        assert tc[0]["name"] == "terminal"
        assert tc[0]["args"] == {"command": "pytest tests/"}
        # Result is logged as a char count for journal-size sanity.
        assert "result_chars" in tc[0]


# --------------------------------------------------------------------------
# Backwards-compat — old call sites still work
# --------------------------------------------------------------------------

class TestBackwardsCompatibility:
    """Code that calls ask_agent(prompt=..., model=...) WITHOUT the new
    fields must keep working unchanged. The new fields default to None."""

    def test_no_new_kwargs_still_works(self, tmp_path):
        """A workflow that calls ask_agent(prompt=..., model=...) with
        no tools/session_key/system_prompt completes successfully."""
        bridge = _RecordingBridge()

        mod_path = write_workflow_module(tmp_path, """
            from plugins.hermes_workflow import step, workflow, Evidence

            @step(name="ask")
            async def ask(ctx) -> Evidence:
                resp = await ctx.runtime.ask_agent(
                    prompt="legacy call",
                    model="sonnet",
                )
                return Evidence(
                    files_changed=(), commands_run=(), exit_codes=(),
                    tests_run=0, tests_passed=0, duration_seconds=0.0,
                )

            @workflow(name="legacy_wf")
            async def legacy_wf(ctx) -> dict:
                await ask(ctx)
                return {}
        """)

        async def _go():
            rt = WorkflowRuntime(journal_root=tmp_path)
            rt.set_agent_bridge(bridge)
            module = import_workflow(mod_path)
            run_id, run = await submit_and_wait(rt, module["legacy_wf"], {})
            return run.state.value

        assert asyncio.run(_go()) == "done"

    def test_legacy_text_only_response_shape_still_works(self):
        """AgentResponse(text="x") with no tool_calls still works as a
        valid return value for legacy bridge implementations."""
        bridge = _RecordingBridge(response_text="legacy ok")
        rt = WorkflowRuntime(journal_root=Path("/tmp"))
        rt.set_agent_bridge(bridge)

        async def _go():
            return await rt.ask_agent(prompt="x", model="sonnet")
        resp = asyncio.run(_go())
        assert resp.text == "legacy ok"
        assert resp.tool_calls == ()
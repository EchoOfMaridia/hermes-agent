"""E2E tests for examples/agent_review.py — the tool-using + threaded workflow.

Pins the contract demonstrated in the example:
1. The workflow loads and the AST validator accepts it.
2. With a stub agent bridge, all three steps run to completion.
3. The journal records the new fields per Path A+B:
   - ``tools_count >= 3`` on each ``agent_call`` event
   - ``session_key`` matching the run_id-derived key on every step
   - Structured ``tool_calls`` (name/args/result_chars) on every
     ``agent_response`` event
4. The verifiers on investigate and apply_fix fire and accept when the
   stub bridge records the right tool_calls.
5. When the stub bridge DOESN'T call the expected tool, the verifier
   rejects and the step fails (this is the load-bearing assertion that
   the new surface is wired correctly — a workflow that says "use
   terminal" but doesn't get one should fail loudly).

The companion test ``test_step5b_agent_bridge_tools.py`` covers the
bridge surface in isolation. This file covers the same surface through
a real workflow that uses it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from plugins.hermes_workflow.agent_bridge import (
    AgentBridge,
    AgentResponse,
)
from plugins.hermes_workflow.examples import agent_review
from plugins.hermes_workflow.journal import Journal
from plugins.hermes_workflow.runtime import WorkflowRuntime

from plugins.hermes_workflow.tests._runtime_helpers import (
    import_workflow,
    submit_and_wait,
    submit_and_drain,
)


# ---------------------------------------------------------------------------
# Stub agent bridge — emits a configurable tool-call sequence per step.
# ---------------------------------------------------------------------------

class _StepRecordingBridge(AgentBridge):
    """Stub bridge that returns different tool_calls depending on which
    step the workflow is currently in. Records every call so tests can
    assert on what the runtime forwarded."""

    def __init__(self, *, behavior: str = "happy") -> None:
        self.calls: list[dict] = []
        self._behavior = behavior

    async def invoke(self, *, prompt, model, max_tokens,
                     tools=None, session_key=None, system_prompt=None,
                     json_schema=None, schema_name=None):
        self.calls.append({
            "prompt": prompt,
            "model": model,
            "max_tokens": max_tokens,
            "tools": tools,
            "session_key": session_key,
            "system_prompt": system_prompt,
        })

        # Determine which step we're in by sniffing the prompt + system_prompt.
        # The three step prompts are distinct enough that this works.
        step = _classify_step(prompt, system_prompt)

        if self._behavior == "happy":
            tool_calls = _happy_tool_calls_for(step)
        elif self._behavior == "missing_terminal":
            tool_calls = _missing_terminal_tool_calls_for(step)
        elif self._behavior == "missing_edit":
            tool_calls = _missing_edit_tool_calls_for(step)
        else:
            raise AssertionError(f"unknown behavior: {self._behavior}")

        return AgentResponse(
            text=f"step={step} model={model}",
            tool_calls=tool_calls,
            tokens_in=100, tokens_out=50,
        )


def _classify_step(prompt: str, system_prompt: str | None) -> str:
    """Map a prompt+system_prompt pair to one of the three workflow steps.
    Cheap regex over the strings — the example workflows use distinct
    enough language that this is unambiguous."""
    sp = (system_prompt or "").lower()
    if "investigator" in sp or "diagnose" in prompt.lower():
        return "investigate"
    if "reviewer" in sp or "minimal correct fix" in prompt.lower():
        return "plan_fix"
    if "implementer" in sp or "apply the planned fix" in prompt.lower():
        return "apply_fix"
    return "unknown"


def _happy_tool_calls_for(step: str) -> tuple[dict, ...]:
    """Returns the tool_calls a successful agent run would record for
    the given step. Matches the example workflow's expectations."""
    if step == "investigate":
        return (
            {"name": "terminal", "args": {"command": "python -m pytest tests/ -v"},
             "result": "1 failed, 4 passed"},
        )
    if step == "plan_fix":
        return ()  # plan_fix only reasons; doesn't call tools
    if step == "apply_fix":
        return (
            {"name": "file_edit",
             "args": {"path": "tests/test_x.py", "old_string": "a == 2",
                      "new_string": "a == 1"},
             "result": "ok"},
            {"name": "terminal",
             "args": {"command": "python -m pytest tests/test_x.py"},
             "result": "1 passed"},
        )
    return ()


def _missing_terminal_tool_calls_for(step: str) -> tuple[dict, ...]:
    """apply_fix never calls terminal — its verifier should reject."""
    if step == "apply_fix":
        return (
            {"name": "file_edit",
             "args": {"path": "tests/test_x.py", "old_string": "a == 2",
                      "new_string": "a == 1"},
             "result": "ok"},
        )
    return _happy_tool_calls_for(step)


def _missing_edit_tool_calls_for(step: str) -> tuple[dict, ...]:
    """apply_fix never calls file_edit — its verifier should reject."""
    if step == "apply_fix":
        return (
            {"name": "terminal",
             "args": {"command": "echo 'no edit applied'"},
             "result": "no edit applied"},
        )
    return _happy_tool_calls_for(step)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestAgentReviewExampleShape:
    """The example workflow is importable, valid, and declares the
    expected surface."""

    def test_module_imports_cleanly(self):
        # Importing the example should not raise.
        assert hasattr(agent_review, "run")
        assert hasattr(agent_review, "investigate")
        assert hasattr(agent_review, "plan_fix")
        assert hasattr(agent_review, "apply_fix")

    def test_module_uses_investigative_tools_constant(self):
        """The example declares a module-level _INVESTIGATIVE_TOOLS tuple
        with terminal/file_edit/search — proves the example demonstrates
        the full tool surface."""
        tools = agent_review._INVESTIGATIVE_TOOLS
        tool_names = {t["name"] for t in tools}
        assert "terminal" in tool_names
        assert "file_edit" in tool_names
        assert "search" in tool_names
        # Each tool has a JSON Schema with required fields.
        for tool in tools:
            assert "schema" in tool
            assert tool["schema"].get("type") == "object"
            assert "properties" in tool["schema"]
            assert "required" in tool["schema"]


class TestAgentReviewHappyPath:
    """End-to-end: stub bridge with happy-path tool_calls drives the
    workflow to completion and journal records the new surface."""

    def test_workflow_completes_with_happy_stub(self, tmp_path):
        bridge = _StepRecordingBridge(behavior="happy")
        rt = WorkflowRuntime(journal_root=tmp_path)
        rt.set_agent_bridge(bridge)

        async def _go():
            workflow_fn = agent_review.run
            run_id, run = await submit_and_wait(
                rt, workflow_fn, {"failing_test": "tests/test_smoke.py"},
            )
            journal = Journal.replay(run_id, tmp_path)
            return (
                run.state.value,
                [e for e in journal.events if e["kind"] == "agent_call"],
                [e for e in journal.events if e["kind"] == "agent_response"],
            )

        state, call_events, response_events = asyncio.run(_go())
        assert state == "done", (
            f"expected workflow to complete; got state={state}, "
            f"events={[e.get('kind') for e in response_events]}"
        )

    def test_journal_records_tools_count_per_call(self, tmp_path):
        """Every agent_call event records tools_count >= 3 (terminal +
        file_edit + search)."""
        bridge = _StepRecordingBridge(behavior="happy")
        rt = WorkflowRuntime(journal_root=tmp_path)
        rt.set_agent_bridge(bridge)

        async def _go():
            workflow_fn = agent_review.run
            run_id, _ = await submit_and_wait(
                rt, workflow_fn, {"failing_test": "tests/test_smoke.py"},
            )
            return Journal.replay(run_id, tmp_path).events

        events = asyncio.run(_go())
        call_events = [e for e in events if e["kind"] == "agent_call"]
        # Three steps = three agent_call events.
        assert len(call_events) == 3
        for ev in call_events:
            assert ev["tools_count"] >= 3, (
                f"step={ev['step']} recorded tools_count={ev['tools_count']}; "
                f"expected >= 3 (terminal + file_edit + search)"
            )

    def test_journal_records_session_key_consistent_across_steps(self, tmp_path):
        """All three steps use the same session_key (run_id-derived)."""
        bridge = _StepRecordingBridge(behavior="happy")
        rt = WorkflowRuntime(journal_root=tmp_path)
        rt.set_agent_bridge(bridge)

        async def _go():
            workflow_fn = agent_review.run
            run_id, _ = await submit_and_wait(
                rt, workflow_fn, {"failing_test": "tests/test_smoke.py"},
            )
            return run_id, Journal.replay(run_id, tmp_path).events

        run_id, events = asyncio.run(_go())
        call_events = [e for e in events if e["kind"] == "agent_call"]
        session_keys = {ev["session_key"] for ev in call_events}
        # All three steps share exactly one session_key.
        assert len(session_keys) == 1
        (sk,) = session_keys
        # The session_key embeds the run_id (agent_review_<run_id>).
        assert sk == f"agent_review_{run_id}"

    def test_journal_records_structured_tool_calls_on_responses(self, tmp_path):
        """agent_response events carry structured tool_calls with
        name/args/result_chars — the new Path A contract."""
        bridge = _StepRecordingBridge(behavior="happy")
        rt = WorkflowRuntime(journal_root=tmp_path)
        rt.set_agent_bridge(bridge)

        async def _go():
            workflow_fn = agent_review.run
            run_id, _ = await submit_and_wait(
                rt, workflow_fn, {"failing_test": "tests/test_smoke.py"},
            )
            return Journal.replay(run_id, tmp_path).events

        events = asyncio.run(_go())
        response_events = [e for e in events if e["kind"] == "agent_response"]
        assert len(response_events) == 3
        # The investigate and apply_fix steps emit tool calls; plan_fix
        # is pure reasoning. We assert AT LEAST ONE structured call
        # exists with the new shape.
        any_structured = False
        for ev in response_events:
            for tc in ev.get("tool_calls") or ():
                assert isinstance(tc, dict), (
                    f"tool_calls should be tuple[dict, ...]; got {type(tc)}"
                )
                assert "name" in tc
                assert "args" in tc
                assert "result_chars" in tc
                any_structured = True
        assert any_structured, "no structured tool_calls were journaled"


class TestAgentReviewVerifiers:
    """The verifiers on investigate and apply_fix fire and reject when
    the agent doesn't use the right tool. This is the load-bearing
    behavior change — workflows that say 'use terminal' but don't get
    one should fail loudly."""

    def test_investigate_verifier_rejects_when_no_terminal(self, tmp_path):
        """If the investigate step doesn't call terminal, the verifier
        raises VerifierMismatch and the workflow fails — the journal
        records exactly which step was rejected and why."""
        # We override the happy-path for investigate specifically.
        class _NoTerminalInInvestigate(_StepRecordingBridge):
            async def invoke(self, *, prompt, model, max_tokens,
                             tools=None, session_key=None, system_prompt=None,
                     json_schema=None, schema_name=None):
                step = _classify_step(prompt, system_prompt)
                # Don't override — let the parent's logic handle it,
                # but force investigate to return no tool_calls.
                if step == "investigate":
                    return AgentResponse(
                        text="investigated without terminal",
                        tool_calls=(),
                    )
                return await super().invoke(
                    prompt=prompt, model=model, max_tokens=max_tokens,
                    tools=tools, session_key=session_key,
                    system_prompt=system_prompt,
                )

        bridge = _NoTerminalInInvestigate(behavior="happy")
        rt = WorkflowRuntime(journal_root=tmp_path)
        rt.set_agent_bridge(bridge)

        async def _go():
            workflow_fn = agent_review.run
            run_id, run, err = await submit_and_drain(
                rt, workflow_fn, {"failing_test": "tests/test_smoke.py"},
            )
            return (
                run.state.value,
                type(err).__name__ if err else None,
                str(err) if err else None,
                [e for e in Journal.replay(run_id, tmp_path).events
                 if e["kind"] == "agent_response"],
            )

        state, err_type, err_msg, response_events = asyncio.run(_go())
        # Verifier rejection is fatal — the runtime raises
        # VerifierMismatch which propagates through the workflow.
        assert err_type == "VerifierMismatch", (
            f"expected VerifierMismatch from rejected verifier; got {err_type}: {err_msg}"
        )
        assert "investigate" in (err_msg or "")
        # The investigate response event shows ZERO tool_calls (the
        # bridge stubbed it), which is what the verifier rejected on.
        investigate_responses = [
            e for e in response_events if e.get("step") == "investigate"
        ]
        assert len(investigate_responses) == 1
        assert investigate_responses[0].get("tool_calls") == []

    def test_apply_fix_verifier_rejects_when_no_edit(self, tmp_path):
        """If apply_fix doesn't call file_edit, the verifier raises
        VerifierMismatch and the workflow fails."""
        bridge = _StepRecordingBridge(behavior="missing_edit")
        rt = WorkflowRuntime(journal_root=tmp_path)
        rt.set_agent_bridge(bridge)

        async def _go():
            workflow_fn = agent_review.run
            run_id, run, err = await submit_and_drain(
                rt, workflow_fn, {"failing_test": "tests/test_smoke.py"},
            )
            return (
                run.state.value,
                type(err).__name__ if err else None,
                str(err) if err else None,
                [e for e in Journal.replay(run_id, tmp_path).events
                 if e["kind"] == "agent_response"],
            )

        state, err_type, err_msg, response_events = asyncio.run(_go())
        assert err_type == "VerifierMismatch", (
            f"expected VerifierMismatch; got {err_type}: {err_msg}"
        )
        assert "apply_fix" in (err_msg or "")
        # The apply_fix response event shows ZERO file_edit calls —
        # which is what the verifier rejected on.
        apply_fix_responses = [
            e for e in response_events if e.get("step") == "apply_fix"
        ]
        assert len(apply_fix_responses) == 1
        edit_calls = [
            tc for tc in apply_fix_responses[0].get("tool_calls") or ()
            if tc.get("name") == "file_edit"
        ]
        assert edit_calls == [], (
            f"missing_edit behavior should produce no file_edit calls; "
            f"got {edit_calls}"
        )


class TestSystemInstructionsDescribeNewSurface:
    """The _SYSTEM_INSTRUCTIONS in script_author.py document the full
    new agent-call surface, so future LLM-generated workflows know about
    tools=/session_key=/system_prompt=."""

    def test_system_instructions_mention_tools_kwarg(self):
        from plugins.hermes_workflow.script_author import _SYSTEM_INSTRUCTIONS
        assert "tools=" in _SYSTEM_INSTRUCTIONS or "tools=[" in _SYSTEM_INSTRUCTIONS, (
            "_SYSTEM_INSTRUCTIONS should document the tools= kwarg so "
            "LLM-generated workflows can give the agent tools to use."
        )

    def test_system_instructions_mention_session_key(self):
        from plugins.hermes_workflow.script_author import _SYSTEM_INSTRUCTIONS
        assert "session_key" in _SYSTEM_INSTRUCTIONS, (
            "_SYSTEM_INSTRUCTIONS should document the session_key= kwarg "
            "so LLM-generated workflows can thread multi-step conversations."
        )

    def test_system_instructions_mention_system_prompt(self):
        from plugins.hermes_workflow.script_author import _SYSTEM_INSTRUCTIONS
        assert "system_prompt" in _SYSTEM_INSTRUCTIONS, (
            "_SYSTEM_INSTRUCTIONS should document the system_prompt= "
            "kwarg so LLM-generated workflows can scope the agent's role."
        )

    def test_system_instructions_show_structured_tool_call_shape(self):
        """The example in the instructions should show the dict shape
        (name/args/result), not the legacy tuple-of-strings shape."""
        from plugins.hermes_workflow.script_author import _SYSTEM_INSTRUCTIONS
        assert '"name"' in _SYSTEM_INSTRUCTIONS
        assert '"args"' in _SYSTEM_INSTRUCTIONS
        assert '"result"' in _SYSTEM_INSTRUCTIONS
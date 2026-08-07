"""test_rest_run_live_progress — verifies that a run started via
WorkflowRuntime.submit() (the same code path the REST endpoint uses)
flows through the desktop_event_bridge and produces the wire-format
JSON-RPC envelope the desktop renderer's $workflowRuns store expects.

This is the load-bearing integration test for "REST-started run shows
up live in the WorkflowsView panel."

Per Pitfall #54 — 4-path stub harness. We replace the LLM bridge with
a stub, drive a real workflow through the runtime, and assert the
emitted envelopes match the desktop wire contract.

Compatible with the project's pytest-asyncio config (mode=Mode.STRICT)
— each async test is decorated with ``@pytest.mark.asyncio`` so
pytest-asyncio schedules it on its own loop.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

import pytest


REPO_ROOT = Path("/home/cage/Desktop/Workspaces/HermesDesktop")
sys.path.insert(0, str(REPO_ROOT))


from plugins.hermes_workflow.agent_bridge import (  # noqa: E402
    AgentBridge, AgentResponse,
)
from plugins.hermes_workflow.runtime import WorkflowRuntime  # noqa: E402
from plugins.hermes_workflow.desktop_event_bridge import (  # noqa: E402
    DesktopWorkflowEventBridge, get_bridge, reset_for_tests,
)


class _StubBridge(AgentBridge):
    """Stub agent bridge: returns a single happy ReviewVerdict."""
    async def invoke(self, *, prompt, model, max_tokens=None, tools=None,
                    session_key=None, system_prompt=None,
                    json_schema=None, schema_name=None, **_unused):
        sn = schema_name
        if sn == "ReviewVerdict":
            parsed = {
                "verdict": "PASS",
                "indirection_verdicts": [],
                "false_positives": [],
                "missed_members": [],
                "summary": "stub",
            }
        else:
            parsed = {
                "headline": {
                    "module_count": 0, "member_total": 0,
                    "symbol_coverage_pct": 0.0, "impl_coverage_pct": 0.0,
                    "review_verdict": "PASS",
                },
                "per_module": [], "per_class": [], "narrative": "stub",
            }
        return AgentResponse(
            text=json.dumps(parsed), parsed=parsed,
            content_type="json", tokens_in=1, tokens_out=1, duration=0.01,
        )


# A minimal workflow body — no LLM calls until agent_review, which the
# stub returns instantly. Lets us assert the event sequence end-to-end.
WORKFLOW_SOURCE = '''
from plugins.hermes_workflow import step, workflow, Evidence


@step(name="alpha")
async def alpha(ctx):
    return Evidence(files_changed=(), commands_run=(), exit_codes=(),
                    tests_run=0, tests_passed=0, duration_seconds=0.0)


@step(name="beta", depends_on=("alpha",))
async def beta(ctx):
    return Evidence(files_changed=(), commands_run=(), exit_codes=(),
                    tests_run=0, tests_passed=0, duration_seconds=0.0)


@workflow(name="event_chain_smoke")
async def run(ctx):
    await alpha(ctx)
    await beta(ctx)
'''


def _emit_recorder():
    """Stand-in for tui_gateway.server._emit — records every call."""
    calls: list[tuple[str, str, dict]] = []

    def _emit(event_type, session_id, payload):
        calls.append((event_type, session_id, payload))

    return calls, _emit


@pytest.mark.asyncio
async def test_workflow_run_emits_wire_format_envelopes():
    """A run started via the runtime emits desktop wire-format
    workflow_run_started / workflow_step_started / workflow_run_completed
    envelopes that match the renderer's contract."""
    reset_for_tests()
    recorder_calls, _emit = _emit_recorder()

    # Build a workflow module on disk and load it as a callable.
    with tempfile.TemporaryDirectory(prefix="wf-rest-test-") as tmp:
        ws = Path(tmp) / "ws"
        jr = Path(tmp) / "jr"
        jr.mkdir()
        ws.mkdir()
        (ws / "wf.py").write_text(WORKFLOW_SOURCE)
        import importlib.util
        spec = importlib.util.spec_from_file_location("_wf_under_test", ws / "wf.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)

        rt = WorkflowRuntime(journal_root=jr, default_max_concurrent=2)
        rt.set_agent_bridge(_StubBridge())

        # Wire the desktop bridge as a chained dispatcher
        bridge = get_bridge()
        bridge.register(rt)
        bridge.set_emit_fn_for_tests(_emit)

        # Start the run (same path the REST endpoint takes)
        rid = await rt.submit(mod.run, inputs={}, workspace=ws)
        run = rt.get_run(rid)
        await run.task  # wait for completion

        # Assert the recorded envelopes include the run-level wire kinds
        # the renderer's $workflowRuns reducer expects. Per the desktop
        # contract (use-workflow-events.ts), step-level progress arrives
        # via the existing tool.start / tool.complete events from the
        # gateway, not as workflow_step_* notices. So we only assert
        # the run-level envelopes here.
        kinds = [c[0] for c in recorder_calls]
        assert "workflow_run_started" in kinds, (
            f"missing workflow_run_started; got {kinds}"
        )
        assert "workflow_run_completed" in kinds, (
            f"missing workflow_run_completed; got {kinds}"
        )

        # The desktop contract: every workflow_* payload is a dict with
        # kind matching the wire event kind
        for event_type, _session_id, payload in recorder_calls:
            if event_type.startswith("workflow_"):
                assert payload.get("kind") == event_type, (
                    f"event {event_type} payload.kind={payload.get('kind')!r} mismatch"
                )
                if event_type == "workflow_run_started":
                    assert payload.get("run_id") == rid
                    assert payload.get("workflow") == "event_chain_smoke"
                if event_type == "workflow_run_completed":
                    assert payload.get("run_id") == rid

        print(
            f"[OK] workflow_run_emits_wire_format_envelopes — "
            f"emitted {len(recorder_calls)} envelopes: {dict((k, kinds.count(k)) for k in set(kinds))}"
        )


@pytest.mark.asyncio
async def test_bridge_chains_dispatcher():
    """Confirm the bridge wraps the runtime's existing dispatcher and
    BOTH the existing dispatcher AND the bridge get called for each event."""
    reset_for_tests()
    recorder_calls, _emit = _emit_recorder()
    existing_calls: list = []

    class _FakeRuntime:
        def __init__(self):
            self._dispatcher = None
            self.existing_calls = existing_calls

        def set_dispatcher(self, fn):
            self._dispatcher = fn

    rt = _FakeRuntime()
    rt.set_dispatcher(lambda ev: existing_calls.append(ev))
    bridge = get_bridge()
    bridge.register(rt)
    bridge.set_emit_fn_for_tests(_emit)

    # Drive one workflow event
    from gateway.stream_events import GatewayNotice
    event = GatewayNotice(
        kind="workflow_run_started",
        text="r_x started",
        extra={"run_id": "r_x", "workflow": "test"},
    )
    rt._dispatcher(event)

    assert len(existing_calls) == 1, "existing dispatcher not called"
    assert len(recorder_calls) == 1, "bridge did not emit"
    assert recorder_calls[0][0] == "workflow_run_started"
    print("[OK] bridge_chains_dispatcher")


if __name__ == "__main__":
    # Manual runner — pytest-asyncio is the primary runner in CI; this
    # block lets a developer run the file directly for a quick local
    # smoke (`python3 test_rest_run_live_progress.py`).
    failed = 0
    for fn in [
        test_workflow_run_emits_wire_format_envelopes,
        test_bridge_chains_dispatcher,
    ]:
        try:
            asyncio.run(fn())
        except AssertionError as exc:
            failed += 1
            print(f"[FAIL] {fn.__name__}: {exc}")
        except Exception as exc:
            import traceback
            failed += 1
            print(f"[FAIL] {fn.__name__}: {type(exc).__name__}: {exc}")
            traceback.print_exc()
    if failed:
        print(f"\n[FAIL] {failed} tests failed")
        sys.exit(1)
    print("\n[OK] all live-progress hookup tests passed")
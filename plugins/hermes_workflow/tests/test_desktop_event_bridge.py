"""test_desktop_event_bridge — Stub-bridge harness for
plugins/hermes_workflow/desktop_event_bridge.py.

Per Pitfall #54 (2026-07-17, shipped) — every plugin feature needs a
deterministic harness that pins the wire contract before any real
WebSocket is involved.

This test exercises ``_translate_to_rpc_envelope`` against every event
kind the desktop hook knows about
(``apps/desktop/src/types/hermes.ts:WorkflowRunStartedPayload +
WorkflowRunFinishedPayload``). It also exercises the full bridge
dispatch path with a stubbed ``tui_gateway.server._emit`` so we can
assert the JSON-RPC envelope actually reaches the emit function.

Per the hermes-workflow-author skill note: runs with plain ``python3``,
not pytest. Prints [OK]/[FAIL] per path.

Usage:
    python3 /home/cage/Desktop/Workspaces/HermesDesktop/plugins/hermes_workflow/tests/test_desktop_event_bridge.py
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path


REPO_ROOT = Path("/home/cage/Desktop/Workspaces/HermesDesktop")
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from gateway.stream_events import GatewayNotice  # noqa: E402

from plugins.hermes_workflow.desktop_event_bridge import (  # noqa: E402
    DesktopWorkflowEventBridge,
    _translate_to_rpc_envelope,
    get_bridge,
    reset_for_tests,
)


# ===========================================================================
# Tests — translation layer (no bridge, no emit)
# ===========================================================================

def test_translate_run_started() -> bool:
    event = GatewayNotice(
        kind="workflow_run_started",
        text="run r_abc started",
        extra={
            "run_id": "r_abc",
            "workflow": "tpipe_abi_module_audit_structured",
            "max_concurrent": 4,
            "max_total": 1000,
        },
    )
    env = _translate_to_rpc_envelope(event)
    if env is None:
        print("[FAIL] translate_run_started — returned None")
        return False
    if env.get("jsonrpc") != "2.0":
        print(f"[FAIL] translate_run_started — bad jsonrpc: {env.get('jsonrpc')}")
        return False
    if env.get("method") != "event":
        print(f"[FAIL] translate_run_started — bad method: {env.get('method')}")
        return False
    params = env["params"]
    if params.get("type") != "workflow_run_started":
        print(f"[FAIL] translate_run_started — bad type: {params.get('type')}")
        return False
    payload = params["payload"]
    if payload.get("kind") != "workflow_run_started":
        print(f"[FAIL] translate_run_started — payload.kind wrong: {payload.get('kind')}")
        return False
    for key in ("run_id", "workflow", "max_concurrent", "max_total"):
        if key not in payload:
            print(f"[FAIL] translate_run_started — missing payload.{key}")
            return False
    print("[OK] translate_run_started")
    return True


def test_translate_run_completed() -> bool:
    event = GatewayNotice(
        kind="workflow_run_completed",
        text="run r_abc done",
        extra={"run_id": "r_abc", "ended_at": 1715000000.0},
    )
    env = _translate_to_rpc_envelope(event)
    if env is None or env["params"]["payload"].get("kind") != "workflow_run_completed":
        print("[FAIL] translate_run_completed — shape wrong")
        return False
    if env["params"]["payload"].get("ended_at") != 1715000000.0:
        print("[FAIL] translate_run_completed — ended_at missing")
        return False
    print("[OK] translate_run_completed")
    return True


def test_translate_run_failed() -> bool:
    event = GatewayNotice(
        kind="workflow_run_failed",
        text="run failed",
        extra={
            "run_id": "r_xyz",
            "error": "boom",
            "error_type": "RuntimeError",
            "ended_at": 1715000123.0,
        },
    )
    env = _translate_to_rpc_envelope(event)
    if env is None:
        print("[FAIL] translate_run_failed — None")
        return False
    p = env["params"]["payload"]
    for k in ("kind", "run_id", "error", "error_type", "ended_at"):
        if k not in p:
            print(f"[FAIL] translate_run_failed — missing {k}")
            return False
    print("[OK] translate_run_failed")
    return True


def test_translate_run_halted() -> bool:
    event = GatewayNotice(
        kind="workflow_run_halted",
        text="run halted",
        extra={"run_id": "r_h", "reason": "max_total_reached", "ended_at": 1.0},
    )
    env = _translate_to_rpc_envelope(event)
    if env is None or env["params"]["payload"].get("reason") != "max_total_reached":
        print("[FAIL] translate_run_halted — reason missing")
        return False
    print("[OK] translate_run_halted")
    return True


def test_translate_run_cancelled() -> bool:
    event = GatewayNotice(
        kind="workflow_run_cancelled",
        text="run cancelled",
        extra={"run_id": "r_c", "reason": "user_cancelled"},
    )
    env = _translate_to_rpc_envelope(event)
    if env is None or env["params"]["payload"].get("reason") != "user_cancelled":
        print("[FAIL] translate_run_cancelled — reason missing")
        return False
    print("[OK] translate_run_cancelled")
    return True


def test_translate_step_started() -> bool:
    event = GatewayNotice(
        kind="workflow_step_started",
        text="step locate_worktree started",
        extra={"run_id": "r_s", "step": "locate_worktree"},
    )
    env = _translate_to_rpc_envelope(event)
    if env is None:
        print("[FAIL] translate_step_started — None")
        return False
    p = env["params"]["payload"]
    if p.get("kind") != "workflow_step_started" or p.get("step") != "locate_worktree":
        print(f"[FAIL] translate_step_started — payload wrong: {p}")
        return False
    print("[OK] translate_step_started")
    return True


def test_translate_step_finished() -> bool:
    event = GatewayNotice(
        kind="workflow_step_finished",
        text="step done",
        extra={
            "run_id": "r_s",
            "step": "compute_module_coverage",
            "tool_name": "compute_module_coverage",
            "duration_seconds": 58.0,
            "ok": True,
        },
    )
    env = _translate_to_rpc_envelope(event)
    if env is None:
        print("[FAIL] translate_step_finished — None")
        return False
    p = env["params"]["payload"]
    if p.get("kind") != "workflow_step_finished":
        print(f"[FAIL] translate_step_finished — kind wrong: {p.get('kind')}")
        return False
    for k in ("step", "tool_name", "duration_seconds", "ok"):
        if k not in p:
            print(f"[FAIL] translate_step_finished — missing {k}")
            return False
    print("[OK] translate_step_finished")
    return True


def test_translate_subagent_spawned() -> bool:
    event = GatewayNotice(
        kind="workflow_subagent_spawned",
        text="subagent",
        extra={"run_id": "r_x", "subagent_id": "sa_1"},
    )
    env = _translate_to_rpc_envelope(event)
    if env is None or env["params"]["payload"].get("subagent_id") != "sa_1":
        print("[FAIL] translate_subagent_spawned — subagent_id missing")
        return False
    print("[OK] translate_subagent_spawned")
    return True


def test_translate_filters_non_workflow() -> bool:
    """Restart / online / long_run notices must NOT reach the desktop reducer."""
    for kind in ("restart", "online", "long_run", "agent_init", ""):
        event = GatewayNotice(kind=kind, text="noise", extra={"foo": "bar"})
        env = _translate_to_rpc_envelope(event)
        if env is not None:
            print(f"[FAIL] translate_filters_non_workflow — leaked: kind={kind}")
            return False
    print("[OK] translate_filters_non_workflow")
    return True


# ===========================================================================
# Tests — bridge dispatch path with stubbed emit
# ===========================================================================

class _EmitRecorder:
    """Stand-in for tui_gateway.server._emit — records every call."""
    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self._lock = threading.Lock()

    def __call__(self, event_type: str, session_id: str, payload: dict) -> None:
        with self._lock:
            self.calls.append((event_type, session_id, payload))


def test_bridge_calls_emit_on_run_started() -> bool:
    reset_for_tests()
    bridge = get_bridge()
    recorder = _EmitRecorder()
    bridge.set_emit_fn_for_tests(recorder)

    event = GatewayNotice(
        kind="workflow_run_started",
        text="run r_x started",
        extra={"run_id": "r_x", "workflow": "wf_test"},
    )
    bridge._handle(event)

    if len(recorder.calls) != 1:
        print(f"[FAIL] bridge_calls_emit_on_run_started — expected 1 call, got {len(recorder.calls)}")
        return False
    event_type, session_id, payload = recorder.calls[0]
    if event_type != "workflow_run_started":
        print(f"[FAIL] bridge_calls_emit_on_run_started — wrong type: {event_type}")
        return False
    if payload.get("run_id") != "r_x":
        print(f"[FAIL] bridge_calls_emit_on_run_started — payload wrong: {payload}")
        return False
    stats = bridge.stats
    if stats["received"] != 1 or stats["translated"] != 1 or stats["emit_ok"] != 1:
        print(f"[FAIL] bridge_calls_emit_on_run_started — stats wrong: {stats}")
        return False
    print("[OK] bridge_calls_emit_on_run_started")
    return True


def test_bridge_filters_non_workflow_at_handle_layer() -> bool:
    reset_for_tests()
    bridge = get_bridge()
    recorder = _EmitRecorder()
    bridge.set_emit_fn_for_tests(recorder)

    event = GatewayNotice(kind="restart", text="noise", extra={"foo": "bar"})
    bridge._handle(event)

    if len(recorder.calls) != 0:
        print(f"[FAIL] bridge_filters_non_workflow — leaked to emit: {recorder.calls}")
        return False
    stats = bridge.stats
    if stats["received"] != 1 or stats["filtered_non_workflow"] != 1:
        print(f"[FAIL] bridge_filters_non_workflow — stats wrong: {stats}")
        return False
    print("[OK] bridge_filters_non_workflow")
    return True


def test_bridge_chains_existing_dispatcher() -> bool:
    """Registering must wrap the runtime's existing dispatcher (chain)."""
    reset_for_tests()

    class FakeRuntime:
        def __init__(self):
            self._dispatcher = None
            self.existing_calls: list = []

        def set_dispatcher(self, fn):
            self._dispatcher = fn

        def existing_dispatcher(self, event):
            self.existing_calls.append(event)

    rt = FakeRuntime()
    rt.set_dispatcher(rt.existing_dispatcher)

    bridge = get_bridge()
    bridge.register(rt)

    # Dispatch one workflow event — both the existing dispatcher AND
    # the bridge must see it.
    recorder = _EmitRecorder()
    bridge.set_emit_fn_for_tests(recorder)
    event = GatewayNotice(
        kind="workflow_run_completed",
        text="done",
        extra={"run_id": "r_chain"},
    )
    rt._dispatcher(event)

    if len(rt.existing_calls) != 1:
        print(f"[FAIL] bridge_chains_existing_dispatcher — existing saw {len(rt.existing_calls)}")
        return False
    if len(recorder.calls) != 1:
        print(f"[FAIL] bridge_chains_existing_dispatcher — bridge saw {len(recorder.calls)}")
        return False
    if recorder.calls[0][2].get("run_id") != "r_chain":
        print(f"[FAIL] bridge_chains_existing_dispatcher — payload wrong: {recorder.calls[0]}")
        return False
    print("[OK] bridge_chains_existing_dispatcher")
    return True


def test_bridge_idempotent_register() -> bool:
    """Re-registering must not double-wrap the dispatcher."""
    reset_for_tests()

    class FakeRuntime:
        def __init__(self):
            self._dispatcher = None
            self.set_count = 0

        def set_dispatcher(self, fn):
            self._dispatcher = fn
            self.set_count += 1

    rt = FakeRuntime()
    bridge = get_bridge()
    bridge.register(rt)
    bridge.register(rt)
    bridge.register(rt)

    if rt.set_count != 1:
        print(f"[FAIL] bridge_idempotent_register — set_dispatcher called {rt.set_count} times")
        return False
    print("[OK] bridge_idempotent_register")
    return True


# ===========================================================================
# Main
# ===========================================================================

def main() -> int:
    tests = [
        test_translate_run_started,
        test_translate_run_completed,
        test_translate_run_failed,
        test_translate_run_halted,
        test_translate_run_cancelled,
        test_translate_step_started,
        test_translate_step_finished,
        test_translate_subagent_spawned,
        test_translate_filters_non_workflow,
        test_bridge_calls_emit_on_run_started,
        test_bridge_filters_non_workflow_at_handle_layer,
        test_bridge_chains_existing_dispatcher,
        test_bridge_idempotent_register,
    ]
    failures = []
    for t in tests:
        if not t():
            failures.append(t.__name__)
    print()
    if failures:
        print(f"[FAIL] {len(failures)}/{len(tests)} desktop_event_bridge tests failed:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"[OK] {len(tests)}/{len(tests)} desktop_event_bridge tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
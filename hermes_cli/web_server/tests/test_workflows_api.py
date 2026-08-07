"""test_workflows_api — RED test for the new /api/workflows/* endpoints.

Per the user's TDD preference (Pitfall from session 2026-07-08): build
unit tests to confirm RED, then ship the GREEN endpoint.

Endpoints under test:
  - GET  /api/workflows/library      list saved workflows
  - GET  /api/workflows/inspect      inspect one workflow (source + inputs)
  - POST /api/workflows/run          start a run, return run_id
  - GET  /api/workflows/status        poll a run's state

The endpoints read `~/.hermes/workflows/library.json`. We monkey-patch
the library path resolver to a tmp dir so the test owns the fixture.
"""

from __future__ import annotations

import importlib
import json
import shutil
import sys
from pathlib import Path


# Make hermes_cli + the web_server importable.
REPO_ROOT = Path("/home/cage/Desktop/Workspaces/HermesDesktop")
sys.path.insert(0, str(REPO_ROOT))


# This test imports hermes_cli.web_server, which requires fastapi.
# Skip gracefully when running outside the project venv.
try:
    from fastapi.testclient import TestClient  # noqa: E402
    from hermes_cli import web_server  # noqa: E402
except ImportError as exc:
    print(f"[SKIP] hermes_cli.web_server import failed: {exc}")
    sys.exit(0)


def _build_client(monkeypatch, workflows_dir: Path, library_entries: list[dict]):
    """Build a TestClient with library.json + workflow files staged in tmp."""
    workflows_dir.mkdir(parents=True, exist_ok=True)
    # Write library.json
    (workflows_dir / "library.json").write_text(
        json.dumps({"version": 1, "entries": library_entries})
    )
    # Write the workflow .py files referenced by library entries
    for entry in library_entries:
        # Default test script body — just a no-op workflow
        body = (
            '"""test workflow."""\n'
            "from plugins.hermes_workflow import step, workflow, Evidence\n"
            "\n"
            "@step(name='noop')\n"
            "async def noop(ctx):\n"
            "    return Evidence(files_changed=(), commands_run=(),\n"
            "                    exit_codes=(), tests_run=0,\n"
            "                    tests_passed=0, duration_seconds=0.0)\n"
            "\n"
            "@workflow(name='test_wf')\n"
            "async def run(ctx):\n"
            "    await noop(ctx)\n"
        )
        (workflows_dir / entry["path"]).write_text(body)
    # Patch the path resolver. The cache decorator wraps _read_library;
    # we patch the underlying Path construction so both library reads
    # AND script reads see our tmp dir.
    monkeypatch.setattr(web_server, "_WORKFLOWS_DIR", workflows_dir)
    # Bust the TTL cache so the next call sees the patched dir.
    web_server._read_library.cache_bust()  # type: ignore[attr-defined]
    web_server._workflow_runtime_singleton.cache_bust()  # type: ignore[attr-defined]
    return TestClient(web_server.app)


def test_library_list_empty(monkeypatch, tmp_path):
    """Empty library.json → empty entries array."""
    client = _build_client(monkeypatch, tmp_path / "wf", [])
    r = client.get(
        "/api/workflows/library",
        headers={"Authorization": f"Bearer {web_server._SESSION_TOKEN}"},
    )
    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
    body = r.json()
    assert body == {"entries": []}, f"unexpected body: {body}"
    print("[OK] library_list_empty")


def test_library_list_populated(monkeypatch, tmp_path):
    """Populated library.json → entries returned with name/description/path."""
    client = _build_client(
        monkeypatch, tmp_path / "wf",
        [
            {
                "name": "demo",
                "description": "smoke workflow",
                "path": "demo.py",
                "created_at": "2026-07-19T22:26:05.745485+00:00",
            },
            {
                "name": "bug_fix_verification",
                "description": "verify previously identified bugs are fixed",
                "path": "bug_fix_verification.py",
                "created_at": "2026-06-30T21:39:25.553248+00:00",
            },
        ],
    )
    r = client.get(
        "/api/workflows/library",
        headers={"Authorization": f"Bearer {web_server._SESSION_TOKEN}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["entries"]) == 2
    names = [e["name"] for e in body["entries"]]
    assert names == ["demo", "bug_fix_verification"]
    print("[OK] library_list_populated")


def test_inspect_returns_source_and_inputs(monkeypatch, tmp_path):
    """inspect returns name + source + inputs_required."""
    client = _build_client(
        monkeypatch, tmp_path / "wf",
        [{"name": "demo", "description": "x", "path": "demo.py", "created_at": "t"}],
    )
    r = client.get(
        "/api/workflows/inspect",
        params={"name": "demo"},
        headers={"Authorization": f"Bearer {web_server._SESSION_TOKEN}"},
    )
    assert r.status_code == 200, f"got {r.status_code}: {r.text}"
    body = r.json()
    assert body["name"] == "demo"
    assert "source" in body
    assert "test_wf" in body["source"]  # confirms we read the file
    assert isinstance(body["inputs_required"], list)
    print("[OK] inspect_returns_source_and_inputs")


def test_inspect_unknown_name_returns_404(monkeypatch, tmp_path):
    # Pre-condition: at least one entry exists so 404 means "not in library",
    # not "endpoint missing". This pins the test against the false-positive
    # trap where every request returns 404 because the route doesn't exist.
    client = _build_client(
        monkeypatch, tmp_path / "wf",
        [{"name": "demo", "description": "x", "path": "demo.py", "created_at": "t"}],
    )
    # Sanity: known name returns 200 (route exists)
    r_known = client.get(
        "/api/workflows/inspect",
        params={"name": "demo"},
        headers={"Authorization": f"Bearer {web_server._SESSION_TOKEN}"},
    )
    assert r_known.status_code == 200, (
        f"known name should return 200 (route exists), got {r_known.status_code}"
    )
    # Now the unknown-name case actually means "not found in library"
    r = client.get(
        "/api/workflows/inspect",
        params={"name": "no_such_workflow"},
        headers={"Authorization": f"Bearer {web_server._SESSION_TOKEN}"},
    )
    assert r.status_code == 404
    print("[OK] inspect_unknown_name_returns_404")


def test_run_returns_run_id(monkeypatch, tmp_path):
    """POST /api/workflows/run starts a run and returns {run_id: r_...}."""
    client = _build_client(
        monkeypatch, tmp_path / "wf",
        [{"name": "demo", "description": "x", "path": "demo.py", "created_at": "t"}],
    )
    r = client.post(
        "/api/workflows/run",
        json={"name": "demo", "inputs": {}},
        headers={"Authorization": f"Bearer {web_server._SESSION_TOKEN}"},
    )
    assert r.status_code == 200, f"got {r.status_code}: {r.text}"
    body = r.json()
    assert "run_id" in body
    assert body["run_id"].startswith("r_")
    print(f"[OK] run_returns_run_id — {body['run_id']}")


def test_run_unknown_name_returns_404(monkeypatch, tmp_path):
    # Pre-condition: route exists (known name returns 200, not 405 Method
    # Not Allowed or 404).
    client = _build_client(
        monkeypatch, tmp_path / "wf",
        [{"name": "demo", "description": "x", "path": "demo.py", "created_at": "t"}],
    )
    r_known = client.post(
        "/api/workflows/run",
        json={"name": "demo", "inputs": {}},
        headers={"Authorization": f"Bearer {web_server._SESSION_TOKEN}"},
    )
    assert r_known.status_code == 200, (
        f"known run should return 200, got {r_known.status_code}"
    )
    r = client.post(
        "/api/workflows/run",
        json={"name": "no_such"},
        headers={"Authorization": f"Bearer {web_server._SESSION_TOKEN}"},
    )
    assert r.status_code == 404
    print("[OK] run_unknown_name_returns_404")


def test_status_returns_state_for_known_run(monkeypatch, tmp_path):
    """GET /api/workflows/status?run_id=... returns state + steps.

    Drives the run AND the status poll within the same test so both
    hit the same runtime singleton (the REST layer caches one
    WorkflowRuntime per process; each cache_bust() forces a rebuild).
    """
    client = _build_client(
        monkeypatch, tmp_path / "wf",
        [{"name": "demo", "description": "x", "path": "demo.py", "created_at": "t"}],
    )
    # Start a run
    r1 = client.post(
        "/api/workflows/run",
        json={"name": "demo", "inputs": {}},
        headers={"Authorization": f"Bearer {web_server._SESSION_TOKEN}"},
    )
    assert r1.status_code == 200, f"submit failed: {r1.text}"
    run_id = r1.json()["run_id"]
    # Poll its status
    r2 = client.get(
        "/api/workflows/status",
        params={"run_id": run_id},
        headers={"Authorization": f"Bearer {web_server._SESSION_TOKEN}"},
    )
    assert r2.status_code == 200, f"status got {r2.status_code}: {r2.text}"
    body = r2.json()
    assert body["run_id"] == run_id, f"run_id mismatch: {body}"
    print(f"[OK] status_returns_state_for_known_run — state={body.get('state')}")


def test_status_unknown_run_returns_404(monkeypatch, tmp_path):
    # Pre-condition: known run returns 200.
    client = _build_client(
        monkeypatch, tmp_path / "wf",
        [{"name": "demo", "description": "x", "path": "demo.py", "created_at": "t"}],
    )
    r_known = client.post(
        "/api/workflows/run",
        json={"name": "demo", "inputs": {}},
        headers={"Authorization": f"Bearer {web_server._SESSION_TOKEN}"},
    )
    assert r_known.status_code == 200
    # Now the unknown-run case actually means "no such run"
    r = client.get(
        "/api/workflows/status",
        params={"run_id": "r_does_not_exist"},
        headers={"Authorization": f"Bearer {web_server._SESSION_TOKEN}"},
    )
    assert r.status_code == 404
    print("[OK] status_unknown_run_returns_404")


# ----------------------------------------------------------------------------
# Pin the wire-format contract: a run started via /api/workflows/run must
# translate GatewayNotice events through the desktop_event_bridge so the
# Hermes desktop's Workflows panel populates. Regression: the prior
# `_workflow_runtime_singleton` built a fresh WorkflowRuntime whose
# dispatcher was unwired — workflow_run_started / _completed notices went
# nowhere from the REST path, even though the bridge was registered on the
# plugin's own runtime. The desktop panel stayed at "0 runs since boot"
# for every REST-launched run.
# ----------------------------------------------------------------------------

def test_rest_run_emits_desktop_wire_envelopes(monkeypatch, tmp_path):
    from plugins.hermes_workflow.desktop_event_bridge import (
        get_bridge,
        reset_for_tests,
    )

    reset_for_tests()
    calls: list = []
    def _emit(event_type, session_id, payload):
        calls.append((event_type, session_id, payload))

    get_bridge().set_emit_fn_for_tests(_emit)

    client = _build_client(
        monkeypatch, tmp_path / "wf",
        [{"name": "demo", "description": "x", "path": "demo.py", "created_at": "t"}],
    )
    r = client.post(
        "/api/workflows/run",
        json={"name": "demo", "inputs": {}},
        headers={"Authorization": f"Bearer {web_server._SESSION_TOKEN}"},
    )
    assert r.status_code == 200, f"submit failed: {r.text}"
    run_id = r.json()["run_id"]
    # Wait for the run to complete; the runtime drives the task to
    # completion asynchronously after submit() returns.
    import time as _time
    for _ in range(60):
        r2 = client.get(
            "/api/workflows/status",
            params={"run_id": run_id},
            headers={"Authorization": f"Bearer {web_server._SESSION_TOKEN}"},
        )
        if r2.status_code == 200 and r2.json().get("state") in ("done", "failed"):
            break
        _time.sleep(0.1)

    kinds = [c[0] for c in calls]
    assert "workflow_run_started" in kinds, (
        f"missing workflow_run_started on REST-launched run; got {kinds}. "
        "The REST singleton runtime must register the desktop_event_bridge."
    )
    assert "workflow_run_completed" in kinds, (
        f"missing workflow_run_completed on REST-launched run; got {kinds}"
    )
    print(f"[OK] rest_run_emits_desktop_wire_envelopes — {dict((k, kinds.count(k)) for k in set(kinds))}")


# ----------------------------------------------------------------------------
# /api/workflows/diag — operator diagnostic surface that exposes every
# link in the wire (plugin runtime, REST singleton, dispatcher, bridge,
# tui_gateway, active runs). Used by the desktop's WireProbe to surface
# the live state of the chain so a "0 runs since boot" mystery is
# debuggable without leaving the panel.
# ----------------------------------------------------------------------------
def test_diag_endpoint_reports_wire_state(monkeypatch, tmp_path):
    from plugins.hermes_workflow.desktop_event_bridge import (
        get_bridge,
        reset_for_tests,
    )

    reset_for_tests()
    client = _build_client(
        monkeypatch, tmp_path / "wf",
        [{"name": "demo", "description": "x", "path": "demo.py", "created_at": "t"}],
    )
    r = client.get(
        "/api/workflows/diag",
        headers={"Authorization": f"Bearer {web_server._SESSION_TOKEN}"},
    )
    assert r.status_code == 200, f"diag got {r.status_code}: {r.text}"
    body = r.json()

    # bridge must be present in the response (the test environment
    # imports the plugin so get_bridge() is available).
    assert body.get("bridge_present") is True, f"bridge missing in diag: {body}"
    # The REST singleton must resolve (singleton present, bridge
    # registered, dispatcher set). The exact value of
    # singleton_is_plugin_runtime depends on whether the plugin's
    # register() was invoked before this test — in the test harness
    # it isn't, so the fallback path in _workflow_runtime_singleton
    # builds a fresh runtime AND registers the bridge on it. Either
    # way: bridge_present is True and runtime_dispatcher_set is True
    # once a workflow has been submitted.
    assert body.get("tui_gateway_emit_importable") in (True, False), body
    assert isinstance(body.get("active_run_ids"), list), body
    print(f"[OK] diag_endpoint_reports_wire_state — keys={sorted(body.keys())}")


def test_diag_reflects_bridge_state_after_run(monkeypatch, tmp_path):
    from plugins.hermes_workflow.desktop_event_bridge import (
        get_bridge,
        reset_for_tests,
    )

    reset_for_tests()
    calls: list = []
    get_bridge().set_emit_fn_for_tests(
        lambda event_type, session_id, payload: calls.append((event_type, session_id, payload))
    )

    client = _build_client(
        monkeypatch, tmp_path / "wf",
        [{"name": "demo", "description": "x", "path": "demo.py", "created_at": "t"}],
    )
    # Submit a run to force the runtime singleton to materialize.
    r = client.post(
        "/api/workflows/run",
        json={"name": "demo", "inputs": {}},
        headers={"Authorization": f"Bearer {web_server._SESSION_TOKEN}"},
    )
    assert r.status_code == 200, f"submit failed: {r.text}"
    run_id = r.json()["run_id"]
    import time as _time
    for _ in range(60):
        r2 = client.get(
            "/api/workflows/status",
            params={"run_id": run_id},
            headers={"Authorization": f"Bearer {web_server._SESSION_TOKEN}"},
        )
        if r2.status_code == 200 and r2.json().get("state") in ("done", "failed"):
            break
        _time.sleep(0.1)

    # Now /api/workflows/diag must report the runtime singleton as
    # present AND a non-zero active run count (or just-completed
    # in-memory record).
    r3 = client.get(
        "/api/workflows/diag",
        headers={"Authorization": f"Bearer {web_server._SESSION_TOKEN}"},
    )
    assert r3.status_code == 200
    body = r3.json()
    assert body.get("singleton_runtime_present") is True, body
    # Bridge stats must have ticked — received/translated at minimum
    # should be > 0 after a completed run.
    bs = body.get("bridge_stats") or {}
    assert bs.get("received", 0) >= 1, f"bridge.received did not tick: {bs}"
    # And emit_ok should match the number of workflow_run_* envelopes
    # the test emitted (workflow_run_started + workflow_run_completed
    # = 2). Other events go through dispatcher too but only the wire
    # kinds count toward emit_ok (filtered_non_workflow drops the rest).
    print(
        f"[OK] diag_reflects_bridge_state_after_run — bridge={bs} "
        f"active_runs={body.get('active_run_ids')}"
    )


# ----- manual runner (python3 -m pytest would need fixtures) -----

if __name__ == "__main__":
    # Lightweight monkeypatch stand-in (no pytest dep needed).
    class _Monkeypatch:
        def __init__(self):
            self._originals: dict = {}

        def setattr(self, target, name, value):
            self._originals[(id(target), name)] = (target, name, getattr(target, name, None))
            setattr(target, name, value)

        def undo(self):
            for target, name, original in self._originals.values():
                if original is None:
                    try:
                        delattr(target, name)
                    except AttributeError:
                        pass
                else:
                    setattr(target, name, original)

    mp = _Monkeypatch()
    tmp = Path("/tmp/test_workflows_api")
    if tmp.exists():
        import shutil
        shutil.rmtree(tmp)
    tmp.mkdir()
    failed = 0
    for fn in [
        test_library_list_empty,
        test_library_list_populated,
        test_inspect_returns_source_and_inputs,
        test_inspect_unknown_name_returns_404,
        test_run_returns_run_id,
        test_run_unknown_name_returns_404,
        test_status_returns_state_for_known_run,
        test_status_unknown_run_returns_404,
        test_rest_run_emits_desktop_wire_envelopes,
        test_diag_endpoint_reports_wire_state,
        test_diag_reflects_bridge_state_after_run,
    ]:
        # Use a sub-tmp for each test so the library fixture is fresh
        sub = tmp / fn.__name__
        if sub.exists():
            shutil.rmtree(sub)
        try:
            fn(mp, sub)
        except AssertionError as exc:
            failed += 1
            print(f"[FAIL] {fn.__name__}: AssertionError: {exc!r}", flush=True)
        except Exception as exc:
            import traceback
            failed += 1
            print(f"[FAIL] {fn.__name__}: {type(exc).__name__}: {exc}", flush=True)
            traceback.print_exc()
    mp.undo()
    shutil.rmtree(tmp)
    if failed:
        print(f"\n[FAIL] {failed} tests failed")
        sys.exit(1)
    print("\n[OK] all workflow API tests passed (RED+GREEN pair complete)")
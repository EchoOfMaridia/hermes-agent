"""End-to-end regression test for the /workflow slash surface.

v0.2.0 (2026-06-30): ScriptAuthor + alias bridge + status/snapshot
za_xxx resolver. Every slash subcommand is exercised through the
real ``_dispatch_workflow`` entry point so a regression in any of
them surfaces here. Uses a canned LLM stub (no network) so the test
runs in <1s and is deterministic.

What this catches:
- ``/workflow create`` failure (e.g. coroutine-not-iterable bug)
- alias file missing/wrong (status/snapshot can't resolve)
- ``/workflow status/snapshot/expand`` returning exit errors when
  given a ``za_`` synthesized run_id
- ``/workflow inspect/run`` failing on a library name (v0.2.0
  added name lookup alongside path lookup)
- CLI commands seeing a different filesystem than the slash
  surface (HERMES_WORKFLOW_ROOT forwarding)

Run: pytest plugins/hermes_workflow/tests/test_slash_e2e.py -v
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

HERMES_ROOT = Path("/home/cage/.hermes/hermes-agent")
if str(HERMES_ROOT) not in sys.path:
    sys.path.insert(0, str(HERMES_ROOT))

from plugins.hermes_workflow.script_author import ScriptAuthor
from plugins.hermes_workflow.runtime_factory import build_runtime
from plugins.hermes_workflow.slash import _dispatch_workflow


def _drain_dispatch(result):
    """Drive _dispatch_workflow's result to a string.

    The streaming-slash fix (2026-06-30) made ``/workflow create``
    return an ``AsyncIterator[str]`` so the chat surface receives
    each yield as an incremental message. Other subcommands still
    return ``str | None`` directly. This helper unifies both shapes
    so existing assertions on the final string keep working.
    """
    if hasattr(result, "__aiter__"):
        async def _drain():
            chunks = []
            async for c in result:
                chunks.append(c)
            return "".join(chunks) or None
        return asyncio.run(_drain())
    return result


# Import the canned streaming LLM from the streaming tests.
from plugins.hermes_workflow.tests.test_script_author_streaming import (
    _CannedStreamingLlm, VALID_SCRIPT,
)


@pytest.fixture
def runtime_and_author(tmp_path):
    """Build a runtime + ScriptAuthor pointed at tmp_path/wf.

    Uses the canned streaming LLM so the test is hermetic and fast.
    """
    runtime = build_runtime(journal_root=tmp_path / "wf")
    llm = _CannedStreamingLlm(parsed={
        "name": "demo",
        "description": "demo workflow",
        "script": VALID_SCRIPT,
        "step_names": ["only"],
    }, chunks=["chunk1"])
    author = ScriptAuthor(
        llm=llm, library_root=tmp_path / "wf" / "library",
    )
    return runtime, author


class TestSlashEndToEnd:
    """All 11 subcommands via _dispatch_workflow."""

    def test_create_generates_workflow_and_writes_alias(
        self, runtime_and_author, tmp_path,
    ):
        """REGRESSION for 2026-06-30: /workflow create must produce
        a working run_id AND a za_<id>.alias sidecar that maps it
        to the runtime-issued r_<id> journal file."""
        runtime, author = runtime_and_author
        result = _drain_dispatch(_dispatch_workflow(
            runtime, "create a demo workflow", script_author=author,
        ))
        assert "✅ generated" in result, result
        za_run_id = result.split("run_id=")[1].split("\n")[0].strip()
        assert za_run_id.startswith("za_demo_"), za_run_id

        # Alias sidecar must exist with the runtime-issued r_ id.
        alias_path = tmp_path / "wf" / f"{za_run_id}.alias"
        assert alias_path.exists(), (
            f"alias file missing: {alias_path} — /workflow status "
            f"{za_run_id} will not be able to resolve"
        )
        real_run_id = alias_path.read_text().strip()
        assert real_run_id.startswith("r_"), real_run_id
        # The real journal file must exist on disk.
        assert (tmp_path / "wf" / f"{real_run_id}.journal").exists(), (
            f"real journal missing: {real_run_id}"
        )

    def test_status_resolves_za_run_id_via_alias(
        self, runtime_and_author, tmp_path,
    ):
        """REGRESSION: /workflow status <za_xxx> must resolve to the
        underlying r_xxx journal via the alias sidecar."""
        runtime, author = runtime_and_author
        create_result = _drain_dispatch(_dispatch_workflow(
            runtime, "create a demo workflow", script_author=author,
        ))
        za_run_id = create_result.split("run_id=")[1].split("\n")[0].strip()

        status = _dispatch_workflow(
            runtime, f"status {za_run_id}", script_author=author,
        )
        # Without the fix this returned "(exit 1)" because the CLI
        # tried to find <journal_root>/<za_run_id>.journal which
        # doesn't exist (the journal is under the r_xxx id).
        assert "(exit" not in status, status
        assert "demo" in status, status

    def test_snapshot_resolves_za_run_id_via_alias(
        self, runtime_and_author,
    ):
        """REGRESSION: /workflow snapshot <za_xxx> must follow the
        alias sidecar to the real journal."""
        runtime, author = runtime_and_author
        create_result = _drain_dispatch(_dispatch_workflow(
            runtime, "create a demo workflow", script_author=author,
        ))
        za_run_id = create_result.split("run_id=")[1].split("\n")[0].strip()

        snap = _dispatch_workflow(
            runtime, f"snapshot {za_run_id}", script_author=author,
        )
        assert "(exit" not in snap, snap
        # The snapshot rendered against the real r_ id, not the
        # synthesized one — the renderer labels with the real id.
        assert "■" in snap, snap

    def test_expand_resolves_za_run_id_via_alias(
        self, runtime_and_author,
    ):
        """REGRESSION: /workflow expand <za_xxx> must follow the
        alias sidecar."""
        runtime, author = runtime_and_author
        create_result = _drain_dispatch(_dispatch_workflow(
            runtime, "create a demo workflow", script_author=author,
        ))
        za_run_id = create_result.split("run_id=")[1].split("\n")[0].strip()

        expand = _dispatch_workflow(
            runtime, f"expand {za_run_id}", script_author=author,
        )
        assert "(exit" not in expand, expand
        assert "■" in expand, expand

    def test_inspect_accepts_library_name(
        self, runtime_and_author,
    ):
        """v0.2.0: /workflow inspect <name> must work for library
        entries created by /workflow create."""
        runtime, author = runtime_and_author
        _drain_dispatch(_dispatch_workflow(
            runtime, "create a demo workflow", script_author=author,
        ))
        result = _drain_dispatch(_dispatch_workflow(
            runtime, "inspect demo", script_author=author,
        ))
        assert "demo" in result, result
        # The path printed should be the saved library script.
        assert ".py" in result, result
        # Without the fix this returned "(exit 1)" because the
        # legacy CLI only accepted filesystem paths.

    def test_run_accepts_library_name(
        self, runtime_and_author,
    ):
        """v0.2.0: /workflow run <name> must work for library
        entries."""
        runtime, author = runtime_and_author
        _drain_dispatch(_dispatch_workflow(
            runtime, "create a demo workflow", script_author=author,
        ))
        result = _drain_dispatch(_dispatch_workflow(
            runtime, "run demo", script_author=author,
        ))
        assert "completed" in result or "failed" in result, result

    def test_list_shows_created_workflow(
        self, runtime_and_author,
    ):
        """REGRESSION: /workflow list must show entries created
        by the slash surface's ScriptAuthor (whose manifest lives
        at <journal_root>/library/library.json). The CLI's
        v0.1.0 list only looked at <journal_root>/library.json."""
        runtime, author = runtime_and_author
        _drain_dispatch(_dispatch_workflow(
            runtime, "create a demo workflow", script_author=author,
        ))
        result = _drain_dispatch(_dispatch_workflow(
            runtime, "list", script_author=author,
        ))
        assert "demo" in result, result

    def test_help(self, runtime_and_author):
        runtime, author = runtime_and_author
        result = _drain_dispatch(_dispatch_workflow(
            runtime, "help", script_author=author,
        ))
        assert "workflow plugin commands" in result
        # All documented subcommands must appear in the help.
        for sub in ("run", "list", "inspect", "status", "snapshot",
                     "cancel", "save", "expand", "create", "help"):
            assert sub in result, f"{sub!r} missing from help text"

    def test_unknown_subcommand(self, runtime_and_author):
        runtime, author = runtime_and_author
        result = _drain_dispatch(_dispatch_workflow(
            runtime, "frobnicate", script_author=author,
        ))
        assert "unknown workflow subcommand" in result, result
        assert "/workflow help" in result, result

    def test_cancel_with_bogus_run_id(self, runtime_and_author):
        runtime, author = runtime_and_author
        result = _drain_dispatch(_dispatch_workflow(
            runtime, "cancel bogus_run_id", script_author=author,
        ))
        assert "unknown run_id" in result or "cancel failed" in result, (
            result
        )

    def test_save_v020_stub(self, runtime_and_author):
        runtime, author = runtime_and_author
        result = _drain_dispatch(_dispatch_workflow(
            runtime, "save some_name", script_author=author,
        ))
        assert "v0.2.0" in result, result

    def test_create_uses_synthesized_id_consistently(
        self, runtime_and_author,
    ):
        """REGRESSION for the double-za-id bug.

        Before the fix, ScriptAuthor's generate() synthesized one
        ``za_<slug>_<hex>`` for internal use (alias file name,
        AuthorResult.run_id), and ``_create_via_script_author`` in
        slash.py synthesized a DIFFERENT ``za_<slug>_<hex>`` for the
        user-visible message. The user typed
        ``/workflow status <the_user_visible_one>`` but the alias
        file was named after the internal one — so /workflow status
        returned ``(exit 1)``.

        After the fix, the user-visible run_id == AuthorResult.run_id
        == the alias filename. /workflow status works.

        Pin both directions: the message and the alias file use
        the SAME id.
        """
        runtime, author = runtime_and_author
        result = _drain_dispatch(_dispatch_workflow(
            runtime, "create a demo workflow", script_author=author,
        ))
        za_in_message = result.split("run_id=")[1].split("\n")[0].strip()
        # The alias file must be named with the EXACT id from the
        # message — not a regenerated one.
        from pathlib import Path
        # Find the alias file for this run.
        wf_dir = runtime.journal_root
        alias_files = list(wf_dir.glob("za_demo_*.alias"))
        assert len(alias_files) == 1, (
            f"expected exactly 1 alias file, found {len(alias_files)}: "
            f"{alias_files}"
        )
        assert alias_files[0].name == f"{za_in_message}.alias", (
            f"alias filename mismatch: message says {za_in_message!r}, "
            f"alias file is {alias_files[0].name!r}"
        )
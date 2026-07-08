"""test_runtime_factory_bridge.py — contract test for build_bridge_from_env().

Pins the safe-default behaviour introduced in the 2026-07-08 fix:

    unset / ""               → StubBridge (was None — caused NotImplementedError)
    "stub"                   → StubBridge
    "hermes-chat" + on-PATH  → HermesChatBridge
    "hermes-chat" + no PATH  → RuntimeError (explicit opt-in, don't hide)
    anything-else            → StubBridge (was None — same NotImplementedError bug)

The PRE-fix default behaviour (`"" / unset → None → ask_agent raises`) is
what crashed every CLI-originated workflow run that called agents. This
test pins the POST-fix behaviour so a future refactor doesn't regress.
"""

from __future__ import annotations

import os
from unittest import mock

import pytest


def _clear_env(monkeypatch):
    """Strip HERMES_WORKFLOW_AGENT_BRIDGE so each test gets a clean slate."""
    monkeypatch.delenv("HERMES_WORKFLOW_AGENT_BRIDGE", raising=False)


@pytest.fixture
def bridge_module():
    """Import the bridge module fresh per-test to avoid cached env state."""
    # Tests run with REPO_ROOT (`~/.hermes/hermes-agent/`) on sys.path via
    # conftest, so `from plugins.hermes_workflow import hermes_chat_bridge`
    # works. Use that same import shape.
    from plugins.hermes_workflow import hermes_chat_bridge
    return hermes_chat_bridge


# ---------------------------------------------------------------------------
# Defaults — the actual fix
# ---------------------------------------------------------------------------

class TestBuildBridgeFromEnvDefaults:
    """The safe default must be a non-None bridge (StubBridge)."""

    def test_unset_returns_stubbridge(self, bridge_module, monkeypatch):
        """BUG REGRESSION PIN: unset used to return None → ask_agent raised."""
        _clear_env(monkeypatch)
        result = bridge_module.build_bridge_from_env()
        assert result is not None, (
            "build_bridge_from_env() returned None for unset env var. "
            "This was the pre-2026-07-08 bug — every ask_agent() call "
            "would raise NotImplementedError in CLI subprocesses."
        )
        assert isinstance(result, bridge_module.StubBridge)

    def test_empty_string_returns_stubbridge(
        self, bridge_module, monkeypatch
    ):
        monkeypatch.setenv("HERMES_WORKFLOW_AGENT_BRIDGE", "")
        result = bridge_module.build_bridge_from_env()
        assert isinstance(result, bridge_module.StubBridge)

    def test_whitespace_only_returns_stubbridge(
        self, bridge_module, monkeypatch
    ):
        """Whitespace shouldn't accidentally disable the bridge."""
        monkeypatch.setenv("HERMES_WORKFLOW_AGENT_BRIDGE", "   ")
        result = bridge_module.build_bridge_from_env()
        assert isinstance(result, bridge_module.StubBridge)

    def test_explicit_stub_returns_stubbridge(
        self, bridge_module, monkeypatch
    ):
        monkeypatch.setenv("HERMES_WORKFLOW_AGENT_BRIDGE", "stub")
        result = bridge_module.build_bridge_from_env()
        assert isinstance(result, bridge_module.StubBridge)

    def test_unknown_value_returns_stubbridge(
        self, bridge_module, monkeypatch, caplog
    ):
        """Unknown values must NOT disable the bridge (regression guard)."""
        monkeypatch.setenv("HERMES_WORKFLOW_AGENT_BRIDGE", "openai-foo-bar")
        with caplog.at_level("WARNING", logger="hermes_workflow.hermes_chat_bridge"):
            result = bridge_module.build_bridge_from_env()
        assert isinstance(result, bridge_module.StubBridge)
        # The warning should have been emitted so operators notice.
        assert any(
            "unrecognized" in rec.message.lower() for rec in caplog.records
        ), "Expected warning log when HERMES_WORKFLOW_AGENT_BRIDGE is unrecognized"


# ---------------------------------------------------------------------------
# Live-mode opt-in
# ---------------------------------------------------------------------------

class TestBuildBridgeFromEnvLive:
    """`hermes-chat` must still work for operators who want live LLM verdicts."""

    def test_hermes_chat_aliases_resolved(
        self, bridge_module, monkeypatch
    ):
        """All three spellings ('hermes-chat', 'hermes_chat', 'chat') should
        route to HermesChatBridge; the underlying init may still fail if the
        hermes binary is missing, in which case we catch RuntimeError but
        must NOT silently return None / StubBridge."""
        for variant in ("hermes-chat", "hermes_chat", "chat"):
            monkeypatch.setenv("HERMES_WORKFLOW_AGENT_BRIDGE", variant)
            try:
                result = bridge_module.build_bridge_from_env()
            except RuntimeError as exc:
                # Binary missing is a legitimate operator-visible failure;
                # the fix explicitly does NOT silently fall back here.
                assert "hermes" in str(exc).lower(), (
                    f"unexpected RuntimeError for {variant!r}: {exc}"
                )
            else:
                assert isinstance(
                    result, bridge_module.HermesChatBridge
                ), f"{variant!r} did not return HermesChatBridge; got {type(result).__name__}"

    def test_hermes_chat_unavailable_does_not_silently_fall_back(
        self, bridge_module, monkeypatch
    ):
        """When operator explicitly asks for hermes-chat and it's missing,
        we MUST raise (not degrade to stub). This is the dual of the
        safe-default fix: explicit opt-in → explicit failure, no surprises."""
        monkeypatch.setenv("HERMES_WORKFLOW_AGENT_BRIDGE", "hermes-chat")
        # Force HermesChatBridge.__init__ to raise by patching _hermes_path
        # discovery to return nothing.
        with mock.patch.object(
            bridge_module,
            "shutil",
            create=True,
        ) if False else mock.patch.dict(
            os.environ, {"PATH": "/nonexistent-empty-path-xyz"}
        ):
            with pytest.raises(RuntimeError):
                bridge_module.build_bridge_from_env()


# ---------------------------------------------------------------------------
# Stub bridge behaviour — make sure the default is actually USABLE
# ---------------------------------------------------------------------------

class TestStubBridgeUsableAsDefault:
    """StubBridge must return a real AgentResponse with parseable text."""

    @pytest.mark.asyncio
    async def test_stub_responds_to_ask_agent(
        self, bridge_module, monkeypatch
    ):
        _clear_env(monkeypatch)
        bridge = bridge_module.build_bridge_from_env()
        assert bridge is not None

        # StubBridge should accept arbitrary kwargs without crashing and
        # return a non-empty AgentResponse. We don't constrain the exact
        # text — some stubs are deterministic, some are heuristic — but
        # the response must be truthy and have a `.text` attribute.
        response = await bridge.invoke(
            prompt="classify this entry as REAL_GAP or INTENTIONAL_NO_ABI",
            model="sonnet",
            max_tokens=None,
        )
        assert response is not None
        assert hasattr(response, "text")
        assert isinstance(response.text, str)
        assert len(response.text) > 0, "StubBridge returned empty text"
        # The default 80-char minimum from the journal-side fallback should hold.
        # (StubBridge's DEFAULT_REVIEW_TEXT is purpose-built to satisfy this.)


# ---------------------------------------------------------------------------
# End-to-end: workflow that calls ask_agent must complete with stub default
# ---------------------------------------------------------------------------

class TestWorkflowCompletesWithDefaultBridge:
    """The original bug surfaced as: workflow → ask_agent → raises → run fails.

    With the fix, the default bridge IS a StubBridge → ask_agent returns a
    real AgentResponse → the workflow step completes → the run returns DONE.
    """

    @pytest.mark.asyncio
    async def test_run_agent_with_unset_env_completes(
        self, bridge_module, monkeypatch, tmp_path
    ):
        """Mini end-to-end: load a stub workflow, unset env, run, assert DONE."""
        _clear_env(monkeypatch)

        # Build a minimal 1-step workflow that calls ask_agent.
        from plugins.hermes_workflow import step, workflow, Evidence

        captured = {}

        @step(name="ask")
        async def ask(ctx) -> Evidence:
            bridge = bridge_module.build_bridge_from_env()
            response = await bridge.invoke(
                prompt="hello",
                model="haiku",
                max_tokens=None,
            )
            captured["text"] = response.text
            return Evidence(
                files_changed=(),
                commands_run=(),
                exit_codes=(),
                tests_run=0,
                tests_passed=0,
                duration_seconds=0.0,
            )

        @workflow(name="agent_bridge_default_smoke",
                  description="pin: unset env still gives the agent a bridge")
        async def run_wf(ctx) -> dict:
            await ask(ctx)
            return {"captured_len": len(captured.get("text", ""))}

        from plugins.hermes_workflow.runtime_factory import build_runtime
        runtime = build_runtime(journal_root=tmp_path)

        run_id = await runtime.submit(run_wf, inputs={}, workspace=tmp_path)
        # submit() returns the run_id string; fetch the Run via get_run.
        run = runtime.get_run(run_id)
        assert run is not None, "get_run returned None — run vanished"
        # Drive the workflow body to completion by awaiting its task.
        task = getattr(run, "task", None)
        if task is not None:
            result = await task
        else:
            # Older runtime shape: poll until done.
            import asyncio
            for _ in range(100):
                if run.state.value == "done":
                    break
                await asyncio.sleep(0.01)
            result = run.task.result() if hasattr(run, "task") else {}
        assert run.state.value == "done", (
            f"workflow did not complete with unset env: state={run.state.value}"
        )
        assert result["captured_len"] > 0, (
            "ask_agent with default stub bridge returned empty text"
        )

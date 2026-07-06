"""End-to-end smoke test for /workflow create — the real path.

This test exercises the production code path end-to-end:

    /workflow create <intent>
        -> slash._dispatch_workflow
        -> _create_via_script_author (returns AsyncIterator[str])
        -> gateway/run.py:9031 async-iterator branch (drains & coalesces)
        -> chat surface receives each yield as an incremental message

It verifies ALL of the following:

    1. The handler returns an async generator (not a single string).
    2. The chat surface receives N>1 incremental messages (not one blob).
    3. The first chunk includes a "🔨 generating..." indicator so the
       user sees activity before the first LLM token lands.
    4. Token deltas are flushed to the chat surface during generation.
    5. The final chunk contains the artifact card (run_id, script path,
       inline body preview, follow-up command).
    6. The workflow file exists on disk after the run.
    7. The library entry is registered.
    8. The generated workflow actually executes (loading the saved file
       and submitting it to the runtime produces a passing run).
    9. The /workflow status command resolves the za_ run_id to the real
       r_ run_id (alias bridge works).
   10. The pre-existing notifier (if any) still receives all events
       (notifier chaining — not replacement).

Run: pytest plugins/hermes_workflow/tests/test_real_workflow_create_e2e.py -v
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

import pytest

from plugins.hermes_workflow.runtime_factory import build_runtime
from plugins.hermes_workflow.script_author import ScriptAuthor
from plugins.hermes_workflow.slash import _dispatch_workflow

# Reuse the canned LLM + valid script from the streaming tests.
from plugins.hermes_workflow.tests.test_script_author_streaming import (
    VALID_SCRIPT,
    _CannedStreamingLlm,
)


# ---------------------------------------------------------------------------
# Minimal fake chat surface that mimics the production gateway dispatch
# (gateway/run.py:9031-9040). It drains the async generator and records
# each yield as an "incremental message" with a timestamp.
# ---------------------------------------------------------------------------

class FakeChatSurface:
    """Mimics how the gateway chat surface receives streaming chunks."""

    def __init__(self) -> None:
        self.messages: list[tuple[float, str]] = []   # (t, text)
        self._t0: float = 0.0

    async def dispatch(self, handler_result: Any) -> str | None:
        """Production branch: drain AsyncIterator[str] into incremental messages.

        Mirrors gateway/run.py:9031-9040 exactly.
        """
        if hasattr(handler_result, "__aiter__"):
            self._t0 = time.monotonic()
            chunks: list[str] = []
            async for chunk in handler_result:
                if chunk is None:
                    continue
                chunks.append(chunk)
                self.messages.append((time.monotonic() - self._t0, chunk))
                await asyncio.sleep(0)
            return "".join(chunks) or None
        if asyncio.iscoroutine(handler_result):
            result = await handler_result
            return str(result) if result else None
        return str(handler_result) if handler_result else None


# ---------------------------------------------------------------------------
# Workflow body that exercises a real @step → @workflow graph (not the
# trivial `only` demo). Includes a step that captures inputs and returns
# structured Evidence, so we can verify the generated file is not just
# importable but produces a real workflow output.
# ---------------------------------------------------------------------------

EXECUTABLE_SCRIPT = '''
from plugins.hermes_workflow import step, workflow, Evidence


@step(name="greet")
async def greet(ctx) -> Evidence:
    target = ctx.inputs.get("target", "world")
    return Evidence(
        files_changed=(),
        commands_run=(f"echo hello {target}",),
        exit_codes=(0,),
        tests_run=0,
        tests_passed=0,
        duration_seconds=0.0,
    )


@workflow(name="hello_e2e")
async def hello_e2e(ctx) -> dict:
    await greet(ctx)
    return {"ok": True, "target": ctx.inputs.get("target", "world")}
'''


# ---------------------------------------------------------------------------
# Streaming LLM that emits tokens in realistic bursts (not all at once).
# This matters because the whole point of the fix is that the chat surface
# receives chunks AS THEY ARRIVE, not after the whole stream completes.
# ---------------------------------------------------------------------------

class _RealisticStreamingLlm:
    """Stub LLM that emits tokens across multiple event-loop ticks.

    Yields each token with an asyncio.sleep(0) between them so the
    consumer's `while not gen_task.done()` loop gets a chance to flush
    captured deltas to the chat surface.
    """

    def __init__(self, parsed: dict, tokens: list[str]) -> None:
        self.parsed = parsed
        self.tokens = tokens
        self.calls: list[dict] = []
        self.stream_calls: list[dict] = []

    async def acomplete_structured(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outer = self

        class _Result:
            text = str(outer.parsed)
            parsed = outer.parsed
        return _Result()

    async def acomplete_stream(self, **kwargs: Any):
        self.stream_calls.append(kwargs)
        outer = self

        class _Chunk:
            def __init__(self, *, delta: str, final: bool = False,
                         parsed: dict | None = None,
                         text: str = "",
                         usage: Any = None) -> None:
                self.delta = delta
                self.final = final
                self.parsed = parsed
                self.text = text
                self.usage = usage

        for t in outer.tokens:
            yield _Chunk(delta=t, final=False)
            await asyncio.sleep(0)   # let the consumer flush
        yield _Chunk(delta="", final=True, parsed=outer.parsed,
                     text="".join(outer.tokens))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def wf_env(tmp_path: Path):
    """Build a runtime + ScriptAuthor pointed at tmp_path/wf.

    Uses the realistic streaming LLM so we can observe per-token flushes.
    """
    runtime = build_runtime(journal_root=tmp_path / "wf")
    # Realistic token stream: the LLM is "thinking" then emits a sequence
    # of tokens that approximate what a real Sonnet/Haiku call returns.
    tokens = [
        "I will ", "create ", "a workflow ", "named ", "`hello_e2e` ",
        "with ", "one step ", "called ", "`greet` ", "that ", "echoes ",
        "the ", "target ",
    ]
    llm = _RealisticStreamingLlm(
        parsed={
            "name": "hello_e2e",
            "description": "hello world e2e test",
            "script": EXECUTABLE_SCRIPT,
            "step_names": ["greet"],
        },
        tokens=tokens,
    )
    author = ScriptAuthor(
        llm=llm, library_root=tmp_path / "wf" / "library",
    )
    return runtime, author, tmp_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestWorkflowCreateEndToEnd:
    """The full /workflow create → saved workflow → executed workflow loop."""

    def test_handler_returns_async_generator_not_string(
        self, wf_env: tuple
    ) -> None:
        """Pitfall #23 regression guard: handler MUST return AsyncIterator[str]."""
        runtime, author, _tmp = wf_env
        result = _dispatch_workflow(runtime, "create hello world", script_author=author)
        assert hasattr(result, "__aiter__"), (
            "_dispatch_workflow must return AsyncIterator[str] for /workflow "
            f"create (got {type(result).__name__}). The whole point of the "
            "streaming-slash fix is that token deltas land on the chat "
            "surface incrementally."
        )
        # Crucially, NOT a coroutine — that would be the old broken path
        # where the whole generation completed before any chat message.
        assert not asyncio.iscoroutine(result)

    def test_chat_surface_receives_multiple_incremental_messages(
        self, wf_env: tuple
    ) -> None:
        """The chat surface must see N>1 messages, not one opaque blob."""
        runtime, author, _tmp = wf_env
        chat = FakeChatSurface()
        result = _dispatch_workflow(runtime, "create hello world", script_author=author)

        coalesced = asyncio.run(chat.dispatch(result))

        assert len(chat.messages) > 1, (
            f"Expected incremental chat messages, got {len(chat.messages)}. "
            "The streaming-slash fix did not land."
        )
        # Sanity: we got many messages, not the old 1-message behavior.
        # With 12 LLM tokens + 1 starting indicator + 1 final card = at
        # least 13 messages. Allow some slack for coalescing.
        assert len(chat.messages) >= 5, (
            f"Expected ≥5 incremental messages, got {len(chat.messages)}"
        )

    def test_first_message_arrives_before_first_token(
        self, wf_env: tuple
    ) -> None:
        """The user must see activity BEFORE the LLM's first token.

        The starting-indicator yield ("🔨 generating...") is what
        prevents the dead-air window the user complained about.
        """
        runtime, author, _tmp = wf_env
        chat = FakeChatSurface()
        result = _dispatch_workflow(runtime, "create hello world", script_author=author)
        asyncio.run(chat.dispatch(result))

        # First message must be the starting indicator.
        first_msg = chat.messages[0][1]
        assert "🔨" in first_msg or "generating" in first_msg.lower(), (
            f"First chat message must be the starting indicator, got: "
            f"{first_msg!r}"
        )

    def test_token_deltas_are_visible_to_chat_surface(
        self, wf_env: tuple
    ) -> None:
        """The LLM's tokens must surface to the chat — not just the final card."""
        runtime, author, _tmp = wf_env
        chat = FakeChatSurface()
        result = _dispatch_workflow(runtime, "create hello world", script_author=author)
        asyncio.run(chat.dispatch(result))

        # Concatenate all non-starting, non-final messages and check the
        # LLM's streamed content is in there. The tokens include "I will"
        # and "create" which should appear in some incremental message.
        all_text = "".join(text for _t, text in chat.messages)
        assert "I will" in all_text, (
            "LLM token deltas were not flushed to the chat surface. "
            "Token streaming is broken or coalescing too aggressively."
        )
        assert "create" in all_text, (
            "Expected to see LLM tokens on chat surface, only saw: "
            f"{all_text[:200]!r}"
        )

    def test_final_message_is_artifact_card(
        self, wf_env: tuple
    ) -> None:
        """The final chunk must be the artifact card with run_id + path + body."""
        runtime, author, _tmp = wf_env
        chat = FakeChatSurface()
        result = _dispatch_workflow(runtime, "create hello world", script_author=author)
        asyncio.run(chat.dispatch(result))

        final_msg = chat.messages[-1][1]
        assert "✅" in final_msg, f"Final card missing ✅ marker: {final_msg!r}"
        assert "run_id=" in final_msg, "Final card missing run_id"
        assert "za_" in final_msg, "Final card missing za_ run_id prefix"
        assert "script saved at" in final_msg, "Final card missing script path"
        assert "```python" in final_msg, "Final card missing inline body preview"
        assert "follow with `/workflow status" in final_msg, (
            "Final card missing follow-up /workflow status pointer"
        )

    def test_first_token_visible_within_500ms_of_handler_invoke(
        self, wf_env: tuple
    ) -> None:
        """Time-to-first-byte for chat must be sub-second.

        Before the fix: 6+ seconds of dead air then 1 blob.
        After the fix: starting indicator immediately, then tokens
        streamed live.
        """
        runtime, author, _tmp = wf_env
        chat = FakeChatSurface()
        result = _dispatch_workflow(runtime, "create hello world", script_author=author)

        # Time the dispatch (this is wallclock for the whole stream).
        t0 = time.monotonic()
        asyncio.run(chat.dispatch(result))
        elapsed = time.monotonic() - t0

        # The first message timestamp relative to the dispatch start.
        first_msg_t = chat.messages[0][0]
        assert first_msg_t < 0.5, (
            f"First chat message took {first_msg_t:.3f}s — user is staring "
            "at a spinner. The starting indicator must land first."
        )
        # Total streaming time is bounded by the LLM stub's `sleep(0)`
        # between tokens — should be well under 2s.
        assert elapsed < 3.0, (
            f"Whole streaming dispatch took {elapsed:.3f}s — too slow for "
            "a 12-token stub LLM. The async-iterator branch is blocking."
        )

    def test_workflow_file_actually_created_on_disk(
        self, wf_env: tuple
    ) -> None:
        """After the dispatch, the .py file must exist in the library."""
        runtime, author, tmp_path = wf_env
        chat = FakeChatSurface()
        result = _dispatch_workflow(runtime, "create hello world", script_author=author)
        coalesced = asyncio.run(chat.dispatch(result))

        # Extract script path from the artifact card.
        m = re.search(r"script saved at (\S+)", coalesced or "")
        assert m, f"Could not parse script path from artifact card: {coalesced!r}"
        script_path = Path(m.group(1))
        assert script_path.exists(), (
            f"Artifact card says script is at {script_path} but the file "
            "does not exist on disk."
        )
        # The file content must match what we told the LLM to generate.
        assert script_path.read_text().strip() == EXECUTABLE_SCRIPT.strip(), (
            "On-disk script content differs from what the LLM supposedly "
            "generated."
        )

    def test_library_entry_registered(
        self, wf_env: tuple
    ) -> None:
        """The library.json must have an entry for the new workflow."""
        runtime, author, tmp_path = wf_env
        library_root = tmp_path / "wf" / "library"
        chat = FakeChatSurface()
        result = _dispatch_workflow(runtime, "create hello world", script_author=author)
        asyncio.run(chat.dispatch(result))

        library_json = library_root / "library.json"
        assert library_json.exists(), (
            f"Expected {library_json} to exist after a successful create, "
            "but it does not."
        )
        data = json.loads(library_json.read_text())
        # Library JSON shape: {"version": 1, "entries": [{"name": ..., ...}, ...]}
        entries = data.get("entries", [])
        names = [e.get("name") for e in entries]
        assert "hello_e2e" in names, (
            f"Expected 'hello_e2e' in library.json entries, got: {names}"
        )
        entry = next(e for e in entries if e.get("name") == "hello_e2e")
        assert "path" in entry, f"Library entry missing 'path' key: {entry}"
        # The path stored is relative (e.g. "hello_e2e.py"); resolve against
        # the library_root which is where the actual file lives.
        entry_path = library_root / entry["path"]
        assert entry_path.exists(), (
            f"Library entry says workflow is at {entry['path']!r} but the "
            f"file does not exist at {entry_path}"
        )

    def test_za_run_id_alias_resolves_to_real_run(
        self, wf_env: tuple
    ) -> None:
        """The /workflow status command must resolve za_xxx to r_xxx via alias.

        This is the alias bridge that lets the slash surface round-trip
        back to the journal files written by the runtime.
        """
        runtime, author, tmp_path = wf_env
        chat = FakeChatSurface()
        result = _dispatch_workflow(runtime, "create hello world", script_author=author)
        coalesced = asyncio.run(chat.dispatch(result))

        m = re.search(r"run_id=(za_\w+)", coalesced or "")
        assert m, f"Could not parse za_ run_id from: {coalesced!r}"
        za_run_id = m.group(1)

        # The alias file must exist in the journal root.
        alias_path = tmp_path / "wf" / f"{za_run_id}.alias"
        assert alias_path.exists(), (
            f"Alias file {alias_path} missing. The /workflow status bridge "
            "to journal files is broken."
        )
        # The alias file must point at a real r_ run_id.
        real_run_id = alias_path.read_text().strip()
        assert real_run_id.startswith("r_"), (
            f"Alias file should contain r_ run_id, got: {real_run_id!r}"
        )

        # /workflow status <za_run_id> must round-trip via the alias bridge.
        # Before the fix, this returned "unknown run_id" because the
        # slash surface and journal root were disconnected.
        status_result = _dispatch_workflow(
            runtime, f"status {za_run_id}", script_author=author,
        )
        assert status_result is not None, (
            f"/workflow status {za_run_id} returned None — alias "
            "resolution failed entirely."
        )
        assert "unknown run_id" not in status_result.lower(), (
            f"/workflow status {za_run_id} returned the legacy "
            f"'unknown run_id' error: {status_result!r}"
        )
        # Must reference the za_ id itself in the status output (proves
        # the alias resolution took, not just a fallback message).
        assert za_run_id in status_result, (
            f"/workflow status output should mention {za_run_id}, got: "
            f"{status_result!r}"
        )

    def test_generated_workflow_actually_executes(
        self, wf_env: tuple
    ) -> None:
        """The saved workflow must be loadable + executable via the runtime.

        Proves the LLM-generated Python is not just parseable but
        semantically valid in the workflow DSL (correct @step/@workflow
        decorators, Evidence return shape, ctx handling).
        """
        runtime, author, tmp_path = wf_env
        chat = FakeChatSurface()
        result = _dispatch_workflow(runtime, "create hello world", script_author=author)
        coalesced = asyncio.run(chat.dispatch(result))

        m = re.search(r"script saved at (\S+)", coalesced or "")
        script_path = Path(m.group(1))

        # Load the generated module dynamically.
        import importlib.util
        import sys
        spec = importlib.util.spec_from_file_location("hello_e2e_gen", script_path)
        assert spec is not None and spec.loader is not None, (
            f"importlib could not build a spec for {script_path}"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules["hello_e2e_gen"] = module
        spec.loader.exec_module(module)

        # The workflow function must be importable.
        assert hasattr(module, "hello_e2e"), (
            "Generated module missing hello_e2e workflow function"
        )
        workflow_fn = module.hello_e2e

        # Submit it to the runtime.
        async def _run_it() -> tuple[str, Any]:
            run_id = await runtime.submit(workflow_fn, {"target": "Maple"})
            run = runtime.get_run(run_id)
            await run.task
            return run_id, run

        run_id, run = asyncio.run(_run_it())
        # The run must have completed successfully. Compare against
        # the enum VALUE ("done"), not the str() repr ("RunState.DONE").
        state_value = (
            run.state.value
            if hasattr(run.state, "value")
            else str(run.state)
        )
        assert state_value in ("done", "completed"), (
            f"Generated workflow did not complete: state={run.state} "
            f"(value={state_value!r}), "
            f"error={getattr(run, 'error', None)}"
        )
        # The greet step must have run and recorded `commands_run` evidence.
        from plugins.hermes_workflow.dsl.types import Evidence
        greet_ev = run.completed_steps.get("greet")
        assert isinstance(greet_ev, Evidence), (
            f"Expected greet step to have Evidence output, got: "
            f"{run.completed_steps}"
        )
        # The Evidence recorded the echo command with our target.
        assert any("Maple" in cmd for cmd in greet_ev.commands_run), (
            f"Evidence.commands_run did not record our input target: "
            f"{greet_ev.commands_run}"
        )
        # The workflow's return value must have made it back. The Run
        # class stashes the workflow fn's return value on the task's
        # result (Run.result is for the runtime's own bookkeeping).
        task_result = run.task.result()
        assert task_result is not None, "run.task.result() was None"
        assert task_result.get("ok") is True
        assert task_result.get("target") == "Maple"

    def test_preexisting_notifier_still_receives_all_events(
        self, wf_env: tuple
    ) -> None:
        """If a notifier was attached BEFORE /workflow create ran, it must
        still receive the full event sequence (notifier chaining, not
        replacement).

        This guards the additive-notifier contract — a notifier attached
        by the journal subsystem or statusbar must not be silently
        disconnected by our streaming handler.
        """
        runtime, author, _tmp = wf_env

        # Attach an external notifier BEFORE the slash handler runs.
        external_events: list[tuple[str, dict]] = []

        def _external_notifier(kind: str, **payload: Any) -> None:
            external_events.append((kind, payload))

        author.notifier = _external_notifier

        chat = FakeChatSurface()
        result = _dispatch_workflow(runtime, "create hello world", script_author=author)
        asyncio.run(chat.dispatch(result))

        # The external notifier must have received the full sequence:
        # stage_started events for llm_call/safety_check/save/
        # graph_validation/submit, plus token events, plus
        # stage_completed events, plus artifact_posted.
        kinds_seen = {k for k, _ in external_events}
        assert "stage_started" in kinds_seen, (
            f"External notifier did not receive stage_started events. "
            f"Seen: {kinds_seen}"
        )
        assert "token" in kinds_seen, (
            f"External notifier did not receive token events. "
            f"Seen: {kinds_seen}"
        )
        assert "stage_completed" in kinds_seen, (
            f"External notifier did not receive stage_completed events. "
            f"Seen: {kinds_seen}"
        )
        assert "artifact_posted" in kinds_seen, (
            f"External notifier did not receive artifact_posted. "
            f"Seen: {kinds_seen}. The artifact_posted emission is the "
            "fix for the streamer not knowing the file landed."
        )

        # Sanity: at least 5 token events from the 12-token stream.
        token_events = [p for k, p in external_events if k == "token"]
        assert len(token_events) >= 5, (
            f"Expected ≥5 token events, got {len(token_events)}"
        )

        # And the script_author's notifier attribute must be restored
        # back to the external one after the handler completes (so
        # the next /workflow create reuses our chaining rather than
        # leaking the closure).
        assert author.notifier is _external_notifier, (
            "Handler did not restore the original notifier. Next "
            "/workflow create will double-notify or skip the external."
        )
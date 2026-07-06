"""Tests for ScriptAuthor streaming + stage visibility.

These verify that ``ScriptAuthor.generate()`` emits stage-by-stage
notifier events, that it consumes ``acomplete_stream`` and emits token
events along the way, and that the slash surface extension emits a
final ``artifact_posted`` event when the script body lands in the
library.

Stages emitted by ScriptAuthor:
``llm_call`` → ``safety_check`` → ``save`` → ``graph_validation`` → ``submit``.

Notifier contract: ``notifier(kind: str, **payload)``.

Emitter kinds used in this file:
- ``stage_started``     → at entry of each stage
- ``stage_completed``   → at exit of each stage on success path
- ``stage_failed``      → at exit of each stage on failure path
- ``token``             → one call per LLM text chunk (non-final)
- ``llm_completed``     → once, after the final LLM chunk
"""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path
from typing import Any

import pytest

from plugins.hermes_workflow.runtime_factory import build_runtime
from plugins.hermes_workflow.script_author import ScriptAuthor


VALID_SCRIPT = textwrap.dedent("""
    from plugins.hermes_workflow import step, workflow, Evidence

    def _ev():
        return Evidence(files_changed=(), commands_run=(), exit_codes=(),
                       tests_run=0, tests_passed=0, duration_seconds=0.0)

    @step(name="only")
    async def only(ctx) -> Evidence:
        return _ev()

    @workflow(name="demo")
    async def demo(ctx) -> dict:
        await only(ctx)
        return {"ok": True}
""")


# ---------------------------------------------------------------------------
# Test scaffolding
# ---------------------------------------------------------------------------

class _NotificationCapture:
    """Appends ``(kind, payload)`` tuples into a list for assertion."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def __call__(self, kind: str, **payload: Any) -> None:
        self.events.append((kind, payload))


class _CannedStreamingLlm:
    """Stub that returns a structured result via the streaming protocol.

    Each ``acomplete_stream`` invocation yields the configured chunks
    then a final ``final=True`` chunk carrying ``parsed``.
    """

    def __init__(self, parsed: dict | None = None, *,
                 chunks: list[str] | None = None,
                 raise_exc: Exception | None = None) -> None:
        self.parsed = parsed or {}
        self.chunks = chunks or []
        self.raise_exc = raise_exc
        self.calls: list[dict] = []
        self.stream_calls: list[dict] = []

    async def acomplete_structured(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.raise_exc is not None:
            raise self.raise_exc
        outer = self

        class _Result:
            text = str(outer.parsed)
            parsed = outer.parsed
        return _Result()

    async def acomplete_stream(self, **kwargs: Any):
        self.stream_calls.append(kwargs)
        if self.raise_exc is not None:
            raise self.raise_exc
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
        for c in outer.chunks:
            yield _Chunk(delta=c, final=False)
        yield _Chunk(delta="", final=True, parsed=outer.parsed,
                     text="".join(outer.chunks))


# ---------------------------------------------------------------------------
# Task 1: stage_started / stage_completed per stage on happy path
# ---------------------------------------------------------------------------

class TestScriptAuthorStageVisibility:
    def _setup(self, tmp_path: Path):
        llm = _CannedStreamingLlm(parsed={
            "name": "demo",
            "description": "demo workflow",
            "script": VALID_SCRIPT,
            "step_names": ["only"],
        })
        runtime = build_runtime(journal_root=tmp_path / "wf")
        return (
            ScriptAuthor(llm=llm, library_root=tmp_path / "wf" / "library"),
            runtime,
            llm,
        )

    def test_script_author_emits_one_notice_per_stage_on_happy_path(
        self, tmp_path,
    ):
        """On a happy-path generate(), the notifier receives one
        ``stage_started`` + ``stage_completed`` pair per stage, in
        order. The token events emitted inside ``llm_call`` may
        interleave between the pair boundaries but never across stage
        boundaries."""
        capture = _NotificationCapture()
        author, runtime, _llm = self._setup(tmp_path)
        author.notifier = capture    # type: ignore[attr-defined]

        asyncio.run(author.generate(
            intent="create a demo workflow", runtime=runtime,
        ))

        # All 10 stage events fire in paired order, with the
        # llm_completed emitted by _call_llm appearing between
        # stage_started(llm_call) and stage_completed(llm_call).
        stage_events = [p for (k, p) in capture.events
                         if k in ("stage_started", "stage_completed")]
        assert [p["stage"] for p in stage_events] == [
            "llm_call", "llm_call",
            "safety_check", "safety_check",
            "save", "save",
            "graph_validation", "graph_validation",
            "submit", "submit",
        ]
        # Completion payloads mark ok=True.
        completed = [p for (k, p) in capture.events if k == "stage_completed"]
        assert all(p.get("ok") is True for p in completed)


# ---------------------------------------------------------------------------
# Task 2: failure modes emit a final stage_failed with the matching stage
# ---------------------------------------------------------------------------

class TestScriptAuthorFailureVisibility:
    def _setup(self, tmp_path: Path, *, parsed=None, raise_exc=None,
                chunks=None):
        llm = _CannedStreamingLlm(
            parsed=parsed, chunks=chunks, raise_exc=raise_exc,
        )
        runtime = build_runtime(journal_root=tmp_path / "wf")
        return (
            ScriptAuthor(llm=llm, library_root=tmp_path / "wf" / "library"),
            runtime,
            llm,
        )

    def test_llm_failure_emits_terminal_notice_with_stage_and_error(
        self, tmp_path,
    ):
        """LLM failure: llm_call stage_started then stage_failed, no
        downstream stages."""
        capture = _NotificationCapture()
        author, runtime, _ = self._setup(
            tmp_path, raise_exc=RuntimeError("rate limit"),
        )
        author.notifier = capture    # type: ignore[attr-defined]

        result = asyncio.run(author.generate(intent="x", runtime=runtime))

        assert not result.ok
        assert result.error_stage == "llm_call"

        kinds = [k for (k, _) in capture.events]
        assert kinds == ["stage_started", "stage_failed"]
        failure = [p for (k, p) in capture.events if k == "stage_failed"]
        assert failure == [{"stage": "llm_call", "error": "rate limit"}]

    def test_safety_check_failure_emits_terminal_notice(self, tmp_path):
        """Safety-check failure: llm_call ok, then stage_failed."""
        bad_script = (
            "import subprocess\n"
            "@workflow(name='bad')\nasync def bad(ctx): pass\n"
        )
        capture = _NotificationCapture()
        author, runtime, _ = self._setup(tmp_path, parsed={
            "name": "bad", "description": "x",
            "script": bad_script, "step_names": [],
        })
        author.notifier = capture    # type: ignore[attr-defined]

        result = asyncio.run(author.generate(intent="x", runtime=runtime))

        assert not result.ok
        assert result.error_stage == "safety_check"
        kinds = [k for (k, _) in capture.events]
        # llm_call stream yields terminal chunk + stage_completed,
        # then safety_check fails.
        assert kinds == [
            "stage_started",                 # llm_call start
            "llm_completed",                 # terminal chunk (no body chunks)
            "stage_completed",               # llm_call ok
            "stage_started",   "stage_failed",  # safety_check failed
        ]
        failure = [p for (k, p) in capture.events if k == "stage_failed"]
        assert failure[0]["stage"] == "safety_check"
        assert "subprocess" in failure[0]["error"]

    def test_submit_failure_emits_terminal_notice(self, tmp_path):
        """Submit failure: all prior stages succeed, then submit fails."""
        bad_script = textwrap.dedent("""
            from plugins.hermes_workflow import step, workflow, Evidence
            def _ev():
                return Evidence(files_changed=(), commands_run=(),
                               exit_codes=(), tests_run=0, tests_passed=0,
                               duration_seconds=0.0)
            @step(name="only", depends_on=("does_not_exist",))
            async def only(ctx) -> Evidence:
                return _ev()
            @workflow(name="demo")
            async def demo(ctx) -> dict:
                await only(ctx)
                return {}
        """)
        capture = _NotificationCapture()
        author, runtime, _ = self._setup(tmp_path, parsed={
            "name": "demo", "description": "x",
            "script": bad_script, "step_names": ["only"],
        })
        author.notifier = capture    # type: ignore[attr-defined]

        result = asyncio.run(author.generate(intent="x", runtime=runtime))

        assert not result.ok
        assert result.error_stage == "submit"
        failures = [p for (k, p) in capture.events if k == "stage_failed"]
        assert len(failures) == 1
        assert failures[0]["stage"] == "submit"
        assert "depends on unknown step" in failures[0]["error"]
        completed = [p for (k, p) in capture.events
                      if k == "stage_completed"]
        assert [p["stage"] for p in completed] == [
            "llm_call", "safety_check", "save", "graph_validation",
        ]


# ---------------------------------------------------------------------------
# Task 4: ScriptAuthor consumes acomplete_stream and emits token events
# ---------------------------------------------------------------------------

class TestScriptAuthorStreamsLlmResponse:
    def test_script_author_emits_one_token_per_chunk_then_llm_completed(
        self, tmp_path,
    ):
        """ScriptAuthor consumes acomplete_stream and emits one
        ``token`` callback per chunk followed by one ``llm_completed``
        carrying the parsed value."""
        chunks = ["hello ", "world", " from LLM"]
        capture = _NotificationCapture()
        llm = _CannedStreamingLlm(parsed={
            "name": "demo",
            "description": "demo workflow",
            "script": VALID_SCRIPT,
            "step_names": ["only"],
        }, chunks=chunks)
        runtime = build_runtime(journal_root=tmp_path / "wf")
        author = ScriptAuthor(
            llm=llm, library_root=tmp_path / "wf" / "library",
        )
        author.notifier = capture    # type: ignore[attr-defined]

        asyncio.run(author.generate(
            intent="create a demo workflow", runtime=runtime,
        ))

        token_events = [p for (k, p) in capture.events if k == "token"]
        llm_completed = [p for (k, p) in capture.events
                          if k == "llm_completed"]

        assert len(token_events) == 3
        assert [p["delta"] for p in token_events] == chunks
        assert all(p.get("stage") == "llm_call" for p in token_events)
        assert len(llm_completed) == 1
        assert llm_completed[0]["chars"] == sum(len(c) for c in chunks)
        assert llm_completed[0]["parsed"]["name"] == "demo"

    def test_script_author_prefers_acomplete_stream_over_acomplete_structured(
        self, tmp_path,
    ):
        """When the LLM exposes both, ScriptAuthor calls
        acomplete_stream (not acomplete_structured)."""
        chunks = ["only-one-chunk"]
        capture = _NotificationCapture()

        class DualLlm:
            def __init__(self):
                self.stream_calls = 0
                self.structured_calls = 0

            async def acomplete_stream(self, **kw):
                self.stream_calls += 1

                class _C:
                    def __init__(self, *, delta, final=False, parsed=None,
                                  text="", usage=None):
                        self.delta, self.final, self.parsed, self.text = (
                            delta, final, parsed, text,
                        )
                for d in chunks:
                    yield _C(delta=d, final=False)
                yield _C(delta="", final=True, parsed={
                    "name": "demo", "description": "x",
                    "script": VALID_SCRIPT, "step_names": ["only"],
                }, text="".join(chunks))

            async def acomplete_structured(self, **kw):
                self.structured_calls += 1
                raise AssertionError(
                    "should not be called when acomplete_stream is present"
                )

        llm = DualLlm()
        runtime = build_runtime(journal_root=tmp_path / "wf")
        author = ScriptAuthor(
            llm=llm, library_root=tmp_path / "wf" / "library",    # type: ignore[arg-type]
        )
        author.notifier = capture    # type: ignore[attr-defined]

        asyncio.run(author.generate(intent="x", runtime=runtime))

        assert llm.stream_calls == 1
        assert llm.structured_calls == 0


# ---------------------------------------------------------------------------
# Task 6: make_script_author_run_id shape
# ---------------------------------------------------------------------------

class TestMakeScriptAuthorRunId:
    def test_id_starts_with_za_prefix(self, tmp_path):
        """Synthesized run_id format: za_<slug>_<8-hex>."""
        from plugins.hermes_workflow.runtime_factory import (
            make_script_author_run_id,
        )

        rid = make_script_author_run_id("hello-world")
        assert rid.startswith("za_hello_world_")
        tail = rid.rsplit("_", 1)[-1]
        assert len(tail) == 8
        int(tail, 16)

    def test_id_slugifies_invalid_characters(self, tmp_path):
        from plugins.hermes_workflow.runtime_factory import (
            make_script_author_run_id,
        )

        rid = make_script_author_run_id("My Workflow!!!")
        # 'My Workflow!!!' -> 'my_workflow'
        assert rid.startswith("za_my_workflow_")
        tail = rid.rsplit("_", 1)[-1]
        assert len(tail) == 8
        int(tail, 16)


# ---------------------------------------------------------------------------
# Task 7: slash._create_via_script_author emits artifact_posted + return string
# ---------------------------------------------------------------------------

class TestSlashCreateViaScriptAuthorArtifactCard:
    def _build(self, tmp_path):
        """Construct a runtime + ScriptAuthor stub wired to a captured
        notifier list. Returns the dispatcher, the notifier, and the
        ScriptAuthor."""
        from plugins.hermes_workflow.script_author import (
            ScriptAuthor as RealScriptAuthor,
        )
        from plugins.hermes_workflow.runtime_factory import build_runtime
        import types

        llm = _CannedStreamingLlm(parsed={
            "name": "demo",
            "description": "demo workflow",
            "script": VALID_SCRIPT,
            "step_names": ["only"],
        }, chunks=["chunk1"])

        # Wrap so we can capture notifier.__set__ if _dispatch_workflow
        # applies the notifier directly to ScriptAuthor.
        capture = _NotificationCapture()
        llm.notifier = capture    # type: ignore[attr-defined]
        sa = RealScriptAuthor(
            llm=llm, library_root=tmp_path / "wf" / "library",
            notifier=capture,
        )
        runtime = build_runtime(journal_root=tmp_path / "wf")
        sa.runtime_ref = runtime   # for test access
        return sa, capture, runtime

    def test_create_via_script_author_emits_artifact_card_with_path_and_body(
        self, tmp_path,
    ):
        """RED for Task 7 — successful generate() emits one
        artifact_posted notifier event carrying path + run_id + body,
        and the slash handler's return string contains the run_id and
        the script path."""
        sa, capture, runtime = self._build(tmp_path)

        # The dispatcher in slash.py doesn't currently take a notifier;
        # test the helper directly until Task 8 wires it.
        from plugins.hermes_workflow.slash import _create_via_script_author

        result_obj = _create_via_script_author(
            runtime, ["greet the user"], sa,
        )
        # The helper now returns an AsyncIterator[str] (streaming fix,
        # 2026-06-30). Drive it to completion and assert the coalesced
        # artifact card carries the run_id and script path.
        assert hasattr(result_obj, "__aiter__"), (
            f"expected async iterator, got {type(result_obj).__name__}"
        )
        async def _drain(obj):
            if not hasattr(obj, "__aiter__"):
                return [str(obj)] if obj is not None else []
            chunks = []
            async for c in obj:
                chunks.append(c)
            return chunks
        chunks = asyncio.run(_drain(result_obj))
        result = "".join(chunks)
        assert "za_demo_" in result
        assert "/tmp" in result or "wf/library/demo.py" in result

        # And the notifier received the artifact_posted event.
        artifact_events = [p for (k, p) in capture.events
                            if k == "artifact_posted"]
        assert len(artifact_events) == 1
        ae = artifact_events[0]
        assert ae["name"] == "demo"
        assert "demo.py" in ae["path"]
        assert ae["run_id"].startswith("za_demo_")
        # The body preview contains the imports (first ~1200 chars).
        assert "from plugins.hermes_workflow import" in ae["body_preview"]


# ---------------------------------------------------------------------------
# Task 8: end-to-end /workflow create produces the full event sequence
# ---------------------------------------------------------------------------

class TestSlashCreateEndToEnd:
    """Final integration test for the streaming shape."""

    def test_full_create_via_slash_emits_full_event_sequence(self, tmp_path):
        """Successful /workflow create produces the full event sequence
        (5 stage_started, 1+ token, 1 llm_completed, 5 stage_completed,
        1 artifact_posted) and the return string contains the run_id
        and the script path."""
        import plugins.hermes_workflow.slash as slash_mod
        from plugins.hermes_workflow.script_author import (
            ScriptAuthor as RealScriptAuthor,
        )
        from plugins.hermes_workflow.runtime_factory import build_runtime

        llm = _CannedStreamingLlm(parsed={
            "name": "demo",
            "description": "demo workflow",
            "script": VALID_SCRIPT,
            "step_names": ["only"],
        }, chunks=["chunk_a", "chunk_b"])

        capture = _NotificationCapture()
        sa = RealScriptAuthor(
            llm=llm,
            library_root=tmp_path / "wf" / "library",
            notifier=capture,
        )
        runtime = build_runtime(journal_root=tmp_path / "wf")

        result_obj = slash_mod._dispatch_workflow(
            runtime, "create greet the user", script_author=sa,
        )

        # Dispatch returns the handler's result — which is now an async
        # generator (streaming fix, 2026-06-30). Drive to completion
        # and verify the final artifact card string.
        assert hasattr(result_obj, "__aiter__"), (
            f"expected async iterator, got {type(result_obj).__name__}"
        )
        async def _drain(obj):
            if not hasattr(obj, "__aiter__"):
                return [str(obj)] if obj is not None else []
            chunks = []
            async for c in obj:
                chunks.append(c)
            return chunks
        chunks = asyncio.run(_drain(result_obj))
        result = "".join(chunks)
        assert "za_demo_" in result
        assert "demo.py" in result

        # Verify the event sequence shape.
        stage_starts = [p for (k, p) in capture.events
                         if k == "stage_started"]
        stage_completes = [p for (k, p) in capture.events
                            if k == "stage_completed"]
        tokens = [p for (k, p) in capture.events if k == "token"]
        llm_complete = [p for (k, p) in capture.events
                          if k == "llm_completed"]
        artifacts = [p for (k, p) in capture.events
                       if k == "artifact_posted"]

        assert len(stage_starts) == 5
        assert len(stage_completes) == 5
        assert [p["stage"] for p in stage_starts] == [
            "llm_call", "safety_check", "save",
            "graph_validation", "submit",
        ]
        assert len(tokens) == 2
        assert [p["delta"] for p in tokens] == ["chunk_a", "chunk_b"]
        assert len(llm_complete) == 1
        assert len(artifacts) == 1


# ---------------------------------------------------------------------------
# Task 10: ScriptAuthor events flow into the runtime dispatcher
#           (live-streaming via GatewayEventDispatcher)
# ---------------------------------------------------------------------------

class TestScriptAuthorLiveDispatcherWiring:
    """Pin the contract that ScriptAuthor events reach a runtime-style
    dispatcher so live users see them in the TUI/desktop statusbar.

    The dispatcher is the callable passed via ``ScriptAuthor(...,
    dispatcher=...)``. Each notifier event is translated via
    ``EventTranslator.translate_script_author_event`` before being
    forwarded to the dispatcher, exactly mirroring how runtime journal
    events get translated and dispatched today.
    """

    def _setup(self, tmp_path):
        from plugins.hermes_workflow.script_author import (
            ScriptAuthor as RealScriptAuthor,
        )
        from plugins.hermes_workflow.runtime_factory import build_runtime

        llm = _CannedStreamingLlm(parsed={
            "name": "demo",
            "description": "demo workflow",
            "script": VALID_SCRIPT,
            "step_names": ["only"],
        })
        runtime = build_runtime(journal_root=tmp_path / "wf")
        return RealScriptAuthor(
            llm=llm,
            library_root=tmp_path / "wf" / "library",
        ), runtime, llm

    def test_dispatcher_receives_stream_events_for_each_stage_transition(
        self, tmp_path,
    ):
        """RED for Task 10 — every stage_started / stage_completed call
        forwards a translated GatewayNotice to the dispatcher."""
        from plugins.hermes_workflow.visibility import (
            EventTranslator, GatewayNotice,
        )

        sa, runtime, _llm = self._setup(tmp_path)
        captured: list = []

        def dispatcher(evt):
            captured.append(evt)

        sa.dispatcher = dispatcher    # type: ignore[attr-defined]
        sa._event_translator = EventTranslator()

        asyncio.run(sa.generate(
            intent="hello world", runtime=runtime,
        ))

        notices = [e for e in captured
                    if isinstance(e, GatewayNotice)
                    and e.kind.startswith("script_author_")]
        kinds = [e.kind for e in notices]
        assert kinds.count("script_author_stage_started") == 5
        assert kinds.count("script_author_stage_completed") == 5

    def test_dispatcher_receives_token_chunks_and_final(self, tmp_path):
        """RED for Task 10 — token chunks stream as ToolCallChunk and
        the terminal llm_completed arrives as ToolCallFinished."""
        from plugins.hermes_workflow.visibility import (
            EventTranslator, ToolCallChunk, ToolCallFinished,
        )
        from plugins.hermes_workflow.script_author import ScriptAuthor
        from plugins.hermes_workflow.runtime_factory import build_runtime

        chunks = ["alpha ", "bravo ", "charlie"]
        llm = _CannedStreamingLlm(parsed={
            "name": "demo",
            "description": "demo workflow",
            "script": VALID_SCRIPT,
            "step_names": ["only"],
        }, chunks=chunks)
        sa = ScriptAuthor(
            llm=llm, library_root=tmp_path / "wf" / "library",
        )
        runtime = build_runtime(journal_root=tmp_path / "wf")

        captured: list = []
        sa.dispatcher = captured.append    # type: ignore[attr-defined]
        sa._event_translator = EventTranslator()

        asyncio.run(sa.generate(
            intent="hello", runtime=runtime,
        ))

        token_events = [e for e in captured
                          if isinstance(e, ToolCallChunk)
                          and e.tool_name == "script_author_llm"]
        final_events = [e for e in captured
                          if isinstance(e, ToolCallFinished)
                          and e.tool_name == "script_author_llm"]
        assert len(token_events) == 3
        assert [e.preview for e in token_events] == chunks
        assert len(final_events) == 1
        assert final_events[0].index == token_events[-1].index

    def test_dispatcher_is_optional(self, tmp_path):
        """RED — without a dispatcher, generation still succeeds and
        the notifier path remains intact (back-compat for tests + CLI
        invocations that don't have a gateway dispatcher)."""
        from plugins.hermes_workflow.script_author import ScriptAuthor

        sa, runtime, _llm = self._setup(tmp_path)
        assert getattr(sa, "dispatcher", None) is None
        result = asyncio.run(sa.generate(
            intent="x", runtime=runtime,
        ))
        assert result.ok


# ---------------------------------------------------------------------------
# Task 11: ScriptAuthor works when acomplete_stream is a coroutine that
#           resolves to an async iterator (real-world provider wrapping).
#
# RED for the 'coroutine' object is not iterable bug.
#
# Real provider wrappers sometimes expose ``acomplete_stream`` as a plain
# ``async def`` whose body is itself ``return await some_async_iter()``,
# so calling it yields a **coroutine** of an async iterator — not an
# async-generator directly. The consumer must ``await`` the call before
# iterating, otherwise it raises::
#
#     TypeError: 'async for' requires an object with __aiter__ method, got coroutine
#
# This regression bit /workflow create in production for the minmax-backed
# LLM bridge (2026-06-30). The existing test stubs declared
# ``async def acomplete_stream(...)`` containing ``yield`` — that shape
# is an async-generator function, so calling it returns an
# ``async_generator`` object, and ``async for ... in async_generator``
# works even without an ``await``. The async-def-coroutine-of-iterator
# shape was never pinned. This test pins both shapes.
# ---------------------------------------------------------------------------

class _CoroutineWrappedStreamingLlm:
    """Stub whose acomplete_stream is a plain async function that
    awaits an inner async generator and returns it — the wrapped
    shape some provider clients use.
    """

    def __init__(
        self, parsed: dict | None = None, *,
        chunks: list[str] | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self.parsed = parsed or {}
        self.chunks = chunks or []
        self.raise_exc = raise_exc
        self.stream_calls: list[dict] = []

    async def acomplete_stream(self, **kwargs: Any):
        self.stream_calls.append(kwargs)
        if self.raise_exc is not None:
            raise self.raise_exc

        class _Chunk:
            def __init__(self, *, delta: str, final: bool = False,
                         parsed: dict | None = None,
                         text: str = "", usage: Any = None) -> None:
                self.delta = delta
                self.final = final
                self.parsed = parsed
                self.text = text
                self.usage = usage

        async def _inner_iter():
            for c in self.chunks:
                yield _Chunk(delta=c, final=False)
            yield _Chunk(
                delta="", final=True, parsed=self.parsed,
                text="".join(self.chunks),
            )

        # Returns a coroutine of an async generator: this is what the
        # consumer must `await` before iterating.
        return _inner_iter()

    async def acomplete_structured(self, **kwargs: Any) -> Any:
        raise AssertionError(
            "acomplete_structured must NOT be called when "
            "acomplete_stream is present (ScriptAuthor prefers stream).",
        )


class TestScriptAuthorCoroutineWrappedStreaming:
    """RED: ScriptAuthor must handle wrapped ``acomplete_stream``
    implementations where the method is a plain async function
    returning an awaitable iterator."""

    def _setup(self, tmp_path, *, chunks=None):
        llm = _CoroutineWrappedStreamingLlm(
            parsed={
                "name": "demo",
                "description": "demo workflow",
                "script": VALID_SCRIPT,
                "step_names": ["only"],
            },
            chunks=chunks or ["hello ", "world"],
        )
        runtime = build_runtime(journal_root=tmp_path / "wf")
        return (
            ScriptAuthor(
                llm=llm,
                library_root=tmp_path / "wf" / "library",
            ),
            runtime,
            llm,
        )

    def test_wrapped_async_stream_does_not_raise_coroutine_not_iterable(
        self, tmp_path,
    ):
        """Driving ScriptAuthor against a coroutine-of-async-iterator
        shaped acomplete_stream must NOT raise the
        'coroutine object is not iterable' TypeError. The consumer
        must await the call before iterating.
        """
        capture = _NotificationCapture()
        author, runtime, _llm = self._setup(tmp_path)

        # Notifier assignment matches the existing test pattern.
        author.notifier = capture    # type: ignore[attr-defined]

        # On the buggy consumer this raises TypeError immediately;
        # after the fix it succeeds and emits the full event sequence.
        result = asyncio.run(author.generate(
            intent="create a demo workflow", runtime=runtime,
        ))

        assert result.ok, (
            f"ScriptAuthor.generate must succeed on a wrapped "
            f"acomplete_stream; got error_stage={result.error_stage!r} "
            f"error={result.error!r}"
        )

        # The token events should have fired one per chunk.
        token_events = [p for (k, p) in capture.events if k == "token"]
        assert [p["delta"] for p in token_events] == ["hello ", "world"]

        # And the llm_completed event carries the parsed dict.
        llm_completed = [p for (k, p) in capture.events
                         if k == "llm_completed"]
        assert len(llm_completed) == 1
        assert llm_completed[0]["parsed"]["name"] == "demo"

    def test_doubly_wrapped_async_stream_is_peeled_recursively(self, tmp_path):
        """REGRESSION: acomplete_stream may return a coroutine of a
        coroutine (rare — happens when the LLM surface wraps a
        wrapped surface). The script_author's peel loop must handle
        this without leaking a coroutine to ``async for``.

        Pin: an LLM whose acomplete_stream is ``async def`` whose
        body is ``return inner.acomplete_stream(...)`` where
        ``inner.acomplete_stream`` is itself a coroutine-of-async-iter
        must work end-to-end through ScriptAuthor.
        """
        parsed = {
            "name": "demo",
            "description": "demo workflow",
            "script": VALID_SCRIPT,
            "step_names": ["only"],
        }

        class _Chunk:
            def __init__(self, *, delta="", final=False, parsed=None,
                         text="", usage=None):
                self.delta = delta
                self.final = final
                self.parsed = parsed
                self.text = text
                self.usage = usage

        async def _inner_iter():
            yield _Chunk(delta="alpha ", final=False)
            yield _Chunk(delta="beta", final=False)
            yield _Chunk(delta="", final=True, parsed=parsed,
                         text="alpha beta")

        class _Inner:
            """An inner LLM whose acomplete_stream is a coroutine of
            an async iter (the wrapped-shape)."""

            async def acomplete_stream(self, **kwargs):
                return _inner_iter()

        class _Outer:
            """An outer LLM that delegates to the inner. This creates
            a coroutine-of-coroutine (calling outer.acomplete_stream
            returns a coroutine; awaiting it returns another coroutine
            from inner.acomplete_stream; awaiting THAT returns the
            async_iter)."""

            def __init__(self):
                self.inner = _Inner()
                self.calls = 0

            async def acomplete_stream(self, **kwargs):
                self.calls += 1
                # Just delegate — the return is a coroutine resolving
                # to another coroutine resolving to the async iter.
                return self.inner.acomplete_stream(**kwargs)

        runtime = build_runtime(journal_root=tmp_path / "wf")
        sa = ScriptAuthor(llm=_Outer(),    # type: ignore[arg-type]
                          library_root=tmp_path / "wf" / "library")

        result = asyncio.run(sa.generate(
            intent="doubly wrapped test", runtime=runtime,
        ))

        assert result.ok, (
            f"doubly-wrapped acomplete_stream must succeed; got "
            f"error_stage={result.error_stage!r} error={result.error!r}"
        )
        assert result.name == "demo"


# ---------------------------------------------------------------------------
# Slash-handler streaming: /workflow create <intent> should yield chunks to
# the dispatcher fan-out as the LLM streams, and finalize with the artifact
# card. This is the user-visible fix for the "opaque success string" bug.
# ---------------------------------------------------------------------------


class _StreamingHandlerChunkCapture:
    """Drives the slash dispatch through the gateway simulator
    (``asyncio.iscoroutine`` + ``hasattr(__aiter__)`` branch) and records
    every chunk that would be sent to the chat surface."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.coalesced_final: str | None = None

    async def simulate_dispatch(self, handler_result):
        """Mirror the gateway dispatcher's branch logic exactly."""
        import asyncio as _aio
        if _aio.iscoroutine(handler_result):
            handler_result = await handler_result
        if hasattr(handler_result, "__aiter__"):
            chunks = []
            async for chunk in handler_result:
                if chunk is None:
                    continue
                chunks.append(chunk)
                # Each yield = one chat-surface message in production.
                self.messages.append(chunk)
            self.coalesced_final = "".join(chunks) or None
            return self.coalesced_final
        if handler_result is None:
            return None
        self.messages.append(str(handler_result))
        self.coalesced_final = str(handler_result)
        return self.coalesced_final


class TestSlashHandlerStreaming:
    """The slash handler must support async-iterator yields so each LLM
    token lands as a discrete chat message instead of one opaque string.

    These tests pin the contract before any code change so the fix can be
    verified incrementally. They mirror the real gateway dispatcher's
    branch logic in ``gateway/run.py:9017-9020`` so the behavior matches
    production exactly.
    """

    def _setup(self, tmp_path):
        runtime = build_runtime(journal_root=tmp_path / "wf")

        class _Chunk:
            def __init__(self, *, delta="", final=False, parsed=None,
                         text=""):
                self.delta = delta
                self.final = final
                self.parsed = parsed
                self.text = text

        return runtime, _Chunk

    def test_handler_yielding_chunks_produces_incremental_messages(
        self, tmp_path,
    ):
        """An async-generator slash handler should produce N incremental
        messages during LLM streaming, then a final message, instead of
        one opaque final string.

        This is the user-visible fix: chat users see tokens appear as
        they arrive instead of staring at a spinner for 6+ seconds.
        """
        runtime, _Chunk = self._setup(tmp_path)

        async def streaming_handler(raw: str):
            # Mimic what _create_via_script_author will do: yield
            # in-progress deltas, then yield the final artifact card.
            yield "🔨 generating script...\n"
            for piece in ["from plugins.hermes_workflow import step",
                          ", workflow, Evidence\n",
                          "@step(name='only')\n",
                          "async def only(ctx): ..."]:
                yield piece
                await asyncio.sleep(0)
            yield "\n✅ done. saved to /tmp/x.py"

        cap = _StreamingHandlerChunkCapture()
        result = asyncio.run(cap.simulate_dispatch(streaming_handler("")))

        # User sees 6 incremental chat messages, not one opaque blob.
        assert len(cap.messages) == 6, (
            f"expected 6 incremental messages, got {len(cap.messages)}: "
            f"{cap.messages!r}"
        )
        # Final coalesced string reconstructs the full output.
        assert cap.coalesced_final is not None
        assert "from plugins.hermes_workflow" in cap.coalesced_final
        assert "✅ done" in cap.coalesced_final
        # And the dispatcher's return value matches what /workflow
        # status would query against.
        assert result is not None
        assert "✅ done" in result

    def test_sync_string_handler_still_works(self, tmp_path):
        """Existing sync str | None handlers must NOT regress."""
        runtime, _Chunk = self._setup(tmp_path)

        def sync_handler(raw: str) -> str:
            return f"sync-ok:{raw}"

        cap = _StreamingHandlerChunkCapture()
        result = asyncio.run(cap.simulate_dispatch(sync_handler("hello")))

        assert result == "sync-ok:hello"
        assert cap.messages == ["sync-ok:hello"]

    def test_async_one_shot_handler_still_works(self, tmp_path):
        """Existing async def handlers (no yield) must NOT regress."""
        runtime, _Chunk = self._setup(tmp_path)

        async def async_one_shot(raw: str) -> str:
            return f"async-ok:{raw}"

        cap = _StreamingHandlerChunkCapture()
        result = asyncio.run(cap.simulate_dispatch(async_one_shot("hello")))

        assert result == "async-ok:hello"
        assert cap.messages == ["async-ok:hello"]

    def test_handler_returning_none_emits_no_messages(self, tmp_path):
        """Returning None = silent no-op. Must NOT regress."""
        runtime, _Chunk = self._setup(tmp_path)

        async def null_handler(raw: str):
            return None

        cap = _StreamingHandlerChunkCapture()
        result = asyncio.run(cap.simulate_dispatch(null_handler("")))

        assert result is None
        assert cap.messages == []

    def test_handler_yielding_none_skips_silently(self, tmp_path):
        """Yields of None must be filtered out (no chat artifact spam)."""
        runtime, _Chunk = self._setup(tmp_path)

        async def sparse_handler(raw: str):
            yield "first "
            yield None    # heartbeat: no visible message
            yield "second"
            yield None
            yield "third"

        cap = _StreamingHandlerChunkCapture()
        result = asyncio.run(cap.simulate_dispatch(sparse_handler("")))

        assert cap.messages == ["first ", "second", "third"]
        assert cap.coalesced_final == "first secondthird"

    def test_script_author_generate_produces_streaming_slash_output(
        self, tmp_path,
    ):
        """END-TO-END: ScriptAuthor.generate() called via an async-iter
        slash handler must surface LLM tokens as incremental chat
        messages, not one opaque blob.

        This is the real-world shape after Fix 1 + Fix 2 land: the slash
        handler wraps script_author.generate() in an async generator
        that yields notifier deltas + the final artifact card.
        """
        # Build a stub LLM that streams deterministically.
        parsed = {
            "name": "demo",
            "description": "streamed demo",
            "script": VALID_SCRIPT,
            "step_names": ["only"],
        }

        class _Chunk:
            def __init__(self, *, delta="", final=False, parsed=None,
                         text=""):
                self.delta = delta
                self.final = final
                self.parsed = parsed
                self.text = text

        chunks_emitted = []

        class _StubLlm:
            async def acomplete_stream(self, **kwargs):
                for piece in ["from ", "plugins.hermes_workflow ",
                              "import ", "step"]:
                    chunks_emitted.append(piece)
                    yield _Chunk(delta=piece)
                    await asyncio.sleep(0)
                yield _Chunk(
                    delta="",
                    final=True,
                    parsed=parsed,
                    text="from plugins.hermes_workflow import step",
                )
            async def acomplete_structured(self, **kwargs):
                # Should NOT be called when acomplete_stream is present.
                raise AssertionError(
                    "acomplete_structured must not be called "
                    "when acomplete_stream is available"
                )

        runtime = build_runtime(journal_root=tmp_path / "wf")
        sa = ScriptAuthor(llm=_StubLlm(),
                          library_root=tmp_path / "wf" / "library")

        # Mirror _create_via_script_author with the new streaming shape.
        async def streaming_slash_handler(raw: str):
            notifier_chunks = []

            def notifier(kind, **payload):
                if kind == "token":
                    notifier_chunks.append(payload.get("delta", ""))

            sa.notifier = notifier
            # Yield a "starting" indicator so the user sees activity.
            yield f"🔨 generating workflow from: {raw!r}\n"

            result = await sa.generate(
                intent="build a tiny demo workflow", runtime=runtime,
            )
            # Yield each captured token as it arrived.
            for delta in notifier_chunks:
                yield delta
                await asyncio.sleep(0)

            if result.ok:
                yield (f"\n✅ generated {result.name!r}, "
                       f"run_id={result.run_id}\n"
                       f"script saved at {result.script_path}")
            else:
                yield (f"\n❌ ScriptAuthor failed at stage="
                       f"{result.error_stage!r}: {result.error}")

        cap = _StreamingHandlerChunkCapture()
        result = asyncio.run(cap.simulate_dispatch(
            streaming_slash_handler("build a tiny demo workflow")))

        # Token chunks arrived during streaming (proves they're from
        # the LLM, not the final-string path).
        streamed_token_msgs = [m for m in cap.messages
                                if m in chunks_emitted]
        assert len(streamed_token_msgs) >= 4, (
            f"expected 4+ streamed token messages, got "
            f"{len(streamed_token_msgs)}: {streamed_token_msgs!r}"
        )

        # Final message is the artifact card.
        final_cards = [m for m in cap.messages if m.startswith("\n✅")]
        assert len(final_cards) == 1, (
            f"expected exactly 1 artifact card, got {len(final_cards)}: "
            f"{final_cards!r}"
        )
        assert "saved at" in final_cards[0]

        # Reconstructed output makes sense.
        full = cap.coalesced_final
        assert full is not None
        assert "from plugins.hermes_workflow" in full
        assert "✅ generated 'demo'" in full


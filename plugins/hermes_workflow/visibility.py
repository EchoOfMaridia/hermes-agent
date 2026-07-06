"""EventTranslator — convert Journal events into hermes StreamEvents.

The workflow plugin emits journal entries for every state transition
(run_started, step_started, agent_call, agent_response, verifier_returned,
step_completed, run_completed, …). The GatewayEventDispatcher in hermes
expects StreamEvent instances (MessageChunk, MessageStop, Commentary,
ToolCallChunk, ToolCallFinished, LongToolHint, GatewayNotice). This
module is the pure-function mapping between the two vocabularies.

The mapping is intentionally one-directional and lossless in the JSONL
sense (every journal event maps to exactly one StreamEvent; the StreamEvent
carries enough structured data that adapters can render meaningful UI).
The reverse direction (StreamEvent -> Journal) does not exist; the journal
is the canonical record, StreamEvents are presentation-layer only.

Concurrency model: this module is pure (no I/O, no globals, no shared
state). A new EventTranslator is constructed per workflow run; the runtime
holds one EventTranslator and calls translate() on each new journal entry.
"""

from __future__ import annotations

from typing import Any, Callable

# Import the StreamEvent types from hermes's gateway module. The
# workflow plugin depends on hermes internals; this is a deliberate
# first-party dependency (the plugin emits gateway events natively).
try:
    from gateway.stream_events import (
        GatewayNotice,
        LongToolHint,
        MessageChunk,
        StreamEvent,
        ToolCallChunk,
        ToolCallFinished,
    )
except ImportError:                          # pragma: no cover — hermes dep
    # Stubs that let the unit tests run without the full hermes tree.
    class _Stub:
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)
    StreamEvent = _Stub        # type: ignore[assignment,misc]
    ToolCallChunk = _Stub       # type: ignore[assignment,misc]
    ToolCallFinished = _Stub    # type: ignore[assignment,misc]
    LongToolHint = _Stub        # type: ignore[assignment,misc]
    GatewayNotice = _Stub       # type: ignore[assignment,misc]
    MessageChunk = _Stub        # type: ignore[assignment,misc]


# Per-step agent call index, reset at step_started.
class StepCallCounter:
    """Monotonic counter for agent calls within a step.

    The dispatcher uses index for ordering and dedup — a higher index
    never appears before a lower index for the same step.

    Usage:
        counter = StepCallCounter()
        counter.next()  # 1
        counter.next()  # 2
        counter.reset()  # back to 0; next() returns 1
    """

    def __init__(self) -> None:
        self._n = 0

    def next(self) -> int:
        self._n += 1
        return self._n

    def current(self) -> int:
        return self._n

    def sync_to(self, n: int) -> None:
        """Set the counter to *n* without incrementing. Used when a
        journal event arrives carrying the canonical call_index from
        the runtime: subsequent response events for the same step
        need to read the same index.
        """
        self._n = max(self._n, int(n))

    def reset(self) -> None:
        self._n = 0


class EventTranslator:
    """Pure translation from Journal events to StreamEvents.

    The translator is parameterized by a step name -> call counter map
    so that nested agent_call events get sequential per-step indices.
    Step counters reset whenever a step_started event is seen.

    Construct one translator per run; pass it the journal event dict;
    it returns zero or one StreamEvent (some journal events are noise
    that don't produce a StreamEvent — e.g., the journal close event).
    """

    # Per-step attempt counter, for verifier retry chains.
    # Resets at step_started.
    def __init__(self) -> None:
        self._step_counters: dict[str, StepCallCounter] = {}
        self._step_attempts: dict[str, int] = {}
        self._script_author_token_index: int = 0

    def _counter_for(self, step_name: str) -> StepCallCounter:
        c = self._step_counters.get(step_name)
        if c is None:
            c = StepCallCounter()
            self._step_counters[step_name] = c
        return c

    def _attempt(self, step_name: str) -> int:
        a = self._step_attempts.get(step_name, 0)
        a += 1
        self._step_attempts[step_name] = a
        return a

    def translate(self, event: dict[str, Any]) -> Any:
        """Translate one journal event to one StreamEvent, or None if
        the event is a noise event (e.g., replay of an unrelated kind)."""
        kind = event.get("kind")
        if kind == "run_started":
            return GatewayNotice(
                kind="workflow_run_started",
                text=f"run {event['run_id']} started: {event.get('workflow')}",
                extra={
                    "run_id": event["run_id"],
                    "workflow": event.get("workflow"),
                    "max_concurrent": event.get("max_concurrent"),
                    "max_total": event.get("max_total"),
                },
            )
        if kind == "step_started":
            step = event["step"]
            # Reset per-step counters when a new step starts.
            self._step_counters.pop(step, None)
            self._step_attempts[step] = 0
            return ToolCallChunk(
                tool_name=step,
                preview=None,
                args=None,
                index=0,
            )
        if kind == "agent_call":
            step = event.get("step", "<unknown>")
            counter = self._counter_for(step)
            # Prefer the journal's call_index (assigned by the agent
            # bridge at journaling time) over the translator's
            # internal counter. Fall back to the internal counter for
            # journal events from older runs that didn't include
            # call_index.
            journal_idx = event.get("call_index")
            if journal_idx is not None:
                idx = int(journal_idx)
                # Sync our internal counter so subsequent response
                # events for the same step align.
                counter.sync_to(idx)
            else:
                idx = counter.next()
            prompt = event.get("prompt_preview") or ""
            return ToolCallChunk(
                tool_name="ask_agent",
                preview=prompt[:80] + ("…" if len(prompt) > 80 else ""),
                args={"model": event.get("model"),
                      "max_tokens": event.get("max_tokens"),
                      "step": step,
                      "agent_index": idx},
                index=idx,
            )
        if kind == "agent_response":
            step = event.get("step", "<unknown>")
            counter = self._counter_for(step)
            # Prefer the journal's call_index; fall back to the counter.
            journal_idx = event.get("call_index")
            if journal_idx is not None:
                idx = int(journal_idx)
                counter.sync_to(idx)
            else:
                idx = counter.current()
            return ToolCallFinished(
                tool_name="ask_agent",
                duration=float(event.get("duration", 0.0)),
                ok=True,
                index=idx,
            )
        if kind == "verifier_returned":
            step = event["step"]
            valid = bool(event.get("valid", False))
            return ToolCallFinished(
                tool_name=step,
                duration=0.0,
                ok=valid,
                index=0,
            )
        if kind == "step_completed":
            step = event["step"]
            self._step_counters.pop(step, None)
            self._step_attempts.pop(step, None)
            return ToolCallFinished(
                tool_name=step,
                duration=float(event.get("evidence", {}).get(
                    "duration_seconds", 0.0,
                )),
                ok=True,
                index=0,
            )
        if kind == "step_failed":
            return ToolCallFinished(
                tool_name=event["step"],
                duration=0.0,
                ok=False,
                index=0,
            )
        if kind == "run_completed":
            return GatewayNotice(
                kind="workflow_run_completed",
                text=f"run {event['run_id']} done",
                extra={"run_id": event["run_id"]},
            )
        if kind == "run_failed":
            return GatewayNotice(
                kind="workflow_run_failed",
                text=f"run {event['run_id']} failed: {event.get('error', '')}",
                extra={
                    "run_id": event["run_id"],
                    "error": event.get("error", ""),
                    "error_type": event.get("error_type", ""),
                },
            )
        if kind == "run_halted":
            return GatewayNotice(
                kind="workflow_run_halted",
                text=f"run {event['run_id']} halted: {event.get('reason', '')}",
                extra={"run_id": event["run_id"],
                       "reason": event.get("reason", "")},
            )
        if kind == "run_cancelled":
            return GatewayNotice(
                kind="workflow_run_cancelled",
                text=f"run {event['run_id']} cancelled",
                extra={"run_id": event["run_id"],
                       "reason": event.get("reason", "user_cancelled")},
            )
        # Unknown kind — skip. Adapters never see a partial event.
        return None

    # -- script_author notifier events --------------------------------------

    def translate_script_author_event(
        self, event: tuple[str, dict[str, Any]],
    ) -> Any:
        """Translate a ScriptAuthor notifier callback to a StreamEvent.

        ``event`` is the ``(kind, payload)`` tuple ScriptAuthor emits
        from its ``_emit`` helper. Returns a StreamEvent subclass or
        ``None`` for unknown kinds.

        Per-kind mapping:

        - ``stage_started(kind=..., text=stage_name, extra=...)``
          → stage + state payload
        - ``stage_completed``: same shape with ``state="completed"``
        - ``stage_failed``:    same shape with ``state="failed"`` and
          ``kind="script_author_stage_failed"`` to make filtering easy
          in the statusbar
        - ``token`` →
          ``ToolCallChunk(tool_name="script_author_llm",
          preview=<delta>, args={"stage":...}, index=N)`` with
          monotonic index counter ``_script_author_token_index``
        - ``llm_completed`` →
          ``ToolCallFinished(tool_name="script_author_llm", ok=True)``
          matching the most recent chunk's index
        - ``artifact_posted`` →
          ``LongToolHint(text=<body preview>, extra={"kind":
          "script_author_artifact", "name":..., "path":...,
          "run_id":...})``
        """
        kind, payload = event
        if kind == "stage_started":
            return GatewayNotice(
                kind="script_author_stage_started",
                text=str(payload.get("stage", "")),
                extra={
                    "stage": payload.get("stage", ""),
                    "state": "started",
                },
            )
        if kind == "stage_completed":
            stage = str(payload.get("stage", ""))
            return GatewayNotice(
                kind="script_author_stage_completed",
                text=f"{stage} done",
                extra={
                    "stage": stage,
                    "state": "completed",
                    "ok": payload.get("ok", True),
                },
            )
        if kind == "stage_failed":
            stage = str(payload.get("stage", ""))
            error = str(payload.get("error", ""))
            return GatewayNotice(
                kind="script_author_stage_failed",
                text=f"{stage} failed: {error}",
                extra={
                    "stage": stage,
                    "state": "failed",
                    "error": error,
                },
            )
        if kind == "token":
            self._script_author_token_index += 1
            return ToolCallChunk(
                tool_name="script_author_llm",
                preview=str(payload.get("delta", ""))[:80],
                args={"stage": payload.get("stage", "")},
                index=self._script_author_token_index,
            )
        if kind == "llm_completed":
            idx = self._script_author_token_index or 1
            return ToolCallFinished(
                tool_name="script_author_llm",
                duration=0.0,
                ok=True,
                index=idx,
            )
        if kind == "artifact_posted":
            name = str(payload.get("name", ""))
            path = str(payload.get("path", ""))
            run_id = str(payload.get("run_id", ""))
            body = str(payload.get("body_preview", ""))
            text = (
                f"📄 ScriptAuthor posted artifact: {name}\n"
                f"  path: {path}\n"
                f"  run_id: {run_id}\n\n"
                + body
            )
            # Use a GatewayNotice for the artifact signal —
            # LongToolHint is reserved for runtime's long-tool nudges.
            # The artifact text lives in the notice text itself.
            return GatewayNotice(
                kind="script_author_artifact",
                text=text,
                extra={
                    "name": name,
                    "path": path,
                    "run_id": run_id,
                },
            )
        return None

    # -- snapshot / inspection helpers --------------------------------------

    def snapshot_for_run(self, run_id: str, journal_events: list[dict],
                          step_names: list[str] | None = None
                          ) -> dict[str, Any]:
        """Build the JSON snapshot payload for `hermes workflow status`.

        Per spec section 19.1. Pure function over the journal events.
        Caller passes the journal events; we don't read the journal file.
        """
        steps: dict[str, dict[str, Any]] = {}
        run_state = "unknown"
        run_started_at: float | None = None
        run_ended_at: float | None = None

        for ev in journal_events:
            kind = ev.get("kind")
            ts = float(ev.get("ts", 0.0))
            if kind == "run_started":
                run_state = "running"
                run_started_at = ts
            elif kind in ("run_completed", "run_failed",
                           "run_halted", "run_cancelled"):
                run_state = {
                    "run_completed": "done",
                    "run_failed": "failed",
                    "run_halted": "halted",
                    "run_cancelled": "cancelled",
                }[kind]
                run_ended_at = ts
            elif kind == "step_started":
                step = ev["step"]
                steps.setdefault(step, {
                    "name": step,
                    "state": "running",
                    "started_at": ts,
                    "agent_calls": 0,
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "verifier_verdict": None,
                    "attempts": 0,
                    "active_agents": [],
                })
            elif kind == "agent_call":
                step = ev.get("step")
                if step and step in steps:
                    steps[step]["agent_calls"] += 1
                    steps[step]["active_agents"].append({
                        "index": steps[step]["agent_calls"],
                        "prompt_preview": ev.get("prompt_preview", ""),
                        "started_at": ts,
                        "duration_so_far_seconds": None,
                    })
            elif kind == "agent_response":
                step = ev.get("step")
                if step and step in steps:
                    steps[step]["tokens_in"] += int(ev.get("tokens_in", 0))
                    steps[step]["tokens_out"] += int(ev.get("tokens_out", 0))
                    # Mark the most recent active agent as finished.
                    agents = steps[step]["active_agents"]
                    if agents and agents[-1].get("duration_so_far_seconds") is None:
                        agents[-1]["duration_so_far_seconds"] = float(
                            ev.get("duration", 0.0)
                        )
                        agents[-1]["ok"] = True
            elif kind == "verifier_returned":
                step = ev["step"]
                if step in steps:
                    steps[step]["verifier_verdict"] = (
                        "pass" if ev.get("valid") else "fail"
                    )
                    steps[step]["attempts"] += 1
            elif kind == "step_completed":
                step = ev["step"]
                if step in steps:
                    steps[step]["state"] = "verified"
                    steps[step]["duration_seconds"] = float(
                        ev.get("evidence", {}).get(
                            "duration_seconds", 0.0,
                        )
                    )
            elif kind == "step_failed":
                step = ev["step"]
                if step in steps:
                    steps[step]["state"] = "failed"

        elapsed = None
        staleness = None
        if run_started_at is not None:
            end = run_ended_at if run_ended_at is not None else (
                journal_events[-1].get("ts", 0.0)
                if journal_events else run_started_at
            )
            elapsed = (end or 0.0) - run_started_at

        return {
            "run_id": run_id,
            "workflow": (journal_events[0].get("workflow")
                          if journal_events else None),
            "state": run_state,
            "started_at": run_started_at,
            "elapsed_seconds": elapsed,
            "steps": list(steps.values()),
        }


# ---------------------------------------------------------------------------
# Three-tier card renderer
# ---------------------------------------------------------------------------

class ThreeTierCardRenderer:
    """Render a snapshot dict into platform-tier-specific text.

    Per spec section 16.2:
      - Tier 1 (TUI/desktop): full card tree with nested agent sub-cards
      - Tier 2 (Discord/Telegram/Slack): one line per step, collapsed agent
      - Tier 3 (iMessage/SMS): plain text, no formatting
    """

    TIER_TUI_DESKTOP = 1
    TIER_CHAT = 2
    TIER_PLAIN_TEXT = 3

    def render(self, snapshot: dict[str, Any], *, tier: int) -> str:
        """Render the snapshot for the given platform tier.

        Raises ValueError on unknown tier.
        """
        if tier not in (1, 2, 3):
            raise ValueError(f"unknown tier: {tier!r} (expected 1, 2, or 3)")
        if tier == 1:
            return self._render_tier1(snapshot)
        if tier == 2:
            return self._render_tier2(snapshot)
        return self._render_tier3(snapshot)

    # -- tier 1: full card tree (TUI/desktop) --------------------------

    def _render_tier1(self, snap: dict[str, Any]) -> str:
        lines: list[str] = []
        state = snap.get("state", "unknown")
        marker = "▶" if state == "running" else "■"
        verb = "started" if state == "running" else (
            "done" if state == "done" else state
        )
        run_id = snap.get("run_id", "?")
        workflow = snap.get("workflow", "?")
        lines.append(f"{marker} run {run_id} ({workflow}) {verb}")

        for step in snap.get("steps", []):
            lines.append(self._render_step_tier1(step))

        if state in ("done", "failed", "halted", "cancelled"):
            verb = {
                "done": "done",
                "failed": "failed",
                "halted": "halted",
                "cancelled": "cancelled",
            }.get(state, state)
            elapsed = snap.get("elapsed_seconds")
            suffix = f" in {elapsed:.1f}s" if elapsed is not None else ""
            lines.append(f"■ run {run_id} {verb}{suffix}")
        return "\n".join(lines)

    def _render_step_tier1(self, step: dict[str, Any]) -> str:
        name = step.get("name", "?")
        state = step.get("state", "?")
        marker = "🔧"
        verdict = step.get("verifier_verdict")
        suffix = f" · {verdict}" if verdict else ""
        agent_calls = step.get("agent_calls", 0)
        tokens_in = step.get("tokens_in", 0)
        tokens_out = step.get("tokens_out", 0)
        duration = step.get("duration_seconds")
        summary_parts = []
        if agent_calls:
            summary_parts.append(f"{agent_calls} agent"
                                   + ("s" if agent_calls != 1 else ""))
        if tokens_in or tokens_out:
            summary_parts.append(f"{tokens_in + tokens_out} tokens")
        if duration is not None:
            summary_parts.append(f"{duration:.1f}s")
        summary = " · ".join(summary_parts)
        header = (f"{marker} {name}"
                   + (f"  ·  {summary}" if summary else "")
                   + suffix)
        out = [f"├── {header}"]
        # Nested agent sub-cards.
        for agent in step.get("active_agents", []):
            idx = agent.get("index", "?")
            preview = agent.get("prompt_preview", "")
            d = agent.get("duration_so_far_seconds")
            ok = agent.get("ok")
            if ok is True and d is not None:
                out.append(f"│   ├── ↳ agent #{idx}: {preview}")
                out.append(f"│   └── ↳ done in {d:.1f}s")
            else:
                out.append(f"│   ├── ↳ agent #{idx}: {preview}")
        return "\n".join(out)

    # -- tier 2: one line per step (chat surfaces) ---------------------

    def _render_tier2(self, snap: dict[str, Any]) -> str:
        lines: list[str] = []
        state = snap.get("state", "unknown")
        marker = "▶" if state == "running" else "■"
        verb = "started" if state == "running" else (
            "done" if state == "done" else state
        )
        run_id = snap.get("run_id", "?")
        workflow = snap.get("workflow", "?")
        lines.append(f"{marker} {run_id} {workflow} {verb}")

        for step in snap.get("steps", []):
            name = step.get("name", "?")
            agent_calls = step.get("agent_calls", 0)
            tokens_in = step.get("tokens_in", 0)
            tokens_out = step.get("tokens_out", 0)
            duration = step.get("duration_seconds")
            verdict = step.get("verifier_verdict")
            state_short = self._state_short(step.get("state"))
            parts = [name]
            if agent_calls:
                parts.append(f"{agent_calls} agent"
                              + ("s" if agent_calls != 1 else ""))
            if tokens_in or tokens_out:
                parts.append(self._tokens_label(tokens_in + tokens_out))
            if duration is not None:
                parts.append(f"{duration:.1f}s")
            if verdict:
                parts.append(verdict)
            if state_short:
                parts.append(state_short)
            lines.append(f"🔧 {' · '.join(parts)}")

        if state in ("done", "failed", "halted", "cancelled"):
            elapsed = snap.get("elapsed_seconds")
            suffix = f" in {elapsed:.1f}s" if elapsed is not None else ""
            lines.append(f"■ {run_id} {state}{suffix}")
        return "\n".join(lines)

    @staticmethod
    def _state_short(state: str | None) -> str:
        if state == "verified":
            return ""
        if state == "failed":
            return "fail"
        if state == "running":
            return "running"
        return state or ""

    @staticmethod
    def _tokens_label(n: int) -> str:
        if n >= 1000:
            return f"{n / 1000:.1f}k tok"
        return f"{n} tok"

    # -- tier 3: plain text (iMessage / SMS) --------------------------

    def _render_tier3(self, snap: dict[str, Any]) -> str:
        lines: list[str] = []
        state = snap.get("state", "unknown")
        run_id = snap.get("run_id", "?")
        workflow = snap.get("workflow", "?")
        if state == "running":
            lines.append(f"run {run_id} started: {workflow}")
        else:
            lines.append(f"run {run_id} {state}: {workflow}")

        for step in snap.get("steps", []):
            name = step.get("name", "?")
            agent_calls = step.get("agent_calls", 0)
            tokens_in = step.get("tokens_in", 0)
            tokens_out = step.get("tokens_out", 0)
            duration = step.get("duration_seconds")
            verdict = step.get("verifier_verdict") or ""
            state = step.get("state", "?")
            parts = []
            if agent_calls:
                parts.append(f"{agent_calls} agent"
                              + ("s" if agent_calls != 1 else ""))
            if tokens_in or tokens_out:
                parts.append(f"{tokens_in + tokens_out} tokens")
            if duration is not None:
                parts.append(f"{duration:.1f}s")
            if verdict:
                parts.append(verdict)
            detail = f" ({', '.join(parts)})" if parts else ""
            lines.append(f"step {name} {state}{detail}")

        if state in ("done", "failed", "halted", "cancelled"):
            elapsed = snap.get("elapsed_seconds")
            suffix = f" in {elapsed:.1f}s" if elapsed is not None else ""
            lines.append(f"run {run_id} {state}{suffix}")
        return "\n".join(lines)

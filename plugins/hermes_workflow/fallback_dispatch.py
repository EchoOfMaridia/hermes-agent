"""Fallback dispatch sink for hermes-chat contexts.

When the host doesn't expose ``hermes_cli.gateway.get_active_dispatcher``
(the common case for hermes-desktop chats), the workflow plugin can't
push StreamEvents to the chat surface through the gateway pipeline.
The fallback sink bridges that gap by:

1. Acting as the ``runtime._dispatcher`` callable: every journal event
   that would have been translated to a StreamEvent is queued here.
2. Providing a ``drain_to(chat_loop)`` method that the chat loop
   periodically pulls from to render pending events as assistant
   messages via ``ctx.inject_message(role="assistant", ...)``.

The result: workflow events reach the chat as a sequence of assistant
messages even when the gateway dispatcher is unavailable. The user
gains the live progress visibility they asked for: every journal
``run_started`` / ``step_started`` / ``agent_call`` / ``step_completed``
/ ``step_failed`` / ``run_cancelled`` becomes a chat message.

Why a sink at all vs. just calling inject_message directly from the
dispatcher? Because the dispatcher IS just a callback from the
runtime — it gets called from inside the journal-writing code path,
which is not async-safe to call inject_message from synchronously.
Queueing gives us async-decoupled injection.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any, Callable, Deque, Optional

_log = logging.getLogger("hermes_workflow.fallback_dispatch")


class FallbackDispatchSink:
    """Collects StreamEvents from the runtime's dispatcher callback.

    Each call to ``dispatch(event)`` appends the event to an
    in-memory queue. A consumer (slash handler, gateway handler,
    chat-loop tick) drains the queue and renders events as chat
    messages.

    Why Deque and not thread-safe Queue? The runtime calls
    ``dispatch`` synchronously from journal-write paths. The
    consumer (chat loop / slash surface / etc.) drains from an
    asyncio loop. Synchronous append + async drain is fine as
    long as we don't try to iterate while appending from another
    thread — and the runtime doesn't do that for workflow events.
    """

    def __init__(self, *, max_size: int = 1024) -> None:
        self._events: Deque[Any] = deque(maxlen=max_size)
        self._dropped = 0
        self._total = 0

    def dispatch(self, event: Any) -> None:
        """Receive one translated StreamEvent from the runtime."""
        if len(self._events) == self._events.maxlen:
            self._dropped += 1
        self._events.append(event)
        self._total += 1

    def drain(self) -> list[Any]:
        """Drain pending events. Returned in FIFO order; each event
        appears once (drained events are removed from the queue)."""
        out = list(self._events)
        self._events.clear()
        return out

    def pending(self) -> int:
        return len(self._events)

    @property
    def stats(self) -> dict[str, int]:
        return {
            "pending": len(self._events),
            "total_received": self._total,
            "dropped_overflow": self._dropped,
        }


def render_event_to_message(event: Any) -> str:
    """Translate a StreamEvent into a chat-renderable string.

    StreamEvent subclasses the runtime emits:
      - MessageChunk / MessageStop: assistant streaming chunks
        (not used by hermes_workflow today; reserved for future)
      - GatewayNotice: high-level status ("workflow X started")
      - LongToolHint: long-running tool progress

    The workflow runtime emits GatewayNotice events for run-level
    state changes and LongToolHint events for step-level progress.

    Each event has ``.text`` (or is the text itself) plus optional
    structured fields. We render them as a compact text block the
    chat can display.
    """
    # Extract the inner text. StreamEvent instances from
    # gateway/stream_events.py are dataclass-like; fall back to
    # either ``text`` attribute or ``str(event)`` for untyped
    # provider-emitted events.
    text = getattr(event, "text", None)
    if text is None and isinstance(event, dict):
        text = event.get("text")
    if text is None:
        text = str(event)
    # Include any structured metadata (workflow name, run_id, step)
    # inline so the user can grep it without re-parsing.
    extras: list[str] = []
    for key in ("workflow", "run_id", "step", "kind"):
        v = getattr(event, key, None)
        if v is None and isinstance(event, dict):
            v = event.get(key)
        if v is not None:
            extras.append(f"{key}={v}")
    if extras:
        return f"{text}\n  ({', '.join(extras)})"
    return text


def drain_sink_to_ctx(sink: FallbackDispatchSink, ctx: Any) -> int:
    """Drain pending events from ``sink`` into ``ctx`` via inject_message.

    Returns the number of messages injected. The caller (slash
    handler, gateway handler, chat-loop tick) should call this on
    every opportunity to give the user live progress.

    Returns 0 when the sink is empty. Idempotent: drains exactly
    the events that have arrived since the last drain.
    """
    if sink is None or sink.pending() == 0:
        return 0
    n = 0
    for event in sink.drain():
        msg = render_event_to_message(event)
        try:
            ctx.inject_message(content=msg, role="assistant")
            n += 1
        except Exception as exc:
            _log.debug(
                "drain_sink_to_ctx: inject_message failed: %s: %s",
                type(exc).__name__, exc,
            )
    if n:
        _log.info(
            "drained %d workflow event(s) into chat", n,
        )
    return n


def attach_fallback_sink(runtime: Any) -> Optional[FallbackDispatchSink]:
    """Construct a FallbackDispatchSink, wire it as the runtime's
    dispatcher, and return the sink so callers can drain it.

    Idempotent: if the runtime is already wired to a non-fallback
    dispatcher (e.g., the gateway dispatcher finally connected),
    return None without overwriting.

    Pin contract: register() calls this function so every plugin
    load gets a sink available, and surfaces that want chat-visibility
    can drain it. The user-facing question "I have no clue what's
    in this workflow" gets answered by the resulting drain.
    """
    current = getattr(runtime, "_dispatcher", None)
    if current is not None and not isinstance(current, FallbackDispatchSink):
        # A real gateway dispatcher was wired. Don't overwrite.
        # Caller can use the gateway pipeline instead.
        return None
    sink = FallbackDispatchSink()
    runtime.set_dispatcher(sink.dispatch)
    _log.info(
        "workflow runtime wired to FallbackDispatchSink — events "
        "queued in-process for chat-loop drain"
    )
    return sink

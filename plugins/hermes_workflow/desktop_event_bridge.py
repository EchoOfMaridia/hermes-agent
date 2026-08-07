"""desktop_event_bridge — Translate workflow GatewayNotice events into the
JSON-RPC wire format the Hermes desktop app expects on its WebSocket.

Pitfall #57 (2026-07-18, fixed 2026-07-19):

The Python backend has had no code path that emits the
``RpcEvent`` shape ``{type, session_id, payload: {kind, run_id, ...}}``
the desktop ``use-workflow-events.ts:188`` hook switches on. Every
workflow run completed silently on the desktop — the Workflows panel
stayed at "0 runs since boot" even after successful runs.

Root cause: the workflow plugin's runtime dispatcher (set up in
``plugins/hermes_workflow/__init__.py:225``) receives translated
``StreamEvent`` subclasses (``GatewayNotice``, ``ToolCallChunk``,
``ToolCallFinished``). The dispatcher routes through
``gateway/stream_dispatch.py:GatewayEventDispatcher.dispatch`` which
calls ``on_long_tool`` / ``on_notice`` callbacks. **None of the
adapters for the desktop transport existed before this commit.**

The fix in two layers:

  Layer 1 — ``DesktopWorkflowEventBridge`` (this module): a singleton
  that registers itself as a SECOND dispatcher on the workflow
  runtime, translates ``GatewayNotice(kind="workflow_run_*")`` to
  the JSON-RPC envelope ``tui_gateway.server._emit`` builds, and
  calls that emit function. The fan-out to the active WebSocket
  transport is already handled by ``tui_gateway.server._emit``.

  Layer 2 — chain-dispatcher in ``runtime.py``: the runtime's
  ``set_dispatcher`` previously only held one callable. This module
  registers a wrapper that calls BOTH the gateway dispatcher (set
  by the existing plugin loader) AND the desktop bridge, so the
  fallback / FallbackDispatchSink path keeps working AND the
  desktop wire format is emitted in the same call.

The bridge is fire-and-forget: any error during emit is logged but
does not affect journal persistence or workflow execution.

Verification harness: ``tests/test_desktop_event_bridge.py`` exercises
the translate layer end-to-end (GatewayNotice → JSON-RPC envelope) for
the four terminal-state kinds (``workflow_run_started``,
``workflow_run_completed``, ``workflow_run_failed``,
``workflow_run_halted``, ``workflow_run_cancelled``) plus the
in-flight kinds (``workflow_step_started``,
``workflow_step_finished``, ``workflow_subagent_spawned``). No live
WebSocket needed — we assert the translated envelope, not the wire
send.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import asdict, is_dataclass
from typing import Any, Callable, Dict, Optional


_log = logging.getLogger("hermes_workflow.desktop_event_bridge")

# Event kinds the desktop hook knows about. Mirrors
# apps/desktop/src/types/hermes.ts:WorkflowRunStartedPayload +
# WorkflowRunFinishedPayload + WorkflowStepFinishedPayload +
# WorkflowSubagentInfo. Single source of truth for the wire contract.
_DESKTOP_WORKFLOW_KINDS = frozenset({
    "workflow_run_started",
    "workflow_run_completed",
    "workflow_run_failed",
    "workflow_run_halted",
    "workflow_run_cancelled",
    "workflow_step_started",
    "workflow_step_finished",
    "workflow_subagent_spawned",
})


def _extract_extra(event: Any) -> Dict[str, Any]:
    """Pull a dict payload out of a GatewayNotice (dataclass) OR a dict.

    For ``GatewayNotice`` (and any other dataclass with an ``extra``
    field), we MUST reach into ``extra`` — the dataclass fields are
    ``kind`` / ``text`` / ``extra``, not the payload keys directly.
    Returning a flat dict where ``extra`` is merged at the top level
    keeps the translator code shape-uniform for dataclass + dict
    events.
    """
    # is_dataclass returns True for both instances and the class itself;
    # we only want instances. The dataclasses.is_dataclass(instance) form
    # narrows to the instance type so asdict() accepts it.
    if is_dataclass(event) and not isinstance(event, type):
        try:
            flat = dict(asdict(event))
            # Merge ``extra`` at the top level so the translator
            # can read keys uniformly.
            nested_extra = flat.pop("extra", None)
            if isinstance(nested_extra, dict):
                flat.update(nested_extra)
            return flat
        except Exception:
            pass
    if isinstance(event, dict):
        return dict(event)
    return {}


def _translate_to_rpc_envelope(event: Any) -> Optional[Dict[str, Any]]:
    """Translate one translated StreamEvent to the JSON-RPC envelope the
    desktop WebSocket consumers expect.

    Returns ``None`` when the event is not a workflow-related notice
    (e.g. a generic ``restart`` / ``online`` notice that the desktop
    doesn't know how to render). Non-workflow events are filtered at
    this seam so the desktop reducer never sees an unknown kind.

    Per apps/desktop/src/types/hermes.ts:1091, the desktop reducer
    expects::

        {
          "kind": "workflow_run_started",
          "run_id": str,
          "workflow": str,
          "max_concurrent"?: int,
          "max_total"?: int,
          "started_at"?: number,
          "steps"?: list[str],
        }

    and::

        {
          "jsonrpc": "2.0",
          "method": "event",
          "params": {"type": <kind>, "session_id"?: str, "payload": {...}},
        }

    The translator below builds both shapes; the outer ``emit()``
    wrapper then routes through ``tui_gateway.server._emit`` which
    attaches ``session_id`` from the active contextvar (if set).
    """
    extra = _extract_extra(event)
    kind = extra.get("kind") if isinstance(extra, dict) else None
    if not isinstance(kind, str) or kind not in _DESKTOP_WORKFLOW_KINDS:
        return None

    payload: Dict[str, Any] = {"kind": kind}

    # Common fields across the union kinds.
    for src_key, dst_key in (
        ("run_id", "run_id"),
        ("workflow", "workflow"),
        ("max_concurrent", "max_concurrent"),
        ("max_total", "max_total"),
        ("started_at", "started_at"),
        ("steps", "steps"),
        ("ended_at", "ended_at"),
        ("error", "error"),
        ("error_type", "error_type"),
        ("reason", "reason"),
        # Per-step fields.
        ("step", "step"),
        ("tool_name", "tool_name"),
        ("duration_seconds", "duration_seconds"),
        ("ok", "ok"),
        # Subagent fields.
        ("subagent_id", "subagent_id"),
    ):
        if src_key in extra and extra[src_key] is not None:
            payload[dst_key] = extra[src_key]

    # Wire envelope (tui_gateway.server._emit contract).
    return {
        "jsonrpc": "2.0",
        "method": "event",
        "params": {"type": kind, "payload": payload},
    }


class DesktopWorkflowEventBridge:
    """Singleton that bridges workflow StreamEvents to desktop JSON-RPC.

    Register on the runtime via :meth:`register` — wraps the existing
    dispatcher so every translated event flows to BOTH the existing
    gateway pipeline AND this bridge. The bridge translates and
    hands the envelope to ``tui_gateway.server._emit`` (which fans
    out to the active WebSocket transport).

    Thread safety: the runtime may call dispatchers from background
    threads (the agent loop). The bridge guards ``_registered`` with
    a lock and the actual ``_emit`` call is thread-safe per the
    tui_gateway.server contract (it uses a contextvar + module-level
    stdio transport fallback).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._wrapped_dispatcher: Optional[Callable[[Any], None]] = None
        self._emit_fn: Optional[Callable[..., None]] = None
        self._stats = {
            "received": 0,
            "translated": 0,
            "filtered_non_workflow": 0,
            "emit_ok": 0,
            "emit_failed": 0,
        }

    def set_emit_fn_for_tests(
        self, fn: Optional[Callable[..., None]]
    ) -> None:
        """Test-only: inject a stub emit callable. Replaces the
        lazily-resolved ``tui_gateway.server._emit`` so the harness
        can drive the dispatch path without an actual WebSocket."""
        self._emit_fn = fn

    @property
    def stats(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._stats)

    def register(
        self,
        runtime: Any,
    ) -> None:
        """Wrap the runtime's existing dispatcher so events also flow here.

        Idempotent: re-registration is a no-op (the wrapper is set
        once). If the runtime has no dispatcher configured, this
        method still wires the bridge as the primary dispatcher
        (the chat-visible ``FallbackDispatchSink`` is registered
        separately by the plugin loader and is unaffected).

        The wrapping pattern lets the existing gateway dispatcher
        keep running unchanged. Per Pitfall #57 layer 1: this is the
        load-bearing fix — without it, the desktop panel never
        populates.
        """
        with self._lock:
            if self._wrapped_dispatcher is not None:
                return  # already wrapped

            existing = getattr(runtime, "_dispatcher", None)

            def _chained(event: Any) -> None:
                # Always route to the existing dispatcher first
                # (gateway pipeline + fallback sink). The existing
                # dispatcher may swallow the event, but we still
                # get a copy via our own translation pass.
                if existing is not None:
                    try:
                        existing(event)
                    except Exception as exc:  # never break the runtime
                        _log.debug(
                            "existing dispatcher raised: %s: %s",
                            type(exc).__name__, exc,
                        )

                # Translate + emit to the desktop wire format.
                self._handle(event)

            self._wrapped_dispatcher = _chained
            runtime.set_dispatcher(_chained)
            _log.info(
                "DesktopWorkflowEventBridge registered — workflow events "
                "will now flow to /api/ws subscribers via JSON-RPC envelopes"
            )

    def _handle(self, event: Any) -> None:
        with self._lock:
            self._stats["received"] += 1

        envelope = _translate_to_rpc_envelope(event)
        if envelope is None:
            with self._lock:
                self._stats["filtered_non_workflow"] += 1
            return

        with self._lock:
            self._stats["translated"] += 1

        # Lazily import the gateway emit so we don't require
        # tui_gateway at module load time. The plugin loader's
        # order ensures tui_gateway is importable when register()
        # is called.
        try:
            if self._emit_fn is None:
                from tui_gateway.server import _emit as emit_fn
                self._emit_fn = emit_fn

            params = envelope["params"]
            payload = params.pop("payload", None)
            event_type = params.pop("type", None)
            session_id = params.get("session_id", "")
            if payload is None or event_type is None:
                with self._lock:
                    self._stats["emit_failed"] += 1
                return
            self._emit_fn(event_type, session_id, payload)
            with self._lock:
                self._stats["emit_ok"] += 1
        except Exception as exc:
            with self._lock:
                self._stats["emit_failed"] += 1
            _log.debug(
                "desktop event emit failed: %s: %s",
                type(exc).__name__, exc,
            )


# Module-level singleton — the runtime has at most one dispatcher
# chain per process, and the plugin loader calls register() once.
_bridge: Optional[DesktopWorkflowEventBridge] = None


def get_bridge() -> DesktopWorkflowEventBridge:
    """Return the process-singleton bridge, creating it on first access."""
    global _bridge
    if _bridge is None:
        with threading.Lock():
            if _bridge is None:
                _bridge = DesktopWorkflowEventBridge()
    return _bridge


def reset_for_tests() -> None:
    """Drop the singleton so the next ``get_bridge()`` returns a fresh one."""
    global _bridge
    _bridge = None
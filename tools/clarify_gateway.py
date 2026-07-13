"""Gateway-side clarify primitive (blocking event-based queue).

The ``clarify`` tool needs to ask the user a question and block the agent
thread until they respond.  In CLI mode this is trivial — ``input()`` is
synchronous.  In gateway mode the agent runs on a worker thread while the
event loop handles the user's reply, so we need a thread-safe primitive
that:

  * stores a pending clarify request (with a generated ``clarify_id``),
  * blocks the agent thread on an ``Event``,
  * resolves the wait when the gateway's button-callback or text-intercept
    fires ``resolve_gateway_clarify(clarify_id, response)``,
  * supports timeouts so a user who never responds does NOT hang the agent
    thread forever (which would also pin the gateway's running-agent guard).

State is module-level (same shape as ``tools.approval``) so platform
adapters can call ``resolve_gateway_clarify`` without holding a back-
reference to the ``GatewayRunner`` instance.

Two delivery paths from the adapter:

  1. **Button UI** — adapters override ``send_clarify`` to render inline
     buttons (e.g. Telegram ``InlineKeyboardMarkup``).  The button
     callback resolves with the chosen string.  A final "Other (type
     answer)" button enters text-capture mode for free-form responses.

  2. **Text fallback** — adapters without rich UI render a numbered list.
     The user replies with a number ("2") or with free text; the gateway's
     ``_handle_message`` intercepts the reply and resolves directly.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# =========================================================================
# Module-level state
# =========================================================================

@dataclass
class _ClarifyEntry:
    """One pending clarify request inside a gateway session."""
    clarify_id: str
    session_key: str
    question: str
    choices: Optional[List[str]]
    event: threading.Event = field(default_factory=threading.Event)
    response: Optional[str] = None
    awaiting_text: bool = False  # set when user picked "Other" or clarify is open-ended

    def signature(self) -> Dict[str, object]:
        return {
            "clarify_id": self.clarify_id,
            "session_key": self.session_key,
            "question": self.question,
            "choices": list(self.choices) if self.choices else None,
        }


_lock = threading.RLock()
# clarify_id → _ClarifyEntry  (primary lookup for button callbacks)
_entries: Dict[str, _ClarifyEntry] = {}
# session_key → list[clarify_id]  (FIFO; for text-fallback intercept and session cleanup)
_session_index: Dict[str, List[str]] = {}


# =========================================================================
# Public API — agent-thread side
# =========================================================================

def register(
    clarify_id: str,
    session_key: str,
    question: str,
    choices: Optional[List[str]],
) -> _ClarifyEntry:
    """Register a pending clarify request and return the entry.

    The caller (gateway clarify_callback) will then send the prompt to the
    user and block on ``wait_for_response(clarify_id, timeout)``.
    """
    entry = _ClarifyEntry(
        clarify_id=clarify_id,
        session_key=session_key,
        question=question,
        choices=list(choices) if choices else None,
        # Open-ended (no choices) → next message IS the response, no buttons needed.
        awaiting_text=not bool(choices),
    )
    with _lock:
        _entries[clarify_id] = entry
        _session_index.setdefault(session_key, []).append(clarify_id)
    return entry


def wait_for_response(clarify_id: str, timeout: float) -> Optional[str]:
    """Block on the entry's event until resolved or timeout fires.

    Polls in 1-second slices so the agent's inactivity heartbeat keeps
    firing — without this, ``Event.wait(timeout=600)`` blocks the thread
    for 10 minutes with zero activity touches and the gateway's inactivity
    watchdog kills the agent while the user is still typing.

    Returns the resolved response string, or ``None`` on timeout.
    """
    with _lock:
        entry = _entries.get(clarify_id)
    if entry is None:
        return None

    try:
        from tools.environments.base import touch_activity_if_due
    except Exception:  # pragma: no cover - optional
        touch_activity_if_due = None

    deadline = time.monotonic() + max(timeout, 0.0)
    activity_state = {"last_touch": time.monotonic(), "start": time.monotonic()}
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if entry.event.wait(timeout=min(1.0, remaining)):
            break
        if touch_activity_if_due is not None:
            touch_activity_if_due(activity_state, "waiting for user clarify response")

    with _lock:
        # Remove from indices regardless of resolution outcome.
        _entries.pop(clarify_id, None)
        ids = _session_index.get(entry.session_key)
        if ids and clarify_id in ids:
            ids.remove(clarify_id)
            if not ids:
                _session_index.pop(entry.session_key, None)

    return entry.response


# =========================================================================
# Public API — gateway / adapter side
# =========================================================================

def resolve_gateway_clarify(clarify_id: str, response: str) -> bool:
    """Unblock the agent thread waiting on ``clarify_id``.

    Returns True if an entry was found and resolved, False otherwise
    (already resolved, expired, or never existed).
    """
    with _lock:
        entry = _entries.get(clarify_id)
        if entry is None:
            return False
    entry.response = str(response) if response is not None else ""
    entry.event.set()
    return True


def resolve_choice_by_index(clarify_id: str, index: int) -> bool:
    """Resolve a pending multi-choice clarify by button index.

    Public helper for platform adapters (Discord ``ClarifyChoiceView``,
    Telegram ``InlineKeyboardButton`` callbacks, etc.) that render one
    button per choice and need a clean way to resolve the choice WITHOUT
    reaching into the module's private ``_entries`` dict.

    Why this exists (the Discord "buttons grayed out, agent stuck" bug):
    The Discord adapter historically did::

        from tools.clarify_gateway import _entries
        entry = _entries.get(self.clarify_id)
        if entry and entry.choices and 0 <= index < len(entry.choices):
            resolved_text = entry.choices[index]
        ...
        resolve_gateway_clarify(self.clarify_id, resolved_text)

    That reach-in pattern has three failure modes:

      1. The entry was already cleaned up by a session-boundary clear or a
         late timeout. ``_entries.get`` returns ``None`` and the adapter
         falls back to the truncated button label as the answer. The
         agent receives a value that doesn't match ``choices_offered``,
         ``clarify_tool`` tags it ``"unresolved"``, the gate halts, and
         the agent is stuck because every button was already grayed out.
      2. The resolve call fails (network error, callback exception). The
         buttons are still grayed. The user can't re-click. Session
         must be destroyed.
      3. Private-state reach means any change to ``clarify_gateway``
         internals silently breaks the adapter's resolve path. The bug
         class never gets fixed in one place.

    This helper centralises the entry lookup and resolve. If the entry is
    gone (cleaned up by timeout or session-boundary cleanup) the call is
    a clean no-op — the adapter's late click doesn't crash, doesn't fall
    back to truncated text, doesn't brick the session.

    Args:
        clarify_id: The pending clarify entry's id (UUID hex, 10 chars).
        index: Zero-based index into the entry's ``choices`` list.

    Returns:
        ``True`` if a resolve fired; ``False`` if the entry was not found,
        already resolved, has no choices (open-ended clarify), or the
        index is out of range.
    """
    with _lock:
        entry = _entries.get(clarify_id)
        if entry is None:
            return False
        # Already resolved — idempotent no-op so a late Discord click
        # doesn't overwrite the user's first choice.
        if entry.event.is_set():
            return False
        if entry.choices is None:
            # Open-ended clarify — use the text-fallback path, not the
            # button-by-index path. Returning False here forces the adapter
            # to fall back to mark_awaiting_text + text-intercept.
            return False
        if not (0 <= index < len(entry.choices)):
            return False
        resolved_text = entry.choices[index]
    return resolve_gateway_clarify(clarify_id, resolved_text)


def get_pending_for_session(session_key: str) -> Optional[_ClarifyEntry]:
    """Return the OLDEST pending clarify entry for a session, or None.

    Used by the text-fallback intercept in ``_handle_message`` — when a
    clarify is awaiting a free-form text response, the next user message
    in that session is captured as the answer.
    """
    with _lock:
        ids = _session_index.get(session_key) or []
        for cid in ids:
            entry = _entries.get(cid)
            if entry is None:
                continue
            if entry.awaiting_text:
                return entry
        return None


def mark_awaiting_text(clarify_id: str) -> bool:
    """Flip an entry into text-capture mode (user picked the 'Other' button).

    Returns True if the entry exists and was flipped, False otherwise.
    """
    with _lock:
        entry = _entries.get(clarify_id)
        if entry is None:
            return False
        entry.awaiting_text = True
        return True


def has_pending(session_key: str) -> bool:
    """Return True when this session has at least one pending clarify entry."""
    with _lock:
        ids = _session_index.get(session_key) or []
        return any(_entries.get(cid) is not None for cid in ids)


def clear_session(session_key: str) -> int:
    """Resolve and drop every pending clarify for a session.

    Used by session-boundary cleanup (e.g. ``/new``, gateway shutdown,
    cached-agent eviction) so blocked agent threads don't hang past the
    end of their session.  Returns the number of entries that were
    *actually cancelled* (still pending when called). Entries that were
    already resolved by a button click or text intercept are removed
    silently and not counted — the user's choice is preserved and the
    agent thread already unblocked.
    """
    with _lock:
        ids = list(_session_index.pop(session_key, []) or [])
        entries = [_entries.pop(cid, None) for cid in ids]
    cancelled = 0
    for entry in entries:
        if entry is None:
            continue
        if entry.event.is_set():
            # Already resolved (button click, text intercept, prior cancel).
            # Don't overwrite the user's response with an empty sentinel.
            continue
        # Empty string sentinel — agent code can distinguish from a real
        # response by inspecting the wait_for_response return value
        # alongside its own timeout deadline.  Most callers just treat any
        # falsy result as "user did not respond".
        entry.response = ""
        entry.event.set()
        cancelled += 1
    return cancelled


def force_cancel_session(session_key: str) -> int:
    """Atomic panic-button cancel — alias-shaped surface for /stop and /new.

    Functionally identical to ``clear_session``. Exists as a separate name
    so call sites that want to signal "user explicitly hit cancel, drop
    everything" read clearly at the call site, and so the bug-fix history
    (the Discord "stuck button" incident of 2026-06-29) has a single
    symbol to point at in postmortems.

    Without this, the only way to escape a stuck clarify was to destroy
    the entire session. Now ``/stop`` can fire ``force_cancel_session``
    to unblock the agent thread and let the agent respond with a halt
    (clarify_tool's ``user_response_mode == "unresolved"`` discipline
    catches the sentinel and forces a halt, so the agent doesn't proceed
    past the gate the user was trying to escape from).
    """
    return clear_session(session_key)


# =========================================================================
# Config
# =========================================================================

def get_clarify_timeout() -> int:
    """Read the clarify response timeout (seconds) from config.

    Defaults to 1800 (30 minutes) — long enough for the user to step away
    during a research-heavy interactive-plan phase (or any long-running
    delegated work), short enough that an abandoned prompt eventually
    unblocks the agent thread instead of pinning the running-agent guard
    forever. Was 600 before interactive-plan workflows started routinely
    exceeding the 10-minute ceiling; raise further in config.yaml if you
    walk away even longer.

    Reads ``agent.clarify_timeout`` from config.yaml.
    """
    try:
        from hermes_cli.config import load_config
        cfg = load_config() or {}
        agent_cfg = cfg.get("agent", {}) or {}
        return int(agent_cfg.get("clarify_timeout", 1800))
    except Exception:
        return 1800


# =========================================================================
# Per-session notify hook (gateway → adapter bridge)
# =========================================================================
# Mirrors tools.approval's _gateway_notify_cbs: the gateway registers a
# per-session callback that sends the clarify prompt to the user.  The
# callback bridges sync→async (runs on the agent thread; schedules the
# adapter ``send_clarify`` call on the event loop).

_notify_cbs: Dict[str, Callable[[_ClarifyEntry], None]] = {}


def register_notify(session_key: str, cb: Callable[[_ClarifyEntry], None]) -> None:
    """Register a per-session notify callback used by ``clarify_callback``."""
    with _lock:
        _notify_cbs[session_key] = cb


def unregister_notify(session_key: str) -> None:
    """Drop the per-session notify callback and cancel any pending clarify entries."""
    with _lock:
        _notify_cbs.pop(session_key, None)
    # Cancel any pending entries so blocked threads unwind when the run
    # ends (interrupt, completion, gateway shutdown).
    clear_session(session_key)


def get_notify(session_key: str) -> Optional[Callable[[_ClarifyEntry], None]]:
    with _lock:
        return _notify_cbs.get(session_key)

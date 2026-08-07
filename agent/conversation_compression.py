"""Context compression — extract the AIAgent methods that drive summarisation.

Three concerns live here:

* :func:`check_compression_model_feasibility` — startup probe of the
  configured auxiliary compression model.  Warns when the aux context
  window can't fit the main model's compression threshold; auto-lowers
  the session threshold when possible; hard-rejects auxes below
  ``MINIMUM_CONTEXT_LENGTH``.

* :func:`replay_compression_warning` — re-emit a stored warning through
  the gateway ``status_callback`` once it's wired up (the callback is
  set after :class:`AIAgent` construction).

* :func:`compress_context` — the actual compression call.  Runs the
  configured compressor, splits the SQLite session, rotates the
  session_id, notifies plugin context engines / memory providers, and
  returns the compressed message list and freshly-built system prompt.

* :func:`try_shrink_image_parts_in_messages` — image-too-large recovery
  helper that re-encodes ``data:image/...;base64,...`` parts at a smaller
  size so retries can fit under provider ceilings (Anthropic's 5 MB).

``run_agent`` keeps thin wrappers for each so existing call sites
(``self._compress_context(...)``) keep working.  Tests that exercise
these paths see no behavioural change.
"""

from __future__ import annotations

import inspect
import logging
import os
import tempfile
import uuid
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Tuple

from agent.model_metadata import estimate_request_tokens_rough

import copy
import json
import math
import time
from typing import Any, Dict, List, Optional, Tuple


logger = logging.getLogger(__name__)

# Stable marker the gateway matches on to re-tag the auto-compaction lifecycle
# status as ``kind="compacting"`` (tui_gateway/server.py::_status_update), so
# drivers like the desktop app can show an explicit "Summarizing…" indicator
# instead of the transcript appearing to silently reset. Keep the marker phrase
# intact if you reword COMPACTION_STATUS.
COMPACTION_STATUS_MARKER = "Compacting context"
COMPACTION_STATUS = (
    f"🗜️ {COMPACTION_STATUS_MARKER} — summarizing earlier conversation so I can continue..."
)


def _lock_api_is_absent_on_session_db(lock_db: Any) -> bool:
    """Whether the live in-memory SessionDB class structurally predates locks.

    In the supported hot-reload skew, this module is new while the already
    imported ``hermes_state.SessionDB`` class (and its live instances) is old.
    Only that exact class identity may fail open. Proxies, nominal lookalikes,
    non-callables, and descriptor failures must fail closed. Static lookup
    avoids invoking a present-but-broken descriptor.
    """
    try:
        from hermes_state import SessionDB

        missing = object()
        return (
            type(lock_db) is SessionDB
            and inspect.getattr_static(
                SessionDB, "try_acquire_compression_lock", missing
            ) is missing
        )
    except Exception:
        return False


def _compression_lock_holder(agent: Any) -> str:
    """Build a unique holder id for the lock: pid:tid:agent-instance:uuid.

    The pid+tid prefix lets ops tell crashed/abandoned holders apart from
    live ones (expiry-based recovery uses the timestamp, but ``holder``
    is what shows up in diagnostics + log lines). The agent instance id
    and a per-acquire uuid disambiguate two co-resident agents on the
    same thread (background_review forks run on a worker thread, but
    on machines where compression itself dispatches to a thread pool
    we want each acquire to be unique).
    """
    import threading
    return (
        f"pid={os.getpid()}"
        f":tid={threading.get_ident()}"
        f":agent={id(agent):x}"
        f":nonce={uuid.uuid4().hex[:8]}"
    )


class _CompressionLockLeaseRefresher:
    def __init__(
        self,
        db: Any,
        session_id: str,
        holder: str,
        ttl_seconds: float,
        refresh_interval_seconds: float | None = None,
    ) -> None:
        self._db = db
        self._session_id = session_id
        self._holder = holder
        self._ttl_seconds = ttl_seconds
        if refresh_interval_seconds is None:
            refresh_interval_seconds = max(1.0, min(60.0, ttl_seconds / 2.0))
        self._refresh_interval_seconds = max(0.1, float(refresh_interval_seconds))
        # Tolerate transient refresh failures for at most one lease's worth of
        # time, so the give-up window is genuinely bounded by the TTL the
        # acquirer set (a single blip recovers on the next tick; a persistent
        # failure stops before the lease could outlive its TTL). Floor of 1 so a
        # degenerate interval >= ttl still tolerates one blip.
        self._max_consecutive_failures = max(
            1, int(self._ttl_seconds / self._refresh_interval_seconds)
        )
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="compression-lock-refresh",
            daemon=True,
        )

    def start(self) -> "_CompressionLockLeaseRefresher":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        # join() may time out while the refresher is mid-UPDATE; that's safe —
        # it's a daemon thread, and a late refresh on an already-released lock
        # matches rowcount 0 (a no-op). stop() returning does not guarantee the
        # thread has fully quiesced, only that we've signalled it and waited
        # briefly.
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=1.0)

    def _run(self) -> None:
        # A single falsy refresh must NOT permanently kill the lease: a
        # transient DB blip (write contention escaping _execute_write's retry
        # budget, a momentary "database is locked") returns False just like a
        # genuine lost-ownership, but only the latter should stop the loop.
        # Tolerate consecutive failures for at most one lease's worth of time
        # (_max_consecutive_failures = ttl / interval), so a one-off blip
        # recovers on the next tick while the total give-up window stays bounded
        # by the TTL the acquirer set — the lock can never be held past its TTL
        # by a stuck refresher.
        consecutive_failures = 0
        while not self._stop.wait(self._refresh_interval_seconds):
            try:
                refreshed = self._db.refresh_compression_lock(
                    self._session_id,
                    self._holder,
                    ttl_seconds=self._ttl_seconds,
                )
            except Exception as exc:
                logger.debug("compression lock refresh raised: %s", exc)
                refreshed = False
            if refreshed:
                consecutive_failures = 0
                continue
            consecutive_failures += 1
            if consecutive_failures >= self._max_consecutive_failures:
                logger.debug(
                    "compression lock refresh failed %d times in a row; "
                    "stopping lease refresher for session %s",
                    consecutive_failures, self._session_id,
                )
                break


def check_compression_model_feasibility(agent: Any) -> None:
    """Warn at session start if the auxiliary compression model's context
    window is smaller than the main model's compression threshold.

    When the auxiliary model cannot fit the content that needs summarising,
    compression will either fail outright (the LLM call errors) or produce
    a severely truncated summary.

    Called during ``AIAgent.__init__`` so CLI users see the warning
    immediately (via ``_vprint``).  The gateway sets ``status_callback``
    *after* construction, so :func:`replay_compression_warning` re-sends
    the stored warning through the callback on the first
    ``run_conversation()`` call.
    """
    if not agent.compression_enabled:
        return
    try:
        from agent.auxiliary_client import (
            _resolve_task_provider_model,
            _try_configured_fallback_for_unavailable_client,
            get_text_auxiliary_client,
        )
        from agent.model_metadata import (
            MINIMUM_CONTEXT_LENGTH,
            get_model_context_length,
        )

        # Best-effort aux provider label for the warning message. The
        # configured provider may be "auto", in which case we fall back
        # to the client's base_url hostname so the user can still tell
        # where the compression model is actually being called.
        try:
            _aux_cfg_provider, _, _, _, _ = _resolve_task_provider_model("compression")
        except Exception:
            _aux_cfg_provider = ""
        client, aux_model = get_text_auxiliary_client(
            "compression",
            main_runtime=agent._current_main_runtime(),
        )
        if client is None or not aux_model:
            fb_client, fb_model, fb_label = _try_configured_fallback_for_unavailable_client(
                "compression",
                _aux_cfg_provider,
            )
            if fb_client is not None and fb_model:
                client, aux_model = fb_client, fb_model
                if "(" in fb_label and fb_label.endswith(")"):
                    _aux_cfg_provider = fb_label.rsplit("(", 1)[1][:-1]
        if client is None or not aux_model:
            if _aux_cfg_provider and _aux_cfg_provider != "auto":
                msg = (
                    "⚠ Configured auxiliary compression provider "
                    f"'{_aux_cfg_provider}' is unavailable — context "
                    "compression will drop middle turns without a summary. "
                    "Check auxiliary.compression in config.yaml and "
                    "reauthenticate that provider."
                )
            else:
                msg = (
                    "⚠ No auxiliary LLM provider configured — context "
                    "compression will drop middle turns without a summary. "
                    "Run `hermes setup` or set OPENROUTER_API_KEY."
                )
            agent._compression_warning = msg
            agent._emit_status(msg)
            logger.warning(
                "No auxiliary LLM provider for compression — "
                "summaries will be unavailable."
            )
            return

        aux_base_url = str(getattr(client, "base_url", ""))
        # ``client.api_key`` may be a callable (Azure Foundry Entra ID
        # bearer provider). The context-length resolver chain expects a
        # string, but it only needs a key for live catalogue probes
        # (provider model lists). For Entra clients the model-metadata
        # chain still resolves via models.dev + hardcoded family
        # fallbacks, which don't require auth — pass empty string rather
        # than minting a bearer JWT just to look up a context length.
        _raw_aux_key = getattr(client, "api_key", "")
        aux_api_key = "" if (callable(_raw_aux_key) and not isinstance(_raw_aux_key, str)) else str(_raw_aux_key or "")

        aux_context = get_model_context_length(
            aux_model,
            base_url=aux_base_url,
            api_key=aux_api_key,
            config_context_length=getattr(agent, "_aux_compression_context_length_config", None),
            # Each model must be resolved with its own provider so that
            # provider-specific paths (e.g. Bedrock static table, OpenRouter API)
            # are invoked for the correct client, not inherited from the main model.
            provider=(_aux_cfg_provider if _aux_cfg_provider and _aux_cfg_provider != "auto" else getattr(agent, "provider", "")),
            custom_providers=agent._custom_providers,
        )

        # Hard floor: the auxiliary compression model must have at least
        # MINIMUM_CONTEXT_LENGTH (64K) tokens of context.  The main model
        # is already required to meet this floor (checked earlier in
        # __init__), so the compression model must too — otherwise it
        # cannot summarise a full threshold-sized window of main-model
        # content.  Mirrors the main-model rejection pattern.
        if aux_context and aux_context < MINIMUM_CONTEXT_LENGTH:
            raise ValueError(
                f"Auxiliary compression model {aux_model} has a context "
                f"window of {aux_context:,} tokens, which is below the "
                f"minimum {MINIMUM_CONTEXT_LENGTH:,} required by Hermes "
                f"Agent.  Choose a compression model with at least "
                f"{MINIMUM_CONTEXT_LENGTH // 1000}K context (set "
                f"auxiliary.compression.model in config.yaml), or set "
                f"auxiliary.compression.context_length to override the "
                f"detected value if it is wrong."
            )

        threshold = agent.context_compressor.threshold_tokens
        if aux_context < threshold:
            # Auto-correct: lower the live session threshold so
            # compression actually works this session.  The hard floor
            # above guarantees aux_context >= MINIMUM_CONTEXT_LENGTH,
            # so the new threshold is always >= 64K.
            #
            # The compression summariser sends a single user-role
            # prompt (no system prompt, no tools) to the aux model, so
            # new_threshold == aux_context is safe: the request is
            # the raw messages plus a small summarisation instruction.
            old_threshold = threshold
            new_threshold = aux_context
            agent.context_compressor.threshold_tokens = new_threshold
            # Keep threshold_percent in sync so future main-model
            # context_length changes (update_model) re-derive from a
            # sensible number rather than the original too-high value.
            main_ctx = agent.context_compressor.context_length
            if main_ctx:
                agent.context_compressor.threshold_percent = (
                    new_threshold / main_ctx
                )
            safe_pct = int((aux_context / main_ctx) * 100) if main_ctx else 50
            # Build human-readable "model (provider)" labels for both
            # the main model and the compression model so users can
            # tell at a glance which provider each side is actually
            # using. When the configured provider is empty or "auto",
            # fall back to the client's base_url hostname.
            _main_model = getattr(agent, "model", "") or "?"
            _main_provider = getattr(agent, "provider", "") or ""
            _aux_provider_label = (
                _aux_cfg_provider
                if _aux_cfg_provider and _aux_cfg_provider != "auto"
                else ""
            )
            if not _aux_provider_label:
                try:
                    from urllib.parse import urlparse
                    _aux_provider_label = (
                        urlparse(aux_base_url).hostname or aux_base_url
                    )
                except Exception:
                    _aux_provider_label = aux_base_url or "auto"
            _main_label = (
                f"{_main_model} ({_main_provider})"
                if _main_provider
                else _main_model
            )
            _aux_label = f"{aux_model} ({_aux_provider_label})"
            msg = (
                f"⚠ Compression model {_aux_label} context is "
                f"{aux_context:,} tokens, but the main model "
                f"{_main_label}'s compression threshold was "
                f"{old_threshold:,} tokens. "
                f"Auto-lowered this session's threshold to "
                f"{new_threshold:,} tokens so compression can run.\n"
                f"  To make this permanent, edit config.yaml — either:\n"
                f"  1. Use a larger compression model:\n"
                f"       auxiliary:\n"
                f"         compression:\n"
                f"           model: <model-with-{old_threshold:,}+-context>\n"
                f"  2. Lower the compression threshold:\n"
                f"       compression:\n"
                f"         threshold: 0.{safe_pct:02d}"
            )
            agent._compression_warning = msg
            agent._emit_status(msg)
            logger.warning(
                "Auxiliary compression model %s has %d token context, "
                "below the main model's compression threshold of %d "
                "tokens — auto-lowered session threshold to %d to "
                "keep compression working.",
                aux_model,
                aux_context,
                old_threshold,
                new_threshold,
            )
    except ValueError:
        # Hard rejections (aux below minimum context) must propagate
        # so the session refuses to start.
        raise
    except Exception as exc:
        logger.debug(
            "Compression feasibility check failed (non-fatal): %s", exc
        )


def replay_compression_warning(agent: Any) -> None:
    """Re-send the compression warning through ``status_callback``.

    During ``__init__`` the gateway's ``status_callback`` is not yet
    wired, so ``_emit_status`` only reaches ``_vprint`` (CLI).  This
    method is called once at the start of the first
    ``run_conversation()`` — by then the gateway has set the callback,
    so every platform (Telegram, Discord, Slack, etc.) receives the
    warning.
    """
    msg = getattr(agent, "_compression_warning", None)
    if msg and agent.status_callback:
        try:
            agent.status_callback("lifecycle", msg)
        except Exception:
            pass


def conversation_history_after_compression(agent: Any, messages: list) -> Optional[list]:
    """Return the correct flush baseline after a compression boundary.

    Legacy compression rotates to a fresh child session. That child has not
    seen the compacted transcript through the normal same-turn flush path yet,
    so callers must clear ``conversation_history`` to ``None`` and let the next
    persistence call write the whole compacted list.

    In-place compaction is different: ``archive_and_compact()`` has already
    soft-archived the previous active rows and inserted ``messages`` as the new
    active live transcript under the same session id. If the same agent turn
    continues with ``conversation_history=None``, the identity-based flush path
    treats those already-persisted compacted dicts as new and appends them a
    second time, doubling the active context and retriggering compression.

    A shallow copy is intentional: it captures the current compacted dict
    identities as history while allowing later same-turn appends to remain new.
    """
    if bool(getattr(agent, "_last_compaction_in_place", False)):
        return list(messages)
    return None


def _ensure_compressed_has_user_turn(original_messages: list, compressed: list) -> None:
    """Preserve a real user turn when a compressor returns assistant/tool-only context.

    On repeated compaction the protected head decays to the system prompt only,
    the middle summary can land as ``role="assistant"``, and a tool-heavy tail
    can be all assistant/tool — so the compacted transcript can legitimately
    contain zero user messages. Strict chat templates (LM Studio / llama.cpp
    Jinja) then fail with "No user query found in messages" (#55677).

    The restored turn is appended at the END: the guard only runs when
    ``compressed`` currently ends with an assistant/tool message (any existing
    user turn — including a todo-snapshot append — short-circuits the
    ``any()`` check), so appending a user message never creates consecutive
    same-role messages. ``_fresh_compaction_message_copy`` copies the message
    and strips the ``_db_persisted`` marker so the rotation/in-place flush
    still persists the restored row to the new session (#57491).

    If the pre-compression transcript itself carried no user turn at all
    (near-impossible — every real conversation opens with a user request —
    but kept as a defensive backstop), a minimal continuation marker is
    appended instead so strict templates still see a user message.
    """
    if any(isinstance(msg, dict) and msg.get("role") == "user" for msg in compressed):
        return
    from agent.context_compressor import _fresh_compaction_message_copy

    for msg in reversed(original_messages):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        compressed.append(_fresh_compaction_message_copy(msg))
        return
    compressed.append({
        "role": "user",
        "content": (
            "Continue from the compressed conversation context above. "
            "This marker exists because the compacted transcript contained "
            "no preserved user turn."
        ),
    })


def compress_context(
    agent: Any,
    messages: list,
    system_message: str,
    *,
    approx_tokens: Optional[int] = None,
    task_id: str = "default",
    focus_topic: Optional[str] = None,
    force: bool = False,
    defer_context_engine_notification: bool = False,
) -> Tuple[list, str, Optional[str]]:
    """Compress conversation context and split the session in SQLite.

    Returns:
        ``(compressed_messages, new_system_prompt, post_compact_context)`` tuple.
        ``post_compact_context`` is a string returned by ``post_context_compact``
        plugin hooks, or ``None``. When non-``None``, the caller injects this
        as an ephemeral re-anchoring message into the next user turn.

    Args:
        agent: The owning :class:`AIAgent`.
        messages: Current message history (will be summarised).
        system_message: Current system prompt; rebuilt after compression.
        approx_tokens: Pre-compression token estimate, logged for ops.
        task_id: Tool task scope (used for clearing file-read dedup state).
        focus_topic: Optional focus string for guided compression — the
            summariser will prioritise preserving information related to
            this topic.  Inspired by Claude Code's ``/compact <focus>``.
        force: If True, bypass any active summary-failure cooldown.  Set
            by the manual ``/compress`` slash command so users can retry
            immediately after an auto-compress abort.  Auto-compress
            callers use the default ``False``.
        defer_context_engine_notification: If True, queue the boundary hook
            (if any) for the caller to commit or discard via
            ``finalize_context_engine_compression_notification(agent,
            committed=True/False)`` after its durable host transaction.
            If False (the default for auto-compress callers), fire the
            boundary hook synchronously when a rotation/in-place boundary
            occurs — the prior behavior. Host handlers (the ``/compress``
            slash command, the TUI ``session.compress`` method) pass
            ``True`` so the boundary hook does not fire against a child
            session the host has not yet committed.

    Returns:
        ``(compressed_messages, new_system_prompt)`` tuple.  When
        compression aborts (aux LLM failed to produce a usable summary),
        returns the original messages unchanged and the existing system
        prompt — the session is NOT rotated.  Callers should detect the
        no-op via ``len(returned) == len(input)`` and stop the retry loop.

        NOTE: this block documents the *abort-path* return shape only.
        The function as a whole returns the 3-tuple documented above
        (``(compressed_messages, new_system_prompt, post_compact_context)``)
        on every reachable path, including this abort path — the third
        element is ``None`` here because no boundary was crossed.
    """
    # Codex app-server sessions: the codex agent owns the real thread context;
    # Hermes' summarizer would only rewrite a local mirror without shrinking
    # the actual thread (#36801). Route compaction to the app server's own
    # thread/compact mechanism. Behavior is controlled by
    # ``compression.codex_app_server_auto`` (native|hermes|off).
    if getattr(agent, "api_mode", None) == "codex_app_server":
        return _compress_context_via_codex_app_server(
            agent,
            messages,
            system_message,
            approx_tokens=approx_tokens,
            task_id=task_id,
            force=force,
        )

    # Every automatic entrypoint must honor compressor-owned cooldown and
    # breaker state. Gateway hygiene constructs a fresh AIAgent, so the
    # persisted fallback streak is loaded by bind_session_state() before this.
    if not force:
        blocked = getattr(
            type(agent.context_compressor),
            "_automatic_compression_blocked",
            None,
        )
        if callable(blocked) and blocked(agent.context_compressor):
            existing_prompt = getattr(agent, "_cached_system_prompt", None)
            if not existing_prompt:
                existing_prompt = agent._build_system_prompt(system_message)
            return messages, existing_prompt, None

    # Lazy feasibility check — run the auxiliary-provider probe + context
    # length lookup just-in-time on the first compression attempt instead of
    # at AIAgent.__init__. Saves ~400ms cold off every short session that
    # never reaches the threshold (the vast majority of ``chat -q`` runs).
    # The check itself sets ``agent._compression_warning`` so the
    # status-callback replay machinery still emits the warning to the user
    # the first time it would matter.
    if not getattr(agent, "_compression_feasibility_checked", False):
        # Mark as checked only after the probe completes. If the check
        # raises (e.g. a fatal aux-context ValueError that aborts the
        # session), leaving the flag unset is harmless; a non-fatal
        # transient failure is swallowed inside the function so the flag
        # is set normally on the next successful pass.
        check_compression_model_feasibility(agent)
        agent._compression_feasibility_checked = True

    _pre_msg_count = len(messages)
    # In-place compaction (config: compression.in_place, see #38763). When True,
    # this compaction rewrites the message list + rebuilds the system prompt but
    # keeps the SAME session_id — no end_session, no parent_session_id child, no
    # `name #N` renumber, no contextvar/env/logging re-sync, no memory/context-
    # engine session-switch. The conversation keeps one durable id for life,
    # eliminating the session-rotation bug cluster. Default False during rollout.
    in_place = bool(getattr(agent, "compression_in_place", False))
    # Set True once the in-place DB write actually completes (the DB block can
    # raise and skip it). Surfaced to the gateway via agent._last_compaction_in_place.
    compacted_in_place = False
    logger.info(
        "context compression started: session=%s messages=%d tokens=~%s model=%s focus=%r",
        agent.session_id or "none", _pre_msg_count,
        f"{approx_tokens:,}" if approx_tokens else "unknown", agent.model,
        focus_topic,
    )
    agent._emit_status(COMPACTION_STATUS)

    # ── Compression lock ────────────────────────────────────────────────
    # Atomic, state.db-backed lock per session_id.  Without this, two
    # AIAgent instances that share the same session_id (most commonly the
    # parent-turn agent and its background-review fork — see
    # ``agent/background_review.py``: ``review_agent.session_id =
    # agent.session_id``) can each call compress() on overlapping
    # snapshots of the same conversation.  Both succeed, both rotate
    # ``agent.session_id`` to a fresh id, both create child sessions in
    # state.db parented to the same old id.  The gateway's SessionEntry
    # only catches one rotation, so the other child becomes an orphan
    # that silently accumulates writes — Damien's repro shape.
    #
    # Acquire keyed on the OLD session_id (the rotation target's parent),
    # because that's the id that competing paths see and read from
    # SessionEntry at the start of their own compression attempt.
    #
    # If we can't acquire the lock, another path is mid-compression on
    # this session.  Aborting is correct: the messages are unchanged, the
    # other path's rotation will produce the canonical new session_id,
    # and our caller's auto-compress loop sees ``len(returned) == len(input)``
    # and stops retrying for this cycle. The session is NOT corrupted —
    # we just sit out this round and let the winner finish.
    _lock_db = getattr(agent, "_session_db", None)
    _lock_sid = agent.session_id or ""
    _lock_holder: Optional[str] = None
    # Probe whether the lock subsystem is actually available on this
    # SessionDB instance. A process running mismatched module versions can have
    # this call site while its long-lived SessionDB instance predates the lock
    # API. Only that structural absence is safe to fail open for: compression
    # must make progress rather than spin forever after an update. Once the
    # method has been resolved, every exception from its implementation fails
    # closed because proceeding without a lock can fork the session lineage.
    _try_acquire_lock = None
    _lock_lookup_error: Optional[Exception] = None
    _legacy_session_db_without_lock_api = False
    if _lock_db is not None:
        try:
            _legacy_session_db_without_lock_api = _lock_api_is_absent_on_session_db(
                _lock_db
            )
        except Exception as exc:
            _lock_lookup_error = exc
        if _lock_lookup_error is None and not _legacy_session_db_without_lock_api:
            try:
                _try_acquire_lock = _lock_db.try_acquire_compression_lock
                if not callable(_try_acquire_lock):
                    _lock_lookup_error = TypeError(
                        "compression lock API is present but not callable"
                    )
            except Exception as exc:
                _lock_lookup_error = exc
    try:
        _lock_ttl = float(getattr(agent, "_compression_lock_ttl_seconds", 300.0) or 300.0)
    except (TypeError, ValueError):
        _lock_ttl = 300.0
    _lock_refresh_interval = getattr(agent, "_compression_lock_refresh_interval", None)
    _lock_refresher: Optional[_CompressionLockLeaseRefresher] = None
    if _lock_db is not None and _lock_sid:
        _lock_holder = _compression_lock_holder(agent)
        if _lock_lookup_error is not None:
            # Attribute lookup itself failed for a reason other than a missing
            # lock API. It is unsafe to proceed without a lock in that case.
            _lock_holder = None
            logger.warning(
                "compression lock lookup raised unexpectedly for session=%s "
                "(%s: %s) — skipping compression this cycle",
                _lock_sid, type(_lock_lookup_error).__name__, _lock_lookup_error,
            )
            _lock_acquired = False
        elif _try_acquire_lock is None:
            # The lock API itself is absent on this in-memory instance. Log once
            # and proceed unlocked so an update-version skew cannot leave the
            # outer auto-compression loop making no progress forever.
            _lock_holder = None
            if getattr(agent, "_last_compression_lock_error_sid", None) != _lock_sid:
                agent._last_compression_lock_error_sid = _lock_sid
                logger.warning(
                    "compression lock subsystem unavailable for session=%s "
                    "— proceeding without lock. This usually means a stale "
                    "in-memory module after an update; restart the process "
                    "(or `hermes update`) to resync.",
                    _lock_sid,
                )
            _lock_acquired = True  # acquired-but-unlocked compatibility path
        else:
            try:
                _lock_acquired = _try_acquire_lock(
                    _lock_sid, _lock_holder, ttl_seconds=_lock_ttl
                )
            except Exception as _lock_err:
                # The method exists and entered its implementation but failed.
                # Do not mistake an internal AttributeError or TypeError for
                # version skew: fail closed and preserve session lineage. A
                # failure after SQLite committed the acquire can leave our
                # holder row behind, so release it best-effort before returning
                # unchanged messages; release is holder-qualified and safe when
                # acquisition never succeeded.
                try:
                    _lock_db.release_compression_lock(_lock_sid, _lock_holder)
                except Exception as _release_err:
                    logger.debug(
                        "compression lock cleanup after failed acquire failed: %s",
                        _release_err,
                    )
                _lock_holder = None
                logger.warning(
                    "compression lock acquisition raised unexpectedly for "
                    "session=%s (%s: %s) — skipping compression this cycle",
                    _lock_sid, type(_lock_err).__name__, _lock_err,
                )
                _lock_acquired = False
        if not _lock_acquired:
            try:
                existing = _lock_db.get_compression_lock_holder(_lock_sid)
            except Exception:
                existing = None
            logger.warning(
                "compression skipped: another path is compressing session=%s "
                "(holder=%s) — returning messages unchanged to avoid session fork",
                _lock_sid, existing,
            )
            _lock_holder = None  # don't release a lock we don't own
            # Surface to the user once — quiet for downstream auto-compress loops
            if getattr(agent, "_last_compression_lock_warning_sid", None) != _lock_sid:
                agent._last_compression_lock_warning_sid = _lock_sid
                try:
                    agent._emit_warning(
                        "⚠ Skipping concurrent compression — another path "
                        "is already compressing this session. Will retry "
                        "after it finishes."
                    )
                except Exception:
                    pass
            _existing_sp = getattr(agent, "_cached_system_prompt", None)
            if not _existing_sp:
                _existing_sp = agent._build_system_prompt(system_message)
            return messages, _existing_sp, None
        if _lock_holder is not None:
            _lock_refresher = _CompressionLockLeaseRefresher(
                _lock_db,
                _lock_sid,
                _lock_holder,
                _lock_ttl,
                _lock_refresh_interval,
            ).start()

    def _release_lock() -> None:
        """Release the lock keyed on the OLD session_id (before rotation)."""
        if _lock_refresher is not None:
            _lock_refresher.stop()
        if _lock_db is not None and _lock_sid and _lock_holder:
            try:
                _lock_db.release_compression_lock(_lock_sid, _lock_holder)
            except Exception as _rel_err:
                logger.debug("compression lock release failed: %s", _rel_err)

    # Notify external memory provider before compression discards context
    if agent._memory_manager:
        try:
            agent._memory_manager.on_pre_compress(messages)
        except Exception:
            pass

    try:
        compressed = agent.context_compressor.compress(messages, current_tokens=approx_tokens, focus_topic=focus_topic, force=force)
    except TypeError:
        # Plugin context engine with strict signature that doesn't accept
        # focus_topic / force — fall back to calling without them.
        try:
            compressed = agent.context_compressor.compress(messages, current_tokens=approx_tokens)
        except BaseException:
            _release_lock()
            raise
    except BaseException:
        # ANY exception during compress() must release the lock so the
        # session isn't permanently blocked from future compression.
        _release_lock()
        raise

    # Capture boundary quality before session-rotation callbacks run. Built-in
    # and plugin lifecycle hooks may reset per-session compressor fields while
    # rebinding to the child id; the completed attempt's verdict must survive
    # that rebind and be recorded only after the full boundary commits.
    _compression_made_progress = bool(
        getattr(agent.context_compressor, "_last_compression_made_progress", False)
    )
    _compression_used_fallback = bool(
        getattr(agent.context_compressor, "_last_summary_fallback_used", False)
    )

    # If compression aborted (aux LLM failed to produce a usable summary)
    # the compressor returns the input messages unchanged.  Surface the
    # error to the user, skip the session-rotation work entirely (no
    # session has logically ended), and let auto-compress callers detect
    # the no-op via len(returned) == len(input).
    if getattr(agent.context_compressor, "_last_compress_aborted", False):
        try:
            _err = getattr(agent.context_compressor, "_last_summary_error", None) or "unknown error"
            if getattr(agent, "_last_compression_summary_warning", None) != _err:
                agent._last_compression_summary_warning = _err
                agent._emit_warning(
                    f"⚠ Compression aborted: {_err}. "
                    "No messages were dropped — conversation continues unchanged. "
                    "Run /compress to retry, or /new to start a fresh session."
                )
            _existing_sp = getattr(agent, "_cached_system_prompt", None)
            if not _existing_sp:
                _existing_sp = agent._build_system_prompt(system_message)
            return messages, _existing_sp, None
        finally:
            _release_lock()

    # A compressor that returns the exact input object made no structural
    # progress. Do not rotate/rewrite the session or arm post-compression
    # deferral in that case; its own anti-thrash counter records the no-op.
    # NOTE: must return a 3-tuple (messages, system_prompt, post_compact_context).
    # Every production caller in run_agent.py / conversation_loop.py /
    # turn_context.py / cli.py / tui_gateway/server.py / acp_adapter/server.py /
    # gateway/{run,slash_commands}.py unpacks 3 names. A 2-tuple here crashes
    # the loop with ``ValueError: too many values to unpack (expected 2)``
    # mid-compression — see the 299k-token /compress crash on 2026-07-22.
    if compressed is messages:
        logger.info(
            "Compression made no progress (session=%s) — skipping boundary rewrite.",
            agent.session_id or "none",
        )
        _existing_sp = getattr(agent, "_cached_system_prompt", None)
        if not _existing_sp:
            _existing_sp = agent._build_system_prompt(system_message)
        _release_lock()
        return messages, _existing_sp, None

    try:
        summary_error = getattr(agent.context_compressor, "_last_summary_error", None)
        if summary_error:
            if getattr(agent, "_last_compression_summary_warning", None) != summary_error:
                agent._last_compression_summary_warning = summary_error
                agent._emit_warning(
                    f"⚠ Compression summary failed: {summary_error}. "
                    "Inserted a fallback context marker."
                )
        else:
            # No hard failure — but did the configured aux model error out
            # and get recovered by retrying on main?  Surface that so users
            # know their auxiliary.compression.model setting is broken even
            # though compression succeeded.
            _aux_fail_model = getattr(agent.context_compressor, "_last_aux_model_failure_model", None)
            _aux_fail_err = getattr(agent.context_compressor, "_last_aux_model_failure_error", None)
            if _aux_fail_model:
                # Dedup on (model, error) so we don't spam on every compaction
                _aux_key = (_aux_fail_model, _aux_fail_err)
                if getattr(agent, "_last_aux_fallback_warning_key", None) != _aux_key:
                    agent._last_aux_fallback_warning_key = _aux_key
                    agent._emit_warning(
                        f"ℹ Configured compression model '{_aux_fail_model}' failed "
                        f"({_aux_fail_err or 'unknown error'}). Recovered using main model — "
                        "check auxiliary.compression.model in config.yaml."
                    )

        todo_snapshot = agent._todo_store.format_for_injection()
        if todo_snapshot:
            compressed.append({"role": "user", "content": todo_snapshot})
        _ensure_compressed_has_user_turn(messages, compressed)

        agent._invalidate_system_prompt()
        new_system_prompt = agent._build_system_prompt(system_message)
        agent._cached_system_prompt = new_system_prompt

        if agent._session_db:
            try:
                # Trigger memory extraction on the current session before the
                # transcript is rewritten (runs in BOTH modes — the logical
                # conversation's pre-compaction turns are about to be summarized
                # away regardless of whether the id rotates).
                agent.commit_memory_session(messages)

                if in_place:
                    # ── In-place compaction: keep the same session_id ──────────
                    # No end_session, no new row, no parent_session_id, no title
                    # renumber, no contextvar/env/logging re-sync. The session's
                    # id, title, cwd, /goal, and gateway routing all stay put.
                    #
                    # Durable, NON-DESTRUCTIVE replace: soft-archive the
                    # pre-compaction turns (active=0, kept on disk + FTS-searchable +
                    # recoverable) and insert `compressed` as the new live (active=1)
                    # set, atomically. `compressed` already carries the surviving
                    # tail (current-turn messages the compressor kept via
                    # protect_last_n), so we DON'T pre-flush here — a flush would
                    # INSERT current-turn rows that archive_and_compact would then
                    # archive alongside the rest (harmless but wasted writes). The
                    # live-context load filters active=1, so a resume reloads ONLY
                    # the compacted set; the original turns remain under the SAME id
                    # for search/recovery (Teknium review — keep one durable id
                    # WITHOUT destroying history, unlike a hard replace_messages).
                    # See #38763.
                    agent._session_db.archive_and_compact(agent.session_id, compressed)
                    # Reset the flush identity set so the next turn's appends are
                    # diffed against the COMPACTED transcript: the compacted dicts
                    # are passed as conversation_history next turn and skipped by
                    # identity, so only genuinely new turn messages get appended
                    # (no dup of the summary, no resurrection of dropped turns).
                    agent._flushed_db_message_ids = set()
                    # Rotation-independent signal: the conversation was compacted in
                    # place (id unchanged). The gateway reads this (NOT an id-change
                    # diff) to re-baseline transcript handling.
                    compacted_in_place = True
                else:
                    # ── Rotation: atomic end-parent + create-child write ────
                    # publish_compression_child is one transaction (close the
                    # parent, insert the child row, attach the compacted
                    # messages, set message_count/tool_call_count). Readers see
                    # either the live parent or a complete child — never an
                    # ended parent with a missing/empty child (#33906/#33907).
                    #
                    # The compression lock held by this path (see lines 600+)
                    # is what makes the rotation safe against a competing
                    # continuation: the lease is required by the publish call
                    # when _lock_holder is set.
                    #
                    # Flush any un-persisted current-turn messages to the OLD
                    # session before ending it, so they survive in the preserved
                    # parent transcript (#47202). (In-place skips this — see above.)
                    try:
                        agent._flush_messages_to_session_db(messages)
                    except Exception:
                        pass  # best-effort — don't block compression on a flush error
                    # Propagate title to the new session with auto-numbering.
                    # Read on the OLD id before we mint the child.
                    old_title = agent._session_db.get_session_title(agent.session_id)
                    old_session_id = agent.session_id
                    new_session_id = (
                        f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_"
                        f"{uuid.uuid4().hex[:6]}"
                    )
                    # Resolve the active profile so the child stays on the
                    # parent's profile (mirror _ensure_db_session's stamp —
                    # "default" persists as NULL). publish_compression_child
                    # additionally COALESCEs from the parent row, covering
                    # app-global remote sessions whose thread lacks the
                    # HERMES_HOME context.
                    try:
                        from hermes_cli.profiles import get_active_profile_name

                        _profile_for_child = get_active_profile_name()
                        if _profile_for_child == "default":
                            _profile_for_child = None
                    except Exception:
                        _profile_for_child = None
                    # Mint the new id on the agent BEFORE the atomic publish
                    # so the boundary notification that runs after this block
                    # attributes the new state correctly. On rollback below
                    # we restore the parent id; the rollback path does NOT
                    # call the context-engine notification because _old_sid
                    # is cleared.
                    agent.session_id = new_session_id
                    agent._session_db_created = False
                    try:
                        agent._session_db.publish_compression_child(
                            parent_session_id=old_session_id,
                            child_session_id=new_session_id,
                            source=agent.platform
                            or os.environ.get("HERMES_SESSION_SOURCE", "cli"),
                            model=agent.model,
                            model_config=agent._session_init_model_config,
                            system_prompt=new_system_prompt,
                            messages=compressed,
                            cwd=getattr(agent, "working_directory", None),
                            profile_name=_profile_for_child,
                            compression_lock_holder=_lock_holder,
                            require_compression_lease=_lock_holder is not None,
                        )
                    except Exception as _pub_err:
                        # Atomic publish failed — nothing was committed (the
                        # transaction rolled back), so the parent is still
                        # live and ended_at IS NULL. Restore the agent's
                        # session_id to the parent, sync the session-context
                        # surfaces (ContextVar / env / logging) back, and
                        # re-raise so the outer handler logs the orphan-
                        # avoidance warning + skips the boundary notification.
                        logger.warning(
                            "Compression publish_compression_child failed (%s) — "
                            "rolling back to parent session %s to avoid an orphan.",
                            _pub_err, old_session_id,
                        )
                        agent.session_id = old_session_id
                        try:
                            from gateway.session_context import set_current_session_id

                            set_current_session_id(agent.session_id)
                        except Exception:
                            os.environ["HERMES_SESSION_ID"] = agent.session_id
                        try:
                            from hermes_logging import set_session_context

                            set_session_context(agent.session_id)
                        except Exception:
                            pass
                        # The parent row already exists in state.db, so mark
                        # the session as created — _ensure_db_session would
                        # otherwise retry a (harmless INSERT OR IGNORE) create
                        # next turn.
                        agent._session_db_created = True
                        old_session_id = None  # no rotation happened
                        raise
                    # Ordering contract: the agent thread updates the
                    # contextvar here; the gateway propagates to SessionEntry
                    # after run_in_executor returns.
                    try:
                        from gateway.session_context import set_current_session_id

                        set_current_session_id(agent.session_id)
                    except Exception:
                        os.environ["HERMES_SESSION_ID"] = agent.session_id
                    # The gateway/tools session context (ContextVar + env)
                    # and the logging session context are SEPARATE mechanisms.
                    # The call above moves the former; the ``[session_id]``
                    # tag on log lines comes from
                    # ``hermes_logging._session_context`` (set once per turn
                    # in conversation_loop.py). Without this, post-rotation
                    # log lines in the same turn keep the STALE old id
                    # while the message/DB/gateway state carry the new one
                    # — breaking log correlation exactly at the compaction
                    # boundary (see #34089).
                    try:
                        from hermes_logging import set_session_context

                        set_session_context(agent.session_id)
                    except Exception:
                        pass
                    agent._session_db_created = True
                    # Carry a persistent /goal onto the continuation session.
                    # Compression mints a fresh child id; load_goal does a flat
                    # per-session lookup with no parent walk, so without this an
                    # active goal silently dies at the boundary (#33618).
                    try:
                        from hermes_cli.goals import migrate_goal_to_session
                        migrate_goal_to_session(old_session_id, agent.session_id, reason="compression")
                    except Exception as _goal_err:
                        logger.debug("Could not migrate goal on compression: %s", _goal_err)
                    # Auto-number the title for the continuation session
                    if old_title:
                        try:
                            new_title = agent._session_db.get_next_title_in_lineage(old_title)
                            agent._session_db.set_session_title(agent.session_id, new_title)
                        except (ValueError, Exception) as e:
                            logger.debug("Could not propagate title on compression: %s", e)

                # Shared post-write steps (both modes target agent.session_id, which
                # in-place keeps and rotation has already reassigned to the new id):
                # refresh the stored system prompt and reset the flush cursor so the
                # next turn re-bases its append diff.
                agent._session_db.update_system_prompt(agent.session_id, new_system_prompt)
                agent._last_flushed_db_idx = 0
            except Exception as e:
                # If the rotation rolled back to the parent (orphan-avoidance
                # above), agent.session_id is the still-indexed parent and
                # old_session_id was cleared — so this is recovery, not an
                # un-indexed orphan. Otherwise an earlier step failed before the
                # child was created and the warning's original meaning holds.
                if locals().get("old_session_id") is None and not in_place:
                    logger.warning(
                        "Compression rotation aborted and rolled back to the "
                        "parent session (%s): %s", agent.session_id or "?", e,
                    )
                else:
                    logger.warning("Session DB compression split failed — new session will NOT be indexed: %s", e)

        # Compaction-boundary bookkeeping, computed once. `old_session_id` is only
        # bound in the rotation branch; in-place leaves it unset. `_boundary_parent`
        # is the id the boundary notifications attribute the prior state to: the old
        # id on rotation, the (unchanged) current id in-place.
        _old_sid = locals().get("old_session_id")
        _is_boundary = bool(_old_sid) or in_place
        _boundary_parent = _old_sid or agent.session_id or ""

        # Notify the context engine that a compaction boundary occurred. Plugin
        # engines (e.g. hermes-lcm) use boundary_reason="compression" to preserve
        # DAG lineage / checkpoint per-session state across the boundary instead of
        # re-initializing fresh. See hermes-lcm#68. Built-in ContextCompressor
        # ignores kwargs. Fires in BOTH modes: rotation passes old→new ids; in-place
        # passes the SAME id (the boundary is real even though the id didn't move).
        #
        # When defer_context_engine_notification=True, the hook is queued on the
        # agent for the host transaction (slash.compress / TUI session.compress)
        # to commit/discard via finalize_context_engine_compression_notification.
        # The host will not repoint the live session onto the new id until AFTER
        # its durable write succeeds; firing the hook synchronously here would
        # signal "compression committed" against a child the host may yet roll
        # back. When False (auto-compress path, the prior default), fire
        # synchronously as before.
        try:
            if _is_boundary and hasattr(agent.context_compressor, "on_session_start"):
                _new_sid = agent.session_id or ""
                _old_sid = _boundary_parent
                if defer_context_engine_notification:
                    _queue_context_engine_compression_notification(
                        agent,
                        new_session_id=_new_sid,
                        old_session_id=_old_sid,
                    )
                else:
                    agent.context_compressor.on_session_start(
                        _new_sid,
                        boundary_reason="compression",
                        old_session_id=_old_sid,
                        platform=getattr(agent, "platform", None) or "cli",
                        conversation_id=getattr(agent, "_gateway_session_key", None),
                    )
        except Exception as _ce_err:
            logger.debug("context engine on_session_start (compression): %s", _ce_err)

        # Notify memory providers of the compaction boundary so provider-cached
        # per-session state (Hindsight's _document_id, accumulated turn buffers,
        # counters) refreshes. reset=False because the logical conversation
        # continues. See #6672. Fires in BOTH modes: in-place uses the same id as
        # parent (the conversation didn't fork, but the buffer must still be told
        # the transcript was compacted so it doesn't double-count dropped turns).
        try:
            if _is_boundary and agent._memory_manager:
                agent._memory_manager.on_session_switch(
                    agent.session_id or "",
                    parent_session_id=_boundary_parent,
                    reset=False,
                    reason="compression",
                )
        except Exception as _me_err:
            logger.debug("memory manager on_session_switch (compression): %s", _me_err)

        # Warn on repeated compressions (quality degrades with each pass).
        # Route through _emit_status (like the other compression warnings above)
        # so the warning reaches the TUI / Telegram / Discord via status_callback,
        # not just CLI stdout. _emit_status still _vprints for the CLI, and
        # storing it on _compression_warning lets replay_compression_warning
        # re-deliver it once a late-bound gateway status_callback is wired (#36908).
        _cc = agent.context_compressor.compression_count
        if _cc >= 2:
            _cc_msg = (
                f"{agent.log_prefix}⚠️  Session compressed {_cc} times — "
                f"accuracy may degrade. Consider /new to start fresh."
            )
            agent._compression_warning = _cc_msg
            agent._emit_status(_cc_msg)

        # Emit session:compress event so hooks (e.g. MemPalace sync) can ingest
        # the completed old session before its details are lost. In in-place mode
        # there is no old id (same session); ``in_place=True`` tells hooks the
        # transcript was compacted on the same id rather than rotated.
        if getattr(agent, "event_callback", None):
            try:
                agent.event_callback("session:compress", {
                    "platform": agent.platform or "",
                    "session_id": agent.session_id,
                    "old_session_id": _old_sid or "",
                    "in_place": in_place,
                    "compression_count": agent.context_compressor.compression_count,
                })
            except Exception as e:
                logger.debug("event_callback error on session:compress: %s", e)

        # Surface the compaction mode to the caller (run_conversation / gateway)
        # via a rotation-independent flag. The gateway uses this — NOT an
        # id-change diff — to re-baseline transcript handling (history_offset=0 +
        # rewrite on the same id) when compaction happened in place. See #38763.
        agent._last_compaction_in_place = compacted_in_place

        # Keep the post-compression rough estimate for diagnostics, but do not
        # treat it as provider-reported prompt usage. Schema-heavy rough estimates
        # can remain above threshold even after the next real API request fits.
        _compressed_est = estimate_request_tokens_rough(
            compressed,
            system_prompt=new_system_prompt or "",
            tools=agent.tools or None,
        )
        agent.context_compressor.last_compression_rough_tokens = _compressed_est
        agent.context_compressor.last_prompt_tokens = -1
        agent.context_compressor.last_completion_tokens = 0
        agent.context_compressor.awaiting_real_usage_after_compression = True
        # Arm the effectiveness verdict only after a completed rewrite crosses
        # the full compaction boundary. Exceptions, aborts, and no-op attempts
        # leave this false, so unrelated later usage cannot be charged to an
        # attempt that never changed the transcript.
        if _compression_made_progress:
            record_boundary = getattr(
                type(agent.context_compressor),
                "record_completed_compaction",
                None,
            )
            if callable(record_boundary):
                record_boundary(
                    agent.context_compressor,
                    used_fallback=_compression_used_fallback,
                )
            else:
                agent.context_compressor._verify_compaction_cleared_threshold = True

        # Clear the file-read dedup cache.  After compression the original
        # read content is summarised away — if the model re-reads the same
        # file it needs the full content, not a "file unchanged" stub.
        try:
            from tools.file_tools import reset_file_dedup
            reset_file_dedup(task_id)
        except Exception:
            pass

        # ── post_context_compact plugin hook ──────────────────────────────────
        # Fire once per compaction event. Plugins return context to re-anchor
        # the LLM after compression loss (todo list, loaded skill rules, plan
        # file progress, system prompt reminders). Context is consumed by the
        # caller (run_conversation) as an ephemeral re-anchoring message in the
        # next user turn — NOT persisted to the session DB.
        # Fail-open: plugin errors are caught so compression itself cannot break.
        _post_compact_context: Optional[str] = None
        try:
            from hermes_cli.plugins import has_hook, invoke_hook as _invoke_hook

            if has_hook("post_context_compact"):
                _results = _invoke_hook(
                    "post_context_compact",
                    session_id=agent.session_id or "",
                    boundary_parent=_boundary_parent or "",
                    compressed_messages=compressed,
                    system_prompt=new_system_prompt or "",
                    compression_count=agent.context_compressor.compression_count,
                    in_place=in_place,
                    platform=getattr(agent, "platform", None) or "cli",
                    model=getattr(agent, "model", None) or "",
                )
                if _results:
                    for _r in _results:
                        _raw: Optional[str] = None
                        if isinstance(_r, str) and _r.strip():
                            _raw = _r.strip()
                        elif isinstance(_r, dict):
                            _raw = _r.get("context", "") or ""
                            if not isinstance(_raw, str):
                                _raw = None
                        if _raw:
                            # Per-plugin bound: truncate at 2000 chars to prevent
                            # a misbehaving plugin from bloating the context.
                            _max_chars = getattr(agent, "_post_compact_max_chars", 2000)
                            if len(_raw) > _max_chars:
                                _raw = (
                                    _raw[:_max_chars]
                                    + f"\n\n[post_context_compact hook: truncated {len(_raw) - _max_chars} chars]"
                                )
                            _post_compact_context = _raw
                            break  # first plugin wins
        except Exception:
            pass  # fail-open

        logger.info(
            "context compression done: session=%s messages=%d->%d rough_tokens=~%s awaiting_real_usage=true",
            agent.session_id or "none", _pre_msg_count, len(compressed),
            f"{_compressed_est:,}",
        )
        return compressed, new_system_prompt, _post_compact_context
    finally:
        # Release the lock on the OLD session_id only AFTER rotation completed
        # and all post-rotation bookkeeping (memory manager, context engine,
        # file dedup) ran. A concurrent path that wakes up the moment we
        # release will see the NEW session_id in state.db / SessionEntry and
        # acquire on that — no race against our just-finished work.
        _release_lock()


def _compress_context_via_codex_app_server(
    agent: Any,
    messages: list,
    system_message: Optional[str],
    *,
    approx_tokens: Optional[int] = None,
    task_id: str = "default",
    force: bool = False,
) -> Tuple[list, str, Optional[str]]:
    """Route compaction to Codex app-server for Codex-owned threads.

    Hermes' normal compressor rewrites the local OpenAI-style transcript.
    That does not shrink the actual Codex app-server thread context. For this
    runtime, ask Codex to compact its own thread and keep Hermes' transcript
    unchanged.
    """
    auto_mode = str(
        getattr(agent, "codex_app_server_auto_compaction", "native") or "native"
    ).lower()
    if auto_mode not in {"native", "hermes", "off"}:
        auto_mode = "native"
    if not force and auto_mode != "hermes":
        logger.info(
            "codex app-server compaction skipped: mode=%s force=false "
            "(session=%s messages=%d tokens=~%s)",
            auto_mode,
            getattr(agent, "session_id", None) or "none",
            len(messages),
            f"{approx_tokens:,}" if approx_tokens else "unknown",
        )
        existing_prompt = getattr(agent, "_cached_system_prompt", None)
        if not existing_prompt:
            existing_prompt = agent._build_system_prompt(system_message)
        return messages, existing_prompt, None

    codex_session = getattr(agent, "_codex_session", None)
    if codex_session is None:
        logger.info(
            "codex app-server compaction skipped: no active codex thread "
            "(session=%s messages=%d tokens=~%s)",
            getattr(agent, "session_id", None) or "none",
            len(messages),
            f"{approx_tokens:,}" if approx_tokens else "unknown",
        )
        existing_prompt = getattr(agent, "_cached_system_prompt", None)
        if not existing_prompt:
            existing_prompt = agent._build_system_prompt(system_message)
        return messages, existing_prompt, None

    logger.info(
        "codex app-server compaction started: session=%s messages=%d tokens=~%s",
        getattr(agent, "session_id", None) or "none",
        len(messages),
        f"{approx_tokens:,}" if approx_tokens else "unknown",
    )
    try:
        agent._emit_status(COMPACTION_STATUS)
    except Exception:
        pass

    result = codex_session.compact_thread()
    if getattr(result, "should_retire", False):
        try:
            codex_session.close()
        except Exception:
            pass
        agent._codex_session = None

    if getattr(result, "interrupted", False) or getattr(result, "error", None):
        try:
            agent._emit_warning(
                f"⚠ Codex app-server compaction failed: {result.error}"
            )
        except Exception:
            pass
        existing_prompt = getattr(agent, "_cached_system_prompt", None)
        if not existing_prompt:
            existing_prompt = agent._build_system_prompt(system_message)
        return messages, existing_prompt, None

    try:
        from agent.codex_runtime import (
            _record_codex_app_server_compaction,
            _record_codex_app_server_usage,
        )

        _record_codex_app_server_compaction(
            agent,
            result,
            approx_tokens=approx_tokens,
            force=True,
        )
        # An empty usage report must consume the pending post-compaction verdict
        # rather than leaving preflight deferral armed until some unrelated later
        # Codex turn supplies usage. Minimal external test engines may not expose
        # the ContextEngine update hook; preserve their existing bookkeeping.
        if hasattr(agent.context_compressor, "update_from_response"):
            _record_codex_app_server_usage(agent, result)
    except Exception:
        logger.debug("codex compaction bookkeeping failed", exc_info=True)

    try:
        from tools.file_tools import reset_file_dedup

        reset_file_dedup(task_id)
    except Exception:
        pass

    logger.info(
        "codex app-server compaction done: session=%s thread=%s turn=%s",
        getattr(agent, "session_id", None) or "none",
        getattr(result, "thread_id", None) or "",
        getattr(result, "turn_id", None) or "",
    )
    existing_prompt = getattr(agent, "_cached_system_prompt", None)
    if not existing_prompt:
        existing_prompt = agent._build_system_prompt(system_message)
    return messages, existing_prompt, None


def try_shrink_image_parts_in_messages(
    api_messages: list,
    *,
    max_dimension: int = 8000,
) -> bool:
    """Re-encode all native image parts at a smaller size to recover from
    image-too-large errors (Anthropic 5 MB, unknown other providers).

    Mutates ``api_messages`` in place. Returns True if any image part was
    actually replaced, False if there were no image parts to shrink or
    Pillow couldn't help (caller should surface the original error).

    Strategy: look for ``image_url`` / ``input_image`` parts carrying a
    ``data:image/...;base64,...`` payload, plus Anthropic-native
    ``{"type": "image", "source": {"type": "base64", ...}}`` blocks.
    For each one whose encoded size exceeds 4 MB (a safe target that slides
    under Anthropic's 5 MB ceiling with header overhead) or whose longest side
    exceeds ``max_dimension``, write the base64 to a tempfile, call
    ``vision_tools._resize_image_for_vision`` to produce a smaller data
    URL, and substitute it in place.

    Non-data-URL images (http/https URLs) are not touched — the provider
    fetches those itself and the size limit is different.
    """
    if not api_messages:
        return False

    try:
        from tools.vision_tools import _resize_image_for_vision
    except Exception as exc:
        logger.warning("image-shrink recovery: vision_tools unavailable — %s", exc)
        return False

    # 4 MB target leaves comfortable headroom under Anthropic's 5 MB.
    # Non-Anthropic providers we haven't observed rejecting are fine with
    # much larger; shrinking to 4 MB here loses quality but only fires
    # after a confirmed provider rejection, so the alternative is failure.
    target_bytes = 4 * 1024 * 1024
    # Anthropic enforces an 8000px per-side dimension cap independently of
    # the 5 MB byte cap.  In many-image requests, the provider can report a
    # lower cap (observed: 2000px).  The caller passes that parsed ceiling
    # when the rejection includes it.
    changed_count = 0
    # Track parts that are over the target but could NOT be shrunk under it.
    # If any survive, retrying is pointless — the same oversized payload will
    # be re-sent and rejected again, wasting the single retry budget.  We only
    # report success (caller retries) when every over-threshold image was
    # actually brought under the target.
    unshrinkable_oversized = 0

    def _decode_pixels(data_url: str) -> Optional[tuple]:
        """Return ``(width, height)`` of a base64 data URL, or None on failure.

        Soft-depends on Pillow; returns None (caller falls back to a
        bytes-only check) if Pillow is missing or the payload is corrupt.
        """
        try:
            import base64 as _b64_dim
            import io as _io_dim
            header_d, _, data_d = data_url.partition(",")
            if not data_d or not data_url.startswith("data:"):
                return None
            from PIL import Image as _PILImage
            with _PILImage.open(_io_dim.BytesIO(_b64_dim.b64decode(data_d))) as _img:
                return _img.size
        except Exception:
            return None

    def _shrink_data_url(url: str) -> tuple:
        """Return ``(resized_url, unshrinkable)`` for a data URL.

        ``resized_url`` is a smaller/dimension-correct data URL, or None when
        no rewrite was applied.  ``unshrinkable`` is True only when the image
        exceeded a constraint (byte-size or dimensions) and the resize failed
        to satisfy *that same* constraint — so the caller knows retrying is
        pointless even if a different image in the request shrank.
        """
        if not isinstance(url, str) or not url.startswith("data:"):
            return None, False

        # Determine which constraint is binding.  The accept/reject gate below
        # MUST be checked against the same axis that triggered the shrink: a
        # downscaled screenshot PNG routinely re-encodes to *more* bytes than
        # the original (PNG compression is non-monotonic in image size — a
        # smaller raster with LANCZOS resampling noise compresses worse than a
        # larger smooth one).  Rejecting a pixel-correct downscale purely
        # because its bytes grew permanently wedges sessions on the Anthropic
        # many-image 2000px path (#48013).
        needs_shrink = len(url) > target_bytes  # over byte budget
        triggered_by = "bytes" if needs_shrink else None
        if not needs_shrink:
            # Bytes are fine — check pixel dimensions against the provider's
            # reported per-side cap.  A screenshot can be tiny in bytes yet
            # too large in pixels.
            dims = _decode_pixels(url)
            if dims is None:
                # Pillow missing or corrupt data — fall back to byte-only.
                return None, False
            if max(dims) <= max_dimension:
                return None, False  # both bytes and pixels are within limits
            needs_shrink = True
            triggered_by = "dimension"

        try:
            header, _, data = url.partition(",")
            mime = "image/jpeg"
            if header.startswith("data:"):
                mime_part = header[len("data:"):].split(";", 1)[0].strip()
                if mime_part.startswith("image/"):
                    mime = mime_part
            import base64 as _b64
            raw = _b64.b64decode(data)
            suffix = {
                "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
                "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/bmp": ".bmp",
            }.get(mime, ".jpg")
            tmp = tempfile.NamedTemporaryFile(
                prefix="hermes_shrink_", suffix=suffix, delete=False,
            )
            try:
                tmp.write(raw)
                tmp.close()
                resized = _resize_image_for_vision(
                    Path(tmp.name),
                    mime_type=mime,
                    max_base64_bytes=target_bytes,
                    max_dimension=max_dimension,
                )
            finally:
                try:
                    Path(tmp.name).unlink(missing_ok=True)
                except Exception:
                    pass
            if not resized:
                # Resize returned nothing — Pillow couldn't help.
                return None, True
            if triggered_by == "bytes":
                # Byte budget is the binding constraint — bytes must shrink.
                if len(resized) >= len(url):
                    return None, True  # re-encode made it bigger
                # The per-side dimension cap is ALSO an active provider
                # constraint on this request (the caller passes the parsed cap
                # to both this helper and the resizer).  _resize_image_for_vision
                # returns a best-effort, possibly-over-cap blob when it
                # exhausts its halving budget — it freezes the long side once
                # the short side hits its 64px floor, so a very-high-aspect
                # image can stay over the cap even after bytes shrank.  If the
                # output is still over the cap, retrying would re-400 on
                # dimensions; treat it as unshrinkable.  (Skip when dims can't
                # be decoded — preserves historical byte-only behaviour.)
                new_dims = _decode_pixels(resized)
                if new_dims is not None and max(new_dims) > max_dimension:
                    return None, True
                return resized, False
            # triggered_by == "dimension": the per-side cap is binding.  The
            # re-encode may have grown in bytes; accept it as long as it is now
            # within the dimension cap.  Verify the new dimensions when we can.
            new_dims = _decode_pixels(resized)
            if new_dims is not None:
                if max(new_dims) <= max_dimension:
                    return resized, False
                # Still over the per-side cap — the resize didn't satisfy it.
                return None, True
            # Couldn't verify the re-encode's dimensions (corrupt output or
            # Pillow gone mid-call).  Fall back to the historical "bytes must
            # shrink" gate so we never accept an unverifiable, byte-larger blob.
            if len(resized) >= len(url):
                return None, True
            return resized, False
        except Exception as exc:
            logger.warning("image-shrink recovery: re-encode failed — %s", exc)
            return None, triggered_by is not None

    def _source_to_data_url(source: Any) -> Optional[str]:
        if not isinstance(source, dict) or source.get("type") != "base64":
            return None
        data = source.get("data")
        if not isinstance(data, str) or not data:
            return None
        media_type = str(source.get("media_type") or "image/jpeg").strip()
        if not media_type.startswith("image/"):
            media_type = "image/jpeg"
        return f"data:{media_type};base64,{data}"

    def _write_data_url_to_source(source: dict, data_url: str) -> None:
        header, _, data = data_url.partition(",")
        media_type = "image/jpeg"
        if header.startswith("data:"):
            candidate = header[len("data:"):].split(";", 1)[0].strip()
            if candidate.startswith("image/"):
                media_type = candidate
        source["type"] = "base64"
        source["media_type"] = media_type
        source["data"] = data

    for msg in api_messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "image":
                source = part.get("source")
                url = _source_to_data_url(source)
                resized, unshrinkable = _shrink_data_url(url or "")
                if resized and isinstance(source, dict):
                    _write_data_url_to_source(source, resized)
                    changed_count += 1
                elif unshrinkable:
                    unshrinkable_oversized += 1
                continue
            if ptype not in {"image_url", "input_image"}:
                continue
            image_value = part.get("image_url")
            # OpenAI chat.completions: {"image_url": {"url": "data:..."}}
            # OpenAI Responses: {"image_url": "data:..."}
            if isinstance(image_value, dict):
                url = image_value.get("url", "")
                resized, unshrinkable = _shrink_data_url(url)
                if resized:
                    image_value["url"] = resized
                    changed_count += 1
                elif unshrinkable:
                    unshrinkable_oversized += 1
            elif isinstance(image_value, str):
                resized, unshrinkable = _shrink_data_url(image_value)
                if resized:
                    part["image_url"] = resized
                    changed_count += 1
                elif unshrinkable:
                    unshrinkable_oversized += 1

    if changed_count:
        logger.info(
            "image-shrink recovery: re-encoded %d image part(s) to fit under %.0f MB",
            changed_count, target_bytes / (1024 * 1024),
        )
    if unshrinkable_oversized:
        # At least one oversized image could not be shrunk under the target.
        # Retrying would re-send it and fail identically, so signal "no
        # progress" even if other parts shrank — the caller will surface the
        # original error rather than burning its single retry on a no-op.
        logger.warning(
            "image-shrink recovery: %d oversized image part(s) could not be "
            "shrunk under %.0f MB — not retrying (would re-send rejected payload)",
            unshrinkable_oversized, target_bytes / (1024 * 1024),
        )
        return False
    return changed_count > 0


__all__ = [
    "COMPACTION_STATUS",
    "COMPACTION_STATUS_MARKER",
    "check_compression_model_feasibility",
    "replay_compression_warning",
    "compress_context",
    "try_shrink_image_parts_in_messages",
]



# === Rule-1 catchup fix: upstream-only additions ===
# 33 missing top-level symbols (functions, classes, constants) appended in topo order so each symbol's dependencies come first. Source: upstream/main @ HEAD. Per user's 5-rule policy, these are
# upstream ADDITIONS that don't collide with our changes — they were correctly stayed-ours under the bulk-merge, but rule 1 forbids stomping them.


# --- COMPRESSION_RETRY_MESSAGES_STATUS_TEMPLATE ---
COMPRESSION_RETRY_MESSAGES_STATUS_TEMPLATE = (
    "🗜️ Compressed {before} → {after} messages, retrying..."
)


# --- PREFLIGHT_COMPRESSION_STATUS_TEMPLATE ---
PREFLIGHT_COMPRESSION_STATUS_TEMPLATE = (
    "📦 Preflight compression: ~{tokens:,} tokens "
    ">= {threshold:,} threshold. This may take a moment."
)


# --- _PENDING_CONTEXT_ENGINE_NOTIFICATION ---
_PENDING_CONTEXT_ENGINE_NOTIFICATION = (
    "_pending_context_engine_compression_notification"
)


# --- _session_was_rotated_by_compression ---
def _session_was_rotated_by_compression(session_db: Any, session_id: str) -> bool:
    """Return whether another path already rotated this compression parent."""
    getter = getattr(type(session_db), "get_session", None)
    if not callable(getter):
        return False
    session = getter(session_db, session_id)
    return bool(
        session
        and session.get("ended_at") is not None
        and session.get("end_reason") == "compression"
    )


# --- _adopt_live_compression_child ---
def _adopt_live_compression_child(
    agent: Any,
    session_db: Any,
    parent_session_id: str,
) -> Optional[List[Dict[str, Any]]]:
    """Move a stale compression contender onto the unique durable child.

    Resolve and load first, then mutate the live agent. This ordering keeps the
    stale contender fail-closed when lineage is ambiguous or the compacted
    handoff cannot be read.
    """
    finder = getattr(type(session_db), "find_live_compression_child", None)
    loader = getattr(type(session_db), "get_messages_as_conversation", None)
    if not callable(finder) or not callable(loader):
        return None
    child = finder(session_db, parent_session_id)
    if not child or not child.get("id"):
        return None
    child_session_id = str(child["id"])
    recovered = loader(session_db, child_session_id)
    if not isinstance(recovered, list) or not recovered:
        return None
    # Revalidate after loading: the child may have rotated or a competing
    # continuation may have appeared between the two DB reads.
    confirmed = finder(session_db, parent_session_id)
    if not confirmed or str(confirmed.get("id") or "") != child_session_id:
        return None

    agent.session_id = child_session_id
    try:
        from gateway.session_context import set_current_session_id

        set_current_session_id(child_session_id)
    except Exception:
        os.environ["HERMES_SESSION_ID"] = child_session_id
    try:
        from hermes_logging import set_session_context

        set_session_context(child_session_id)
    except Exception:
        pass

    agent._session_db_created = True
    if child.get("system_prompt"):
        agent._cached_system_prompt = child["system_prompt"]
    agent._last_flushed_db_idx = len(recovered)
    agent._flushed_db_message_session_id = child_session_id
    agent._flushed_db_message_ids = {
        id(message) for message in recovered if isinstance(message, dict)
    }

    on_session_start = getattr(agent.context_compressor, "on_session_start", None)
    if callable(on_session_start):
        try:
            on_session_start(
                child_session_id,
                boundary_reason="compression",
                old_session_id=parent_session_id,
                session_db=session_db,
                platform=getattr(agent, "platform", None) or "cli",
                conversation_id=getattr(agent, "_gateway_session_key", None),
            )
        except Exception as exc:
            logger.debug("context engine compression-child adoption failed: %s", exc)
    else:
        bind_state = getattr(agent.context_compressor, "bind_session_state", None)
        if callable(bind_state):
            try:
                bind_state(session_db=session_db, session_id=child_session_id)
            except Exception:
                pass
    try:
        if agent._memory_manager:
            agent._memory_manager.on_session_switch(
                child_session_id,
                parent_session_id=parent_session_id,
                reset=False,
                reason="compression",
            )
    except Exception as exc:
        logger.debug("memory manager compression-child adoption failed: %s", exc)

    return recovered


# --- recover_rotated_compression_session ---
def recover_rotated_compression_session(
    agent: Any,
) -> Optional[List[Dict[str, Any]]]:
    """Recover a stale live agent before a new turn writes to its old parent."""
    session_db = getattr(agent, "_session_db", None)
    session_id = getattr(agent, "session_id", None) or ""
    if session_db is None or not session_id:
        return None
    try:
        if not _session_was_rotated_by_compression(session_db, session_id):
            return None
        # Rotation publication holds the parent compression lease until the
        # child handoff is durable. A concurrent turn waits briefly rather than
        # observing the intentional parent-ended/child-empty intermediate state.
        holder_getter = getattr(session_db, "get_compression_lock_holder", None)
        for attempt in range(21):
            recovered = _adopt_live_compression_child(agent, session_db, session_id)
            if recovered is not None:
                return recovered
            holder = holder_getter(session_id) if callable(holder_getter) else None
            if not holder or attempt == 20:
                return None
            time.sleep(0.05)
        return None
    except Exception as exc:
        logger.warning(
            "compression session recovery failed for session=%s (%s: %s)",
            session_id,
            type(exc).__name__,
            exc,
        )
        return None


# --- _strip_stale_todo_snapshot ---
def _strip_stale_todo_snapshot(content: Any) -> Any:
    """Remove a previously merged todo-snapshot block from message content.

    Snapshot merges (see the injection site in ``compress_context``) always
    append the block at the end of the trailing user turn, so a surviving
    header marks stale todo state from an earlier compaction boundary.
    Stripping before re-injection keeps repeated boundaries from
    accumulating outdated snapshots (#26981).
    """
    from tools.todo_tool import TODO_INJECTION_HEADER

    if isinstance(content, str):
        idx = content.find(TODO_INJECTION_HEADER)
        if idx == -1:
            return content
        return content[:idx].rstrip()
    if isinstance(content, list):
        return [
            part
            for part in content
            if not (
                isinstance(part, dict)
                and part.get("type") == "text"
                and str(part.get("text") or "")
                .lstrip()
                .startswith(TODO_INJECTION_HEADER)
            )
        ]
    return content


# --- _builtin_memory_prompt_snapshot ---
def _builtin_memory_prompt_snapshot(agent: Any) -> Optional[Tuple[str, str]]:
    """Return the built-in memory text that can affect a system prompt.

    ``MemoryStore`` freezes this text until ``load_from_disk()``.  Rendering
    the frozen blocks after that reload lets compression retain the exact
    cached system prompt when it already embeds the current memory (see
    :func:`_cached_prompt_reflects_builtin_memory`).  An unreadable snapshot
    returns ``None`` so callers take the conservative rebuild path.
    """
    store = getattr(agent, "_memory_store", None)
    if store is None:
        return "", ""
    try:
        memory = (
            store.format_for_system_prompt("memory") or ""
            if getattr(agent, "_memory_enabled", False)
            else ""
        )
        user = (
            store.format_for_system_prompt("user") or ""
            if getattr(agent, "_user_profile_enabled", False)
            else ""
        )
    except Exception:
        return None
    return memory, user


# --- _cached_prompt_reflects_builtin_memory ---
def _cached_prompt_reflects_builtin_memory(agent: Any, cached_prompt: str) -> bool:
    """Whether the cached system prompt already embeds current built-in memory.

    The retention fast path must NOT compare the memory snapshot before vs
    after the disk reload: on fresh-agent surfaces (gateway, TUI) the cached
    prompt is restored from the session DB and can predate mid-session memory
    writes that the fresh ``MemoryStore`` already picked up at init — the
    snapshot is then identical on both sides of the reload while the prompt
    itself is stale, and retaining it would latch old memory for the life of
    the session (and re-persist it via ``update_system_prompt``).

    Instead, verify the CURRENT (post-reload) rendered blocks appear verbatim
    in the cached prompt, and that no leftover block header remains for a
    target whose entries have since been emptied or disabled.
    """
    snapshot = _builtin_memory_prompt_snapshot(agent)
    if snapshot is None:
        return False
    try:
        from tools.memory_tool import MEMORY_BLOCK_HEADERS
    except Exception:
        return False
    for target, block in zip(("memory", "user"), snapshot):
        block = block.strip()
        if block:
            # build_system_prompt_parts embeds the stripped block verbatim;
            # the rendered text includes the usage header, so any entry
            # change (or char-count change) breaks containment → rebuild.
            if block not in cached_prompt:
                return False
        elif MEMORY_BLOCK_HEADERS[target] in cached_prompt:
            # The prompt still carries a block for a target that is now
            # empty/disabled — stale; rebuild.
            return False
    return True


# --- _message_text ---
def _message_text(message: Any) -> str:
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text") or part.get("content") or "")
            for part in content
            if isinstance(part, dict)
        )
    return ""


# --- _SYNTHETIC_USER_FLAGS ---
_SYNTHETIC_USER_FLAGS = (
    "_todo_snapshot_synthetic",
    "_empty_recovery_synthetic",
    "_verification_stop_synthetic",
    "_pre_verify_synthetic",
    "_dropped_toolcall_nudge",
)


# --- _SYNTHETIC_USER_PREFIXES ---
_SYNTHETIC_USER_PREFIXES = (
    "[System: Your previous response was truncated",
    "[System: The previous response was cut off",
    "[System: Your previous tool call",
    "[Your active task list was preserved across context compression]",
    "[IMPORTANT: Background process ",
)


# --- _is_real_user_message ---
def _is_real_user_message(message: Any) -> bool:
    """Distinguish human intent from user-role runtime scaffolding.

    A compaction summary pinned to ``role="user"`` (the compressor flips the
    summary role to preserve alternation when the tail starts with an
    assistant message) is scaffolding too: treating it as human intent would
    short-circuit anchor restoration with a message the model is explicitly
    told NOT to act on.
    """
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    if any(message.get(flag) for flag in _SYNTHETIC_USER_FLAGS):
        return False
    text = _message_text(message).strip()
    if not text:
        return False
    if text.startswith(_SYNTHETIC_USER_PREFIXES):
        return False
    from agent.context_compressor import ContextCompressor

    return not ContextCompressor._is_synthetic_compression_user_turn(message)


# --- COMPACTION_DONE_STATUS ---
COMPACTION_DONE_STATUS = "✓ Context compaction complete — continuing turn..."


# --- _CompressionActivityHeartbeat ---
class _CompressionActivityHeartbeat:
    """Refresh the agent inactivity tracker while compression blocks in an aux call."""

    def __init__(self, agent: Any, interval_seconds: float | None = None) -> None:
        self._agent = agent
        if interval_seconds is None:
            interval_seconds = getattr(agent, "_compression_activity_heartbeat_interval", 60.0)
        try:
            interval_seconds = float(interval_seconds or 60.0)
        except (TypeError, ValueError):
            interval_seconds = 60.0
        if not math.isfinite(interval_seconds):
            interval_seconds = 60.0
        self._interval_seconds = max(0.1, interval_seconds)
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="compression-activity-heartbeat",
            daemon=True,
        )

    def start(self) -> "_CompressionActivityHeartbeat":
        self._touch("context compression started")
        self._thread.start()
        return self

    def stop(self, desc: str = "context compression completed") -> None:
        self._stop.set()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=1.0)
        self._touch(desc)

    def _touch(self, desc: str) -> None:
        try:
            touch = getattr(self._agent, "_touch_activity", None)
            if callable(touch):
                touch(desc)
        except Exception:
            logger.debug("compression activity heartbeat touch failed", exc_info=True)

    def _run(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            self._touch("context compression in progress")


# --- COMPRESSION_RETRY_TOKENS_STATUS_TEMPLATE ---
COMPRESSION_RETRY_TOKENS_STATUS_TEMPLATE = (
    "🗜️ Compressed ~{before:,} → ~{after:,} tokens, retrying..."
)


# --- _merge_anchor_into_user_message ---
def _merge_anchor_into_user_message(target: dict, anchor: dict) -> None:
    """Fold the human anchor into an existing user-role scaffolding turn.

    Used only when every insertion slot would create two consecutive
    user-role messages. The anchor text leads (it is the active task), the
    scaffolding content is preserved after it, and the synthetic flags are
    cleared because the merged turn now carries real human intent.
    """
    anchor_content = anchor.get("content")
    target_content = target.get("content")
    if isinstance(anchor_content, list) or isinstance(target_content, list):
        anchor_parts = (
            list(anchor_content)
            if isinstance(anchor_content, list)
            else [{"type": "text", "text": str(anchor_content or "")}]
        )
        target_parts = (
            list(target_content)
            if isinstance(target_content, list)
            else [{"type": "text", "text": str(target_content or "")}]
        )
        target["content"] = anchor_parts + target_parts
    else:
        merged = f"{anchor_content or ''}\n\n{target_content or ''}".strip()
        target["content"] = merged
    for flag in _SYNTHETIC_USER_FLAGS:
        target.pop(flag, None)


# --- _insert_real_user_anchor ---
def _insert_real_user_anchor(messages: list, anchor: dict) -> None:
    """Insert the latest human turn without breaking role alternation."""

    def _role(msg: Any) -> Optional[str]:
        return msg.get("role") if isinstance(msg, dict) else None

    # Preferred: the summary boundary — before the first assistant message
    # not already preceded by a user turn. The left neighbour is then
    # non-user by construction and the right neighbour is an assistant.
    for index, message in enumerate(messages):
        if _role(message) != "assistant":
            continue
        previous_role = _role(messages[index - 1]) if index > 0 else None
        if previous_role != "user":
            messages.insert(index, anchor)
            return
    # Every assistant is user-preceded (or there are none). Appending is
    # safe whenever the transcript does not already end with a user turn.
    if not messages or _role(messages[-1]) != "user":
        messages.append(anchor)
        return
    # The transcript ends with a user-role message and no slot avoids
    # user/user adjacency.
    from agent.context_compressor import ContextCompressor

    if ContextCompressor._is_context_summary_content(
        _message_text(messages[-1])
    ):
        # Never merge into a compaction summary: the summary prefix must
        # stay at the start of its message for downstream summary detection.
        # Appending after it makes the anchor "the latest user message after
        # the summary" — exactly what the handoff prefix instructs — and the
        # adjacent user turns are merged summary-first by
        # repair_message_sequence before the next API call.
        messages.append(anchor)
        return
    # Trailing user-role scaffolding (e.g. the todo snapshot): merge instead
    # of inserting a consecutive same-role message (#55677 strict templates).
    _merge_anchor_into_user_message(messages[-1], anchor)


# --- finalize_context_engine_compression_notification ---
def finalize_context_engine_compression_notification(
    agent: Any,
    *,
    committed: bool,
) -> bool:
    """Emit or discard a deferred notification; repeated calls are no-ops."""
    pending = getattr(agent, _PENDING_CONTEXT_ENGINE_NOTIFICATION, None)
    setattr(agent, _PENDING_CONTEXT_ENGINE_NOTIFICATION, None)
    if not committed or not callable(pending):
        return False
    return bool(pending())


# --- CompressionCommitFence ---
class CompressionCommitFence:
    """Fence timeout cancellation against post-summary session mutation.

    Compression itself is synchronous and may be running in an executor thread.
    A caller can stop waiting for the summary, but it cannot kill that thread.
    This fence makes the commit boundary deterministic: cancellation either wins
    before session mutation starts, or waits until an already-started commit is
    fully complete before the caller proceeds.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = False
        self._commit_started = False
        # Forward-progress telemetry: the compression worker touches this
        # whenever the streamed summary call produces a token (see
        # ContextCompressor._call_summary_llm). Waiters use it to distinguish
        # a SLOW-but-alive summary model from a HUNG one, so slow models are
        # not killed by a fixed wall-clock deadline while tokens are moving.
        self._last_progress = time.monotonic()

    def touch_progress(self) -> None:
        """Record forward progress (e.g. a streamed summary token arriving).

        Called from the compression worker thread; read by async waiters via
        :meth:`seconds_since_progress`. A bare float store is atomic in
        CPython, so no lock is needed.
        """
        self._last_progress = time.monotonic()

    def seconds_since_progress(self) -> float:
        """Seconds since the worker last reported forward progress."""
        return max(0.0, time.monotonic() - self._last_progress)

    def cancel_before_commit(self) -> bool:
        """Cancel a pending commit, or wait for an active commit to finish.

        Returns ``True`` when cancellation won before the commit boundary.
        Returns ``False`` when the worker had already entered the boundary; in
        that case acquiring this lock waits until all session mutation finishes.
        """
        with self._lock:
            if self._commit_started:
                return False
            self._cancelled = True
            return True

    def try_cancel_before_commit(self) -> Optional[bool]:
        """Non-blocking form of :meth:`cancel_before_commit`.

        Returns ``None`` while an active commit owns the fence, allowing an
        async caller to yield instead of blocking its event loop.
        """
        if not self._lock.acquire(blocking=False):
            return None
        try:
            if self._commit_started:
                return False
            self._cancelled = True
            return True
        finally:
            self._lock.release()

    def begin_commit(self) -> bool:
        """Enter the commit boundary unless cancellation already won."""
        self._lock.acquire()
        if self._cancelled:
            self._lock.release()
            return False
        self._commit_started = True
        return True

    def finish_commit(self) -> None:
        """Leave a commit boundary entered by :meth:`begin_commit`."""
        self._lock.release()


# --- PRE_API_COMPRESSION_STATUS_TEMPLATE ---
PRE_API_COMPRESSION_STATUS_TEMPLATE = (
    "📦 Pre-API compression: ~{tokens:,} tokens "
    "near the context/output limit. Compacting before the next model call."
)


# --- _notify_context_engine_compression_complete ---
def _notify_context_engine_compression_complete(
    agent: Any,
    *,
    new_session_id: str,
    old_session_id: str,
) -> bool:
    """Notify the active context engine after a durable compression commit."""
    callback = getattr(agent.context_compressor, "on_session_start", None)
    if not callable(callback):
        return False
    try:
        callback(
            new_session_id,
            boundary_reason="compression",
            old_session_id=old_session_id,
            platform=getattr(agent, "platform", None) or "cli",
            conversation_id=getattr(agent, "_gateway_session_key", None),
        )
    except Exception:
        # Context-engine hooks are observers. A callback failure must not undo
        # history that the core or an outer host transaction already committed.
        logger.debug(
            "context engine on_session_start (compression) failed",
            exc_info=True,
        )
        return False
    return True


# --- COMPRESSION_RETRY_CONTEXT_REDUCED_STATUS_TEMPLATE ---
COMPRESSION_RETRY_CONTEXT_REDUCED_STATUS_TEMPLATE = (
    "🗜️ Context reduced to {new_ctx:,} tokens (was {old_ctx:,}), retrying..."
)


# --- COMPRESSION_RETRY_TOO_LARGE_STATUS_TEMPLATE ---
COMPRESSION_RETRY_TOO_LARGE_STATUS_TEMPLATE = (
    "🗜️ Context too large (~{tokens:,} tokens) — compressing ({attempt}/{cap})..."
)


# --- _emit_compaction_done ---
def _emit_compaction_done(agent: Any) -> None:
    """Emit the structured terminal edge for a started compaction."""
    status_callback = getattr(agent, "status_callback", None)
    if not status_callback:
        return
    try:
        status_callback("compacted", COMPACTION_DONE_STATUS)
    except Exception:
        logger.debug("status_callback error in compaction completion", exc_info=True)


# --- IDLE_COMPACTION_STATUS_TEMPLATE ---
IDLE_COMPACTION_STATUS_TEMPLATE = (
    "💤 Resumed after {idle_seconds}s idle — compacting "
    "~{tokens:,} tokens before continuing."
)


# --- ROUTINE_COMPRESSION_STATUS_SAMPLES ---
ROUTINE_COMPRESSION_STATUS_SAMPLES = (
    COMPACTION_STATUS,
    PRE_API_COMPRESSION_STATUS_TEMPLATE.format(tokens=123456),
    PREFLIGHT_COMPRESSION_STATUS_TEMPLATE.format(tokens=120000, threshold=100000),
    IDLE_COMPACTION_STATUS_TEMPLATE.format(idle_seconds=3600, tokens=120000),
    COMPRESSION_RETRY_TOO_LARGE_STATUS_TEMPLATE.format(tokens=250000, attempt=1, cap=3),
    COMPRESSION_RETRY_MESSAGES_STATUS_TEMPLATE.format(before=30, after=12),
    COMPRESSION_RETRY_TOKENS_STATUS_TEMPLATE.format(before=250000, after=120000),
    COMPRESSION_RETRY_CONTEXT_REDUCED_STATUS_TEMPLATE.format(
        new_ctx=120000, old_ctx=250000
    ),
)


# --- _emit_compression_attempt_telemetry ---
def _emit_compression_attempt_telemetry(
    agent: Any,
    *,
    started_at: float,
    commit_status: str,
    split_status: str,
    failure_class: str | None = None,
) -> None:
    """Emit one content-free JSON log line for a compression attempt."""
    try:
        telemetry = getattr(agent.context_compressor, "_last_compression_telemetry", None)
        if not isinstance(telemetry, dict):
            telemetry = {}
        payload = dict(telemetry)
        payload.setdefault("event", "compression_attempt")
        payload.setdefault("attempt_id", getattr(agent, "_compression_attempt_id", "") or uuid.uuid4().hex)
        payload.setdefault("session_id", getattr(agent, "session_id", "") or "")
        payload["total_duration_ms"] = int((time.monotonic() - started_at) * 1000)
        payload["commit_status"] = commit_status
        payload["split_status"] = split_status
        if failure_class:
            payload["failure_class"] = failure_class
        payload.setdefault("chunking", False)
        payload.setdefault("chunk_count", 0)
        payload["fallback_used"] = bool(
            payload.get("fallback_used")
            or getattr(agent.context_compressor, "_last_summary_fallback_used", False)
            or getattr(agent.context_compressor, "_last_aux_model_failure_model", None)
        )
        logger.info(
            "context compression attempt telemetry: %s",
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
        )
    except Exception as exc:
        logger.debug("failed to emit compression attempt telemetry: %s", exc)


# --- compression_skipped_due_to_lock ---
def compression_skipped_due_to_lock(agent: Any) -> bool:
    """Type-pinned read of the #69870 lock-skip signal.

    ``agent._compression_skipped_due_to_lock`` is set by ``compress_context``
    when a compression pass no-ops because another path holds the per-session
    compression lock (holder string when the holder was confirmed, ``True``
    otherwise) and cleared to ``None`` at the entry of every call.

    The read MUST be type-pinned (``is True or isinstance(x, str)``), never
    bare truthiness: MagicMock test-double agents auto-create truthy
    attributes, and a bare ``if getattr(agent, ...)`` would hijack every
    mocked agent in sibling suites into the lock-skip branch (the
    #69870 × #69840 type-ahead incident).
    """
    _sig = getattr(agent, "_compression_skipped_due_to_lock", None)
    return _sig is True or isinstance(_sig, str)


# --- _queue_context_engine_compression_notification ---
def _queue_context_engine_compression_notification(
    agent: Any,
    *,
    new_session_id: str,
    old_session_id: str,
) -> None:
    """Stage exactly one existing hook call for an outer host transaction."""
    if callable(getattr(agent, _PENDING_CONTEXT_ENGINE_NOTIFICATION, None)):
        raise RuntimeError("a compression notification is already pending")

    def _notify() -> bool:
        return _notify_context_engine_compression_complete(
            agent,
            new_session_id=new_session_id,
            old_session_id=old_session_id,
        )

    setattr(agent, _PENDING_CONTEXT_ENGINE_NOTIFICATION, _notify)


# --- CONTEXT_OVERFLOW_BLOCKED_WARNING_TEMPLATE ---
CONTEXT_OVERFLOW_BLOCKED_WARNING_TEMPLATE = (
    "⚠ Context is over the compression threshold "
    "(~{tokens:,} tokens >= {threshold:,}) "
    "but compression is currently blocked ({reason}). "
    "The model may stop responding. Run /new to start a fresh "
    "session or /compress to retry immediately."
)


# --- _refresh_persisted_compression_guards ---
def _refresh_persisted_compression_guards(compressor: Any) -> None:
    """Refresh durable automatic-compression guards on a built-in compressor."""
    method_calls = (
        ("get_active_compression_failure_cooldown", {"refresh": True}),
        ("_load_fallback_compression_streak", {}),
        ("_load_ineffective_compression_count", {}),
    )
    for method_name, kwargs in method_calls:
        method = getattr(type(compressor), method_name, None)
        if not callable(method):
            continue
        try:
            method(compressor, **kwargs)
        except Exception as exc:
            logger.debug("compression guard refresh failed (%s): %s", method_name, exc)


# --- _supported_compression_kwargs ---
def _supported_compression_kwargs(
    compress_fn: Any,
    *,
    current_tokens: Optional[int],
    focus_topic: Optional[str],
    force: bool,
    memory_context: str,
) -> dict:
    """Return only compression kwargs accepted by an engine callable.

    Context-engine plugins can outlive additions to the optional host contract.
    Inspecting the callable before invoking it keeps those older signatures
    compatible without catching an internal ``TypeError`` and executing a
    stateful compressor twice.
    """
    candidates = {
        "current_tokens": current_tokens,
        "focus_topic": focus_topic,
        "force": force,
    }
    if memory_context:
        candidates["memory_context"] = memory_context
    try:
        parameters = inspect.signature(compress_fn).parameters
    except (TypeError, ValueError):
        # ``current_tokens`` has been part of the ContextEngine ABC since its
        # introduction. Keep the oldest documented call shape when a C-backed
        # or otherwise opaque callable has no inspectable signature.
        return {"current_tokens": current_tokens}

    accepts_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    if accepts_kwargs:
        return candidates
    return {name: value for name, value in candidates.items() if name in parameters}

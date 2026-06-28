"""DispatchingJournal: a Journal that also emits StreamEvents on append.

The workflow runtime writes events to the Journal for durability. The
visibility layer needs those same events to flow into the gateway's
GatewayEventDispatcher so platform adapters (Telegram, Discord, TUI,
desktop) can render live progress. DispatchingJournal bridges the two:

  1. Persists the event to the append-only JSONL file (durability).
  2. Translates the event to a StreamEvent via EventTranslator.
  3. Calls dispatcher.dispatch(stream_event) for live visibility.

The dispatcher reference is injected at construction. If the dispatcher
is None, the journal falls back to its plain-mode behavior (durability
only) — useful in tests and in CLI invocations that don't need live
streaming.

Failure isolation: a dispatcher error MUST NOT prevent the event from
being persisted. The dispatch call is wrapped in try/except; persistence
is the trust boundary.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from .journal import Journal
from .visibility import EventTranslator


_log = logging.getLogger("hermes_workflow.dispatching_journal")


class DispatchingJournal:
    """Wraps a Journal and a translator + dispatcher.

    Drop-in replacement for Journal: same .append(event) signature. Adds
    side-effect dispatch to the gateway's event stream.

    Construct with:
        journal = Journal(run_id, journal_root)
        dj = DispatchingJournal(journal, translator, dispatcher_fn)
    """

    def __init__(
        self,
        inner: Journal,
        translator: EventTranslator,
        dispatcher_fn: Callable[[Any], None] | None,
    ) -> None:
        self._inner = inner
        self._translator = translator
        self._dispatch = dispatcher_fn

    @property
    def run_id(self) -> str:
        return self._inner.run_id

    @property
    def events(self) -> list[dict[str, Any]]:
        return self._inner.events

    @property
    def path(self) -> Path:
        return self._inner.path

    def append(self, event: dict[str, Any]) -> None:
        """Persist first, then dispatch. If dispatch raises, the event is
        still on disk and the user can replay it later.
        """
        # Persist (this can raise on disk-full; let it propagate).
        self._inner.append(event)
        # Dispatch (best-effort).
        if self._dispatch is not None:
            try:
                stream_event = self._translator.translate(event)
                if stream_event is not None:
                    self._dispatch(stream_event)
            except Exception:
                # Stream dispatch is best-effort. Persistence is canonical.
                _log.debug(
                    "dispatching_journal: dispatch error for run %s",
                    self.run_id, exc_info=True,
                )

    def close(self) -> None:
        self._inner.close()

    def __enter__(self) -> "DispatchingJournal":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

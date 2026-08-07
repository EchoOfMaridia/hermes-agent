"""Append-only journal for workflow runs.

Why append-only: we never modify past events. The journal is the canonical
record of what happened in a run; replays reconstruct state from it. There
is no SQLite, no daemon, no subprocess mesh. Just a file with one JSON
object per line.

Why fsync per write: a crash mid-write either commits the line fully or
leaves it absent. There is no "partial JSON" state that could mislead a
replay. The cost is modest: one fsync per event, batched for high-volume
runs is a future optimization.

Why no truncation: events are immutable once written. The only operations
on a journal are append (during a run) and read (for inspection or replay).
There is no delete, no edit, no compression. Old runs accumulate as files
under ~/.hermes/workflows/<run-id>.journal; cleanup is the user's call.

Why JSONL: human-readable with `cat`, parseable line-by-line in O(1) memory,
no embedded binary, easy to grep, easy to diff.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


class Journal:
    """Append-only event log for one workflow run.

    One Journal per run. One JSON object per line. Each line carries a
    `kind` discriminator and a `ts` (Unix timestamp, float seconds).

    Usage:
        j = Journal(run_id="r_abc123", journal_root=Path("~/.hermes/workflows"))
        j.append({"kind": "run_started", "workflow": "my_wf"})
        ...
        j.close()

    Replay:
        j = Journal.replay("r_abc123", Path("~/.hermes/workflows"))
        for event in j.events:
            print(event)
    """

    # Reserved event kinds. Workflow scripts and the runtime use these.
    # New kinds are allowed; old journals replay cleanly when new kinds are
    # added (the schema is intentionally open).
    KIND_RUN_STARTED = "run_started"
    KIND_RUN_COMPLETED = "run_completed"
    KIND_RUN_FAILED = "run_failed"
    KIND_RUN_HALTED = "run_halted"
    KIND_RUN_CANCELLED = "run_cancelled"
    KIND_STEP_STARTED = "step_started"
    KIND_STEP_COMPLETED = "step_completed"
    KIND_STEP_FAILED = "step_failed"
    KIND_VERIFIER_RETURNED = "verifier_returned"
    KIND_AGENT_CALL = "agent_call"
    KIND_AGENT_RESPONSE = "agent_response"

    def __init__(
        self,
        run_id: str,
        journal_root: Path,
        *,
        _open_for_append: bool = True,
    ) -> None:
        self.run_id = run_id
        self.journal_root = Path(journal_root)
        self.path = self.journal_root / f"{run_id}.journal"
        self.events: list[dict[str, Any]] = []
        self._fp: Any = None
        if _open_for_append:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # buffering=1 (line-buffered) so each write is flushed to the
            # underlying file object on newline; we still call fsync() to
            # ensure durability past process exit.
            self._fp = open(self.path, "a", buffering=1, encoding="utf-8")

    # -- write path --------------------------------------------------------

    def append(self, event: dict[str, Any]) -> None:
        """Append an event. Sets `ts` if not present. fsync per write.

        Raises:
            RuntimeError: if the journal was opened via replay() (read-only).
        """
        if self._fp is None:
            raise RuntimeError(
                f"Journal {self.run_id} is read-only (opened via replay)"
            )
        if "ts" not in event:
            event["ts"] = time.time()
        line = json.dumps(event, separators=(",", ":"), default=str)
        self._fp.write(line + "\n")
        self._fp.flush()
        os.fsync(self._fp.fileno())
        self.events.append(event)

    def close(self) -> None:
        if self._fp is not None:
            self._fp.flush()
            os.fsync(self._fp.fileno())
            self._fp.close()
            self._fp = None

    # -- context manager ----------------------------------------------------

    def __enter__(self) -> "Journal":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    # -- read path / replay -------------------------------------------------

    @classmethod
    def replay(cls, run_id: str, journal_root: Path) -> "Journal":
        """Reconstruct a Journal from disk. Read-only after this call.

        Returns a Journal whose .events list contains every event written
        to the underlying file. The returned object cannot be appended to.
        """
        path = Path(journal_root) / f"{run_id}.journal"
        j = cls.__new__(cls)
        j.run_id = run_id
        j.journal_root = Path(journal_root)
        j.path = path
        j.events = []
        j._fp = None
        if not path.exists():
            # Replay of a missing journal: return empty events, do not raise.
            # Inspection tools want to know "what happened?" and the answer
            # for a missing journal is "nothing recorded".
            return j
        with open(path, "r", encoding="utf-8") as fp:
            for raw_line in fp:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    j.events.append(json.loads(line))
                except json.JSONDecodeError:
                    # Tolerate partial writes from a kill-9 crash that landed
                    # mid-line. A full-line fsync guarantees atomicity of
                    # complete lines, but a half-written line is possible
                    # if the process was killed between the write() and
                    # fsync() calls. Skip such lines.
                    continue
        return j

    # -- inspection helpers -------------------------------------------------

    def steps_completed(self) -> list[str]:
        return [
            e["step"]
            for e in self.events
            if e.get("kind") == self.KIND_STEP_COMPLETED
        ]

    def steps_failed(self) -> dict[str, str]:
        return {
            e["step"]: e.get("error", "")
            for e in self.events
            if e.get("kind") == self.KIND_STEP_FAILED
        }

    def verifier_verdicts(self) -> list[dict[str, Any]]:
        return [
            e for e in self.events
            if e.get("kind") == self.KIND_VERIFIER_RETURNED
        ]

    def agent_calls(self) -> list[dict[str, Any]]:
        return [
            e for e in self.events
            if e.get("kind") == self.KIND_AGENT_CALL
        ]

    def final_outcome(self) -> str | None:
        """Returns the last terminal kind (completed/failed/halted/cancelled)
        or None if the run never reached a terminal state."""
        terminal_kinds = (
            self.KIND_RUN_COMPLETED,
            self.KIND_RUN_FAILED,
            self.KIND_RUN_HALTED,
            self.KIND_RUN_CANCELLED,
        )
        for event in reversed(self.events):
            if event.get("kind") in terminal_kinds:
                return event["kind"]
        return None

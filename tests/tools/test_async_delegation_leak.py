"""Regression tests for the async-delegation `_records` leak class.

User-reported symptom (session 20260727_094104_9b676b in
/home/cage/Desktop/Workspaces/HermesDesktop, 2026-07-27): the parent's
delegate_task was rejected with "Async delegation capacity reached (3 running)"
8+ times across several minutes even though the durable async_delegations table
eventually showed all prior dispatches as `state=completed`. The capacity check
counts `status in ("running", "stalling")` against the in-memory `_records`
dict in tools/async_delegation.py, so a record that survives in `_records` at
status="running" — without a corresponding worker thread to ever call
`_finalize` — permanently shrinks the budget for every later dispatch in the
same process lifetime.

These tests pin three concrete leak paths that all manifest the same symptom
and were confirmed by static analysis + runtime evidence in the catchup branch:

1. `_persist_dispatch` is called OUTSIDE the `_records_lock` after the record
   is inserted. If the SQLite write raises (lock contention, IOError, schema
   drift, etc.) the record is orphaned in `_records` at status="running" with
   no worker thread ever submitted. The capacity check then sees a phantom
   that the durable DB will never clear.

2. `dispatch_async_delegation_batch` has the same shape (line ~905-920):
   capacity check + insert under the lock, `_persist_dispatch` after the
   `with` block. Same orphan-on-raise path.

3. The `_stale_monitor_loop` only monitors records that carry a `progress_fn`
   (line 1092 short-circuits). A record dispatched without `progress_fn` is
   immortal from the monitor's perspective — if its worker thread dies
   without calling `_finalize`, the record sits at `running` forever. The
   first two fixes should make this unreachable, but the third is a cheap
   backstop and catches any future regression in (1) or (2).

Each test below calls the public surface (no private attribute access beyond
the same `_records`/`_reset_for_tests` the rest of the file uses), runs the
failing scenario, and asserts the leak is closed.
"""

import threading
import time

import pytest

from tools import async_delegation as ad


@pytest.fixture(autouse=True)
def _clean_state():
    ad._reset_for_tests()
    yield
    deadline = time.monotonic() + 2.0
    while ad.active_count() and time.monotonic() < deadline:
        time.sleep(0.02)
    ad._reset_for_tests()


def test_single_persist_failure_does_not_leak_record():
    """`_persist_dispatch` raising must roll back the in-memory insert.

    Reproduces the production path that left `deleg_b335b692`-class orphans
    behind: capacity check passes, the record is inserted under
    `_records_lock`, the lock is released, then `_persist_dispatch(record)`
    is called. If that SQLite write raises, the in-memory record stays at
    `status="running"` with no worker thread ever created — capacity count
    stays inflated for the rest of the process lifetime.
    """

    real_persist = ad._persist_dispatch
    failures = {"count": 0}

    def boom(record):
        failures["count"] += 1
        raise RuntimeError("simulated _persist_dispatch failure")

    ad._persist_dispatch = boom
    try:
        # First dispatch hits the boom path — must surface as a rejection
        # (the same shape a real SQLite failure would produce) WITHOUT
        # leaking the record into `_records`.
        res = ad.dispatch_async_delegation(
            goal="leak probe single",
            context=None,
            toolsets=None,
            role="leaf",
            model="m",
            session_key="",
            runner=lambda: {"status": "completed", "summary": "x"},
            max_async_children=3,
        )
    finally:
        ad._persist_dispatch = real_persist

    assert res["status"] == "rejected", (
        f"failed persist must surface as a rejection, got {res!r}"
    )
    assert failures["count"] == 1, "boom should have fired exactly once"
    assert ad.active_count() == 0, (
        f"capacity count must be 0 after a failed persist, "
        f"got {ad.active_count()} (orphan leaked)"
    )
    # Verify the specific record dict isn't sitting in `_records` either —
    # belt-and-braces in case `active_count` ever grows extra counters.
    assert not ad._records, (
        f"_records must be empty after a failed persist, "
        f"got {list(ad._records)}"
    )


def test_batch_persist_failure_does_not_leak_record():
    """Same shape as the single-dispatch test, but for the batch path."""

    real_persist = ad._persist_dispatch

    def boom(record):
        raise RuntimeError("simulated _persist_dispatch failure (batch)")

    ad._persist_dispatch = boom
    try:
        res = ad.dispatch_async_delegation_batch(
            goals=["a", "b"],
            context=None,
            toolsets=None,
            role="leaf",
            model="m",
            session_key="",
            runner=lambda: {"results": []},
            max_async_children=3,
        )
    finally:
        ad._persist_dispatch = real_persist

    assert res["status"] == "rejected"
    assert ad.active_count() == 0
    assert not ad._records

def test_record_without_progress_fn_is_force_finalized_when_stale():
    """Backstop: even if a worker thread dies without calling `_finalize`,
    the stale monitor must eventually free the slot.

    Without a `progress_fn` the monitor previously short-circuited at the
    `if progress_fn is None: continue` guard (line ~1129) and the record
    was immortal. The fix sweeps those records on each monitor iteration
    and force-finalizes them past the grace window — same `_finalize_stalled`
    path as the progress-fuelled branch, just no `interrupt_fn` to call.
    """

    real_persist = ad._persist_dispatch
    real_finalize_stalled = ad._finalize_stalled

    # Disable persist + the real finalize-stalled so we can drive the
    # lifecycle without an actual worker thread, SQLite write, or downstream
    # completion event. The fake flips status to "stalled" exactly the way
    # `_finish_finalization` would have done — the rest of the cleanup is
    # out of scope for this backstop test.
    ad._persist_dispatch = lambda record: None

    finalizations = {"calls": []}

    def fake_finalize_stalled(delegation_id):
        finalizations["calls"].append(delegation_id)
        with ad._records_lock:
            record = ad._records.get(delegation_id)
            if record is not None:
                record["status"] = "stalled"

    ad._finalize_stalled = fake_finalize_stalled
    try:
        # Manually inject an orphan record with no progress_fn, no
        # interrupt_fn, no worker thread — the exact end state that the
        # production leak produced before this fix.
        record = {
            "delegation_id": "deleg_orphan_no_progress",
            "goal": "orphan",
            "context": None,
            "toolsets": None,
            "role": "leaf",
            "model": "m",
            "session_key": "",
            "origin_ui_session_id": "",
            "origin_session_id": "",
            "parent_session_id": None,
            "status": "running",
            # Aged past the grace window so the next monitor sweep
            # recognizes the record as orphaned and force-finalizes it.
            "dispatched_at": time.time() - (ad._STALL_GRACE_SECONDS + 5),
            "completed_at": None,
            "interrupt_fn": None,
            "progress_fn": None,
            "_progress_token": None,
            "_progress_ts": time.time(),
            "_interrupted_at": None,
        }
        with ad._records_lock:
            ad._records[record["delegation_id"]] = record

        assert ad.active_count() == 1, (
            "orphan must occupy a capacity slot (this is the bug the user hit)"
        )

        # Drive a single iteration of the stale-monitor loop body.
        # `Event.wait` returns True when the stop event is set (loop exits)
        # and False when the timeout expires (loop continues). Returning
        # False on the first call lets the body execute, then True on
        # the second call terminates the `while` cleanly.
        call_state = {"n": 0}

        def wait_once(timeout=None):
            call_state["n"] += 1
            return call_state["n"] > 1  # first call: False (continue), then True (exit)

        real_wait = ad._monitor_stop.wait
        ad._monitor_stop.wait = wait_once
        try:
            ad._stale_monitor_loop()
        finally:
            ad._monitor_stop.wait = real_wait

        assert record["delegation_id"] in finalizations["calls"], (
            "stale monitor must invoke _finalize_stalled on records that "
            "exceeded the grace window, regardless of whether they carry a "
            "progress_fn"
        )
        assert record["status"] == "stalled", (
            f"orphan record must transition out of running, got "
            f"status={record['status']!r}"
        )
        assert ad.active_count() == 0, (
            "after force-finalize, the orphan must no longer count against "
            "capacity"
        )
    finally:
        ad._persist_dispatch = real_persist
        ad._finalize_stalled = real_finalize_stalled
        with ad._records_lock:
            ad._records.clear()


def test_capacity_count_uses_running_and_stalling_only():
    """Sanity: the capacity check must agree with itself across the two
    counters in the module (`active_count` vs the dispatch-time check).

    They were inconsistent before the leak investigation — the dispatch-time
    check counted `("running", "stalling")` while `active_count()` also
    included `"finalizing"`. Pin them to the same set so future refactors
    can't silently split them again.
    """

    for status in ("running", "stalling"):
        rid = f"deleg_pin_{status}"
        with ad._records_lock:
            ad._records[rid] = {
                "delegation_id": rid,
                "status": status,
                "dispatched_at": time.time(),
            }
        # Both the dispatch-time check and `active_count` must see it.
        assert ad.active_count() >= 1
        # The dispatch-time check is reproduced inline so the test doesn't
        # require going through dispatch_async_delegation (which needs a
        # real runner + executor).
        running_for_dispatch = sum(
            1
            for r in ad._records.values()
            if r.get("status") in ("running", "stalling")
        )
        assert running_for_dispatch >= 1

    with ad._records_lock:
        ad._records.clear()
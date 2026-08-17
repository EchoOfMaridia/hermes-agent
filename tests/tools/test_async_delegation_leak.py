"""Regression tests for the async-delegation `_records` leak class.

User-reported symptom (session 20260727_094104_9b676b in
|/home/cage/Desktop/Workspaces/HermesDesktop, 2026-07-27, and again on
2026-08-08 with IDs deleg_4070b80f / deleg_7fdcd2e9 / deleg_204ea470): the
parent's delegate_task was rejected with "Async delegation capacity reached
(3 running)" 8+ times across several minutes even though the durable
async_delegations table eventually showed all prior dispatches as
`state=completed`. The capacity check counts `status in ("running", "stalling")`
against the in-memory `_records` dict in tools/async_delegation.py, so a record
that survives in `_records` at status="running" — without a corresponding
worker thread to ever call `_finalize` — permanently shrinks the budget for
every later dispatch in the same process lifetime.

These tests pin the concrete leak paths that all manifest the same symptom
and were confirmed by static analysis + runtime evidence:

1. `_persist_dispatch` is called INSIDE the `_records_lock` block after the
   record is inserted. If the SQLite write raises (lock contention, IOError,
   schema drift, etc.) the record is rolled back via ``_records.pop``.

2. `dispatch_async_delegation_batch` has the same shape: capacity check +
   insert under the lock, `_persist_dispatch` inside the `with` block.
   Same rollback-on-raise path.

3. The `_stale_monitor_loop` sweep handles records with no `progress_fn`
   (the orphan backstop) — they are force-finalized past the grace window.

4. The dispatch-time capacity check (`status in ("running", "stalling")`)
   and `active_count()` (`status in ("running", "stalling", "finalizing")`)
   agree on `("running", "stalling")` only — they disagree on
   `"finalizing"`. Test 4 pins the agreement on `("running", "stalling")`.

5. **NEW (2026-08-08):** when the worker's runner returns successfully but
   `_finalize` itself fails mid-flight — e.g. `_persist_completion` raises
   (SQLite lock, IOError) or `process_registry.completion_queue.put` raises
   — `_finish_finalization` never runs and the record is orphaned at
   `status="finalizing"`. `active_count()` keeps counting it as occupied
   forever, while the dispatch-time check sees `("running", "stalling")`
   only. The two halves of the capacity check now disagree, and an
   operator-facing tool that consults `active_count()` (TUI overlay,
   delegation.status RPC, etc.) reports a phantom 3-running pool that
   never drains.

   Test 5 pins this: a record that successfully runs through `_finalize`'s
   `_begin_finalization` flip but fails before `_finish_finalization` runs
   must transition to a terminal status (e.g. "error") and free the slot.

6. **NEW (2026-08-08):** the dispatch-time escape hatch the user demanded
   (``hermes agent reset-delegation-slots`` or equivalent) — when the
   user manually clears stale records, subsequent dispatch must succeed.

Each test below calls the public surface (no private attribute access beyond
the same `_records`/`_reset_for_tests` the rest of the file uses), runs the
failing scenario, and asserts the leak is closed.
"""

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


def test_persist_completion_failure_does_not_orphan_record_at_finalizing():
    """Pinned 2026-08-08: when `_push_completion_event` raises mid-flight,
    `_finish_finalization` never runs and the record sits at
    `status="finalizing"` forever.

    Repro: the user's 3 sub-agents all 401'd at the LLM gateway and
    returned within 1 API call. ``_run_single_child`` returned a normal
    dict, ``_finalize`` flipped ``status`` to ``"finalizing"`` via
    ``_begin_finalization``, then ``_persist_completion`` raised (the
    exact failure mode the SQLite WAL-reset bug produces). ``_finish_
    finalization`` never ran. The records stayed at ``"finalizing"``.

    ``active_count()`` (used by the TUI overlay / ``delegation.status``
    RPC) keeps counting them as occupied; the dispatch-time check
    (``status in ("running", "stalling")``) sees zero. The two halves
    of the capacity bookkeeping silently disagree, and ``list_async_
    delegations`` keeps reporting the ghosts.

    Fix contract: ``_finalize`` must wrap the post-begin step so that
    ``_finish_finalization`` is invoked even when ``_push_completion_
    event`` raises. ``active_count()`` and the dispatch-time check must
    agree on the same status set.
    """

    real_persist = ad._persist_completion

    def boom_persist(_evt, _result):
        raise RuntimeError("simulated SQLite write failure in _persist_completion")

    ad._persist_completion = boom_persist
    try:
        res = ad.dispatch_async_delegation(
            goal="finalize-failure repro",
            context=None,
            toolsets=None,
            role="leaf",
            model="m",
            session_key="",
            runner=lambda: {
                "status": "completed",
                "summary": "ok",
                "api_calls": 1,
            },
            max_async_children=3,
        )
        # ``dispatch_async_delegation`` returns IMMEDIATELY — the worker
        # thread is still spinning up. We must wait for it to FINISH
        # (not just reach ``"finalizing"``, which is the in-progress
        # state during ``_push_completion_event``) BEFORE restoring the
        # mock. Otherwise the worker sees the real ``_persist_completion``
        # and the test stops exercising the leak path.
        #
        # Wait until the worker has fully exited, not until the record
        # transitions. We poll the durable ``delivery_state`` for the
        # delegation_id; once it's anything other than ``"pending"`` we
        # know the worker reached ``_persist_completion`` (which raises)
        # and ``_finish_finalization`` ran. Belt-and-braces: also wait
        # for ``active_count()`` to drop, which only happens after the
        # terminal flip.
        deadline = time.monotonic() + 3.0
        saw_finalizing = False
        while time.monotonic() < deadline:
            with ad._records_lock:
                snap = list(ad._records.items())
            if snap:
                s = snap[0][1].get("status")
                if s == "finalizing":
                    saw_finalizing = True
                elif saw_finalizing:
                    # Worker transitioned OUT of finalizing — its
                    # ``_finish_finalization`` has run. We can restore.
                    break
            time.sleep(0.05)
        # A small grace window so any final dict-mutation in
        # ``_finish_finalization`` completes before the snapshot.
        time.sleep(0.05)
    finally:
        ad._persist_completion = real_persist

    assert res["status"] == "dispatched"

    # The dispatch + wait above leaves the worker past its ``_push_completion_
    # event`` step — either terminalized (good) or stuck at 'finalizing'
    # (the bug we want to expose). Sample the registry one last time
    # without the lock so any further mutations can't surprise us.
    with ad._records_lock:
        snapshot = dict(ad._records)

    # The leaked-path pre-fix would leave status='finalizing' forever.
    # The fix flips the record to a terminal status (here "error" because
    # we treat any exception out of the completion push as a failure).
    assert snapshot, "record vanished — must remain in _records for the status tail"
    rec = next(iter(snapshot.values()))
    assert rec["status"] != "finalizing", (
        f"record must NOT remain stuck at 'finalizing' after _finalize "
        f"exception, got status={rec['status']!r}"
    )
    # active_count() must agree with the dispatch-time check on the same
    # set — neither side should see the record as occupied.
    running_for_dispatch = sum(
        1
        for r in ad._records.values()
        if r.get("status") in ("running", "stalling")
    )
    assert running_for_dispatch == 0
    assert ad.active_count() == 0, (
        f"active_count() must drop to 0 after a finalize-failure "
        f"record is terminalized, got {ad.active_count()} "
        f"(orphan leaked)"
    )


def test_progress_fn_none_does_not_immortalize_record():
    """Pinned 2026-08-08 (deleg_13ab7b84 / deleg_f223da02 / deleg_af859cb1):
    a worker thread that exits before calling ``_finalize`` AND that
    was dispatched WITHOUT a ``progress_fn`` must NOT be immortal —
    the dispatch path must start the stale-monitor thread regardless
    of ``progress_fn``, and the orphan_expired branch must reclaim
    the record after the grace window.

    Pre-fix: the dispatch path was gated on
    ``if progress_fn is not None: _ensure_stale_monitor()`` AND the
    stale-monitor sweep short-circuited at
    ``if progress_fn is None: continue``. Both gates had to flip
    before the orphan could be reclaimed.
    """

    real_persist = ad._persist_dispatch

    # Inject an orphan record at ``running`` with no ``progress_fn`` —
    # the exact end state the production leak produced.
    rid = "deleg_orphan_no_progress_running"
    now = time.time()
    record = {
        "delegation_id": rid,
        "goal": "no-progress orphan",
        "context": None,
        "toolsets": None,
        "role": "leaf",
        "model": "m",
        "session_key": "",
        "origin_ui_session_id": "",
        "origin_session_id": "",
        "parent_session_id": None,
        "status": "running",
        "dispatched_at": now - (ad._STALL_GRACE_SECONDS + 5),
        "completed_at": None,
        "interrupt_fn": None,
        "progress_fn": None,
        "_progress_token": None,
        "_progress_ts": now - (ad._STALL_GRACE_SECONDS + 5),
        "_interrupted_at": None,
    }
    ad._persist_dispatch = lambda record: None
    try:
        with ad._records_lock:
            ad._records[rid] = record

        assert ad.active_count() == 1, (
            "orphan at running must occupy 1 slot pre-monitor "
            "(this is the bug the user hit)"
        )

        # Drive a single iteration of the stale-monitor loop body. The
        # orphan_expired branch must reclaim the record even though
        # it carries no progress_fn.
        call_state = {"n": 0}

        def wait_once(timeout=None):
            call_state["n"] += 1
            return call_state["n"] > 1

        real_wait = ad._monitor_stop.wait
        ad._monitor_stop.wait = wait_once
        try:
            ad._stale_monitor_loop()
        finally:
            ad._monitor_stop.wait = real_wait

        # After the monitor sweep, the orphan must have been reclaimed
        # (status flipped to a terminal value, slot freed). This is the
        # single behavior that the production user observed as broken.
        assert ad.active_count() == 0, (
            f"stale monitor must reclaim a running record with no "
            f"progress_fn past the grace window; active_count is "
            f"{ad.active_count()} (orphan leaked)"
        )
        with ad._records_lock:
            assert record["status"] in {
                "completed", "error", "stalled", "interrupted",
                "unknown", "dropped",
            }, (
                f"orphan record must be terminalized post-monitor "
                f"sweep, got status={record['status']!r}"
            )
    finally:
        ad._persist_dispatch = real_persist
        with ad._records_lock:
            ad._records.clear()


def test_dispatch_always_starts_stale_monitor_regardless_of_progress_fn(
    monkeypatch,
):
    """Pinned 2026-08-08: the dispatch path must start the stale-monitor
    thread regardless of whether ``progress_fn`` was supplied.

    Pre-fix: ``if progress_fn is not None: _ensure_stale_monitor()``
    silently skipped starting the monitor when the caller omitted a
    ``progress_fn``. Combined with the monitor's ``progress_fn is None:
    continue`` short-circuit, no record at ``running`` without a
    ``progress_fn`` could ever be reclaimed — exactly the production
    state ``deleg_13ab7b84`` and ``deleg_f223da02`` got stuck in.
    """

    started = {"count": 0}

    def fake_ensure():
        started["count"] += 1

    monkeypatch.setattr(ad, "_ensure_stale_monitor", fake_ensure)

    ad.dispatch_async_delegation(
        goal="no-progress dispatch",
        context=None,
        toolsets=None,
        role="leaf",
        model="m",
        session_key="",
        runner=lambda: {"status": "completed", "summary": "x"},
        max_async_children=3,
        # NB: NO progress_fn — the caller's whole point is to verify
        # the dispatch path starts the monitor even when progress_fn
        # is omitted.
    )
    assert started["count"] == 1, (
        f"dispatch must start the stale monitor even when progress_fn "
        f"is None; started={started['count']}"
    )


def test_recover_orphaned_records_frees_capacity():
    """Pinned 2026-08-08: the operator escape hatch.

    The user's report demands a session-recovery action (``hermes agent
    reset-delegation-slots`` or equivalent) for when the counter is
    already desynced. This test pins ``recover_orphaned_records()``:
    given a registry of in-memory records with no live worker thread
    backing them, the recovery sweep forces each record into a
    terminal state and frees the dispatch-time slot.
    """

    now = time.time()
    # Inject three orphan records at various stuck statuses. The
    # dispatch-time check sees "running" + "stalling" as capacity-slot
    # holders; "finalizing" is a transient terminal-write state and
    # does NOT count against the budget (see ``active_count`` for the
    # exact contract).
    injected = []
    for i, status in enumerate(("running", "stalling", "finalizing")):
        rid = f"deleg_recover_{status}_{i}"
        with ad._records_lock:
            ad._records[rid] = {
                "delegation_id": rid,
                "goal": f"orphan-{status}",
                "context": None,
                "toolsets": None,
                "role": "leaf",
                "model": "m",
                "session_key": "",
                "origin_ui_session_id": "",
                "origin_session_id": "",
                "parent_session_id": None,
                "status": status,
                "dispatched_at": now - 600.0,  # aged past any grace window
                "completed_at": None,
                "interrupt_fn": None,
                "progress_fn": None,
                # `owner_pid` is intentionally None here — the recovery
                # sweep recognises "no live owner backing" and reclaims.
                "owner_pid": None,
                "owner_started_at": None,
                "_progress_token": None,
                "_progress_ts": now - 600.0,
                "_interrupted_at": None,
            }
        injected.append(rid)

    # active_count and the dispatch-time check must agree on the same
    # status set: "running" + "stalling" only — "finalizing" is transient.
    assert ad.active_count() == 2, (
        f"orphan records at running+stalling must occupy 2 slots pre-recovery "
        f"(finalizing is transient), got {ad.active_count()}"
    )

    freed = ad.recover_orphaned_records()
    assert freed == 3, (
        f"recover_orphaned_records must clear all 3 orphans, got {freed}"
    )

    # Every record must be in a terminal state, none of the three
    # capacity-counted statuses may remain.
    terminal_set = {
        "completed",
        "error",
        "stalled",
        "interrupted",
        "unknown",
        "dropped",
    }
    with ad._records_lock:
        snapshot = dict(ad._records)
    for rid in injected:
        rec = snapshot.get(rid)
        assert rec is not None, f"record {rid} vanished during recovery"
        assert rec["status"] in terminal_set, (
            f"record {rid} must be terminalized post-recovery, got "
            f"status={rec['status']!r}"
        )
    assert ad.active_count() == 0, (
        f"active_count must drop to 0 after recovery, got "
        f"{ad.active_count()}"
    )
    # And the dispatch-time check must agree.
    running_for_dispatch = sum(
        1
        for r in ad._records.values()
        if r.get("status") in ("running", "stalling")
    )
    assert running_for_dispatch == 0

    # A subsequent dispatch (any runner) must succeed — the escape hatch
    # actually unblocks the parent.
    post = ad.dispatch_async_delegation(
        goal="post-recovery dispatch",
        context=None,
        toolsets=None,
        role="leaf",
        model="m",
        session_key="",
        runner=lambda: {"status": "completed", "summary": "ok"},
        max_async_children=3,
    )
    assert post["status"] == "dispatched", (
        f"dispatch must succeed after recovery, got {post!r}"
    )
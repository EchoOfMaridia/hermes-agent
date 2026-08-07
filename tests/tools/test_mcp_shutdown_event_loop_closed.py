"""Regression test for 'RuntimeError: Event loop is closed' on Hermes shutdown.

Reported symptom (operator paste, exiting hermes-agent):
    Exception ignored in: <coroutine object MCPServerTask.run at 0x78e522911f80>
    Traceback (most recent call last):
      File "/home/cage/Desktop/Workspaces/HermesDesktop/tools/mcp_tool.py", line 2968, in run
        parked = await self._wait_for_reconnect_or_shutdown(
      File "/home/cage/Desktop/Workspaces/HermesDesktop/tools/mcp_tool.py", line 2182, in _wait_for_reconnect_or_shutdown
        t.cancel()
      File ".../asyncio/base_events.py", line 762, in call_soon
        self._check_closed()
    RuntimeError: Event loop is closed

Root cause: ``_wait_for_reconnect_or_shutdown`` (and its siblings
``_wait_for_lifecycle_event`` / ``_wait_for_lazy_reconnect``) create
``shutdown_task`` and ``reconnect_task`` via ``asyncio.ensure_future``
/ ``asyncio.create_task``, await ``asyncio.wait`` on them, and then in
the ``finally`` block call ``t.cancel()``. ``cancel()`` invokes
``call_soon`` on the running loop; if the loop has been closed between
when ``wait()`` returned and when ``finally`` runs, ``call_soon`` raises
``RuntimeError: Event loop is closed`` — which Python surfaces as
"Exception ignored in: <coroutine>" because the coroutine was being
finalized (Python's GC threw ``GeneratorExit`` into the suspended
coroutine, the GeneratorExit triggered the ``finally`` block, and the
``t.cancel()`` inside ``finally`` then raised).

This test pins the contract: the finally-cleanup ``t.cancel()`` paths
in the three MCP waiter helpers must NEVER raise
``RuntimeError: Event loop is closed`` regardless of the loop state
at cleanup time.

Captures the leak via pytest's ``unraisable_exceptions`` stash —
pytest installs its own ``sys.unraisablehook`` (see
``_pytest/unraisableexception.py``) that buffers events into a
deque; we drain the deque after ``gc.collect()``.
"""

import asyncio
import gc

import pytest


HELPERS = (
    "_wait_for_reconnect_or_shutdown",
    "_wait_for_lazy_reconnect",
    "_wait_for_lifecycle_event",
)


def _build_task_for_helper(name):
    from tools.mcp_tool import MCPServerTask

    task = MCPServerTask(f"srv-{name}")
    task._config = {}
    return task


def _drain_unraisable(request):
    """Pull every captured unraisable out of pytest's stash queue.

    Returns a list of dicts describing each unraisable event. Empty
    list if pytest isn't installed (we can't run without it anyway,
    but the test stays robust if invoked via ``python -m unittest``).
    """
    try:
        from _pytest.unraisableexception import unraisable_exceptions

        cfg = request.config
        deque_obj = cfg.stash.get(unraisable_exceptions, None)
    except Exception:
        return []
    if deque_obj is None:
        return []
    out = []
    while deque_obj:
        item = deque_obj.popleft()
        exc_value = getattr(item, "exc_value", None)
        out.append(
            {
                "exc_type": type(item).__name__,
                "exc_value": exc_value,
                "message": str(exc_value or item),
            }
        )
    return out


@pytest.mark.parametrize("helper_name", HELPERS)
def test_helper_finally_does_not_leak_when_loop_closed(
    monkeypatch, tmp_path, helper_name, request
):
    """The ``finally`` block of each waiter helper must not raise on a
    closed loop.

    Reproduces the operator's race deterministically:
      1. Drive the helper as a coroutine on a fresh event loop.
      2. Pump the loop until the coroutine suspends inside
         ``asyncio.wait``.
      3. Close the loop while the coroutine is suspended.
      4. Drop the future reference and ``gc.collect()`` — Python
         finalizes the abandoned coroutine, throwing ``GeneratorExit``
         into it, which triggers the ``finally`` block.
      5. The ``finally`` block calls ``t.cancel()`` on tasks whose loop
         is now closed — ``cancel()`` invokes ``call_soon`` which
         raises ``RuntimeError: Event loop is closed``.
      6. The contract: no such RuntimeError must escape from the helper.

    Captures the leak via pytest's ``unraisable_exceptions`` stash —
    pytest installs its own ``sys.unraisablehook`` that buffers events
    into a deque; we drain the deque after ``gc.collect()``.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    task = _build_task_for_helper(helper_name)
    helper = getattr(task, helper_name)

    # Drain any unraisable events left over from earlier tests so
    # we measure only what THIS test produces.
    _drain_unraisable(request)

    async def _runner():
        # Do NOT pre-set any events. The helper must genuinely park
        # inside asyncio.wait() so we can close the loop mid-await —
        # otherwise the finally block runs against a live loop and
        # the bug does not surface.
        if helper_name == "_wait_for_reconnect_or_shutdown":
            await helper(timeout=0.01)
        else:
            await helper()

    loop = asyncio.new_event_loop()
    future = loop.create_task(_runner())

    # Pump until the coroutine is suspended inside asyncio.wait(),
    # then close the loop on the second pump.
    pumped = 0
    while pumped < 200 and not future.done():
        loop.call_later(0.005, loop.stop)
        loop.run_forever()
        pumped += 1
        if pumped == 2:
            try:
                loop.close()
            except Exception:
                pass
            break

    # Drop the future reference and force GC. Python's finalizer
    # will throw GeneratorExit into the suspended coroutine,
    # triggering the finally block and (in the buggy version)
    # leaking RuntimeError.
    del future
    gc.collect()

    # Allow any deferred finalizers to fire.
    gc.collect()

    leaked = [
        e for e in _drain_unraisable(request)
        if "Event loop is closed" in e["message"]
    ]
    assert not leaked, (
        f"{helper_name} leaked "
        f"{len(leaked)} RuntimeError('Event loop is closed') event(s) "
        f"from its finally cleanup. "
        f"First leaked event: {leaked[0]['exc_value']!r}"
    )
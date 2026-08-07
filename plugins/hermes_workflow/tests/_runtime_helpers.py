"""Shared helpers for runtime tests.

The runtime schedules each Run as an asyncio.Task. Tests must interact with
both the synchronous submit() and the async task in the same event loop.
Pattern:
    async def _go():
        run_id = await rt.submit(...)
        await rt.get_run(run_id).task
        ...
    asyncio.run(_go())

This helper centralizes that pattern.
"""

from __future__ import annotations

import asyncio
import importlib.util
import textwrap
from pathlib import Path
from typing import Any, Awaitable, Callable


def write_workflow_module(tmp_path: Path, *lines: str, name: str = "wf.py") -> Path:
    """Write a workflow module file to disk. Returns the path."""
    p = tmp_path / name
    p.write_text(textwrap.dedent("\n".join(lines)).lstrip())
    return p


def import_workflow(module_path: Path) -> dict[str, Any]:
    """Import a workflow module by path. Returns module __dict__.

    Registers the module in sys.modules so runtime.submit() can find it via
    workflow_fn.__module__. Without this, the validator's graph check
    would be skipped.
    """
    import sys
    module_name = f"_dyn_wf_{module_path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module        # register for runtime.submit lookup
    spec.loader.exec_module(module)
    return vars(module)


async def submit_and_wait(
    rt, workflow_fn, inputs: dict, **kwargs,
) -> tuple[str, "Run"]:
    """Submit a workflow and wait for its task to complete (or fail).
    Returns (run_id, run). Run.task.result() carries the exception if any.
    Caller should catch via try/except around this helper if they want to
    assert on failure.
    """
    run_id = await rt.submit(workflow_fn, inputs, **kwargs)
    run = rt.get_run(run_id)
    try:
        await run.task
    except Exception:
        # Re-raise; test code handles asserting on run.state.
        raise
    return run_id, run


async def submit_and_drain(
    rt, workflow_fn, inputs: dict, **kwargs,
) -> tuple[str, "Run", BaseException | None]:
    """Like submit_and_wait but catches the task's exception and returns it.
    Lets tests assert on run.state without a try/except dance.
    """
    run_id = await rt.submit(workflow_fn, inputs, **kwargs)
    run = rt.get_run(run_id)
    err: BaseException | None = None
    try:
        await run.task
    except BaseException as e:
        err = e
    return run_id, run, err

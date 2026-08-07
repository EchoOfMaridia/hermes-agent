"""DSL primitives: @step, parallel/gather, @workflow.

These are the three building blocks a workflow author composes. Everything
else in the DSL — types, the runtime, the journal — exists to make these
three primitives correct.

Design notes:

- @step is a decorator that wraps the coroutine in a dispatch shim. The
  shim, when invoked from inside a workflow body (i.e., when the
  `_CURRENT_RUN` contextvar is set), looks up the current Run and calls
  Run.execute_step() — which acquires the semaphore, journals events,
  runs the verifier, etc. When invoked outside a workflow body (e.g.,
  in tests), the shim calls the underlying coroutine directly with no
  runtime machinery. The original coroutine is preserved on the wrapper as
  __wrapped__ for tests and inspection.

- parallel() and gather() are functions the workflow body calls to compose
  step invocations. They return awaitables that, when awaited, dispatch the
  steps to the runtime's semaphore and return their evidence. They always
  run inside an active workflow (the @workflow decorator installs the
  _CURRENT_RUN contextvar around the workflow body).

- @workflow is also a decorator. It marks the entrypoint coroutine of a
  workflow script. When the runtime calls run.execute(), it sets the
  _CURRENT_RUN contextvar before invoking the workflow body, so every
  step call inside the body is correctly routed.
"""

from __future__ import annotations

import contextvars
from typing import Any, Awaitable, Callable

from .types import Evidence, StepSpec, Verifier


# Context variable that tracks the currently-active Run. Set by the
# runtime before invoking the workflow body; read by the @step shim to
# know which Run to dispatch to. None when a step is called outside a
# workflow (e.g., from tests).
_CURRENT_RUN: contextvars.ContextVar[Any | None] = contextvars.ContextVar(
    "_CURRENT_RUN", default=None,
)


def get_current_run() -> Any | None:
    """Public accessor for the current Run contextvar. Useful for tests
    and for the runtime itself when wiring up the workflow body."""
    return _CURRENT_RUN.get()


def set_current_run(run: Any | None) -> contextvars.Token:
    """Set the current Run contextvar. Returns the token for resetting
    via _CURRENT_RUN.reset(token). Used by the runtime."""
    return _CURRENT_RUN.set(run)


# Sentinel for unset name (vs empty string which is a valid name).
_UNSET = object()


# ---------------------------------------------------------------------------
# @step
# ---------------------------------------------------------------------------

def step(
    name: str,
    *,
    depends_on: tuple[str, ...] = (),
    inputs_from: dict[str, str] | None = None,
    verifier: Verifier | None = None,
    max_retries: int = 0,
    retry_backoff_seconds: float = 1.0,
    timeout_seconds: float | None = None,
    output_schema: dict | None = None,
) -> Callable[[Callable[..., Awaitable[Evidence]]], Callable[..., Awaitable[Evidence]]]:
    """Decorator. Wraps a coroutine as a workflow step with a declared contract.

    The wrapper, when invoked, dispatches through the runtime if a Run is
    currently active; otherwise it executes the underlying coroutine
    directly (useful for tests and ad-hoc invocation).

    Args:
        name:                 Unique step name within the workflow. Required.
        depends_on:           Tuple of step names that must complete before
                              this step runs.
        inputs_from:          Dict mapping this step's parameter names to
                              the upstream step names that produce them.
                              The runtime injects the upstream Evidence as
                              a keyword argument.
        verifier:             Optional coroutine (Evidence, RunContext) ->
                              VerifierResult. Called after the step fn
                              returns and before the runtime marks the
                              step VERIFIED.
        max_retries:          Number of times to retry on failure. Default 0.
        retry_backoff_seconds: Sleep between retry attempts. Default 1.0.
                              Doubles each retry (exponential backoff).
        timeout_seconds:      Hard wall-clock timeout. None = no timeout.
        output_schema:        Optional JSON Schema describing the structured
                              output contract for this step. When set, the
                              runtime (a) forwards the schema to every
                              AgentBridge.invoke() call made inside the
                              step, (b) parses the LLM response back into
                              a Python object via
                              ``ctx.runtime.parse_structured``, and (c)
                              installs an auto-verifier that runs
                              ``jsonschema.validate`` against the parsed
                              payload. None (default) = unstructured text
                              response (v0.1.0 behaviour).

    Returns:
        Decorator that wraps the coroutine. The wrapper carries a
        __workflow_step__ attribute pointing to the StepSpec.
    """
    if not isinstance(name, str) or not name:
        raise ValueError(f"step() requires non-empty name, got {name!r}")
    if not isinstance(depends_on, tuple):
        raise TypeError(
            f"step({name!r}): depends_on must be a tuple, got "
            f"{type(depends_on).__name__}"
        )
    if inputs_from is not None and not isinstance(inputs_from, dict):
        raise TypeError(
            f"step({name!r}): inputs_from must be a dict or None, got "
            f"{type(inputs_from).__name__}"
        )
    if max_retries < 0:
        raise ValueError(
            f"step({name!r}): max_retries must be >= 0, got {max_retries}"
        )
    if retry_backoff_seconds < 0:
        raise ValueError(
            f"step({name!r}): retry_backoff_seconds must be >= 0, got "
            f"{retry_backoff_seconds}"
        )
    if timeout_seconds is not None and timeout_seconds <= 0:
        raise ValueError(
            f"step({name!r}): timeout_seconds must be > 0 or None, got "
            f"{timeout_seconds}"
        )
    if output_schema is not None and not isinstance(output_schema, dict):
        raise TypeError(
            f"step({name!r}): output_schema must be a dict or None, got "
            f"{type(output_schema).__name__}"
        )

    def decorator(
        fn: Callable[..., Awaitable[Evidence]],
    ) -> Callable[..., Awaitable[Evidence]]:
        spec = StepSpec(
            name=name,
            fn=fn,
            verifier=verifier,
            depends_on=depends_on,
            inputs_from=inputs_from if inputs_from is not None else {},
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff_seconds,
            timeout_seconds=timeout_seconds,
            output_schema=output_schema,
        )

        async def wrapper(*args: Any, **kwargs: Any) -> Evidence:
            run = _CURRENT_RUN.get()
            if run is None:
                # Outside a workflow body: just call the underlying fn.
                return await fn(*args, **kwargs)
            # Inside a workflow body: dispatch via the runtime.
            return await run.execute_step(spec, kwargs)

        # Attach metadata. Tests rely on __workflow_step__ being present
        # on the wrapper; the underlying coroutine is on __wrapped__.
        wrapper.__workflow_step__ = spec          # type: ignore[attr-defined]
        wrapper.__wrapped__ = fn                  # type: ignore[attr-defined]
        wrapper.__name__ = getattr(fn, "__name__", "step_wrapper")
        wrapper.__qualname__ = getattr(fn, "__qualname__", "step_wrapper")
        wrapper.__doc__ = getattr(fn, "__doc__", None)
        # Inherit __module__ from the wrapped function so the validator's
        # __module__ check identifies the wrapper as belonging to the
        # workflow script that defined the step.
        try:
            wrapper.__module__ = fn.__module__
        except (AttributeError, TypeError):
            pass
        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# parallel / gather — composition primitives
# ---------------------------------------------------------------------------

def parallel(
    *steps: Callable[..., Awaitable[Evidence]],
) -> Callable[..., Awaitable[tuple[Evidence, ...]]]:
    """Run independent steps concurrently. Returns a coroutine function.

    The returned coroutine function, when called with (ctx, **kwargs), returns
    a coroutine that dispatches each input step with those arguments and
    returns their Evidence in declaration order.

    Usage inside a @workflow body:

        @workflow(name="example")
        async def run(ctx: RunContext) -> dict:
            user_ev, order_ev, payment_ev = await parallel(
                fetch_user,
                fetch_orders,
                fetch_payments,
            )(ctx, user_id=42)
            return {"user": user_ev, "orders": order_ev, "payments": payment_ev}

    The runtime enforces the 16-concurrent cap via its semaphore. Calls to
    parallel() that would exceed the cap queue internally — they do NOT raise.
    The cap is on TOTAL steps spawned, which raises MaxTotalReached.
    """
    if not steps:
        raise ValueError("parallel() requires at least one step")

    async def dispatch(ctx: Any, **kwargs: Any) -> tuple[Evidence, ...]:
        coros = [step(ctx, **kwargs) for step in steps]
        return tuple(await asyncio.gather(*coros))

    return dispatch


class GatherHandle:
    """Returned by gather(). Calling it with (ctx, **kwargs) returns a coroutine
    that dispatches each named step and returns a dict keyed by step name."""

    def __init__(
        self,
        names: list[str],
        step_callables: list[Callable[..., Awaitable[Evidence]]],
    ) -> None:
        self._names = names
        self._steps = step_callables

    def __call__(
        self, ctx: Any, **kwargs: Any
    ) -> Awaitable[dict[str, Evidence]]:
        names = self._names
        steps = self._steps

        async def _gather() -> dict[str, Evidence]:
            coros = [step(ctx, **kwargs) for step in steps]
            results = await asyncio.gather(*coros)
            return dict(zip(names, results))

        return _gather()


def gather(
    **named_steps: Callable[..., Awaitable[Evidence]],
) -> GatherHandle:
    """Like parallel(), but returns a dict keyed by step name.

    Returns:
        A GatherHandle that, when called with (ctx, **kwargs), returns a
        coroutine that produces a dict of {name: Evidence}.

    Usage inside a @workflow body:

        @workflow(name="example")
        async def run(ctx: RunContext) -> dict:
            reviews = await gather(
                auth=review_file,
                db=review_file,
                api=review_file,
            )(ctx, paths=["auth.py", "db.py", "api.py"])
            # reviews is {"auth": Evidence(...), "db": Evidence(...),
            #              "api": Evidence(...)}
            return {"review_count": len(reviews)}
    """
    if not named_steps:
        raise ValueError("gather() requires at least one named step")

    names = list(named_steps.keys())
    step_callables = [named_steps[name] for name in names]
    return GatherHandle(names, step_callables)


# ---------------------------------------------------------------------------
# @workflow
# ---------------------------------------------------------------------------

def workflow(
    name: str,
    *,
    description: str = "",
    max_concurrent: int = 16,
    max_total: int = 1000,
) -> Callable[[Callable[..., Awaitable[dict]]], Any]:
    """Decorator. Marks the entrypoint coroutine of a workflow.

    The body of a @workflow coroutine composes @step calls using parallel()
    and gather(). When the body returns, the run is marked DONE.

    Args:
        name:           Unique workflow name. Used in CLI, slash command,
                        library entries, journal entries. Required.
        description:    Human-readable description. Shown by `hermes workflow
                        list`. Optional but recommended.
        max_concurrent: Per-run concurrent-step cap. Default 16 (matches
                        Dynamic Workflows cap so the mental model transfers).
                        The runtime enforces this via its semaphore; the
                        value here is informational for documentation only.
                        To change the cap, edit WorkflowRuntime defaults.
        max_total:      Per-run total-step cap. Default 1000. Informational
                        only; enforced by the runtime.

    Returns:
        Decorator that attaches workflow metadata to the coroutine via
        __workflow_meta__. The coroutine is returned unchanged.
    """
    if not isinstance(name, str) or not name:
        raise ValueError(f"workflow() requires non-empty name, got {name!r}")
    if max_concurrent < 1:
        raise ValueError(
            f"workflow({name!r}): max_concurrent must be >= 1, got "
            f"{max_concurrent}"
        )
    if max_total < 1:
        raise ValueError(
            f"workflow({name!r}): max_total must be >= 1, got {max_total}"
        )

    class _WorkflowMeta:
        """Metadata attached to the @workflow coroutine."""

        def __init__(self) -> None:
            self.name = name
            self.description = description
            self.max_concurrent = max_concurrent
            self.max_total = max_total

    meta = _WorkflowMeta()

    def decorator(fn: Callable[..., Awaitable[dict]]) -> Callable[..., Awaitable[dict]]:
        fn.__workflow_meta__ = meta        # type: ignore[attr-defined]
        return fn

    return decorator


# Late import to avoid circular issues at module import time.
import asyncio  # noqa: E402

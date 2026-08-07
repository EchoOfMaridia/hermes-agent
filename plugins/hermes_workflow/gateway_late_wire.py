"""Late-binding dispatcher wiring.

The runtime's StreamEvent dispatcher is set via ``runtime.set_dispatcher(callable)``.
At plugin-registration time, the gateway runner may not yet be active;
calling ``get_active_dispatcher()`` returns None in that case, so the
runtime ends up with no dispatcher and live streaming is silent.

This module solves that problem by registering a hook that re-checks
the gateway state on every opportunity (``on_session_start``,
``pre_gateway_dispatch``) and wires the dispatcher the moment a
gateway runner becomes active.

The runtime singleton is the same one the entrypoint constructed; we
hold a reference and re-wire on every check.

Late-binding is idempotent: if the dispatcher is already wired, the
hook is a no-op.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

_log = logging.getLogger("hermes_workflow.gateway_late_wire")


def build_late_wire_callback(
    runtime: Any,
    dispatcher_resolver: Callable[[], Any] | None = None,
) -> Callable:
    """Build a hook callback that late-wires the dispatcher.

    Args:
        runtime:             The WorkflowRuntime singleton.
        dispatcher_resolver: A callable returning the active dispatcher
                              (or None). Defaults to the standard
                              ``hermes_cli.gateway.get_active_dispatcher``.

    Returns:
        A function suitable for ``ctx.register_hook()``. The function
        accepts any kwargs (the hook is observer-only) and returns None.
    """
    if dispatcher_resolver is None:
        def _default_resolver() -> Any:
            try:
                from hermes_cli.gateway import get_active_dispatcher
                return get_active_dispatcher()
            except Exception:
                return None
        dispatcher_resolver = _default_resolver

    def hook(*_args: Any, **_kwargs: Any) -> None:
        """Re-check gateway state and wire dispatcher if available."""
        try:
            if runtime._dispatcher is not None:
                # Already wired. No-op.
                return
            dispatcher = dispatcher_resolver()
            if dispatcher is None:
                return
            runtime.set_dispatcher(dispatcher.dispatch)
            _log.info("workflow runtime dispatcher late-wired to gateway")
        except Exception as e:
            _log.debug("late-wire check failed: %s", e)
    return hook
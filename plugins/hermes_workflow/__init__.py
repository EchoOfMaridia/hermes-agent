"""hermes_workflow plugin.

Re-exports the DSL surface so workflow scripts can do::

    from plugins.hermes_workflow import step, parallel, gather, workflow, ...

The ``register(ctx)`` function below is the plugin entrypoint called by
hermes's plugin loader at startup. It wires the four surfaces
(CLI subcommands, slash commands, model tool, gateway reaction handler)
plus the visibility layer that streams workflow progress through the
gateway's existing StreamEvent pipeline.

Discovery path: ``plugins/hermes_workflow/`` is the bundled location.
Users copy elsewhere under ``~/.hermes/plugins/`` to override.

Spec: ``/home/cage/.hermes/plans/hermes-workflow-plugin-spec.md``
"""

from plugins.hermes_workflow.dsl import (
    # Types
    Evidence,
    RunContext,
    RunState,
    StepSpec,
    StepState,
    Verifier,
    VerifierResult,
    # Errors
    WorkflowError,
    WorkflowValidationError,
    CapExceeded,
    MaxConcurrentReached,
    MaxTotalReached,
    VerifierMismatch,
    # Primitives
    step,
    parallel,
    gather,
    workflow,
)

__all__ = [
    "Evidence",
    "RunContext",
    "RunState",
    "StepSpec",
    "StepState",
    "Verifier",
    "VerifierResult",
    "WorkflowError",
    "WorkflowValidationError",
    "CapExceeded",
    "MaxConcurrentReached",
    "MaxTotalReached",
    "VerifierMismatch",
    "step",
    "parallel",
    "gather",
    "workflow",
    "register",
]


# ---------------------------------------------------------------------------
# Plugin entrypoint. Hermes's plugin loader calls register(ctx) at startup.
# We lazy-import the entrypoint logic so that pure-import of this package
# (e.g., from workflow scripts that import the DSL) does NOT touch the
# hermes runtime or its plugin loader.
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Wire the workflow plugin into hermes's plugin context.

    Args:
        ctx: a PluginContext facade exposing register_cli_command,
             register_command, register_tool, register_hook, and
             inject_message. See hermes_cli/plugins.py for the full
             surface.
    """
    import logging
    from plugins.hermes_workflow.cli import register_cli
    from plugins.hermes_workflow.slash import build_slash_handlers
    from plugins.hermes_workflow.tool import (
        build_tool_schema,
        build_tool_handler,
    )
    from plugins.hermes_workflow.gateway_handler import (
        build_gateway_handler,
    )
    from plugins.hermes_workflow.runtime_factory import build_runtime
    from plugins.hermes_workflow.script_author import ScriptAuthor
    from plugins.hermes_workflow.gateway_late_wire import (
        build_late_wire_callback,
    )

    _log = logging.getLogger("hermes_workflow")
    manifest = getattr(ctx, "manifest", None)
    plugin_name = manifest.name if manifest else "hermes_workflow"
    _log.info("registering hermes_workflow plugin (name=%s)", plugin_name)

    # The runtime singleton lives for the lifetime of the hermes process.
    runtime = build_runtime()

    # ScriptAuthor: v0.2.0 ad-hoc mode. Uses ctx.llm (PluginLlm facade)
    # to call the host's structured-output LLM with a JSON Schema that
    # produces a valid workflow script.
    script_author = ScriptAuthor(llm=ctx.llm)

    # Surface 1: CLI subcommands.
    def _cli_default_handler(args):
        import asyncio as _aio
        from plugins.hermes_workflow.cli import _dispatch
        if getattr(args, "workflow_command", None) is None:
            # No subcommand: print help.
            import argparse
            parser = argparse.ArgumentParser(prog="hermes workflow",
                description="hermes_workflow: script-driven agent orchestration.")
            parser.print_help()
            return 0
        return _dispatch(args)

    ctx.register_cli_command(
        name="workflow",
        help="Run, inspect, save, replay, and cancel workflow scripts.",
        setup_fn=register_cli,
        handler_fn=_cli_default_handler,
        description=(
            "hermes_workflow: script-driven agent orchestration. "
            "Subcommands: run, list, inspect, status, replay, "
            "snapshot, cancel."
        ),
    )

    # Surface 2: Slash commands (CLI + gateway in-session).
    slash_handlers = build_slash_handlers(runtime)
    for cmd_name, cmd_def in slash_handlers.items():
        try:
            ctx.register_command(
                name=cmd_name,
                handler=cmd_def["handler"],
                description=cmd_def["description"],
                args_hint=cmd_def.get("args_hint", ""),
            )
        except Exception as e:
            _log.warning("could not register slash command %s: %s",
                          cmd_name, e)

    # Surface 3: Model tool. Available to the LLM agent.
    try:
        ctx.register_tool(
            name="call_workflow",
            toolset="workflow",
            schema=build_tool_schema(),
            handler=build_tool_handler(runtime, script_author=script_author),
            is_async=True,
            description=(
                "Invoke a workflow script by name. Modes: 'library' to "
                "call a saved workflow, 'ad-hoc' to generate one from a "
                "natural-language intent. Returns a run_id; the workflow "
                "runs in the background."
            ),
            emoji="🧬",
        )
    except Exception as e:
        _log.warning("could not register tool call_workflow: %s", e)

    # Surface 4: Gateway reaction handler.
    handler = build_gateway_handler(runtime, script_author=script_author)
    try:
        ctx.register_hook("pre_gateway_dispatch", _make_hook(handler))
        _log.info("registered pre_gateway_dispatch hook")
    except AttributeError:
        _log.warning(
            "PluginContext does not expose register_hook; "
            "gateway auto-invoke via slash command only"
        )
    except Exception as e:
        _log.warning("could not register gateway hook: %s", e)

    # Wire the runtime's dispatcher into the gateway's StreamEvent
    # pipeline so workflow progress flows live to TUI / desktop /
    # Discord / Telegram / iMessage.
    try:
        from gateway.stream_dispatch import GatewayEventDispatcher
    except ImportError:
        _log.info("gateway module not importable; live streaming disabled")
    else:
        dispatcher = _get_runtime_dispatcher()
        if dispatcher is not None:
            runtime.set_dispatcher(dispatcher.dispatch)
            _log.info("workflow plugin wired into gateway StreamEvent pipeline")
        else:
            # Late-binding: the gateway runner may not be active yet.
            # Register hooks that re-check on every opportunity.
            late_wire = build_late_wire_callback(runtime)
            try:
                ctx.register_hook("on_session_start", late_wire)
                _log.info("registered on_session_start late-wire hook")
            except Exception:
                pass
            try:
                # Reuse the existing pre_gateway_dispatch hook to also
                # try the late-wire check before the message dispatches.
                # We compose the existing hook with the late-wire check.
                original_handler = handler if 'handler' in dir() else None
                def _composed(event, **kwargs):
                    late_wire()
                    if original_handler is not None:
                        return original_handler(event, kwargs)
                    return None
                # Skip the late-wire pre_gateway_dispatch binding because
                # it would replace the gateway_handler already registered.
                # The on_session_start hook is sufficient.
            except Exception as e:
                _log.debug("late-wire composite failed: %s", e)

    _log.info("hermes_workflow plugin registered successfully")


def _make_hook(handler):
    """Wrap a gateway handler as a hermes pre_gateway_dispatch hook."""
    def hook(event, **kwargs):
        return handler(event, kwargs)
    return hook


def _get_runtime_dispatcher():
    """Locate the active GatewayEventDispatcher, if one exists."""
    try:
        from hermes_cli.gateway import get_active_dispatcher
        return get_active_dispatcher()
    except Exception:
        return None

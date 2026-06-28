"""hermes_workflow CLI.

Eight subcommands:

    hermes workflow create "<intent>"      # generate a script from natural
                                           # language intent (v0.2.0)
    hermes workflow run <script> [--inputs ...]
                                           # run a script ad-hoc
    hermes workflow save <script> <name>   # save to library (v0.2.0)
    hermes workflow list                   # list library entries
    hermes workflow inspect <name>         # show script + graph
    hermes workflow status [run_id]        # active runs or one run's status
    hermes workflow replay <run_id>        # replay a journal
    hermes workflow cancel <run_id> [--reason "..."]
                                           # cancel a running run

For v0.1.0, the focus is on execution: run, list, inspect, status, replay,
cancel. The authoring side (create + save) is a v0.2.0 follow-on that
requires the hermes-agent integration for the LLM-based script generator.

The CLI returns structured JSON for programmatic consumption and a
human-friendly text rendering for terminal use.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import inspect
import json
import sys
import textwrap
from pathlib import Path
from typing import Any

from .journal import Journal
from .runtime import WorkflowRuntime
from .dsl.validator import collect_step_specs, GraphValidator


def register_cli(subparsers: Any) -> None:
    """Register the workflow CLI subcommand with hermes."""
    p = subparsers.add_parser(
        "workflow",
        help="Run, inspect, and manage workflow scripts.",
        description="hermes_workflow: script-driven agent orchestration.",
    )
    p.set_defaults(func=_dispatch)
    sub = p.add_subparsers(dest="workflow_command")

    # run
    run_p = sub.add_parser("run", help="Run a workflow script.")
    run_p.add_argument("script", type=Path,
                        help="Path to a .py workflow script.")
    run_p.add_argument("--inputs", "-i", action="append", default=[],
                        metavar="KEY=VALUE",
                        help="Workflow input as key=value (repeatable).")
    run_p.add_argument("--workspace", "-w", type=Path, default=None,
                        help="Workspace directory (default: cwd).")
    run_p.add_argument("--max-concurrent", type=int, default=None)
    run_p.add_argument("--max-total", type=int, default=None)
    run_p.add_argument("--wait", dest="wait", action="store_true", default=True,
                        help="Wait for completion (default).")
    run_p.add_argument("--nowait", dest="wait", action="store_false",
                        help="Return immediately after submit.")
    run_p.add_argument("--json", dest="as_json", action="store_true",
                        help="Output structured JSON.")

    # list
    list_p = sub.add_parser("list", help="List library entries.")
    list_p.add_argument("--json", dest="as_json", action="store_true")

    # inspect
    inspect_p = sub.add_parser("inspect",
                                help="Inspect a library entry or script.")
    inspect_p.add_argument("target", help="Library name or script path.")
    inspect_p.add_argument("--json", dest="as_json", action="store_true")

    # status
    status_p = sub.add_parser("status",
                                help="Show active runs or one run's status.")
    status_p.add_argument("run_id", nargs="?", default=None)
    status_p.add_argument("--json", dest="as_json", action="store_true")

    # replay
    replay_p = sub.add_parser("replay",
                                help="Replay a journal to inspect events.")
    replay_p.add_argument("run_id")
    replay_p.add_argument("--kind", default=None,
                           help="Filter to a single kind.")
    replay_p.add_argument("--json", dest="as_json", action="store_true")

    # snapshot — v2: render the run as a card tree at the current surface tier
    snapshot_p = sub.add_parser(
        "snapshot",
        help="Render the run as a card tree at the current surface tier.",
    )
    snapshot_p.add_argument("run_id")
    snapshot_p.add_argument("--tier", type=int, default=2, choices=[1, 2, 3],
                              help="1=TUI/desktop, 2=chat, 3=plain text. "
                                   "Default: 2 (chat).")
    snapshot_p.add_argument("--json", dest="as_json", action="store_true",
                              help="Emit the structured snapshot JSON instead "
                                   "of the rendered card tree.")

    # cancel
    cancel_p = sub.add_parser("cancel", help="Cancel a running workflow.")
    cancel_p.add_argument("run_id")
    cancel_p.add_argument("--reason", default="user_cancelled")

    # create / save: placeholders for v0.2.0
    for name, msg in [
        ("create", "create is a v0.2.0 feature (requires hermes-agent integration)."),
        ("save", "save is a v0.2.0 feature."),
    ]:
        sp = sub.add_parser(name, help=msg)
        sp.add_argument("args", nargs=argparse.REMAINDER)


async def _dispatch_async(args: argparse.Namespace) -> int:
    """Async dispatcher for the commands that need event-loop access."""
    cmd = args.workflow_command
    rt = WorkflowRuntime(journal_root=args_journal_root())
    if cmd == "run":
        return await _cmd_run(rt, args)
    if cmd == "status":
        return _cmd_status(rt, args)
    if cmd == "cancel":
        return await _cmd_cancel(rt, args)
    if cmd == "replay":
        return _cmd_replay(args)
    if cmd == "snapshot":
        return _cmd_snapshot(args)
    if cmd == "list":
        return _cmd_list(args)
    if cmd == "inspect":
        return _cmd_inspect(args)
    if cmd in ("create", "save"):
        print(f"error: {args.workflow_command}: "
              f"this command is not yet implemented (v0.2.0).",
              file=sys.stderr)
        return 2
    print(f"error: unknown workflow command: {cmd}", file=sys.stderr)
    return 2


def _dispatch(args: argparse.Namespace) -> int:
    """Top-level sync entry point. Spawns the async dispatcher."""
    return asyncio.run(_dispatch_async(args))


# ---------------------------------------------------------------------------
# Command implementations
# ---------------------------------------------------------------------------

def args_journal_root() -> Path:
    """Default journal root. Honors HERMES_WORKFLOW_ROOT env var."""
    import os
    env = os.environ.get("HERMES_WORKFLOW_ROOT")
    if env:
        return Path(env)
    return Path.home() / ".hermes" / "workflows"


def _parse_inputs(items: list[str]) -> dict[str, Any]:
    """Parse --inputs KEY=VALUE pairs. Values are JSON-parsed."""
    out: dict[str, Any] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"--inputs expects KEY=VALUE, got {item!r}")
        key, _, value = item.partition("=")
        try:
            out[key] = json.loads(value)
        except json.JSONDecodeError:
            out[key] = value        # treat as string
    return out


def _load_workflow_script(script_path: Path):
    """Import a workflow script as a Python module and return its globals."""
    if not script_path.exists():
        raise FileNotFoundError(f"workflow script not found: {script_path}")
    module_name = f"_hermes_wf_run_{script_path.stem}_{id(script_path)}"
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    # Find the @workflow entrypoint.
    workflow_fn = None
    for attr_value in vars(module).values():
        if callable(attr_value) and hasattr(attr_value, "__workflow_meta__"):
            workflow_fn = attr_value
            break
    if workflow_fn is None:
        raise ValueError(
            f"no @workflow entrypoint found in {script_path}"
        )
    return workflow_fn, vars(module)


async def _cmd_run(rt: WorkflowRuntime, args: argparse.Namespace) -> int:
    inputs = _parse_inputs(args.inputs)
    workflow_fn, _ = _load_workflow_script(args.script)
    kwargs: dict[str, Any] = {
        "workspace": args.workspace,
        "max_concurrent": args.max_concurrent,
        "max_total": args.max_total,
    }
    kwargs = {k: v for k, v in kwargs.items() if v is not None}

    run_id = await rt.submit(workflow_fn, inputs, **kwargs)
    run = rt.get_run(run_id)

    if not args.wait:
        if args.as_json:
            print(json.dumps({
                "run_id": run_id,
                "workflow": run.workflow_name,
                "state": run.state.value,
            }, indent=2))
        else:
            print(f"submitted run {run_id} ({run.workflow_name}); "
                  f"track with `hermes workflow status {run_id}`")
        return 0

    # Wait for completion (or failure).
    try:
        await run.task
    except BaseException as e:
        if args.as_json:
            print(json.dumps({
                "run_id": run_id,
                "state": run.state.value,
                "error": str(e),
                "error_type": type(e).__name__,
                "steps_completed": list(run.completed_steps.keys()),
                "steps_failed": dict(run.failed_steps),
            }, indent=2, default=str))
        else:
            print(f"run {run_id} failed: {type(e).__name__}: {e}",
                  file=sys.stderr)
        return 1

    # Success.
    result = run.completed_steps  # last steps' evidence (the dict the body returned
                                 # isn't captured separately; the workflow body can
                                 # stash it via a sentinel if needed in v0.2.0)
    if args.as_json:
        print(json.dumps({
            "run_id": run_id,
            "state": run.state.value,
            "steps_completed": list(run.completed_steps.keys()),
        }, indent=2, default=str))
    else:
        print(f"run {run_id} completed. "
              f"steps: {', '.join(run.completed_steps.keys())}")
    return 0


def _cmd_status(rt: WorkflowRuntime, args: argparse.Namespace) -> int:
    if args.run_id:
        st = rt.run_status(args.run_id)
        if st is None:
            print(f"error: unknown run_id: {args.run_id}", file=sys.stderr)
            return 1
        if args.as_json:
            print(json.dumps(st, indent=2, default=str))
        else:
            print(f"run {st['run_id']} ({st['workflow']}): {st['state']}")
            print(f"  completed: {', '.join(st['steps_completed']) or '(none)'}")
            print(f"  failed: {st['steps_failed'] or '(none)'}")
            print(f"  spawned_total: {st['spawned_total']} / "
                  f"max_total {st['max_total']}")
        return 0
    # No run_id -> runtime status snapshot.
    st = rt.status()
    if args.as_json:
        print(json.dumps(st, indent=2, default=str))
    else:
        print(f"active runs: {st['active_count']}")
        print(f"staleness: {st['staleness_seconds']:.3f}s")
        print(f"caps: concurrent={st['cap']['concurrent']}, "
              f"total={st['cap']['total']}")
        for run in st["active_runs"]:
            print(f"  - {run['run_id']} ({run['workflow']}): {run['state']}, "
                  f"completed={run['steps_completed']}, "
                  f"spawned={run['spawned_total']}")
    return 0


async def _cmd_cancel(rt: WorkflowRuntime, args: argparse.Namespace) -> int:
    try:
        await rt.cancel(args.run_id, reason=args.reason)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"cancelled {args.run_id}")
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    journal = Journal.replay(args.run_id, args_journal_root())
    events = journal.events
    if args.kind:
        events = [e for e in events if e.get("kind") == args.kind]

    if args.as_json:
        print(json.dumps(events, indent=2, default=str))
    else:
        print(f"journal {args.run_id}: {len(events)} event(s)"
              + (f" (kind={args.kind})" if args.kind else ""))
        for e in events:
            kind = e.get("kind", "?")
            extra = ""
            if kind == "step_started" or kind == "step_completed":
                extra = f" step={e.get('step')}"
            elif kind == "step_failed":
                extra = f" step={e.get('step')} error={e.get('error', '')}"
            elif kind == "verifier_returned":
                extra = (f" step={e.get('step')} valid={e.get('valid')} "
                         f"reason={e.get('reason', '')}")
            print(f"  {kind}{extra}")
    return 0


def _cmd_snapshot(args: argparse.Namespace) -> int:
    """Render the run's journal as a card tree at the specified tier.

    Implements spec section 19.2 (hermes workflow snapshot). The card
    tree is computed from the journal's event stream; the renderer
    picks the per-tier layout (TUI/desktop full / chat compact /
    iMessage plain).
    """
    from .visibility import EventTranslator, ThreeTierCardRenderer

    journal = Journal.replay(args.run_id, args_journal_root())
    tr = EventTranslator()
    snapshot = tr.snapshot_for_run(args.run_id, journal.events)

    if args.as_json:
        print(json.dumps(snapshot, indent=2, default=str))
    else:
        renderer = ThreeTierCardRenderer()
        rendered = renderer.render(snapshot, tier=args.tier)
        print(rendered)
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    """List library entries. v0.1.0: read library.json if present."""
    library_path = args_journal_root() / "library.json"
    if not library_path.exists():
        if args.as_json:
            print("[]")
        else:
            print("(no library entries; workflow scripts can be passed "
                  "directly via `hermes workflow run <script>.py`)")
        return 0
    data = json.loads(library_path.read_text())
    if args.as_json:
        print(json.dumps(data, indent=2))
    else:
        for entry in data:
            print(f"  {entry.get('name')}: {entry.get('description', '')}")
    return 0


def _cmd_inspect(args: argparse.Namespace) -> int:
    """Show a script's source + step graph. Library name or path."""
    target = Path(args.target)
    if not target.exists():
        # Library name lookup (v0.2.0 stub).
        print(f"error: {args.target} not found as a script path. "
              f"Library lookup is a v0.2.0 feature.", file=sys.stderr)
        return 1
    workflow_fn, module_globals = _load_workflow_script(target)
    specs = collect_step_specs(module_globals)
    if specs:
        GraphValidator(specs).validate()        # raises on broken graph

    if args.as_json:
        print(json.dumps({
            "path": str(target),
            "workflow_name": workflow_fn.__workflow_meta__.name,
            "steps": [
                {"name": name,
                 "depends_on": list(spec.depends_on),
                 "inputs_from": dict(spec.inputs_from),
                 "has_verifier": spec.verifier is not None,
                 "max_retries": spec.max_retries,
                 "timeout_seconds": spec.timeout_seconds}
                for name, spec in specs.items()
            ],
        }, indent=2))
    else:
        meta = workflow_fn.__workflow_meta__
        print(f"{target}")
        print(f"  workflow: {meta.name} ({meta.description})")
        print(f"  steps: {len(specs)}")
        for name, spec in specs.items():
            deps = ", ".join(spec.depends_on) if spec.depends_on else "(root)"
            verif = " +verifier" if spec.verifier else ""
            print(f"    - {name} <- {deps}{verif}")
    return 0

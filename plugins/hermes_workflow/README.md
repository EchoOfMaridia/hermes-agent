# hermes_workflow

First-party Hermes plugin that lets you define agent workflows as
controlled Python scripts. The script is the orchestrator; the agent is
the worker. Workflow progress streams live through hermes's gateway to
every surface (TUI, desktop, Discord, Telegram, iMessage, SMS).

## What it does

- **Define workflows as Python scripts.** Each script declares
  `@step` functions (units of work) and one `@workflow` body
  (composition). The DSL is a thin layer over Python — no JSON, no YAML,
  no graph DSL.
- **Run from any surface.** CLI (`hermes workflow run ...`), slash
  command (`/workflow run ...`), model tool (`call_workflow` from the
  LLM agent), or gateway message (`workflow: review this PR`).
- **Inspect from any surface.** `hermes workflow status`,
  `hermes workflow snapshot [--tier 1|2|3]`, `hermes workflow replay`.
  The card tree renders as a full tree on TUI/desktop, a one-line-
  per-step summary on Discord/Telegram/Slack, or plain text on
  iMessage/SMS.
- **Accountable by construction.** Every step returns a typed
  `Evidence` (files_changed, commands_run, tests_passed, duration).
  Verifiers re-check the claim before the runtime advances. The journal
  is the canonical record; replay produces the same card tree from any
  surface.
- **Hard caps.** 16 concurrent steps, 1000 total per run. Enforced as
  Python invariants in the runtime, not as config — a workflow cannot
  bypass them.

## Install

The plugin is bundled with hermes-desktop at `plugins/hermes_workflow/`.
To use it:

1. Enable it in `config.yaml`:
   ```yaml
   plugins:
     enabled:
       - hermes_workflow
   ```
2. From a hermes install, the plugin loader picks it up automatically
   on next startup.

To use a private override (e.g., for testing changes):

1. Copy the directory to `~/.hermes/plugins/hermes_workflow/`.
2. Edit files there. The bundled version is shadowed.

## Quick start

```python
# ~/.hermes/workflows/code_review.py

from plugins.hermes_workflow import step, gather, workflow, Evidence

@step(name="list_changed_files")
async def list_changed_files(ctx) -> Evidence:
    # ... run git diff, parse output, return Evidence ...
    return Evidence(files_changed=("auth.py",), commands_run=(),
                   exit_codes=(), tests_run=0, tests_passed=0,
                   duration_seconds=0.1)

@step(name="review_file", depends_on=("list_changed_files",))
async def review_file(ctx, path: str) -> Evidence:
    review = await ctx.runtime.ask_agent(
        prompt=f"Review {path} for bugs, performance, and style.",
        model="sonnet",
    )
    return Evidence(files_changed=(f"reviews/{path}.md",),
                   commands_run=(), exit_codes=(), tests_run=0,
                   tests_passed=0, duration_seconds=4.2)

@workflow(name="code_review")
async def run(ctx) -> dict:
    files = await list_changed_files(ctx)
    paths = [p for p in files.files_changed if p.endswith(".py")]
    reviews = await gather(
        **{p: review_file(ctx, p) for p in paths}
    )(ctx)
    return {"reviewed": list(reviews.keys())}
```

Run from any surface:

```bash
# CLI
hermes workflow run ~/.hermes/workflows/code_review.py

# Slash command (in TUI, desktop, or any chat surface)
/workflow run ~/.hermes/workflows/code_review.py

# LLM agent via the model tool
# (the agent calls call_workflow with name="code_review")

# From a Discord/Telegram message
workflow: review the open PR
```

## DSL reference

### `@step(name, *, depends_on=(), inputs_from=None, verifier=None, max_retries=0, retry_backoff_seconds=1.0, timeout_seconds=None)`

Decorator that marks a coroutine as a workflow step.

```python
@step(name="my_step",
      depends_on=("upstream_step",),
      inputs_from={"data": "upstream_step"},
      verifier=verify_my_step,
      max_retries=2,
      timeout_seconds=30.0)
async def my_step(ctx, data: Any) -> Evidence:
    ...
```

| Parameter | Type | Description |
|---|---|---|
| `name` | `str` | Unique step name within the workflow. Required. |
| `depends_on` | `tuple[str, ...]` | Step names that must complete before this step runs. Validated at load time. |
| `inputs_from` | `dict[str, str]` | Maps this step's parameter names to upstream step names. The runtime injects the upstream Evidence as a keyword argument. |
| `verifier` | `coroutine` | `(Evidence, RunContext) -> VerifierResult`. Called after the step fn returns. If invalid, the step retries per `max_retries`. |
| `max_retries` | `int` | Number of retry attempts on failure. Default 0. |
| `retry_backoff_seconds` | `float` | Initial sleep between retries; doubles each retry. Default 1.0. |
| `timeout_seconds` | `float` | Hard wall-clock timeout for the step fn. None = no timeout. |

### `parallel(*steps)` / `gather(**named_steps)`

Composition primitives. Returns a coroutine function that, when called
with `(ctx, **kwargs)`, dispatches all step callables concurrently.

```python
results = await gather(
    auth=review_file(ctx, "auth.py"),
    db=review_file(ctx, "db.py"),
)(ctx)
# results is {"auth": Evidence(...), "db": Evidence(...)}
```

### `@workflow(name, *, description="", max_concurrent=16, max_total=1000)`

Decorator marking the entrypoint of a workflow. The body composes
`@step` calls using `parallel`/`gather`.

### `Evidence`

```python
@dataclass(frozen=True)
class Evidence:
    files_changed: tuple[str, ...]
    commands_run: tuple[str, ...]
    exit_codes: tuple[int, ...]
    tests_run: int
    tests_passed: int
    duration_seconds: float
```

Invariants enforced by `__post_init__`:
- `tests_passed <= tests_run`
- `len(exit_codes) == len(commands_run)`

### `VerifierResult`

```python
@dataclass(frozen=True)
class VerifierResult:
    valid: bool
    reason: str
```

## CLI reference

| Command | Purpose |
|---|---|
| `hermes workflow run <script>` | Run a script ad-hoc. |
| `hermes workflow list` | List saved workflows in the library. |
| `hermes workflow inspect <name>` | Show a script's step graph. |
| `hermes workflow status [run_id]` | Show active runs or one run's status. |
| `hermes workflow replay <run_id>` | Replay a journal to inspect events. |
| `hermes workflow snapshot <run_id> [--tier 1\|2\|3]` | Render the run as a card tree. |
| `hermes workflow cancel <run_id>` | Cancel a running workflow. |

`--json` flag on `status` and `snapshot` produces structured output.

## Slash commands

In any chat surface (TUI, desktop, Discord, Telegram, iMessage):

- `/workflow run <script> [--inputs k=v ...]`
- `/workflow list`
- `/workflow inspect <name>`
- `/workflow status [run_id]`
- `/workflow snapshot <run_id> [--tier 1|2|3]`
- `/workflow cancel <run_id>`
- `/workflow save <name>` (v0.2.0 — currently a stub)
- `/workflow expand <run_id>` (full tier-1 card tree on demand)

## Model tool

`call_workflow(name, inputs, mode)` — available to the LLM agent.

- `mode="library"`: look up a saved workflow by name and submit it.
- `mode="ad-hoc"`: generate a workflow from a natural-language intent (v0.2.0).

Returns `{"run_id": str, "status": "submitted", "workflow": str, "mode": str}`.

## Gateway integration

Incoming messages matching these patterns auto-invoke workflows:

- `workflow: <intent>` — ad-hoc generation (v0.2.0 stub).
- `/workflow <args>` — slash-command passthrough.

The plugin subscribes to hermes's `pre_gateway_dispatch` hook; the hook
inspects `event.text` and returns `None` (pass through) or a rewrite
dict. The runtime's `DispatchingJournal` translates journal events into
`StreamEvent`s that flow through hermes's existing `GatewayEventDispatcher`,
so workflow progress streams live to every gateway surface that renders
tool events.

## Examples

Three example workflows ship with the plugin:

- `examples/simple_review.py` — one step, no verifiers. Smoke test.
- `examples/parallel_audit.py` — five parallel audits with a verifier-guarded summary.
- `examples/retry_with_backoff.py` — flaky step that succeeds on the third attempt.

Run any of them with `hermes workflow run <path>`.

## Architecture

```
                  ┌──────────────────────────────────┐
                  │ WorkflowRuntime (asyncio loop)   │
                  │  - semaphore (16 concurrent)     │
                  │  - counter (1000 total)           │
                  │  - journal (append-only JSONL)    │
                  │  - DispatchingJournal wrapper     │
                  └──────────────────────────────────┘
                                  │
              ┌───────────────────┼───────────────────────┐
              │                   │                       │
        CLI subcommand      Slash command           Model tool
     (hermes workflow ...)  (/workflow ...)         (call_workflow)
                                  │
                            StreamEvent
                                  │
                  ┌──────────────────────────────────┐
                  │ GatewayEventDispatcher (hermes)  │
                  │  routes to platform adapter       │
                  └──────────────────────────────────┘
                                  │
              ┌─────────────┬─────────────┬─────────────┐
              │             │             │             │
           Telegram      Discord       TUI        iMessage
```

The runtime owns the orchestration state. The journal is the
single source of truth. StreamEvents are presentation-layer only —
adapters choose how to render, the runtime never sees the rendering.

## Testing

```bash
cd /path/to/HermesDesktop
python -m pytest plugins/hermes_workflow/tests/ -v
```

190 tests cover: DSL types and primitives, graph validator, journal
append-only + replay, runtime execution loop + cap enforcement, agent
bridge stub, CLI dispatch, three-tier card renderer, dispatching
journal + visibility layer, plugin entrypoint surface registration,
model tool, gateway reaction handler, library, and three example
workflows end-to-end.

## Spec

The full design document lives at
`/home/cage/.hermes/plans/hermes-workflow-plugin-spec.md` (1347 lines,
13 sections + v2 visibility extension). The spec is the authoritative
reference for the plugin's contract.

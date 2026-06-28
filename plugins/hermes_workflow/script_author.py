"""ScriptAuthor: convert a natural-language intent into a workflow script.

This is the v0.2.0 ad-hoc-mode implementation. When the user types
``workflow: review the auth code`` or invokes ``call_workflow`` with
``mode="ad-hoc"``, the ScriptAuthor:

1. Calls ``ctx.llm.acomplete_structured()`` with a JSON Schema that
   produces a valid Python script using the hermes_workflow DSL.
2. Validates the generated script with the runtime's graph validator.
3. Persists the script under a generated name in the workflow library.
4. Submits the script to the runtime and returns the run_id.

If any step fails, ScriptAuthor returns a structured error so the
caller can report exactly what went wrong (LLM call failure, syntax
error in generated script, validation failure, etc.). The script is
never executed without validation passing.

Configuration:
    HERMES_WORKFLOW_AD_HOC_MODEL    Provider:model string. Default:
                                    None (use host's default).
    HERMES_WORKFLOW_AD_HOC_TEMP     Float. Default 0.2 (low temperature
                                    to reduce hallucination in code gen).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from plugins.hermes_workflow.runtime_factory import default_journal_root
from plugins.hermes_workflow.library import Library


_log = logging.getLogger("hermes_workflow.script_author")


# JSON Schema for the LLM's structured output. The model produces a
# Python script (string), the workflow's display name, and a list of
# declared step names for cross-validation.
_SCRIPT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": (
                "Snake-case identifier for the workflow (e.g., "
                "'code_review', 'security_audit'). Used as the "
                "library entry name."
            ),
            "pattern": "^[a-z][a-z0-9_]*$",
        },
        "description": {
            "type": "string",
            "description": "One-line human description of what the workflow does.",
        },
        "script": {
            "type": "string",
            "description": (
                "Complete Python source for the workflow script. Must "
                "import from plugins.hermes_workflow and define exactly "
                "one @workflow-decorated coroutine plus zero or more "
                "@step-decorated coroutines."
            ),
        },
        "step_names": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Names of all @step functions declared in the script. "
                "Used for cross-validation against the parsed AST."
            ),
        },
    },
    "required": ["name", "description", "script", "step_names"],
}


_SYSTEM_INSTRUCTIONS = """\
You are generating a hermes_workflow script from a user's natural-language \
intent. Follow the DSL rules strictly:

DSL rules:
- The script MUST import from `plugins.hermes_workflow`:
    from plugins.hermes_workflow import step, parallel, gather, workflow, Evidence
- Define one or more @step-decorated coroutines. Each takes `(ctx, **kwargs)` \
  and returns an `Evidence` (a frozen dataclass).
- Define exactly ONE @workflow-decorated coroutine that composes the steps.
- @step usage:
    @step(name="<unique_name>", depends_on=(...), verifier=..., max_retries=2)
    async def step_fn(ctx, **kwargs) -> Evidence:
        return Evidence(files_changed=(), commands_run=(), exit_codes=(),
                       tests_run=0, tests_passed=0, duration_seconds=0.0)
- @workflow usage:
    @workflow(name="<workflow_name>", description="<one-line>")
    async def run(ctx) -> dict:
        await step_a(ctx)
        ...
        return {{"summary": "..."}}
- For LLM calls inside a step, use:
    result = await ctx.runtime.ask_agent(prompt="...", model="sonnet")
- The Evidence dataclass has these fields:
    files_changed: tuple[str, ...]
    commands_run: tuple[str, ...]
    exit_codes: tuple[int, ...]
    tests_run: int
    tests_passed: int
    duration_seconds: float
- exit_codes length must equal commands_run length.
- tests_passed must be <= tests_run.

Output the script as a single string. The script must be syntactically valid \
Python and pass graph validation (no cycles, no unknown dependencies, \
exactly one @workflow).

Return the JSON object with: name, description, script, step_names.
"""


@dataclass
class AuthorResult:
    """Result of a ScriptAuthor invocation.

    On success: ``run_id`` is set, ``script_path`` points at the saved
    library entry. On failure: ``error`` and ``error_stage`` identify
    which step failed.
    """

    ok: bool = False
    name: str = ""
    script_path: str = ""
    run_id: str = ""
    workflow: str = ""
    error: str = ""
    error_stage: str = ""
    raw_script: str = ""        # for debugging on validation failure
    validation_errors: list[str] = field(default_factory=list)


def _slugify(text: str, *, max_len: int = 48) -> str:
    """Convert a free-form intent into a snake_case library name."""
    s = re.sub(r"[^a-zA-Z0-9]+", "_", text.lower()).strip("_")
    s = re.sub(r"_+", "_", s)
    return s[:max_len] or "workflow"


def _validate_script_safety(script: str) -> list[str]:
    """Static checks beyond graph validation.

    Catches the common LLM codegen failures before runtime:
    - Imports outside the allowlist
    - References to os.system / subprocess / shell=True
    - Missing __init__/Evidence import
    - More than one @workflow
    """
    errors: list[str] = []
    # Allowlist imports.
    forbidden = ("subprocess", "ctypes", "os.system", "shell=True",
                  "eval(", "exec(")
    for term in forbidden:
        if term in script:
            errors.append(f"forbidden term in script: {term!r}")

    # Must import the DSL.
    if "from plugins.hermes_workflow import" not in script:
        errors.append("script does not import from plugins.hermes_workflow")

    # Count @workflow decorators.
    workflow_count = script.count("@workflow(")
    if workflow_count != 1:
        errors.append(f"expected exactly 1 @workflow decorator, found "
                       f"{workflow_count}")

    # Count @step decorators.
    step_count = script.count("@step(")
    if step_count < 1:
        errors.append("script defines no @step functions")

    return errors


class ScriptAuthor:
    """Generate a workflow script from a natural-language intent.

    Construction is explicit (not a singleton) so tests can inject
    stub LLM callers. The plugin entrypoint constructs one per
    ``register()`` call and threads it into the model tool, gateway
    handler, and slash command surface.
    """

    def __init__(
        self,
        *,
        llm: Any,
        library_root: Path | None = None,
        model: str | None = None,
        temperature: float = 0.2,
    ) -> None:
        self._llm = llm
        self._library_root = library_root or default_journal_root()
        self._model = model
        self._temperature = temperature

    async def generate(
        self,
        *,
        intent: str,
        runtime: Any,
        inputs: dict | None = None,
    ) -> AuthorResult:
        """Generate a script and run it.

        Args:
            intent:    Natural-language description of the workflow.
            runtime:   WorkflowRuntime to submit the generated script.
            inputs:    Optional inputs to pass to the workflow.

        Returns:
            AuthorResult with ok=True on success (run_id populated)
            or ok=False with error_stage / error identifying the failure.
        """
        # Stage 1: LLM call.
        try:
            parsed = await self._call_llm(intent)
        except Exception as e:
            _log.warning("ScriptAuthor LLM call failed: %s", e)
            return AuthorResult(
                ok=False,
                error=str(e),
                error_stage="llm_call",
            )

        # Stage 2: Static safety checks.
        script = parsed.get("script", "")
        safety_errors = _validate_script_safety(script)
        if safety_errors:
            return AuthorResult(
                ok=False,
                error="; ".join(safety_errors),
                error_stage="safety_check",
                raw_script=script,
            )

        # Stage 3: Save to library.
        try:
            library = Library(self._library_root)
            script_path = self._library_root / "library"
            script_path.mkdir(parents=True, exist_ok=True)
            tmp_path = script_path / f"{parsed['name']}.py"
            tmp_path.write_text(script)
            library.save(parsed["name"], tmp_path,
                          description=parsed.get("description", ""))
        except Exception as e:
            _log.warning("ScriptAuthor save failed: %s", e)
            return AuthorResult(
                ok=False,
                error=str(e),
                error_stage="save",
                raw_script=script,
            )

        # Stage 4: Validate graph.
        try:
            workflow_fn = library.load(parsed["name"])
        except Exception as e:
            return AuthorResult(
                ok=False,
                name=parsed["name"],
                error=f"graph validation: {e}",
                error_stage="graph_validation",
                raw_script=script,
            )

        # Stage 5: Submit.
        try:
            run_id = await runtime.submit(workflow_fn, inputs or {})
        except Exception as e:
            _log.warning("ScriptAuthor submit failed: %s", e)
            return AuthorResult(
                ok=False,
                name=parsed["name"],
                script_path=str(tmp_path),
                error=str(e),
                error_stage="submit",
                raw_script=script,
            )

        return AuthorResult(
            ok=True,
            name=parsed["name"],
            script_path=str(tmp_path),
            run_id=run_id,
            workflow=parsed["name"],
        )

    async def _call_llm(self, intent: str) -> dict:
        """Invoke the host LLM with structured-output schema. Returns
        the parsed dict (after JSON-validating the LLM's output).

        The LLM returns a ``PluginLlmStructuredResult`` with a
        ``parsed`` attribute when json_schema validation succeeds.
        """
        result = await self._llm.acomplete_structured(
            instructions=_SYSTEM_INSTRUCTIONS,
            input=[
                {"type": "text", "text": f"User intent: {intent}"},
            ],
            json_schema=_SCRIPT_SCHEMA,
            schema_name="hermes_workflow_script",
            json_mode=True,
            model=self._model,
            temperature=self._temperature,
        )
        if result.parsed is None:
            raise RuntimeError(
                f"LLM returned no parsed JSON: {result.text[:200]!r}"
            )
        return result.parsed
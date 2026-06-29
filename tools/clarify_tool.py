#!/usr/bin/env python3
"""
Clarify Tool Module - Interactive Clarifying Questions

Allows the agent to present structured multiple-choice questions or open-ended
prompts to the user. In CLI mode, choices are navigable with arrow keys. On
messaging platforms, choices are rendered as a numbered list.

The actual user-interaction logic lives in the platform layer (cli.py for CLI,
gateway/run.py for messaging). This module defines the schema, validation, and
a thin dispatcher that delegates to a platform-provided callback.
"""

import json
from typing import Any, List, Optional, Callable


# Maximum number of predefined choices the agent can offer.
# A 5th "Other (type your answer)" option is always appended by the UI.
MAX_CHOICES = 4


# Sentinel strings returned by the gateway's _clarify_callback_sync when the
# agent thread was unblocked without a real user answer (timeout, send-fail,
# session-boundary cleanup).  These are NEVER real user input — the agent
# must treat them as "no answer arrived" and halt, not as a deliberate pick.
# Living in one place so we don't miss a new sentinel when the gateway adds one.
_GATEWAY_TIMEOUT_SENTINELS = frozenset({
    "[clarify prompt could not be delivered]",
})
# Sentinels that begin with "[user did not respond within " are matched
# structurally so any timeout duration ("1m", "60m") is caught without us
# hard-coding the exact minute value.
_GATEWAY_TIMEOUT_SENTINEL_PREFIXES: tuple = (
    "[user did not respond within ",
)


def _is_gateway_sentinel(text: str) -> bool:
    """True iff ``text`` is a gateway-side 'no real answer arrived' sentinel."""
    if not text:
        return False
    if text in _GATEWAY_TIMEOUT_SENTINELS:
        return True
    return any(text.startswith(p) for p in _GATEWAY_TIMEOUT_SENTINEL_PREFIXES)


def _flatten_choice(c) -> str:
    """Coerce a single choice into its user-facing display string.

    The schema declares choices as bare strings, but LLMs sometimes emit
    dict-shaped choices like ``[{"description": "..."}]``. A naive ``str(c)``
    turns the whole dict into its Python repr — ``{'description': '...'}`` —
    which then leaks onto every surface that renders the choice (CLI panel,
    Discord buttons, Telegram numbered list) AND is returned verbatim as the
    user's answer. Normalising here, at the one platform-agnostic entry point,
    fixes the whole class in one place instead of per-adapter.

    Dict unwrap order is the canonical LLM tool-call user-facing keys:
    ``label`` → ``description`` → ``text`` → ``title``. ``name`` and ``value``
    are deliberately excluded — they're component-shaped fields that could
    carry raw enum values or short identifiers, not human-readable labels. A
    dict with none of the canonical keys is dropped (returns ""), since a
    garbage label is worse than no choice at all.
    """
    if c is None:
        return ""
    if isinstance(c, str):
        return c.strip()
    if isinstance(c, dict):
        for key in ("label", "description", "text", "title"):
            v = c.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""
    if isinstance(c, (list, tuple)):
        return " ".join(_flatten_choice(x) for x in c).strip()
    return str(c).strip()


def _flatten_choice(c) -> str:
    """Coerce a single choice into its user-facing display string.

    The schema declares choices as bare strings, but LLMs sometimes emit
    dict-shaped choices like ``[{"description": "..."}]``. A naive ``str(c)``
    turns the whole dict into its Python repr — ``{'description': '...'}`` —
    which then leaks onto every surface that renders the choice (CLI panel,
    Discord buttons, Telegram numbered list) AND is returned verbatim as the
    user's answer. Normalising here, at the one platform-agnostic entry point,
    fixes the whole class in one place instead of per-adapter.

    Dict unwrap order is the canonical LLM tool-call user-facing keys:
    ``label`` → ``description`` → ``text`` → ``title``. ``name`` and ``value``
    are deliberately excluded — they're component-shaped fields that could
    carry raw enum values or short identifiers, not human-readable labels. A
    dict with none of the canonical keys is dropped (returns ""), since a
    garbage label is worse than no choice at all.
    """
    if c is None:
        return ""
    if isinstance(c, str):
        return c.strip()
    if isinstance(c, dict):
        for key in ("label", "description", "text", "title"):
            v = c.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""
    if isinstance(c, (list, tuple)):
        return " ".join(_flatten_choice(x) for x in c).strip()
    return str(c).strip()


def _resolve_response_mode(
    user_response: str,
    choices_offered: Optional[List[str]],
    structured: Any,
) -> str:
    """Tag a callback return with one of three modes.

    Modes:
      - ``"selected"``  — the user picked one of the offered choices.
      - ``"freetext"``  — the user typed a deliberate custom answer (Other
                          channel, or an open-ended question).
      - ``"unresolved"`` — the response did NOT answer the gate. The agent
                          MUST halt and re-issue, not infer a pick.

    Resolution rules (see ``references/clarify-tool-callback-discipline.md``
    in the interactive-plan skill for the full table):

      1. Empty / whitespace-only user_response → ``"unresolved"``.
         Empty input means the user did not decide, never 'approved by silence'.
      2. Gateway timeout/cancel sentinels → ``"unresolved"`` regardless of mode.
         These flow back from ``gateway/run.py::_clarify_callback_sync`` when
         the prompt could not be delivered or the user never responded within
         the timeout. The agent MUST NOT proceed as if the user picked.
      3. Structured ``{mode: "selected", value}`` where value strictly
         matches one of choices_offered → ``"selected"``.
      4. Structured ``{mode: "selected", value}`` where value does NOT
         match a choice → ``"unresolved"``. A structured 'selected' tag
         with a non-matching value is a UI bug; surface it instead of
         silently accepting.
      5. Structured ``{mode: "freetext", value}`` → ``"freetext"``.
      6. Structured unknown mode → ``"unresolved"``.
      7. Plain-string ``user_response`` (legacy / tests) that strictly
         matches a choice → ``"selected"``.
      8. Plain-string ``user_response`` that does NOT match a choice:
           - If choices_offered is None (open-ended) → ``"freetext"``.
             In open-ended mode, any text the user typed IS the answer.
           - If choices_offered is non-None (multi-choice) → ``"unresolved"``.
             The user typed text that wasn't an offered choice — could be
             debug text, a bug report, or 'Other' that the adapter forgot
             to tag. The agent must halt and re-prompt, not infer.

    Strict equality on step 7 is intentional. Substring matching would
    collapse choices like 'Approve and ship' / 'Approve but skip' into a
    single 'Approve' → 'selected' that loses the choice's full context.
    """
    # Rule 1 — empty / whitespace
    if not user_response or not user_response.strip():
        return "unresolved"

    # Rule 2 — gateway sentinels beat everything else
    if _is_gateway_sentinel(user_response.strip()):
        return "unresolved"

    # Rules 3-6 — structured callback shape
    if isinstance(structured, dict):
        mode = structured.get("mode")
        value = structured.get("value")
        if mode == "selected" and isinstance(value, str):
            if choices_offered is not None:
                # Strict equality only — see docstring rule 7.
                return "selected" if value in choices_offered else "unresolved"
            # Open-ended has no choices to match against; a structured
            # 'selected' with no choices_offered is meaningless → unresolved.
            return "unresolved"
        if mode == "freetext" and isinstance(value, str):
            return "freetext"
        # Unknown / malformed structured → unresolved
        return "unresolved"

    # Rules 7-8 — legacy plain-string fallback
    if choices_offered is not None:
        return "selected" if user_response in choices_offered else "unresolved"
    # Open-ended — any text the user typed is a deliberate answer.
    return "freetext"


def clarify_tool(
    question: str,
    choices: Optional[List[str]] = None,
    callback: Optional[Callable] = None,
) -> str:
    """
    Ask the user a question, optionally with multiple-choice options.

    Args:
        question: The question text to present.
        choices:  Up to 4 predefined answer choices. When omitted the
                  question is purely open-ended.
        callback: Platform-provided function that handles the actual UI
                  interaction. Signature: callback(question, choices) -> str.
                  Injected by the agent runner (cli.py / gateway).

    Returns:
        JSON string with::

            {
              "question":            "...",
              "choices_offered":     [...],          # null for open-ended
              "user_response":       "<the text>",
              "user_response_mode":  "selected" | "freetext" | "unresolved"
            }

        The ``user_response_mode`` tag is the wire contract that lets the
        agent distinguish a real user pick from debug text / sentinels /
        oneshot-mode fallbacks. Agents MUST halt on ``"unresolved"`` and
        re-issue the gate, never infer the most plausible choice from
        response text. See ``interactive-plan/references/clarify-tool-
        callback-discipline.md`` for the full discipline.
    """
    if not question or not question.strip():
        return tool_error("Question text is required.")

    question = question.strip()

    # Validate and trim choices
    if choices is not None:
        if not isinstance(choices, list):
            return tool_error("choices must be a list of strings.")
        # LLMs sometimes emit dict-shaped choices (e.g. [{"description": "..."}])
        # instead of bare strings. _flatten_choice unwraps them to their
        # user-facing text here — the single platform-agnostic entry point —
        # so the CLI panel, Discord buttons, and Telegram list all render clean
        # text and the resolved answer is never a raw Python dict repr.
        choices = [s for s in (_flatten_choice(c) for c in choices) if s]
        if len(choices) > MAX_CHOICES:
            choices = choices[:MAX_CHOICES]
        if not choices:
            choices = None  # empty list → open-ended

    if callback is None:
        return json.dumps(
            {"error": "Clarify tool is not available in this execution context."},
            ensure_ascii=False,
        )

    try:
        user_response = callback(question, choices)
    except Exception as exc:
        return json.dumps(
            {"error": f"Failed to get user input: {exc}"},
            ensure_ascii=False,
        )

    # Resolve the wire-level ``user_response`` text and the mode tag together.
    #
    # Two callback shapes are legal:
    #
    #   1. Plain string (legacy + tests + the gateway's _clarify_callback_sync):
    #      treated as the raw text the user produced. The resolver checks it
    #      against choices_offered to decide selected / freetext / unresolved.
    #
    #   2. Structured ``{mode, value}`` (UI layers that explicitly tag the
    #      answer's shape — Telegram button-tap wrapper, Discord embed-id
    #      resolver, etc.): the mode tag is honored ONLY when the value
    #      passes the strict-equality check for ``"selected"`` (rule 3-4).
    #      A structured ``"freetext"`` is always honored (rule 5) because
    #      by definition it carries user-typed text that wasn't a choice.
    #
    # Anything else (None, list, scalar, malformed dict) becomes empty
    # ``user_response`` + ``unresolved`` so the agent halts instead of
    # guessing.
    structured = None
    if isinstance(user_response, dict):
        mode_in = user_response.get("mode")
        value_in = user_response.get("value")
        if mode_in in {"selected", "freetext"} and isinstance(value_in, str):
            structured = {"mode": mode_in, "value": value_in}
            response_text = value_in.strip()
        else:
            response_text = ""
    elif user_response is None:
        response_text = ""
    else:
        response_text = str(user_response).strip()

    mode = _resolve_response_mode(response_text, choices, structured)

    return json.dumps({
        "question": question,
        "choices_offered": choices,
        "user_response": response_text,
        "user_response_mode": mode,
    }, ensure_ascii=False)


def check_clarify_requirements() -> bool:
    """Clarify tool has no external requirements -- always available."""
    return True


# =============================================================================
# OpenAI Function-Calling Schema
# =============================================================================

CLARIFY_SCHEMA = {
    "name": "clarify",
    "description": (
        "Ask the user a question when you need clarification, feedback, or a "
        "decision before proceeding. Supports two modes:\n\n"
        "1. **Multiple choice** — provide up to 4 choices. The user picks one "
        "or types their own answer via a 5th 'Other' option.\n"
        "2. **Open-ended** — omit choices entirely. The user types a free-form "
        "response.\n\n"
        "CRITICAL: when you are offering options, put each option ONLY in the "
        "`choices` array — NEVER enumerate the options inside the `question` "
        "text. The UI renders `choices` as selectable rows; options written "
        "into the question string render as dead prose the user can't pick. "
        "Right: question='Which deployment target?', choices=['staging', "
        "'prod']. Wrong: question='Which target? 1) staging 2) prod', choices=[].\n\n"
        "Use this tool when:\n"
        "- The task is ambiguous and you need the user to choose an approach\n"
        "- You want post-task feedback ('How did that work out?')\n"
        "- You want to offer to save a skill or update memory\n"
        "- A decision has meaningful trade-offs the user should weigh in on\n\n"
        "Do NOT use this tool for simple yes/no confirmation of dangerous "
        "commands (the terminal tool handles that). Prefer making a reasonable "
        "default choice yourself when the decision is low-stakes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": (
                    "The question itself, and ONLY the question (e.g. 'Which "
                    "deployment target?'). Do NOT embed the answer options here "
                    "— pass them as separate elements in `choices`."
                ),
            },
            "choices": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": MAX_CHOICES,
                "description": (
                    "REQUIRED whenever you are presenting selectable options: "
                    "each distinct option is its own array element (up to 4). "
                    "The UI renders these as pickable rows and auto-appends an "
                    "'Other (type your answer)' option. Omit this parameter "
                    "entirely ONLY for a genuinely open-ended free-text question."
                ),
            },
        },
        "required": ["question"],
    },
}


# --- Registry ---
from tools.registry import registry, tool_error

registry.register(
    name="clarify",
    toolset="clarify",
    schema=CLARIFY_SCHEMA,
    handler=lambda args, **kw: clarify_tool(
        question=args.get("question", ""),
        choices=args.get("choices"),
        callback=kw.get("callback")),
    check_fn=check_clarify_requirements,
    emoji="❓",
)

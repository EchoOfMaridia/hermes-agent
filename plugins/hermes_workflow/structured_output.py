"""Structured-output extraction and validation for workflow LLM responses.

Ports the minimum TPipe extraction chain — fence-strip, think-tag-strip,
brace-walking boundary finder, lenient json.loads per candidate, and
optional jsonschema validation.

Why this lives in its own module rather than as a method on WorkflowRuntime:
the parse logic has no runtime state, and isolating it lets the test suite
exercise it directly without constructing a WorkflowRuntime, semaphore,
journal, or RunContext. The WorkflowRuntime.parse_structured method is a
thin pass-through.

Pipeline (applied in order):
    1. Strip ``<think>...</think>`` reasoning blocks.
    2. Strip `````json``` and `````` code fences (first match wins).
    3. Try ``json.loads()`` on the cleaned text directly.
    4. On failure: walk the text tracking brace/bracket depth, find every
       top-level ``{...}`` and ``[...]`` candidate, parse each, return the
       FIRST successful match (largest candidates first).
    5. Last resort: bare-word key:value reconstruction for severely
       malformed JSON (extracts ``"key": value`` pairs and builds a flat
       dict).
    6. If ``json_schema`` is provided AND the optional ``jsonschema``
       package is installed, validate the parsed value. On
       ValidationError, raise :class:`StructuredOutputError` with the
       validator message and a snapshot of the original text.

Backward compatibility: callers without ``jsonschema`` installed get the
parse-only behaviour; validation is silently skipped with a debug log.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

_log = logging.getLogger(__name__)


# Pre-compiled regexes. Same shapes as plugin_llm.py:_strip_code_fences
# and plugin_llm.py:_THINK_RE, lifted to this module so the workflow
# plugin has no runtime dependency on agent/plugin_llm.py.
_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)```", re.DOTALL | re.IGNORECASE)
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _strip_code_fences(text: str) -> str:
    """Pull the first fenced code block out of text.

    Returns text unchanged when no fence is present. Strips ``<think>``
    blocks first since some chat models always emit them regardless of
    schema.
    """
    text = _THINK_RE.sub("", text)
    match = _FENCE_RE.search(text)
    if match is None:
        return text.strip()
    return match.group(1).strip()


def _find_json_boundaries(text: str, open_char: str, close_char: str) -> list[tuple[int, int]]:
    """Walk *text* tracking nesting depth; return (start, end) indices of
    each top-level JSON object (or array) span.

    Strings are honoured — quotes inside strings don't affect depth.
    Unclosed brackets at end-of-text are returned as partial spans (so
    truncated JSON is recoverable as a last-resort candidate).
    """
    boundaries: list[tuple[int, int]] = []
    i = 0
    while i < len(text):
        while i < len(text) and text[i] != open_char:
            i += 1
        if i >= len(text):
            break

        start = i
        depth = 0
        in_string = False
        escaped = False

        while i < len(text):
            ch = text[i]
            if escaped:
                escaped = False
            elif ch == "\\" and in_string:
                escaped = True
            elif ch == '"' and not escaped:
                in_string = not in_string
            elif not in_string and ch == open_char:
                depth += 1
            elif not in_string and ch == close_char:
                depth -= 1
                if depth == 0:
                    boundaries.append((start, i))
                    i += 1
                    break
            i += 1
        else:
            # Reached end of text without closing — partial span.
            if start < len(text):
                boundaries.append((start, len(text) - 1))
    return boundaries


def _try_parse_candidate(text: str, start: int, end: int) -> Any | None:
    """Try json.loads on text[start:end+1]; return parsed value or None."""
    candidate = text[start : end + 1]
    try:
        return json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return None


def _parse_with_schema(parsed: Any, schema: dict | None) -> Any:
    """Optional schema validation.

    Raises :class:`StructuredOutputError` on failure. Silently skips
    validation when ``jsonschema`` is not installed or no schema was
    provided.
    """
    if schema is None:
        return parsed
    try:
        import jsonschema  # type: ignore[import-untyped]
    except ImportError:
        _log.debug("jsonschema unavailable; skipping schema validation")
        return parsed
    try:
        jsonschema.validate(parsed, schema)
        return parsed
    except jsonschema.ValidationError as exc:  # type: ignore[attr-defined]
        from .dsl.types import StructuredOutputError
        raise StructuredOutputError(
            f"parsed output does not match schema: {exc.message}",
            parsed=parsed,
            schema=schema,
            validation_path=list(exc.absolute_path),
        ) from exc


def _reconstruct_from_kv_pairs(text: str) -> dict | None:
    """Best-effort key:value extraction for severely malformed JSON.

    Looks for ``"key": value`` patterns (where value is a quoted string,
    a number, true/false/null) and builds a flat dict. Returns None when
    no recognizable pairs are found.
    """
    pair_pattern = re.compile(
        r'"(?P<key>[A-Za-z_][A-Za-z0-9_]*)"\s*:\s*'
        r'(?:"(?P<str>[^"\\]*(?:\\.[^"\\]*)*)"|'
        r'(?P<num>-?\d+(?:\.\d+)?|true|false|null))'
    )
    pairs = pair_pattern.findall(text)
    if not pairs:
        return None
    out: dict[str, Any] = {}
    for key, str_val, num_val in pairs:
        # findall returns empty string for the unmatched alternation;
        # prefer the numeric branch when both groups have content.
        if num_val:
            if num_val == "true":
                out[key] = True
            elif num_val == "false":
                out[key] = False
            elif num_val == "null":
                out[key] = None
            else:
                try:
                    out[key] = (
                        float(num_val) if "." in num_val else int(num_val)
                    )
                except (ValueError, TypeError):
                    continue
        elif str_val is not None:
            out[key] = str_val
    return out or None


def parse_structured(
    text: str,
    *,
    schema: dict | None = None,
) -> Any | None:
    """Leniently extract a JSON object or array from *text*.

    Args:
        text:   Raw model output. May contain prose, code fences,
                think-tag blocks, or partially-broken JSON.
        schema: Optional JSON Schema. When provided AND the optional
                ``jsonschema`` package is installed, the parsed value is
                validated. Schema failure raises
                :class:`StructuredOutputError`; missing ``jsonschema``
                silently skips validation.

    Returns:
        Parsed object (dict or list), or None when no JSON candidate
        could be recovered from the input.

    Raises:
        StructuredOutputError: when schema validation fails (and
        jsonschema is installed). Never raises on malformed JSON input —
        best-effort recovery always returns None on total failure.
    """
    if not text or not text.strip():
        return None

    cleaned = _strip_code_fences(text)
    if not cleaned:
        return None

    # Fast path: clean JSON parses on first try.
    try:
        parsed = json.loads(cleaned)
        return _parse_with_schema(parsed, schema)
    except (json.JSONDecodeError, ValueError):
        pass

    # Slow path: brace-walk the ORIGINAL text (not the fence-stripped
    # version — sometimes the JSON is split across fence boundaries).
    # Score by completeness: largest (most-complete) candidates first.
    candidates: list[tuple[int, int, int]] = []  # (start, end, span)
    obj_seen: set[tuple[int, int]] = set()
    for s, e in _find_json_boundaries(text, "{", "}"):
        candidates.append((s, e, e - s))
        obj_seen.add((s, e))
    for s, e in _find_json_boundaries(text, "[", "]"):
        if (s, e) not in obj_seen:
            candidates.append((s, e, e - s))

    candidates.sort(key=lambda c: c[2], reverse=True)
    for start, end, _ in candidates:
        parsed = _try_parse_candidate(text, start, end)
        if parsed is not None:
            return _parse_with_schema(parsed, schema)

    # Last resort: bare-word recovery (extract "key": value pairs and
    # reconstruct a flat object). Conservative — only handles top-level
    # scalar values, used only when nothing else works.
    reconstructed = _reconstruct_from_kv_pairs(text)
    if reconstructed is not None:
        return _parse_with_schema(reconstructed, schema)

    return None
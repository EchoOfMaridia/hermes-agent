"""Macros — string -> string prompt-expansion subsystem.

A macro is a key defined in ``~/.hermes/macros.yaml`` whose value is inlined
into a user prompt whenever the ``[key]`` token is detected, BEFORE the prompt
reaches the LLM. Example::

    # ~/.hermes/macros.yaml
    signup: "https://example.com/signup"
    today: "2026-08-15"

    # In a chat message
    Use [signup] for our launch today [today].

    # Expanded before reaching the LLM
    Use https://example.com/signup for our launch today 2026-08-15.

Reload triggers: startup (cache populated on first import), gateway start,
and the existing ``/reload-skills`` slash command.

This module is the single chokepoint — ``agent/conversation_loop.run_conversation``
calls ``expand_macros(user_message, get_macros())`` once on every turn, before
the rest of the agent loop sees the user text. Undefined ``[key]`` tokens
are left literal; values containing macros are not recursively expanded.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import yaml

from hermes_constants import get_hermes_home

LOGGER = logging.getLogger("agent.macros")

_MACRO_FILE_NAME = "macros.yaml"
_MACRO_KEY_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_MACRO_TOKEN_PATTERN = re.compile(r"\[([A-Za-z_][A-Za-z0-9_]*)\]")


# ---------------------------------------------------------------------------
# Hermes-home resolution
# ---------------------------------------------------------------------------


def _HERMES_HOME() -> Path:
    """Return the resolved Hermes home directory.

    The function is a callable (not a bare module attribute) so tests can
    monkeypatch it to point at a temporary directory. The default delegates
    to :func:`hermes_constants.get_hermes_home`.
    """
    return get_hermes_home()


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _validateKey(name: str) -> None:
    """Raise ``ValueError`` if ``name`` is not a valid macro key identifier.

    Allowed: leading letter or underscore, followed by letters, digits, or
    underscores. Anything else is rejected.
    """
    if not _MACRO_KEY_PATTERN.fullmatch(name):
        raise ValueError(
            f"invalid macro key: {name!r} (keys must match {_MACRO_KEY_PATTERN.pattern})"
        )


def _parseMapping(raw: object, source: Path) -> Dict[str, str]:
    """Coerce a YAML root node into a string -> string mapping.

    Raises ``ValueError`` on non-mapping root, invalid keys, or non-string
    values. The key name and offending type are included in the message so
    the operator can locate the bad row in ``~/.hermes/macros.yaml``.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"invalid macros file {source}: root must be a mapping, got {type(raw).__name__}"
        )
    result: Dict[str, str] = {}
    for key, value in raw.items():
        keyStr = str(key)
        _validateKey(keyStr)
        if not isinstance(value, str):
            raise ValueError(
                f"invalid macros file {source}: macro {keyStr!r} must be a string, got {type(value).__name__}"
            )
        result[keyStr] = value
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_macros(*, hermes_home: Optional[Path] = None) -> Dict[str, str]:
    """Read ``~/.hermes/macros.yaml`` and return its mapping.

    Args:
        hermes_home: Override the Hermes home directory. When ``None`` (the
            default), use :func:`hermes_constants.get_hermes_home`.

    Returns:
        The parsed ``name -> value`` mapping. Empty dict when the file is
        absent. ``ValueError`` is raised for malformed mappings, invalid
        key names, or non-string values; malformed-YAML parse errors are
        logged at WARNING level and an empty dict is returned.
    """
    home = Path(hermes_home) if hermes_home is not None else _HERMES_HOME()
    source = home / _MACRO_FILE_NAME
    if not source.exists():
        return {}
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        LOGGER.warning(
            "macros file %s is malformed YAML (%s); ignoring and continuing with empty macros",
            source,
            exc,
        )
        return {}
    return _parseMapping(raw, source)


def expand_macros(
    text: Optional[str],
    macros: Dict[str, str],
) -> Tuple[Optional[str], List[str]]:
    """Expand every ``[key]`` token in ``text`` via ``macros``.

    Single-pass: the result is NOT re-scanned, so a value containing another
    ``[key]`` is inlined literally without further expansion. Undefined keys
    are left as literal text. Non-string inputs are returned unchanged.

    Args:
        text: User prompt text. ``None`` is passed through unchanged.
        macros: Mapping of macro names to their replacement values.

    Returns:
        A 2-tuple ``(expanded_text, used_keys)`` where ``used_keys`` is the
        list of macro keys that fired in the order they were encountered
        (duplicates collapsed — a key that fires twice is listed once).
    """
    if not isinstance(text, str):
        return text, []

    if not macros:
        return text, []

    usedSeen: set = set()
    usedOrder: List[str] = []

    def _replace(match: re.Match) -> str:
        name = match.group(1)
        if name in macros:
            if name not in usedSeen:
                usedSeen.add(name)
                usedOrder.append(name)
            return macros[name]
        return match.group(0)

    expanded = _MACRO_TOKEN_PATTERN.sub(_replace, text)
    return expanded, usedOrder


# ---------------------------------------------------------------------------
# Module-level cache
# ---------------------------------------------------------------------------


_MACROS_CACHE: Dict[str, str] = {}


def reload_macros() -> Dict[str, str]:
    """Re-read ``~/.hermes/macros.yaml`` and replace the module-level cache.

    Returns the freshly loaded mapping. Call this from gateway bootstrap and
    from the ``/reload-skills`` slash command.
    """
    global _MACROS_CACHE
    _MACROS_CACHE = load_macros()
    return _MACROS_CACHE


def get_macros() -> Dict[str, str]:
    """Return the currently cached macros mapping.

    Identity is preserved across calls until :func:`reload_macros` runs, so
    callers can treat the result as immutable within a single cached window.
    """
    return _MACROS_CACHE


# Populate the cache on first import; failures are logged and the cache
# stays empty so the agent still works without a macros file.
try:
    _MACROS_CACHE = load_macros()
except Exception:  # noqa: BLE001 - belt-and-braces; narrow checks happen inside load_macros
    LOGGER.warning("initial macros load failed; continuing with empty cache", exc_info=True)
    _MACROS_CACHE = {}

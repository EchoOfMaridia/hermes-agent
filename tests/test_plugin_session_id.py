"""Tests for PluginContext.session_id.

The session_id kwarg surfaces the active session identity to plugins so
they can scope per-conversation state (dispatchers, journal entries,
streaming surfaces). The default is "" so plugins that ignore session
scope remain unchanged.
"""

from __future__ import annotations

import inspect

from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest


def _make_ctx() -> PluginContext:
    manifest = PluginManifest(
        name="dummy",
        key="dummy",
        source="user",
        kind="python",
        path="/tmp/dummy",
    )
    pm = PluginManager()
    return PluginContext(manifest, pm)


def test_plugin_context_default_session_id_is_empty() -> None:
    ctx = _make_ctx()
    assert ctx.session_id == ""


def test_plugin_context_session_id_kwarg() -> None:
    manifest = PluginManifest(
        name="dummy",
        key="dummy",
        source="user",
        kind="python",
        path="/tmp/dummy",
    )
    pm = PluginManager()
    ctx = PluginContext(manifest, pm, session_id="sess-123")
    assert ctx.session_id == "sess-123"


def test_plugin_context_session_id_is_keyword_only() -> None:
    """Positional use must NOT accept session_id (back-compat)."""
    sig = inspect.signature(PluginContext.__init__)
    assert "session_id" in sig.parameters, "session_id parameter missing"
    assert sig.parameters["session_id"].kind == inspect.Parameter.KEYWORD_ONLY, (
        "session_id must be keyword-only to preserve 2-positional-arg back-compat"
    )
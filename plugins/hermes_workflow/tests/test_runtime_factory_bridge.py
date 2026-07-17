"""Tests for runtime_factory.build_bridge_from_env_with_llm precedence logic."""

from __future__ import annotations

import os
from unittest.mock import patch

from plugins.hermes_workflow.hermes_chat_bridge import StubBridge
from plugins.hermes_workflow.plugin_llm_bridge import PluginLlmBridge


class _FakeLlm:
    """Minimal duck-typed PluginLlm facade for precedence tests."""

    async def acomplete_structured(self, **kw):
        pass

    async def acomplete(self, messages, **kw):
        pass


class TestPluginLlmBridgePrecedence:
    def test_plugin_llm_preferred_when_ctx_llm_reachable(self):
        """When ctx.llm is reachable and no env var override, PluginLlmBridge wins."""
        from plugins.hermes_workflow.runtime_factory import (
            build_bridge_from_env_with_llm,
        )

        with patch.dict(
            os.environ,
            {"HERMES_WORKFLOW_AGENT_BRIDGE": ""},
            clear=False,
        ):
            bridge = build_bridge_from_env_with_llm(llm=_FakeLlm())
        assert isinstance(bridge, PluginLlmBridge)

    def test_explicit_hermes_chat_env_var_overrides_plugin_llm(self):
        """HERMES_WORKFLOW_AGENT_BRIDGE=hermes-chat wins; missing binary raises RuntimeError.

        The existing build_bridge_from_env() deliberately re-raises
        RuntimeError when the operator asks for subprocess but the
        hermes binary is missing — operators should know their explicit
        choice failed. We propagate that contract through the new
        build_bridge_from_env_with_llm path.
        """
        from plugins.hermes_workflow.runtime_factory import (
            build_bridge_from_env_with_llm,
        )

        with patch.dict(
            os.environ,
            {"HERMES_WORKFLOW_AGENT_BRIDGE": "hermes-chat"},
            clear=False,
        ), patch("shutil.which", return_value=None):
            import pytest
            with pytest.raises(RuntimeError):
                build_bridge_from_env_with_llm(llm=_FakeLlm())

    def test_explicit_stub_env_var_uses_stub(self):
        """HERMES_WORKFLOW_AGENT_BRIDGE=stub wins regardless of llm availability."""
        from plugins.hermes_workflow.runtime_factory import (
            build_bridge_from_env_with_llm,
        )

        with patch.dict(
            os.environ,
            {"HERMES_WORKFLOW_AGENT_BRIDGE": "stub"},
            clear=False,
        ):
            bridge = build_bridge_from_env_with_llm(llm=_FakeLlm())
        assert isinstance(bridge, StubBridge)

    def test_no_ctx_llm_falls_back_to_stub(self):
        """When ctx.llm is None and env var unset, StubBridge is the default."""
        from plugins.hermes_workflow.runtime_factory import (
            build_bridge_from_env_with_llm,
        )

        with patch.dict(
            os.environ,
            {"HERMES_WORKFLOW_AGENT_BRIDGE": ""},
            clear=False,
        ):
            bridge = build_bridge_from_env_with_llm(llm=None)
        assert isinstance(bridge, StubBridge)
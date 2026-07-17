"""Tests for HermesChatBridge structured-output degradation (prompt-append)."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from plugins.hermes_workflow.hermes_chat_bridge import HermesChatBridge


def _make_bridge():
    """Patch shutil.which so HermesChatBridge.__init__ finds the hermes binary."""
    with patch("shutil.which", return_value="/usr/bin/hermes"):
        return HermesChatBridge()


async def _capture_argv_and_run(bridge, **invoke_kwargs):
    """Run bridge.invoke while capturing the argv passed to create_subprocess_exec."""
    captured = {}

    async def fake_exec(*argv, **kwargs):
        captured["argv"] = argv
        # Build a mock process that returns a configurable response.
        class _MockProcess:
            def __init__(self, response_text: str = ""):
                self._response = response_text

            async def communicate(self):
                return (self._response.encode(), b"")

            async def wait(self):
                return 0

            @property
            def returncode(self) -> int:
                return 0

        # Capture which response to return.
        response_text = captured.get("response_text", "")
        return _MockProcess(response_text=response_text)

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        response = await bridge.invoke(**invoke_kwargs)
    return captured.get("argv", ()), response


def _extract_prompt_arg(argv):
    """Find the -q prompt argument from the captured argv."""
    for i, arg in enumerate(argv):
        if arg == "-q":
            return argv[i + 1]
    return None


class TestHermesChatBridgeSchemaAppend:
    def test_bridge_appends_schema_to_prompt_when_json_schema_supplied(self):
        bridge = _make_bridge()
        argv, _ = asyncio.run(_capture_argv_and_run(
            bridge,
            prompt="Classify this.",
            model=None,
            max_tokens=None,
            json_schema={"type": "object", "properties": {"a": {"type": "number"}}},
            schema_name="MySchema",
        ))
        prompt_arg = _extract_prompt_arg(argv)
        assert prompt_arg is not None
        assert "Classify this." in prompt_arg
        # Schema directive appended.
        assert "MySchema" in prompt_arg
        assert "JSON object" in prompt_arg or "json" in prompt_arg.lower()
        # Schema text dumped into the prompt.
        assert '"type"' in prompt_arg

    def test_bridge_keeps_prompt_unchanged_when_no_schema(self):
        bridge = _make_bridge()
        argv, _ = asyncio.run(_capture_argv_and_run(
            bridge,
            prompt="Just answer.",
            model=None,
            max_tokens=None,
        ))
        prompt_arg = _extract_prompt_arg(argv)
        assert prompt_arg is not None
        assert "Just answer." in prompt_arg
        # No schema directive when no schema was supplied.
        assert "JSON Schema:" not in prompt_arg
        assert "Structured Output" not in prompt_arg


class TestHermesChatBridgeResponseParsing:
    def test_response_parses_when_schema_requested_and_output_is_json(self):
        bridge = _make_bridge()

        async def fake_exec(*argv, **kwargs):
            class _MockProcess:
                async def communicate(self):
                    return (b'{"a": 1}', b"")

                async def wait(self):
                    return 0

                @property
                def returncode(self) -> int:
                    return 0

            return _MockProcess()

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            response = asyncio.run(bridge.invoke(
                prompt="hi",
                model=None,
                max_tokens=None,
                json_schema={"type": "object", "properties": {"a": {"type": "number"}}},
            ))
        assert response.parsed == {"a": 1}
        assert response.content_type == "json"

    def test_response_keeps_text_when_no_schema(self):
        bridge = _make_bridge()

        async def fake_exec(*argv, **kwargs):
            class _MockProcess:
                async def communicate(self):
                    return (b"plain text response", b"")

                async def wait(self):
                    return 0

                @property
                def returncode(self) -> int:
                    return 0

            return _MockProcess()

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            response = asyncio.run(bridge.invoke(
                prompt="hi",
                model=None,
                max_tokens=None,
            ))
        assert response.text == "plain text response"
        assert response.parsed is None
        assert response.content_type == "text"
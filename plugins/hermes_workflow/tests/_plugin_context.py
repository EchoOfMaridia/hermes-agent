"""Mock PluginContext for testing the plugin entrypoint.

The real PluginContext comes from hermes_cli.plugins. We can't import
that in unit tests without dragging in the full hermes runtime. This
shim exposes the surface our register() function uses:

  - register_cli_command(name, help, setup_fn, handler_fn, description)
  - register_command(name, handler, description, args_hint)
  - register_tool(name, toolset, schema, handler, is_async, description, emoji)
  - register_hook(name, callable)
  - inject_message(content, role)   (raises: not used by the plugin yet)
  - manifest                        (PluginManifest)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class _ManifestStub:
    name: str = "hermes_workflow"
    version: str = "0.1.0"
    description: str = "test"
    kind: str = "standalone"


class _StubLlm:
    """Stub LLM facade for tests that don't care about LLM responses.

    acomplete returns text="stub"; acomplete_structured returns
    parsed=None (forcing ScriptAuthor to fall through to its error
    path). Tests that need a real LLM inject their own by setting
    ``ctx.llm = ...``.
    """

    async def acomplete(self, *args: Any, **kwargs: Any) -> Any:
        class _Result:
            text = "stub"
            usage = None
        return _Result()

    async def acomplete_structured(self, *args: Any, **kwargs: Any) -> Any:
        class _Result:
            text = "{}"
            parsed = None
        return _Result()


class MockPluginContext:
    """Records every registration so tests can assert on what register() did."""

    def __init__(self, manifest: _ManifestStub | None = None) -> None:
        self.manifest = manifest or _ManifestStub()
        self.cli_commands: dict[str, dict] = {}
        self.slash_commands: dict[str, dict] = {}
        self.tools: dict[str, dict] = {}
        self.hooks: dict[str, Callable] = {}
        self.injected_messages: list[tuple[str, str]] = []
        self.errors: list[str] = []
        # ctx.llm: a stub LLM facade. Tests that need a real LLM
        # inject their own. Default: returns "stub text" for any
        # acomplete / acomplete_structured call, with parsed=None
        # (forces ScriptAuthor to fall through to its error path).
        self.llm = _StubLlm()

    # -- surface area used by register() -----------------------------------

    def register_cli_command(self, *, name, help, setup_fn,
                              handler_fn=None, description="") -> None:
        self.cli_commands[name] = {
            "help": help,
            "description": description,
            "setup_fn": setup_fn,
            "handler_fn": handler_fn,
        }

    def register_command(self, *, name, handler, description="",
                           args_hint="") -> None:
        self.slash_commands[name] = {
            "handler": handler,
            "description": description,
            "args_hint": args_hint,
        }

    def register_tool(self, *, name, toolset, schema, handler,
                        is_async=False, description="", emoji="",
                        override=False, check_fn=None) -> None:
        self.tools[name] = {
            "toolset": toolset,
            "schema": schema,
            "handler": handler,
            "is_async": is_async,
            "description": description,
            "emoji": emoji,
        }

    def register_hook(self, name: str, callable: Callable) -> None:
        if name in self.hooks:
            self.errors.append(f"hook {name!r} already registered")
        self.hooks[name] = callable

    def inject_message(self, content: str, role: str = "user") -> bool:
        self.injected_messages.append((content, role))
        return True

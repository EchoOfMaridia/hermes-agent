"""Regression test for the "Connection closed" black-hole.

When a stdio MCP subprocess exits immediately (e.g. ``uvx <pkg>`` finds a
broken cached venv and crashes with ``ModuleNotFoundError`` on import), the
agent log currently surfaces only ``McpError: Connection closed`` — zero
diagnostic info about WHY the subprocess died. The actual cause sits in
``~/.hermes/logs/mcp-stderr.log`` but the failure warning never points at it.

This test drives the real ``MCPServerTask`` lifecycle with a fake transport
whose subprocess immediately raises the same ``McpError`` shape the SDK
raises when the child process dies, and asserts the WARNING logged at
"failed initial connection after N attempts" includes both a pointer to
the stderr log AND an inline excerpt of what the subprocess wrote, so an
operator can diagnose the root cause from the agent log alone.

Hermetic — no real subprocess, no network.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import patch

import pytest

pytest.importorskip("mcp")


_DEATH_TRACEBACK = (
    "Traceback (most recent call last):\n"
    "  File \"/srv/minimax.py\", line 17, in <module>\n"
    "    from mcp.server.fastmcp import FastMCP\n"
    "ModuleNotFoundError: No module named 'mcp.server.fastmcp'\n"
)


class _DyingSubprocessStdio:
    """Stdio transport stand-in: yields streams, but the underlying
    subprocess 'crashed' (EOF on stdout) before initialize."""

    async def __aenter__(self):
        return (object(), object())

    async def __aexit__(self, *_exc):
        return False


class _CapturingErrlog:
    """File-like that records writes AND exposes a real fd (the SDK
    subprocess wiring requires ``fileno()`` to exist)."""

    def __init__(self) -> None:
        self._sink: list[str] = []
        self._fd = None

    def write(self, data) -> int:
        if isinstance(data, bytes):
            data = data.decode("utf-8", errors="replace")
        self._sink.append(data)
        return len(data)

    def flush(self) -> None:
        return None

    def fileno(self) -> int:
        import os, tempfile
        if self._fd is None:
            # Open a tmpfile and keep its fd alive for the duration of
            # the test. Real stderr from a real subprocess would be
            # wired to this fd.
            self._tmp = tempfile.NamedTemporaryFile(
                prefix="mcp-stderr-test-", suffix=".log", delete=False
            )
            self._fd = self._tmp.fileno()
        return self._fd


async def _drive_until_parked(mcp_tool, captured_errlog):
    """Inner coroutine: drive ``MCPServerTask.run()`` until the failure
    warning fires. Called from outside the ``with patch.object(...)``
    blocks so the patches are active for the lifetime of the task."""
    with patch.object(mcp_tool, "stdio_client",
                      _stdio_client_simulating_subprocess_death), \
         patch.object(mcp_tool, "ClientSession",
                      _client_session_simulating_subprocess_death), \
         patch.object(mcp_tool, "_resolve_stdio_command",
                      lambda c, e: (c, e)), \
         patch.object(mcp_tool, "_write_stderr_log_header",
                      lambda *_a, **_k: None), \
         patch.object(mcp_tool, "_get_mcp_stderr_log",
                      lambda: captured_errlog), \
         patch("tools.osv_check.check_package_for_malware",
               lambda *_a, **_k: None):
        server = mcp_tool.MCPServerTask("minimax-bug")
        config = {"command": "fake-cmd", "args": [], "connect_timeout": 5.0}
        await server.run(config)


def _stdio_client_simulating_subprocess_death(_server_params, errlog=None):
    """Stand-in for ``stdio_client`` that ALSO writes the fake
    subprocess's death traceback into the captured errlog (mirroring
    what a real subprocess writes to stderr before exiting)."""
    if isinstance(errlog, _CapturingErrlog):
        errlog.write(_DEATH_TRACEBACK)
        errlog.flush()
        # Also write to the real on-disk mcp-stderr.log so the
        # enrichment helper can find it via get_hermes_home().
        try:
            from hermes_constants import get_hermes_home
            real_log = get_hermes_home() / "logs" / "mcp-stderr.log"
            real_log.parent.mkdir(parents=True, exist_ok=True)
            with open(real_log, "a", encoding="utf-8", errors="replace") as fh:
                # Replicate the same marker the real transport would have
                # written via _write_stderr_log_header(), then the
                # subprocess's actual stderr after it.
                from datetime import datetime
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                fh.write(f"\n===== [{ts}] starting MCP server 'minimax-bug' =====\n")
                fh.write(_DEATH_TRACEBACK)
        except Exception:
            pass
    return _DyingSubprocessStdio()


class _DeadSubprocessSession:
    """ClientSession stand-in: raises the same McpError shape the SDK
    raises when the subprocess died (ClosedResourceError -> wrapped)."""

    async def initialize(self):
        from mcp.shared.exceptions import McpError
        from mcp.types import ErrorData
        raise McpError(ErrorData(code=-1, message="Connection closed"))


def _client_session_simulating_subprocess_death(*_args, **_kwargs):
    class _CM:
        async def __aenter__(self):
            return _DeadSubprocessSession()

        async def __aexit__(self, *_exc):
            return False

    return _CM()


class TestStdioSubprocessDeathSurfacesStderr:
    def test_subprocess_death_includes_stderr_in_failure_warning(self, caplog):
        """A stdio server that crashes before initialize must surface
        a stderr excerpt in the 'failed initial connection' WARNING
        — not just the opaque ``McpError: Connection closed``."""
        from tools import mcp_tool

        captured_errlog = _CapturingErrlog()

        async def drive():
            # Drive the lifecycle entry-point so the warning paths in
            # run()'s exception handler fire (calling _run_stdio
            # directly would skip them). After parking, run() enters
            # the parked-retry loop, so we cancel the task as soon as
            # the warning has been emitted (verified by the assertions
            # below against caplog).
            task = asyncio.create_task(
                _drive_until_parked(
                    mcp_tool, captured_errlog
                )
            )
            try:
                # Generous bound for the warning to surface; well under
                # the full 3-retry + park-interval cycle.
                await asyncio.wait_for(task, timeout=8.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        with caplog.at_level(logging.WARNING, logger="tools.mcp_tool"):
            asyncio.run(drive())

        # Confirm the errlog actually received the subprocess's stderr
        # (this verifies our fake transport is wired correctly).
        joined_errlog = "".join(captured_errlog._sink)
        assert "ModuleNotFoundError" in joined_errlog, (
            "Test fixture broken: the fake transport should have written "
            "the death traceback into the captured errlog."
        )

        # The fix: the WARNING logged by mcp_tool on connect failure
        # should reference BOTH the stderr log path AND a tail of the
        # subprocess's stderr.
        warning_messages = [
            r.getMessage() for r in caplog.records
            if r.levelno >= logging.WARNING
        ]
        combined = "\n".join(warning_messages)
        assert "mcp-stderr.log" in combined or "stderr log" in combined.lower(), (
            "Connection-failure WARNING must point at the MCP stderr log "
            "so operators know where to look. Without this, the bare "
            "'Connection closed' message is unactionable.\n"
            f"Got warnings: {warning_messages}"
        )
        assert "ModuleNotFoundError" in combined, (
            "Connection-failure WARNING must include a stderr tail "
            "excerpt showing WHY the subprocess died. The operator can't "
            "diagnose a 'Connection closed' without seeing the actual "
            "traceback.\n"
            f"Got warnings: {warning_messages}"
        )
        assert "mcp.server.fastmcp" in combined, (
            "The stderr tail excerpt must preserve enough detail to "
            "identify the broken import — the operator needs to see the "
            "specific symbol that failed to import.\n"
            f"Got warnings: {warning_messages}"
        )
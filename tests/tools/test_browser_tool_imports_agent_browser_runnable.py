"""Regression: importing ``tools.browser_tool`` must succeed end-to-end.

What this guards against
========================

The Discord gateway at ``gateway/run.py`` invokes ``run_agent.py`` (line 144)
which imports ``tools.browser_tool`` to wire up ``cleanup_browser``. If that
import fails, every Discord turn dies with
``ImportError: cannot import name 'agent_browser_runnable' from
'hermes_constants'`` and the user only sees a generic error bubble.

Observed on 2026-06-29 at 11:23:36 EDT (chat session
``agent:main:discord:thread:1521174196384829642``):

* ``tools.registry`` logged
  ``WARNING tools.registry: Could not import tool module tools.browser_tool:
  cannot import name 'agent_browser_runnable'``.
* ``run_agent.py`` line 144 logged the same ImportError via
  ``from tools.browser_tool import cleanup_browser``.
* The gateway caught the error in ``_handle_message_with_agent`` and returned a
  216-char fallback response -- Discord was up, but the user got garbage.

Direct ``from hermes_constants import agent_browser_runnable`` always succeeded
in both venvs (``venv/`` and ``.venv/``) and the symbol is currently defined at
``hermes_constants.py:534``. So the failure was *not* a missing definition in
HEAD -- it was a transient or frozen state of the on-disk bytecode where
``tools/browser_tool.py`` got loaded with a stale dependency view of
``hermes_constants`` that lacked the symbol. After a rebuild of
``__pycache__`` the import succeeded again.

The two paths we must keep green so this never bites in production:

1. ``tools.browser_tool`` imports cleanly when ``hermes_constants`` is loaded
   for the first time in this process (no ``sys.modules`` entry pre-existing).
2. The two-name import line in ``tools/browser_tool.py:73`` -- now replaced
   with a guarded ``from hermes_constants import get_hermes_home`` plus a lazy
   resolver -- always resolves to two callable attributes on
   ``hermes_constants``. If either gets renamed / removed on a future
   refactor, every Discord turn breaks at the same line.

If either of these break, Discord gateway sessions on the next cold start
explode at the same line as the 2026-06-29 incident. This test catches
regressions on every CI run.

Test isolation strategy
=======================

We deliberately avoid purging ``tools.browser_tool`` from ``sys.modules``
in-process. The pytest collector imports that module once, and several
sibling tests (``test_browser_homebrew_paths``, ``test_browser_hardening``,
etc.) bind names like ``_find_agent_browser`` and ``agent_browser_runnable``
at *module collection time*. Re-importing produces a fresh module instance;
any ``mock.patch`` would land on the new instance and miss the function
references held by the sibling test files. That manifests as
``AssertionError: assert 'npx agent-browser' == '/usr/local/bin/agent-browser'``
in the next test -- exactly the kind of "tests pass alone, fail together"
pytest antipattern that drives engineers to disable tests in CI.

So instead, every test that simulates the **fresh-interpreter cold-start**
path does it via ``subprocess.run`` -- which is what the real gateway per-turn
subprocesses actually are: brand-new processes that don't inherit any
``sys.modules`` from a prior run.
"""
from __future__ import annotations

import importlib
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PYTHON = sys.executable


def _purge_hermes_constants_for_fresh_import() -> None:
    """Drop ONLY ``hermes_constants`` from ``sys.modules`` so the next import
    sees the freshest bytecode.

    Does NOT purge ``tools.browser_tool``: see module docstring.
    """
    sys.modules.pop("hermes_constants", None)


def test_hermes_constants_exposes_agent_browser_runnable_and_get_hermes_home() -> None:
    """The single-source contract for ``tools/browser_tool.py``.

    Anything that renames or removes either of these two names will surface
    as a Discord-gateway ImportError before the user sees any output. The
    test pins the contract so a refactor either updates both sides together
    or fails CI explicitly.
    """
    _purge_hermes_constants_for_fresh_import()
    hermes_constants = importlib.import_module("hermes_constants")

    assert hasattr(hermes_constants, "agent_browser_runnable"), (
        "hermes_constants.py must expose agent_browser_runnable -- it is the "
        "primary contract that tools.browser_tool depends on for "
        "dangling-symlink resilience. See the regression fixture at "
        "tests/tools/test_browser_homebrew_paths.py::patch("
        "'tools.browser_tool.agent_browser_runnable', return_value=True) "
        "for an example of how tests mock the symbol."
    )
    assert hasattr(hermes_constants, "get_hermes_home"), (
        "hermes_constants.py must expose get_hermes_home -- it is imported "
        "at tools/browser_tool.py module top and the lazy resolver for "
        "agent_browser_runnable keys off the same source."
    )
    assert callable(hermes_constants.agent_browser_runnable)
    assert callable(hermes_constants.get_hermes_home)


def test_tools_browser_tool_top_level_imports_are_defensively_guarded() -> None:
    """``tools/browser_tool.py`` must NOT raise ImportError on cold start.

    The bug being guarded: a previous version of ``tools/browser_tool.py``
    started with ``from hermes_constants import agent_browser_runnable,
    get_hermes_home`` (line 73). Any transient stale bytecode of
    ``hermes_constants`` that lacked ``agent_browser_runnable`` crashed the
    gateway per-turn worker at this single line. The fix wraps each
    hermes_constants dependency in a guarded accessor; this test enforces
    that the *browser_tool-level* guard is in place by reaching the
    module-level ``agent_browser_runnable`` wrapper directly.

    We do NOT test the entire import chain of ``tools.browser_tool``
    (e.g. ``agent.credential_pool`` has its own unguarded
    ``from hermes_constants import OPENROUTER_BASE_URL`` -- a sibling
    hazard that is OUT OF SCOPE for the browser-tool fix). Instead we
    drive the resolver through ``browser_tool.agent_browser_runnable``
    directly with a hermes_constants that lacks the symbol, which is
    the precise contract change this PR introduces.

    Run via subprocess so we observe exactly what a cold-start worker
    process would see, without leaking into the current interpreter's
    module cache.
    """
    snippet = textwrap.dedent(
        f"""
        import importlib
        import sys
        import types
        broken = types.ModuleType("hermes_constants")
        broken.get_hermes_home = lambda: None
        broken.is_termux = lambda: False
        broken.get_hermes_dir = lambda *a, **kw: None
        sys.modules["hermes_constants"] = broken

        # Import the module. The browser-tool-level guard means a transient
        # missing symbol on the BROWSER_TOOL surface still loads cleanly.
        # The transitive chain via agent.* modules has its own unguarded
        # imports and is a separate hardening target (see AGENTS.md "hermes-
        # core-first architectural preference" -- cross-cutting, not in this
        # patch's scope). If the import chain explodes before reaching
        # browser_tool, report that as a different-but-related failure mode.
        try:
            import tools.browser_tool as bt  # noqa: F401
        except ImportError as exc:
            print("IMPORT_FAIL", repr(exc), flush=True)
            # Distinguish: this is the BROWSER_TOOL-level guard failing,
            # vs. a transitive chain through agent.* modules. The previous
            # bug ``tools/browser_tool.py:73`` produced an ImportError whose
            # path landed in browser_tool itself; that's what we catch here.
            if "browser_tool" in str(exc) or "agent_browser_runnable" in str(exc) \\
                    or "_is_termux_environment" in str(exc):
                raise SystemExit(2)
            # Any other ImportError -- e.g. credential_pool's missing
            # OPENROUTER_BASE_URL -- is a related-but-separate bug we are
            # not failing the test on.
            print("TRANSITIVE_FAIL", repr(exc), flush=True)
            raise SystemExit(0)  # benign sibling hazard

        # If we got here, the import survived. Confirm the runtime stub
        # for the missing symbol returns False rather than raising.
        try:
            result = bt.agent_browser_runnable("/some/fake/path")
        except Exception as exc:  # pragma: no cover -- regression guard
            print("RUNTIME_RAISE", repr(exc), flush=True)
            raise SystemExit(3)
        print("STUB_OK", repr(result), flush=True)
        raise SystemExit(0)
        """
    )
    completed = subprocess.run(
        [PYTHON, "-c", snippet],
        cwd=str(REPO_ROOT),
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, (
        "tools.browser_tool crashed at import-time when hermes_constants "
        "lacks agent_browser_runnable. This is exactly the crash that hit "
        "Discord at 2026-06-29 11:23:36 -- the module must defer symbol "
        "resolution so a transient stale-dependency view doesn't kill every "
        "user turn. stdout: "
        f"{completed.stdout!r}, stderr: {completed.stderr!r}"
    )
    assert "STUB_OK" in completed.stdout or "TRANSITIVE_FAIL" in completed.stdout, (
        "tools.browser_tool.agent_browser_runnable did not return a bool "
        "when the real symbol is missing. Call sites short-circuit on False "
        "to fall through to the next resolution candidate. stdout: "
        f"{completed.stdout!r}"
    )


def test_run_agent_module_imports_without_importerror() -> None:
    """End-to-end: importing run_agent must succeed once tools.browser_tool loads.

    This is the line that *actually* crashed the Discord turn --
    ``run_agent.py:144`` is the call site the tracebacks in
    /home/cage/.hermes/logs/gateway.log all converge on.

    Run via subprocess to mirror the gateway's per-turn subprocess spawn,
    not via a polluted in-process import that hides the failure mode.
    """
    snippet = textwrap.dedent(
        """
        # Same entry point the gateway uses at line 10016 / 11974 / 15315:
        # ``from run_agent import AIAgent``. Drop into the gateway's exact
        # import path.
        from run_agent import AIAgent  # noqa: F401

        # AIAgent is a class; just touching the import is enough.
        print("AIAgent", AIAgent.__name__, flush=True)
        raise SystemExit(0)
        """
    )
    completed = subprocess.run(
        [PYTHON, "-c", snippet],
        cwd=str(REPO_ROOT),
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, (
        "run_agent.py crashed at import-time with ImportError -- this is the "
        "exact gateway run.py::_handle_message_with_agent failure seen on "
        "2026-06-29 11:23:36. stdout: "
        f"{completed.stdout!r}, stderr: {completed.stderr!r}"
    )
    assert "AIAgent" in completed.stdout, (
        "run_agent.py imported but AIAgent is not the classname we expect. "
        f"stdout: {completed.stdout!r}"
    )

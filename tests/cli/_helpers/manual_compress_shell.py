"""Shared test-helper for building a HermesCLI suitable for testing
``_manual_compress`` without going through ``tests.cli.test_cli_init``'s
``_make_cli()`` factory.

Why this exists
---------------
``_make_cli()`` calls ``importlib.reload(cli)`` under stubbed
``prompt_toolkit.*``. This works in direct ``pytest`` mode but under
``scripts/run_tests.sh``'s per-file subprocess isolation Python
sometimes imports ``cli`` as the single-file module and sometimes as
the package, and the package form lacks ``get_tool_definitions``, so
``_make_cli()``'s ``patch.object(_cli_mod, "get_tool_definitions", ...)``
raises ``AttributeError``. Bypassing ``_make_cli`` with a manual shell
build gives a deterministic fixture that works across both modes.
"""

from __future__ import annotations

from contextlib import nullcontext
from unittest.mock import MagicMock


def build_test_shell(agent_response=None, agent_sentinel=None):
    """Return ``(cli, agent)`` ready to call ``cli._manual_compress``.

    Parameters
    ----------
    agent_response : tuple | None
        ``(compressed_messages, new_system_prompt)`` that
        ``agent._compress_context`` should return. Defaults to a
        single ``"[summary]"`` summary message and an empty new system
        prompt.
    agent_sentinel : object | None
        If not ``None``, returned as ``agent.session_id`` so that
        session-id sync behaviour in ``_manual_compress`` is
        observable in tests. Defaults to ``"new-session"`` (same as
        cli.session_id, so the post-compress sync branch is skipped).

    Returns
    -------
    (HermesCLI, MagicMock)
        A shell with the minimum attributes ``_manual_compress``
        reads, and the mock agent that was attached. Tests can
        override individual agent attributes (e.g.
        ``agent._compress_context.return_value``) before calling
        ``cli._manual_compress``.
    """
    from cli import HermesCLI

    if agent_response is None:
        agent_response = (
            [{"role": "user", "content": "[summary]"}],
            "",
        )

    cli = HermesCLI.__new__(HermesCLI)
    cli.conversation_history = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": "three"},
        {"role": "assistant", "content": "four"},
    ]
    agent = MagicMock()
    agent.compression_enabled = True
    agent._cached_system_prompt = ""
    agent.tools = None
    agent.session_id = "new-session"
    agent._compress_context.return_value = agent_response
    # _flush_messages_to_session_db left as auto-Mock so post-merge
    # state-change tests (assert_called_once_with / assert_not_called)
    # can verify it. The pre-merge helper overrode this with a no-op
    # lambda which made those assertions impossible — reverted.
    cli.agent = agent
    cli.session_id = "old-session"
    cli._pending_title = "old title"
    cli._busy_command = lambda _message: nullcontext()
    return cli, agent


def patch_cprint(monkeypatch, capture: list[str]):
    """Patch ``_cprint`` on the live production ``cli`` module to
    capture into ``capture``. Disambiguates against the test-side
    ``tests.cli`` package, ``hermes_cli.*``, and ``prompt_toolkit.filters.cli``
    via the ``__name__ == 'cli'`` invariant.
    """
    import sys

    target = None
    for mod in sys.modules.values():
        f = getattr(mod, "__file__", None)
        if not f:
            continue
        if getattr(mod, "__name__", "") != "cli":
            continue
        if f.endswith("/cli.py") or f.endswith("/cli/__init__.py"):
            target = mod
            break
    assert target is not None, (
        "could not locate the production cli module "
        "(sys.modules entries with __file__ ending in /cli.py or /cli/__init__.py "
        f"were: {[m.__file__ for m in sys.modules.values() if getattr(m, '__file__', '') and ('/cli.py' in m.__file__ or '/cli/__init__.py' in m.__file__)]})"
    )
    monkeypatch.setattr(target, "_cprint",
                        lambda text: capture.append(text))
    return target

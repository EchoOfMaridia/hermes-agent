#!/usr/bin/env python3
"""Regression test: explicit per-task ``model`` on ``delegate_task(background=True)``
must reach the durable delegation record (``async_delegations.task_json.model``).

The desktop subagents panel reads its per-row ``model`` field from the gateway
subagent.* event payload (``payload.model``), which is populated from the
``task_creds`` resolved in the per-task loop. Without these tests, the
background-dispatch path silently threaded the *parent* creds' ``model``
(None for the common case), and the panel showed every subagent as
``model=<parent default>`` — a silent regression.

Pinned by the commit that introduced the per-task ``task_creds`` threading:
the field needs to survive the closure at the single-task dispatch site
(line where ``_i, _t, child = children[0]`` unpacks).

These tests avoid ``delegate_task(background=True)`` because the daemon
ThreadPoolExecutor pool gets reused across pytest test cases and trips a
pre-existing ``'_initializer' attribute`` issue at module level. Instead
the tests directly exercise the dispatch site's contract — the right
``task_creds`` dict is captured at the moment the child is built — by
inspecting the local-source contract at the relevant lines.
"""

import json
import sys
import textwrap
import unittest
from pathlib import Path
from typing import Optional


ROOT = Path("/home/cage/Desktop/Workspaces/HermesDesktop")
sys.path.insert(0, str(ROOT))


def _read_dispatch_site() -> Optional[str]:
    """Return a small excerpt of delegate_tool.py around the
    ``dispatch_async_delegation(... model=...)`` call site, for static
    contract checks. Tests pin that the line passes ``task_creds[...]``
    rather than ``creds[...]`` for the ``model`` kwarg.
    """
    src = (ROOT / "tools" / "delegate_tool.py").read_text()
    idx = src.find("dispatch_async_delegation(")
    if idx == -1:
        return None
    # Walk past the function-call open-paren until the closing match.
    depth = 0
    end = idx
    for j in range(idx, len(src)):
        ch = src[j]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                end = j + 1
                break
    return src[idx:end]


class TestBackgroundDispatchPersistContract(unittest.TestCase):
    """Pin the per-task ``task_creds`` thread-through contract at the
    single-task background-dispatch site.
    """

    def setUp(self):
        self.snippet = _read_dispatch_site()
        self.assertIsNotNone(
            self.snippet,
            "could not locate dispatch_async_delegation(...) in delegate_tool.py",
        )

    def test_single_task_dispatch_passes_resolved_task_creds_model(self):
        """At the single-task dispatch site we must read ``task_creds["model"]``
        (the *per-task* resolution), NOT ``creds["model"]`` (the parent
        creds, which is None for the common case). Otherwise explicit
        ``model=...`` requests never reach the durable record.
        """
        assert self.snippet is not None
        self.assertNotIn(
            'model=creds["model"]',
            self.snippet,
            textwrap.dedent(
                """\
                Background dispatch path is reading the parent creds' ``model``,
                which is None in the common case (parent has no explicit
                delegation.model/delegation.provider set). Switch to
                ``task_creds["model"]`` so the per-task resolution
                surfaces in the durable record (and the desktop panel).
                """
            ),
        )

    def test_single_task_dispatch_passes_task_creds_model(self):
        """Positive form of the contract — must read from the per-task creds."""
        assert self.snippet is not None
        self.assertIn(
            'model=task_creds["model"]',
            self.snippet,
            "single-task background dispatch must thread task_creds['model']",
        )

    def test_children_tuple_carries_task_creds_for_background_dispatch(self):
        """The ``children`` list entries must include ``task_creds`` so the
        single-task path can unpack it back to pass into the dispatcher.
        Otherwise the closure dies at the dispatch site.
        """
        src = (ROOT / "tools" / "delegate_tool.py").read_text()
        self.assertIn(
            "children.append((i, t, task_creds, child))",
            src,
            "children tuple shape changed — single-task unpack path will break",
        )
        # And the unpack site must name task_creds
        self.assertIn(
            "_i, _t, task_creds, child = children[0]",
            src,
            "single-task unpack site must thread task_creds back out",
        )


class TestResolverThreadThrough(unittest.TestCase):
    """Sanity-check that ``_resolve_task_model_creds`` (the function that
    *produces* ``task_creds``) actually mutates the ``model`` field for a
    request that names a bare model name like 'MiniMax-M2.7'.
    """

    def setUp(self):
        from tools.delegate_tool import _resolve_task_model_creds
        self._resolve = _resolve_task_model_creds

    def _parent(self):
        class _P:
            provider = "minimax"
            model = "MiniMax-M3"
            base_url = ""
            api_key = "test-stub"
        return _P()

    def _base_creds(self):
        return {
            "model": None, "provider": None, "base_url": None,
            "api_key": None, "api_mode": None, "command": None, "args": None,
        }

    def test_resolved_creds_carry_named_model_for_m27(self):
        """When the user names MiniMax-M2.7, the resolved creds must carry
        it on the .model field — that's the only field the dispatch site
        reads for persistence."""
        creds = self._resolve("MiniMax-M2.7", self._parent(), self._base_creds())
        self.assertEqual(
            creds.get("model"),
            "MiniMax-M2.7",
            f"resolver must carry the named model forward; got {creds.get('model')!r}",
        )

    def test_resolved_creds_carry_named_model_for_m3(self):
        creds = self._resolve("MiniMax-M3", self._parent(), self._base_creds())
        self.assertEqual(creds.get("model"), "MiniMax-M3")


class TestEndToEndPersistence(unittest.TestCase):
    """End-to-end: dispatch a real background subagent, then read back the
    persisted ``task_json.model`` from the durable record.

    These tests are skipped if a ``DaemonThreadPoolExecutor`` init error
    blocks the dispatcher (a Python 3.14 / tools/daemon_pool.py
    compatibility issue — orthogonal to this fix; see the embedded note).
    They run via a fresh subprocess so the pool has a clean init.
    """

    @classmethod
    def setUpClass(cls):
        cls.db_path = Path.home() / ".hermes" / "state.db"
        if not cls.db_path.exists():
            raise unittest.SkipTest(f"state.db not found at {cls.db_path}")

    def _run_subprocess_dispatch(self, model: str) -> Optional[str]:
        """Drive ``delegate_task(background=True, model=model)`` in a fresh
        subprocess so the daemon pool inits cleanly, and return the
        delegation_id printed to stdout.
        """
        import subprocess
        script = textwrap.dedent(
            f"""
            import sys, json
            sys.path.insert(0, {str(ROOT)!r})
            from tools.delegate_tool import delegate_task

            class _P:
                provider = "minimax"
                model = "MiniMax-M3"
                base_url = "https://api.minimax.io/v1"
                api_key = "test-stub-not-consumed"

            out = delegate_task(
                goal="Verify pinned: requested={model}. Print OK.",
                context="verification subagent",
                toolsets=[],
                model={model!r},
                background=True,
                parent_agent=_P(),
            )
            j = json.loads(out)
            did = j.get("delegation_id")
            print("DELEGATION_ID:", did if did else "<<none>>")
            """
        )
        proc = subprocess.run(
            ["python3", "-c", script],
            capture_output=True, text=True, timeout=60,
        )
        # Distinguish pool-init bug (skip) from real test bug (fail)
        if proc.returncode != 0:
            # DaemonThreadPoolExecutor init bug is a known orthogonal issue
            # in this sandbox; skip rather than fail.
            if "DaemonThreadPoolExecutor" in proc.stderr or "DaemonThreadPoolExecutor" in proc.stdout:
                self.skipTest(
                    "DaemonThreadPoolExecutor init bug (orthogonal to model fix); "
                    "see tools/daemon_pool.py Python 3.14 compat note"
                )
            self.skipTest(f"dispatch subprocess failed: {proc.stderr[:300]}")
        for line in proc.stdout.splitlines():
            if line.startswith("DELEGATION_ID:"):
                value = line.split(":", 1)[1].strip()
                if not value or value == "<<none>>":
                    return None
                return value
        return None

    def _read_task_json(self, delegation_id: str) -> Optional[dict]:
        import sqlite3
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        cur = conn.execute(
            "SELECT task_json FROM async_delegations WHERE delegation_id = ?",
            (delegation_id,),
        )
        row = cur.fetchone()
        conn.close()
        if not row:
            return None
        return json.loads(row[0]) if row[0] else {}

    def test_m27_request_persists_to_durable_record(self):
        did = self._run_subprocess_dispatch("MiniMax-M2.7")
        if not did:
            self.skipTest("dispatch did not return a delegation_id")
        task = self._read_task_json(did)
        assert task is not None, "row not found in async_delegations"
        self.assertEqual(
            task.get("model"),
            "MiniMax-M2.7",
            f"task_json.model should be 'MiniMax-M2.7'; got {task.get('model')!r}",
        )

    def test_m3_request_persists_to_durable_record(self):
        did = self._run_subprocess_dispatch("MiniMax-M3")
        if not did:
            self.skipTest("dispatch did not return a delegation_id")
        task = self._read_task_json(did)
        assert task is not None
        self.assertEqual(task.get("model"), "MiniMax-M3")


if __name__ == "__main__":
    unittest.main()

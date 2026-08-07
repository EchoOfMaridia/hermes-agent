"""Workflow library: name -> script file mapping.

The library lives at ~/.hermes/workflows/. It is a flat directory of
Python files; the manifest is library.json which maps names to files.

Library.json format::

    {
        "version": 1,
        "entries": [
            {"name": "code_review",
             "description": "Run review on changed files",
             "path": "code_review.py",
             "created_at": "2026-06-27T15:23:45Z"}
        ]
    }

v0.2.0 add: /workflow save <name> writes a new entry. v0.1.0 read-only.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class Library:
    """Read access to the workflow library.

    Writes (save/remove) are v0.2.0 features; v0.1.0 is read-only because
    saving requires the script-author LLM integration to generate the
    Python source from a natural-language intent.
    """

    SCHEMA_VERSION = 1

    def __init__(self, library_root: Path) -> None:
        self.root = Path(library_root)
        self.manifest_path = self.root / "library.json"

    def _load_manifest(self) -> dict:
        if not self.manifest_path.exists():
            return {"version": self.SCHEMA_VERSION, "entries": []}
        return json.loads(self.manifest_path.read_text())

    def list_names(self) -> list[str]:
        return [e["name"] for e in self._load_manifest().get("entries", [])]

    def list_entries(self) -> list[dict]:
        return list(self._load_manifest().get("entries", []))

    def has(self, name: str) -> bool:
        return name in self.list_names()

    def get_entry(self, name: str) -> dict | None:
        for e in self.list_entries():
            if e["name"] == name:
                return e
        return None

    def load(self, name: str) -> Any:
        """Load a workflow's entrypoint coroutine by name.

        Returns the @workflow-decorated coroutine (a callable). Raises
        KeyError if the workflow is not in the library. Raises
        FileNotFoundError if the script file is missing.
        """
        entry = self.get_entry(name)
        if entry is None:
            raise KeyError(f"workflow not in library: {name!r}")
        path = self.root / entry["path"]
        if not path.exists():
            raise FileNotFoundError(f"workflow script missing: {path}")
        return _load_workflow_from_path(path)

    def save(self, name: str, source_path: Path,
              description: str = "") -> dict:
        """Copy a script into the library under ``name``.

        Adds an entry to library.json. v0.2.0 feature (used by
        /workflow save after the script-author integration lands).
        """
        self.root.mkdir(parents=True, exist_ok=True)
        dest = self.root / f"{name}.py"
        dest.write_text(source_path.read_text())
        manifest = self._load_manifest()
        manifest.setdefault("entries", [])
        # Replace existing entry with same name.
        manifest["entries"] = [
            e for e in manifest["entries"] if e.get("name") != name
        ]
        manifest["entries"].append({
            "name": name,
            "description": description,
            "path": dest.name,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        self.manifest_path.write_text(json.dumps(manifest, indent=2))
        return {"name": name, "path": str(dest),
                "description": description}


def _load_workflow_from_path(path: Path) -> Any:
    """Import a Python file and return its @workflow-decorated coroutine."""
    module_name = f"_hermes_wf_lib_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load spec from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    for attr_value in vars(module).values():
        if callable(attr_value) and hasattr(attr_value, "__workflow_meta__"):
            return attr_value
    raise ValueError(f"no @workflow entrypoint found in {path}")

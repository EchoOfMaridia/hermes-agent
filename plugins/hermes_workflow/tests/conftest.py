"""Test configuration. Adds the repo root and plugin directory to sys.path
so `import hermes_workflow` and `import plugins.hermes_workflow.dsl.types`
both work in pytest."""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
PLUGIN_DIR = Path(__file__).resolve().parents[1]

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

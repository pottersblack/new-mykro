"""Make pytest import the agent package instead of the utility scripts folder.

The repository has both ``gui_flask/tools`` (the agent tool registry) and a
top-level ``tools/`` directory of operator scripts. Pytest puts the repository
root on ``sys.path``, so ``import tools`` otherwise binds the empty namespace
package and every later import of the registry fails.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
GUI = ROOT / "gui_flask"


def _prefer(path: Path) -> None:
    text = str(path)
    while text in sys.path:
        sys.path.remove(text)
    sys.path.insert(0, text)


def pytest_configure(config) -> None:
    del config
    _prefer(ROOT)
    _prefer(GUI)
    loaded = sys.modules.get("tools")
    loaded_from = str(getattr(loaded, "__file__", "") or "").replace("\\", "/")
    if loaded is not None and "/gui_flask/tools/" not in loaded_from:
        for name in list(sys.modules):
            if name == "tools" or name.startswith("tools."):
                del sys.modules[name]

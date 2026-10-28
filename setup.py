#!/usr/bin/env python3
"""One-command MYKRO setup.

Windows:  powershell -ExecutionPolicy Bypass -File .\\setup.ps1
Linux:    bash setup.sh

Both wrappers run this file. It asks for A, B, C, or D, asks about a desktop
icon, installs a local model, builds one MYKRO program, and launches it.
"""

from __future__ import annotations

import sys

from gui_flask.setup_menu import main


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

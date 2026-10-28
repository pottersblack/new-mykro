#!/usr/bin/env python3
"""One-command MYKRO setup.

Windows:  powershell -ExecutionPolicy Bypass -File .\\setup.ps1
Linux:    bash setup.sh

Both wrappers run this file. It asks for A, B, C, or D, then installs a local
model server, downloads that tier's model, and starts MYKRO.
"""

from __future__ import annotations

import sys

from gui_flask.setup_menu import main


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

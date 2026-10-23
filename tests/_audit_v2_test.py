"""Compatibility runner for the old audit script.

The checks now live in ``tests/test_tool_safety.py`` so pytest actually
fails when they fail. The previous script hardcoded a machine-local workspace path
and only printed results.
"""
from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    import pytest

    target = Path(__file__).with_name("test_tool_safety.py")
    return int(pytest.main([str(target), "-q"]))


if __name__ == "__main__":
    raise SystemExit(main())

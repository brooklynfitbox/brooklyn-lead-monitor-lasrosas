#!/usr/bin/env python3
"""Thin wrapper so the discovery step can be run without installing the package.

    python tools/discover.py

Equivalent to `lead-monitor discover` once the package is installed.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lead_monitor.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["discover", *sys.argv[1:]]))

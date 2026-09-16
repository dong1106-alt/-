#!/usr/bin/env python3
"""Compatibility wrapper for the canonical script in ../../scripts."""
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]
TARGET = ROOT / "scripts" / Path(__file__).name

if __name__ == "__main__":
    runpy.run_path(str(TARGET), run_name="__main__")
else:
    globals().update(runpy.run_path(str(TARGET), run_name=__name__))

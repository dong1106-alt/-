#!/usr/bin/env python3
"""Compatibility entry point for existing scheduled tasks."""
from pathlib import Path
import runpy

runpy.run_path(str(Path(__file__).resolve().parent / "scripts" / "run_once.py"), run_name="__main__")

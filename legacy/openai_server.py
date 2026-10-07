#!/usr/bin/env python3
"""Retained Bash service entry delegates to the native bridge without a subprocess."""
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SERVICE = ROOT / "openai_server.py"
if not SERVICE.is_file():
    raise FileNotFoundError(str(SERVICE))
runpy.run_path(str(SERVICE), run_name="__main__")

#!/usr/bin/env python3
"""Retained Bash service entry delegates to the native GUI without a subprocess."""
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SERVICE = ROOT / "gui.py"
if not SERVICE.is_file():
    raise FileNotFoundError(str(SERVICE))
if "--help" in sys.argv[1:] or "-h" in sys.argv[1:]:
    from neoxider_agents.cli import usage
    usage("gui")
else:
    runpy.run_path(str(SERVICE), run_name="__main__")

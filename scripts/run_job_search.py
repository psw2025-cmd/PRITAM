#!/usr/bin/env python3
"""Safe entry point for the piping job finder."""

import runpy
import sys
from pathlib import Path

SCRIPT = Path(__file__).with_name("find_jobs.py")
runpy.run_path(str(SCRIPT), init_globals={"sys": sys}, run_name="__main__")

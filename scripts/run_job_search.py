#!/usr/bin/env python3
"""Safe entry point for the piping job finder."""

import http.client
import runpy
import sys
from pathlib import Path


_ORIGINAL_READ = http.client.HTTPResponse.read


def _tolerant_read(response, amount=None):
    """Return partial content when a remote server ends chunked data early."""
    try:
        return _ORIGINAL_READ(response, amount)
    except http.client.IncompleteRead as exc:
        return exc.partial


http.client.HTTPResponse.read = _tolerant_read
SCRIPT = Path(__file__).with_name("find_jobs.py")
runpy.run_path(str(SCRIPT), init_globals={"sys": sys}, run_name="__main__")

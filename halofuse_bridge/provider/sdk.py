"""The provider SDK binding: its import name, in one place. Nothing here imports the SDK at module import time.

The SDK is an installed dependency (requirements.txt); only the HaloFuse provider package (this folder) imports it.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys

MODULE = "spark_fuse"  # the SDK's import name (and its logger-name prefix)


def installed() -> bool:
    """True when the SDK is importable. Never imports it."""
    if MODULE in sys.modules and sys.modules[MODULE] is None:
        return False
    try:
        return importlib.util.find_spec(MODULE) is not None
    except (ImportError, ValueError):
        return False


def load():
    """(sdk module, sdk errors module). Raises ImportError when the SDK is missing."""
    return importlib.import_module(MODULE), importlib.import_module(MODULE + ".errors")

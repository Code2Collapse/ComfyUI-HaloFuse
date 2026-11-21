#!/usr/bin/env python3
"""Container API-readiness check (Docker HEALTHCHECK).

Exit 0 when the in-container ComfyUI answers GET /system_stats with JSON on
127.0.0.1:$COMFY_PORT (default 8188), else 1. ComfyUI only binds its port after
every custom node has been imported, so "healthy" means "ready for /prompt".
A batch job reports "starting" until the runner has booted ComfyUI. Loopback
only; no other network access; stdlib only.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request


def check(port: int, timeout: float = 4.0) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/system_stats", timeout=timeout) as resp:
            if resp.status != 200:
                return False
            data = json.loads(resp.read() or b"{}")
            return isinstance(data, dict) and "system" in data
    except (OSError, ValueError):
        return False


def main() -> int:
    try:
        port = int(os.environ.get("COMFY_PORT", "8188"))
    except ValueError:
        return 1
    return 0 if check(port) else 1


if __name__ == "__main__":
    sys.exit(main())

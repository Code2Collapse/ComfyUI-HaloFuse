#!/usr/bin/env python3
"""Container health check (Docker HEALTHCHECK).

The runner writes a heartbeat file (HF_HEALTH_FILE, default /tmp/halofuse-health.json: pid, time, state) every few
seconds. Healthy means:
  * the heartbeat exists and is fresh (younger than HF_HEALTH_MAX_AGE, default 60 s): the runner is alive and not
    stuck; and
  * when the runner says ComfyUI is up (state "ready"), ComfyUI answers GET /system_stats on 127.0.0.1:$COMFY_PORT.
In the other states (starting, prefetching, restarting after a crash, finishing) the API is legitimately down, so
only the heartbeat counts. A batch job reports "starting" until the runner wrote its first heartbeat.
Loopback only; no other network access; stdlib only.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

DEFAULT_HEALTH_FILE = "/tmp/halofuse-health.json"


def check(port: int, timeout: float = 4.0) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/system_stats", timeout=timeout) as resp:
            if resp.status != 200:
                return False
            data = json.loads(resp.read() or b"{}")
            return isinstance(data, dict) and "system" in data
    except (OSError, ValueError):
        return False


def check_health(health_file, port: int, max_age: float = 60.0, now: float | None = None) -> bool:
    try:
        beat = json.loads(Path(health_file).read_text(encoding="utf-8"))
        ts = float(beat["ts"])
        state = str(beat.get("state") or "")
    except (OSError, ValueError, KeyError, TypeError):
        return False
    if (time.time() if now is None else now) - ts > max_age:
        return False
    if state == "ready":
        return check(port)
    return True


def main() -> int:
    try:
        port = int(os.environ.get("COMFY_PORT", "8188"))
        max_age = float(os.environ.get("HF_HEALTH_MAX_AGE", "60"))
    except ValueError:
        return 1
    health_file = os.environ.get("HF_HEALTH_FILE", DEFAULT_HEALTH_FILE)
    return 0 if check_health(health_file, port, max_age) else 1


if __name__ == "__main__":
    sys.exit(main())

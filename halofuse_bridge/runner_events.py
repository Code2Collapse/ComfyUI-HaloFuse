"""Parser for the cloud runner's structured event lines (contract C-3, `[hf] {json}` on the job log stream).

Used by lifecycle (hook H-c) for every relayed line. Pure functions, no I/O. Lines that are not valid v1 events
(including ComfyUI output the runner escaped as `[comfyui] [hf] ...`) return None and stay ordinary log lines.
"""
from __future__ import annotations

import json

PREFIX = "[hf] "
PROTOCOL_VERSION = 1
MAX_LINE_BYTES = 65536


def parse(line, nonce: str | None = None) -> dict | None:
    """The decoded event plus a one-line English `text`, or None. With `nonce`, unsigned or foreign events are
    rejected (the runner signs every event with the job's HF_EVENT_NONCE)."""
    if not isinstance(line, str):
        return None
    line = line.rstrip("\r\n")
    if not line.startswith(PREFIX) or len(line.encode("utf-8", "replace")) + 1 > MAX_LINE_BYTES:
        return None
    try:
        obj = json.loads(line[len(PREFIX):])
    except ValueError:
        return None
    if not isinstance(obj, dict) or obj.get("v") != PROTOCOL_VERSION:
        return None
    t, seq, ts = obj.get("t"), obj.get("seq"), obj.get("ts")
    if not isinstance(t, str) or not t:
        return None
    if not isinstance(seq, int) or isinstance(seq, bool):
        return None
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return None
    if nonce is not None and obj.get("n") != nonce:
        return None
    event = dict(obj)
    event["text"] = render(event)
    return event


def _num(value, fmt="{:g}"):
    return fmt.format(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else "?"


def render(ev: dict) -> str:
    """One display line for the console. Never includes preview data."""
    t = ev.get("t")
    if t == "phase":
        name, state = ev.get("phase", "?"), ev.get("state")
        if state == "start":
            return f"Phase {name} started"
        if state == "end":
            return f"Phase {name} finished in {_num(ev.get('dur'), '{:.1f}')} s"
        return f"Phase {name}" + (f" after {_num(ev.get('dur'), '{:.1f}')} s" if ev.get("dur") is not None else "")
    if t == "progress":
        text = f"Node {ev.get('node', '?')}: step {_num(ev.get('value'))}/{_num(ev.get('max'))}"
        if ev.get("eta_s") is not None:
            text += f" (about {_num(round(ev['eta_s']) if isinstance(ev['eta_s'], (int, float)) else None)} s left)"
        return text
    if t == "node":
        state = ev.get("state")
        if state == "cached":
            return f"{len(ev.get('nodes') or [])} node(s) reused from cache"
        label = f"Node {ev.get('node', '?')}" + (f" ({ev['class_type']})" if ev.get("class_type") else "")
        if state == "done":
            return f"{label} finished in {_num(ev.get('dur'), '{:.1f}')} s"
        return f"{label} started"
    if t == "telemetry":
        parts = []
        for gpu in ev.get("gpu") or []:
            parts.append(f"GPU{gpu.get('index', '')} {_num(gpu.get('util_pct'))}% "
                         f"VRAM {_num(gpu.get('mem_used_mb'))}/{_num(gpu.get('mem_total_mb'))} MB")
        ram = ev.get("ram") or {}
        if ram:
            used = (ram.get("total_mb") or 0) - (ram.get("available_mb") or 0)
            parts.append(f"RAM {used}/{_num(ram.get('total_mb'))} MB")
        return "Resources: " + (" · ".join(parts) if parts else "no data")
    if t == "preview":
        return f"Preview ({ev.get('kind', '?')})" + (f" from node {ev['node']}" if ev.get("node") else "")
    if t == "ui":
        keys = ev.get("keys") if isinstance(ev.get("keys"), dict) else {}
        parts = ", ".join(f"{k} {_num(v)}" for k, v in list(keys.items())[:8])
        return (f"Node {ev.get('node', '?')} output" + (" (cached)" if ev.get("cached") is True else "")
                + (f": {parts}" if parts else ""))
    if t == "output":
        return f"Output: {ev.get('path', '?')} ({_num(ev.get('bytes'))} bytes)"
    if t == "error":
        return f"Error [{ev.get('code', 'unknown')}]: {ev.get('message') or 'see details'}"
    if t == "fingerprint":
        gpus = ", ".join(str(g.get("name")) for g in ev.get("gpus") or [] if isinstance(g, dict)) or "no GPU"
        return (f"Environment: ComfyUI {ev.get('comfyui_version') or '?'}, torch {ev.get('torch') or '?'}, {gpus}, "
                f"preset {ev.get('preset') or '?'}")
    if t == "recovery":
        text = f"Recovery {ev.get('action', '?')}"
        if ev.get("attempt") is not None:
            text += f" (attempt {ev['attempt']})"
        text += f" {ev.get('result', '?')}"
        if ev.get("reason"):
            text += f" after {ev['reason']}"
        return text
    if t == "item":
        text = f"Workflow {ev.get('wf', '?')}/{ev.get('of', '?')} {ev.get('state', '?')}"
        return text + (f": {ev['reason']}" if ev.get("reason") else "")
    if t == "final":
        return f"Runner finished: {ev.get('status', '?')} (exit code {ev.get('exit_code', '?')})"
    return f"Runner event {t}"


class SeqTracker:
    """Counts events lost in transport from gaps in the runner's dense `seq` (C-3 §2)."""

    def __init__(self):
        self.last = None
        self.missing = 0

    def feed(self, event) -> int:
        seq = event.get("seq") if isinstance(event, dict) else None
        if not isinstance(seq, int):
            return 0
        gap = 0
        if self.last is not None and seq > self.last + 1:
            gap = seq - self.last - 1
            self.missing += gap
        if self.last is None or seq > self.last:
            self.last = seq
        return gap


def telemetry_to_c4(ev: dict) -> dict:
    """C-3 telemetry payload -> C-4 Telemetry {ts, gpu_util, vram_used_gb, vram_total_gb, ram_used_gb,
    ram_total_gb, cpu_util, disk_free_gb}; missing values are None."""
    def gb(mb):
        return round(mb / 1024, 2) if isinstance(mb, (int, float)) else None

    gpus = [g for g in ev.get("gpu") or [] if isinstance(g, dict)]
    utils = [g["util_pct"] for g in gpus if isinstance(g.get("util_pct"), (int, float))]
    used = [g["mem_used_mb"] for g in gpus if isinstance(g.get("mem_used_mb"), (int, float))]
    total = [g["mem_total_mb"] for g in gpus if isinstance(g.get("mem_total_mb"), (int, float))]
    ram = ev.get("ram") or {}
    ram_total = ram.get("total_mb")
    ram_used = (ram_total - ram["available_mb"]) if isinstance(ram_total, (int, float)) and isinstance(
        ram.get("available_mb"), (int, float)) else None
    disk = ((ev.get("disk") or {}).get("output") or {}).get("free_mb")
    return {"ts": ev.get("ts"), "gpu_util": max(utils) if utils else None,
            "vram_used_gb": gb(sum(used)) if used else None, "vram_total_gb": gb(sum(total)) if total else None,
            "ram_used_gb": gb(ram_used), "ram_total_gb": gb(ram_total), "cpu_util": None, "disk_free_gb": gb(disk)}

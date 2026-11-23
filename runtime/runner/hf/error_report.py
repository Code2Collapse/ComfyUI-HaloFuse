"""The complete error of a failed job, written as files the bridge downloads (L9.338).

The cloud log stream drops and reorders lines, so the bridge must never depend on it for the error. This module
writes two files into the reserved runner folder (/output/_halofuse/, delivered with the outputs even when the job
fails):

- `error.json`: {"schema": 1, "errors": [entry, ...], "final": {...}} - one entry per failure of this job, and at the
  end the job's result (exit code, status, the error code that ended it). An entry can describe a failure that was
  recovered (an out-of-memory retry) or an earlier manifest entry, so readers match `final.error` to an entry's code. An entry holds the node id, class_type and title, the exception type, the COMPLETE ComfyUI
  exception message (with the "Tried to allocate" line and the torch memory summary), the complete traceback, the
  runner's own resource samples (peak and at failure), the last lines ComfyUI printed (the runner's own capture), and
  for a process death the exit code, signal and a short factual diagnosis.
- `comfyui.log`: every line ComfyUI printed, from the runner's own pipe (never from the cloud stream).

`oneline()` is the single-line JSON record printed after "[runner] execution_error: " (one line, so a lossy stream
cannot split it). Writing never raises: a broken disk must not change the job's exit code.
"""
import json
import os
import re
import threading
import time
from pathlib import Path

from . import events as hf_events

SCHEMA = 1
REPORT_NAME = "error.json"
COMFY_LOG_NAME = "comfyui.log"
MAX_ENTRIES = 20          # failures kept per job (a manifest can fail entry after entry)
TAIL_LINES = 200          # ComfyUI lines kept in an entry (the runner's capture holds 200)
ONELINE_MESSAGE_MAX = 2000
ONELINE_FRAMES = 6
RAM_NEAR_LIMIT = 0.90     # a RAM peak at or above this share of the limit counts as "ran out of system memory"

_FILE_RE = re.compile(r'^\s*File "[^"]*", line \d+')


def traceback_text(tb) -> str:
    """ComfyUI sends the traceback as a list of formatted chunks (traceback.format_tb); join them unchanged."""
    if tb is None:
        return ""
    if isinstance(tb, (list, tuple)):
        return "".join(str(x) for x in tb)
    return str(tb)


def last_frames(tb, n=ONELINE_FRAMES) -> list:
    """The last n frames of a traceback, each "File ..., line N, in f" plus its code line when there is one."""
    lines = traceback_text(tb).splitlines()
    frames = []
    for i, line in enumerate(lines):
        if _FILE_RE.match(line):
            frame = line.strip()
            nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
            if nxt and not _FILE_RE.match(lines[i + 1]) and set(nxt) - set("^~ "):
                frame += " | " + nxt
            frames.append(frame)
    return frames[-n:]


def oneline(data, classification=None) -> str:
    """One-line JSON record of a ComfyUI execution_error (node, class, exception, message, last frames).

    Default JSON separators keep `"node_type": "..."` spelled as older bridges search for it."""
    data = data if isinstance(data, dict) else {}
    message = str(data.get("exception_message") or "")
    if len(message) > ONELINE_MESSAGE_MAX:
        message = message[:ONELINE_MESSAGE_MAX] + f"…[{len(message) - ONELINE_MESSAGE_MAX} more characters in " \
                                                  f"_halofuse/{REPORT_NAME}]"
    record = {"node_id": data.get("node_id"), "node_type": data.get("node_type"),
              "exception_type": data.get("exception_type"), "exception_message": message,
              "frames": last_frames(data.get("traceback"))}
    code = (classification or {}).get("code")
    if code:
        record["code"] = code
    record["full_report"] = f"_halofuse/{REPORT_NAME}"
    return json.dumps(record, ensure_ascii=True)


def node_title(workflow, node_id):
    """The node's title from the API-format workflow (`_meta.title`), or None."""
    if not isinstance(workflow, dict) or node_id is None:
        return None
    node = workflow.get(str(node_id))
    meta = node.get("_meta") if isinstance(node, dict) else None
    title = meta.get("title") if isinstance(meta, dict) else None
    return title if isinstance(title, str) and title.strip() else None


def resource_facts(sample=None, peaks=None) -> dict:
    """RAM / VRAM at failure and at peak, in MB, from the runner's own telemetry sampler. Missing sources are left
    out, never estimated."""
    sample = sample if isinstance(sample, dict) else {}
    peaks = peaks if isinstance(peaks, dict) else {}
    out = {}
    ram = sample.get("ram") or {}
    cg = sample.get("cgroup") or {}
    if "total_mb" in ram and "available_mb" in ram:
        out["ram_used_mb"] = ram["total_mb"] - ram["available_mb"]
        out["ram_total_mb"] = ram["total_mb"]
    if cg.get("current_mb") is not None:
        out["container_ram_used_mb"] = cg["current_mb"]
    if cg.get("max_mb") is not None:
        out["container_ram_limit_mb"] = cg["max_mb"]
    limit = cg.get("max_mb") if cg.get("max_mb") is not None else ram.get("total_mb")
    if limit is not None:
        out["ram_limit_mb"] = limit
    peak_candidates = [v for v in (peaks.get("cgroup_peak_mb"), peaks.get("ram_used_mb")) if v is not None]
    if peak_candidates:
        out["ram_peak_mb"] = max(peak_candidates)
    gpus = sample.get("gpu") or []
    if gpus:
        gpu = gpus[0]
        if gpu.get("mem_used_mb") is not None:
            out["vram_used_mb"] = gpu["mem_used_mb"]
        if gpu.get("mem_total_mb") is not None:
            out["vram_total_mb"] = gpu["mem_total_mb"]
        if gpu.get("name"):
            out["gpu_name"] = gpu["name"]
    if peaks.get("gpu_mem_used_mb") is not None:
        out["vram_peak_mb"] = peaks["gpu_mem_used_mb"]
    if cg.get("oom_kill") is not None:
        out["oom_kill_count"] = cg["oom_kill"]
    return out


def _gb(mb):
    return f"{mb / 1024:.1f}"


def ram_near_limit(facts) -> bool:
    peak, limit = facts.get("ram_peak_mb"), facts.get("ram_limit_mb")
    return bool(peak and limit and peak >= RAM_NEAR_LIMIT * limit)


def death_diagnosis(classification, facts, returncode=None) -> str:
    """Short factual diagnosis of a ComfyUI process death (the bridge owns the artist-facing wording)."""
    cls = classification or {}
    code = cls.get("code")
    sig = cls.get("signal")
    how = f"code {returncode}" + (f", {sig}" if sig else "") if returncode is not None else (sig or "no exit code")
    ram = ""
    if facts.get("ram_peak_mb") and facts.get("ram_limit_mb"):
        ram = f"RAM peak {_gb(facts['ram_peak_mb'])} of {_gb(facts['ram_limit_mb'])} GB"
    if code == "host_ram_exhausted" or (code == "process_killed" and ram_near_limit(facts)):
        return (f"The machine ran out of system memory ({ram or 'the kernel out-of-memory killer fired'}); "
                f"the system stopped ComfyUI ({how}).")
    if code == "process_killed":
        return (f"ComfyUI was stopped by the system ({how}) without an error message"
                + (f"; {ram}, so running out of memory is not confirmed." if ram else "."))
    if code == "native_crash":
        return f"ComfyUI crashed in native code ({how}); a CUDA, driver or compiled-extension fault is the usual cause."
    if code == "startup_failed":
        return f"ComfyUI exited during start-up ({how})."
    return f"ComfyUI stopped unexpectedly ({how})."


def build_entry(kind, classification, *, data=None, workflow=None, tail=(), sample=None, peaks=None,
                returncode=None, prompt_id=None, wf=None, of=None, clock=time.time) -> dict:
    """One error.json entry. `data` is ComfyUI's execution_error message data when there is one."""
    cls = dict(classification or {})
    data = data if isinstance(data, dict) else {}
    node = data.get("node_id", cls.get("node"))
    node = None if node is None else str(node)
    class_type = data.get("node_type") or cls.get("class_type")
    facts = resource_facts(sample, peaks)
    entry = {
        "kind": kind, "code": cls.get("code"), "message": cls.get("message"), "time": round(float(clock()), 3),
        "node_id": node, "class_type": class_type, "node_title": node_title(workflow, node),
        "exception_type": data.get("exception_type") or cls.get("exception_type"),
        "exception_message": data.get("exception_message"),
        "traceback": traceback_text(data.get("traceback")) or None,
        "detail": cls.get("detail"),
        "resources": facts,
        "comfy_tail": [hf_events.strip_ansi(str(x)) for x in list(tail or ())[-TAIL_LINES:]],
    }
    for key, value in (("returncode", returncode), ("signal", cls.get("signal")), ("cause", cls.get("cause")),
                       ("prompt_id", prompt_id), ("wf", wf), ("of", of)):
        if value is not None:
            entry[key] = value
    if kind in ("process_died", "unresponsive", "startup_failed"):
        entry["diagnosis"] = death_diagnosis(cls, facts, returncode)
    return {k: v for k, v in entry.items() if v is not None}


class ErrorReport:
    """error.json in `directory`, rewritten atomically after every failure."""

    def __init__(self, directory):
        self.path = Path(directory) / REPORT_NAME
        self._lock = threading.Lock()
        self._entries = []
        self._final = None

    def record(self, entry) -> bool:
        with self._lock:
            self._entries.append(entry)
            self._entries = self._entries[-MAX_ENTRIES:]
            return self._write()

    def finish(self, final) -> bool:
        """The job's result, so a reader can tell which entry ended the job (or that none did, e.g. no output)."""
        with self._lock:
            self._final = dict(final)
            return self._write()

    def _write(self) -> bool:
        """Rewrite the file atomically; the caller holds the lock."""
        doc = {"schema": SCHEMA, "errors": list(self._entries)}
        if self._final is not None:
            doc["final"] = self._final
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, ensure_ascii=True, indent=1, default=str)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
            return True
        except (OSError, ValueError, TypeError):
            return False


class ComfyLog:
    """comfyui.log: every ComfyUI line from the runner's own pipe, colour codes removed. Opened on the first line."""

    def __init__(self, directory):
        self.path = Path(directory) / COMFY_LOG_NAME
        self._lock = threading.Lock()
        self._fh = None
        self._broken = False
        self._closed = False

    def write(self, text) -> None:
        with self._lock:
            if self._broken or self._closed:  # a line after the job finished is not written to a closed log
                return
            try:
                if self._fh is None:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    self._fh = open(self.path, "a", encoding="utf-8", buffering=1)
                self._fh.write(hf_events.strip_ansi(str(text)) + "\n")
            except (OSError, ValueError):
                self._broken = True

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._fh is None:
                return
            try:
                self._fh.flush()
                os.fsync(self._fh.fileno())
                self._fh.close()
            except (OSError, ValueError):
                pass
            self._fh = None

"""L9.338: the full error of a failed job, in plain English, never dependent on the lossy cloud log stream.

Sources, in order of trust:
1. ``runtime_file``: the runner's complete report ``_halofuse/error.json``, delivered with the outputs (also for
   failed jobs) to ``<job_dir>/logs/runner/error.json``.
2. ``runner_log``: runtime images without that file still deliver the runner's complete ``render.log``; the error
   is rebuilt from it (nothing is lost there, only the live stream drops lines).
3. ``log_stream``: only what arrived over the cloud log stream. Lines may be missing; the note says so honestly.

``attach(job_id, store)`` builds the explanation (headline, explanation, next steps, facts, complete technical text)
and stores it on the job record as ``error_report`` (the console's Problems tab and the panel read it), replaces the
generic "The cloud render failed (exit code 1)." message with the headline, and adds one ``error`` log event.
"""
from __future__ import annotations

import collections
import json
import logging
import re
from pathlib import Path

from .diagnose import describe
from .job_store import MAX_READ_LIMIT, redact_obj
from .provider.redact import sanitize_line

_log = logging.getLogger(__name__)

REPORT_REL = "logs/runner/error.json"
RUNNER_LOG_REL = "logs/render.log"
RUNNER_LOG_KEY = "_halofuse/render.log"       # delivery manifest key of the runner's own (complete) render.log
MANIFEST_REL = ".halofuse/manifest.json"
STREAM_EVENTS = MAX_READ_LIMIT                 # the last events of the job log (one read page)
MAX_TECHNICAL = 400_000                        # characters (job record + 1 s detail poll); the file keeps the rest
_SIZE = r"([\d.]+\s*[KMGT]i?B)"
_MEMORY_PATTERNS = (
    ("tried_to_allocate", re.compile(r"Tried to allocate " + _SIZE)),
    ("gpu_total", re.compile(r"total capacity of " + _SIZE)),
    ("gpu_free", re.compile(r"of which " + _SIZE + r" is free")),
    ("allocated_by_pytorch", re.compile(_SIZE + r" is allocated by PyTorch")),
    ("currently_allocated", re.compile(r"Currently allocated\s*:\s*" + _SIZE)),
    ("device_limit", re.compile(r"Device limit\s*:\s*" + _SIZE)),
)
_GPU_OOM = ("outofmemoryerror", "cuda out of memory", "allocation on device", "hip out of memory",
            "cudnn_status_alloc_failed", "cublas_status_alloc_failed")
_HOST_OOM = ("memoryerror", "defaultcpuallocator: can't allocate memory", "cannot allocate memory")
_EXEC_MARK = "[runner] execution_error:"
_EXC_START = "!!! Exception during processing !!!"
_END_MARKS = ("Prompt executed in", "[runner] execution_error", "[runner] ERROR:")
_DIED_RE = re.compile(r"ComfyUI (?:died mid-run|exited during startup) \(code (-?\d+)\)")
_SIGNALS = {9: "SIGKILL", 11: "SIGSEGV", 6: "SIGABRT", 7: "SIGBUS", 4: "SIGILL", 8: "SIGFPE", 15: "SIGTERM"}
_MEMORY_DEATHS = ("host_ram_exhausted", "process_killed")


# ---- reading the sources -------------------------------------------------------------------------------------------

def load_runtime_report(job_dir: Path) -> dict | None:
    """The entry of the runner's error.json that ENDED the job, or None (old runtime image, nothing delivered).

    `final.error` names the error code that ended the job; the last entry with that code is the one. An earlier entry
    (a recovered out-of-memory retry, another manifest entry) is never presented as the cause: when no entry matches
    (e.g. the job ended with no output), the result is described from `final` alone."""
    try:
        doc = json.loads((Path(job_dir) / REPORT_REL).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    errors = doc.get("errors") if isinstance(doc, dict) else None
    if not isinstance(errors, list):
        return None
    errors = [e for e in errors if isinstance(e, dict)]
    final = doc.get("final") if isinstance(doc.get("final"), dict) else None
    wanted = (final or {}).get("error")
    if wanted:
        match = [e for e in errors if e.get("code") == wanted]
        if match:
            return dict(match[-1], failures=len(errors))
        return {"kind": "job_failed", "code": wanted, "detail": final.get("detail"), "failures": len(errors),
                "final_only": True}
    return dict(errors[-1], failures=len(errors)) if errors else None


def runner_log_lines(job_dir: Path) -> list[str] | None:
    """Lines of the runner's own complete render.log when delivery fetched it (not the bridge's streamed copy)."""
    job_dir = Path(job_dir)
    try:
        manifest = json.loads((job_dir / MANIFEST_REL).read_text(encoding="utf-8"))
        entry = (manifest.get("files") or {}).get(RUNNER_LOG_KEY) or {}
        rel = entry.get("local") or RUNNER_LOG_REL
        if not entry:
            return None
        return (job_dir / rel).read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, ValueError, AttributeError):
        return None


def _parse_hf(line: str) -> dict | None:
    if not line.startswith("[hf] "):
        return None
    try:
        obj = json.loads(line[5:])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _json_after_marker(lines: list[str], start: int) -> tuple[dict | None, int]:
    """(ComfyUI's execution_error data, lines it used after the marker): one line (new runner) or the multi-line block
    an old runner printed."""
    head = lines[start][len(_EXEC_MARK):].strip() if lines[start].startswith(_EXEC_MARK) else ""
    if head:
        try:
            obj = json.loads(head)
            return (obj if isinstance(obj, dict) else None), 0
        except ValueError:
            return None, 0
    buf = []
    for line in lines[start + 1:start + 20000]:
        buf.append(line)
        if line.startswith("}"):
            try:
                obj = json.loads("\n".join(buf))
                return (obj if isinstance(obj, dict) else None), len(buf)
            except ValueError:
                return None, 0
    return None, 0


def rebuild_from_lines(lines: list[str], events: list[dict] | None = None) -> dict | None:
    """An error.json-like entry rebuilt from log text (runner render.log or streamed lines) + runner events.

    `events` are C-3 runner events (parsed `[hf]` lines or stored entries' data) for node / error / death facts."""
    lines = [str(x) for x in lines or []]
    events = list(events or [])
    for line in lines:
        ev = _parse_hf(line)
        if ev is not None:
            events.append(ev)
    entry: dict = {}
    used = range(0)
    marker = max((i for i, l in enumerate(lines) if l.startswith(_EXEC_MARK)), default=None)
    if marker is not None:
        data, n_used = _json_after_marker(lines, marker)
        used = range(marker + 1, marker + 1 + n_used)
        if data:
            entry.update(kind="execution_error", node_id=None if data.get("node_id") is None else str(data["node_id"]),
                         class_type=data.get("node_type") or data.get("class_type"),
                         exception_type=data.get("exception_type"), exception_message=data.get("exception_message"))
            tb = data.get("traceback")
            if isinstance(tb, list):
                entry["traceback"] = "".join(str(x) for x in tb)
            elif data.get("frames"):
                entry["traceback"] = "\n".join(str(x) for x in data["frames"])
            if data.get("code"):
                entry["code"] = data["code"]
    # the exception block ComfyUI printed (message + traceback): complete in render.log, partial in a stream
    start = max((i for i, l in enumerate(lines) if _EXC_START in l), default=None)
    if start is not None:
        block = []
        for line in lines[start:]:
            if block and any(m in line for m in _END_MARKS):
                break
            if line.startswith("[hf] "):
                continue
            block.append(line)
        entry["printed_block"] = "\n".join(block)
        entry.setdefault("kind", "execution_error")
    errors = [e for e in events if e.get("t") == "error" and e.get("code")]
    if errors:
        last = errors[-1]
        entry.setdefault("code", last.get("code"))
        if entry.get("node_id") is None and last.get("node") is not None:
            entry["node_id"] = str(last["node"])
        entry.setdefault("class_type", last.get("class_type"))
        entry.setdefault("exception_type", last.get("exception_type"))
        entry.setdefault("detail", last.get("detail"))
        for key in ("returncode", "signal"):
            if last.get(key) is not None:
                entry.setdefault(key, last[key])
    died = [m for m in (_DIED_RE.search(l) for l in lines) if m]
    if died:
        rc = int(died[-1].group(1))
        entry.setdefault("kind", "process_died")
        entry.setdefault("returncode", rc)
        sig = _SIGNALS.get(-rc) if rc < 0 else _SIGNALS.get(rc - 128) if 128 < rc < 160 else None
        if sig:
            entry.setdefault("signal", sig)
        if not entry.get("code"):
            entry["code"] = ("process_killed" if sig == "SIGKILL" else
                             "native_crash" if sig in ("SIGSEGV", "SIGBUS", "SIGILL", "SIGABRT", "SIGFPE")
                             else "process_exit")
    if entry.get("node_id") is None:
        started = [e for e in events if e.get("t") == "node" and e.get("state") in ("start", "started", "running")]
        if started:
            entry["node_id"] = None if started[-1].get("node") is None else str(started[-1]["node"])
            entry.setdefault("class_type", started[-1].get("class_type"))
            entry["node_guessed"] = True
    telemetry = [e for e in events if e.get("t") == "telemetry"]
    final = [e for e in events if e.get("t") == "final"]
    facts = {}
    if telemetry:
        ram = telemetry[-1].get("ram") or {}
        if "total_mb" in ram and "available_mb" in ram:
            facts["ram_used_mb"] = ram["total_mb"] - ram["available_mb"]
            facts["ram_limit_mb"] = ram["total_mb"]
        gpus = telemetry[-1].get("gpu") or []
        if gpus:
            facts.update({k: v for k, v in (("vram_used_mb", gpus[0].get("mem_used_mb")),
                                            ("vram_total_mb", gpus[0].get("mem_total_mb")),
                                            ("gpu_name", gpus[0].get("name"))) if v is not None})
    peak = (final[-1].get("peak") or {}) if final else {}
    peak_ram = [v for v in (peak.get("cgroup_peak_mb"), peak.get("ram_used_mb")) if v is not None]
    if peak_ram:
        facts["ram_peak_mb"] = max(peak_ram)
    elif telemetry:
        ram_used = [(t.get("ram") or {}).get("total_mb", 0) - (t.get("ram") or {}).get("available_mb", 0)
                    for t in telemetry if (t.get("ram") or {}).get("total_mb")]
        if ram_used:
            facts["ram_peak_mb"] = max(ram_used)
    if peak.get("gpu_mem_used_mb") is not None:
        facts["vram_peak_mb"] = peak["gpu_mem_used_mb"]
    if facts:
        entry["resources"] = facts
    if final and not entry.get("code") and final[-1].get("error"):
        entry["code"] = final[-1]["error"]
    entry["comfy_tail"] = [l for i, l in enumerate(lines)
                           if i not in used and not l.startswith(("[hf] ", "[runner] "))][-200:]
    if not entry.get("code") and not entry.get("printed_block") and marker is None:
        return None
    return entry


def _classify(entry: dict) -> str:
    code = entry.get("code")
    hay = " ".join(str(entry.get(k) or "") for k in ("exception_type", "exception_message", "printed_block")).lower()
    if code in (None, "", "execution_error", "unknown"):
        if any(p in hay for p in _GPU_OOM):
            code = "vae_decode_oom" if str(entry.get("class_type") or "").startswith("VAEDecode") else "vram_oom"
        elif any(p in hay for p in _HOST_OOM):
            code = "host_ram_oom"
        else:
            code = code or "execution_error"
    return code


# ---- explanation -------------------------------------------------------------------------------------------------

def memory_numbers(text: str) -> dict:
    out = {}
    for key, pattern in _MEMORY_PATTERNS:
        m = pattern.search(text or "")
        if m:
            out[key] = re.sub(r"\s+", " ", m.group(1))
    return out


def _gb(mb) -> str:
    return f"{mb / 1024:.1f} GB"


def _who(entry: dict) -> str | None:
    node, cls, title = entry.get("node_id"), entry.get("class_type"), entry.get("node_title")
    if node is None and not cls:
        return None
    label = f"Node {node}" if node is not None else "A node"
    if cls:
        label += f" ({cls})"
    if title and title != cls:
        label += f' "{title}"'
    return label


def _first_line(text) -> str:
    for line in str(text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _technical(entry: dict, headline: str) -> str:
    parts = [headline, ""]
    who = _who(entry)
    if who:
        parts.append(f"Node: {who}" + (" (last node that started; the log did not name it)" if entry.get("node_guessed")
                                       else ""))
    if entry.get("code"):
        parts.append(f"Error code: {entry['code']}")
    if entry.get("exception_type"):
        parts.append(f"Exception type: {entry['exception_type']}")
    if entry.get("returncode") is not None:
        parts.append(f"ComfyUI exit code: {entry['returncode']}" + (f" ({entry['signal']})" if entry.get("signal") else ""))
    if entry.get("diagnosis"):
        parts.append(f"Runner diagnosis: {entry['diagnosis']}")
    res = entry.get("resources") or {}
    if res:
        parts.append("Resources: " + ", ".join(f"{k}={v}" for k, v in sorted(res.items())))
    if entry.get("exception_message"):
        parts += ["", "--- Error message ---", str(entry["exception_message"]).rstrip()]
    if entry.get("traceback"):
        parts += ["", "--- Traceback ---", str(entry["traceback"]).rstrip()]
    if entry.get("printed_block") and not entry.get("traceback"):
        parts += ["", "--- What ComfyUI printed ---", str(entry["printed_block"]).rstrip()]
    if entry.get("detail") and not entry.get("traceback") and not entry.get("printed_block"):
        parts += ["", "--- Detail ---", str(entry["detail"]).rstrip()]
    tail = entry.get("comfy_tail") or []
    if tail:
        parts += ["", f"--- Last {len(tail)} lines ComfyUI printed ---", *[str(x) for x in tail]]
    text = "\n".join(parts).rstrip() + "\n"
    if len(text) > MAX_TECHNICAL:
        text = text[:MAX_TECHNICAL] + f"\n[{len(text) - MAX_TECHNICAL} more characters in {REPORT_REL}]\n"
    return text


_PACK_RE = re.compile(r"custom_nodes[\\/]+([^\\/\"']+)[\\/]")


def culprit_pack(entry: dict) -> str | None:
    """The node pack whose code raised the error: the DEEPEST custom_nodes/<pack>/ frame of the traceback
    (owner 2026-11-24: a pack patching core made the KSampler fail, and the advice said to check the KSampler)."""
    tb = entry.get("traceback")
    text = "".join(tb) if isinstance(tb, list) else str(tb or "")
    text += "\n" + str(entry.get("printed_block") or "")
    packs = [m.group(1) for m in _PACK_RE.finditer(text)]
    packs = [p for p in packs if not p.startswith("halofuse_") and not p.endswith(".py")]
    return packs[-1] if packs else None


def explain(entry: dict, source: str = "runtime_file", dropped_lines: int | None = None) -> dict:
    """Plain-English headline, explanation, next steps and the complete technical text for one error entry."""
    entry = dict(entry or {})
    code = _classify(entry)
    entry["code"] = code
    who = _who(entry)
    res = entry.get("resources") or {}
    message = str(entry.get("exception_message") or "")
    mem = memory_numbers("\n".join([message, str(entry.get("printed_block") or ""), str(entry.get("detail") or "")]))
    facts: list[dict] = []

    def fact(label, value):
        if value not in (None, ""):
            facts.append({"label": label, "value": str(value)})

    if code in ("vram_oom", "vae_decode_oom"):
        headline = f"{who or 'A node'} ran out of GPU memory"
        bits = []
        if mem.get("tried_to_allocate"):
            bits.append(f"it asked for {mem['tried_to_allocate']} more")
        in_use = mem.get("currently_allocated") or mem.get("allocated_by_pytorch")
        limit = mem.get("device_limit") or mem.get("gpu_total")
        if in_use:
            bits.append(f"{in_use} was already in use" + (f" of the {limit} limit" if limit else ""))
        elif limit:
            bits.append(f"the GPU limit is {limit}")
        explanation = ("The GPU did not have enough free memory for this step" + (": " + "; ".join(bits) if bits else "")
                       + ".")
        gpu = res.get("gpu_name")
        total = res.get("vram_total_mb")
        bigger = "Choose a GPU with more memory" + (f" (this job ran on {gpu}" + (f", {_gb(total)})" if total else ")")
                                                     if gpu else "") + "."
        tiled = "Decode with VAE Decode (Tiled) instead of VAE Decode (smaller tiles use less memory)."
        steps = [bigger, "Lower the resolution or the number of frames for this render.", tiled,
                 "For video, render fewer frames per window (a shorter context window or chunk length).",
                 "Or use the Max Memory preset, which offloads more and retries after an out-of-memory error."]
        if code == "vae_decode_oom":
            steps = [tiled] + [s for s in steps if s != tiled]
        fact("Asked for", mem.get("tried_to_allocate"))
        fact("GPU memory in use", in_use)
        fact("GPU memory limit", limit)
        fact("Free on the GPU", mem.get("gpu_free"))
    elif code in _MEMORY_DEATHS and (code == "host_ram_exhausted" or _ram_near_limit(res)):
        headline = "The cloud machine ran out of system memory" + (f" while {who} was running" if who else "")
        ram = (f"RAM peaked at {_gb(res['ram_peak_mb'])} of {_gb(res['ram_limit_mb'])}"
               if res.get("ram_peak_mb") and res.get("ram_limit_mb") else "System memory (RAM) was full")
        explanation = (f"{ram}, so the system stopped ComfyUI"
                       + (f" (code {entry['returncode']}{', ' + entry['signal'] if entry.get('signal') else ''})"
                          if entry.get("returncode") is not None else "") + ". This is system RAM, not GPU memory.")
        steps = ["Choose a machine type with more system RAM.",
                 "Or use the Max Memory preset (no pinned memory, more offloading to disk).",
                 "Lower the frame count or resolution, or split a long video into shorter windows.",
                 "Load fewer large models at once (unload or reuse models between steps)."]
    elif code == "process_killed":
        headline = "ComfyUI was stopped by the system" + (f" while {who} was running" if who else "")
        ram = (f" RAM peaked at {_gb(res['ram_peak_mb'])} of {_gb(res['ram_limit_mb'])}, so running out of memory is "
               f"possible but not confirmed." if res.get("ram_peak_mb") and res.get("ram_limit_mb") else
               " Running out of system memory is the most likely cause, but it was not confirmed.")
        explanation = ("ComfyUI was killed without an error message"
                       + (f" (code {entry['returncode']})" if entry.get("returncode") is not None else "") + "." + ram)
        steps = ["Choose a machine type with more system RAM.", "Or use the Max Memory preset.",
                 "Lower the frame count or resolution, then render again."]
    elif code == "native_crash":
        headline = "ComfyUI crashed" + (f" while {who} was running" if who else "")
        explanation = ("ComfyUI crashed in native code"
                       + (f" ({entry.get('signal') or 'code ' + str(entry.get('returncode'))})"
                          if entry.get("returncode") is not None or entry.get("signal") else "")
                       + ". A CUDA, driver or compiled-extension fault in a node is the usual cause.")
        steps = [f"Check {who} first: it was running when ComfyUI crashed." if who else
                 "Check the last node that started in the log: it was running when ComfyUI crashed.",
                 "Render again with the Safe preset.",
                 "If it repeats, copy the error below and send it with the HaloFuse log to support."]
    elif code == "host_ram_oom":
        headline = f"{who or 'A node'} could not get enough system memory"
        explanation = "The node asked for more system RAM than was free. " + (_first_line(message) or "")
        steps = ["Choose a machine type with more system RAM.", "Or use the Max Memory preset.",
                 "Lower the frame count or resolution, then render again."]
    elif code == "execution_error":
        first = _first_line(message) or _first_line(entry.get("detail")) or _first_line(entry.get("printed_block"))
        exc = entry.get("exception_type")
        reason = f"{exc}: {first}" if exc and first and not first.startswith(str(exc)) else (first or exc or "")
        pack = culprit_pack(entry)
        headline = f"{who or 'A node'} failed" + (f" inside the node pack {pack}" if pack else "") + (f": {reason}" if reason else "")
        explanation = "A node raised an error while the workflow was running. The exact Python error is below."
        steps = [f"Open {who} on the canvas and check its inputs, connected models and settings." if who else
                 "Find the failing node with the technical detail below and check its inputs.",
                 "Read the error message and the last lines of the traceback below; they name the cause.",
                 "Fix it and render again."]
        if pack:
            explanation += (f" The error was raised inside the node pack {pack} (the last pack in the traceback), "
                            f"not by {who or 'the node'}'s own settings.")
            steps = [f"The cause is the node pack {pack}: update it, or bypass its node in the workflow, then render "
                     "again."] + steps[1:]
            fact("Node pack", pack)
    else:
        d = describe(code)
        headline = d.title + (f" ({who})" if who else "")
        explanation = d.cause
        steps = [d.next_action]
        if who:
            steps.insert(0, f"Check {who}: it is named in the error.")
    fact("Error type", entry.get("exception_type"))
    if entry.get("returncode") is not None:
        fact("ComfyUI exit code", f"{entry['returncode']}" + (f" ({entry['signal']})" if entry.get("signal") else ""))
    if res.get("ram_peak_mb"):
        fact("System RAM peak", _gb(res["ram_peak_mb"]) + (f" of {_gb(res['ram_limit_mb'])}" if res.get("ram_limit_mb")
                                                           else ""))
    if res.get("vram_peak_mb"):
        fact("GPU memory peak (sampled)", _gb(res["vram_peak_mb"]) + (f" of {_gb(res['vram_total_mb'])}"
                                                                     if res.get("vram_total_mb") else ""))
    if res.get("gpu_name"):
        fact("GPU", res["gpu_name"])
    if entry.get("failures", 1) > 1:
        fact("Failures in this job", entry["failures"])
    if entry.get("diagnosis") and code not in ("vram_oom", "vae_decode_oom", "execution_error"):
        explanation += f" Runner: {entry['diagnosis']}"
    if source == "runtime_file":
        note = None
    elif source == "runner_log":
        note = ("Rebuilt from the runner's complete log file; this runtime image does not write the error report yet."
                + (f" The live cloud stream dropped {dropped_lines} line(s), but nothing is missing here."
                   if dropped_lines else ""))
    else:
        note = ((f"The cloud dropped {dropped_lines} line(s) of the live log, so this error can be incomplete. "
                 if dropped_lines else "The cloud log stream can drop lines; this is what arrived. ")
                + "Full detail needs the new runtime image (it writes the complete error report).")
    if entry.get("node_guessed"):
        steps.append("The log did not name the failing node; the node shown is the last one that started.")
    technical = _technical(entry, headline)
    return {"code": code, "headline": headline, "explanation": explanation.strip(), "next_steps": steps,
            "node_id": entry.get("node_id"), "class_type": entry.get("class_type"),
            "node_title": entry.get("node_title"), "facts": facts, "note": note, "source": source,
            "dropped_lines": dropped_lines, "technical": technical, "technical_lines": technical.count("\n"),
            "report_path": REPORT_REL if source == "runtime_file" else
            (RUNNER_LOG_REL if source == "runner_log" else None)}


def _ram_near_limit(res: dict) -> bool:
    peak, limit = res.get("ram_peak_mb"), res.get("ram_limit_mb")
    return bool(peak and limit and peak >= 0.9 * limit)


# ---- building + attaching ------------------------------------------------------------------------------------------

def _stream(store, job_id: str) -> tuple[list[str], list[dict], int]:
    """(stream line texts, runner events, lines the cloud reported dropped + runner event seq gaps)."""
    total = store.total_events(job_id)
    entries = store.read_events(job_id, since=max(0, total - STREAM_EVENTS), limit=STREAM_EVENTS)["entries"]
    lines, events, dropped, seqs = [], [], 0, []
    for e in entries:
        kind = e.get("kind")
        data = e.get("data") if isinstance(e.get("data"), dict) else {}
        if kind == "line":
            lines.append(str(e.get("text") or ""))
        elif kind == "truncated":
            dropped += int(data.get("dropped") or 0)
        elif data.get("t") == kind:
            events.append(data)
            if isinstance(data.get("seq"), int):
                seqs.append(data["seq"])
    seqs.sort()
    dropped += sum(b - a - 1 for a, b in zip(seqs, seqs[1:]) if b > a + 1)
    return lines, events, dropped


def _missing(full: list[str], arrived: list[str], window_only: bool = False) -> int:
    """Lines of the runner's complete log that never arrived over the stream. With `window_only` (the job log held
    more events than one read page) only the part of the file from the first line we read is compared. Both sides
    are normalised the way the job log stores a line (no colour codes, one line, redacted)."""
    full = [sanitize_line(x) for x in full]
    if window_only and arrived:
        try:
            full = full[full.index(arrived[0]):]
        except ValueError:
            return 0  # cannot line the two up: say nothing rather than a wrong number
    have = collections.Counter(arrived)
    missing = 0
    for line in full:
        if line.startswith("[hf] "):
            continue
        if have[line] > 0:
            have[line] -= 1
        else:
            missing += 1
    return missing


def build(job_id: str, store) -> dict | None:
    job_dir = store.job_dir(job_id)
    window_only = store.total_events(job_id) > STREAM_EVENTS
    lines, events, dropped = _stream(store, job_id)
    entry = load_runtime_report(job_dir)
    if entry is not None:
        return explain(entry, "runtime_file", None)
    full = runner_log_lines(job_dir)
    if full:
        entry = rebuild_from_lines(full)
        if entry is not None:
            return explain(entry, "runner_log", _missing(full, lines, window_only) or None)
    entry = rebuild_from_lines(lines, events)
    if entry is None:
        return None
    return explain(entry, "log_stream", dropped or None)


def attach(job_id: str, store) -> dict | None:
    """Build the report for a failed job and put it on the record, the error message and the log. Never raises."""
    try:
        return _attach(job_id, store)
    except Exception:  # noqa: BLE001 - the explanation must never break delivery
        _log.debug("HaloFuse error report failed for %s", job_id, exc_info=True)
        return None


def _attach(job_id: str, store) -> dict | None:
    report = build(job_id, store)
    if report is None:
        return None
    rec = store.get(job_id) or {}
    before = rec.get("error_report") if isinstance(rec.get("error_report"), dict) else {}
    # the job record keeps one line per string (JobStore flattens line breaks while redacting): the technical text
    # is stored as its list of lines and joined again by from_record()
    stored = dict(report, technical=report["technical"].split("\n"))
    same = before.get("headline") == report["headline"] and before.get("technical") == redact_obj(stored["technical"])
    diagnosis = dict(rec.get("diagnosis") or {})
    diagnosis.update(code=report["code"], title=report["headline"], cause=report["explanation"],
                     next_action=report["next_steps"][0] if report["next_steps"] else None)
    fields = {"error_report": stored, "diagnosis": diagnosis,
              "error": {"code": report["code"], "message": report["headline"],
                        "detail_ref": report["report_path"] or "logs/error.log"}}
    store.mutate(job_id, lambda r: r.update(fields))  # replace, never deep-merge an older report
    if not same:
        store.append_event(job_id, "error", source="bridge", level="error",
                           text=f"{report['headline']}. {report['next_steps'][0] if report['next_steps'] else ''}"
                           .strip(),
                           data={"code": report["code"], "message": report["headline"], "node": report["node_id"],
                                 "class_type": report["class_type"], "detail": report["explanation"],
                                 "source": "error_report", "report_source": report["source"]})
    return report


def from_record(report) -> dict | None:
    """The stored report as the console reads it: the technical text joined back into one string."""
    if not isinstance(report, dict):
        return None
    out = dict(report)
    if isinstance(out.get("technical"), list):
        out["technical"] = "\n".join(str(x) for x in out["technical"])
    return out

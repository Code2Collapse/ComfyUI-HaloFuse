#!/usr/bin/env python3
"""Image smoke test: boot the REAL runner (through tini in the image) on CPU, offline, with a model-free manifest.

  smoke_run.py --runner /runner/halofuse_run.py --comfyui-dir DIR --install-report FILE --out REPORT.json
                [--tini /usr/local/bin/tini] [--python python3.13] [--runner-arg X ...] [--work DIR]
                [--timeout 600] [--compare-args "--flag ..."]

The manifest has three entries: EmptyImage -> SaveImage, the same with an unknown node class, and EmptyImage ->
SaveImage again. A healthy image therefore ends with runner exit 1, final status "partial", items
succeeded / failed (missing_node) / succeeded and two output files: that proves boot, every installed custom-node
pack's import, submission, continue-on-failure, output collection, shutdown and the exit code path. The runner gets
HF_ALLOW_CPU=1 (it adds --cpu only when no NVIDIA GPU is visible).

Import policy: the install report lists the packs that were installed. A pack ComfyUI reports as "(IMPORT FAILED)"
or never lists is a problem in a strict profile (the build fails) and is reported only in other profiles.

--compare-args repeats the whole run with COMFY_EXTRA_ARGS set (for example --disable-partner-nodes) and records its
timings next to the main run; it is measured, not judged. Exit 0 when every check passed, else 1. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCHEMA = "halofuse.smoke/1"
IMPORT_LINE = re.compile(r"([\d.]+) seconds( \(IMPORT FAILED\))?: (.+?)\s*$")
# ComfyUI 0.43 colours its log lines even into a pipe (ESC[32m[INFO]ESC[0m ...): strip CSI / 2-byte ESC sequences
# before matching anything in ComfyUI output.
ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|[@-Z\\-_])")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)

OK_WF = {"1": {"class_type": "EmptyImage", "inputs": {"width": 64, "height": 64, "batch_size": 1, "color": 0}},
         "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0], "filename_prefix": "halofuse_smoke"}}}
MISSING_WF = {"1": {"class_type": "HaloFuseSmokeMissingNode", "inputs": {}},
              "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0], "filename_prefix": "halofuse_smoke"}}}
# Entry 3 is identical to entry 1 on purpose: ComfyUI serves it from its cache (no new file), which the runner must
# report as succeeded with the earlier files copied (CI run 45099882256 found it reported no_output).
MANIFEST = [OK_WF, MISSING_WF, OK_WF]
EXPECTED_ITEMS = ["succeeded", "failed", "succeeded"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _events(stdout: str) -> list:
    out = []
    for line in stdout.splitlines():
        if line.startswith("[hf] "):
            try:
                out.append(json.loads(line[5:]))
            except ValueError:
                pass
    return out


def parse_imports(text: str) -> dict:
    """{folder: (seconds, failed)} from ComfyUI's 'Import times for custom nodes' block. Pass stdout AND stderr:
    ComfyUI logs to stderr by default, and the runner relays each stream to its own."""
    found = {}
    for line in text.splitlines():
        m = IMPORT_LINE.search(strip_ansi(line))
        if m:
            folder = re.split(r"[\\/]", m.group(3).rstrip("\\/"))[-1]
            found[folder] = (float(m.group(1)), bool(m.group(2)))
    return found


def _phases(events: list) -> dict:
    out = {}
    for e in events:
        if e.get("t") != "phase" or e.get("dur") is None:
            continue
        name = e.get("phase")
        if e.get("state") == "end" or (e.get("state") == "mark" and name == "node_import"):
            out[name] = round(out.get(name, 0.0) + float(e["dur"]), 3)
    return out


def _items(final: dict, events: list) -> list:
    """Final items plus the reason of each entry's last item event (for example "served from ComfyUI's cache")."""
    reasons = {e.get("wf"): e.get("reason") for e in events if e.get("t") == "item" and e.get("state") != "started"}
    return [{"wf": i.get("wf"), "status": i.get("status"), "error": i.get("error"), "reason": reasons.get(i.get("wf"))}
            for i in final.get("items", [])]


def run_once(args, work: Path, extra_args: str | None = None) -> dict:
    """Run the runner once in `work`; return what it did (no judgement)."""
    inp, out = work / "input", work / "output"
    inp.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    (inp / "halofuse_job.json").write_text(json.dumps({"workflows": [{"workflow": wf} for wf in MANIFEST]}),
                                           encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in ("HF_PRESET", "COMFY_EXTRA_ARGS")}
    env.update({
        "INPUT_DIR": str(inp), "OUTPUT_DIR": str(out), "COMFYUI_DIR": str(args.comfyui_dir),
        "MODEL_BASE_DIR": str(work / "models"), "HF_ALLOW_CPU": "1", "PREFETCH_MODELS": "0",
        "COMFY_PORT": str(_free_port()), "STARTUP_TIMEOUT_SECONDS": str(int(args.timeout)),
        "POLL_INTERVAL_SECONDS": "0.5", "HF_HEALTH_FILE": str(work / "health.json"), "PYTHONIOENCODING": "utf-8",
    })
    if extra_args:
        env["COMFY_EXTRA_ARGS"] = extra_args
    cmd = [args.python, args.runner, *args.runner_arg]
    if args.tini:
        cmd = [args.tini, "-s", "--", *cmd]
    started = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=args.timeout + 60)
        rc, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        rc = None
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", "replace")
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", "replace")
    wall = round(time.monotonic() - started, 3)
    events = _events(stdout)
    finals = [e for e in events if e.get("t") == "final"]
    final = finals[-1] if finals else {}
    files = [p for p in out.rglob("*") if p.is_file() and "_halofuse" not in p.relative_to(out).parts]
    return {"runner_exit": rc, "timed_out": timed_out, "wall_seconds": wall, "final": final,
            "items": _items(final, events),
            "outputs": len(files), "phases": _phases(events), "imports": parse_imports(stdout + "\n" + stderr),
            "tail": (stdout + stderr).splitlines()[-40:]}


def judge(run: dict, report: dict | None, timeout: float) -> tuple:
    problems = []
    if run["timed_out"]:
        problems.append(f"runner did not finish within {timeout:.0f}s (hang)")
    elif run["runner_exit"] != 1:
        problems.append(f"runner exit {run['runner_exit']} (expected 1: one entry fails on purpose)")
    if run["final"].get("status") != "partial":
        problems.append(f"final status {run['final'].get('status')!r} (expected 'partial')")
    statuses = [i["status"] for i in run["items"]]
    if statuses != EXPECTED_ITEMS:
        problems.append(f"item statuses {statuses} (expected {EXPECTED_ITEMS})")
    elif run["items"][1]["error"] != "missing_node":
        problems.append(f"entry 2 error {run['items'][1]['error']!r} (expected 'missing_node')")
    if run["outputs"] < 2:
        problems.append(f"{run['outputs']} output file(s) (expected 2)")

    strict = bool((report or {}).get("strict"))
    expected = [p["folder"] for p in (report or {}).get("packs", [])
                if p.get("status") in ("installed", "fetched") and p.get("folder")]
    seen = {k.lower(): v for k, v in run["imports"].items()}
    failed = sorted(f for f in expected if f.lower() in seen and seen[f.lower()][1])
    missing = sorted(f for f in expected if f.lower() not in seen)
    loaded = sorted(f for f in expected if f.lower() in seen and not seen[f.lower()][1])
    if strict:
        problems += [f"custom-node pack {f} failed to import (strict profile)" for f in failed]
        problems += [f"custom-node pack {f} is installed but ComfyUI never loaded it (strict profile)"
                     for f in missing]
    slowest = sorted(((k, v[0]) for k, v in run["imports"].items()), key=lambda kv: kv[1], reverse=True)[:10]
    imports = {"loaded": loaded, "failed": failed, "missing": missing, "slowest": [list(x) for x in slowest]}
    return problems, strict, imports


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="HaloFuse runtime image smoke test.")
    ap.add_argument("--runner", required=True)
    ap.add_argument("--runner-arg", action="append", default=[])
    ap.add_argument("--work")
    ap.add_argument("--comfyui-dir", required=True)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--tini")
    ap.add_argument("--install-report")
    ap.add_argument("--out", required=True)
    ap.add_argument("--timeout", type=float, default=600)
    ap.add_argument("--compare-args")
    args = ap.parse_args(argv)

    work = Path(args.work) if args.work else Path(tempfile.mkdtemp(prefix="halofuse-smoke-"))
    report = None
    if args.install_report:
        try:
            report = json.loads(Path(args.install_report).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"[smoke] install report unreadable: {exc}", flush=True)
    run = run_once(args, work)
    problems, strict, imports = judge(run, report, args.timeout)
    compare = None
    if args.compare_args:
        c = run_once(args, work / "compare", args.compare_args)
        compare = {"args": args.compare_args.split(), "runner_exit": c["runner_exit"],
                   "wall_seconds": c["wall_seconds"], "phases": c["phases"]}
    result = {"schema": SCHEMA, "passed": not problems, "problems": problems, "strict": strict,
              "runner_exit": run["runner_exit"], "wall_seconds": run["wall_seconds"], "final": run["final"],
              "items": run["items"], "outputs": run["outputs"], "imports": imports, "phases": run["phases"],
              "compare": compare, "log_tail": [] if not problems else run["tail"]}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, indent=1), encoding="utf-8")

    print(f"[smoke] {'PASSED' if not problems else 'FAILED'}: runner exit {run['runner_exit']}, "
          f"{run['wall_seconds']:.1f}s wall, ComfyUI ready after {run['phases'].get('comfy_start', '?')}s, "
          f"{len(imports['loaded'])} pack(s) loaded, {len(imports['failed'])} failed, "
          f"{len(imports['missing'])} missing", flush=True)
    for name, secs in imports["slowest"][:5]:
        print(f"[smoke]   import {secs:6.2f}s  {name}", flush=True)
    if compare:
        print(f"[smoke] compare {' '.join(compare['args'])}: exit {compare['runner_exit']}, "
              f"{compare['wall_seconds']:.1f}s wall, ComfyUI ready after "
              f"{compare['phases'].get('comfy_start', '?')}s", flush=True)
    for prob in problems:
        print(f"[smoke] problem: {prob}", flush=True)
    if problems:
        for line in run["tail"]:
            print(f"[smoke] | {line}", flush=True)
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())

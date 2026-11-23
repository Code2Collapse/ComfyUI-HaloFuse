#!/usr/bin/env python3
"""Build / cold-start measurement records (NEW_WORK section 10: nine metrics, measured separately).

  measure.py parse-log LOG --out REC.json [--profile P] [--image-ref R] [--host-label L]
      Derive startup metrics from a container log. Timed metrics need `docker logs --timestamps`
      (RFC 3339 prefix per line); durations ComfyUI prints itself (custom-node import times,
      "Prompt executed in X seconds") work without timestamps.
  measure.py record --out REC.json --metric NAME=VALUE [--metric ...] --source TEXT
      Merge externally measured values (CI build time, `docker image inspect` size, pull time).

Every metric is {value, unit, status, source}. A metric that the inputs cannot provide stays
value=null, status "NOT VERIFIED". Nothing is estimated or invented.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

SCHEMA = "halofuse.measurement/1"
METRICS = {  # name -> unit
    "build_duration": "s", "image_size": "bytes", "image_pull": "s", "container_start": "s",
    "comfyui_init": "s", "custom_node_imports": "s", "api_ready": "s", "model_load": "s", "first_render": "s",
}
TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})\s(.*)$")
IMPORT = re.compile(r"^\s*(?:\[[A-Z]+\]\s*)?([\d.]+) seconds( \(IMPORT FAILED\))?: (.+?)\s*$")
EXECUTED = re.compile(r"Prompt executed in ([\d.]+) seconds")
# ComfyUI 0.43 colours its log lines even into a pipe (ESC[32m[INFO]ESC[0m ...): strip CSI / 2-byte ESC sequences
# before matching anything in ComfyUI output.
ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|[@-Z\\-_])")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)



def empty_record(profile=None, image_ref=None, host_label=None) -> dict:
    return {
        "schema": SCHEMA,
        "recorded_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "host": {"label": host_label or "unspecified"},
        "profile": profile, "image_ref": image_ref,
        "metrics": {name: {"value": None, "unit": unit, "status": "NOT VERIFIED", "source": "not measured"}
                    for name, unit in METRICS.items()},
        "custom_node_imports_detail": [],
    }


def _ts(line: str):
    m = TS.match(line)
    if not m:
        return None, line
    frac = (m.group(2) or ".0")[:7]  # microseconds max
    zone = "+00:00" if m.group(3) == "Z" else m.group(3)
    try:
        return dt.datetime.fromisoformat(m.group(1) + frac + zone), m.group(4)
    except ValueError:  # impossible date: treat the line as untimed rather than guess
        return None, m.group(4)


def parse_log(lines) -> dict:
    """Return {metric: (value, source)} for what the log proves, plus the import detail list."""
    t0 = t_runner = t_launch = t_ready = t_first_load = t_last_loaded = None
    imports, first_render, in_imports = [], None, False
    for raw in lines:
        ts, line = _ts(strip_ansi(raw.rstrip("\n")))
        if ts and t0 is None:
            t0 = ts
        if "Import times for custom nodes" in line:
            in_imports = True
            continue
        m = IMPORT.match(line)
        if in_imports and m:
            folder = re.split(r"[\\/]", m.group(3).rstrip("\\/"))[-1]
            imports.append({"folder": folder, "seconds": float(m.group(1)), "failed": bool(m.group(2))})
            continue
        in_imports = False
        if line.startswith("[runner]") and t_runner is None:
            t_runner = ts
        if "[runner] starting ComfyUI" in line and t_launch is None:
            t_launch = ts
        if "[runner] ComfyUI ready" in line and t_ready is None:
            t_ready = ts
        if first_render is None:
            if "Requested to load" in line and t_first_load is None:
                t_first_load = ts
            if re.search(r"loaded (completely|partially)", line):
                t_last_loaded = ts
            em = EXECUTED.search(line)
            if em:
                try:
                    first_render = float(em.group(1))
                except ValueError:
                    first_render = None

    out = {}
    if imports:
        out["custom_node_imports"] = (round(sum(i["seconds"] for i in imports), 3),
                                      f"sum of ComfyUI 'Import times for custom nodes' ({len(imports)} entries)")
    if first_render is not None:
        out["first_render"] = (first_render, "ComfyUI 'Prompt executed in' (first prompt)")

    def span(a, b):
        return round((b - a).total_seconds(), 3) if a and b else None

    for name, val, src in (
        ("container_start", span(t0, t_runner), "first log line -> first [runner] line (docker timestamps)"),
        ("comfyui_init", span(t_launch, t_ready), "[runner] starting ComfyUI -> [runner] ComfyUI ready"),
        ("api_ready", span(t0, t_ready), "first log line -> [runner] ComfyUI ready"),
        ("model_load", span(t_first_load, t_last_loaded), "first 'Requested to load' -> last 'loaded ...' before "
                                                          "the first prompt finished"),
    ):
        if val is not None:
            out[name] = (val, src)
    return {"metrics": out, "imports": imports}


def merge(record: dict, values: dict, source_prefix: str) -> dict:
    for name, (value, source) in values.items():
        if name not in METRICS:
            raise SystemExit(f"unknown metric {name!r}; known: {', '.join(METRICS)}")
        record["metrics"][name] = {"value": value, "unit": METRICS[name], "status": "MEASURED",
                                   "source": f"{source_prefix}: {source}" if source_prefix else source}
    return record


def _load_or_new(path: Path, args) -> dict:
    if path.is_file():
        rec = json.loads(path.read_text(encoding="utf-8"))
        if rec.get("schema") != SCHEMA:
            raise SystemExit(f"{path} is not a {SCHEMA} record")
    else:
        rec = empty_record()
    for key in ("profile", "image_ref"):
        if getattr(args, key, None):
            rec[key] = getattr(args, key)
    if getattr(args, "host_label", None):
        rec["host"]["label"] = args.host_label
    return rec


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("parse-log", "record"):
        p = sub.add_parser(name)
        p.add_argument("--out", required=True)
        p.add_argument("--profile")
        p.add_argument("--image-ref")
        p.add_argument("--host-label")
    sub.choices["parse-log"].add_argument("log")
    sub.choices["record"].add_argument("--metric", action="append", default=[], help="NAME=VALUE")
    sub.choices["record"].add_argument("--source", required=True)
    args = ap.parse_args(argv)
    out = Path(args.out)
    rec = _load_or_new(out, args)
    if args.cmd == "parse-log":
        parsed = parse_log(Path(args.log).read_text(encoding="utf-8", errors="replace").splitlines())
        merge(rec, parsed["metrics"], f"log {Path(args.log).name}")
        rec["custom_node_imports_detail"] = parsed["imports"]
    else:
        values = {}
        for item in args.metric:
            name, _, value = item.partition("=")
            try:
                values[name] = (float(value), args.source)
            except ValueError:
                raise SystemExit(f"--metric {item!r}: value must be a number")
        merge(rec, values, "")
    out.parent.mkdir(parents=True, exist_ok=True)  # CI: out/ may not exist when the build step failed
    out.write_text(json.dumps(rec, indent=1) + "\n", encoding="utf-8")
    measured = [k for k, v in rec["metrics"].items() if v["status"] == "MEASURED"]
    print(f"{out}: measured {len(measured)}/9 ({', '.join(measured) or 'none'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

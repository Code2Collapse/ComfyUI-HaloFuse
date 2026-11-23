#!/usr/bin/env python3
"""Export a node snapshot (contract C-5, schema halofuse.node_snapshot/1).

A snapshot tells the bridge's preflight which node classes a runtime profile
provides, so an unknown class_type is blocked before a cloud job is paid for.

  export_node_snapshot.py export --profile P --out FILE (--url URL | --boot --comfyui-dir DIR | --catalog-only)
                                 [--catalog-dir catalog] [--image-ref REF] [--install-report FILE]
                                 [--comfyui-version V] [--source LABEL]
  export_node_snapshot.py index --dir DIR [--default-profile default]

--url reads GET /object_info from a running ComfyUI (loopback only).
--boot starts ComfyUI from --comfyui-dir on CPU, loopback, records import
failures from its log, reads /object_info and stops it (image build step).
--catalog-only writes the pack list without object_info.

Privacy: file-list combos (models, input images) are emptied, errors are
path-stripped and truncated; no host, user path or model name is written.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hf_catalog  # noqa: E402

SCHEMA = "halofuse.node_snapshot/1"
INDEX_SCHEMA = "halofuse.node_snapshot_index/1"
MAX_ERROR = 300
CORE_MODULES = ("nodes", "comfy_extras", "comfy_api_nodes", "comfy_api")
FILE_EXT = re.compile(
    r"\.(safetensors|sft|ckpt|pt|pth|bin|gguf|onnx|pkl|engine|png|jpe?g|webp|gif|bmp|tiff?|exr|hdr|mp4|mov|webm|mkv|"
    r"avi|wav|mp3|flac|ogg|m4a|json|ya?ml|txt|npz|npy|glb|gltf|obj|fbx|ply)$", re.I)
# optional "[INFO]" level prefix (ComfyUI 0.43 logging), matched after strip_ansi()
IMPORT_LINE = re.compile(r"^\s*(?:\[[A-Z]+\]\s*)?[\d.]+ seconds( \(IMPORT FAILED\))?: (.+?)\s*$")
# ComfyUI 0.43 colours its log lines even into a pipe (ESC[32m[INFO]ESC[0m ...): strip CSI / 2-byte ESC sequences
# before matching anything in ComfyUI output.
ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|[@-Z\\-_])")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)



# ------------------------------------------------------------------ pure parts

def _clean_error(text: str | None) -> str | None:
    if not text:
        return None
    t = re.sub(r"([A-Za-z]:)?([\\/][^\s\\/:'\"]+)+[\\/]", "", str(text))  # drop directories, keep file names
    t = re.sub(r"https?://\S+", "<url>", t)
    return t.strip()[:MAX_ERROR]


def _is_file_list(options) -> bool:
    return isinstance(options, list) and any(isinstance(o, str) and (FILE_EXT.search(o) or "/" in o or "\\" in o)
                                             for o in options)


def _scrub_inputs(entry: dict) -> dict:
    """Return a copy of one /object_info entry with file-list combos emptied."""
    out = copy.deepcopy(entry)
    for section in ("required", "optional"):
        for spec in ((out.get("input") or {}).get(section) or {}).values():
            if not isinstance(spec, list) or not spec:
                continue
            if isinstance(spec[0], list) and _is_file_list(spec[0]):          # legacy combo: [[...], {...}]
                spec[0] = []
            elif spec[0] == "COMBO" and len(spec) > 1 and isinstance(spec[1], dict) \
                    and _is_file_list(spec[1].get("options")):                 # V3 combo: ["COMBO", {"options"}]
                spec[1]["options"] = []
    return out


def _module_folder(python_module: str | None) -> str | None:
    if not python_module or not python_module.startswith("custom_nodes."):
        return None
    return python_module.split(".", 2)[1]


def _is_core(python_module: str | None) -> bool:
    return bool(python_module) and python_module.split(".")[0] in CORE_MODULES


def parse_import_failures(lines) -> dict:
    """Folders ComfyUI reported as '(IMPORT FAILED)' in its 'Import times for custom nodes' block."""
    failed = {}
    for line in lines:
        m = IMPORT_LINE.match(strip_ansi(line))
        if m and m.group(1):
            folder = re.split(r"[\\/]", m.group(2).rstrip("\\/"))[-1]
            failed[folder] = "IMPORT FAILED (see build log)"
    return failed


def build_snapshot(catalog: dict, profiles: dict, profile: str, *, object_info: dict | None,
                   present_folders: set, import_failures: dict, comfyui_version: str | None,
                   image_ref: str | None, source: str, object_info_source: str, catalog_sha256: str,
                   install_report: dict | None = None) -> dict:
    res = hf_catalog.resolve(catalog, profiles, profile)
    failures = {k.lower(): v for k, v in (import_failures or {}).items()}
    present = {f.lower() for f in present_folders or ()}
    report = {p["repo"]: p for p in (install_report or {}).get("packs", [])}

    classes_by_folder: dict[str, list] = {}
    core = []
    for name, info in (object_info or {}).items():
        mod = info.get("python_module")
        folder = _module_folder(mod)
        if folder:
            classes_by_folder.setdefault(folder.lower(), []).append(name)
        elif _is_core(mod):
            core.append(name)

    packs = []
    loaded_classes = set()
    missing_any = False
    for e in res.installed:
        folder = hf_catalog.folder_of(e) or ""
        key = folder.lower()
        rec = {"repo": e["repo"], "folder": folder, "commit": e["pin"].get("commit"),
               "version": e["pin"].get("version"), "status": "not_captured", "node_classes": None, "error": None}
        rep = report.get(e["repo"])
        if rep and rep.get("status") == "failed":
            rec.update(status="failed", error=_clean_error(rep.get("error")) or "install failed")
        elif object_info is None:
            pass
        elif key in failures:
            rec.update(status="failed", error=_clean_error(failures[key]))
        elif key in present or key in classes_by_folder:
            names = sorted(classes_by_folder.get(key, []))
            rec.update(status="loaded", node_classes=names)
            loaded_classes.update(names)
        else:
            missing_any = True
        packs.append(rec)
    # extra_nodes.txt packs (install report only, not in the catalog): listed and their classes kept, otherwise the
    # bridge preflight would block their nodes as unknown
    for rep in (install_report or {}).get("packs", []):
        if not rep.get("extra"):
            continue
        folder = rep.get("folder") or ""
        key = folder.lower()
        rec = {"repo": rep["repo"], "folder": folder, "commit": rep.get("commit"), "version": rep.get("version"),
               "status": "not_captured", "node_classes": None, "error": None, "extra": True}
        if rep.get("status") == "failed":
            rec.update(status="failed", error=_clean_error(rep.get("error")) or "install failed")
        elif object_info is None:
            pass
        elif key in failures:
            rec.update(status="failed", error=_clean_error(failures[key]))
        elif key in present or key in classes_by_folder:
            names = sorted(classes_by_folder.get(key, []))
            rec.update(status="loaded", node_classes=names)
            loaded_classes.update(names)
        else:
            missing_any = True
        packs.append(rec)
    for e, reason in res.excluded:
        packs.append({"repo": e["repo"], "folder": hf_catalog.folder_of(e), "commit": e["pin"].get("commit"),
                      "version": e["pin"].get("version"), "status": "not_installed", "node_classes": None,
                      "error": _clean_error(reason)})

    if object_info is None:
        status, filtered, core_names = "not_captured", {}, None
    else:
        keep = set(core) | loaded_classes
        filtered = {k: _scrub_inputs(v) for k, v in sorted(object_info.items()) if k in keep}
        status, core_names = ("partial" if missing_any else "captured"), sorted(core)
    return {
        "schema": SCHEMA,
        "profile": profile,
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source,
        "comfyui_version": comfyui_version,
        "image_ref": image_ref,
        "catalog_sha256": catalog_sha256,
        "packs": packs,
        "core_node_classes": core_names,
        "object_info_status": status,
        "object_info_source": object_info_source,
        "object_info": filtered,
    }


def build_index(directory: Path, default_profile: str = "default") -> dict:
    profiles = {}
    sha = None
    for f in sorted(Path(directory).glob("*.json")):
        if f.name == "index.json":
            continue
        snap = json.loads(f.read_text(encoding="utf-8"))
        if snap.get("schema") != SCHEMA:
            continue
        sha = sha or snap.get("catalog_sha256")
        profiles[snap["profile"]] = {
            "file": f.name, "source": snap["source"], "object_info_status": snap["object_info_status"],
            "comfyui_version": snap.get("comfyui_version"),
            "packs": sum(1 for p in snap["packs"] if p["status"] != "not_installed"),
            "packs_loaded": sum(1 for p in snap["packs"] if p["status"] == "loaded"),
        }
    return {"schema": INDEX_SCHEMA, "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "catalog_sha256": sha, "default_profile": default_profile, "profiles": profiles}


# ------------------------------------------------------------------ live ComfyUI

def get_object_info(url: str, timeout: float = 60) -> dict:
    host = re.sub(r"^https?://", "", url).split("/")[0].rsplit(":", 1)[0].strip("[]")
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("refusing a non-loopback ComfyUI URL (snapshots are taken from a local/isolated server)")
    with urllib.request.urlopen(url.rstrip("/") + "/object_info", timeout=timeout) as resp:
        return json.loads(resp.read())


def comfyui_version_of(comfy_dir: Path) -> str | None:
    f = Path(comfy_dir) / "comfyui_version.py"
    if f.is_file():
        m = re.search(r'__version__\s*=\s*"([^"]+)"', f.read_text(encoding="utf-8"))
        return m.group(1) if m else None
    return None


@dataclass
class BootResult:
    object_info: dict
    import_failures: dict
    present_folders: set
    comfyui_version: str | None
    log_tail: list = field(default_factory=list)
    process_exited: bool = False


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def boot_and_capture(comfy_dir: Path, python: str = sys.executable, timeout: float = 600,
                     extra_args: list | None = None) -> BootResult:
    """Start ComfyUI on CPU on loopback, wait for /object_info, then stop it."""
    comfy_dir = Path(comfy_dir)
    port = _free_port()
    argv = [python, "main.py", "--cpu", "--listen", "127.0.0.1", "--port", str(port), "--disable-auto-launch",
            *(extra_args or [])]
    if not (comfy_dir / "comfy").is_dir():
        argv.remove("--disable-auto-launch")  # fake ComfyUI in tests does not need it
    lines: list[str] = []
    proc = subprocess.Popen(argv, cwd=comfy_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", env={**os.environ, "PYTHONUNBUFFERED": "1"})
    reader = threading.Thread(target=lambda: [lines.append(l.rstrip("\n")) for l in proc.stdout], daemon=True)
    reader.start()
    info = None
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise SystemExit(f"ComfyUI exited during the snapshot boot (code {proc.returncode}):\n"
                                 + "\n".join(lines[-40:]))
            try:
                info = get_object_info(f"http://127.0.0.1:{port}", timeout=30)
                break
            except OSError:
                time.sleep(1)
        if info is None:
            raise SystemExit(f"ComfyUI did not serve /object_info within {timeout:.0f}s")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=20)
        reader.join(timeout=5)
    cn = comfy_dir / "custom_nodes"
    present = {p.name for p in cn.iterdir() if p.is_dir() and not p.name.startswith((".", "__"))} if cn.is_dir() else set()
    return BootResult(object_info=info, import_failures=parse_import_failures(lines), present_folders=present,
                      comfyui_version=comfyui_version_of(comfy_dir), log_tail=lines[-200:],
                      process_exited=proc.poll() is not None)


# ------------------------------------------------------------------ CLI

def main(argv=None) -> int:
    default_catalog = str(Path(__file__).resolve().parents[1] / "catalog")
    ap = argparse.ArgumentParser(description="Export a C-5 node snapshot.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("export")
    ex.add_argument("--catalog-dir", default=default_catalog)
    ex.add_argument("--profile", required=True)
    ex.add_argument("--out", required=True)
    mode = ex.add_mutually_exclusive_group(required=True)
    mode.add_argument("--url")
    mode.add_argument("--boot", action="store_true")
    mode.add_argument("--catalog-only", action="store_true")
    ex.add_argument("--comfyui-dir")
    ex.add_argument("--python", default=sys.executable)
    ex.add_argument("--image-ref")
    ex.add_argument("--install-report")
    ex.add_argument("--comfyui-version")
    ex.add_argument("--source")
    ex.add_argument("--timeout", type=float, default=900)
    ix = sub.add_parser("index")
    ix.add_argument("--dir", required=True)
    ix.add_argument("--default-profile", default="default")
    args = ap.parse_args(argv)

    if args.cmd == "index":
        idx = build_index(Path(args.dir), args.default_profile)
        (Path(args.dir) / "index.json").write_text(json.dumps(idx, indent=1) + "\n", encoding="utf-8")
        print(f"index: {len(idx['profiles'])} profiles")
        return 0

    catalog, profiles = hf_catalog.load(args.catalog_dir)
    hf_catalog.validate(catalog, profiles)
    report = json.loads(Path(args.install_report).read_text()) if args.install_report else None
    version = args.comfyui_version
    if args.boot:
        if not args.comfyui_dir:
            ap.error("--boot needs --comfyui-dir")
        boot = boot_and_capture(Path(args.comfyui_dir), python=args.python, timeout=args.timeout)
        oi, present, failures = boot.object_info, boot.present_folders, boot.import_failures
        version = version or boot.comfyui_version
        source, oi_source = args.source or "image-build", "ComfyUI CPU boot during the image build"
    elif args.url:
        oi = get_object_info(args.url)
        failures = {}
        present = {f for f in (_module_folder(v.get("python_module")) for v in oi.values()) if f}
        source, oi_source = args.source or "fixture-testbed", f"GET /object_info from a local ComfyUI ({args.source or 'testbed'})"
    else:
        oi, present, failures = None, set(), {}
        source, oi_source = "catalog-only", "NOT CAPTURED"
    if version is None and args.comfyui_dir:
        version = comfyui_version_of(Path(args.comfyui_dir))
    snap = build_snapshot(catalog, profiles, args.profile, object_info=oi, present_folders=present,
                          import_failures=failures, comfyui_version=version, image_ref=args.image_ref,
                          source=source, object_info_source=oi_source,
                          catalog_sha256=hf_catalog.catalog_sha256(args.catalog_dir), install_report=report)
    Path(args.out).write_text(json.dumps(snap, indent=1, sort_keys=False) + "\n", encoding="utf-8")
    counts = {}
    for p in snap["packs"]:
        counts[p["status"]] = counts.get(p["status"], 0) + 1
    print(f"snapshot {args.profile}: object_info {snap['object_info_status']} ({len(snap['object_info'])} classes), "
          f"packs {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

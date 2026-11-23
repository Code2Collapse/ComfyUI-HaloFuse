"""Organised saving of a job's delivered outputs (NODE_PREVIEWS_DESIGN section 2; owner 2026-11-23).

The job folder `<output>/HaloFuse/<job_id>/` stays exactly as delivery wrote it (resume and Retry delivery depend on
it). Next to it a readable index is written:

    <output>/<organized_output_dir: HaloFuse/by-name>/<input name>/<YYYY-MM-DD>/<HHMMSS>_<job id 8>/
        job.log  metadata.json  workflow_api.json  [workflow_ui.json]
        <node title>/<file>        hard link to the delivered file (a copy when the volume refuses links)
        <node title>/<file>.json   sidecar: node id/title/class, seeds + models of its inputs, GPU, digest, timings,
                                   sha256, how the file was matched to the node
        unattributed/<file>        a delivered output no node could be matched to

It runs when delivery reports it finished (also after Retry delivery), is idempotent, and never moves or deletes a
delivered file. Every path part is sanitised (delivery's rules: reserved names, bad characters, length caps, no
traversal). Nothing here names the provider, a host, a credential or the runtime image namespace, and nothing comes
from inside an H2Vault vault: a vault contributes its own node id/title/class only, its inputs are omitted from the
exported workflow copies, and log entries about its inner nodes are left out of job.log.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path

from . import brand, node_board
from .job_store import atomic_write_json

_log = logging.getLogger(__name__)

DEFAULT_DIR = f"{brand.OUTPUT_DIRNAME}/by-name"
UNATTRIBUTED = "unattributed"
MAX_NAME_BYTES = 120
OMITTED = "<omitted: H2Vault>"
_MEDIA_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".exr", ".mp4", ".webm", ".mov",
               ".mkv", ".avi", ".m4v", ".wav", ".mp3", ".flac", ".ogg", ".m4a")
_MODEL_EXTS = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".sft", ".onnx")
_SEED_INPUTS = ("seed", "noise_seed")
_BAD = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_LOCK = threading.Lock()


# ---- names ---------------------------------------------------------------------------------------------------------

def safe_part(text, fallback: str = "untitled", limit: int = MAX_NAME_BYTES) -> str:
    """One Windows-safe path part: no separators or reserved characters, no device name, no leading/trailing dots or
    spaces, at most `limit` UTF-8 bytes (a short hash keeps long names distinct)."""
    raw = str(text or "")
    s = re.sub(r"\.{2,}", "_", _BAD.sub("_", raw)).strip().strip(".").strip()
    if not s or set(s) <= {"_", "."}:
        s = fallback
    if s.split(".", 1)[0].upper() in _RESERVED:
        s = "_" + s
    if len(s.encode("utf-8")) > limit:
        tag = "~" + hashlib.sha1(raw.encode("utf-8", "surrogatepass")).hexdigest()[:8]
        s = s.encode("utf-8")[: limit - len(tag)].decode("utf-8", "ignore").rstrip(". ") + tag
    return s


def _strip_annotation(value: str) -> str:
    for tag in (" [input]", " [output]", " [temp]"):
        if value.endswith(tag):
            return value[: -len(tag)]
    return value


def input_name(workflow: dict, workflow_name: str | None = None) -> str:
    """The job's primary input: the first Load* node (graph order) whose value is a media file name, without folder
    and extension; else the workflow's file name; else 'untitled'. H2Vault nodes are never read."""
    for node_id in node_board.graph_order(workflow or {}):
        node = workflow[node_id]
        ct = str(node.get("class_type") or "")
        if node_board.is_vault(ct) or "Load" not in ct:
            continue
        for value in (node.get("inputs") or {}).values():
            if isinstance(value, str) and _strip_annotation(value).lower().endswith(_MEDIA_EXTS):
                base = _strip_annotation(value).replace("\\", "/").rsplit("/", 1)[-1]
                return safe_part(base.rsplit(".", 1)[0])
    if isinstance(workflow_name, str) and workflow_name.strip():
        base = workflow_name.replace("\\", "/").rsplit("/", 1)[-1]
        if base.lower().endswith(".json"):
            base = base[:-5]
        return safe_part(base)
    return "untitled"


def valid_dir_setting(value) -> bool:
    """organized_output_dir: '' (off) or a relative folder inside ComfyUI's output folder, at most 4 parts."""
    if not isinstance(value, str) or len(value) > 200 or "\x00" in value:
        return False
    if value == "":
        return True
    v = value.replace("\\", "/")
    if v.startswith("/") or re.match(r"^[A-Za-z]:", v):
        return False
    parts = [p for p in v.split("/") if p]
    return 0 < len(parts) <= 4 and all(p not in (".", "..") and safe_part(p) == p for p in parts)


# ---- facts from the job ---------------------------------------------------------------------------------------------

def _ancestors(workflow: dict, node_id: str) -> list[str]:
    seen, stack = [], [str(node_id)]
    while stack:
        nid = stack.pop()
        node = workflow.get(nid)
        if not isinstance(node, dict) or nid in seen:
            continue
        seen.append(nid)
        for value in (node.get("inputs") or {}).values():
            if node_board._is_link(value):
                stack.append(str(value[0]))
    return seen


def seeds_and_models(workflow: dict, node_id: str | None) -> tuple[list, list]:
    """Seed values and model file names of the node and everything upstream of it. Vault nodes contribute nothing."""
    ids = _ancestors(workflow, node_id) if node_id else [k for k in workflow]
    seeds, models = [], []
    for nid in sorted(ids, key=node_board._id_key):
        node = workflow.get(nid) or {}
        if node_board.is_vault(node.get("class_type")):
            continue
        for name, value in sorted((node.get("inputs") or {}).items()):
            if name in _SEED_INPUTS and isinstance(value, int) and not isinstance(value, bool):
                seeds.append({"node_id": nid, "title": node_board.node_title(workflow, nid), "input": name,
                              "value": value})
            elif isinstance(value, str) and value.lower().endswith(_MODEL_EXTS) and value not in models:
                models.append(value.replace("\\", "/"))
    return seeds, models


def input_files(workflow: dict) -> list[str]:
    out = []
    for nid in node_board.graph_order(workflow):
        node = workflow[nid]
        if node_board.is_vault(node.get("class_type")):
            continue
        for value in (node.get("inputs") or {}).values():
            if isinstance(value, str) and _strip_annotation(value).lower().endswith(_MEDIA_EXTS):
                v = _strip_annotation(value).replace("\\", "/")
                if v not in out:
                    out.append(v)
    return out


def public_workflow(workflow: dict) -> dict:
    """The API workflow without the inputs of H2Vault nodes (links stay: they are the artist's own graph)."""
    out = {}
    for k, node in workflow.items():
        if isinstance(node, dict) and node_board.is_vault(node.get("class_type")):
            node = dict(node, inputs={n: v if node_board._is_link(v) else OMITTED
                                      for n, v in (node.get("inputs") or {}).items()})
        out[k] = node
    return out


def public_ui_workflow(ui: dict) -> dict:
    if not isinstance(ui, dict):
        return ui
    ui = json.loads(json.dumps(ui))
    for n in ui.get("nodes") or []:
        if isinstance(n, dict) and node_board.is_vault(n.get("type")):
            if isinstance(n.get("widgets_values"), list):
                n["widgets_values"] = [OMITTED for _ in n["widgets_values"]]
            elif isinstance(n.get("widgets_values"), dict):
                n["widgets_values"] = {k: OMITTED for k in n["widgets_values"]}
            n.pop("properties", None)
    return ui


def _inner_vault_entry(entry: dict, workflow: dict) -> bool:
    """A log entry about a node INSIDE an H2Vault vault: its id is `<vault>.<...>`."""
    d = entry.get("data") if isinstance(entry.get("data"), dict) else {}
    for raw in (entry.get("node_id"), d.get("node")):
        s = str(raw) if raw is not None else ""
        root = node_board.fold_id(s, workflow) if "." in s and s not in workflow else None
        if root is not None and node_board.is_vault((workflow.get(root) or {}).get("class_type")):
            return True
    return False


def job_log(store, job_id: str, workflow: dict) -> str:
    from .job_store import iso, redact
    lines = []
    for e in store.iter_entries(job_id, 0):
        if _inner_vault_entry(e, workflow):
            continue
        text = redact(node_board.scrub_inner_ids(str(e.get("text") or ""), workflow))
        lines.append(f"{iso(e.get('ts'))} {e.get('source')} {str(e.get('level') or '').upper()} {text}\n")
    return "".join(lines)


def _durations(rec: dict) -> dict:
    created, ended = rec.get("created_at"), rec.get("ended_at")
    ts = (rec.get("cloud") or {}).get("timestamps") or {}
    total = round(ended - created) if isinstance(created, (int, float)) and isinstance(ended, (int, float)) else None
    return {"total_s": total, "submitted_at": rec.get("submitted_at"), "ended_at": ended,
            "started_running_at": ts.get("started_running_at"), "terminal_at": ts.get("terminal_at")}


def _gpu(rec: dict) -> str | None:
    cloud = rec.get("cloud") or {}
    if cloud.get("gpu_name"):
        return str(cloud["gpu_name"])
    gpus = (rec.get("fingerprint") or {}).get("gpus") or []
    names = [str(g.get("name")) for g in gpus if isinstance(g, dict) and g.get("name")]
    return ", ".join(names) or None


def _digest(rec: dict) -> str | None:
    d = (rec.get("cloud") or {}).get("image_digest")
    return d if isinstance(d, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", d) else None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# ---- placing files --------------------------------------------------------------------------------------------------

def _place(src: Path, dest: Path) -> str:
    """Hard link (no admin rights needed on NTFS, same volume) else copy. Returns the method used."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.part")
    if tmp.exists():
        tmp.unlink()
    try:
        os.link(src, tmp)
        method = "hardlink"
    except OSError:
        shutil.copy2(src, tmp)
        method = "copy"
    os.replace(tmp, dest)
    return method


def _free(dest: Path, sha: str | None, taken: set) -> tuple[Path, bool]:
    """(destination, already there): the same name when free or holding the same bytes, else `name (n).ext`."""
    n = 1
    candidate = dest
    while True:
        key = str(candidate).casefold()
        if key not in taken:
            if not candidate.exists():
                return candidate, False
            if sha and candidate.is_file() and _sha256(candidate) == sha:
                return candidate, True
        n += 1
        stem, dot, ext = dest.name.rpartition(".")
        candidate = dest.with_name(f"{stem} ({n}).{ext}" if dot and stem else f"{dest.name} ({n})")


def _settings(settings):
    if settings is not None:
        return settings
    try:
        from . import config
        return config.load_settings()
    except Exception:  # noqa: BLE001 - organise with the defaults when settings cannot be read
        return {}


def _lock(job_id: str) -> threading.Lock:
    with _LOCKS_LOCK:
        return _LOCKS.setdefault(job_id, threading.Lock())


def organize(job_id: str, store, *, settings: dict | None = None, defs=None) -> dict | None:
    """Write (or complete) the readable index for one delivered job. None when switched off or nothing to do."""
    settings = _settings(settings)
    folder = settings.get("organized_output_dir", DEFAULT_DIR) if isinstance(settings, dict) else DEFAULT_DIR
    if not folder or not valid_dir_setting(folder):
        return None
    rec = store.get(job_id)
    if not rec:
        return None
    with _lock(job_id):
        return _organize(job_id, store, rec, folder, defs)


def _organize(job_id: str, store, rec: dict, folder: str, defs) -> dict | None:
    job_dir = Path(store.job_dir(job_id))
    try:
        manifest = json.loads((job_dir / ".halofuse" / "manifest.json").read_text(encoding="utf-8")).get("files") or {}
    except (OSError, ValueError, AttributeError):
        manifest = {}
    board = node_board.NodeBoard(job_id, store, defs=defs)
    workflow = board.workflow
    view = board.view()
    sections = {s["node_id"]: s for s in view["sections"]}
    by_remote, by_preview = {}, {}
    for s in view["sections"]:
        for item in s["items"]:
            if item.get("source") != "delivered":
                continue
            if item.get("remote"):
                by_remote[item["remote"]] = (s["node_id"], item.get("match"))
            elif item.get("preview"):
                by_preview[item["preview"]] = (s["node_id"], "runner")
    # one folder per node; equal titles get the node id so two nodes never share a folder
    titles: dict[str, str] = {}
    used: dict[str, str] = {}
    for s in view["sections"]:
        name = safe_part(s["title"], fallback=f"node {s['node_id']}")
        if name.casefold() in used and used[name.casefold()] != s["node_id"]:
            name = safe_part(f"{s['title']} #{s['node_id']}")
        used[name.casefold()] = s["node_id"]
        titles[s["node_id"]] = name

    created = rec.get("created_at") if isinstance(rec.get("created_at"), (int, float)) else time.time()
    local = time.localtime(created)
    parts = [*(p for p in folder.replace("\\", "/").split("/") if p),
             input_name(workflow, rec.get("workflow_name")), time.strftime("%Y-%m-%d", local),
             f"{time.strftime('%H%M%S', local)}_{job_id[:8]}"]
    root = Path(store.root).parent
    run_dir = root.joinpath(*parts)
    base = root.resolve()
    if os.path.commonpath([str(base), str(run_dir.resolve())]) != str(base):
        return None
    run_dir.mkdir(parents=True, exist_ok=True)

    gpu, digest = _gpu(rec), _digest(rec)
    files_meta, methods, taken = [], set(), set()
    for remote in sorted(manifest):
        entry = manifest[remote]
        if not isinstance(entry, dict) or entry.get("category") not in ("output", "preview"):
            continue
        local_rel = str(entry.get("local") or "")
        src = job_dir / local_rel
        if not local_rel or not src.is_file():
            continue
        if entry["category"] == "output":
            node_id, match = by_remote.get(remote, (None, None))
        else:
            node_id, match = by_preview.get(local_rel.rsplit("/", 1)[-1], (None, None))
            if node_id is None:
                continue  # a runner preview no node claims (e.g. the last full-size sampler frame) is not an output
        sub = titles.get(node_id, UNATTRIBUTED) if node_id else UNATTRIBUTED
        sha = entry.get("sha256") if isinstance(entry.get("sha256"), str) else None
        dest, there = _free(run_dir / sub / safe_part(src.name, fallback="file", limit=180), sha, taken)
        taken.add(str(dest).casefold())
        if not there:
            methods.add(_place(src, dest))
        sec = sections.get(node_id) or {}
        seeds, models = seeds_and_models(workflow, node_id) if node_id else ([], [])
        rel = dest.relative_to(run_dir).as_posix()
        side = {"schema": "halofuse.output_sidecar/1", "job_id": job_id, "file": dest.name,
                "category": entry["category"], "node_id": node_id, "title": sec.get("title"),
                "class_type": sec.get("class_type"), "kind": node_board.kind_of_name(dest.name),
                "match": match or "none", "size": entry.get("size"), "sha256": sha or _sha256(dest),
                "seeds": seeds, "models": models, "gpu": gpu, "image_digest": digest,
                "timings": _durations(rec), "source": local_rel}
        atomic_write_json(dest.with_name(dest.name + ".json"), side)
        files_meta.append({"path": rel, "node_id": node_id, "title": sec.get("title"),
                           "class_type": sec.get("class_type"), "kind": side["kind"], "size": entry.get("size"),
                           "sha256": side["sha256"], "match": side["match"], "category": entry["category"]})

    seeds, models = seeds_and_models(workflow, None)
    fp = rec.get("fingerprint") or {}
    meta = {
        "schema": "halofuse.run_metadata/1", "job_id": job_id, "label": rec.get("label"),
        "date": time.strftime("%Y-%m-%d", local), "created_at": created, "outcome": rec.get("outcome"),
        "runtime": {"image_digest": digest, "comfyui_version": fp.get("comfyui_version")},
        "gpu": {"instance_type": (rec.get("cloud") or {}).get("instance_type"), "name": gpu},
        "durations": _durations(rec),
        "cost": {"estimate_usd": (rec.get("cost") or {}).get("estimate_usd"), "label": "estimate"},
        "input_name": parts[-3], "input_files": input_files(workflow), "workflow_name": rec.get("workflow_name"),
        "seeds": seeds, "models": models,
        "nodes": [{"node_id": s["node_id"], "title": s["title"], "class_type": s["class_type"], "state": s["state"]}
                  for s in view["sections"]],
        "files": files_meta, "file_method": sorted(methods) or ["none"],
        "delivered_job_folder": f"{brand.OUTPUT_DIRNAME}/{job_id}",
    }
    atomic_write_json(run_dir / "metadata.json", meta)
    atomic_write_json(run_dir / "workflow_api.json", public_workflow(workflow))
    ui = board._read_json("workflow_ui.json")
    if isinstance(ui, dict):
        atomic_write_json(run_dir / "workflow_ui.json", public_ui_workflow(ui))
    tmp = run_dir / ".job.log.tmp"
    tmp.write_text(job_log(store, job_id, workflow), encoding="utf-8")
    os.replace(tmp, run_dir / "job.log")
    subfolder = "/".join(parts)
    method = "copy" if methods == {"copy"} else "hardlink" if methods == {"hardlink"} else \
        ("mixed" if methods else "none")
    out = {"folder": subfolder, "files": len(files_meta), "method": method, "at": time.time()}
    store.update(job_id, organized=out)
    store.append_event(job_id, "organized", text=f"Saved {len(files_meta)} file(s) by name to {subfolder}.",
                       data={"folder": subfolder, "files": len(files_meta), "method": method})
    return out


# ---- trigger: delivery finished (lifecycle event hook; no lifecycle change) --------------------------------------------

HOOK_NAME = "organize"


def _on_event(store, defs, background: bool):
    def hook(job_id: str, entry: dict) -> None:
        d = entry.get("data") if isinstance(entry.get("data"), dict) else {}
        if entry.get("kind") != "delivery" or d.get("phase") != "finished":
            return

        def work():
            try:
                st = store
                if st is None:
                    from .job_store import default_store
                    st = default_store()
                node_board.remember(job_id, st, defs=defs)
                organize(job_id, st, defs=defs)
            except Exception as exc:  # noqa: BLE001 - organising never changes the job or its delivery
                _log.info("HaloFuse could not organise the outputs of %s (%s)", job_id, type(exc).__name__)
        if background:
            threading.Thread(target=work, daemon=True, name="halofuse-organize").start()
        else:
            work()
    return hook


def install(store=None, *, defs=None, background: bool = True) -> None:
    from . import lifecycle
    if store is not None:
        lifecycle._ensure_listener(store)
    lifecycle.register_event_hook(_on_event(store, defs, background), name=HOOK_NAME)


def uninstall() -> None:
    from . import lifecycle
    lifecycle.unregister_event_hook(HOOK_NAME)

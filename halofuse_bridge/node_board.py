"""Per-node preview sections of a cloud job (NODE_PREVIEWS_DESIGN section 2; owner 2026-11-23).

`board_for(job_id).view()` answers: which nodes of the submitted workflow show something, in graph order, with a
state badge (waiting / running / done / cached / failed / not_run) and their items (live frames, images, masks,
videos, audio, text, file chips), plus a plain line for anything that cannot be shown here.

Sources, all inside the job folder: `workflow.json` (the API workflow the cloud ran, `_meta.title`),
`workflow_ui.json` (titles, optional), the job log (C-3 runner events as lifecycle stored them: node, progress,
preview + the live sink record, ui, error, output, final) read incrementally from a cursor, and the delivery manifest.
Generic by design: a node gets a section because its definition says OUTPUT_NODE (local ComfyUI, else the node
snapshot), because it sends frames/progress, or because the runner reports its `executed` payload - never because
its class is on a list (a name heuristic is used only when no definition is known).

H2Vault (zero-information rule): every id is folded to a node of the submitted workflow; an inner id of a vault (or
of any node expansion, `<root>.<call>.<graph>.<inner>`) becomes the vault itself and never contributes a class, a
title or an id. A vault shows only what H2Vault already publishes on the vault node.

Cache: core 0.39-0.46 re-send `executed` for cached output nodes, so most cached sections fill from this job. When
one does not (an older core, a vault with previews off, a node core did not revisit), the section shows the last
result this computer saw for the same node + inputs (`NodeMemory`, keyed by `input_hash`).
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path

from . import brand
from .job_store import atomic_write_json

VAULT_PREFIX = "H2Vault_ProtectedSubgraph"
AUTO_PREVIEW_KEY = "halofuse_auto_preview_of"
MAX_ITEMS = 24
MEMORY_FILE = ".halofuse/node_memory.json"
_NAME_GUESS_RE = re.compile(r"(Preview|Save|Show|VideoCombine)")
_WF_RE = re.compile(r"^wf_\d+$")
_EXT = {
    **dict.fromkeys(("png", "jpg", "jpeg", "webp", "gif", "bmp", "avif", "tif", "tiff", "exr"), "image"),
    **dict.fromkeys(("mp4", "webm", "mov", "mkv", "avi", "m4v"), "video"),
    **dict.fromkeys(("wav", "mp3", "flac", "ogg", "m4a", "aac", "opus"), "audio"),
    **dict.fromkeys(("glb", "gltf", "obj", "ply", "fbx", "stl", "usdz"), "3d"),
}
_KEY_KIND = {"images": "image", "gifs": "video", "video": "video", "videos": "video", "audio": "audio",
             "3d": "3d", "mesh": "3d", "model_file": "3d", "h2v_previews": "file"}
_SILENT_KEYS = {"animated", "text", "string"}

STATE_TEXT = {
    "waiting": "Waiting to run",
    "running": "Running",
    "done": "Done",
    "cached": "Cached - shows the last result",
    "not_run": "Did not run in the cloud (not needed for the outputs, or the render stopped first)",
}
OLD_RUNTIME_NOTE = ("This cloud runtime does not report each node's output kinds yet: sections show live frames and "
                    "delivered files matched by file name. The new HaloFuse runtime image adds audio, text and exact "
                    "file-to-node matching.")
VAULT_NOTE = "H2Vault node: shows only what the vault's author allows; nothing inside the vault is listed."


# ---- workflow helpers -------------------------------------------------------------------------------------------

def is_vault(class_type) -> bool:
    return isinstance(class_type, str) and class_type.startswith(VAULT_PREFIX)


def _is_link(value) -> bool:
    return (isinstance(value, list) and len(value) == 2 and isinstance(value[0], (str, int))
            and isinstance(value[1], int) and not isinstance(value[1], bool))


def _id_key(node_id: str):
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in re.split(r"[:._]", str(node_id)))


def graph_order(workflow: dict) -> list[str]:
    """Topological order of the API graph (Kahn; the smallest ready id first, so ties follow the ids)."""
    nodes = {str(k): v for k, v in workflow.items() if isinstance(v, dict)}
    deps = {k: set() for k in nodes}
    users: dict[str, set] = {k: set() for k in nodes}
    for k, node in nodes.items():
        for value in (node.get("inputs") or {}).values():
            if _is_link(value) and str(value[0]) in nodes and str(value[0]) != k:
                deps[k].add(str(value[0]))
                users[str(value[0])].add(k)
    ready = sorted((k for k, d in deps.items() if not d), key=_id_key)
    order = []
    while ready:
        k = ready.pop(0)
        order.append(k)
        for u in users[k]:
            deps[u].discard(k)
            if not deps[u] and u not in order and u not in ready:
                ready.append(u)
        ready.sort(key=_id_key)
    order += sorted((k for k in nodes if k not in order), key=_id_key)  # a cycle never hides a node
    return order


def input_hash(workflow: dict, node_id: str, _memo=None, _stack=None) -> str | None:
    """Merkle hash of a node: class + inputs, each link replaced by the upstream node's hash and slot. `_meta` and
    core's stored `is_changed` are ignored, so a renamed node or a re-posted prompt keeps its key."""
    memo = {} if _memo is None else _memo
    stack = set() if _stack is None else _stack
    node_id = str(node_id)
    if node_id in memo:
        return memo[node_id]
    node = workflow.get(node_id)
    if not isinstance(node, dict) or node_id in stack:
        return None
    stack.add(node_id)
    inputs = {}
    for name, value in sorted((node.get("inputs") or {}).items()):
        if _is_link(value):
            inputs[name] = ["link", input_hash(workflow, str(value[0]), memo, stack), value[1]]
        else:
            inputs[name] = value
    stack.discard(node_id)
    digest = hashlib.sha256(json.dumps({"c": node.get("class_type"), "i": inputs}, sort_keys=True,
                                       default=str).encode("utf-8")).hexdigest()
    memo[node_id] = digest
    return digest


def default_defs(workflow: dict):
    """Node definitions for the workflow's classes: the local ComfyUI first, then the node snapshot (C-5)."""
    classes = {n.get("class_type") for n in workflow.values() if isinstance(n, dict) and n.get("class_type")}
    try:
        from . import workflow_analysis
        local = workflow_analysis.local_node_defs(classes)
        snapshot = workflow_analysis._default_snapshot()
    except Exception:  # noqa: BLE001 - outside ComfyUI or no snapshot: the name heuristic still applies
        local, snapshot = {}, None
    object_info = getattr(snapshot, "object_info", None) or {}
    return lambda ct: local.get(ct) or object_info.get(ct)


def _lookup(defs):
    if defs is None:
        return lambda ct: None
    if callable(defs):
        return defs
    return lambda ct: defs.get(ct)


def is_output_node(class_type, lookup) -> bool:
    if is_vault(class_type):
        return True
    d = lookup(class_type)
    if isinstance(d, dict) and "output_node" in d:
        return bool(d.get("output_node"))
    return bool(_NAME_GUESS_RE.search(class_type or ""))


def sampler_like(class_type, lookup) -> bool:
    if "Sampler" in (class_type or ""):
        return True
    d = lookup(class_type) or {}
    outs = d.get("output") or []
    req = ((d.get("input") or {}).get("required") or {}) if isinstance(d.get("input"), dict) else {}
    return "LATENT" in outs and ("seed" in req or "noise_seed" in req)


def ui_titles(ui_workflow) -> dict:
    out = {}
    for n in (ui_workflow or {}).get("nodes") or [] if isinstance(ui_workflow, dict) else []:
        if isinstance(n, dict) and n.get("id") is not None and isinstance(n.get("title"), str) and n["title"].strip():
            out[str(n["id"])] = n["title"].strip()[:80]
    return out


def node_title(workflow: dict, node_id: str, titles: dict | None = None) -> str:
    node = workflow.get(str(node_id)) or {}
    meta = node.get("_meta") if isinstance(node.get("_meta"), dict) else {}
    title = meta.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()[:80]
    if titles and str(node_id) in titles:
        return titles[str(node_id)]
    return f"{node.get('class_type') or 'Node'} #{node_id}"


def helper_sources(workflow: dict) -> dict:
    """Auto-preview helper node id -> the artist's node it previews (the cloud copy only, auto_preview.py)."""
    out = {}
    for k, node in workflow.items():
        meta = node.get("_meta") if isinstance(node, dict) and isinstance(node.get("_meta"), dict) else {}
        src = meta.get(AUTO_PREVIEW_KEY)
        if isinstance(src, str) and src in workflow:
            out[str(k)] = src
    return out


def fold_id(node_id, workflow: dict, helpers: dict | None = None):
    """The submitted workflow's node an event id belongs to, or None. `<root>.<...>` (node expansion: H2Vault inner
    nodes) -> `<root>`; an auto-preview helper -> the node it previews."""
    if node_id is None:
        return None
    s = str(node_id)
    helpers = helpers or {}
    if s in helpers:
        return helpers[s]
    if s in workflow:
        return s
    if "." in s:
        return fold_id(s.split(".", 1)[0], workflow, helpers)
    return None


def scrub_inner_ids(text: str, workflow: dict) -> str:
    """Every `<vault>.<n>.<n>.<x>` in a text -> `<vault>` (H2Vault inner ids). Only H2Vault nodes are matched, so an
    IP address or a version number that happens to start with another node's id is left alone."""
    if not isinstance(text, str) or "." not in text:
        return text

    def repl(m):
        root = m.group(1)
        return root if is_vault((workflow.get(root) or {}).get("class_type")) else m.group(0)
    return re.sub(r"(?<![\w.])([\w:]+)\.\d+\.\d+\.[\w:.-]+", repl, text)


def kind_of_name(name: str, key: str | None = None) -> str:
    ext = str(name).rsplit(".", 1)[-1].lower() if "." in str(name) else ""
    return _EXT.get(ext) or _KEY_KIND.get(key or "", "file")


def _str(value, limit=200):
    return value[:limit] if isinstance(value, str) else None


def _num(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


# ---- the cache memory ----------------------------------------------------------------------------------------------

class NodeMemory:
    """Last items per node input hash on this computer (bounded, atomic writes, a corrupt file reads as empty)."""

    def __init__(self, path, max_entries: int = 2000):
        self.path = Path(path)
        self.max_entries = max(1, int(max_entries))
        self._lock = threading.Lock()
        self._data = None

    def _load(self) -> "OrderedDict[str, dict]":
        if self._data is None:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                entries = raw.get("entries") if isinstance(raw, dict) else None
                self._data = OrderedDict((k, v) for k, v in (entries or {}).items() if isinstance(v, dict))
            except (OSError, ValueError):
                self._data = OrderedDict()
        return self._data

    def get(self, key):
        if not key:
            return None
        with self._lock:
            entry = self._load().get(key)
            return json.loads(json.dumps(entry)) if entry is not None else None

    def put(self, key, entry: dict) -> None:
        if not key:
            return
        with self._lock:
            data = self._load()
            data.pop(key, None)
            data[key] = dict(entry, ts=entry.get("ts") or time.time())
            while len(data) > self.max_entries:
                data.popitem(last=False)
            try:
                atomic_write_json(self.path, {"schema": 1, "entries": data})
            except OSError:
                pass


def memory_for(store) -> NodeMemory:
    with _REG_LOCK:
        key = str(store.root)
        mem = _MEMORIES.get(key)
        if mem is None:
            mem = _MEMORIES[key] = NodeMemory(Path(store.root) / MEMORY_FILE)
        return mem


# ---- the board ------------------------------------------------------------------------------------------------------

class NodeBoard:
    def __init__(self, job_id: str, store, *, defs=None, memory: NodeMemory | None = None):
        self.job_id = job_id
        self.store = store
        self.job_dir = Path(store.job_dir(job_id))
        self.workflow = self._read_json("workflow.json") or {}
        if not isinstance(self.workflow, dict):
            self.workflow = {}
        self.titles = ui_titles(self._read_json("workflow_ui.json"))
        self.helpers = helper_sources(self.workflow)
        self.lookup = _lookup(defs if defs is not None else default_defs(self.workflow))
        self.memory = memory if memory is not None else memory_for(store)
        self.order = {k: i for i, k in enumerate(graph_order(self.workflow))}
        self._lock = threading.RLock()
        self.cursor = 0
        self.ingested = 0
        self.version = 0
        self.saw_ui = False
        self.saw_output = False   # an output reached the bridge (a new runtime sends `ui` with it)
        self.outputs: list[dict] = []
        self.final = None
        self.sections: dict[str, dict] = {}
        self._manifest = (None, {})
        for node_id, node in self.workflow.items():
            ct = node.get("class_type") if isinstance(node, dict) else None
            if node_id in self.helpers or not ct:
                continue
            if is_output_node(ct, self.lookup):
                self._section(node_id, role="vault" if is_vault(ct) else "output", planned=True)
        for helper, src in self.helpers.items():
            self._section(src, role=self.sections.get(src, {}).get("role") or "auto_preview", planned=True)

    # -- files --
    def _read_json(self, rel):
        try:
            return json.loads((self.job_dir / rel).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _load_manifest(self) -> dict:
        path = self.job_dir / ".halofuse" / "manifest.json"
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            return {}
        if self._manifest[0] != stamp:
            data = self._read_json(".halofuse/manifest.json")
            files = data.get("files") if isinstance(data, dict) else None
            self._manifest = (stamp, files if isinstance(files, dict) else {})
        return self._manifest[1]

    # -- sections --
    def _section(self, node_id: str, role: str = "output", planned: bool = False) -> dict:
        sec = self.sections.get(node_id)
        if sec is None:
            ct = (self.workflow.get(node_id) or {}).get("class_type")
            if is_vault(ct):
                role = "vault"
            sec = self.sections[node_id] = {
                "node_id": node_id, "title": node_title(self.workflow, node_id, self.titles), "class_type": ct,
                "role": role, "planned": planned, "state": "waiting", "error": None, "step": None, "steps": None,
                "frames": [], "frame_count": 0, "live": [], "ui_temp": [], "ui_out": [], "path_previews": [],
                "texts": [], "unknown": [], "cached_ui": False}
        return sec

    def _fold(self, node_id):
        return fold_id(node_id, self.workflow, self.helpers)

    def refresh(self) -> None:
        with self._lock:
            n = 0
            for entry in self.store.iter_entries(self.job_id, self.cursor):
                self.cursor = int(entry.get("seq", self.cursor)) + 1
                n += 1
                try:
                    self._ingest(entry)
                except Exception:  # noqa: BLE001 - one odd event never breaks the board
                    continue
            self.ingested = n
            if n:
                self.version += 1

    def _ingest(self, entry: dict) -> None:
        kind = entry.get("kind")
        d = entry.get("data") if isinstance(entry.get("data"), dict) else {}
        if kind == "output" or (kind == "preview" and d.get("kind") in ("temp_output", "output_thumb")):
            self.saw_output = True
        if kind == "node":
            if d.get("state") == "cached":
                for raw in d.get("nodes") or []:
                    if "." in str(raw):
                        continue  # an inner node of an expansion is never listed (would mark the vault cached)
                    nid = self._fold(raw)
                    if nid is None:
                        continue
                    ct = (self.workflow.get(nid) or {}).get("class_type")
                    if nid in self.sections or sampler_like(ct, self.lookup):
                        sec = self._section(nid, role="sampler")
                        if sec["state"] in ("waiting",):
                            sec["state"] = "cached"
                return
            nid = self._fold(d.get("node", entry.get("node_id")))
            sec = self.sections.get(nid) if nid is not None else None
            if sec is None or sec["state"] == "failed":
                return
            if d.get("state") == "start":
                sec["state"] = "running"
            elif d.get("state") == "done" and sec["state"] in ("running", "waiting"):
                sec["state"] = "done"
        elif kind == "progress":
            nid = self._fold(d.get("node", entry.get("node_id")))
            if nid is None:
                return
            sec = self._section(nid, role="sampler")
            if sec["state"] != "failed":
                sec["state"] = "running"
            sec["step"], sec["steps"] = _num(d.get("value")), _num(d.get("max"))
        elif kind == "preview":
            rec = d.get("preview") if isinstance(d.get("preview"), dict) else None
            nid = self._fold((rec or {}).get("node_id") or d.get("node"))
            if nid is None:
                return
            pkind = (rec or {}).get("kind") or d.get("kind")
            sec = self._section(nid, role="sampler" if pkind == "latent" else "output")
            if rec and rec.get("name"):
                if pkind == "latent":
                    sec["frames"] = [rec]
                    sec["frame_count"] += 1
                    if _num(rec.get("step")) is not None:
                        sec["step"], sec["steps"] = rec.get("step"), rec.get("steps")
                    if sec["state"] == "waiting":
                        sec["state"] = "running"
                else:
                    sec["live"].append(rec)
                    sec["live"] = sec["live"][-MAX_ITEMS:]
            elif isinstance(d.get("path"), str) and pkind == "temp_output":
                name = _delivered_preview_name(d["path"])
                if name and name not in sec["path_previews"]:
                    sec["path_previews"].append(name)
        elif kind == "ui":
            self.saw_ui = True
            nid = self._fold(d.get("node", entry.get("node_id")))
            if nid is None:
                return
            sec = self._section(nid, role="output")
            if sec["state"] != "failed":
                sec["state"] = "cached" if d.get("cached") is True else "done"
            sec["cached_ui"] = d.get("cached") is True
            pid = _str(d.get("prompt_id"), 64) or ""
            with_items = set()
            for item in d.get("items") or []:
                if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
                    continue
                with_items.add(item.get("key"))
                clean = {"key": _str(item.get("key"), 64), "filename": _str(item["filename"], 255),
                         "subfolder": _str(item.get("subfolder"), 255) or "", "label": _str(item.get("label"), 40),
                         "kind": _str(item.get("kind"), 16), "format": _str(item.get("format"), 64),
                         "prompt_id": pid}
                if "://" in clean["filename"] or "://" in clean["subfolder"]:
                    continue
                (sec["ui_out"] if item.get("type") == "output" else sec["ui_temp"]).append(clean)
            sec["ui_temp"] = sec["ui_temp"][-MAX_ITEMS:]
            sec["ui_out"] = sec["ui_out"][-200:]
            for t in d.get("texts") or []:
                if isinstance(t, str):
                    sec["texts"] = (sec["texts"] + [scrub_inner_ids(t[:500], self.workflow)])[-3:]
            for key in (d.get("keys") or {}):
                if key not in _SILENT_KEYS and key not in with_items and key not in sec["unknown"]:
                    sec["unknown"].append(str(key)[:64])
        elif kind == "error":
            nid = self._fold(d.get("node", entry.get("node_id")))
            if nid is None:
                return
            sec = self._section(nid, role="output")
            sec["state"] = "failed"
            msg = d.get("message") if isinstance(d.get("message"), str) else entry.get("text")
            sec["error"] = scrub_inner_ids(str(msg or "error")[:300], self.workflow)
        elif kind == "output" and isinstance(d.get("path"), str):
            self.outputs.append({"path": d["path"], "kind": d.get("kind")})
        elif kind == "final":
            self.final = {"status": d.get("status")}

    # -- view --
    def view(self) -> dict:
        self.refresh()
        with self._lock:
            rec = self.store.get(self.job_id) or {}
            terminal = bool(rec.get("terminal"))
            outcome = rec.get("outcome")
            manifest = self._load_manifest()
            claimed, by_node = self._attribute(manifest)
            sections = []
            for nid in sorted(self.sections, key=lambda k: (self.order.get(k, 10**9), _id_key(k))):
                sections.append(self._view_section(self.sections[nid], terminal, outcome, by_node.get(nid, [])))
            notes = []
            if not self.saw_ui and (self.saw_output or terminal):
                notes.append(OLD_RUNTIME_NOTE)
            org = rec.get("organized") if isinstance(rec.get("organized"), dict) else None
            return {"job_id": self.job_id, "version": self.version, "terminal": terminal, "sections": sections,
                    "capabilities": {"node_kinds": self.saw_ui, "live": rec.get("preview_mode") == "live"},
                    "notes": notes, "organized": {"folder": org.get("folder"), "files": org.get("files"),
                                                  "method": org.get("method")} if org else None}

    def preview_owners(self) -> dict:
        """{delivered preview file name: (node_id, mask)} for the runner preview files this job's log attributes to
        a node (the Live tab labels delivered thumbnails with it). `mask` follows the section's live records, as in
        the per-node view."""
        self.refresh()
        out = {}
        with self._lock:
            for nid, sec in self.sections.items():
                masky = any(r.get("mask") for r in sec["live"])
                names = list(sec["path_previews"])
                for item in sec["ui_temp"]:
                    if item.get("prompt_id"):
                        name = _delivered_preview_name(f"_halofuse/previews/{item['prompt_id'][:8]}_{nid}_"
                                                       f"{item['filename']}")
                        if name:
                            names.append(name)
                for name in names:
                    out.setdefault(name, (str(nid), masky))
        return out

    def _out_ref(self, key: str, entry: dict) -> dict:
        parts = tuple(str(entry.get("local") or "").split("/"))
        return {"filename": parts[-1], "subfolder": "/".join((brand.OUTPUT_DIRNAME, self.job_id, *parts[:-1])),
                "type": "output", "size": entry.get("size"), "remote": key}

    def _attribute(self, manifest: dict):
        """(claimed remote keys, {node id: [(match, remote key, entry)]}) for delivered output files."""
        outputs = {k: e for k, e in manifest.items() if isinstance(e, dict) and e.get("category") == "output"}
        bare = {}
        for key in outputs:
            parts = key.split("/")
            rest = "/".join(parts[1:]) if len(parts) > 1 and _WF_RE.match(parts[0]) else key
            bare.setdefault(rest, []).append(key)
        by_node: dict[str, list] = {}
        claimed = set()
        for nid, sec in self.sections.items():
            for item in sec["ui_out"]:
                rel = f"{item['subfolder'].strip('/')}/{item['filename']}" if item["subfolder"] else item["filename"]
                for key in bare.get(rel, []):
                    if key not in claimed:
                        claimed.add(key)
                        by_node.setdefault(nid, []).append(("runner", key, outputs[key], item))
        prefixes = []
        for nid in self.sections:
            node = self.workflow.get(nid) or {}
            if is_vault(node.get("class_type")):
                continue
            prefix = (node.get("inputs") or {}).get("filename_prefix")
            if isinstance(prefix, str) and prefix.strip():
                prefixes.append((prefix.strip().replace("\\", "/").strip("/"), nid))
        for rest, keys in bare.items():
            for key in keys:
                if key in claimed:
                    continue
                hits = [(len(p), nid) for p, nid in prefixes if rest.startswith(p)]
                if not hits:
                    continue
                best = max(h[0] for h in hits)
                winners = {nid for n, nid in hits if n == best}
                if len(winners) == 1:
                    claimed.add(key)
                    by_node.setdefault(winners.pop(), []).append(("file_name", key, outputs[key], None))
        return claimed, by_node

    def _preview_exists(self, name: str) -> bool:
        try:
            return (self.job_dir / "previews" / name).is_file()
        except OSError:
            return False

    def _view_section(self, sec: dict, terminal: bool, outcome, delivered: list) -> dict:
        items = []
        if sec["frames"]:
            f = sec["frames"][-1]
            items.append({"kind": "frame", "preview": f["name"], "step": f.get("step"), "steps": f.get("steps"),
                          "count": sec["frame_count"], "source": "live", "w": f.get("w"), "h": f.get("h")})
        # full-size temp previews (PreviewImage / mask) the runner copied out; delivered at the end of the job
        full, pending, seen = [], [], set()
        for name in sec["path_previews"]:
            if self._preview_exists(name) and name not in seen:
                seen.add(name)
                full.append({"kind": kind_of_name(name), "preview": name, "source": "delivered"})
        vault_names = {i["filename"] for i in sec["ui_temp"] if i["key"] == "h2v_previews"}
        temp_seen = set()
        for item in sec["ui_temp"]:
            if item["key"] != "h2v_previews" and item["filename"] in vault_names:
                continue  # H2Vault lists the same file under core's `images` too; keep the labelled one
            if item["filename"] in temp_seen:
                continue
            temp_seen.add(item["filename"])
            name = _delivered_preview_name(f"_halofuse/previews/{item['prompt_id'][:8]}_{sec['node_id']}_"
                                           f"{item['filename']}") if item["prompt_id"] else None
            kind = item["kind"] if item["kind"] in ("image", "video", "audio") else \
                kind_of_name(item["filename"], item["key"])
            if name and name in seen:
                continue
            if name and self._preview_exists(name):
                seen.add(name)
                full.append({"kind": kind, "preview": name, "source": "delivered", "label": item["label"]})
            else:
                pending.append({"kind": kind, "filename": item["filename"], "pending": True, "source": "runner",
                                "label": item["label"]})
        masky = any(r.get("mask") for r in sec["live"])
        for item in full:
            if masky and item["kind"] == "image":
                item["kind"] = "mask"
        if full:
            items += full                # the full-size files replace the small live thumbnails
            items += [{"kind": "image", "preview": r["name"], "source": "live", "label": "4 frames of the video",
                       "w": r.get("w"), "h": r.get("h")} for r in sec["live"] if r.get("source_kind") == "video"]
        else:
            for r in sec["live"]:
                label = "4 frames of the video" if r.get("source_kind") == "video" else None
                items.append({"kind": "mask" if r.get("mask") else "image", "preview": r["name"], "source": "live",
                              "label": label, "w": r.get("w"), "h": r.get("h")})
            if not sec["live"]:
                items += pending
        delivered_keys = set()
        for match, key, entry, item in delivered:
            delivered_keys.add(key)
            ref = self._out_ref(key, entry)
            kind = kind_of_name(ref["filename"], (item or {}).get("key"))
            items.append({"kind": kind, **ref, "match": match, "source": "delivered",
                          "label": (item or {}).get("label")})
        names_done = {i.get("filename") for i in items if i.get("source") == "delivered"}
        for item in sec["ui_out"]:
            if item["filename"] in names_done:
                continue
            names_done.add(item["filename"])
            items.append({"kind": kind_of_name(item["filename"], item["key"]), "filename": item["filename"],
                          "pending": True, "match": "runner", "source": "runner", "label": item["label"]})
        for t in sec["texts"]:
            items.append({"kind": "text", "text": t, "source": "runner"})
        items = [{k: v for k, v in i.items() if v is not None} for i in items][-MAX_ITEMS:]

        state = sec["state"]
        if terminal and state == "waiting":
            state = "not_run"
        elif terminal and state == "running":
            state = "done" if outcome == "succeeded" else "failed"
        text = STATE_TEXT.get(state, state)
        if state == "running" and sec["step"] is not None and sec["steps"]:
            text = f"Running - step {sec['step']} of {sec['steps']}"
        if state == "failed":
            text = f"Failed: {sec['error']}" if sec["error"] else \
                f"Stopped when the render ended ({outcome or 'not finished'})"
        earlier = None
        real = [i for i in items if not i.get("pending")]
        if state == "cached" and not real:
            mem = self.memory.get(input_hash(self.workflow, sec["node_id"]))
            if mem and mem.get("job_id") and mem.get("job_id") != self.job_id:
                earlier = mem["job_id"]
                # the cached node's files were made by the earlier job: show those, not this job's pending names
                items = [dict(i, job_id=earlier, source="earlier_job") for i in mem.get("items") or []] + \
                    [i for i in items if not i.get("pending")]
                text = f"Cached - shows the last result (from job {earlier[:8]})"
            else:
                text = "Cached - no earlier result of this node on this computer"
        notes = []
        if sec["role"] == "vault":
            notes.append(VAULT_NOTE)
        for key in sec["unknown"]:
            notes.append(f"Node {sec['title']} sent '{key}' output, which HaloFuse cannot show here.")
        if not items and state == "done" and sec["role"] not in ("vault",):
            notes.append("This node finished without anything HaloFuse can show.")
        return {"node_id": sec["node_id"], "title": sec["title"], "class_type": sec["class_type"],
                "role": sec["role"], "state": state, "state_text": text, "step": sec["step"], "steps": sec["steps"],
                "items": items, "notes": notes, "earlier_job": earlier, "frames": sec["frame_count"]}


def _delivered_preview_name(runner_path: str):
    """The name delivery gives a runner preview file (`_halofuse/previews/<name>` -> previews/<name>)."""
    from . import delivery
    parts = [p for p in str(runner_path).replace("\\", "/").split("/") if p]
    if len(parts) < 3 or parts[0] != delivery.RUNNER_DIR or parts[1] != "previews" or parts[2] == "live":
        return None
    name = delivery._preview_name(delivery.sanitize_rel_parts(parts[2:]))
    return "runner_" + name if name.lower().startswith("live_") else name


# ---- registry + memory recording ----------------------------------------------------------------------------------

_REG_LOCK = threading.Lock()
_BOARDS: "OrderedDict[tuple, NodeBoard]" = OrderedDict()
_MEMORIES: dict[str, NodeMemory] = {}
MAX_BOARDS = 32


def board_for(job_id: str, store=None, *, defs=None) -> NodeBoard:
    if store is None:
        from .job_store import default_store
        store = default_store()
    key = (str(store.root), job_id)
    with _REG_LOCK:
        board = _BOARDS.get(key)
        if board is not None:
            _BOARDS.move_to_end(key)
            return board
    board = NodeBoard(job_id, store, defs=defs)
    with _REG_LOCK:
        _BOARDS[key] = board
        while len(_BOARDS) > MAX_BOARDS:
            _BOARDS.popitem(last=False)
    return board


def remember(job_id: str, store, *, defs=None) -> int:
    """Record the shown items of every section that really ran in this job (not cached, not pending)."""
    board = NodeBoard(job_id, store, defs=defs) if defs is not None else board_for(job_id, store)
    view = board.view()
    n = 0
    for sec in view["sections"]:
        if sec["state"] != "done":
            continue
        items = [{**i, "job_id": job_id} for i in sec["items"]
                 if not i.get("pending") and i.get("source") != "earlier_job"]
        if not items:
            continue
        board.memory.put(input_hash(board.workflow, sec["node_id"]),
                         {"job_id": job_id, "node_id": sec["node_id"], "items": items[-8:]})
        n += 1
    return n


def reset_for_tests() -> None:
    with _REG_LOCK:
        _BOARDS.clear()
        _MEMORIES.clear()

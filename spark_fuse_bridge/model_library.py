"""The HaloFuse cloud model library as a first-class thing (C1, C2, C6): listing with sizes, local-vs-cloud
comparison, duplicates, content hashes recorded at upload, and explicit model aliases.

What the service allows (CloudBackend port, contract C-1, SDK 0.5.0): listing (`library_list`, PROPFIND depth 1),
existence + size (`library_stat`) and whole-file upload (`library_upload`). It offers no delete, no server-side
checksum or ETag, no resumable upload and no cloud-side download from a source URL (that needs provider probe P8).
Those capabilities are reported as unavailable with the reason; nothing pretends to do them.

Content hashes: the SHA-256 of a file is computed on this computer when HaloFuse uploads it and recorded in a local
manifest next to the settings (path, size, sha256, time). A library file is "sha256" verified only when the local
file's (cached) hash equals the recorded upload hash and the cloud size matches; otherwise it is "size" matched or
unverified. Hashes are never computed during a listing request (a 66 GB file takes minutes to hash).

Aliases (C2): an explicit, artist-made mapping "this local file is that cloud file" for one model folder. Applying
aliases to an outgoing workflow returns a copy plus the list of changes; nothing is ever substituted silently.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

from . import brand
from .workflow_analysis import MODEL_EXTS, ModelIndex, cloud_folder, local_model_index

_log = logging.getLogger(__name__)

MANIFEST_FILE = "halofuse_library_manifest.json"
HASH_CACHE_FILE = "halofuse_hash_cache.json"
ALIASES_FILE = "halofuse_model_aliases.json"
MAX_LIST_DEPTH = 3
MAX_LIST_ENTRIES = 5000
_HASH_CHUNK = 8 * 1024 * 1024
_NAME_RE = re.compile(r"^[^\x00\\:]{1,512}$")
_file_lock = threading.Lock()

CAPABILITIES = {
    "list": {"supported": True},
    "upload": {"supported": True, "resumable": False,
               "note": "Uploads restart from zero when interrupted; the service has no resume protocol."},
    "delete": {"supported": False, "note": f"Deleting library files is not available through {brand.PRODUCT} yet; "
                                           "ask your administrator."},
    "server_checksum": {"supported": False, "note": "The library stores no checksum; HaloFuse records SHA-256 at "
                                                    "upload time on this computer."},
    "cloud_side_download": {"supported": False, "note": "Fetching a model from its original source directly into "
                                                        "the cloud library is not available yet."},
}


def _settings_file(name: str) -> Path:
    from .config import settings_dir

    return Path(settings_dir()) / name


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


# ---- hashes ----------------------------------------------------------------------------------------------------

def sha256_file(path: Path, *, cancel=None) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        while True:
            if cancel is not None and cancel():
                raise InterruptedError("hashing cancelled")
            chunk = fh.read(_HASH_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def cached_hash(path: Path, *, cache_path: Path | None = None) -> str | None:
    """The cached SHA-256 of *path* when its size and mtime still match the cache entry; never computes."""
    try:
        st = Path(path).stat()
    except OSError:
        return None
    cache = _read_json(cache_path or _settings_file(HASH_CACHE_FILE), {})
    entry = cache.get(str(path)) if isinstance(cache, dict) else None
    if isinstance(entry, dict) and entry.get("size") == st.st_size and entry.get("mtime_ns") == st.st_mtime_ns:
        return entry.get("sha256")
    return None


def hash_and_cache(path: Path, *, cache_path: Path | None = None, cancel=None) -> str:
    """SHA-256 of *path*, reusing the cache when size + mtime are unchanged."""
    known = cached_hash(path, cache_path=cache_path)
    if known:
        return known
    st = Path(path).stat()
    value = sha256_file(path, cancel=cancel)
    cache_path = cache_path or _settings_file(HASH_CACHE_FILE)
    with _file_lock:
        cache = _read_json(cache_path, {})
        cache = cache if isinstance(cache, dict) else {}
        cache[str(path)] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": value}
        _write_json(cache_path, cache)
    return value


def record_upload(remote_path: str, size: int, sha256: str, *, manifest_path: Path | None = None) -> None:
    manifest_path = manifest_path or _settings_file(MANIFEST_FILE)
    with _file_lock:
        data = _read_json(manifest_path, {})
        data = data if isinstance(data, dict) else {}
        data.setdefault("files", {})[remote_path] = {"size": size, "sha256": sha256, "uploaded_at": time.time()}
        data["schema"] = "halofuse.library_manifest/1"
        _write_json(manifest_path, data)


def load_manifest(manifest_path: Path | None = None) -> dict:
    data = _read_json(manifest_path or _settings_file(MANIFEST_FILE), {})
    files = data.get("files") if isinstance(data, dict) else None
    return files if isinstance(files, dict) else {}


# ---- listing and comparison ------------------------------------------------------------------------------------

def _rel_parts(assets_path: str) -> list[str]:
    return [p for p in str(assets_path or "").split("/") if p]


def list_cloud(be, settings: dict, *, max_depth: int = MAX_LIST_DEPTH,
               max_entries: int = MAX_LIST_ENTRIES) -> tuple[list[dict], bool]:
    """Files under the configured assets path: [{folder, name, size, remote_path}], truncated flag. Breadth-first,
    at most *max_depth* folder levels below each model folder and *max_entries* entries in total."""
    space = settings.get("assets_share_sync_space_name") or None
    root = "/".join(_rel_parts(settings.get("assets_share_sync_path")))
    out: list[dict] = []
    queue: list[tuple[str, int]] = [(root, 0)]
    seen = 0
    truncated = False
    while queue:
        directory, depth = queue.pop(0)
        for entry in be.library_list(directory, space):
            seen += 1
            if seen > max_entries:
                return out, True
            rel = entry.remote_path[len(root) + 1:] if root else entry.remote_path
            parts = [p for p in rel.split("/") if p]
            if not parts or any(p in (".", "..") for p in parts):
                continue
            if entry.is_dir:
                if depth < max_depth:
                    queue.append((entry.remote_path, depth + 1))
                else:
                    truncated = True
                continue
            if len(parts) < 2:
                continue  # a loose file at the assets root is not in a model folder
            out.append({"folder": parts[0], "name": "/".join(parts[1:]), "size": entry.size,
                        "remote_path": entry.remote_path})
    return out, truncated


def list_local(index: ModelIndex | None = None) -> list[dict]:
    """Model files this ComfyUI install has, by canonical cloud folder: [{folder, name, size}]."""
    index = index if index is not None else local_model_index()
    out = []
    try:
        import folder_paths  # ComfyUI-provided
    except Exception:  # noqa: BLE001
        folder_paths = None
    for name, keys in sorted(index.by_name.items()):
        key = keys[0]
        size = None
        if folder_paths is not None:
            try:
                path = folder_paths.get_full_path(key, name)
                size = Path(path).stat().st_size if path else None
            except Exception:  # noqa: BLE001
                size = None
        out.append({"folder": cloud_folder(key), "name": name, "size": size, "local_key": key})
    return out


def compare(cloud: list[dict], local: list[dict], *, manifest: dict | None = None, local_hash=None) -> dict:
    """Join cloud and local listings. Status: both | local_only | cloud_only | size_mismatch. `verified`:
    "sha256" / "size" / None (see module docstring). Duplicates: the same name and size in more than one folder."""
    manifest = manifest or {}
    by_key: dict = {}
    for c in cloud:
        by_key.setdefault((c["folder"], c["name"]), {})["cloud"] = c
    for loc in local:
        by_key.setdefault((loc["folder"], loc["name"]), {})["local"] = loc
    items = []
    for (folder, name), pair in sorted(by_key.items()):
        c, loc = pair.get("cloud"), pair.get("local")
        cloud_size = c["size"] if c else None
        local_size = loc["size"] if loc else None
        if c and loc:
            status = "both" if cloud_size == local_size or None in (cloud_size, local_size) else "size_mismatch"
        else:
            status = "cloud_only" if c else "local_only"
        verified = None
        if status == "both" and cloud_size is not None and cloud_size == local_size:
            verified = "size"
            recorded = manifest.get(c["remote_path"]) if c else None
            current = local_hash(loc) if (local_hash and loc) else None
            if recorded and current and recorded.get("sha256") == current and recorded.get("size") == cloud_size:
                verified = "sha256"
        items.append({"folder": folder, "name": name, "cloud_size": cloud_size, "local_size": local_size,
                      "status": status, "verified": verified})
    groups: dict = {}
    for it in items:
        size = it["cloud_size"] or it["local_size"]
        if size:
            groups.setdefault((it["name"].rsplit("/", 1)[-1], size), []).append(f"{it['folder']}/{it['name']}")
    duplicates = [{"name": n, "size": s, "paths": sorted(set(p))} for (n, s), p in sorted(groups.items())
                  if len(set(p)) > 1]
    totals = {
        "cloud_files": sum(1 for i in items if i["cloud_size"] is not None or i["status"] in ("both", "cloud_only")),
        "cloud_bytes": sum(i["cloud_size"] or 0 for i in items),
        "local_only_bytes": sum(i["local_size"] or 0 for i in items if i["status"] == "local_only"),
        "duplicate_bytes": sum(d["size"] * (len(d["paths"]) - 1) for d in duplicates),
    }
    return {"items": items, "duplicates": duplicates, "totals": totals}


def library_report(be, settings: dict, *, index: ModelIndex | None = None, manifest_path: Path | None = None,
                   cache_path: Path | None = None) -> dict:
    cloud, truncated = list_cloud(be, settings)
    local = list_local(index)

    def local_hash(loc):
        try:
            import folder_paths  # ComfyUI-provided
            path = folder_paths.get_full_path(loc.get("local_key") or loc["folder"], loc["name"])
        except Exception:  # noqa: BLE001
            return None
        return cached_hash(Path(path), cache_path=cache_path) if path else None

    report = compare(cloud, local, manifest=load_manifest(manifest_path), local_hash=local_hash)
    report["truncated"] = truncated
    report["capabilities"] = CAPABILITIES
    return report


# ---- aliases (C2) ----------------------------------------------------------------------------------------------

def _valid_name(name) -> bool:
    if not isinstance(name, str) or not _NAME_RE.match(name):
        return False
    parts = name.split("/")
    return all(p not in ("", ".", "..") for p in parts) and not name.startswith("/")


def validate_aliases(aliases) -> list[dict]:
    """[{folder, local_name, cloud_name}] with safe names and model extensions. Raises ValueError."""
    if not isinstance(aliases, list) or len(aliases) > 1000:
        raise ValueError("aliases must be a list of at most 1000 entries")
    out, seen = [], set()
    for entry in aliases:
        if not isinstance(entry, dict):
            raise ValueError("each alias must be an object")
        folder = entry.get("folder")
        local_name = str(entry.get("local_name") or "").replace("\\", "/")
        cloud_name = str(entry.get("cloud_name") or "").replace("\\", "/")
        if not isinstance(folder, str) or not re.match(r"^[A-Za-z0-9_.-]{1,64}$", folder):
            raise ValueError("alias folder is not a valid model folder name")
        if not (_valid_name(local_name) and _valid_name(cloud_name)):
            raise ValueError("alias names must be relative model file names")
        if not (local_name.lower().endswith(MODEL_EXTS) and cloud_name.lower().endswith(MODEL_EXTS)):
            raise ValueError("alias names must be model files")
        key = (folder, local_name)
        if key in seen:
            raise ValueError("an alias for this file already exists")
        seen.add(key)
        out.append({"folder": folder, "local_name": local_name, "cloud_name": cloud_name})
    return out


def load_aliases(path: Path | None = None) -> list[dict]:
    data = _read_json(path or _settings_file(ALIASES_FILE), {})
    try:
        return validate_aliases(data.get("aliases", []) if isinstance(data, dict) else [])
    except ValueError:
        _log.warning("model alias file is invalid and was ignored")
        return []


def save_aliases(aliases, path: Path | None = None) -> list[dict]:
    clean = validate_aliases(aliases)
    with _file_lock:
        _write_json(path or _settings_file(ALIASES_FILE), {"schema": "halofuse.model_aliases/1", "aliases": clean})
    return clean


def apply_aliases(workflow: dict, aliases: list[dict], *, node_defs=None, index=None) -> tuple[dict, list]:
    """A copy of *workflow* with every aliased model reference replaced by its cloud name, plus the changes
    [{node_id, input, from, to, folder}]. Only references discovery places in the alias's folder are changed."""
    from .workflow_analysis import discover_models

    out = copy.deepcopy(workflow)
    table = {(a["folder"], a["local_name"]): a["cloud_name"] for a in aliases}
    changes = []
    if not table:
        return out, changes
    for node_id, node in out.items():
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            continue
        refs = discover_models({node_id: node}, node_defs=node_defs, index=index)
        for ref in refs:
            target = table.get((ref.folder, ref.name))
            if target is None:
                continue
            for field_name, value in node["inputs"].items():
                if isinstance(value, str) and value.replace("\\", "/").strip("/") == ref.name:
                    node["inputs"][field_name] = target
                    changes.append({"node_id": node_id, "input": field_name, "from": value, "to": target,
                                    "folder": ref.folder})
    return out, changes

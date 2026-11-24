"""Model cache status and partial-download cleanup on this computer (L9.340).

No network and no disk scan: only files named in HaloFuse's own registries are looked at (the cloud model library
manifest of model_library, the partial-download registry of model_fetch). Nothing returned holds a local path, and no
function raises to its caller because one file could not be read (that file is skipped).

cleanup_partials deletes ONLY ``<target>.halofuse-part`` files of the partial-download registry that no download is
using; a complete model file is never deleted, even when the registry was edited to name one.
"""
from __future__ import annotations

import logging
import math
import os
import time
from pathlib import Path

from . import model_fetch, transfer_pool

_log = logging.getLogger(__name__)

ABANDONED_AFTER_S = 7 * 86400
_clock = time.time                 # replaced in tests


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _active_part_keys() -> set[str]:
    """Registry keys (normcased target paths) of the files a queued or running download is using."""
    keys = set()
    with model_fetch._FETCH_LOCK:
        for state in model_fetch._FETCHES.values():
            if state.get("status") not in ("queued", "running"):
                continue
            for f in state.get("files") or ():
                target = f.get("_target")
                if target and f.get("status") in ("queued", "downloading", "verifying"):
                    keys.add(os.path.normcase(str(target)))
    return keys


def _is_partial_file(path: Path) -> bool:
    """The deletion guard: a regular file (not a link) whose name ends with the partial-download suffix."""
    try:
        return path.name.endswith(model_fetch.PART_SUFFIX) and path.is_file() and not path.is_symlink()
    except (OSError, ValueError):                     # ValueError: a NUL byte in a tampered registry key
        return False


def _partials() -> list[dict]:
    """[{key, path, name, bytes, age_s, active}] for every registered partial whose part file can be read."""
    try:
        registry = model_fetch._partials()
        active = _active_part_keys()
    except Exception:  # noqa: BLE001 - an unreadable registry lists nothing
        return []
    now = _clock()
    out = []
    for key, meta in registry.items():
        if not isinstance(key, str) or not key:
            continue
        part = Path(key + model_fetch.PART_SUFFIX)
        try:
            st = part.stat()
            real = part.resolve(strict=True).name     # the real case on Windows (the key is normcased)
        except (OSError, ValueError, RuntimeError):
            continue                                  # no part file (or unreadable): nothing to show or delete
        suffix = model_fetch.PART_SUFFIX
        name = real[: -len(suffix)] if real.lower().endswith(suffix) else Path(key).name   # a link: its own name
        started = meta.get("started") if isinstance(meta, dict) else None
        since = float(started) if _number(started) and math.isfinite(started) else st.st_mtime
        out.append({"key": key, "path": part, "name": name, "bytes": int(st.st_size),
                    "age_s": max(0.0, now - since), "active": key in active})
    return out


def _upload_files() -> list[dict]:
    from . import model_sync

    return model_sync._active_upload_files()


def status() -> dict:
    """What the model cache on this computer holds and what is moving (see the module docstring)."""
    try:
        from . import model_library

        manifest = model_library.load_manifest()
    except Exception:  # noqa: BLE001 - no settings folder / unreadable manifest
        manifest = {}
    entries = [e for e in manifest.values() if isinstance(e, dict)]
    partials = _partials()
    try:
        downloads = [{k: f.get(k) for k in ("folder", "name", "received", "size", "status")}
                     for f in model_fetch.active_fetch_files()]
    except Exception:  # noqa: BLE001
        downloads = []
    try:
        uploads = [{k: f.get(k) for k in ("folder", "name", "sent", "size", "status")} for f in _upload_files()]
    except Exception:  # noqa: BLE001
        uploads = []
    return {
        "library_files": len(manifest),
        "library_bytes": sum(int(e["size"]) for e in entries if _number(e.get("size"))),
        "verified_files": sum(1 for e in entries if isinstance(e.get("sha256"), str) and e["sha256"]),
        "partials": [{k: p[k] for k in ("name", "bytes", "age_s", "active")} for p in partials],
        "partial_bytes": sum(p["bytes"] for p in partials),
        "abandoned": sum(1 for p in partials if not p["active"] and p["age_s"] > ABANDONED_AFTER_S),
        "transfers": transfer_pool.status(),
        "downloads": downloads,
        "uploads": uploads,
    }


def _still_active(key: str) -> bool:
    """Checked again right before a delete: a download of the same file may have started since the listing."""
    try:
        return key in _active_part_keys()
    except Exception:  # noqa: BLE001 - when in doubt, keep the file
        return True


def cleanup_partials(only_abandoned: bool = True) -> dict:
    """Delete registered partial downloads no download is using (with *only_abandoned*: only those older than
    ABANDONED_AFTER_S) and drop their registry entries. Returns {"removed": n, "freed_bytes": b}."""
    removed = freed = 0
    for p in _partials():
        if p["active"] or (only_abandoned and p["age_s"] <= ABANDONED_AFTER_S):
            continue
        path: Path = p["path"]
        if not _is_partial_file(path) or _still_active(p["key"]):
            continue
        try:
            path.unlink()
        except OSError:
            _log.info("a partial download could not be deleted (in use?); it is kept")
            continue
        if not _still_active(p["key"]):               # a download that started meanwhile keeps its registry entry
            model_fetch._save_partial(p["key"], None)
        removed += 1
        freed += p["bytes"]
    return {"removed": removed, "freed_bytes": freed}

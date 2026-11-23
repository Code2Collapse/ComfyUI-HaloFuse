"""Automatic delivery of a job's outputs and logs into <ComfyUI output>/HaloFuse/<job_id>/ (NEW_WORK s4, WS3).

One mechanism for every path that brings files home: the lifecycle terminal hook (every outcome, so partial results of
failed / cancelled / timed-out jobs arrive too), the Retry button, manual recovery of an older job and the 0.3.x
``output_download`` routes (now a shim over this module).

    <job_dir>/outputs/...            every remote output file, remote tree and names kept (sanitised, never escaping)
    <job_dir>/logs/render.log        the runner's own log (/output/_halofuse/render.log), redacted line by line
    <job_dir>/logs/events.runner.jsonl, logs/runner/...   other runner files, redacted
    <job_dir>/previews/...           runner preview files (flat, strict names)
    <job_dir>/.halofuse/manifest.json + partial/   private: what was delivered (size, sha256) and resumable parts

Transfers go through the C-1 port only (``lifecycle.open_backend``): ``walk_outputs`` + ``download(offset=...)``.
Every file lands through a temporary part file and an atomic ``os.replace``; a dropped stream resumes from the bytes
already on disk (Range when the server honours it, otherwise from 0); the size is verified against the listing and a
sha256 is recorded. A second delivery downloads nothing that is already present (no ``_1`` copies, X8).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import brand, lifecycle
from .job_store import HaloFuseError, JobStore, atomic_write_json, default_store, validate_job_id
from .provider.redact import redact

_log = logging.getLogger(__name__)

OUTPUTS_DIR = "outputs"
LOGS_DIR = "logs"
PREVIEWS_DIR = "previews"
STATE_DIR = ".halofuse"
MANIFEST = f"{STATE_DIR}/manifest.json"
RUNNER_DIR = "_halofuse"           # reserved runner folder in the cloud /output (C-3 section 4); never an output
UNATTRIBUTED_DIR = "unattributed"

MAX_ERRORS = 50
PERSIST_INTERVAL_S = 1.0
PROGRESS_EVENT_INTERVAL_S = 5.0
OUTPUT_LAG_CHECKS = 3
MAX_PART_BYTES = 200
MAX_PARTS = 32

_NO_RETRY = frozenset({"not_found", "forbidden", "unsafe_name"})
_REDACTED_SUFFIXES = (".log", ".txt", ".jsonl", ".json")
_WF_RE = re.compile(r"wf_(\d+)")
_PREVIEW_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
_BAD_CHARS_RE = re.compile(r'[\x00-\x1f\x7f<>:"|?*]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}

MESSAGES = {
    "size_mismatch": "HaloFuse received an incomplete copy of this file; it is fetched again on the next retry.",
    "transfer_failed": "HaloFuse could not download this file. Use Retry delivery to resume it.",
    "listing_failed": "HaloFuse could not list the job's outputs. Use Retry delivery to try again.",
    "write_failed": "HaloFuse could not save this file in the ComfyUI output folder (disk full or no permission?).",
    "unsafe_name": "HaloFuse skipped a file whose name could not be stored safely.",
    "not_found": "This file is no longer available in HaloFuse cloud storage.",
    "job_not_found": "HaloFuse has no job with that id on this computer or in your account.",
    "job_not_terminal": "This job is still running. HaloFuse brings its outputs in automatically when it ends.",
    "bad_request": "That is not a valid HaloFuse job id.",
}

_EXT_KIND = {
    **dict.fromkeys(("png", "jpg", "jpeg", "webp", "gif", "exr", "tif", "tiff", "bmp", "hdr", "dpx"), "image"),
    **dict.fromkeys(("mp4", "webm", "mov", "mkv", "avi", "m4v"), "video"),
    **dict.fromkeys(("wav", "mp3", "flac", "ogg", "m4a", "aac", "opus"), "audio"),
    "latent": "latent",
}

# ---- process state -------------------------------------------------------------------------------------------------

_REG_LOCK = threading.Lock()
_JOB_LOCKS: dict[str, threading.Lock] = {}
_THREADS: dict[str, threading.Thread] = {}
_LIVE: dict[str, dict] = {}


def _job_lock(job_id: str) -> threading.Lock:
    with _REG_LOCK:
        return _JOB_LOCKS.setdefault(job_id, threading.Lock())


def _store(store: JobStore | None) -> JobStore:
    return store if store is not None else default_store()


def _message(code: str) -> str:
    return MESSAGES.get(code) or brand.message(code)


# ---- names ---------------------------------------------------------------------------------------------------------

def _sanitize_piece(piece: str) -> str:
    original = piece
    piece = _BAD_CHARS_RE.sub("_", piece).rstrip(". ")
    if not piece or set(piece) <= {"."}:
        return "_"
    if piece.split(".", 1)[0].upper() in _RESERVED:
        piece = "_" + piece
    if len(piece.encode("utf-8")) > MAX_PART_BYTES:
        stem, dot, ext = piece.rpartition(".")
        if not dot or not stem or len(ext) > 16:
            stem, ext = piece, ""
        tag = "~" + hashlib.sha1(original.encode("utf-8", "surrogatepass")).hexdigest()[:8]
        tail = (tag + "." + ext) if ext else tag
        budget = MAX_PART_BYTES - len(tail.encode("utf-8"))
        stem = stem.encode("utf-8")[:budget].decode("utf-8", "ignore")
        piece = stem + tail
    return piece


def sanitize_rel_parts(parts) -> tuple[str, ...]:
    """Remote relative path parts -> safe local parts: no traversal, no separators, no device names, bounded length.

    Ordinary names (spaces, brackets, +, unicode) are kept unchanged.
    """
    pieces: list[str] = []
    for part in parts if isinstance(parts, (list, tuple)) else (parts,):
        for piece in re.split(r"[/\\]", str(part)):
            if piece:
                pieces.append(_sanitize_piece(piece))
    if not pieces:
        return ("_",)
    if len(pieces) > MAX_PARTS:
        pieces = pieces[: MAX_PARTS - 1] + [_sanitize_piece("_".join(pieces[MAX_PARTS - 1:]))]
    return tuple(pieces)


def _preview_name(parts: tuple[str, ...]) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]", "_", "_".join(parts)).strip(".") or "preview"
    if len(name) > 120:
        stem, dot, ext = name.rpartition(".")
        ext = ext[:16] if dot else ""
        tag = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
        name = (stem if dot else name)[: 120 - len(ext) - 10] + "~" + tag + ("." + ext if ext else "")
    return name


def _kind(rel_parts: tuple[str, ...]) -> str:
    lowered = [p.lower() for p in rel_parts]
    if any("mask" in p for p in lowered):
        return "mask"
    if any("depth" in p for p in lowered):
        return "depth"
    return _EXT_KIND.get(lowered[-1].rpartition(".")[2], "other") if "." in lowered[-1] else "other"


def _with_counter(rel: str, n: int) -> str:
    head, _, last = rel.rpartition("/")
    stem, dot, ext = last.rpartition(".")
    last = f"{stem} ({n}).{ext}" if dot and stem else f"{last} ({n})"
    return f"{head}/{last}" if head else last


def _contained(job_dir: Path, rel: str) -> Path:
    base = job_dir.resolve()
    dest = (job_dir / rel).resolve()
    if os.path.commonpath([str(base), str(dest)]) != str(base) or dest == base:
        raise HaloFuseError("unsafe_name", _message("unsafe_name"), 400)
    return job_dir / rel


# ---- planning --------------------------------------------------------------------------------------------------------

def _plan_one(rf, is_chunk: bool) -> tuple[str, str]:
    """(category, local rel path below the job dir) for one remote file."""
    parts = tuple(rf.rel_parts)
    if parts and parts[0] == RUNNER_DIR:
        rest = parts[1:]
        if rest == ("render.log",):
            return "log", f"{LOGS_DIR}/render.log"
        if rest == ("events.jsonl",):
            return "log", f"{LOGS_DIR}/events.runner.jsonl"
        if rest[:1] == ("previews",) and len(rest) > 1:
            name = _preview_name(sanitize_rel_parts(rest[1:]))
            if name.lower().startswith("live_"):
                name = "runner_" + name  # live_* names belong to the bridge's live writer (C-4 6a)
            return "preview", f"{PREVIEWS_DIR}/{name}"
        return "runner", f"{LOGS_DIR}/runner/" + "/".join(sanitize_rel_parts(rest))
    safe = sanitize_rel_parts(parts)
    if is_chunk and not (len(safe) > 1 and _WF_RE.fullmatch(safe[0])):
        safe = (UNATTRIBUTED_DIR, *safe)
    return "output", f"{OUTPUTS_DIR}/" + "/".join(safe)


def _assign(remote, manifest: dict, is_chunk: bool) -> list[tuple[object, str, str, str]]:
    """[(remote_file, key, category, local rel)] with stable, collision-free local paths."""
    taken: dict[str, str] = {}      # casefolded local rel -> key
    out: list = []
    pending: list = []
    for rf in sorted(remote, key=lambda f: tuple(f.rel_parts)):
        key = "/".join(rf.rel_parts)
        category, rel = _plan_one(rf, is_chunk)
        prev = (manifest.get(key) or {}).get("local")
        if prev and prev.casefold() not in taken:
            taken[prev.casefold()] = key
            out.append((rf, key, category, prev))
        else:
            pending.append((rf, key, category, rel))
    for rf, key, category, rel in pending:
        candidate, n = rel, 2
        while candidate.casefold() in taken:
            candidate = _with_counter(rel, n)
            n += 1
        taken[candidate.casefold()] = key
        out.append((rf, key, category, candidate))
    return out


# ---- manifest --------------------------------------------------------------------------------------------------------

def _load_manifest(job_dir: Path) -> dict:
    try:
        data = json.loads((job_dir / MANIFEST).read_text(encoding="utf-8"))
        return data.get("files", {}) if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_manifest(job_dir: Path, files: dict) -> None:
    atomic_write_json(job_dir / MANIFEST, {"schema": 1, "files": files})


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# ---- one delivery run -------------------------------------------------------------------------------------------------

class _Run:
    def __init__(self, store: JobStore, job_id: str, outcome, max_attempts: int, retry_sleep_s: float):
        self.store = store
        self.job_id = job_id
        self.rec = store.get(job_id) or {}
        self.outcome = outcome or self.rec.get("outcome")
        self.max_attempts = max(1, int(max_attempts))
        self.retry_sleep_s = retry_sleep_s
        self.job_dir = store.job_dir(job_id)
        self.is_chunk = self.rec.get("kind") == "chunk"
        prev = self.rec.get("delivery") if isinstance(self.rec.get("delivery"), dict) else {}
        self.status = {
            "state": "downloading", "partial": False, "outcome": self.outcome,
            "files_total": 0, "files_done": 0, "bytes_total": 0, "bytes_done": 0, "outputs_count": 0,
            "folder": {"subfolder": f"{brand.OUTPUT_DIRNAME}/{job_id}", "type": "output"},
            "errors": [], "items": [], "unattributed": 0,
            "started_at": time.time(), "finished_at": None, "updated_at": time.time(),
            "attempt": int(prev.get("attempt") or 0) + 1, "verification": "size+sha256", "manifest": MANIFEST,
        }
        self._last_persist = 0.0
        self._last_event = 0.0
        self._last_manifest = 0.0

    # -- status --
    def _persist(self, force: bool = False) -> None:
        now = time.time()
        self.status["updated_at"] = now
        _LIVE[self.job_id] = json.loads(json.dumps(self.status))
        if force or now - self._last_persist >= PERSIST_INTERVAL_S:
            self._last_persist = now
            self.store.update(self.job_id, delivery=self.status)

    def _event(self, text: str, level: str = "info", data: dict | None = None) -> None:
        self.store.append_event(self.job_id, "delivery", text=text, level=level, data=data)

    def _error(self, rel: str, code: str) -> None:
        errors = self.status["errors"]
        if len(errors) < MAX_ERRORS:
            errors.append({"rel_path": rel, "code": code, "message": _message(code)})
        self._event(f"Could not deliver {rel}: {_message(code)}", level="error", data={"code": code, "rel_path": rel})

    def _progress(self, done_in_file: int) -> None:
        now = time.time()
        live_done = self.status["bytes_done"] + done_in_file
        if now - self._last_persist >= PERSIST_INTERVAL_S:
            snapshot = dict(self.status, bytes_done=live_done)
            _LIVE[self.job_id] = snapshot
        if now - self._last_event >= PROGRESS_EVENT_INTERVAL_S:
            self._last_event = now
            self._event(f"Downloading outputs: {self.status['files_done']}/{self.status['files_total']} files, "
                        f"{_fmt_bytes(live_done)} of {_fmt_bytes(self.status['bytes_total'])}",
                        data={"phase": "progress", "files_done": self.status["files_done"],
                              "bytes_done": live_done})

    # -- main --
    def run(self) -> dict:
        if self.rec.get("state") == "finalizing":
            try:
                lifecycle.transition(self.store, self.job_id, "downloading")
            except Exception:  # noqa: BLE001 - the lifecycle owns the state; delivery never blocks on it
                pass
        self._persist(force=True)
        cloud_ids = [c for c in ((self.rec.get("cloud") or {}).get("job_ids") or []) if isinstance(c, str) and c]
        manifest = _load_manifest(self.job_dir)
        try:
            remote = self._list(cloud_ids) if cloud_ids else []
        except Exception as exc:  # noqa: BLE001
            err = lifecycle.as_halofuse_error(exc)
            _log.info("delivery listing for %s failed: %s", self.job_id, err.code)
            self._error(OUTPUTS_DIR, "listing_failed")
            return self._finish(manifest, listing_failed=True)
        # the runner's live ring (previews/live/) already reached the job folder during the run (C-4 6a)
        remote = [rf for rf in remote if tuple(rf.rel_parts)[:3] != (RUNNER_DIR, "previews", "live")]
        plan = _assign(remote, manifest, self.is_chunk)
        self.status["files_total"] = len(plan)
        self.status["bytes_total"] = sum(int(rf.size or 0) for rf, *_ in plan)
        self._event(f"Downloading outputs: {len(plan)} files ({_fmt_bytes(self.status['bytes_total'])}) "
                    f"to {self.status['folder']['subfolder']}.", data={"phase": "started", "files": len(plan)})
        self._persist(force=True)
        backend = None
        try:
            for rf, key, category, rel in plan:
                if backend is None and not self._already_there(rf, key, rel, category, manifest):
                    backend = lifecycle.open_backend(None)
                if self._deliver_file(backend, rf, key, category, rel, manifest):
                    self.status["files_done"] += 1
                    self.status["bytes_done"] += int(manifest[key]["size"])
                    if category == "output":
                        self.status["outputs_count"] += 1
                self._persist()
        except HaloFuseError as exc:   # no backend (not configured / no credentials): nothing more can be fetched
            done_keys = {k for _rf, k, *_ in plan if k in manifest}
            for _rf, key, _c, rel in plan:
                if key not in done_keys:
                    self._error(rel, "transfer_failed")
            _log.info("delivery for %s stopped: %s", self.job_id, exc.code)
        finally:
            if backend is not None:
                lifecycle.close_backend(backend)
        return self._finish(manifest)

    def _list(self, cloud_ids: list[str]) -> list:
        backend = lifecycle.open_backend(None)
        try:
            remote = []
            for cid in cloud_ids:
                files = backend.walk_outputs(cid)
                checks = 0
                while not files and checks < OUTPUT_LAG_CHECKS and getattr(backend.get(cid), "has_output", False):
                    checks += 1           # the output folder can lag the terminal status by a few seconds
                    time.sleep(self.retry_sleep_s)
                    files = backend.walk_outputs(cid)
                remote.extend(files)
            return remote
        finally:
            lifecycle.close_backend(backend)

    def _already_there(self, rf, key: str, rel: str, category: str, manifest: dict) -> bool:
        entry = manifest.get(key)
        try:
            dest = _contained(self.job_dir, rel)
        except HaloFuseError:
            return False
        if not dest.is_file():
            return False
        size = dest.stat().st_size
        if entry and entry.get("local") == rel:
            if rf.size is None or entry.get("remote_size") == rf.size:
                return True
            return False
        if category == "output" and rf.size is not None and size == rf.size:
            manifest[key] = {"local": rel, "size": size, "remote_size": rf.size, "sha256": _sha256(dest),
                             "category": category, "delivered_at": time.time()}
            return True
        return False

    def _deliver_file(self, backend, rf, key: str, category: str, rel: str, manifest: dict) -> bool:
        if self._already_there(rf, key, rel, category, manifest):
            return True
        try:
            dest = _contained(self.job_dir, rel)
        except HaloFuseError:
            self._error(rel, "unsafe_name")
            return False
        part_dir = self.job_dir / STATE_DIR / "partial"
        part_dir.mkdir(parents=True, exist_ok=True)
        part = part_dir / (hashlib.sha256(f"{key}|{rf.size}|{rf.etag}".encode("utf-8")).hexdigest()[:32] + ".part")
        code = "transfer_failed"
        for attempt in range(1, self.max_attempts + 1):
            if attempt > 1 and self.retry_sleep_s:
                time.sleep(self.retry_sleep_s)
            offset = 0
            if part.exists():
                have = part.stat().st_size
                if rf.size is not None and 0 < have < rf.size:
                    offset = have
                else:
                    part.unlink()
            try:
                backend.download(rf, part, offset=offset, on_progress=lambda done, _total: self._progress(done))
            except Exception as exc:  # noqa: BLE001 - keep the partial: the next attempt resumes it
                err = lifecycle.as_halofuse_error(exc)
                code = "not_found" if err.code == "not_found" else "transfer_failed"
                _log.info("delivery of a file for %s failed (%s), attempt %d", self.job_id, err.code, attempt)
                if err.code in _NO_RETRY:
                    break
                continue
            got = part.stat().st_size if part.exists() else -1
            if rf.size is not None and got != rf.size:
                code = "size_mismatch"
                part.unlink(missing_ok=True)
                continue
            try:
                self._place(part, dest, category)
            except OSError:
                code = "write_failed"
                break
            manifest[key] = {"local": rel, "size": dest.stat().st_size, "remote_size": rf.size,
                             "sha256": _sha256(dest), "category": category, "delivered_at": time.time()}
            self._save(manifest)
            return True
        self._error(rel, code)
        return False

    def _place(self, part: Path, dest: Path, category: str) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if category == "log" and dest.name == "render.log":
            self._keep_streamed_copy(dest)
        if category in ("log", "runner") and dest.suffix.lower() in _REDACTED_SUFFIXES:
            tmp = dest.with_name(f".{dest.name}.tmp")
            with open(part, "r", encoding="utf-8", errors="replace", newline="") as src, \
                    open(tmp, "w", encoding="utf-8", newline="") as out:
                for line in src:
                    body = line.rstrip("\r\n")
                    out.write(redact(body) + line[len(body):])
            os.replace(tmp, dest)
            part.unlink(missing_ok=True)
        else:
            os.replace(part, dest)

    def _keep_streamed_copy(self, dest: Path) -> None:
        """WS2's live-streamed render.log becomes render.stream.log; the runner's complete file takes its name."""
        if not dest.exists():
            return
        stream = dest.with_name("render.stream.log")
        close_idle = getattr(self.store, "close_idle", None)
        if callable(close_idle):
            close_idle(0)
        try:
            if stream.exists():
                with open(dest, "rb") as src, open(stream, "ab") as out:
                    out.write(src.read())
                dest.unlink()
            else:
                os.replace(dest, stream)
        except OSError:
            with open(dest, "rb") as src, open(stream, "ab") as out:
                out.write(src.read())

    def _save(self, manifest: dict) -> None:
        now = time.time()
        if now - self._last_manifest >= PERSIST_INTERVAL_S:
            self._last_manifest = now
            _save_manifest(self.job_dir, manifest)

    def _finish(self, manifest: dict, listing_failed: bool = False) -> dict:
        _save_manifest(self.job_dir, manifest)
        part_dir = self.job_dir / STATE_DIR / "partial"
        try:
            part_dir.rmdir()           # only when empty: kept partials resume on the next delivery
        except OSError:
            pass
        st = self.status
        if self.is_chunk:
            st["items"], st["unattributed"] = self._items(manifest)
        if listing_failed:
            st["state"] = "failed"
        elif st["errors"]:
            st["state"] = "partial" if st["files_done"] else "failed"
        elif self.outcome not in (None, "succeeded") and st["files_done"]:
            st["state"] = "partial"
        else:
            st["state"] = "complete"
        st["partial"] = st["state"] == "partial"
        st["finished_at"] = time.time()
        level = "info" if st["state"] == "complete" or (st["state"] == "partial" and not st["errors"]) else "warning"
        self._event(f"Delivered {st['files_done']} files ({_fmt_bytes(st['bytes_done'])}) to "
                    f"{st['folder']['subfolder']}" + (f"; {len(st['errors'])} could not be delivered" if st["errors"]
                                                       else "") + ".",
                    level=level, data={"phase": "finished", "state": st["state"], "files_done": st["files_done"],
                                       "files_total": st["files_total"], "bytes_done": st["bytes_done"]})
        self._persist(force=True)
        _LIVE.pop(self.job_id, None)
        return json.loads(json.dumps(st))

    def _items(self, manifest: dict) -> tuple[list, int]:
        by_wf: dict[int, list] = {}
        unattributed = 0
        for entry in manifest.values():
            if entry.get("category") != "output":
                continue
            parts = entry["local"].split("/")
            if len(parts) > 2 and parts[1] == UNATTRIBUTED_DIR:
                unattributed += 1
                continue
            m = _WF_RE.fullmatch(parts[1]) if len(parts) > 2 else None
            if m:
                by_wf.setdefault(int(m.group(1)), []).append(int(entry.get("size") or 0))
        items = []
        for it in self.rec.get("items") or []:
            local = it.get("local_index")
            if isinstance(local, int) and local in by_wf:
                sizes = by_wf[local]
                items.append({"item_id": it.get("item_id"), "wf": f"wf_{local:02d}", "files": len(sizes),
                              "bytes": sum(sizes)})
        return items, unattributed


def _fmt_bytes(n: int) -> str:
    size = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


# ---- public interface -------------------------------------------------------------------------------------------------

def deliver(job_id: str, outcome: str | None = None, store: JobStore | None = None, *, max_attempts: int = 3,
            retry_sleep_s: float = 1.0) -> dict:
    """Bring every output + log of *job_id* home. Synchronous, idempotent, serialised per job. Returns DeliveryStatus."""
    store = _store(store)
    validate_job_id(job_id)
    with _job_lock(job_id):
        return _Run(store, job_id, outcome, max_attempts, retry_sleep_s).run()


def terminal_hook(job_id: str, outcome: str, store: JobStore) -> None:
    """C-2 terminal hook: runs for every outcome, so partial outputs of failed / cancelled jobs arrive too."""
    try:
        deliver(job_id, outcome, store)
    except Exception as exc:  # noqa: BLE001 - a delivery problem never changes the job outcome
        err = lifecycle.as_halofuse_error(exc)
        store.append_event(job_id, "delivery", level="error", text=f"Output delivery failed: {err.message}",
                           data={"code": err.code})


def install() -> None:
    """Register delivery as the lifecycle terminal hook (the lead also unregisters "legacy_images")."""
    lifecycle.register_terminal_hook(terminal_hook, name="delivery")


def _resolve(store: JobStore, any_id) -> str | None:
    if not isinstance(any_id, str) or not any_id:
        return None
    if store.exists(any_id):
        return any_id
    return store.find_by_cloud_id(any_id)


def _check_id(any_id) -> str:
    try:
        return validate_job_id(any_id)
    except ValueError:
        raise HaloFuseError("bad_request", _message("bad_request"), 400) from None


def _deliverable(store: JobStore, job_id: str) -> bool:
    rec = store.get(job_id) or {}
    if rec.get("terminal") or rec.get("state") in ("finalizing", "downloading"):
        return True
    cloud_ids = (rec.get("cloud") or {}).get("job_ids") or []
    if not cloud_ids:
        return False
    backend = lifecycle.open_backend(None)
    try:
        return all(backend.get(cid).terminal for cid in cloud_ids)
    finally:
        lifecycle.close_backend(backend)


def _spawn(store: JobStore, job_id: str, kw: dict) -> dict:
    with _REG_LOCK:
        thread = _THREADS.get(job_id)
        if thread is None or not thread.is_alive():
            _LIVE[job_id] = dict(status(job_id, store) or {}, state="pending")
            thread = threading.Thread(target=_background, args=(store, job_id, kw), daemon=True,
                                      name=f"halofuse-delivery-{job_id[:8]}")
            _THREADS[job_id] = thread
            thread.start()
    return status(job_id, store) or {"state": "pending"}


def _background(store: JobStore, job_id: str, kw: dict) -> None:
    try:
        deliver(job_id, None, store, **kw)
    except Exception as exc:  # noqa: BLE001 - recorded, never raised into a daemon thread
        err = lifecycle.as_halofuse_error(exc)
        _LIVE.pop(job_id, None)
        store.append_event(job_id, "delivery", level="error", text=f"Output delivery failed: {err.message}",
                           data={"code": err.code})


def start(any_id: str, store: JobStore | None = None, **kw) -> dict:
    """Retry delivery of a known job in the background; returns the current status at once."""
    store = _store(store)
    _check_id(any_id)
    job_id = _resolve(store, any_id)
    if job_id is None:
        raise HaloFuseError("job_not_found", _message("job_not_found"), 404)
    if not _deliverable(store, job_id):
        raise HaloFuseError("job_not_terminal", _message("job_not_terminal"), 409)
    return _spawn(store, job_id, kw)


_OUTCOME = {"succeeded": "succeeded", "failed": "failed", "smartcompute-interrupted": "failed",
            "cancelled": "cancelled"}


def recover(any_id: str, store: JobStore | None = None, *, background: bool = True, **kw) -> dict:
    """Manual recovery of an older job (also one with no local record) into HaloFuse/<job_id>/.

    Own jobs only (X12): a HaloFuse-tagged cloud job of another installation is reported as not found. Untagged jobs
    from before HaloFuse stay recoverable by id, as in 0.3.x.
    """
    store = _store(store)
    cloud_id = _check_id(any_id)
    job_id = _resolve(store, cloud_id)
    if job_id is None:
        backend = lifecycle.open_backend(None)
        try:
            job = backend.get(cloud_id)
        except Exception as exc:  # noqa: BLE001
            err = lifecycle.as_halofuse_error(exc)
            if err.code == "not_found":
                raise HaloFuseError("job_not_found", _message("job_not_found"), 404) from None
            raise err from None
        finally:
            lifecycle.close_backend(backend)
        tags = tuple(job.tags or ())
        if "halofuse" in tags and store.install_tag not in tags:
            raise HaloFuseError("job_not_found", _message("job_not_found"), 404)
        if not job.terminal:
            raise HaloFuseError("job_not_terminal", _message("job_not_terminal"), 409)
        outcome = _OUTCOME.get(job.status, "failed")
        try:
            store.create(kind="recovered", job_id=cloud_id, label="Recovered job", fields={
                "state": outcome, "terminal": True, "outcome": outcome, "ended_at": time.time(),
                "cloud": {"job_ids": [cloud_id], "current_job_id": cloud_id, "status": job.status,
                          "instance_type": job.instance_type, "exit_code": job.exit_code}})
        except FileExistsError:
            pass
        job_id = cloud_id
    elif not _deliverable(store, job_id):
        raise HaloFuseError("job_not_terminal", _message("job_not_terminal"), 409)
    if background:
        return _spawn(store, job_id, kw)
    return deliver(job_id, None, store, **kw)


def status(any_id: str, store: JobStore | None = None) -> dict | None:
    store = _store(store)
    job_id = _resolve(store, any_id)
    if job_id is None:
        return None
    live = _LIVE.get(job_id)
    if live is not None:
        return dict(live)
    rec = store.get(job_id) or {}
    return rec.get("delivery") if isinstance(rec.get("delivery"), dict) else None


def outputs(any_id: str, store: JobStore | None = None, *, limit: int | None = None,
            with_hash: bool = False) -> list[dict]:
    """Delivered output files as ComfyUI /view references (filename + subfolder + type), sorted by path."""
    store = _store(store)
    job_id = _resolve(store, any_id)
    if job_id is None:
        return []
    refs = []
    for entry in _load_manifest(store.job_dir(job_id)).values():
        if entry.get("category") != "output":
            continue
        rel = entry["local"]
        parts = tuple(rel.split("/"))
        ref = {"rel_path": rel, "size": entry.get("size"), "kind": _kind(parts[1:]), "filename": parts[-1],
               "subfolder": "/".join((brand.OUTPUT_DIRNAME, job_id, *parts[:-1])), "type": "output"}
        if with_hash:
            ref["sha256"] = entry.get("sha256")
        refs.append(ref)
    refs.sort(key=lambda r: r["rel_path"])
    return refs if limit is None else refs[:limit]


def _epoch(iso) -> float | None:
    if not isinstance(iso, str) or not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


def recent_jobs(limit: int = 50, store: JobStore | None = None) -> list[dict]:
    """This installation's jobs (local records + cloud jobs carrying our install tag), newest first (X12)."""
    store = _store(store)
    rows: dict[str, dict] = {}
    for s in store.list(limit=None):
        if s.get("kind") not in ("render", "chunk", "recovered"):
            continue
        rec = store.get(s["job_id"]) or {}
        cloud = rec.get("cloud") or {}
        cids = cloud.get("job_ids") or []
        row = {"job_id": s["job_id"], "cloud_job_id": cids[0] if cids else None, "created_at": rec.get("created_at"),
               "status": rec.get("state"), "instance_type": cloud.get("instance_type"), "label": rec.get("label")}
        rows[row["cloud_job_id"] or s["job_id"]] = row
    try:
        backend = lifecycle.open_backend(None)
        try:
            cloud_jobs = backend.list_jobs([store.install_tag])
        finally:
            lifecycle.close_backend(backend)
    except Exception as exc:  # noqa: BLE001 - offline: the local records still list
        _log.info("recent jobs: cloud listing unavailable (%s)", lifecycle.as_halofuse_error(exc).code)
        cloud_jobs = []
    for job in cloud_jobs:
        if job.job_id in rows or store.install_tag not in (job.tags or ()):
            continue
        rows[job.job_id] = {"job_id": job.job_id, "cloud_job_id": job.job_id, "created_at": _epoch(job.created_at),
                            "status": job.status, "instance_type": job.instance_type, "label": None}
    out = sorted(rows.values(), key=lambda r: r.get("created_at") or 0, reverse=True)
    return out[: max(0, int(limit))]


def resume_incomplete(store: JobStore | None = None, *, background: bool = True, **kw) -> list[str]:
    """Deliveries interrupted by a ComfyUI restart (state pending / downloading on a finished job) are resumed."""
    store = _store(store)
    resumed = []
    for s in store.list(limit=None):
        rec = store.get(s["job_id"]) or {}
        d = rec.get("delivery")
        if not isinstance(d, dict) or d.get("state") not in ("pending", "downloading"):
            continue
        if not rec.get("terminal"):
            continue          # the lifecycle watcher re-runs the terminal hook for jobs it is still finishing
        resumed.append(s["job_id"])
        if background:
            _spawn(store, s["job_id"], kw)
        else:
            deliver(s["job_id"], None, store, **kw)
    return resumed


def wait_idle(timeout: float = 5.0) -> bool:
    """Join background delivery threads (tests, shutdown)."""
    deadline = time.time() + timeout
    with _REG_LOCK:
        threads = list(_THREADS.values())
    for t in threads:
        t.join(max(0.0, deadline - time.time()))
    return not any(t.is_alive() for t in threads)

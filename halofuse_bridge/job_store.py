"""Durable job record + append-only event log (contract C-2).

Every render, queue and queue chunk gets a folder ``<ComfyUI output>/HaloFuse/<job_id>/`` holding ``job.json``
(atomically replaced) and ``logs/events.jsonl`` (append-only, one C-4 LogEntry per line). A small index in the
ComfyUI system-user directory lists the records; it is a cache and is rebuilt from the folders when lost.

Nothing here keeps log lines in memory: per job only a sparse ``seq -> byte offset`` checkpoint table and an
append handle (closed after a minute idle) are held, so a multi-hour render cannot grow the process.

No ComfyUI, provider SDK or bridge-config import at module import time (tests run without them).
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

_log = logging.getLogger(__name__)

from .brand import OUTPUT_DIRNAME
from .provider import redact as _provider_redact

SCHEMA = 1
INDEX_NAME = "halofuse_jobs_index.json"
INSTALL_ID_NAME = "halofuse_install_id"
CHECKPOINT_EVERY = 256
FSYNC_INTERVAL_S = 0.5
HANDLE_IDLE_S = 60.0
MAX_READ_LIMIT = 10000
_FSYNC_KINDS = frozenset({"state", "cancel", "timeout", "error"})
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$")
TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "timed_out"})


class HaloFuseError(Exception):
    """A branded, URL-free error for the browser. ``message`` is safe to show; ``code`` is stable."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def __str__(self) -> str:
        return self.message


def validate_job_id(job_id) -> str:
    """Path-safety rule for HaloFuse/<job_id>/ (C-2 §1). Raises ValueError."""
    if not isinstance(job_id, str) or not _ID_RE.match(job_id):
        raise ValueError("Not a valid HaloFuse job id.")
    return job_id


def now() -> float:
    return time.time()


def iso(ts: float | None) -> str:
    if ts is None:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ---- redaction (C-1 §5; one implementation: provider/redact.py) ------------------------------------------------

WITHHELD = "[line withheld: redaction error]"


def redact(text) -> str:
    """One display line, redacted and flattened (ANSI, CR/LF). Fails closed."""
    try:
        return _provider_redact.sanitize_line(text if isinstance(text, str) else str(text))
    except Exception:  # noqa: BLE001
        return WITHHELD


def redact_obj(obj):
    """Deep-redact every string inside a JSON-like object (keys are left alone)."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    return obj


# ---- file helpers -----------------------------------------------------------------------------------------------

def atomic_write_json(path: Path, obj) -> None:
    """Write JSON crash-safely: temp file in the same dir, fsync, os.replace (retried for Windows readers)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    data = json.dumps(obj, indent=1, ensure_ascii=False).encode("utf-8")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    for attempt in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 9:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise
            time.sleep(0.02)


def _deep_merge(dst: dict, src: dict) -> None:
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            _deep_merge(dst[key], value)
        else:
            dst[key] = copy.deepcopy(value)


def new_record(kind: str, job_id: str, label: str = "", parent_id: str | None = None,
               ts: float | None = None) -> dict:
    """A job.json with every C-2 §3 key at its default."""
    ts = now() if ts is None else ts
    rec = {
        "schema": SCHEMA, "job_id": job_id, "kind": kind, "parent_id": parent_id, "label": label or "",
        "created_at": ts, "updated_at": ts, "submitted_at": None, "ended_at": None,
        "state": "queued", "terminal": False, "outcome": None, "status_note": None, "error": None,
        "client_ref": None,
        "cloud": {
            "job_ids": [], "current_job_id": None, "status": None, "exit_code": None,
            "error_code": None, "error_message": None, "instance_type": None, "instance_handle_set": False,
            "timestamps": {"started_provisioning_at": None, "started_running_at": None,
                           "terminal_at": None, "cancel_requested_at": None},
            "gpu_name": None, "cuda_version": None, "image_digest": None,
            "image_cache_hit": None, "image_affinity": None,
        },
        "prompt_ids": [], "render_repeats": 1, "workflow": None,
        "limits": {"max_runtime_s": None, "max_wall_clock_s_sent": None, "max_job_cost_usd": None},
        "cancel": {"state": None, "mode": None, "reason": None, "requested_at": None, "accepted_at": None,
                   "confirmed_at": None, "failed_at": None, "backend_calls": 0, "detail": None,
                   "prev_state": None},
        "cost": {"label": "estimate", "rate_per_hour_usd": None, "billable_seconds": None,
                 "estimate_usd": None, "actual_usd": None,
                 "basis": "rate x seconds from provider timestamps"},
        "fingerprint": {}, "progress": None, "diagnosis": None, "delivery": None,
    }
    if kind == "queue":
        rec["queue"] = {"paused": False, "stop_mode": None, "chunk_size": 10, "active_chunk_id": None,
                        "instance_type": None,
                        "session": {"held": False, "idle_since": None, "handle": None, "idle_warned": False}}
        rec["items"] = []
    if kind == "chunk":
        rec["items"] = []
    return rec


def _summary(rec: dict) -> dict:
    cost = rec.get("cost") or {}
    amount = cost.get("actual_usd")
    if amount is None:
        amount = cost.get("estimate_usd")
    return {
        "kind": rec.get("kind"), "parent_id": rec.get("parent_id"), "label": rec.get("label"),
        "created_at": rec.get("created_at"), "updated_at": rec.get("updated_at"),
        "state": rec.get("state"), "terminal": bool(rec.get("terminal")),
        "cloud_job_ids": list((rec.get("cloud") or {}).get("job_ids") or []),
        "cost_usd": None if amount is None else round(float(amount), 2), "client_ref": rec.get("client_ref"),
    }


class _LogState:
    """Per-job append state. Holds no log content."""

    __slots__ = ("next_seq", "checkpoints", "size", "fh", "text_fhs", "last_fsync", "last_used")

    def __init__(self):
        self.next_seq = 0
        self.checkpoints: list[tuple[int, int]] = []
        self.size = 0
        self.fh = None
        self.text_fhs: dict[str, object] = {}
        self.last_fsync = 0.0
        self.last_used = 0.0


class JobStore:
    """The durable job store (C-2 §7). Thread-safe within one process."""

    def __init__(self, root: Path | None = None, index_dir: Path | None = None):
        self._root = Path(root) if root is not None else None
        self._index_dir = Path(index_dir) if index_dir is not None else None
        self._lock = threading.RLock()            # index + registry lock
        self._job_locks: dict[str, threading.RLock] = {}
        self._logs: dict[str, _LogState] = {}
        self._index: dict[str, dict] | None = None
        self._install_id: str | None = None
        self._listeners: list[Callable[[str, dict], None]] = []
        self.listener_errors = 0

    # -- locations ------------------------------------------------------------------------------------------------
    @property
    def root(self) -> Path:
        if self._root is None:
            import folder_paths  # ComfyUI-provided; available at runtime
            self._root = Path(folder_paths.get_output_directory()) / OUTPUT_DIRNAME
        self._root.mkdir(parents=True, exist_ok=True)
        return self._root

    @property
    def index_dir(self) -> Path:
        if self._index_dir is None:
            from . import config  # the bridge's private settings folder (migrated from its 0.3 name there)
            self._index_dir = config.settings_dir()
        self._index_dir.mkdir(parents=True, exist_ok=True)
        return self._index_dir

    def job_dir(self, job_id: str) -> Path:
        return self.root / validate_job_id(job_id)

    def _job_json(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "job.json"

    def _events_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "logs" / "events.jsonl"

    def job_lock(self, job_id: str) -> threading.RLock:
        with self._lock:
            lock = self._job_locks.get(job_id)
            if lock is None:
                lock = self._job_locks[job_id] = threading.RLock()
            return lock

    # -- install identity -----------------------------------------------------------------------------------------
    @property
    def install_id(self) -> str:
        with self._lock:
            if self._install_id is None:
                path = self.index_dir / INSTALL_ID_NAME
                value = ""
                try:
                    value = path.read_text(encoding="utf-8").strip()
                except OSError:
                    pass
                if not re.fullmatch(r"[0-9a-f]{12}", value):
                    value = secrets.token_hex(6)
                    tmp = path.with_name(path.name + ".tmp")
                    tmp.write_text(value, encoding="utf-8")
                    os.replace(tmp, path)
                self._install_id = value
            return self._install_id

    @property
    def install_tag(self) -> str:
        return f"hf-install-{self.install_id}"

    # -- index ----------------------------------------------------------------------------------------------------
    def _load_index(self) -> dict[str, dict]:
        if self._index is not None:
            return self._index
        path = self.index_dir / INDEX_NAME
        jobs: dict[str, dict] | None = None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("jobs"), dict):
                jobs = data["jobs"]
        except (OSError, ValueError):
            jobs = None
        if jobs is None:
            jobs = self._rebuild_index()
        self._index = jobs
        return jobs

    def _rebuild_index(self) -> dict[str, dict]:
        jobs: dict[str, dict] = {}
        root = self.root
        for child in root.iterdir() if root.is_dir() else []:
            if not child.is_dir() or not _ID_RE.match(child.name):
                continue
            rec = self._read_json(child / "job.json")
            if rec is None:
                if (child / "job.json").exists():
                    jobs[child.name] = {"kind": "corrupt", "state": "unknown", "terminal": True,
                                        "created_at": None, "updated_at": None, "cloud_job_ids": [],
                                        "label": "", "parent_id": None, "cost_usd": None, "client_ref": None}
                continue
            jobs[child.name] = _summary(rec)
        self._write_index(jobs)
        return jobs

    def _write_index(self, jobs: dict[str, dict]) -> None:
        atomic_write_json(self.index_dir / INDEX_NAME,
                          {"schema": SCHEMA, "install_id": self.install_id, "jobs": jobs})

    def _set_summary(self, job_id: str, rec: dict) -> None:
        """Rewrite the index only when a listed field changed (not on every heartbeat ``updated_at``)."""
        new = _summary(rec)
        with self._lock:
            jobs = self._load_index()
            old = jobs.get(job_id)
            jobs[job_id] = new
            if old is not None and {k: v for k, v in old.items() if k != "updated_at"} ==                     {k: v for k, v in new.items() if k != "updated_at"}:
                return
            self._write_index(jobs)

    # -- records --------------------------------------------------------------------------------------------------
    @staticmethod
    def _read_json(path: Path):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            _log.warning("HaloFuse: unreadable job record %s", Path(path).parent.name)
            return None

    def create(self, *, kind: str, label: str = "", job_id: str | None = None,
               parent_id: str | None = None, fields: dict | None = None) -> dict:
        job_id = validate_job_id(job_id or str(uuid.uuid4()))
        with self.job_lock(job_id):
            jdir = self.job_dir(job_id)
            if (jdir / "job.json").exists():
                raise FileExistsError(job_id)
            (jdir / "logs").mkdir(parents=True, exist_ok=True)
            (jdir / "previews").mkdir(parents=True, exist_ok=True)
            rec = new_record(kind, job_id, label=label, parent_id=parent_id)
            if fields:
                _deep_merge(rec, fields)
            rec = redact_obj(rec)
            atomic_write_json(jdir / "job.json", rec)
            self._set_summary(job_id, rec)
            return copy.deepcopy(rec)

    def exists(self, job_id: str) -> bool:
        try:
            return self._job_json(job_id).exists()
        except ValueError:
            return False

    def get(self, job_id: str) -> dict | None:
        try:
            path = self._job_json(job_id)
        except ValueError:
            return None
        with self.job_lock(job_id):
            return self._read_json(path)

    def mutate(self, job_id: str, fn: Callable[[dict], object]) -> dict:
        """Load, let ``fn`` change the record in place, save atomically. All under the job lock."""
        with self.job_lock(job_id):
            rec = self._read_json(self._job_json(job_id))
            if rec is None:
                raise KeyError(job_id)
            fn(rec)
            rec["updated_at"] = now()
            rec = redact_obj(rec)
            atomic_write_json(self._job_json(job_id), rec)
            self._set_summary(job_id, rec)
            return copy.deepcopy(rec)

    def update(self, job_id: str, **fields) -> dict:
        return self.mutate(job_id, lambda rec: _deep_merge(rec, fields))

    def write_file(self, job_id: str, rel: str, text: str) -> Path:
        """Write a small job-owned file (workflow snapshots) atomically. ``rel`` is a fixed, code-chosen path."""
        if ".." in Path(rel).parts or Path(rel).is_absolute():
            raise ValueError("bad relative path")
        path = self.job_dir(job_id) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return path

    def read_file(self, job_id: str, rel: str) -> str:
        if ".." in Path(rel).parts or Path(rel).is_absolute():
            raise ValueError("bad relative path")
        return (self.job_dir(job_id) / rel).read_text(encoding="utf-8")

    def list(self, kind: str | None = None, active_only: bool = False, limit: int | None = 100) -> list[dict]:
        with self._lock:
            jobs = dict(self._load_index())
        out = []
        for job_id, s in jobs.items():
            if kind is not None and s.get("kind") != kind:
                continue
            if active_only and s.get("terminal"):
                continue
            out.append({"job_id": job_id, **s})
        out.sort(key=lambda s: s.get("created_at") or 0, reverse=True)
        return out if limit is None else out[:limit]

    def find_by_cloud_id(self, cloud_job_id: str) -> str | None:
        with self._lock:
            for job_id, s in self._load_index().items():
                if cloud_job_id in (s.get("cloud_job_ids") or []):
                    return job_id
        return None

    def find_by_client_ref(self, client_ref: str) -> str | None:
        with self._lock:
            for job_id, s in self._load_index().items():
                if client_ref and s.get("client_ref") == client_ref:
                    return job_id
        return None

    def resolve_id(self, any_id: str) -> str | None:
        """Accept a HaloFuse job id or a cloud job id (C-2 §1)."""
        if self.exists(any_id):
            return any_id
        if isinstance(any_id, str) and any_id:
            return self.find_by_cloud_id(any_id)  # index lookup only; never used as a path
        return None

    # -- listeners ------------------------------------------------------------------------------------------------
    def add_listener(self, fn: Callable[[str, dict], None]) -> None:
        with self._lock:
            if fn not in self._listeners:
                self._listeners.append(fn)

    def remove_listener(self, fn) -> None:
        with self._lock:
            if fn in self._listeners:
                self._listeners.remove(fn)

    # -- event log ------------------------------------------------------------------------------------------------
    def _scan(self, job_id: str) -> _LogState:
        """Build the checkpoint table and next seq from disk (once per job per process)."""
        st = _LogState()
        path = self._events_path(job_id)
        try:
            with open(path, "rb") as fh:
                offset = 0
                last_seq = -1
                for raw in fh:
                    if not raw.endswith(b"\n"):
                        break  # torn tail; terminated before the next append
                    try:
                        seq = int(json.loads(raw)["seq"])
                    except (ValueError, KeyError, TypeError):
                        seq = None
                    if seq is not None and seq > last_seq:
                        if seq % CHECKPOINT_EVERY == 0 or not st.checkpoints:
                            st.checkpoints.append((seq, offset))
                        last_seq = seq
                    offset += len(raw)
                st.next_seq = last_seq + 1
                st.size = path.stat().st_size
        except FileNotFoundError:
            pass
        return st

    def _state(self, job_id: str) -> _LogState:
        st = self._logs.get(job_id)
        if st is None:
            st = self._logs[job_id] = self._scan(job_id)
        return st

    def _open_append(self, job_id: str, st: _LogState) -> None:
        if st.fh is not None:
            return
        path = self._events_path(job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "ab")
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        if size:
            with open(path, "rb") as rd:
                rd.seek(size - 1)
                if rd.read(1) != b"\n":
                    fh.write(b"\n")  # terminate a torn final line from a crash
                    fh.flush()
        st.fh = fh
        st.size = fh.tell()

    def _append_text(self, job_id: str, st: _LogState, name: str, line: str) -> None:
        fh = st.text_fhs.get(name)
        if fh is None:
            fh = st.text_fhs[name] = open(self.job_dir(job_id) / "logs" / name, "a", encoding="utf-8")
        fh.write(line)
        fh.flush()

    def append_event(self, job_id: str, kind: str, *, text: str = "", source: str = "bridge",
                     level: str = "info", data: dict | None = None, node_id=None, phase=None) -> int:
        validate_job_id(job_id)
        with self.job_lock(job_id):
            st = self._state(job_id)
            self._open_append(job_id, st)
            ts = now()
            entry = {"seq": st.next_seq, "ts": ts, "kind": kind, "source": source, "level": level,
                     "text": redact(text or ""), "node_id": node_id, "phase": phase,
                     "data": redact_obj(data) if data is not None else None}
            raw = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
            offset = st.size
            st.fh.write(raw)
            st.fh.flush()
            if (kind in _FSYNC_KINDS or level == "error" or ts - st.last_fsync >= FSYNC_INTERVAL_S):
                os.fsync(st.fh.fileno())
                st.last_fsync = ts
            st.size += len(raw)
            if entry["seq"] % CHECKPOINT_EVERY == 0 or not st.checkpoints:
                st.checkpoints.append((entry["seq"], offset))
            st.next_seq += 1
            st.last_used = ts
            text_line = f"{iso(ts)} {source} {level.upper()} {entry['text']}\n"
            if kind == "line":
                self._append_text(job_id, st, "render.log", text_line)
            if source == "bridge":
                self._append_text(job_id, st, "bridge.log", text_line)
            if level == "error":
                self._append_text(job_id, st, "error.log", text_line)
        for fn in list(self._listeners):
            try:
                fn(job_id, dict(entry))
            except Exception:  # noqa: BLE001 - listeners must never break the writer
                self.listener_errors += 1
        self.close_idle()
        return entry["seq"]

    def total_events(self, job_id: str) -> int:
        try:
            validate_job_id(job_id)
        except ValueError:
            return 0
        with self.job_lock(job_id):
            return self._state(job_id).next_seq

    def checkpoint_count(self, job_id: str) -> int:
        with self.job_lock(job_id):
            return len(self._state(job_id).checkpoints)

    def iter_entries(self, job_id: str, since: int = 0, stats: dict | None = None) -> Iterator[dict]:
        """Yield entries with seq >= since, in order. Torn/corrupt lines are skipped (counted in stats)."""
        with self.job_lock(job_id):
            st = self._state(job_id)
            start = 0
            for seq, off in st.checkpoints:
                if seq <= since:
                    start = off
                else:
                    break
        path = self._events_path(job_id)
        try:
            fh = open(path, "rb")
        except FileNotFoundError:
            return
        with fh:
            fh.seek(start)
            for raw in fh:
                if not raw.endswith(b"\n"):
                    break  # being written right now, or torn tail
                try:
                    entry = json.loads(raw)
                    seq = int(entry["seq"])
                except (ValueError, KeyError, TypeError):
                    if stats is not None:
                        stats["skipped_corrupt"] = stats.get("skipped_corrupt", 0) + 1
                    continue
                if seq < since:
                    continue
                yield entry

    def read_events(self, job_id: str, since: int = 0, limit: int = 2000) -> dict:
        since = max(0, int(since or 0))
        limit = max(1, min(int(limit or 2000), MAX_READ_LIMIT))
        stats: dict = {}
        entries = []
        for entry in self.iter_entries(job_id, since, stats):
            entries.append(entry)
            if len(entries) >= limit:
                break
        nxt = entries[-1]["seq"] + 1 if entries else since
        return {"entries": entries, "next": nxt, "total": self.total_events(job_id),
                "skipped_corrupt": stats.get("skipped_corrupt", 0)}

    def tail_text(self, job_id: str, n: int = 2000) -> list[str]:
        total = self.total_events(job_id)
        page = self.read_events(job_id, since=max(0, total - n), limit=min(max(n, 1), MAX_READ_LIMIT))
        return [e.get("text") or "" for e in page["entries"]]

    def iter_text(self, job_id: str) -> Iterator[str]:
        for e in self.iter_entries(job_id, 0):
            yield f"{iso(e.get('ts'))} {e.get('source')} {str(e.get('level') or '').upper()} {e.get('text') or ''}\n"

    # -- lifecycle of handles -------------------------------------------------------------------------------------
    def close_idle(self, max_idle_s: float = HANDLE_IDLE_S) -> None:
        cutoff = now() - max_idle_s
        with self._lock:
            idle = [j for j, st in self._logs.items() if st.fh is not None and st.last_used < cutoff]
        for job_id in idle:
            with self.job_lock(job_id):
                self._close_handles(self._logs.get(job_id))

    @staticmethod
    def _close_handles(st: _LogState | None) -> None:
        if st is None:
            return
        for fh in [st.fh, *st.text_fhs.values()]:
            if fh is None:
                continue
            try:
                fh.flush()
                os.fsync(fh.fileno())
            except (OSError, ValueError):
                pass
            try:
                fh.close()
            except OSError:
                pass
        st.fh = None
        st.text_fhs = {}

    def close(self) -> None:
        with self._lock:
            ids = list(self._logs)
        for job_id in ids:
            with self.job_lock(job_id):
                self._close_handles(self._logs.get(job_id))


_DEFAULT: JobStore | None = None
_DEFAULT_LOCK = threading.Lock()


def default_store() -> JobStore:
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = JobStore()
        return _DEFAULT


def set_default_store(store: JobStore | None) -> None:
    """Tests only: replace (or reset with None) the process-wide store."""
    global _DEFAULT
    with _DEFAULT_LOCK:
        _DEFAULT = store

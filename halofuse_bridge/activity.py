"""HaloFuse bridge activity: a small persisted record of what the bridge refused or reported OUTSIDE a job log.

Why (owner 2026-11-23, "the console shows no data at all, not even failures"): a Workflow Check that blocks a
render, a submit refused before a job exists, a failed model upload and a sign-in error never reach a job log, so the
console had nothing to show. Every such event is appended here and the console's Activity tab reads it (history on
open, then polling) through ``GET /halofuse/activity``.

* Entries use the C-4 log-entry shape (seq, ts, source, level, kind, text, data) so the console's log model reads
  them unchanged. ``seq`` is global and never reused, also after the file is trimmed.
* Text is branded and redacted (job_store.redact), like every job log line. No request bodies, no credentials.
* The file is ``activity.jsonl`` in the bridge's private folder (the job store's index_dir), trimmed to the newest
  KEEP entries once it holds twice that many.
* Recording never breaks a request: every failure inside this module is swallowed.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from aiohttp import web

from .job_store import default_store, redact, redact_obj

_log = logging.getLogger(__name__)

FILE_NAME = "activity.jsonl"
KEEP = 1000
DEDUP_WINDOW_S = 60.0          # the same failure of the same route is recorded at most once a minute (polling)
MAX_PROBLEMS = 20              # Workflow Check problems listed one per entry
MAX_LIMIT = 1000


class ActivityLog:
    def __init__(self, path_fn):
        self._path_fn = path_fn
        self._lock = threading.Lock()
        self._entries: list[dict] | None = None
        self._recent: dict[str, float] = {}

    # -- storage ---------------------------------------------------------------------------------------------------
    def _path(self) -> Path:
        return Path(self._path_fn())

    def _load(self) -> list[dict]:
        if self._entries is None:
            entries: list[dict] = []
            try:
                with open(self._path(), encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            e = json.loads(line)
                        except ValueError:
                            continue  # a torn last line after a crash
                        if isinstance(e, dict) and isinstance(e.get("seq"), int):
                            entries.append(e)
            except OSError:
                pass
            self._entries = entries
        return self._entries

    def _trim(self, entries: list[dict]) -> None:
        keep = entries[-KEEP:]
        path = self._path()
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            for e in keep:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
        tmp.replace(path)
        entries[:] = keep

    # -- API -------------------------------------------------------------------------------------------------------
    def append(self, text: str, *, level: str = "error", kind: str = "note", data: dict | None = None,
               dedup_key: str | None = None) -> int | None:
        """Add one entry; returns its seq, or None when it was a repeat inside the dedup window."""
        ts = time.time()
        with self._lock:
            if dedup_key:
                last = self._recent.get(dedup_key)
                if last is not None and ts - last < DEDUP_WINDOW_S:
                    return None
                self._recent[dedup_key] = ts
                if len(self._recent) > 500:
                    cutoff = ts - DEDUP_WINDOW_S
                    self._recent = {k: v for k, v in self._recent.items() if v >= cutoff}
            entries = self._load()
            seq = entries[-1]["seq"] + 1 if entries else 0
            entry = {"seq": seq, "ts": ts, "kind": kind, "source": "bridge", "level": level,
                     "text": redact(text or ""), "data": redact_obj(data) if data else None}
            path = self._path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            entries.append(entry)
            if len(entries) >= 2 * KEEP:
                self._trim(entries)
            return seq

    def read(self, since: int | None = None, limit: int = 200) -> dict:
        """Entries with seq >= *since* (oldest first), or the newest *limit* entries when *since* is None."""
        with self._lock:
            entries = list(self._load())
        nxt = entries[-1]["seq"] + 1 if entries else 0
        if since is None:
            page = entries[-limit:]
        else:
            page = [e for e in entries if e["seq"] >= since][:limit]
            if page:
                nxt = page[-1]["seq"] + 1
            else:
                nxt = max(nxt, since)
        return {"entries": page, "next": nxt, "total": len(entries)}

    def reset_for_tests(self) -> None:
        with self._lock:
            self._entries = None
            self._recent = {}


_DEFAULT = ActivityLog(lambda: default_store().index_dir / FILE_NAME)


def default_log() -> ActivityLog:
    return _DEFAULT


def record(text: str, **kw) -> int | None:
    """Append to the default log; never raises."""
    try:
        return _DEFAULT.append(text, **kw)
    except Exception as exc:  # noqa: BLE001 - recording must never break a request
        _log.warning("HaloFuse could not record activity (%s)", type(exc).__name__)
        return None


# ---- what is recorded --------------------------------------------------------------------------------------------

# Labels for the request a failure belongs to (method, route template) -> plain words.
_LABELS = {
    ("POST", "/jobs"): "Render submit",
    ("POST", "/queues"): "Queue submit",
    ("POST", "/preflight"): "Workflow Check",
    ("POST", "/draft"): "Quick draft",
    ("POST", "/models/upload"): "Model upload",
    ("GET", "/models/upload/{upload_id}"): "Model upload",
    ("POST", "/models/check"): "Model check",
    ("GET", "/settings"): "Loading settings",
    ("POST", "/settings"): "Saving settings",
    ("GET", "/account"): "Account",
    ("POST", "/account/credentials"): "Saving sign-in details",
    ("POST", "/account/test"): "Sign-in test",
    ("GET", "/skus"): "GPU list",
    ("GET", "/estimate"): "Price estimate",
    ("GET", "/library"): "Cloud model library",
}
# Polled reads whose failure is not news (the job / queue / log of an id that does not exist, preview files).
_QUIET = {("GET", "/activity"), ("GET", "/jobs/{job_id}/previews/{name}")}


def _label(method: str, tail: str) -> str:
    return _LABELS.get((method, tail)) or f"{method} {tail}"


def _problem_text(p: dict) -> str:
    where = []
    if p.get("node_id"):
        where.append(f"node {p['node_id']}" + (f" ({p['class_type']})" if p.get("class_type") else ""))
    if p.get("input"):
        where.append(f"input '{p['input']}'")
    msg = str(p.get("message") or p.get("code") or "problem")
    return f"{', '.join(where)}: {msg}" if where else msg


def _record_items(items: list, level: str, what: str) -> None:
    for p in items[:MAX_PROBLEMS]:
        # blocking problems carry node / input fields; warnings and notes already name the node in their message
        text = _problem_text(p) if what == "problem" else str(p.get("message") or p.get("code") or what)
        record(f"Workflow Check: {text}", level=level, data={"code": p.get("code"), "node_id": p.get("node_id")})
    if len(items) > MAX_PROBLEMS:
        record(f"Workflow Check: {len(items) - MAX_PROBLEMS} more {what}(s) not listed here.", level=level)


def record_response(method: str, tail: str, status: int, payload) -> None:
    """Record what one /halofuse response says went wrong (or the Workflow Check result). Never raises."""
    try:
        if (method, tail) in _QUIET:
            return
        err = payload.get("error") if isinstance(payload, dict) else None
        if status >= 400 and isinstance(err, dict):
            code = str(err.get("code") or f"http_{status}")
            data = {"code": code, "status": status, "request": _label(method, tail)}
            if err.get("id"):
                data["error_id"] = err["id"]
            if err.get("job_id"):
                data["job_id"] = err["job_id"]
            text = f"{_label(method, tail)} failed: {err.get('message') or code}"
            if err.get("job_id"):
                text += f" (job {err['job_id']}; its log has the details)"
            record(text, level="error", data=data, dedup_key=f"{method} {tail} {code}")
            return
        if status >= 400 or not isinstance(payload, dict):
            return
        if (method, tail) == ("POST", "/preflight"):
            blocking = [p for p in payload.get("blocking") or [] if isinstance(p, dict)]
            if not blocking and payload.get("ok") is not False:
                record("Workflow Check passed.", level="info", kind="note",
                       data={"warnings": len(payload.get("warnings") or [])})
            else:
                n = len(blocking)
                record(f"Workflow Check blocked the render: {n} problem{'s' if n != 1 else ''} must be fixed first. "
                       "Nothing was started.", level="error", data={"code": "preflight_blocked", "problems": n})
                _record_items(blocking, "error", "problem")
            # warnings (worth a look) and notes (information only, e.g. a model the node downloads in the cloud)
            _record_items([p for p in payload.get("warnings") or [] if isinstance(p, dict)], "warning", "warning")
            _record_items([p for p in payload.get("notes") or [] if isinstance(p, dict)], "info", "note")
        elif (method, tail) == ("POST", "/jobs"):
            job = payload.get("job") or {}
            if job.get("job_id"):
                # a repeated client_ref answers with the job it already made (idempotent submit): say so
                text = (f"Render submitted as job {job['job_id']}." if not job.get("terminal") else
                        f"Render request answered with the earlier job {job['job_id']} ({job.get('status')}); "
                        "nothing new was started.")
                record(text, level="info", kind="submit", data={"job_id": job["job_id"]},
                       dedup_key=f"submit {job['job_id']}")
        elif (method, tail) == ("GET", "/models/upload/{upload_id}") and payload.get("status") in ("failed",):
            uid = payload.get("upload_id")
            record(f"Model upload failed: {payload.get('error') or 'see the bridge log'}", level="error",
                   data={"code": "upload_failed", "upload_id": uid}, dedup_key=f"upload-failed {uid}")
        elif (method, tail) == ("POST", "/account/test") and payload.get("ok") is False:
            record(f"Sign-in test failed: {payload.get('message') or payload.get('code')}", level="error",
                   data={"code": payload.get("code")}, dedup_key=f"account-test {payload.get('code')}")
    except Exception as exc:  # noqa: BLE001
        _log.warning("HaloFuse could not record activity (%s)", type(exc).__name__)


def _wrap(method: str, tail: str, handler):
    async def wrapped(request):
        resp = await handler(request)
        try:
            body = getattr(resp, "body", None)
            ctype = getattr(resp, "content_type", "") or ""
            if isinstance(body, (bytes, bytearray)) and ctype == "application/json" and (
                    resp.status >= 400 or (method, tail) in _RECORD_OK):
                record_response(method, tail, resp.status, json.loads(body.decode("utf-8")))
        except Exception as exc:  # noqa: BLE001
            _log.warning("HaloFuse could not record activity (%s)", type(exc).__name__)
        return resp
    wrapped.__name__ = getattr(handler, "__name__", "handler")
    return wrapped


_RECORD_OK = {("POST", "/preflight"), ("POST", "/jobs"), ("GET", "/models/upload/{upload_id}"),
              ("POST", "/account/test")}


class RecordingRoutes:
    """Wraps a route table (aiohttp RouteTableDef or anything with .route/.get/.post/...) so every handler added
    through it records its failures here. Used by routes.py for all /halofuse route modules."""

    def __init__(self, table, prefix: str = "/halofuse"):
        self._table = table
        self._prefix = "/" + prefix.strip("/")

    def _tail(self, path: str) -> str:
        return path[len(self._prefix):] if path.startswith(self._prefix) else path

    def route(self, method: str, path: str, **kw):
        inner = self._table.route(method, path, **kw)
        tail = self._tail(path)
        return lambda handler: inner(_wrap(method.upper(), tail, handler))

    def get(self, path, **kw):
        return self.route("GET", path, **kw)

    def post(self, path, **kw):
        return self.route("POST", path, **kw)

    def patch(self, path, **kw):
        return self.route("PATCH", path, **kw)

    def delete(self, path, **kw):
        return self.route("DELETE", path, **kw)

    def put(self, path, **kw):
        return self.route("PUT", path, **kw)


# ---- route -------------------------------------------------------------------------------------------------------

def _int(value, default, lo, hi):
    try:
        return max(lo, min(int(value), hi))
    except (TypeError, ValueError):
        return default


async def get_activity(request):
    from .routes_account import request_guard
    refused = request_guard(request, needs_header=False)
    if refused is not None:
        return refused
    since = request.query.get("since")
    limit = _int(request.query.get("limit"), 200, 1, MAX_LIMIT)
    try:
        page = default_log().read(None if since in (None, "") else _int(since, 0, 0, 2**62), limit)
    except Exception as exc:  # noqa: BLE001
        _log.warning("HaloFuse could not read activity (%s)", type(exc).__name__)
        page = {"entries": [], "next": 0, "total": 0}
    return web.json_response(page)


def register(route_table, prefix: str = "/halofuse") -> None:
    route_table.route("GET", "/" + prefix.strip("/") + "/activity")(get_activity)

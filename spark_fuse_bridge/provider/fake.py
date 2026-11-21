"""FakeBackend: an in-memory, scriptable CloudBackend for every workstream's tests (contract C-1 section 8).

It never touches the network. Tests that use it are mocked tests and must say so (marker or docstring).
"""
from __future__ import annotations

import itertools
import threading
from dataclasses import replace
from pathlib import Path

from .backend import (
    TERMINAL_STATUSES,
    AccountCheck,
    BackendError,
    CloudJob,
    CloudJobRef,
    Estimate,
    LibraryEntry,
    RemoteFile,
    Session,
    Sku,
    SubmitSpec,
    validate_instance_type,
    validate_remote_path,
)


class FakeBackend:
    def __init__(self, skus=None, rates=None, accept=None) -> None:
        self.skus: list[Sku] = list(skus or [Sku("fake.xlarge", "FakeGPU", 24.0, 8, 64.0)])
        self.rates: dict[str, str] = dict(rates or {s.instance_type: "1.00" for s in self.skus})
        self.accept = tuple(accept) if accept else None
        self.auth_mode = "ok"
        self.last_credentials = None
        self.range_supported = True
        self.cancel_takes_polls = 0
        self.calls: list[tuple] = []
        self.submitted: SubmitSpec | None = None
        self.jobs: dict[str, CloudJob] = {}
        self.job_events: dict[str, list] = {}
        self.job_outputs: dict[str, dict[str, bytes]] = {}
        self.sessions: dict[str, Session] = {}
        self.library: dict[str, bytes] = {}
        self.closed = False
        self._fail: dict[str, list[BaseException]] = {}
        self._pending_cancel: dict[str, int] = {}
        self._ids = itertools.count(1)
        self._lock = threading.RLock()

    # ---- scripting ---------------------------------------------------------------------------

    def add_job(self, job_id: str, status: str = "queued", events=(), outputs=None, **fields) -> CloudJob:
        with self._lock:
            job = CloudJob(job_id=job_id, status=status, terminal=status in TERMINAL_STATUSES,
                           has_output=bool(outputs), **fields)
            self.jobs[job_id] = job
            self.job_events[job_id] = list(events)
            self.job_outputs[job_id] = dict(outputs or {})
            return job

    def set_status(self, job_id: str, status: str, **fields) -> CloudJob:
        with self._lock:
            job = replace(self.jobs[job_id], status=status, terminal=status in TERMINAL_STATUSES, **fields)
            self.jobs[job_id] = job
            return job

    def fail_next(self, method: str, exc: BaseException) -> None:
        with self._lock:
            self._fail.setdefault(method, []).append(exc)

    def _record(self, method: str, *args) -> None:
        with self._lock:
            self.calls.append((method, args))
            queue = self._fail.get(method)
            exc = queue.pop(0) if queue else None
        if exc is not None:
            raise exc

    def _job(self, job_id: str) -> CloudJob:
        try:
            return self.jobs[job_id]
        except KeyError:
            raise BackendError("not_found", detail="unknown fake job") from None

    # ---- CloudBackend --------------------------------------------------------------------------

    def login_check(self) -> AccountCheck:
        self._record("login_check")
        if self.auth_mode == "auth_failed":
            raise BackendError("auth_failed", detail="fake auth failure")
        if self.auth_mode == "unreachable":
            raise BackendError("unreachable", detail="fake network failure")
        if self.accept and self.last_credentials is not None and \
                (self.last_credentials.email, self.last_credentials.password) != self.accept:
            raise BackendError("auth_failed", detail="fake credential mismatch")
        return AccountCheck(ok=True)

    def list_skus(self) -> list[Sku]:
        self._record("list_skus")
        return list(self.skus)

    def estimate(self, instance_type: str, runtime_s=None, idle_hold_s=None) -> Estimate:
        self._record("estimate", instance_type)
        validate_instance_type(instance_type)
        rate = self.rates.get(instance_type)
        if rate is None:
            raise BackendError("invalid_request", detail="unpriced fake SKU")
        return Estimate(instance_type=instance_type, rate_per_hour_usd=rate)

    def submit(self, spec: SubmitSpec) -> CloudJobRef:
        self._record("submit", spec)
        with self._lock:
            job_id = f"fake-{next(self._ids):04d}"
            self.submitted = spec
            self.add_job(job_id, status="queued", tags=tuple(spec.tags), instance_type=spec.instance_type,
                         max_wall_clock_s=spec.max_wall_clock_s)
        return CloudJobRef(job_id=job_id, status="queued")

    def get(self, job_id: str) -> CloudJob:
        self._record("get", job_id)
        with self._lock:
            if job_id in self._pending_cancel:
                self._pending_cancel[job_id] -= 1
                if self._pending_cancel[job_id] <= 0:
                    del self._pending_cancel[job_id]
                    if not self._job(job_id).terminal:
                        self.set_status(job_id, "cancelled")
            return self._job(job_id)

    def cancel(self, job_id: str) -> CloudJob:
        self._record("cancel", job_id)
        with self._lock:
            job = self._job(job_id)
            if job.terminal:
                return job
            if self.cancel_takes_polls > 0:
                self._pending_cancel[job_id] = self.cancel_takes_polls
                return self.set_status(job_id, job.status, cancel_requested_at="fake-now")
            return self.set_status(job_id, "cancelled", cancel_requested_at="fake-now")

    def events(self, job_id: str):
        self._record("events", job_id)
        self._job(job_id)
        yield from list(self.job_events.get(job_id, []))

    def walk_outputs(self, job_id: str, max_depth: int = 16) -> list[RemoteFile]:
        self._record("walk_outputs", job_id)
        self._job(job_id)
        return [RemoteFile(rel_parts=tuple(path.split("/")), size=len(data), etag=None, ref=(job_id, path))
                for path, data in sorted(self.job_outputs.get(job_id, {}).items())]

    def download(self, remote_file: RemoteFile, dest: Path, offset: int = 0, on_progress=None) -> int:
        self._record("download", remote_file.rel_parts, offset)
        job_id, path = remote_file.ref
        data = self.job_outputs[job_id][path]
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if offset and self.range_supported:
            with dest.open("ab") as fh:
                fh.write(data[offset:])
            start = offset
        else:
            dest.write_bytes(data)
            start = 0
        if on_progress is not None:
            on_progress(len(data), len(data))
        return start

    def list_jobs(self, tags) -> list[CloudJob]:
        self._record("list_jobs", tuple(tags or ()))
        tags = [t for t in (tags or []) if t]
        if not tags:
            raise BackendError("invalid_request", detail="list_jobs needs at least one tag")
        return [j for j in self.jobs.values() if all(t in j.tags for t in tags)]

    def prepare_session(self, instance_type: str, hold_s: int) -> Session:
        self._record("prepare_session", instance_type, hold_s)
        validate_instance_type(instance_type)
        handle = f"fake-session-{next(self._ids)}"
        session = Session(handle=handle, status="ready", ready=True, terminal=False,
                          instance_type=instance_type, hold_s=hold_s)
        self.sessions[handle] = session
        return session

    def get_session(self, handle: str) -> Session:
        self._record("get_session", handle)
        try:
            return self.sessions[handle]
        except KeyError:
            raise BackendError("not_found", detail="unknown fake session") from None

    def release_session(self, handle: str) -> Session | None:
        self._record("release_session", handle)
        session = self.sessions.get(handle)
        if session is None or session.terminal:
            return None
        session = replace(session, status="released", ready=False, terminal=True)
        self.sessions[handle] = session
        return session

    def append_session_jobs(self, handle: str, specs, halt_on_failure: bool = False) -> list[CloudJobRef]:
        self._record("append_session_jobs", handle, len(specs))
        self.get_session(handle)
        refs = []
        for spec in specs:
            if spec.input_dir is not None:
                raise BackendError("invalid_request", detail="session jobs cannot push an input folder")
            job_id = f"fake-{next(self._ids):04d}"
            self.add_job(job_id, status="queued", tags=tuple(spec.tags))
            refs.append(CloudJobRef(job_id=job_id, status="queued"))
        return refs

    def library_stat(self, remote_path: str, space=None) -> LibraryEntry | None:
        self._record("library_stat", remote_path)
        validate_remote_path(remote_path)
        data = self.library.get(remote_path)
        return None if data is None else LibraryEntry(remote_path=remote_path, size=len(data))

    def library_list(self, remote_dir: str, space=None) -> list[LibraryEntry]:
        self._record("library_list", remote_dir)
        validate_remote_path(remote_dir, allow_empty=True)
        prefix = remote_dir + "/" if remote_dir else ""
        return [LibraryEntry(remote_path=p, size=len(d)) for p, d in sorted(self.library.items())
                if p.startswith(prefix) and "/" not in p[len(prefix):]]

    def library_upload(self, local_path: Path, remote_path: str, space=None, on_progress=None,
                       max_bytes=None) -> None:
        self._record("library_upload", remote_path)
        validate_remote_path(remote_path)
        data = Path(local_path).read_bytes()
        if max_bytes is not None and len(data) > max_bytes:
            raise BackendError("too_large", detail="fake upload over the limit")
        if on_progress is not None:
            on_progress(len(data), len(data))
        self.library[remote_path] = data

    def close(self) -> None:
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

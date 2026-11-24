"""CloudBackend adapter over the provider SDK (its import name lives in provider/sdk.py).

This is the only module that imports the SDK, and it does so lazily, so the bridge (and its tests) load without it.
Every SDK exception is translated to a BackendError with a branded message; the redacted technical detail goes to
the private bridge log only.
"""
from __future__ import annotations

import inspect
import logging
import threading
import urllib.parse
from collections.abc import Iterator, Sequence
from pathlib import Path

import time

import httpx

from . import redact, sdk as _sdk_binding
from .backend import (
    TERMINAL_STATUSES,
    AccountCheck,
    BackendError,
    CloudJob,
    CloudJobRef,
    Estimate,
    LibraryEntry,
    LogLine,
    RemoteFile,
    Session,
    Sku,
    StatusEvent,
    SubmitSpec,
    Truncated,
    safe_name,
    sku_sort_key,
    validate_handle,
    validate_instance_type,
    validate_job_id,
    validate_remote_path,
)

_log = logging.getLogger("halofuse_bridge.provider")

_DOWNLOAD_TIMEOUT = httpx.Timeout(connect=30.0, read=300.0, write=120.0, pool=30.0)
_CHUNK = 1 << 20


def _import_sdk():
    try:
        sdk, errors = _sdk_binding.load()
    except ImportError:
        raise BackendError("connector_missing", detail="provider SDK not importable") from None
    return sdk, errors


def _status_code_to_code(status: int | None) -> str:
    if status == 400:
        return "invalid_request"
    if status == 401:
        return "auth_failed"
    if status == 403:
        return "forbidden"
    if status == 404:
        return "not_found"
    if status == 429:
        return "rate_limited"
    if status is not None and status >= 500:
        return "service_unavailable"
    return "unknown"


def _translate(exc: BaseException, errors) -> BackendError:
    def is_(name: str) -> bool:
        cls = getattr(errors, name, None) if errors is not None else None
        return isinstance(cls, type) and isinstance(exc, cls)

    retry_after = None
    if is_("AuthError") or is_("TokenExpiredError"):
        code = "auth_failed"
    elif is_("ForbiddenError"):
        code = "forbidden"
    elif is_("RateLimitError"):
        code = "rate_limited"
        retry_after = getattr(exc, "retry_after", None)
    elif is_("NoWarmPoolCapacityError"):
        code = "no_capacity"
    elif is_("SessionNotFoundError"):
        code = "not_found"
    elif is_("SessionFailedError") or is_("SessionError"):
        code = "session_failed"
    elif is_("ShareSyncError"):
        code = "storage_error"
    elif is_("ServiceUnavailableError"):
        code = "service_unavailable"
    elif is_("SparkHttpError"):
        code = _status_code_to_code(getattr(exc, "status_code", None))
    elif isinstance(exc, httpx.TransportError):
        code = "unreachable"
    elif isinstance(exc, httpx.HTTPStatusError):
        code = _status_code_to_code(exc.response.status_code if exc.response is not None else None)
    else:
        code = "unknown"
    err = BackendError(code, detail=f"{type(exc).__name__}: {exc}", retry_after=retry_after)
    _log.warning("provider call failed [%s] %s", code, err.detail)
    return err


def _register_storage(url) -> None:
    if isinstance(url, str) and url:
        redact.register_secret(url, "storage")


def _job_from_sdk(job) -> CloudJob:
    status = str(getattr(job, "status", "") or "")
    terminal = getattr(job, "is_terminal", None)
    if not isinstance(terminal, bool):
        terminal = status in TERMINAL_STATUSES
    output = getattr(job, "output", None)
    base = getattr(output, "share_sync_base_url", None) if output is not None else None
    _register_storage(base)
    msg = getattr(job, "error_message", None)
    return CloudJob(
        job_id=str(getattr(job, "id", "") or getattr(job, "job_id", "")),
        status=status,
        terminal=terminal,
        exit_code=getattr(job, "exit_code", None),
        error_code=getattr(job, "error_code", None),
        error_message=redact.redact(msg) if msg else None,
        created_at=getattr(job, "created_at", None),
        started_provisioning_at=getattr(job, "started_provisioning_at", None),
        started_running_at=getattr(job, "started_running_at", None),
        terminal_at=getattr(job, "terminal_at", None),
        cancel_requested_at=getattr(job, "cancel_requested_at", None),
        instance_type=getattr(job, "instance_type_name", None),
        gpu_name=getattr(job, "gpu_name", None),
        cuda_version=getattr(job, "cuda_version", None),
        driver_version=getattr(job, "driver_version", None),
        image_digest=getattr(job, "image_digest", None),
        image_cache_hit=getattr(job, "image_cache_hit", None),
        image_affinity=getattr(job, "image_affinity", None),
        max_wall_clock_s=getattr(job, "max_wall_clock_seconds", None),
        tags=tuple(getattr(job, "tags", None) or ()),
        has_output=bool(base),
    )


def _session_from_sdk(s) -> Session:
    queue = getattr(s, "queue", None)
    msg = getattr(s, "error_message", None)
    status = str(getattr(s, "status", "") or "")
    ready = getattr(s, "is_ready", None)
    terminal = getattr(s, "is_terminal", None)
    return Session(
        handle=str(getattr(s, "instance_handle", "") or ""),
        status=status,
        ready=ready if isinstance(ready, bool) else status == "ready",
        terminal=terminal if isinstance(terminal, bool) else status in ("released", "expired", "failed"),
        instance_type=getattr(s, "instance_type", None),
        hold_s=getattr(s, "hold_seconds", None),
        error_code=getattr(s, "error_code", None),
        error_message=redact.redact(msg) if msg else None,
        expires_at=getattr(s, "expires_at", None),
        queue_depth=getattr(queue, "depth", None) if queue is not None else None,
    )


def _spec_kwargs(spec: SubmitSpec) -> dict:
    kw = {
        "image": spec.image,
        "command": list(spec.command),
        "instance_type": validate_instance_type(spec.instance_type),
        "env": dict(spec.env) if spec.env else None,
        "input_push_mode": "auto-prepare" if spec.input_dir is not None else None,
        "max_wall_clock_seconds": spec.max_wall_clock_s,
        "container_inactivity_seconds": spec.container_inactivity_s,
        "idle_hold_seconds": spec.idle_hold_s,
        "tags": list(spec.tags) if spec.tags else None,
        "instance_handle": spec.instance_handle,
        "assets_share_sync_path": spec.assets_path,
        "assets_share_sync_space_name": spec.assets_space,
        "image_affinity": spec.image_affinity,
        "shm_size": spec.shm_size,
    }
    return {k: v for k, v in kw.items() if v is not None}


_CAMEL = {
    "image": "image", "command": "command", "instance_type": "instanceType", "env": "env",
    "max_wall_clock_seconds": "maxWallClockSeconds", "container_inactivity_seconds": "containerInactivitySeconds",
    "idle_hold_seconds": "idleHoldSeconds", "tags": "tags", "assets_share_sync_path": "assetsShareSyncPath",
    "assets_share_sync_space_name": "assetsShareSyncSpaceName", "image_affinity": "imageAffinity",
    "shm_size": "shmSize",
}


class CloudAdapter:
    """CloudBackend over the provider SDK. One instance per operation; close() releases its HTTP clients."""

    def __init__(self, endpoint: str, credentials, *, http_client: httpx.Client | None = None) -> None:
        redact.register_secret(endpoint, "endpoint")
        redact.register_secret(credentials.email, "email")
        redact.register_secret(credentials.password, "password")
        self._sdk, self._errors = _import_sdk()
        self._lock = threading.Lock()
        self._token: str | None = None
        self._dav_bases: dict = {}
        self._http = http_client
        self._own_http = http_client is None
        self._closed = False
        self._client = self._call(self._sdk.SparkFuseClient, host=endpoint, email=credentials.email,
                                  password=credentials.password)

    # ---- plumbing ------------------------------------------------------------------------------

    def _call(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except BackendError:
            raise
        except Exception as exc:  # noqa: BLE001 - every SDK failure becomes a branded BackendError
            raise _translate(exc, self._errors) from None

    def _login(self) -> str | None:
        resp = self._call(self._client.login)
        if getattr(resp, "success", True) is False:
            raise BackendError("auth_failed", detail="login returned success=false")
        token = getattr(resp, "token", None)
        if token:
            redact.register_secret(token, "token")
        with self._lock:
            self._token = token
        return token

    def _bearer(self) -> str:
        with self._lock:
            token = self._token
        token = token or self._login()
        if not token:
            raise BackendError("auth_failed", detail="login returned no token")
        return token

    def _http_client(self) -> httpx.Client:
        with self._lock:
            if self._http is None:
                self._http = httpx.Client(timeout=_DOWNLOAD_TIMEOUT, follow_redirects=False)
            return self._http

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._client.close()
        except Exception:  # noqa: BLE001 - closing must never raise
            pass
        if self._own_http and self._http is not None:
            try:
                self._http.close()
            except Exception:  # noqa: BLE001
                pass

    # ---- account / catalogue -------------------------------------------------------------------

    def login_check(self) -> AccountCheck:
        self._login()
        return AccountCheck(ok=True)

    def list_skus(self) -> list[Sku]:
        raw = self._call(self._client.list_skus) or []
        out = []
        for sku in raw:
            if not isinstance(sku, dict) or not sku.get("gpuType") or not sku.get("instanceType"):
                continue
            out.append(Sku(
                instance_type=str(sku["instanceType"]),
                gpu_type=str(sku["gpuType"]),
                gpu_memory_gb=sku.get("gpuMemoryGb"),
                core_count=sku.get("coreCount"),
                memory_gb=sku.get("memoryGb"),
            ))
        out.sort(key=lambda s: sku_sort_key(s.instance_type))
        return out

    def estimate(self, instance_type: str, runtime_s: int | None = None, idle_hold_s: int | None = None) -> Estimate:
        validate_instance_type(instance_type)
        kw = {"instance_type": instance_type}
        if runtime_s is not None:
            kw["estimated_runtime_seconds"] = int(runtime_s)
        if idle_hold_s is not None:
            kw["idle_hold_seconds"] = int(idle_hold_s)
        est = self._call(self._client.estimate, **kw)
        rate = getattr(est, "rate", None)
        total = getattr(est, "estimate", None)
        return Estimate(
            instance_type=instance_type,
            rate_per_hour_usd=getattr(rate, "billed_per_hour_usd", None),
            rate_per_second_cents=getattr(rate, "billed_per_second_cents", None),
            billable_seconds=getattr(total, "billable_seconds", None) if total is not None else None,
            total_usd=getattr(total, "total_usd", None) if total is not None else None,
            notes=tuple(redact.redact(n) for n in (getattr(est, "notes", None) or ())),
        )

    # owner 2026-11-24: a large input bundle (video) failed with "WriteTimeout: The write operation timed out".
    _UPLOAD_WRITE_TIMEOUT_S = 600.0
    _UPLOAD_ATTEMPTS = 3

    def _widen_upload_timeouts(self) -> None:
        """Raise the write/read timeout of every httpx client the SDK holds (best effort, never raises)."""
        try:
            seen = set()
            for holder in (self._client, getattr(self._client, "_client", None), getattr(self._client, "_http", None)):
                if holder is None or id(holder) in seen:
                    continue
                seen.add(id(holder))
                for value in [holder, *vars(holder).values()] if hasattr(holder, "__dict__") else [holder]:
                    if isinstance(value, (httpx.Client, httpx.AsyncClient)):
                        t = value.timeout
                        value.timeout = httpx.Timeout(connect=t.connect, pool=t.pool,
                                                      read=max(t.read or 0, self._UPLOAD_WRITE_TIMEOUT_S),
                                                      write=max(t.write or 0, self._UPLOAD_WRITE_TIMEOUT_S))
        except Exception:  # noqa: BLE001 - only a wider timeout; the upload still runs
            _log.info("could not widen the SDK upload timeout")

    def _upload_input(self, input_dir: Path, upload) -> None:
        """Upload the job inputs with a long write timeout, retrying network timeouts (3 attempts, 5/15 s apart)."""
        self._widen_upload_timeouts()
        for attempt in range(1, self._UPLOAD_ATTEMPTS + 1):
            try:
                self._call(self._client.upload_input, input_dir, upload)
                return
            except BackendError as err:
                if err.code not in ("unreachable", "network_error", "timeout") or attempt == self._UPLOAD_ATTEMPTS:
                    raise
                _log.warning("input upload attempt %d failed (%s); retrying", attempt, err.code)
                time.sleep(5 if attempt == 1 else 15)

    # ---- jobs ------------------------------------------------------------------------------

    def submit(self, spec: SubmitSpec) -> CloudJobRef:
        resp = self._call(self._client.submit, **_spec_kwargs(spec))
        job_id = str(getattr(resp, "job_id", "") or "")
        if spec.input_dir is not None:
            upload = getattr(getattr(resp, "input", None), "upload_url", None)
            _register_storage(upload)
            try:
                if not upload:
                    raise BackendError("input_upload_failed", detail="no upload slot returned")
                self._upload_input(Path(spec.input_dir), upload)
            except BackendError as err:
                try:
                    if job_id:
                        self._client.cancel(job_id)
                except Exception:  # noqa: BLE001 - best effort; the original failure is what matters
                    _log.warning("could not cancel cloud job %s after a failed input upload", job_id)
                raise BackendError("input_upload_failed", detail=f"{err.code}: {err.detail}") from None
        return CloudJobRef(
            job_id=job_id,
            status=str(getattr(resp, "status", "") or ""),
            queue_position=getattr(resp, "queue_position", None),
            estimated_start_s=getattr(resp, "estimated_start_seconds", None),
        )

    def get(self, job_id: str) -> CloudJob:
        validate_job_id(job_id)
        return _job_from_sdk(self._call(self._client.get_job, job_id))

    def cancel(self, job_id: str) -> CloudJob:
        validate_job_id(job_id)
        return _job_from_sdk(self._call(self._client.cancel, job_id))

    def events(self, job_id: str) -> Iterator:
        validate_job_id(job_id)
        stream = self._call(self._client.stream_logs, job_id)
        iterator = iter(stream)
        while True:
            try:
                ev = next(iterator)
            except StopIteration:
                return
            except BackendError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise _translate(exc, self._errors) from None
            kind = type(ev).__name__
            if kind == "QueueStatusEvent":
                yield StatusEvent(status=str(getattr(ev, "status", "")),
                                  queue_position=getattr(ev, "queue_position", None),
                                  estimated_start_s=getattr(ev, "estimated_start_seconds", None),
                                  done=bool(getattr(ev, "done", False)))
            elif kind == "LogEvent":
                yield LogLine(ts=str(getattr(ev, "ts", "")), stream=str(getattr(ev, "stream", "stdout")),
                              line=str(getattr(ev, "line", "")), phase=str(getattr(ev, "phase", "container")),
                              event_id=getattr(ev, "event_id", None))
            elif kind == "TruncatedEvent":
                data = getattr(ev, "data", None) or {}
                dropped = data.get("dropped", data.get("count")) if isinstance(data, dict) else None
                try:
                    dropped = int(dropped) if dropped is not None else None
                except (TypeError, ValueError):
                    dropped = None
                raw = {str(k): redact.redact(v) for k, v in data.items()} if isinstance(data, dict) else {}
                yield Truncated(dropped=dropped, raw=raw)

    def list_jobs(self, tags: Sequence[str]) -> list[CloudJob]:
        tags = [t for t in (tags or []) if isinstance(t, str) and t]
        if not tags:
            raise BackendError("invalid_request", detail="list_jobs needs at least one tag")
        return [_job_from_sdk(j) for j in self._call(self._client.list_jobs, tags=tags) or []]

    # ---- outputs -----------------------------------------------------------------------------

    def walk_outputs(self, job_id: str, max_depth: int = 16) -> list[RemoteFile]:
        validate_job_id(job_id)
        job = self._call(self._client.get_job, job_id)
        output = getattr(job, "output", None)
        base = getattr(output, "share_sync_base_url", None) if output is not None else None
        if not base:
            return []
        _register_storage(base)
        files: list[RemoteFile] = []
        self._walk(base, (), files, max_depth)
        return files

    def _walk(self, url: str, prefix: tuple, files: list, depth_left: int) -> None:
        parts = urllib.parse.urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        root_path = parts.path.rstrip("/")
        for entry in self._call(self._client.list_outputs, url) or []:
            href = str(getattr(entry, "href", "") or "")
            name = getattr(entry, "name", "")
            if getattr(entry, "is_collection", False):
                if href.rstrip("/") == root_path:
                    continue
                if not safe_name(name):
                    _log.warning("skipped an output folder with an unsafe name")
                    continue
                if depth_left > 0:
                    self._walk((origin + href).rstrip("/"), prefix + (name,), files, depth_left - 1)
                continue
            if not safe_name(name):
                _log.warning("skipped an output file with an unsafe name")
                continue
            files.append(RemoteFile(rel_parts=prefix + (name,), size=getattr(entry, "content_length", None),
                                    etag=None, ref=origin + href))

    def download(self, remote_file: RemoteFile, dest: Path, offset: int = 0, on_progress=None) -> int:
        url = remote_file.ref
        if not isinstance(url, str) or not url:
            raise BackendError("invalid_request", detail="remote file has no reference")
        _register_storage(url)
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        offset = max(0, int(offset or 0))
        label = "/".join(remote_file.rel_parts)
        for attempt in (0, 1):
            headers = {"Authorization": f"Bearer {self._bearer()}"}
            if offset:
                headers["Range"] = f"bytes={offset}-"
            try:
                with self._http_client().stream("GET", url, headers=headers) as resp:
                    if resp.status_code == 401 and attempt == 0:
                        with self._lock:
                            self._token = None
                        continue
                    if resp.status_code == 206 and offset:
                        start, mode = offset, "ab"
                    elif resp.status_code == 200:
                        start, mode = 0, "wb"
                    else:
                        raise BackendError(_status_code_to_code(resp.status_code) if resp.status_code in (401, 403, 404)
                                           else "storage_error",
                                           detail=f"download of output file {label} returned HTTP {resp.status_code}")
                    length = resp.headers.get("Content-Length")
                    total = start + int(length) if length and length.isdigit() else None
                    done = start
                    with dest.open(mode) as fh:
                        if on_progress is not None:
                            on_progress(done, total)
                        for chunk in resp.iter_bytes(_CHUNK):
                            fh.write(chunk)
                            done += len(chunk)
                            if on_progress is not None:
                                on_progress(done, total)
                    return start
            except BackendError:
                raise
            except OSError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise _translate(exc, self._errors) from None
        raise BackendError("auth_failed", detail=f"download of output file {label} was refused twice")

    # ---- sessions ------------------------------------------------------------------------------

    def prepare_session(self, instance_type: str, hold_s: int) -> Session:
        validate_instance_type(instance_type)
        return _session_from_sdk(self._call(self._client.prepare_instance, instance_type=instance_type,
                                            hold_seconds=int(hold_s)))

    def get_session(self, handle: str) -> Session:
        validate_handle(handle)
        return _session_from_sdk(self._call(self._client.get_instance, handle))

    def release_session(self, handle: str) -> Session | None:
        validate_handle(handle)
        try:
            return _session_from_sdk(self._client.release_instance(handle))
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ in ("SessionNotFoundError", "SessionConflictError"):
                return None
            raise _translate(exc, self._errors) from None

    def append_session_jobs(self, handle: str, specs: Sequence[SubmitSpec],
                            halt_on_failure: bool = False) -> list[CloudJobRef]:
        validate_handle(handle)
        bodies = []
        for spec in specs:
            if spec.input_dir is not None:
                raise BackendError("invalid_request", detail="session jobs cannot push an input folder")
            kw = _spec_kwargs(spec)
            kw.pop("instance_handle", None)
            bodies.append({_CAMEL[k]: v for k, v in kw.items() if k in _CAMEL})
        resp = self._call(self._client.append_instance_jobs, handle, bodies, halt_on_failure=bool(halt_on_failure))
        return [CloudJobRef(job_id=str(getattr(a, "job_id", "")), status=str(getattr(a, "status", "queued")),
                            queue_position=getattr(a, "queue_position", None))
                for a in getattr(resp, "accepted", None) or []]

    # ---- model library -------------------------------------------------------------------------

    def _dav_base(self, space: str | None) -> str:
        key = space or ""
        with self._lock:
            cached = self._dav_bases.get(key)
        if cached:
            return cached
        base = self._call(self._client.sharesync_dav_base, space or None)
        _register_storage(base)
        with self._lock:
            self._dav_bases[key] = base
        return base

    def library_stat(self, remote_path: str, space: str | None = None) -> LibraryEntry | None:
        validate_remote_path(remote_path)
        base = self._dav_base(space)
        entry = self._call(self._client.sharesync_stat, base, remote_path)
        if entry is None:
            return None
        return LibraryEntry(remote_path=remote_path, size=getattr(entry, "content_length", None),
                            is_dir=bool(getattr(entry, "is_collection", False)))

    def library_list(self, remote_dir: str, space: str | None = None) -> list[LibraryEntry]:
        validate_remote_path(remote_dir, allow_empty=True)
        base = self._dav_base(space).rstrip("/")
        url = base + ("/" + urllib.parse.quote(remote_dir, safe="/") if remote_dir else "")
        root_path = urllib.parse.urlsplit(url).path.rstrip("/")
        out = []
        for entry in self._call(self._client.list_outputs, url) or []:
            if str(getattr(entry, "href", "")).rstrip("/") == root_path:
                continue
            name = getattr(entry, "name", "")
            if not safe_name(name):
                continue
            out.append(LibraryEntry(remote_path=f"{remote_dir}/{name}" if remote_dir else name,
                                    size=getattr(entry, "content_length", None),
                                    is_dir=bool(getattr(entry, "is_collection", False))))
        return out

    def library_upload(self, local_path: Path, remote_path: str, space: str | None = None,
                       on_progress=None, max_bytes: int | None = None) -> None:
        validate_remote_path(remote_path)
        local_path = Path(local_path)
        size = local_path.stat().st_size
        if max_bytes is not None and size > max_bytes:
            raise BackendError("too_large", detail=f"{size} bytes over the {max_bytes}-byte upload limit")
        base = self._dav_base(space)
        self._call(self._client.upload_file, local_path, base, remote_path, progress=on_progress,
                   make_parents=True, max_bytes=max_bytes)


# ---- legacy shim (C-1 section 6): raw SDK client behind a redacting proxy --------------------------

def _scrub_exception(exc: BaseException) -> None:
    try:
        exc.args = (redact.redact(str(exc)),)
    except Exception:  # noqa: BLE001
        exc.args = (redact.WITHHELD,)
    for attr in ("text", "resp"):
        value = getattr(exc, attr, None)
        if isinstance(value, str):
            try:
                setattr(exc, attr, redact.redact(value))
            except Exception:  # noqa: BLE001
                pass
    exc.__cause__ = None
    exc.__context__ = None
    exc.__suppress_context__ = True


def _wrap_iterator(iterator):
    try:
        yield from iterator
    except Exception as exc:  # noqa: BLE001
        _scrub_exception(exc)
        raise


class _RedactingProxy:
    """Raw SDK client whose exceptions (same types) carry redacted text and no chained causes."""

    def __init__(self, target) -> None:
        object.__setattr__(self, "_target", target)

    def __getattr__(self, name):
        attr = getattr(self._target, name)
        if not callable(attr):
            return attr

        def call(*args, **kwargs):
            try:
                result = attr(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                _scrub_exception(exc)
                raise
            if inspect.isgenerator(result) or (hasattr(result, "__next__") and hasattr(result, "__iter__")):
                return _wrap_iterator(result)
            return result

        return call

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        try:
            self._target.close()
        except Exception:  # noqa: BLE001
            pass


def legacy_sdk_client():
    """Deprecated: the raw SDK client for modules not yet on the port. Endpoint/credentials from the trusted layer."""
    from .. import provider as _provider

    endpoint = _provider.resolve_endpoint()
    if not endpoint:
        raise BackendError("not_configured", detail="no deployment endpoint")
    from .. import credentials as _credentials

    creds = _credentials.default_store().get()
    if creds is None:
        raise BackendError("credentials_missing", detail="no stored or deployment credential")
    redact.register_secret(creds.email, "email")
    redact.register_secret(creds.password, "password")
    sdk, errors = _import_sdk()
    try:
        client = sdk.SparkFuseClient(host=endpoint, email=creds.email, password=creds.password)
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc, errors) from None
    return _RedactingProxy(client)

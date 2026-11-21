"""The CloudBackend port (contract C-1): typed values, the Protocol and the single error type.

Nothing here imports the provider SDK. No field of any type holds a URL, token, email or password, except the
opaque, never-serialised `RemoteFile.ref`.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, Union, runtime_checkable

from .. import brand
from .redact import redact

TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled", "smartcompute-interrupted"})


class BackendError(RuntimeError):
    """Every backend failure. `str(err)` is the branded, URL-free user message."""

    def __init__(self, code: str, user_message: str | None = None, detail: str = "", *,
                 retry_after: float | None = None, status: int | None = None) -> None:
        self.code = code
        self.user_message = user_message or brand.message(code)
        self.detail = redact(detail)
        self.retry_after = retry_after
        self.status = status if status is not None else brand.status_for(code)
        super().__init__(self.user_message)

    def __str__(self) -> str:
        return self.user_message


# ---- values --------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Sku:
    instance_type: str
    gpu_type: str
    gpu_memory_gb: float | None = None
    core_count: int | None = None
    memory_gb: float | None = None


@dataclass(frozen=True)
class Estimate:
    instance_type: str
    rate_per_hour_usd: str | None = None
    rate_per_second_cents: str | None = None
    billable_seconds: int | None = None
    total_usd: str | None = None
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class SubmitSpec:
    image: str
    command: tuple[str, ...]
    instance_type: str
    env: dict[str, str] = field(default_factory=dict)
    input_dir: Path | None = None
    max_wall_clock_s: int | None = None
    container_inactivity_s: int | None = None
    idle_hold_s: int | None = None
    tags: tuple[str, ...] = ()
    instance_handle: str | None = None
    assets_path: str | None = None
    assets_space: str | None = None
    image_affinity: str | None = None
    shm_size: str | None = None


@dataclass(frozen=True)
class CloudJobRef:
    job_id: str
    status: str
    queue_position: int | None = None
    estimated_start_s: int | None = None


@dataclass(frozen=True)
class CloudJob:
    job_id: str
    status: str
    terminal: bool
    exit_code: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: str | None = None
    started_provisioning_at: str | None = None
    started_running_at: str | None = None
    terminal_at: str | None = None
    cancel_requested_at: str | None = None
    instance_type: str | None = None
    gpu_name: str | None = None
    cuda_version: str | None = None
    driver_version: str | None = None
    image_digest: str | None = None
    image_cache_hit: bool | None = None
    image_affinity: str | None = None
    max_wall_clock_s: int | None = None
    tags: tuple[str, ...] = ()
    has_output: bool = False


@dataclass(frozen=True)
class StatusEvent:
    status: str
    queue_position: int | None = None
    estimated_start_s: int | None = None
    done: bool = False


@dataclass(frozen=True)
class LogLine:
    ts: str
    stream: str
    line: str
    phase: str = "container"
    event_id: str | None = None


@dataclass(frozen=True)
class Truncated:
    dropped: int | None = None
    raw: dict = field(default_factory=dict)


Event = Union[StatusEvent, LogLine, Truncated]


@dataclass(frozen=True)
class RemoteFile:
    rel_parts: tuple[str, ...]
    size: int | None = None
    etag: str | None = None
    ref: object = field(default=None, repr=False, compare=False)  # opaque; never serialise


@dataclass(frozen=True)
class LibraryEntry:
    remote_path: str
    size: int | None = None
    is_dir: bool = False


@dataclass(frozen=True)
class Session:
    handle: str
    status: str
    ready: bool = False
    terminal: bool = False
    instance_type: str | None = None
    hold_s: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    expires_at: str | None = None
    queue_depth: int | None = None


@dataclass(frozen=True)
class AccountCheck:
    ok: bool = True


ProgressFn = Callable[[int, "int | None"], None]


@runtime_checkable
class CloudBackend(Protocol):
    def login_check(self) -> AccountCheck: ...
    def list_skus(self) -> list[Sku]: ...
    def estimate(self, instance_type: str, runtime_s: int | None = None,
                 idle_hold_s: int | None = None) -> Estimate: ...
    def submit(self, spec: SubmitSpec) -> CloudJobRef: ...
    def get(self, job_id: str) -> CloudJob: ...
    def cancel(self, job_id: str) -> CloudJob: ...
    def events(self, job_id: str) -> Iterator[Event]: ...
    def walk_outputs(self, job_id: str, max_depth: int = 16) -> list[RemoteFile]: ...
    def download(self, remote_file: RemoteFile, dest: Path, offset: int = 0,
                 on_progress: ProgressFn | None = None) -> int: ...
    def list_jobs(self, tags: Sequence[str]) -> list[CloudJob]: ...
    def prepare_session(self, instance_type: str, hold_s: int) -> Session: ...
    def get_session(self, handle: str) -> Session: ...
    def release_session(self, handle: str) -> Session | None: ...
    def append_session_jobs(self, handle: str, specs: Sequence[SubmitSpec],
                            halt_on_failure: bool = False) -> list[CloudJobRef]: ...
    def library_stat(self, remote_path: str, space: str | None = None) -> LibraryEntry | None: ...
    def library_list(self, remote_dir: str, space: str | None = None) -> list[LibraryEntry]: ...
    def library_upload(self, local_path: Path, remote_path: str, space: str | None = None,
                       on_progress: ProgressFn | None = None, max_bytes: int | None = None) -> None: ...
    def close(self) -> None: ...
    def __enter__(self): ...
    def __exit__(self, *exc) -> None: ...


# ---- validation helpers (TM-011) ----------------------------------------------------------------

_INSTANCE_TYPE_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_HANDLE_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SIZE_RE = re.compile(r"(\d*)xlarge")


def validate_instance_type(value) -> str:
    if not isinstance(value, str) or not _INSTANCE_TYPE_RE.match(value):
        raise BackendError("invalid_request", detail="invalid instance type")
    return value


def validate_job_id(value) -> str:
    if not isinstance(value, str) or not _JOB_ID_RE.match(value):
        raise BackendError("invalid_request", detail="invalid job id")
    return value


def validate_handle(value) -> str:
    if not isinstance(value, str) or not _HANDLE_RE.match(value):
        raise BackendError("invalid_request", detail="invalid session handle")
    return value


def validate_remote_path(value, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise BackendError("invalid_request", detail="invalid remote path")
    if value == "" and allow_empty:
        return value
    bad = (
        value == "" or value.startswith("/") or "\\" in value or "\x00" in value
        or re.match(r"^[A-Za-z]:", value) is not None
        or any(part in ("", ".", "..") for part in value.split("/"))
    )
    if bad:
        raise BackendError("invalid_request", detail="invalid remote path")
    return value


def safe_name(name) -> bool:
    """A single path component that cannot escape its parent."""
    return (isinstance(name, str) and name not in ("", ".", "..")
            and not any(c in name for c in ("/", "\\", "\x00")))


def sku_sort_key(instance_type: str) -> tuple:
    """Group SKUs by family (text before the first '.'), then order by the Nxlarge size multiplier."""
    family, _, size = instance_type.partition(".")
    match = _SIZE_RE.fullmatch(size)
    if match:
        multiplier = int(match.group(1)) if match.group(1) else 1
        return (0, family, multiplier)
    return (1, instance_type, 0)

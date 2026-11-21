"""Delivery routes (C-4 section 4 + the previews of section 6; WS3), plus the 0.3.x /spark_fuse download aliases.

The lead's routes.py calls::

    routes_delivery.register(routes, prefix="/halofuse")
    routes_delivery.register(routes, prefix="/spark_fuse", legacy=True)   # one-release alias, old shapes

Every route goes through the WS1 request guard (loopback peer + Host allowlist; the X-HaloFuse-Request header on all
new routes and on the legacy POST). Job ids from the path are validated before any filesystem or cloud use; preview
files are served only from the job's own previews/ folder. No response carries a URL, a host or exception text.
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import re
import threading

from aiohttp import web

from . import brand, delivery, lifecycle, output_download
from .job_store import HaloFuseError, default_store
from .routes_account import request_guard

_log = logging.getLogger(__name__)

_PREVIEW_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
_MIME = {".webp": "image/webp", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
         ".gif": "image/gif", ".mp4": "video/mp4", ".webm": "video/webm", ".wav": "audio/wav", ".mp3": "audio/mpeg"}
_PREVIEW_KIND = {"image": "image", "video": "video", "audio": "audio"}
_NOT_STARTED = {"state": "not_started", "partial": False, "files_total": 0, "files_done": 0, "bytes_total": 0,
                "bytes_done": 0, "errors": [], "updated_at": None}


async def _run(fn):
    return await asyncio.get_running_loop().run_in_executor(None, fn)


def _error(code: str, message: str | None, status: int) -> web.Response:
    return web.json_response(brand.error_envelope(code, message=message), status=status)


def _from_exc(exc: BaseException) -> web.Response:
    err = lifecycle.as_halofuse_error(exc)
    return _error(err.code, err.message, err.status)


def _int(value, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def _job_id(request) -> str:
    """The path id resolved to a local job id (accepts a cloud job id). Raises HaloFuseError."""
    any_id = request.match_info["job_id"].strip()
    if not output_download.valid_job_id(any_id):
        raise HaloFuseError("bad_request", delivery.MESSAGES["bad_request"], 400)
    resolved = default_store().resolve_id(any_id)
    if resolved is None:
        raise HaloFuseError("job_not_found", delivery.MESSAGES["job_not_found"], 404)
    return resolved


# ---- new routes ------------------------------------------------------------------------------------------------------

async def get_delivery(request):
    try:
        job_id = await _run(lambda: _job_id(request))
        limit = _int(request.query.get("limit"), 1000, 1, 100000)

        def read():
            status = delivery.status(job_id) or dict(_NOT_STARTED)
            refs = delivery.outputs(job_id)
            return {"delivery": status, "outputs": refs[:limit], "outputs_total": len(refs)}
        return web.json_response(await _run(read))
    except Exception as exc:  # noqa: BLE001
        return _from_exc(exc)


async def post_retry(request):
    try:
        job_id = await _run(lambda: _job_id(request))
        status = await _run(lambda: delivery.start(job_id))
        return web.json_response({"delivery": status}, status=202)
    except Exception as exc:  # noqa: BLE001
        return _from_exc(exc)


async def post_recovery(request):
    any_id = request.match_info["job_id"].strip()
    try:
        status = await _run(lambda: delivery.recover(any_id, background=True))
        return web.json_response({"delivery": status}, status=202)
    except Exception as exc:  # noqa: BLE001
        return _from_exc(exc)


async def get_recent_jobs(request):
    limit = _int(request.query.get("limit"), 50, 1, 500)
    try:
        return web.json_response({"jobs": await _run(lambda: delivery.recent_jobs(limit))})
    except Exception as exc:  # noqa: BLE001
        return _from_exc(exc)


def _previews_dir(job_id: str):
    return default_store().job_dir(job_id) / delivery.PREVIEWS_DIR


def _list_previews(job_id: str, since: int, limit: int) -> dict:
    folder = _previews_dir(job_id)
    entries = []
    if folder.is_dir():
        for p in folder.iterdir():
            if p.is_file() and _PREVIEW_RE.match(p.name):
                st = p.stat()
                entries.append((st.st_mtime, p.name, st.st_size))
    entries.sort()
    previews = []
    for seq, (mtime, name, size) in enumerate(entries):
        if seq < since or len(previews) >= limit:
            continue
        mime = _MIME.get(os.path.splitext(name)[1].lower()) or mimetypes.guess_type(name)[0] or \
            "application/octet-stream"
        previews.append({"seq": seq, "name": name, "kind": _PREVIEW_KIND.get(mime.split("/")[0], "image"),
                         "mime": mime, "ts": mtime, "size": size})
    return {"previews": previews, "next": since + len(previews), "total": len(entries)}


async def get_previews(request):
    try:
        job_id = await _run(lambda: _job_id(request))
        since = _int(request.query.get("since"), 0, 0, 10**9)
        limit = _int(request.query.get("limit"), 500, 1, 5000)
        return web.json_response(await _run(lambda: _list_previews(job_id, since, limit)))
    except Exception as exc:  # noqa: BLE001
        return _from_exc(exc)


async def get_preview_file(request):
    name = request.match_info["name"]
    try:
        job_id = await _run(lambda: _job_id(request))
    except Exception as exc:  # noqa: BLE001
        return _from_exc(exc)
    missing = _error("preview_not_found", "That preview is not available.", 404)
    if not _PREVIEW_RE.match(name) or name.startswith("."):
        return missing
    folder = _previews_dir(job_id)
    path = folder / name
    try:
        base = folder.resolve()
        if path.resolve().parent != base or not path.is_file():
            return missing
    except OSError:
        return missing
    mime = _MIME.get(path.suffix.lower()) or mimetypes.guess_type(name)[0] or "application/octet-stream"
    data = await _run(path.read_bytes)
    return web.Response(body=data, content_type=mime, headers={"Cache-Control": "no-store"})


_NEW = (
    ("GET", "/jobs/{job_id}/delivery", get_delivery),
    ("POST", "/jobs/{job_id}/delivery/retry", post_retry),
    ("GET", "/recent_jobs", get_recent_jobs),
    ("POST", "/recovery/{job_id}", post_recovery),
    ("GET", "/jobs/{job_id}/previews", get_previews),
    ("GET", "/jobs/{job_id}/previews/{name}", get_preview_file),
)


# ---- legacy 0.3.x shapes ----------------------------------------------------------------------------------------------

def _legacy_error(message: str, status: int) -> web.Response:
    return web.json_response({"error": message}, status=status)


async def legacy_recent_jobs(request):
    try:
        return web.json_response({"jobs": await _run(output_download.recent_jobs)})
    except Exception as exc:  # noqa: BLE001
        return _legacy_error(lifecycle.as_halofuse_error(exc).message, 400)


async def legacy_post_download(request):
    job_id = request.match_info["job_id"].strip()
    try:
        state = await _run(lambda: output_download.start_download(job_id))
    except ValueError:
        return _legacy_error(delivery.MESSAGES["bad_request"], 400)
    except Exception as exc:  # noqa: BLE001
        return _legacy_error(lifecycle.as_halofuse_error(exc).message, 400)
    return web.json_response(state)


async def legacy_get_download(request):
    job_id = request.match_info["job_id"].strip()
    if not output_download.valid_job_id(job_id):
        return _legacy_error(delivery.MESSAGES["bad_request"], 400)
    state = await _run(lambda: output_download.get_download_state(job_id))
    if state is None:
        return _legacy_error("HaloFuse has not delivered this job yet.", 404)
    return web.json_response(state)


_LEGACY = (
    ("GET", "/recent_jobs", legacy_recent_jobs, False),
    ("POST", "/download/{job_id}", legacy_post_download, True),
    ("GET", "/download/{job_id}", legacy_get_download, False),
)


# ---- registration -------------------------------------------------------------------------------------------------------

def _guarded(handler, needs_header: bool, legacy: bool):
    async def wrapper(request):
        refused = request_guard(request, needs_header=needs_header)
        if refused is not None:
            if legacy:
                return _legacy_error(brand.message("forbidden_origin" if refused.status == 403 else "unknown"),
                                     refused.status)
            return refused
        return await handler(request)

    wrapper.__name__ = handler.__name__
    return wrapper


def register(route_table, prefix: str = "/halofuse", legacy: bool = False, *, resume: bool = True) -> None:
    """Add the delivery routes under *prefix*. ``resume`` restarts deliveries a ComfyUI restart interrupted."""
    prefix = "/" + prefix.strip("/")
    if legacy:
        for method, path, handler, needs_header in _LEGACY:
            route_table.route(method, prefix + path)(_guarded(handler, needs_header, True))
    else:
        for method, path, handler in _NEW:
            route_table.route(method, prefix + path)(_guarded(handler, True, False))
    if resume and not legacy:
        threading.Thread(target=_resume, daemon=True, name="halofuse-delivery-resume").start()


def _resume() -> None:
    try:
        delivery.resume_incomplete()
    except Exception as exc:  # noqa: BLE001 - never stops ComfyUI from starting
        _log.warning("HaloFuse could not resume interrupted deliveries (%s)", lifecycle.as_halofuse_error(exc).code)

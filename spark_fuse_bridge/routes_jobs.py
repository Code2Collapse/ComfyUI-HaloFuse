"""HTTP routes for jobs, logs, cancellation, queues and cost (C-4 §2/§3/§7 with the C-2 §8 decisions).

The lead's ``routes.py`` calls ``register(PromptServer.instance.routes, prefix="/halofuse")`` and registers
``LEGACY_ROUTES`` (the 0.3.4 ``/spark_fuse/*`` shapes). This module never imports ComfyUI's ``server``.
Errors use the envelope ``{"error": {"code", "message", "id"}}``; messages are branded and URL-free, and the
correlation id is logged privately next to the (redacted) detail.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from aiohttp import web

from . import cost as costmod
from . import jobs, lifecycle, render_queue
from .job_store import HaloFuseError, default_store, redact, validate_job_id
from .routes_account import request_guard

_log = logging.getLogger(__name__)
REQUEST_HEADER = "X-HaloFuse-Request"
MAX_LOG_LIMIT = 10000


async def _run(func):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, func)


def _error(code: str, message: str, status: int, detail: str | None = None) -> web.Response:
    err_id = lifecycle.new_error_id()
    if detail:
        _log.warning("HaloFuse %s %s: %s", err_id, code, redact(detail))
    return web.json_response({"error": {"code": code, "message": message, "id": err_id}}, status=status)


def _from_exc(exc: BaseException) -> web.Response:
    if isinstance(exc, KeyError):
        return _error("job_not_found", "This HaloFuse job was not found.", 404)
    err = lifecycle.as_halofuse_error(exc)
    return _error(err.code, err.message, err.status)


def _guard(request) -> web.Response | None:
    # one policy for every HaloFuse route (lead, R-WS3-02): loopback peer + Host allowlist always, header on writes
    return request_guard(request, needs_header=request.method not in ("GET", "HEAD", "OPTIONS"))


async def _json(request) -> dict:
    try:
        body = await request.json()
    except (ValueError, json.JSONDecodeError):
        raise HaloFuseError("bad_request", "The request body is not valid JSON.", 400) from None
    if not isinstance(body, dict):
        raise HaloFuseError("bad_request", "The request body must be a JSON object.", 400)
    return body


def _resolve(any_id: str) -> str:
    try:
        validate_job_id(any_id)
    except ValueError:
        raise HaloFuseError("bad_request", "Not a valid HaloFuse job id.", 400) from None
    store = default_store()
    job_id = store.resolve_id(any_id)
    if not job_id:
        raise HaloFuseError("job_not_found", "This HaloFuse job was not found.", 404)
    return job_id


def _int(value, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(int(value), hi))
    except (TypeError, ValueError):
        return default


def _handler(fn):
    async def wrapped(request):
        denied = _guard(request)
        if denied is not None:
            return denied
        try:
            return await fn(request)
        except HaloFuseError as exc:
            return _error(exc.code, exc.message, exc.status)
        except Exception as exc:  # noqa: BLE001
            return _error("internal", "HaloFuse hit an internal error.", 500, detail=f"{type(exc).__name__}: {exc}")
    wrapped.__name__ = fn.__name__
    return wrapped


# ---- jobs -------------------------------------------------------------------------------------------------------

@_handler
async def post_job(request):
    body = await _json(request)
    workflow = body.get("workflow") or body.get("prompt")
    if not isinstance(workflow, dict) or not workflow:
        raise HaloFuseError("bad_request", "No workflow provided.", 400)
    rec = await _run(lambda: jobs.submit_render(
        workflow, instance_type=body.get("instance_type"),
        render_repeats=body.get("render_repeats", body.get("batch_count")),
        max_runtime_s=body.get("max_runtime_s"), max_job_cost_usd=body.get("max_job_cost_usd"),
        label=str(body.get("label") or "Render"), client_ref=body.get("client_ref"),
        preview_mode=body.get("preview_mode")))
    return web.json_response({"job": lifecycle.job_summary(rec, default_store())}, status=202)


@_handler
async def list_jobs(request):
    store = default_store()
    active = request.query.get("active") in ("1", "true", "yes")
    kind = request.query.get("kind") or None
    limit = _int(request.query.get("limit"), 50, 1, 500)
    rows = await _run(lambda: store.list(kind=kind, active_only=active, limit=None))
    out = []
    for s in rows:
        if s.get("kind") not in ("render", "chunk", "recovered"):
            continue
        rec = store.get(s["job_id"])
        if rec is not None:
            out.append(lifecycle.job_summary(rec, store))
        if len(out) >= limit:
            break
    active_id = next((j["job_id"] for j in out if not j["terminal"] and j["kind"] != "chunk"), None)
    return web.json_response({"jobs": out, "active_job_id": active_id})


@_handler
async def get_job(request):
    job_id = _resolve(request.match_info["job_id"])
    store = default_store()
    rec = store.get(job_id)
    if rec is None:
        raise HaloFuseError("job_not_found", "This HaloFuse job was not found.", 404)
    view = render_queue.queue_view(rec, store) if rec.get("kind") == "queue" else lifecycle.job_detail(rec, store)
    return web.json_response({"job": view})


async def _log_page(request, job_id: str) -> web.Response:
    store = default_store()
    since = _int(request.query.get("since"), 0, 0, 2**62)
    limit = _int(request.query.get("limit"), 2000, 1, MAX_LOG_LIMIT)
    page = await _run(lambda: store.read_events(job_id, since=since, limit=limit))
    rec = store.get(job_id) or {}
    return web.json_response({"job_id": job_id, "entries": page["entries"], "next": page["next"],
                              "total": page["total"], "terminal": bool(rec.get("terminal")),
                              "skipped_corrupt": page["skipped_corrupt"]})


@_handler
async def get_job_log(request):
    return await _log_page(request, _resolve(request.match_info["job_id"]))


@_handler
async def get_job_log_txt(request):
    job_id = _resolve(request.match_info["job_id"])
    store = default_store()
    resp = web.StreamResponse(headers={
        "Content-Type": "text/plain; charset=utf-8",
        "Content-Disposition": f'attachment; filename="halofuse-{job_id}.log"'})
    await resp.prepare(request)
    batch: list[str] = []
    for line in store.iter_text(job_id):
        batch.append(line)
        if len(batch) >= 2000:
            await resp.write("".join(batch).encode("utf-8"))
            batch = []
    if batch:
        await resp.write("".join(batch).encode("utf-8"))
    await resp.write_eof()
    return resp


@_handler
async def post_job_cancel(request):
    job_id = _resolve(request.match_info["job_id"])
    body = await _json(request) if request.can_read_body else {}
    mode = body.get("mode") or "now"
    store = default_store()
    rec = store.get(job_id)
    if rec is not None and rec.get("kind") == "queue":
        rec = await _run(lambda: render_queue.cancel_queue(job_id, mode))
        return web.json_response({"queue": render_queue.queue_view(rec, store)}, status=202)
    status = 200 if rec is not None and rec.get("terminal") else 202
    rec = await _run(lambda: lifecycle.request_cancel(job_id, mode, "user"))
    return web.json_response({"job": lifecycle.job_summary(rec, store)}, status=status)


# ---- queues -----------------------------------------------------------------------------------------------------

def _queue_id(request) -> str:
    qid = request.match_info["queue_id"]
    try:
        validate_job_id(qid)
    except ValueError:
        raise HaloFuseError("bad_request", "Not a valid HaloFuse queue id.", 400) from None
    return qid


def _queue_response(rec, status: int = 200) -> web.Response:
    return web.json_response({"queue": render_queue.queue_view(rec, default_store())}, status=status)


@_handler
async def post_queue(request):
    body = await _json(request)
    items = body.get("items") or []
    if not isinstance(items, list) or not items:
        raise HaloFuseError("bad_request", "No workflows in the queue.", 400)
    qid = await _run(lambda: render_queue.submit_queue(items, body.get("instance_type"),
                                                       client_ref=body.get("client_ref")))
    return _queue_response(default_store().get(qid), 202)


@_handler
async def list_queues(request):
    store = default_store()
    active = request.query.get("active") in ("1", "true", "yes")
    rows = store.list(kind="queue", active_only=active, limit=_int(request.query.get("limit"), 50, 1, 500))
    return web.json_response({"queues": [render_queue.queue_view(store.get(s["job_id"]), store)
                                         for s in rows if store.get(s["job_id"]) is not None]})


@_handler
async def get_queue(request):
    qid = _queue_id(request)
    store = default_store()
    rec = render_queue._get_queue(store, qid)
    return _queue_response(rec)


@_handler
async def get_queue_log(request):
    qid = _queue_id(request)
    render_queue._get_queue(default_store(), qid)
    return await _log_page(request, qid)


@_handler
async def post_queue_cancel(request):
    qid = _queue_id(request)
    body = await _json(request) if request.can_read_body else {}
    rec = await _run(lambda: render_queue.cancel_queue(qid, body.get("mode") or "after_current"))
    return _queue_response(rec, 202)


@_handler
async def post_queue_pause(request):
    return _queue_response(await _run(lambda: render_queue.pause(_queue_id(request))))


@_handler
async def post_queue_resume(request):
    return _queue_response(await _run(lambda: render_queue.resume(_queue_id(request))))


@_handler
async def post_queue_reorder(request):
    qid = _queue_id(request)
    body = await _json(request)
    ids = body.get("item_ids", body.get("order"))
    if not isinstance(ids, list):
        raise HaloFuseError("bad_request", "Reorder needs a list of item ids.", 400)
    return _queue_response(await _run(lambda: render_queue.reorder(qid, ids)))


@_handler
async def post_queue_items(request):
    qid = _queue_id(request)
    body = await _json(request)
    items = body.get("items") or []
    if not isinstance(items, list) or not items:
        raise HaloFuseError("bad_request", "No workflows to add.", 400)
    return _queue_response(await _run(lambda: render_queue.append_items(qid, items)))


@_handler
async def patch_queue_item(request):
    qid = _queue_id(request)
    item_id = request.match_info["item_id"]
    body = await _json(request)
    return _queue_response(await _run(lambda: render_queue.patch_item(
        qid, item_id, label=body.get("label"), render_repeats=body.get("render_repeats", body.get("batch_count")),
        workflow=body.get("workflow"))))


@_handler
async def delete_queue_item(request):
    qid = _queue_id(request)
    item_id = request.match_info["item_id"]
    return _queue_response(await _run(lambda: render_queue.delete_item(qid, item_id)))


# ---- cost -------------------------------------------------------------------------------------------------------

@_handler
async def get_cost_summary(request):
    settings = await _run(jobs._load_settings)
    return web.json_response(costmod.cost_summary(default_store(), settings, time.time()))


# ---- registration -----------------------------------------------------------------------------------------------

def _table(prefix: str) -> list[tuple[str, str, object]]:
    p = prefix.rstrip("/")
    return [
        ("POST", f"{p}/jobs", post_job),
        ("GET", f"{p}/jobs", list_jobs),
        ("GET", f"{p}/jobs/{{job_id}}", get_job),
        ("GET", f"{p}/jobs/{{job_id}}/log", get_job_log),
        ("GET", f"{p}/jobs/{{job_id}}/log.txt", get_job_log_txt),
        ("POST", f"{p}/jobs/{{job_id}}/cancel", post_job_cancel),
        ("POST", f"{p}/queues", post_queue),
        ("GET", f"{p}/queues", list_queues),
        ("GET", f"{p}/queues/{{queue_id}}", get_queue),
        ("GET", f"{p}/queues/{{queue_id}}/log", get_queue_log),
        ("POST", f"{p}/queues/{{queue_id}}/cancel", post_queue_cancel),
        ("POST", f"{p}/queues/{{queue_id}}/pause", post_queue_pause),
        ("POST", f"{p}/queues/{{queue_id}}/resume", post_queue_resume),
        ("POST", f"{p}/queues/{{queue_id}}/reorder", post_queue_reorder),
        ("POST", f"{p}/queues/{{queue_id}}/items", post_queue_items),
        ("PATCH", f"{p}/queues/{{queue_id}}/items/{{item_id}}", patch_queue_item),
        ("DELETE", f"{p}/queues/{{queue_id}}/items/{{item_id}}", delete_queue_item),
        ("GET", f"{p}/cost/summary", get_cost_summary),
    ]


def register(routes, prefix: str = "/halofuse", *, boot: bool = True) -> None:
    """Add the routes to an aiohttp ``RouteTableDef`` (or anything with ``.get/.post/.patch/.delete``)."""
    for method, path, handler in _table(prefix):
        getattr(routes, method.lower())(path)(handler)
    if boot:
        try:
            lifecycle.boot()
        except Exception:  # noqa: BLE001 - reattachment must never stop ComfyUI from starting
            _log.warning("HaloFuse could not reattach to running jobs at startup")


# ---- legacy 0.3.4 shapes (/spark_fuse/*) ------------------------------------------------------------------------

def _legacy_error(exc: BaseException, status: int = 400) -> web.Response:
    err = lifecycle.as_halofuse_error(exc)
    return web.json_response({"error": err.message}, status=status)


async def legacy_submit(request):
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "No workflow provided."}, status=400)
    prompt = body.get("workflow") or body.get("prompt")
    if not prompt:
        return web.json_response({"error": "No workflow provided."}, status=400)
    try:
        job_id = await _run(lambda: jobs.submit_workflow(prompt, body.get("instance_type")))
    except Exception as exc:  # noqa: BLE001
        return _legacy_error(exc)
    return web.json_response({"jobId": job_id})


async def legacy_get_job(request):
    try:
        state = jobs.get_job_state(request.match_info["job_id"])
    except Exception:  # noqa: BLE001
        state = None
    if state is None:
        return web.json_response({"error": "unknown job"}, status=404)
    return web.json_response(state)


async def legacy_post_queue(request):
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "No workflows in the queue."}, status=400)
    items = body.get("items") or []
    if not items:
        return web.json_response({"error": "No workflows in the queue."}, status=400)
    for it in items:
        wf = it.get("workflow") or it.get("prompt")
        if not wf:
            return web.json_response({"error": "A queued item is missing its workflow."}, status=400)
        it["workflow"] = wf
    try:
        qid = await _run(lambda: render_queue.submit_queue(items, body.get("instance_type")))
    except Exception as exc:  # noqa: BLE001
        return _legacy_error(exc)
    return web.json_response({"queueId": qid})


async def legacy_get_queue(request):
    try:
        state = render_queue.get_queue_state(request.match_info["queue_id"])
    except Exception:  # noqa: BLE001
        state = None
    if state is None:
        return web.json_response({"error": "unknown queue"}, status=404)
    return web.json_response(state)


async def legacy_cancel_queue(request):
    try:
        await _run(lambda: render_queue.cancel_queue(request.match_info["queue_id"], "after_current"))
    except Exception as exc:  # noqa: BLE001
        return _legacy_error(exc)
    return web.json_response({"ok": True})


LEGACY_ROUTES = [
    ("POST", "/spark_fuse/submit", legacy_submit),
    ("GET", "/spark_fuse/job/{job_id}", legacy_get_job),
    ("POST", "/spark_fuse/queue", legacy_post_queue),
    ("GET", "/spark_fuse/queue/{queue_id}", legacy_get_queue),
    ("POST", "/spark_fuse/queue/{queue_id}/cancel", legacy_cancel_queue),
]


def _legacy_guarded(handler):
    """Legacy shapes keep {"error": "<text>"} but get the same request policy as /halofuse/* (a POST here can start
    a paid render, so it needs X-HaloFuse-Request like the legacy account POSTs)."""
    async def wrapped(request):
        refused = _guard(request)
        if refused is not None:
            try:
                text = json.loads(refused.body)["error"]["message"]
            except (ValueError, KeyError, TypeError):
                text = "This request is not allowed."
            return web.json_response({"error": text}, status=refused.status)
        return await handler(request)
    wrapped.__name__ = handler.__name__
    return wrapped


def register_legacy(routes) -> None:
    for method, path, handler in LEGACY_ROUTES:
        getattr(routes, method.lower())(path)(_legacy_guarded(handler))

"""HTTP routes for jobs, logs, cancellation, queues and cost (C-4 §2/§3/§7 with the C-2 §8 decisions).

The lead's ``routes.py`` calls ``register(PromptServer.instance.routes, prefix="/halofuse")``. This module never
imports ComfyUI's ``server``.
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
    client_ref = body.get("client_ref")
    try:
        rec = await _run(lambda: jobs.submit_render(
            workflow, instance_type=body.get("instance_type"),
            render_repeats=body.get("render_repeats", body.get("batch_count")),
            max_runtime_s=body.get("max_runtime_s"), max_job_cost_usd=body.get("max_job_cost_usd"),
            label=str(body.get("label") or "Render"), client_ref=client_ref,
            preview_mode=body.get("preview_mode"), preview_method=body.get("preview_method")))
    except HaloFuseError as exc:
        # A submit that failed AFTER the record was created (sign-in, upload, cloud refusal) left a failed job whose
        # log holds the failure: name it, so the panel and the console can open that log (owner 2026-11-23).
        job_id = await _run(lambda: default_store().find_by_client_ref(client_ref)) \
            if isinstance(client_ref, str) and client_ref else None
        resp = _error(exc.code, exc.message, exc.status)
        if job_id:
            env = json.loads(resp.body.decode("utf-8"))
            env["error"]["job_id"] = job_id
            resp = web.json_response(env, status=exc.status)
        return resp
    return web.json_response({"job": lifecycle.job_summary(rec, default_store())}, status=202)


RESEED_REFUSED = "No seed in this workflow can be changed safely. Change it in the graph and render again."


def _resubmit(job_id: str, stop_current: bool) -> dict:
    """Stop and try a new seed (C-4 6a): new seeds first; the old job is stopped only when a new workflow exists."""
    from . import reseed
    from . import workflow_analysis as wa

    store = default_store()
    rec = store.get(job_id)
    if rec is None:
        raise HaloFuseError("job_not_found", "This HaloFuse job was not found.", 404)
    if rec.get("kind") not in ("render", None):
        raise HaloFuseError("not_supported", "Stop and try a new seed works for single renders, not queues.", 409)
    try:
        workflow = json.loads(store.read_file(job_id, "workflow.json"))
    except (OSError, ValueError):
        raise HaloFuseError("no_workflow", "The workflow of this render was not kept, so it cannot be rendered "
                                           "again from here.", 409) from None
    types = {n.get("class_type") for n in workflow.values() if isinstance(n, dict) and n.get("class_type")}
    defs = wa.NodeDefs(wa.local_node_defs(types), wa._default_snapshot())
    node_defs = {ct: defs.node_def(ct) for ct in types if defs.node_def(ct) is not None}
    result = reseed.reseed(workflow, node_defs)
    if not result.changes:
        raise HaloFuseError("reseed_not_possible", RESEED_REFUSED, 422)
    stopped = None
    if stop_current and not rec.get("terminal"):
        lifecycle.request_cancel(job_id, "now", "user")
        stopped = job_id
    try:
        new = jobs.submit_render(
            result.workflow, instance_type=(rec.get("cloud") or {}).get("instance_type"),
            render_repeats=rec.get("render_repeats"), preview_mode=rec.get("preview_mode"),
            preview_method=rec.get("preview_method"), label=f"{rec.get('label') or 'Render'} (new seed)")
    except Exception as exc:  # noqa: BLE001
        err = lifecycle.as_halofuse_error(exc)
        if stopped:
            raise HaloFuseError(err.code, "The old render was stopped, but the new one could not start: "
                                + err.message, err.status) from None
        raise err from None
    return {"job": lifecycle.job_summary(new, store), "changes": result.changes, "skipped": result.skipped,
            "stopped": stopped}


@_handler
async def post_job_resubmit(request):
    job_id = _resolve(request.match_info["job_id"])
    body = await _json(request) if request.can_read_body else {}
    if body.get("reseed", True) is not True:
        raise HaloFuseError("bad_request", "Only a re-render with new seeds is supported.", 400)
    out = await _run(lambda: _resubmit(job_id, body.get("stop_current", True) is not False))
    return web.json_response(out, status=202)


DECODER_PREFIXES = ("taesd_decoder", "taesdxl_decoder", "taesd3_decoder", "taef1_decoder", "taehv", "lighttaew2_2",
                    "lighttaew2_1", "lighttaehy1_5", "taeltx_2", "taeh3")
DETAILED_UNAVAILABLE = ("No preview decoder files in models/vae_approx on this computer. Detailed preview needs them "
                        "(they are uploaded to the cloud model library with your consent).")


def preview_decoders() -> list:
    """Local small-decoder files ComfyUI uses for `--preview-method taesd` (models/vae_approx)."""
    try:
        import folder_paths  # ComfyUI-provided
        names = folder_paths.get_filename_list("vae_approx") or []
    except Exception:  # noqa: BLE001 - no folder registered / not inside ComfyUI
        return []
    out = []
    for name in names:
        base = str(name).replace(chr(92), "/").rsplit("/", 1)[-1]
        if base.startswith(DECODER_PREFIXES):
            out.append(str(name))
    return sorted(out)


@_handler
async def get_preview_methods(request):
    decoders = await _run(preview_decoders)
    return web.json_response({
        "methods": [
            {"id": "none", "label": "Off", "available": True, "reason": None},
            {"id": "latent2rgb", "label": "Fast", "available": True, "reason": None},
            {"id": "taesd", "label": "Detailed", "available": bool(decoders),
             "reason": None if decoders else DETAILED_UNAVAILABLE},
        ],
        "decoders": decoders, "experimental": True})


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
        ("POST", f"{p}/jobs/{{job_id}}/resubmit", post_job_resubmit),
        ("GET", f"{p}/preview_methods", get_preview_methods),
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

"""HTTP routes for the Workflow Check, Quick Draft, capacity, model sync and the cloud model library (C-4 section 5,
owner WS5). The lead registers them in routes.py:

    routes_preflight.register(PromptServer.instance.routes, prefix="/halofuse")

Every route goes through the single HaloFuse request guard (loopback peer, Host allowlist, `X-HaloFuse-Request: 1`).
Errors use the C-4 envelope with branded, URL-free text. Nothing here starts paid compute: the check only lists /
stats the library and reads the GPU catalogue.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

from aiohttp import web

from . import capacity, draft, lifecycle, model_library, model_sync, provider
from . import workflow_analysis as wa
from .job_store import HaloFuseError
from .routes_account import request_guard

_log = logging.getLogger(__name__)

MAX_WORKFLOWS = 50
_UPLOAD_ID_RE = re.compile(r"^[0-9a-f]{12}$")


async def _run(fn):
    return await asyncio.get_running_loop().run_in_executor(None, fn)


def _error(code: str, message: str, status: int) -> web.Response:
    return web.json_response({"error": {"code": code, "message": message, "id": lifecycle.new_error_id()}},
                             status=status)


def _from_exc(exc: BaseException) -> web.Response:
    err = lifecycle.as_halofuse_error(exc)
    return _error(err.code, err.message, err.status)


async def _body(request) -> dict:
    try:
        body = await request.json()
    except (ValueError, json.JSONDecodeError):
        raise HaloFuseError("bad_request", "The request body is not valid JSON.", 400) from None
    if not isinstance(body, dict):
        raise HaloFuseError("bad_request", "The request body must be a JSON object.", 400)
    return body


def _workflows(body: dict) -> list[dict]:
    workflows = body.get("workflows")
    if workflows is None and isinstance(body.get("workflow"), dict):
        workflows = [body["workflow"]]
    if not isinstance(workflows, list) or not workflows or not all(isinstance(w, dict) for w in workflows):
        raise HaloFuseError("bad_request", "Send the workflow(s) to check as a list of API-format graphs.", 400)
    if len(workflows) > MAX_WORKFLOWS:
        raise HaloFuseError("workflow_too_large", f"At most {MAX_WORKFLOWS} workflows can be checked at once.", 413)
    return workflows


# ---- seams the tests replace (production: ComfyUI's folders, the shipped snapshot, the C-1 port) ---------------

def _input_dir() -> Path | None:
    try:
        import folder_paths  # ComfyUI-provided
        return Path(folder_paths.get_input_directory())
    except Exception:  # noqa: BLE001 - outside ComfyUI the input check is skipped (and said so)
        return None


def _snapshot(profile):
    try:
        return wa.load_snapshot(profile)
    except ValueError:
        raise HaloFuseError("bad_request", "That runtime profile name is not valid.", 400) from None


def _settings() -> dict:
    from .config import load_settings

    return load_settings()


# ---- work functions (run in the executor) ------------------------------------------------------------------------

def _models_check(settings: dict):
    def check(refs):
        with provider.open_backend(settings) as be:
            return model_sync.classify_models(refs, settings, be)
    return check


def _sizes_from_models(models: dict) -> dict:
    sizes = {}
    for items in models.values():
        for item in items:
            size = item.get("size") or item.get("remote_size")
            if item.get("folder") and isinstance(size, int):
                sizes[(item["folder"], item["name"])] = size
    return sizes


def _capacity(workflows, models: dict, instance_type, snapshot) -> dict:
    defs = wa.NodeDefs(wa.local_node_defs(set().union(*(wa._class_types(w) for w in workflows))), snapshot)
    work = capacity.workload(workflows, node_defs=defs, model_sizes=_sizes_from_models(models))
    try:
        with provider.open_backend() as be:
            skus = be.list_skus()
    except Exception as exc:  # noqa: BLE001 - capacity degrades to "not checked", branded
        return {"error": lifecycle.as_halofuse_error(exc).message, "facts": None, "skus": [], "selected": None,
                "recommended": None, "blocked": False}
    return capacity.assess(work, skus, calibration=capacity.load_calibration(), instance_type=instance_type)


def _preflight(body: dict) -> dict:
    workflows = _workflows(body)
    instance_type = body.get("instance_type")
    if instance_type is not None and not isinstance(instance_type, str):
        raise HaloFuseError("bad_request", "instance_type must be text.", 400)
    snapshot = _snapshot(body.get("profile"))
    input_dir = _input_dir()
    report = wa.preflight(workflows, snapshot=snapshot, input_dir=input_dir,
                          models_check=_models_check(_settings()))
    if input_dir is None:
        report["warnings"].append(wa.Problem("inputs_not_checked", "Input files were not checked.").to_dict())
    if instance_type:
        cap = _capacity(workflows, report["models"], instance_type, snapshot)
        report["capacity"] = cap
        if cap.get("blocked"):
            for reason in (cap.get("selected") or {}).get("reasons", []):
                report["blocking"].append(wa.Problem("capacity_blocked", f"{instance_type}: {reason}").to_dict())
            report["ok"] = False
    return report


def _capacity_only(body: dict) -> dict:
    workflows = _workflows(body)
    snapshot = _snapshot(body.get("profile"))
    settings = _settings()
    refs, seen = [], set()
    for wf in workflows:
        for ref in wa.discover_models(wf):
            if ref.key() not in seen:
                seen.add(ref.key())
                refs.append(ref)
    try:
        models = _models_check(settings)(refs) if refs else {}
    except Exception:  # noqa: BLE001 - sizes then come from local files only
        models = {}
    return _capacity(workflows, models, body.get("instance_type"), snapshot)


def _draft(body: dict) -> dict:
    workflow = body.get("workflow")
    if not isinstance(workflow, dict) or not workflow:
        raise HaloFuseError("bad_request", "Send the workflow to draft as an API-format graph.", 400)
    options = body.get("options") or {}
    if not isinstance(options, dict):
        raise HaloFuseError("bad_request", "options must be an object.", 400)
    try:
        result = draft.make_draft(workflow, options, snapshot=_snapshot(body.get("profile")))
    except ValueError as exc:
        raise HaloFuseError("bad_request", str(exc)[:200], 400) from None
    return result.to_dict()


def _latent_checkpoint(body: dict) -> dict:
    workflow = body.get("workflow")
    if not isinstance(workflow, dict) or not workflow:
        raise HaloFuseError("bad_request", "Send the workflow as an API-format graph.", 400)
    return draft.insert_latent_checkpoints(workflow, snapshot=_snapshot(body.get("profile"))).to_dict()


def _library() -> dict:
    settings = _settings()
    with provider.open_backend(settings) as be:
        return model_library.library_report(be, settings)


# ---- handlers ----------------------------------------------------------------------------------------------------

def _json_handler(work):
    async def handler(request):
        try:
            body = await _body(request)
            return web.json_response(await _run(lambda: work(body)))
        except Exception as exc:  # noqa: BLE001 - branded envelope only
            return _from_exc(exc)
    handler.__name__ = work.__name__.lstrip("_")
    return handler


async def post_models_check(request):
    try:
        body = await _body(request)
        workflows = _workflows(body)
        return web.json_response(await _run(lambda: model_sync.check_models(workflows)))
    except Exception as exc:  # noqa: BLE001
        return _from_exc(exc)


async def post_models_upload(request):
    try:
        body = await _body(request)
        files = body.get("files")
        if not isinstance(files, list) or not files or not all(isinstance(f, dict) for f in files):
            raise HaloFuseError("bad_request", "Choose the models to upload.", 400)
        try:
            prepared = await _run(lambda: model_sync.prepare_upload_files(files))
        except ValueError as exc:
            raise HaloFuseError("bad_request", str(exc)[:300], 400) from None
        upload_id = model_sync.start_upload(prepared)
        return web.json_response({"upload_id": upload_id, "uploadId": upload_id}, status=202)
    except Exception as exc:  # noqa: BLE001
        return _from_exc(exc)


def _upload_id(request) -> str:
    upload_id = request.match_info["upload_id"]
    if not _UPLOAD_ID_RE.match(upload_id) or model_sync.get_upload_state(upload_id) is None:
        raise HaloFuseError("upload_not_found", "This model upload was not found.", 404)
    return upload_id


def _public_upload(state: dict) -> dict:
    """An upload registry entry without the local absolute path (TM-004)."""
    out = {k: v for k, v in state.items() if k != "cancel"}
    out["files"] = [{k: v for k, v in f.items() if k != "local_path"} for f in state["files"]]
    return out


async def get_models_upload(request):
    try:
        upload_id = _upload_id(request)
        return web.json_response({"upload_id": upload_id, **_public_upload(model_sync.get_upload_state(upload_id))})
    except Exception as exc:  # noqa: BLE001
        return _from_exc(exc)


async def get_models_uploads(request):
    return web.json_response({"uploads": {k: _public_upload(v) for k, v in model_sync.list_uploads().items()}})


async def post_models_upload_cancel(request):
    try:
        upload_id = _upload_id(request)
        model_sync.cancel_upload(upload_id)
        return web.json_response({"ok": True, "upload_id": upload_id}, status=202)
    except Exception as exc:  # noqa: BLE001
        return _from_exc(exc)


async def get_library(request):
    try:
        return web.json_response(await _run(_library))
    except Exception as exc:  # noqa: BLE001
        return _from_exc(exc)


async def get_aliases(request):
    return web.json_response({"aliases": await _run(model_library.load_aliases)})


async def post_aliases(request):
    try:
        body = await _body(request)
        try:
            saved = await _run(lambda: model_library.save_aliases(body.get("aliases")))
        except ValueError as exc:
            raise HaloFuseError("bad_request", str(exc)[:200], 400) from None
        return web.json_response({"aliases": saved})
    except Exception as exc:  # noqa: BLE001
        return _from_exc(exc)


# ---- registration ------------------------------------------------------------------------------------------------

_ROUTES = (
    ("POST", "/preflight", _json_handler(_preflight)),
    ("POST", "/capacity", _json_handler(_capacity_only)),
    ("POST", "/draft", _json_handler(_draft)),
    ("POST", "/latent_checkpoint", _json_handler(_latent_checkpoint)),
    ("POST", "/models/check", post_models_check),
    ("POST", "/models/upload", post_models_upload),
    ("GET", "/models/upload/{upload_id}", get_models_upload),
    ("GET", "/models/uploads", get_models_uploads),
    ("POST", "/models/upload/{upload_id}/cancel", post_models_upload_cancel),
    ("GET", "/library", get_library),
    ("GET", "/library/aliases", get_aliases),
    ("POST", "/library/aliases", post_aliases),
)


def _guarded(handler):
    async def wrapper(request):
        refused = request_guard(request, needs_header=True)
        return refused if refused is not None else await handler(request)

    wrapper.__name__ = handler.__name__
    return wrapper


def register(route_table, prefix: str = "/halofuse") -> None:
    """Add the WS5 routes under *prefix*; also records runner telemetry peaks for capacity calibration (lifecycle
    event hook)."""
    prefix = "/" + prefix.strip("/")
    for method, path, handler in _ROUTES:
        route_table.route(method, prefix + path)(_guarded(handler))
    lifecycle.register_event_hook(capacity.calibration_event_hook, name="ws5_capacity_calibration")

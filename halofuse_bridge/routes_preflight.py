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

from . import capacity, draft, lifecycle, model_fetch, model_library, model_sync, provider, user_nodes
from . import workflow_analysis as wa
from .job_store import HaloFuseError
from .routes_account import request_guard

_log = logging.getLogger(__name__)

MAX_WORKFLOWS = 50
_UPLOAD_ID_RE = re.compile(r"^[0-9a-f]{12}$")
# H2Vault (a separate pack): a locked vault is one of these nodes; its inner graph and models are encrypted in it.
VAULT_CLASSES = ("H2Vault_ProtectedSubgraph", "H2Vault_ProtectedSubgraphOutput")


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


def runtime_snapshot_profile(settings: dict | None = None) -> str:
    """The node-snapshot profile of the cloud runtime the artist selected (Advanced > Cloud runtime): the Workflow
    Check must check against the node set of the image the render will actually run on."""
    from .provider import image_contract

    if settings is None:
        try:
            settings = _settings()
        except Exception:  # noqa: BLE001 - unreadable settings -> the default contract, as load_settings does
            settings = {}
    contract = image_contract.CONTRACTS.get(settings.get("runtime_image"), image_contract.select(settings={}))
    return contract.snapshot_profile


def _snapshot(profile):
    if profile is None:
        profile = runtime_snapshot_profile()
    try:
        return wa.load_snapshot(profile)
    except ValueError:
        raise HaloFuseError("bad_request", "That runtime profile name is not valid.", 400) from None


def _settings() -> dict:
    from .config import load_settings

    return load_settings()


# ---- work functions (run in the executor) ------------------------------------------------------------------------

def _models_check(settings: dict, links=None):
    """links None: classification only. A list (the Workflow Check): models missing everywhere are looked up at their
    public download sources (model_fetch), the workflow's own links first."""
    def check(refs):
        with provider.open_backend(settings) as be:
            if links is None:
                return model_sync.classify_models(refs, settings, be)
            return model_sync.classify_models(refs, settings, be, find_sources=True, links=links)
    return check


def _model_links(body: dict, workflows: list[dict]) -> list[dict]:
    """The editor graph's node properties.models (sent by the panel as model_links) plus URL widgets of the API graphs."""
    links = model_fetch.clean_links(body.get("model_links"))
    for wf in workflows:
        links += model_fetch.links_from_prompt(wf)
    return links


def _vault_notes(workflows: list[dict]) -> list[dict]:
    """H2Vault vaults provision their own models (download links kept inside the vault): their inner models are never
    searched, logged or shown here - one note per vault node, by node id only."""
    notes = []
    for wf in workflows:
        for node_id, node in wa._nodes(wf):
            ct = node["class_type"]
            if ct in VAULT_CLASSES:
                notes.append(wa.Problem("vault_models", f"Node {node_id} (H2Vault vault): its models are handled by "
                                                        "the vault, which downloads them itself. Nothing to do here.",
                                        node_id, ct).to_dict())
    return notes


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


PREVIEW_DECODER_MISSING = "Detailed preview will fall back to Fast: no preview decoder files in models/vae_approx."


def preview_decoder_refs(preview_method) -> tuple[list, list]:
    """Detailed live preview (`taesd`) needs the small decoders in the cloud model library: the local
    models/vae_approx decoder files join the model check (and its upload consent). Returns (refs, warnings)."""
    if preview_method != "taesd":
        return [], []
    from . import routes_jobs

    names = routes_jobs.preview_decoders()
    if not names:
        return [], [wa.Problem("preview_decoder_missing", PREVIEW_DECODER_MISSING).to_dict()]
    refs = [wa.ModelRef(None, "HaloFuse live preview", "decoder", "vae_approx", n.replace(chr(92), "/"), "preview",
                        True) for n in names]
    return refs, []


RUNTIME_LABELS = {"legacy": "Original image", "halofuse": "HaloFuse image"}


def _offer_other_runtimes(report: dict, snapshot) -> None:
    """Owner 2026-11-23: a node missing from the selected cloud runtime may be in the other one. Name that runtime and
    the classes it covers (report["runtime_alternatives"]), so the panel offers a one-click switch instead of 'ask
    your administrator'."""
    from .provider import image_contract

    missing = sorted({b.get("class_type") for b in report.get("blocking", [])
                      if b.get("code") == "unknown_node" and b.get("class_type")})
    if not missing:
        return
    current = getattr(snapshot, "profile", None)
    offers = []
    for name, contract in image_contract.CONTRACTS.items():
        profile = contract.snapshot_profile
        if profile == current:
            continue
        try:
            other = wa.load_snapshot(profile)
        except ValueError:
            other = None
        if other is None:
            continue
        covers = [c for c in missing if c in other.object_info]
        if covers:
            offers.append({"runtime": name, "label": RUNTIME_LABELS.get(name, name), "covers": covers,
                           "still_missing": [c for c in missing if c not in covers]})
    if offers:
        report["runtime_alternatives"] = offers
        best = max(offers, key=lambda o: len(o["covers"]))
        for b in report.get("blocking", []):
            if b.get("code") == "unknown_node" and b.get("class_type") in best["covers"]:
                b["message"] = (f"{b['class_type']} is not in the selected cloud runtime, but the {best['label']} has "
                                f"it: switch under Advanced > Cloud runtime (or use the button below).")


def _preflight(body: dict) -> dict:
    workflows = _workflows(body)
    instance_type = body.get("instance_type")
    if instance_type is not None and not isinstance(instance_type, str):
        raise HaloFuseError("bad_request", "instance_type must be text.", 400)
    snapshot = _snapshot(body.get("profile"))
    input_dir = _input_dir()
    decoder_refs, decoder_warnings = preview_decoder_refs(body.get("preview_method"))
    report = wa.preflight(workflows, snapshot=snapshot, input_dir=input_dir,
                          models_check=_models_check(_settings(), _model_links(body, workflows)),
                          extra_refs=decoder_refs)
    report["warnings"].extend(decoder_warnings)
    report["notes"].extend(_vault_notes(workflows))
    # the artist's own custom nodes count as available (a note); missing classes get an "add this pack" offer
    user_nodes.apply_to_report(report, workflows, _settings())
    _offer_other_runtimes(report, snapshot)
    if input_dir is None:
        report["warnings"].append(wa.Problem("inputs_not_checked", "Input files were not checked.").to_dict())
    if instance_type:
        cap = _capacity(workflows, report["models"], instance_type, snapshot)
        report["capacity"] = cap
        if cap.get("blocked"):
            for reason in (cap.get("selected") or {}).get("reasons", []):
                report["blocking"].append(wa.Problem("capacity_blocked", f"{instance_type}: {reason}").to_dict())
            report["ok"] = False
        _offer_capacity_switch(report, cap)
    return report


def _gb_text(value) -> str:
    return f"{value:g} GB" if isinstance(value, (int, float)) else "unknown"


def _offer_capacity_switch(report: dict, cap: dict) -> None:
    """L9.338 memory guard before paying: a machine that is likely too small (or blocked) gets a plain-English warning
    and report["capacity_switch"], which the panel shows as a one-click switch (owner: no dead ends). Nothing is
    blocked by this: the artist may still render on the selected machine."""
    switch = cap.get("switch")
    selected = cap.get("selected") or {}
    if selected.get("verdict") == "likely_insufficient":
        fix = (f" Switch to {switch['instance_type']} ({switch['gpu_type']}, {_gb_text(switch['gpu_memory_gb'])} GPU "
               f"memory, {_gb_text(switch['memory_gb'])} RAM) with the button below, or render anyway."
               if switch else " No listed GPU type has enough RAM by this estimate; render anyway or use smaller models.")
        report["warnings"].append(wa.Problem(
            "capacity_memory", f"{selected.get('instance_type')} is likely too small for this workflow: "
                               f"{(selected.get('reasons') or ['not enough memory'])[0]}.{fix}").to_dict())
    if switch:
        report["capacity_switch"] = switch


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


def _resolve_user_nodes(body: dict) -> dict:
    """Check the artist's custom node lines against the public GitHub / Comfy Registry APIs (nothing is saved)."""
    text = body.get("lines")
    if not isinstance(text, str):
        raise HaloFuseError("bad_request", "Send the custom node lines as text.", 400)
    if len(text) > user_nodes.MAX_TEXT:
        raise HaloFuseError("bad_request", f"The list is too long: at most {user_nodes.MAX_PACKS} packs.", 413)
    out = user_nodes.resolve_text(text)
    out["supported"], out["note"] = user_nodes.runtime_support(_settings())
    return out


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
            upload_id = model_sync.start_upload(prepared)   # refuses a model already being uploaded (L9.340)
        except ValueError as exc:
            raise HaloFuseError("bad_request", str(exc)[:300], 400) from None
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


async def post_models_fetch(request):
    """Download models from their public source into this computer's model folders (the Workflow Check found the
    source, or the artist pasted a link / picked a candidate). Body: {files: [{folder, name, url?, choice?}]}."""
    try:
        body = await _body(request)
        files = body.get("files")
        if not isinstance(files, list) or not files or not all(isinstance(f, dict) for f in files):
            raise HaloFuseError("bad_request", "Choose the models to download.", 400)
        try:
            prepared = await _run(lambda: model_fetch.prepare_fetch(files, _settings()))
            fetch_id = model_fetch.start_fetch(prepared)   # refuses a model already being downloaded (L9.340)
        except model_fetch.FetchError as exc:
            raise HaloFuseError("bad_request", str(exc)[:600], 400) from None
        return web.json_response({"fetch_id": fetch_id, **model_fetch.get_fetch_state(fetch_id)}, status=202)
    except Exception as exc:  # noqa: BLE001
        return _from_exc(exc)


def _fetch_id(request) -> str:
    fetch_id = request.match_info["fetch_id"]
    if not _UPLOAD_ID_RE.match(fetch_id) or model_fetch.get_fetch_state(fetch_id) is None:
        raise HaloFuseError("fetch_not_found", "This model download was not found.", 404)
    return fetch_id


async def get_models_fetch(request):
    try:
        fetch_id = _fetch_id(request)
        return web.json_response({"fetch_id": fetch_id, **model_fetch.get_fetch_state(fetch_id)})
    except Exception as exc:  # noqa: BLE001
        return _from_exc(exc)


async def get_models_fetches(request):
    return web.json_response({"fetches": model_fetch.list_fetches()})


async def post_models_fetch_cancel(request):
    try:
        fetch_id = _fetch_id(request)
        model_fetch.cancel_fetch(fetch_id)
        return web.json_response({"ok": True, "fetch_id": fetch_id}, status=202)
    except Exception as exc:  # noqa: BLE001
        return _from_exc(exc)


async def post_hf_token(request):
    """Save the optional Hugging Face token (gated repositories) in this computer's keychain. Never echoed."""
    from . import credentials

    try:
        body = await _body(request)
        try:
            status = await _run(lambda: credentials.default_store().save_hf_token(body.get("token")))
        except credentials.CredentialError as exc:
            if exc.code == "invalid_input":
                raise HaloFuseError("bad_request", "That is not a Hugging Face access token (it starts with hf_).",
                                    400) from None
            raise HaloFuseError(exc.code, exc.user_message, exc.status) from None
        return web.json_response(status)
    except Exception as exc:  # noqa: BLE001
        return _from_exc(exc)


async def delete_hf_token(request):
    from . import credentials

    try:
        return web.json_response(await _run(lambda: credentials.default_store().clear_hf_token()))
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
    ("POST", "/user_nodes/resolve", _json_handler(_resolve_user_nodes)),
    ("POST", "/models/check", post_models_check),
    ("POST", "/models/upload", post_models_upload),
    ("GET", "/models/upload/{upload_id}", get_models_upload),
    ("GET", "/models/uploads", get_models_uploads),
    ("POST", "/models/upload/{upload_id}/cancel", post_models_upload_cancel),
    ("POST", "/models/fetch", post_models_fetch),
    ("GET", "/models/fetch/{fetch_id}", get_models_fetch),
    ("GET", "/models/fetches", get_models_fetches),
    ("POST", "/models/fetch/{fetch_id}/cancel", post_models_fetch_cancel),
    ("POST", "/models/hf_token", post_hf_token),
    ("DELETE", "/models/hf_token", delete_hf_token),
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

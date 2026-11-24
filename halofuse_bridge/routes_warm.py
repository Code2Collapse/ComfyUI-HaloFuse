"""HTTP routes for the warm cloud machine (warm_machine.py): status, start, shut down, check; the warm mode, render
intent (Smart Warm), "keep it warm" and the model-cache status / partial-download cleanup (L9.340).

Same guard and error envelope as routes_jobs (loopback peer + Host allowlist; the X-HaloFuse-Request header on
writes). Every answer is warm_machine.view(): branded text, no handle, URL, host or credential.
"""
from __future__ import annotations

import logging

from aiohttp import web

from . import config, lifecycle, warm_machine
from .job_store import HaloFuseError
from .routes_jobs import _handler, _json, _run

_log = logging.getLogger(__name__)


def _view_response(view: dict, status: int = 200) -> web.Response:
    return web.json_response({"warm": view}, status=status)


@_handler
async def get_warm(request):
    return _view_response(await _run(lambda: warm_machine.view()))


@_handler
async def post_warm_start(request):
    body = await _json(request) if request.can_read_body else {}
    sku = body.get("instance_type")
    if sku is not None and not isinstance(sku, str):
        sku = None
    return _view_response(await _run(lambda: warm_machine.start(sku or None)), 202)


@_handler
async def post_warm_shutdown(request):
    return _view_response(await _run(lambda: warm_machine.shutdown(reason="user")), 202)


@_handler
async def post_warm_check(request):
    return _view_response(await _run(lambda: warm_machine.check()))


@_handler
async def post_warm_mode(request):
    """{warm_mode: off|always|smart, warm_idle_timeout_seconds?: int}: saved, then Always starts the machine now."""
    body = await _json(request) if request.can_read_body else {}
    prefs = {k: body[k] for k in ("warm_mode", "warm_idle_timeout_seconds") if k in body}
    bad = config.invalid_prefs(prefs)
    if not prefs or bad:
        raise HaloFuseError("bad_request", "Choose Off, Always Warm or Smart Warm, and a warm duration between 5 "
                                           "minutes and 7 days.", 400)

    def apply():
        config.save_settings(prefs)
        return warm_machine.apply_mode()
    return _view_response(await _run(apply))


@_handler
async def post_warm_intent(request):
    """The panel: a Workflow Check passed when Run was pressed (Smart Warm starts the machine). Never opens a machine
    in Off mode; answers {intent: {action, mode, message?}}."""
    body = await _json(request) if request.can_read_body else {}
    sku = body.get("instance_type")
    sku = sku if isinstance(sku, str) and sku else None
    reason = body.get("reason") if body.get("reason") in ("run", "models") else "run"
    return web.json_response({"intent": await _run(lambda: warm_machine.intent(reason, sku))})


@_handler
async def post_warm_keep(request):
    return _view_response(await _run(lambda: warm_machine.keep_longer()))


@_handler
async def get_warm_cache(request):
    from . import model_cache
    return web.json_response({"cache": await _run(model_cache.status)})


@_handler
async def post_warm_cache_cleanup(request):
    """Remove partial downloads nothing has used for 7 days. Never a complete model file, never the cloud library."""
    from . import model_cache
    result = await _run(lambda: model_cache.cleanup_partials(only_abandoned=True))
    return web.json_response({"cleanup": result, "cache": await _run(model_cache.status)})


def _table(prefix: str):
    p = prefix.rstrip("/")
    return [
        ("GET", f"{p}/warm", get_warm),
        ("POST", f"{p}/warm/start", post_warm_start),
        ("POST", f"{p}/warm/shutdown", post_warm_shutdown),
        ("POST", f"{p}/warm/check", post_warm_check),
        ("POST", f"{p}/warm/mode", post_warm_mode),
        ("POST", f"{p}/warm/intent", post_warm_intent),
        ("POST", f"{p}/warm/keep", post_warm_keep),
        ("GET", f"{p}/warm/cache", get_warm_cache),
        ("POST", f"{p}/warm/cache/cleanup", post_warm_cache_cleanup),
    ]


def register(routes, prefix: str = "/halofuse", *, boot: bool = True) -> None:
    for method, path, handler in _table(prefix):
        getattr(routes, method.lower())(path)(handler)
    lifecycle.register_event_hook(warm_machine.observe_event, name="warm_machine_features")
    if boot:
        try:
            warm_machine.boot()
        except Exception:  # noqa: BLE001 - never stops ComfyUI from starting
            _log.warning("HaloFuse could not re-check the warm machine at startup")

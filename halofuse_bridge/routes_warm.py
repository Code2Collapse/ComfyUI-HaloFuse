"""HTTP routes for the warm cloud machine (warm_machine.py): status, start, shut down, check.

Same guard and error envelope as routes_jobs (loopback peer + Host allowlist; the X-HaloFuse-Request header on
writes). Every answer is warm_machine.view(): branded text, no handle, URL, host or credential.
"""
from __future__ import annotations

import logging

from aiohttp import web

from . import lifecycle, warm_machine
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


def _table(prefix: str):
    p = prefix.rstrip("/")
    return [
        ("GET", f"{p}/warm", get_warm),
        ("POST", f"{p}/warm/start", post_warm_start),
        ("POST", f"{p}/warm/shutdown", post_warm_shutdown),
        ("POST", f"{p}/warm/check", post_warm_check),
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

"""HTTP routes for Live ComfyUI (live_comfy.py, L9.345): status, start, open (one-shot link), stop.

Same guard and error envelope as routes_jobs. The user is ComfyUI's "comfy-user" header (multi-user mode), else
"default". No answer carries a token except /open, whose link is single-use-short (2 minutes) and bound to this
user's own session.
"""
from __future__ import annotations

import logging

from aiohttp import web

from . import live_comfy
from .routes_jobs import _handler, _json, _run

_log = logging.getLogger(__name__)


def _user(request) -> str:
    return request.headers.get("comfy-user") or "default"


@_handler
async def get_live(request):
    user = _user(request)
    return web.json_response({"live": await _run(lambda: live_comfy.view(user))})


@_handler
async def post_live_start(request):
    user = _user(request)
    body = await _json(request) if request.can_read_body else {}
    sku = body.get("instance_type")
    sku = sku if isinstance(sku, str) and sku else None
    return web.json_response({"live": await _run(lambda: live_comfy.start(user, sku))}, status=202)


@_handler
async def post_live_open(request):
    user = _user(request)
    return web.json_response({"url": await _run(lambda: live_comfy.open_url(user))})


@_handler
async def post_live_stop(request):
    user = _user(request)
    return web.json_response({"live": await _run(lambda: live_comfy.stop(user))}, status=202)


def _table(prefix: str):
    p = prefix.rstrip("/")
    return [
        ("GET", f"{p}/live", get_live),
        ("POST", f"{p}/live/start", post_live_start),
        ("POST", f"{p}/live/open", post_live_open),
        ("POST", f"{p}/live/stop", post_live_stop),
    ]


def register(routes, prefix: str = "/halofuse", *, boot: bool = True) -> None:
    for method, path, handler in _table(prefix):
        getattr(routes, method.lower())(path)(handler)
    if boot:
        try:
            live_comfy.recover()
        except Exception:  # noqa: BLE001 - never stops ComfyUI from starting
            _log.warning("HaloFuse could not re-check Live ComfyUI sessions at startup")

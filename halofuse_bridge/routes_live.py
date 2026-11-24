"""HTTP routes for Live ComfyUI (live_comfy.py, L9.345): status, start, open (one-shot link), stop.

Same guard and error envelope as routes_jobs. The user is ComfyUI's "comfy-user" header (multi-user mode), else
"default". No answer carries a token except /open, whose link is single-use-short (2 minutes) and bound to this
user's own session.
"""
from __future__ import annotations

import logging

from aiohttp import web

from . import live_comfy, live_tunnel
from .job_store import HaloFuseError
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


# ---- one-click host on this computer (live_tunnel.py): switch, cloudflared download, named-tunnel token ----------

def _settings() -> dict:
    return live_comfy._settings(None)


@_handler
async def get_live_host(request):
    return web.json_response({"host": live_tunnel.host().status()})


@_handler
async def post_live_host(request):
    body = await _json(request) if request.can_read_body else {}
    on = body.get("on")
    if not isinstance(on, bool):
        raise HaloFuseError("bad_request", "Send on: true or false.", 400)

    def act():
        from . import config
        settings = config.save_live_host_settings({"live_comfy_enabled": on})
        h = live_tunnel.host()
        return h.start(settings) if on else h.stop()
    return web.json_response({"host": await _run(act)})


@_handler
async def post_live_host_download(request):
    # the ONLY place a cloudflared download starts: the artist pressed "Download cloudflared"
    return web.json_response({"host": await _run(lambda: live_tunnel.host().download(_settings()))})


@_handler
async def post_live_tunnel(request):
    """Named tunnel: {token, hostname} saves both; {clear: true} goes back to a quick tunnel. Never echoes the token."""
    body = await _json(request)
    from . import config, credentials

    def act():
        store = credentials.default_store()
        if body.get("clear") is True:
            store.clear_tunnel_token()
            config.save_live_host_settings({"live_tunnel_hostname": ""})
        else:
            host = body.get("hostname")
            if not isinstance(host, str) or not live_tunnel._HOSTNAME_RE.match(host.strip()):
                raise HaloFuseError("bad_request", "Enter the tunnel's host name, for example live.example.com.", 400)
            try:
                store.save_tunnel_token(body.get("token"))
            except credentials.CredentialError as exc:
                msg = ("That does not look like a tunnel token." if exc.code == "invalid_input"
                       else "This computer has no system keychain to keep the tunnel token in.")
                raise HaloFuseError("bad_request", msg, 400) from None
            config.save_live_host_settings({"live_tunnel_hostname": host.strip()})
        h = live_tunnel.host()
        if h.on:  # restart the tunnel with the new mode
            h.stop()
            return h.start(_settings())
        return h.status()
    return web.json_response({"host": await _run(act)})


def _boot_host() -> None:
    """The switch stays on across restarts: start the host again (in the background) when it was on."""
    s = _settings()
    if s.get("live_comfy_enabled") and not str(s.get("live_gateway_url") or "").strip():
        import threading
        threading.Thread(target=lambda: live_tunnel.host().start(s), name="halofuse-live-host-boot",
                         daemon=True).start()


def _table(prefix: str):
    p = prefix.rstrip("/")
    return [
        ("GET", f"{p}/live", get_live),
        ("POST", f"{p}/live/start", post_live_start),
        ("POST", f"{p}/live/open", post_live_open),
        ("POST", f"{p}/live/stop", post_live_stop),
        ("GET", f"{p}/live/host", get_live_host),
        ("POST", f"{p}/live/host", post_live_host),
        ("POST", f"{p}/live/host/download", post_live_host_download),
        ("POST", f"{p}/live/tunnel", post_live_tunnel),
    ]


def register(routes, prefix: str = "/halofuse", *, boot: bool = True) -> None:
    for method, path, handler in _table(prefix):
        getattr(routes, method.lower())(path)(handler)
    if boot:
        try:
            live_comfy.recover()
            _boot_host()
        except Exception:  # noqa: BLE001 - never stops ComfyUI from starting
            _log.warning("HaloFuse could not re-check Live ComfyUI sessions at startup")

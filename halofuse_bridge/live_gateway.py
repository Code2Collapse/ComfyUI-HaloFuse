"""Live ComfyUI relay gateway (L9.345). DISABLED by default: it only runs when someone starts it on purpose.

The cloud container publishes no port, so a live ComfyUI is reached through a relay: the container's agent dials OUT
to this gateway over one authenticated WebSocket, and browsers reach the gateway over HTTPS. Every browser request
and WebSocket is multiplexed onto the agent's connection. Hosting this gateway on a public HTTPS address is an
owner decision (docs/LIVE_COMFYUI.md).

Auth: the agent shows an "agent" token; a browser arrives once with a short "viewer" token (/live/open?t=...) and
gets an HttpOnly cookie holding a "cookie" token bound to that one session; the bridge polls /status/<sid> with a
"bridge" token. Without a valid token nothing is relayed (401). A cookie for one session never reaches another.

Run: ``HALOFUSE_LIVE_SECRET=... python -m halofuse_bridge.live_gateway --host 0.0.0.0 --port 8443 --cert c --key k``
(or behind a TLS-terminating proxy). Plain HTTP is allowed ONLY on a loopback address (127.0.0.1 / ::1) behind a
tunnel or proxy (--behind-tunnel / --behind-tls-proxy); a plain-HTTP bind to any other address is refused
(check_bind). The one-click host (live_tunnel.py) runs this app in-process on 127.0.0.1 behind cloudflared.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import itertools
import json
import logging
import os
import time

from aiohttp import WSMsgType, web

try:  # run as a module inside the package, or as a file next to live_proto.py
    from . import live_proto as proto
except ImportError:  # pragma: no cover
    import live_proto as proto  # type: ignore[no-redef]

_log = logging.getLogger("halofuse.live_gateway")

COOKIE_TTL_S = 900
REQUEST_TIMEOUT_S = 120.0
AGENT_GRACE_S = 300.0
MAX_BODY = 512 * 1024 * 1024
GATEWAY_KEY = web.AppKey("gateway", object)


class _Session:
    def __init__(self, sid: str, user: str, agent_exp: int) -> None:
        self.sid, self.user, self.agent_exp = sid, user, agent_exp
        self.ws: web.WebSocketResponse | None = None
        self.pending: dict[int, asyncio.Future] = {}
        self.sockets: dict[int, web.WebSocketResponse] = {}
        self.last_activity = time.time()
        self.gone_since: float | None = None

    async def send(self, msg: dict) -> None:
        if self.ws is None or self.ws.closed:
            raise ConnectionError("agent not connected")
        await self.ws.send_str(json.dumps(msg, separators=(",", ":")))

    def fail_all(self) -> None:
        for fut in self.pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("agent disconnected"))
        self.pending.clear()
        for ws in list(self.sockets.values()):
            asyncio.ensure_future(ws.close())
        self.sockets.clear()


class Gateway:
    def __init__(self, secret, *, now=time.time, max_body: int = MAX_BODY,
                 request_timeout_s: float = REQUEST_TIMEOUT_S, agent_grace_s: float = AGENT_GRACE_S,
                 secure_cookie: bool = True) -> None:
        proto._key(secret)  # refuse a missing / short secret at construction
        self.secret = secret
        self.now = now
        self.max_body = max_body
        self.request_timeout_s = request_timeout_s
        self.agent_grace_s = agent_grace_s
        self.secure_cookie = secure_cookie
        self.sessions: dict[str, _Session] = {}
        self._ids = itertools.count(1)

    # ---- agent -------------------------------------------------------------------------------------------------
    async def agent(self, request: web.Request):
        try:
            claims = proto.verify(self.secret, proto.bearer(request.headers), "agent", now=self.now())
        except proto.TokenError:
            return web.Response(status=401, text="unauthorized")
        sid = claims["sid"]
        sess = self.sessions.get(sid)
        if sess is None or sess.user != claims["user"]:
            sess = _Session(sid, claims["user"], claims["exp"])
            self.sessions[sid] = sess
        if sess.ws is not None and not sess.ws.closed:  # a reconnecting agent replaces the old link
            old = sess.ws
            sess.fail_all()
            await old.close()
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=self.max_body * 2)
        await ws.prepare(request)
        sess.ws, sess.gone_since, sess.agent_exp = ws, None, claims["exp"]
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                await self._from_agent(sess, data)
        finally:
            if sess.ws is ws:
                sess.ws = None
                sess.gone_since = self.now()
                sess.fail_all()
        return ws

    async def _from_agent(self, sess: _Session, data: dict) -> None:
        t, rid = data.get("t"), data.get("id")
        if t == "res":
            fut = sess.pending.pop(rid, None)
            if fut is not None and not fut.done():
                fut.set_result(data)
        elif t == "ws_msg":
            ws = sess.sockets.get(rid)
            if ws is not None and not ws.closed:
                if data.get("kind") == "binary":
                    await ws.send_bytes(base64.b64decode(data.get("data") or ""))
                else:
                    await ws.send_str(data.get("data") or "")
        elif t == "ws_close":
            ws = sess.sockets.pop(rid, None)
            if ws is not None:
                await ws.close()

    # ---- browser -----------------------------------------------------------------------------------------------
    async def open(self, request: web.Request):
        try:
            claims = proto.verify(self.secret, request.query.get("t"), "viewer", now=self.now())
        except proto.TokenError:
            return web.Response(status=401, text="This Live ComfyUI link has expired. Open it again from HaloFuse.")
        cookie = proto.mint(self.secret, claims["sid"], "cookie", claims["user"], COOKIE_TTL_S, now=self.now())
        resp = web.HTTPFound("/")
        resp.set_cookie(proto.COOKIE_NAME, cookie, max_age=COOKIE_TTL_S, httponly=True, secure=self.secure_cookie,
                        samesite="Lax", path="/")
        raise resp

    def _viewer(self, request: web.Request):
        try:
            claims = proto.verify(self.secret, request.cookies.get(proto.COOKIE_NAME), "cookie", now=self.now())
        except proto.TokenError:
            return None, None
        sess = self.sessions.get(claims["sid"])
        if sess is None or sess.user != claims["user"]:
            return claims, None
        return claims, sess

    def _renew(self, resp, claims: dict) -> None:
        if claims["exp"] - self.now() < COOKIE_TTL_S / 2:  # sliding renewal while the viewer is active
            resp.set_cookie(proto.COOKIE_NAME, proto.mint(self.secret, claims["sid"], "cookie", claims["user"],
                                                          COOKIE_TTL_S, now=self.now()),
                            max_age=COOKIE_TTL_S, httponly=True, secure=self.secure_cookie, samesite="Lax", path="/")

    async def proxy(self, request: web.Request):
        claims, sess = self._viewer(request)
        if claims is None:
            return web.Response(status=401, text="Open Live ComfyUI from the HaloFuse panel.")
        if sess is None or sess.ws is None or sess.ws.closed:
            return web.Response(status=502, text="The live ComfyUI is not connected (starting, stopped or lost).")
        sess.last_activity = self.now()
        rid = next(self._ids)
        path = request.rel_url.raw_path_qs if hasattr(request.rel_url, "raw_path_qs") else str(request.rel_url)
        headers = proto.forward_headers(request.headers)
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await self._proxy_ws(request, sess, rid, path, headers, claims)
        body = await request.read() if request.can_read_body else b""
        if len(body) > self.max_body:
            return web.Response(status=413, text="request too large")
        fut = asyncio.get_running_loop().create_future()
        sess.pending[rid] = fut
        try:
            await sess.send({"t": "req", "id": rid, "method": request.method, "path": path, "headers": headers,
                             "body": base64.b64encode(body).decode("ascii")})
            res = await asyncio.wait_for(fut, self.request_timeout_s)
        except (ConnectionError, asyncio.TimeoutError):
            sess.pending.pop(rid, None)
            return web.Response(status=502, text="The live ComfyUI did not answer.")
        resp = web.Response(status=int(res.get("status") or 502), body=base64.b64decode(res.get("body") or ""),
                            headers=proto.forward_headers(res.get("headers") or {}))
        self._renew(resp, claims)
        return resp

    async def _proxy_ws(self, request, sess: _Session, rid: int, path: str, headers: dict, claims: dict):
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=self.max_body)
        await ws.prepare(request)
        sess.sockets[rid] = ws
        try:
            await sess.send({"t": "ws_open", "id": rid, "path": path, "headers": headers})
            async for msg in ws:
                sess.last_activity = self.now()
                if msg.type == WSMsgType.TEXT:
                    await sess.send({"t": "ws_msg", "id": rid, "kind": "text", "data": msg.data})
                elif msg.type == WSMsgType.BINARY:
                    await sess.send({"t": "ws_msg", "id": rid, "kind": "binary",
                                     "data": base64.b64encode(msg.data).decode("ascii")})
        except ConnectionError:
            pass
        finally:
            if sess.sockets.pop(rid, None) is not None:
                with contextlib.suppress(ConnectionError):
                    await sess.send({"t": "ws_close", "id": rid})
        return ws

    # ---- bridge ------------------------------------------------------------------------------------------------
    async def status(self, request: web.Request):
        sid = request.match_info["sid"]
        try:
            proto.verify(self.secret, proto.bearer(request.headers), "bridge", sid=sid, now=self.now())
        except proto.TokenError:
            return web.Response(status=401, text="unauthorized")
        sess = self.sessions.get(sid)
        return web.json_response({
            "agent_connected": bool(sess and sess.ws is not None and not sess.ws.closed),
            "last_activity": sess.last_activity if sess else None,
            "viewers": len(sess.sockets) if sess else 0,
        })

    # ---- cleanup -----------------------------------------------------------------------------------------------
    def sweep(self) -> list[str]:
        """Drop sessions whose agent is gone past the grace period or whose agent token expired."""
        now, gone = self.now(), []
        for sid, sess in list(self.sessions.items()):
            dead = sess.gone_since is not None and now - sess.gone_since > self.agent_grace_s
            if dead or now >= sess.agent_exp:
                if sess.ws is not None:
                    asyncio.ensure_future(sess.ws.close())
                sess.fail_all()
                del self.sessions[sid]
                gone.append(sid)
        return gone

    async def _sweeper(self, app):
        async def loop():
            while True:
                await asyncio.sleep(30)
                self.sweep()
        task = asyncio.create_task(loop())
        yield
        task.cancel()


def check_bind(host: str, *, tls: bool) -> None:
    """Plain HTTP only on loopback (live_tunnel.check_bind)."""
    try:
        from .live_tunnel import check_bind as _cb
    except ImportError:  # pragma: no cover - run as a loose file
        import ipaddress
        if not tls and host != "localhost":
            try:
                ok = ipaddress.ip_address(host).is_loopback
            except ValueError:
                ok = False
            if not ok:
                raise ValueError(f"refusing a plain-HTTP gateway on {host}: plain HTTP is allowed on 127.0.0.1 only")
        return
    _cb(host, tls=tls)


def make_gateway_app(secret, **kw) -> web.Application:
    gw = Gateway(secret, **kw)
    app = web.Application(client_max_size=gw.max_body)
    app[GATEWAY_KEY] = gw
    app.router.add_get("/agent", gw.agent)
    app.router.add_get("/live/open", gw.open)
    app.router.add_get("/status/{sid}", gw.status)
    app.router.add_route("*", "/{tail:.*}", gw.proxy)
    app.cleanup_ctx.append(gw._sweeper)
    return app


def main(argv=None) -> int:  # pragma: no cover - operator entry point
    ap = argparse.ArgumentParser(description="HaloFuse Live ComfyUI relay gateway (owner-hosted, off by default)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8443)
    ap.add_argument("--cert")
    ap.add_argument("--key")
    ap.add_argument("--behind-tls-proxy", action="store_true", help="a TLS proxy in front terminates HTTPS")
    ap.add_argument("--behind-tunnel", action="store_true", help="a tunnel (cloudflared) in front; loopback only")
    args = ap.parse_args(argv)
    secret = os.environ.get("HALOFUSE_LIVE_SECRET", "")
    try:
        proto._key(secret)
    except proto.TokenError as exc:
        print(f"refusing to start: {exc} (set HALOFUSE_LIVE_SECRET)")
        return 2
    ssl_ctx = None
    if args.cert and args.key:
        import ssl
        ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_ctx.load_cert_chain(args.cert, args.key)
    elif not (args.behind_tls_proxy or args.behind_tunnel):
        print("refusing to start without TLS: pass --cert/--key, --behind-tls-proxy or --behind-tunnel")
        return 2
    try:
        check_bind(args.host, tls=ssl_ctx is not None)
    except ValueError as exc:
        print(str(exc))
        return 2
    web.run_app(make_gateway_app(secret), host=args.host, port=args.port, ssl_context=ssl_ctx)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

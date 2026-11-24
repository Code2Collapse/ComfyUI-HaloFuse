"""HaloFuse Live ComfyUI agent (L9.345): an interactive ComfyUI inside a cloud job, reached through an OUTBOUND relay.

The cloud publishes no port. This agent starts ComfyUI on container loopback at LIVE_PORT (never 8188, which stays
the headless render port), then dials the HaloFuse relay gateway over wss:// with a short-lived agent token and
relays the browser's HTTP requests and WebSockets to ComfyUI. Nothing listens on a public interface.

Env: LIVE_PORT, GATEWAY_URL (wss://.../agent), AGENT_TOKEN, optional COMFY_ARGS (space separated),
COMFYUI_DIR, LIVE_HEARTBEAT_S (60). HALOFUSE_LIVE_ALLOW_INSECURE=1 permits ws:// (tests only).
Prints `[hf] {"phase":"live",...}` heartbeats so the cloud's inactivity detector sees a living container.
Exit codes: 0 stopped, 2 bad configuration, 6 ComfyUI failed to start or died.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import signal
import subprocess
import sys
import time

RENDER_PORT = 8188
HOP = frozenset(("connection", "keep-alive", "transfer-encoding", "upgrade", "host", "content-length",
                 "content-encoding", "authorization", "cookie", "set-cookie", "te", "trailer",
                 "proxy-authorization", "proxy-authenticate"))


def log(text: str, **fields) -> None:
    print(f"[hf] {json.dumps({'phase': 'live', 'msg': text, **fields}, separators=(',', ':'))}", flush=True)


class ConfigError(ValueError):
    pass


def read_config(env=None) -> dict:
    env = os.environ if env is None else env
    try:
        port = int(env.get("LIVE_PORT", ""))
    except ValueError:
        raise ConfigError("LIVE_PORT is not a number") from None
    if port == RENDER_PORT or not 1024 < port < 65536:
        raise ConfigError(f"LIVE_PORT {port} is not allowed (8188 is the render port)")
    url = env.get("GATEWAY_URL", "")
    insecure = env.get("HALOFUSE_LIVE_ALLOW_INSECURE") == "1"
    if not (url.startswith("wss://") or (insecure and url.startswith("ws://"))):
        raise ConfigError("GATEWAY_URL must be a wss:// address")
    token = env.get("AGENT_TOKEN", "")
    if not token:
        raise ConfigError("AGENT_TOKEN is missing")
    return {"port": port, "gateway": url, "token": token, "args": (env.get("COMFY_ARGS") or "").split(),
            "comfy_dir": env.get("COMFYUI_DIR", "/default-comfyui-bundle/ComfyUI"),
            "heartbeat_s": float(env.get("LIVE_HEARTBEAT_S") or 60)}


def _clean(headers) -> dict:
    return {k: v for k, v in headers.items() if k.lower() not in HOP}


class Agent:
    """Relay between one gateway connection and the local ComfyUI. Testable without ComfyUI."""

    def __init__(self, comfy_base_url: str, gateway_url: str, token: str, *, heartbeat_s: float = 60.0) -> None:
        self.comfy = comfy_base_url.rstrip("/")
        self.gateway = gateway_url
        self.token = token
        self.heartbeat_s = heartbeat_s
        self.sockets: dict = {}
        self._stop = asyncio.Event()
        self.connected = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self, *, max_backoff: float = 30.0) -> None:
        import aiohttp
        backoff = 1.0
        async with aiohttp.ClientSession() as http:
            self.http = http
            beat = asyncio.create_task(self._heartbeat())
            try:
                while not self._stop.is_set():
                    try:
                        async with http.ws_connect(self.gateway, headers={"Authorization": f"Bearer {self.token}"},
                                                   heartbeat=30, max_msg_size=0) as gw:
                            backoff = 1.0
                            self.connected.set()
                            await self._pump(gw)
                    except aiohttp.WSServerHandshakeError as exc:
                        if exc.status == 401:
                            log("the gateway refused the agent token; stopping")
                            return
                        log("gateway handshake failed", status=exc.status)
                    except (aiohttp.ClientError, OSError) as exc:
                        log("gateway connection lost", error=type(exc).__name__)
                    self.connected.clear()
                    if self._stop.is_set():
                        break
                    try:
                        await asyncio.wait_for(self._stop.wait(), backoff)
                    except asyncio.TimeoutError:
                        pass
                    backoff = min(backoff * 2, max_backoff)
            finally:
                beat.cancel()
                for ws in list(self.sockets.values()):
                    await ws.close()

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_s)
            log("alive", viewers=len(self.sockets))

    async def _pump(self, gw) -> None:
        import aiohttp
        stop = asyncio.create_task(self._stop.wait())
        try:
            while True:
                recv = asyncio.create_task(gw.receive())
                done, _ = await asyncio.wait({recv, stop}, return_when=asyncio.FIRST_COMPLETED)
                if stop in done:
                    recv.cancel()
                    await gw.close()
                    return
                msg = recv.result()
                if msg.type != aiohttp.WSMsgType.TEXT:
                    return
                data = json.loads(msg.data)
                t = data.get("t")
                if t == "req":
                    asyncio.create_task(self._request(gw, data))
                elif t == "ws_open":
                    asyncio.create_task(self._ws_open(gw, data))
                elif t == "ws_msg":
                    ws = self.sockets.get(data.get("id"))
                    if ws is not None:
                        if data.get("kind") == "binary":
                            await ws.send_bytes(base64.b64decode(data.get("data") or ""))
                        else:
                            await ws.send_str(data.get("data") or "")
                elif t == "ws_close":
                    ws = self.sockets.pop(data.get("id"), None)
                    if ws is not None:
                        await ws.close()
        finally:
            stop.cancel()

    async def _request(self, gw, data: dict) -> None:
        try:
            async with self.http.request(data["method"], self.comfy + data["path"], headers=_clean(data["headers"]),
                                         data=base64.b64decode(data.get("body") or ""), allow_redirects=False) as r:
                body = await r.read()
                res = {"t": "res", "id": data["id"], "status": r.status, "headers": _clean(r.headers),
                       "body": base64.b64encode(body).decode("ascii")}
        except Exception as exc:  # noqa: BLE001 - every failure becomes a 502 for the browser
            res = {"t": "res", "id": data["id"], "status": 502, "headers": {"Content-Type": "text/plain"},
                   "body": base64.b64encode(f"ComfyUI unreachable: {type(exc).__name__}".encode()).decode()}
        if not gw.closed:
            await gw.send_str(json.dumps(res, separators=(",", ":")))

    async def _ws_open(self, gw, data: dict) -> None:
        import aiohttp
        rid = data["id"]
        url = "ws" + self.comfy[4:] + data["path"]
        try:
            async with self.http.ws_connect(url, headers=_clean(data.get("headers") or {}), max_msg_size=0) as ws:
                self.sockets[rid] = ws
                async for msg in ws:
                    if gw.closed:
                        break
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        out = {"t": "ws_msg", "id": rid, "kind": "text", "data": msg.data}
                    elif msg.type == aiohttp.WSMsgType.BINARY:
                        out = {"t": "ws_msg", "id": rid, "kind": "binary",
                               "data": base64.b64encode(msg.data).decode("ascii")}
                    else:
                        break
                    await gw.send_str(json.dumps(out, separators=(",", ":")))
        except (aiohttp.ClientError, OSError):
            pass
        finally:
            if self.sockets.pop(rid, None) is not None and not gw.closed:
                await gw.send_str(json.dumps({"t": "ws_close", "id": rid}))


def start_comfyui(cfg: dict) -> subprocess.Popen:
    cmd = [sys.executable, "main.py", "--listen", "127.0.0.1", "--port", str(cfg["port"]),
           "--output-directory", "/output", "--temp-directory", "/tmp/comfyui"] + cfg["args"]
    log("starting ComfyUI", port=cfg["port"])
    return subprocess.Popen(cmd, cwd=cfg["comfy_dir"])


def wait_ready(port: int, proc: subprocess.Popen, timeout: float = 600.0) -> bool:
    import urllib.request
    end = time.time() + timeout
    while time.time() < end:
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/system_stats", timeout=5):
                return True
        except OSError:
            time.sleep(2)
    return False


def main() -> int:  # pragma: no cover - needs a GPU container
    try:
        cfg = read_config()
    except ConfigError as exc:
        log(f"bad configuration: {exc}")
        return 2
    proc = start_comfyui(cfg)
    if not wait_ready(cfg["port"], proc):
        log("ComfyUI failed to start")
        proc.kill()
        return 6
    log("ComfyUI ready; connecting to the relay")
    agent = Agent(f"http://127.0.0.1:{cfg['port']}", cfg["gateway"], cfg["token"], heartbeat_s=cfg["heartbeat_s"])
    loop = asyncio.new_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, agent.stop)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        loop.run_until_complete(agent.run())
    finally:
        proc.terminate()
        try:
            proc.wait(30)
        except subprocess.TimeoutExpired:
            proc.kill()
    return 0 if proc.returncode in (0, -15, None) else 6


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

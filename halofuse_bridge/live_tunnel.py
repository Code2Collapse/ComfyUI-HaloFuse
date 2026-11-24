"""One-click Live ComfyUI host (owner 2026-11-24): the relay gateway on this computer + a cloudflared tunnel.

The gateway (live_gateway.py) runs inside the bridge on 127.0.0.1 on a free port, plain HTTP, and ONLY on loopback:
check_bind() refuses any other plain-HTTP bind. cloudflared gives it a public https address:

- quick tunnel (no account): ``cloudflared tunnel --no-autoupdate --url http://127.0.0.1:<port>``; the address is
  the https://*.trycloudflare.com URL cloudflared prints. It changes on every tunnel restart.
- named tunnel (optional): a tunnel token (env HALOFUSE_TUNNEL_TOKEN or the keychain) plus a fixed hostname
  (setting live_tunnel_hostname). The token travels in cloudflared's environment, never on its command line.

cloudflared is looked up on PATH, then in <settings dir>/tools. When missing, download_cloudflared() fetches the
official release asset of CLOUDFLARED_VERSION for this OS/arch and checks its SHA-256 against the release checksums
(pinned below); it only runs when the artist presses "Download cloudflared". The tunnel is restarted when it dies
and stopped with the bridge (atexit). The gateway secret is generated on first use and kept in the keychain
(credentials.CredentialStore.live_secret); it is never returned by status().
"""
from __future__ import annotations

import asyncio
import atexit
import hashlib
import ipaddress
import logging
import os
import platform
import re
import secrets
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

_log = logging.getLogger(__name__)

CLOUDFLARED_VERSION = "2026.10.7"
_RELEASE = f"https://github.com/cloudflare/cloudflared/releases/download/{CLOUDFLARED_VERSION}/"
# SHA-256 from the "SHA256 Checksums" block of the 2026.10.7 release notes (checked 2026-11-24).
ASSETS = {
    ("windows", "amd64"): ("cloudflared-windows-amd64.exe",
                           "2493dbc168923bb3e59e5e7716f71deced92992eea9acd6e9d74f84ac7751343"),
    ("linux", "amd64"): ("cloudflared-linux-amd64", "1f99fb518b83dec2ba8d870a7cc991d82a77e6196295a3d2754ba17ab9f91872"),
    ("linux", "arm64"): ("cloudflared-linux-arm64", "3df43aedc9ff291d1735b5cb576cb97e0b157488796db2e32b28aecabb445775"),
    ("darwin", "amd64"): ("cloudflared-darwin-amd64.tgz",
                          "5c55e77da7012cebca19170357e196176c48c9647cc2ead404702fa125ddbd86"),
    ("darwin", "arm64"): ("cloudflared-darwin-arm64.tgz",
                          "2136431d5eb467bf447d2ee514b713e192719b143aec1d9e09bf7dc290d5bb12"),
}
QUICK_URL_RE = re.compile(r"https://[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.trycloudflare\.com")
_NAMED_READY = ("Registered tunnel connection", "Connection registered")
_HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9-]{1,63})+$")
MAX_DOWNLOAD = 200 * 1024 * 1024


class TunnelError(Exception):
    pass


# ---- loopback rule -------------------------------------------------------------------------------------------

def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_bind(host: str, *, tls: bool) -> None:
    """Plain HTTP is allowed only on loopback (a tunnel or TLS proxy in front). Anything else needs TLS."""
    if not tls and not is_loopback(host):
        raise ValueError(f"refusing a plain-HTTP gateway on {host}: plain HTTP is allowed on 127.0.0.1 only")


# ---- locating / downloading cloudflared ----------------------------------------------------------------------

def platform_key(system: str | None = None, machine: str | None = None) -> tuple[str, str] | None:
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    key = (system, arch)
    return key if key in ASSETS else None


def exe_name() -> str:
    return "cloudflared.exe" if os.name == "nt" else "cloudflared"


def tools_dir() -> Path:
    from . import config
    return config.settings_dir() / "tools"


def locate(tools: Path | None = None, which=shutil.which) -> str | None:
    found = which("cloudflared")
    if found:
        return found
    cand = (tools or tools_dir()) / exe_name()
    return str(cand) if cand.is_file() else None


def download_cloudflared(dest: Path | None = None, *, opener=urllib.request.urlopen, key=None) -> str:
    """Fetch the pinned release asset for this OS/arch, verify its SHA-256, install it into dest. Returns the path."""
    key = key or platform_key()
    if key is None:
        raise TunnelError("There is no cloudflared download for this computer's system. Install cloudflared yourself "
                          "and put it on PATH.")
    asset, want = ASSETS[key]
    dest = Path(dest or tools_dir())
    dest.mkdir(parents=True, exist_ok=True)
    h, size = hashlib.sha256(), 0
    fd, tmp = tempfile.mkstemp(prefix=".cloudflared-", dir=str(dest))
    try:
        with os.fdopen(fd, "wb") as out, opener(_RELEASE + asset, timeout=60) as resp:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_DOWNLOAD:
                    raise TunnelError("The cloudflared download is larger than expected; it was discarded.")
                h.update(chunk)
                out.write(chunk)
        if h.hexdigest() != want:
            raise TunnelError("The cloudflared download did not match the published checksum; it was discarded.")
        final = dest / exe_name()
        if asset.endswith(".tgz"):
            with tarfile.open(tmp, "r:gz") as tar:
                member = next((m for m in tar.getmembers() if m.isfile() and Path(m.name).name == "cloudflared"), None)
                if member is None:
                    raise TunnelError("The cloudflared archive has no cloudflared program inside.")
                src = tar.extractfile(member)
                part = final.with_name(final.name + ".part")
                with open(part, "wb") as fh:
                    shutil.copyfileobj(src, fh)
                os.replace(part, final)
            os.unlink(tmp)
        else:
            os.replace(tmp, final)
        if os.name != "nt":
            final.chmod(final.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return str(final)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ---- the tunnel process --------------------------------------------------------------------------------------

class Tunnel:
    """cloudflared in front of http://127.0.0.1:<port>. state: starting | ready | error | stopped."""

    def __init__(self, exe: str, port: int, *, token: str | None = None, hostname: str | None = None,
                 popen=subprocess.Popen, restart_delay: float = 2.0, max_delay: float = 30.0,
                 ready_timeout: float = 60.0, argv_prefix: tuple = ()) -> None:
        if hostname and not _HOSTNAME_RE.match(hostname):
            raise TunnelError("live_tunnel_hostname is not a valid host name.")
        if bool(token) != bool(hostname):
            raise TunnelError("A named tunnel needs both the tunnel token and live_tunnel_hostname.")
        self.exe, self.port, self.token, self.hostname = exe, int(port), token, hostname
        self.popen, self.restart_delay, self.max_delay, self.ready_timeout = popen, restart_delay, max_delay, ready_timeout
        self.argv_prefix = tuple(argv_prefix)
        self.state, self.url, self.error, self.restarts = "stopped", None, None, 0
        self.proc = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._changed = threading.Condition(self._lock)

    def argv(self) -> list[str]:
        base = [*self.argv_prefix, self.exe, "tunnel", "--no-autoupdate"]
        if self.token:
            return [*base, "run"]
        return [*base, "--url", f"http://127.0.0.1:{self.port}"]

    def _env(self) -> dict:
        env = dict(os.environ)
        env.pop("TUNNEL_TOKEN", None)
        if self.token:
            env["TUNNEL_TOKEN"] = self.token  # never on the command line (visible in process lists)
        return env

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._set("starting")
            self._thread = threading.Thread(target=self._supervise, name="halofuse-tunnel", daemon=True)
            self._thread.start()

    def _set(self, state, url=None, error=None) -> None:  # caller holds the lock
        self.state, self.url, self.error = state, url, error
        self._changed.notify_all()

    def wait_ready(self, timeout: float) -> bool:
        end = time.monotonic() + timeout
        with self._lock:
            while self.state != "ready":
                left = end - time.monotonic()
                if left <= 0 or self.state == "stopped":
                    return self.state == "ready"
                self._changed.wait(left)
            return True

    def _supervise(self) -> None:
        delay = self.restart_delay
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                proc = self.popen(self.argv(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  stdin=subprocess.DEVNULL, env=self._env(), text=True, encoding="utf-8",
                                  errors="replace", bufsize=1,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except OSError as exc:
                with self._lock:
                    self._set("error", error=f"cloudflared could not be started ({type(exc).__name__}).")
                if self._stop.wait(delay):
                    break
                delay = min(delay * 2, self.max_delay)
                continue
            self.proc = proc
            for line in proc.stdout:
                self._line(line)
            proc.wait()
            if self._stop.is_set():
                break
            with self._lock:
                self.restarts += 1
                self._set("starting", error=f"cloudflared stopped (exit {proc.returncode}); restarting.")
            _log.warning("HaloFuse tunnel stopped (exit %s); restarting", proc.returncode)
            if time.monotonic() - started > 60:
                delay = self.restart_delay  # it ran for a while: restart quickly
            if self._stop.wait(delay):
                break
            delay = min(delay * 2, self.max_delay)
        with self._lock:
            self._set("stopped")

    def _line(self, line: str) -> None:
        with self._lock:
            if self.state == "ready":
                return
            if self.hostname:
                if any(m in line for m in _NAMED_READY):
                    self._set("ready", url=f"https://{self.hostname}")
                return
            m = QUICK_URL_RE.search(line)
            if m and m.group(0) != "https://api.trycloudflare.com":
                self._set("ready", url=m.group(0))

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        proc = self.proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
        if self._thread is not None:
            self._thread.join(timeout)
        with self._lock:
            self._set("stopped")


# ---- the local gateway ---------------------------------------------------------------------------------------

class LocalGateway:
    """live_gateway's app on its own event loop thread, bound to 127.0.0.1 on a free port (plain HTTP)."""

    def __init__(self, secret: str, host: str = "127.0.0.1", port: int = 0) -> None:
        check_bind(host, tls=False)
        self.secret, self.host, self.port = secret, host, port
        self.loop: asyncio.AbstractEventLoop | None = None
        self._runner = None
        self._thread: threading.Thread | None = None

    def start(self, timeout: float = 10.0) -> int:
        from aiohttp import web

        from .live_gateway import make_gateway_app
        ready, err = threading.Event(), []

        def run():
            loop = asyncio.new_event_loop()
            self.loop = loop
            asyncio.set_event_loop(loop)
            try:
                self._runner = web.AppRunner(make_gateway_app(self.secret, secure_cookie=True))
                loop.run_until_complete(self._runner.setup())
                site = web.TCPSite(self._runner, self.host, self.port)
                loop.run_until_complete(site.start())
                self.port = site._server.sockets[0].getsockname()[1]
            except Exception as exc:  # noqa: BLE001
                err.append(exc)
                ready.set()
                return
            ready.set()
            loop.run_forever()
            loop.run_until_complete(self._runner.cleanup())
            loop.close()

        self._thread = threading.Thread(target=run, name="halofuse-live-gateway", daemon=True)
        self._thread.start()
        if not ready.wait(timeout):
            raise TunnelError("The local gateway did not start.")
        if err:
            raise TunnelError(f"The local gateway did not start ({type(err[0]).__name__}).")
        return self.port

    def stop(self, timeout: float = 5.0) -> None:
        if self.loop is not None and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self._thread is not None:
            self._thread.join(timeout)


# ---- the host: gateway + tunnel + secret ---------------------------------------------------------------------

_TEXT = {
    "off": "Live ComfyUI is off. Turn the switch on to start it on this computer.",
    "needs_cloudflared": "Live ComfyUI needs cloudflared (a free program that gives it a secure web address). "
                         "Press Download cloudflared.",
    "downloading": "Downloading cloudflared…",
    "starting": "Starting the secure address…",
    "ready": "Live ComfyUI is ready.",
}


def tunnel_token() -> str | None:
    token = str(os.environ.get("HALOFUSE_TUNNEL_TOKEN") or "").strip()
    if token:
        return token
    try:
        from . import credentials
        return credentials.default_store().tunnel_token()
    except Exception:  # noqa: BLE001
        return None


def live_secret() -> str:
    """The gateway secret: generated on first use, kept in the keychain; this process only when there is none."""
    global _MEM_SECRET
    try:
        from . import credentials
        return credentials.default_store().live_secret(create=True)
    except Exception:  # noqa: BLE001 - no keychain: a per-process secret (sessions end when ComfyUI restarts)
        if _MEM_SECRET is None:
            _MEM_SECRET = secrets.token_urlsafe(32)
            _log.warning("no system keychain: the Live ComfyUI secret lasts until ComfyUI restarts")
        return _MEM_SECRET


_MEM_SECRET: str | None = None


class LiveHost:
    def __init__(self, *, locate_fn=locate, download_fn=download_cloudflared, tunnel_cls=Tunnel,
                 gateway_cls=LocalGateway, secret_fn=live_secret, token_fn=tunnel_token) -> None:
        self.locate_fn, self.download_fn, self.tunnel_cls, self.gateway_cls = locate_fn, download_fn, tunnel_cls, gateway_cls
        self.secret_fn, self.token_fn = secret_fn, token_fn
        self.on, self.downloading, self.error = False, False, None
        self.gateway: LocalGateway | None = None
        self.tunnel: Tunnel | None = None
        self.secret: str | None = None
        self.hostname: str | None = None
        self._lock = threading.RLock()

    def start(self, settings: dict | None = None) -> dict:
        settings = settings or {}
        with self._lock:
            self.on, self.error = True, None
            try:
                if self.secret is None:
                    self.secret = self.secret_fn()
                if self.gateway is None:
                    gw = self.gateway_cls(self.secret)
                    gw.start()
                    self.gateway = gw
                if self.tunnel is None:
                    exe = self.locate_fn()
                    if exe is None:
                        return self.status()
                    host = str(settings.get("live_tunnel_hostname") or "").strip() or None
                    token = self.token_fn() if host else None
                    if host and not token:
                        raise TunnelError("live_tunnel_hostname is set, but there is no tunnel token: save it with "
                                          "Save tunnel token, or clear live_tunnel_hostname to use a quick tunnel.")
                    self.hostname = host
                    self.tunnel = self.tunnel_cls(exe, self.gateway.port, token=token, hostname=host)
                    self.tunnel.start()
            except Exception as exc:  # noqa: BLE001
                self.error = str(exc) if isinstance(exc, TunnelError) else \
                    f"Live ComfyUI could not start on this computer ({type(exc).__name__})."
            return self.status()

    def download(self, settings: dict | None = None) -> dict:
        with self._lock:
            self.downloading, self.error = True, None
        try:
            self.download_fn()
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self.downloading = False
                self.error = str(exc) if isinstance(exc, TunnelError) else \
                    f"cloudflared could not be downloaded ({type(exc).__name__}). Check the internet connection and try again."
                return self.status()
        with self._lock:
            self.downloading = False
            return self.start(settings) if self.on else self.status()

    def stop(self) -> dict:
        with self._lock:
            self.on, self.error = False, None
            t, g, self.tunnel, self.gateway = self.tunnel, self.gateway, None, None
        if t is not None:
            t.stop()
        if g is not None:
            g.stop()
        return self.status()

    def public_url(self) -> str | None:
        t = self.tunnel
        return t.url if (self.on and t is not None and t.state == "ready") else None

    def local_url(self) -> str | None:
        g = self.gateway
        return f"http://127.0.0.1:{g.port}" if (self.on and g is not None) else None

    def state(self) -> str:
        if not self.on:
            return "off"
        if self.downloading:
            return "downloading"
        if self.error:
            return "error"
        if self.tunnel is None:
            return "needs_cloudflared"
        return {"ready": "ready", "error": "error"}.get(self.tunnel.state, "starting")

    def status(self) -> dict:
        """The panel's view of the host. Never the secret or a token."""
        st = self.state()
        msg = self.error if st == "error" and self.error else (
            self.tunnel.error if st == "error" and self.tunnel else _TEXT.get(st, ""))
        if st == "starting" and self.tunnel is not None and self.tunnel.error:
            msg = self.tunnel.error
        nxt = {"off": "turn_on", "needs_cloudflared": "download", "downloading": "wait", "starting": "wait",
               "ready": "open", "error": "retry"}[st]
        return {"state": st, "message": msg, "address": self.public_url(), "next_action": nxt,
                "named_tunnel": bool(self.hostname), "cloudflared_version": CLOUDFLARED_VERSION}


_HOST: LiveHost | None = None
_HOST_LOCK = threading.Lock()


def host() -> LiveHost:
    global _HOST
    with _HOST_LOCK:
        if _HOST is None:
            _HOST = LiveHost()
            atexit.register(_shutdown)
        return _HOST


def current() -> LiveHost | None:
    """The running host, or None when the switch is off (live_comfy then uses the settings as before)."""
    h = _HOST
    return h if h is not None and h.on else None


def set_host(h: LiveHost | None) -> None:  # tests
    global _HOST
    _HOST = h


def _shutdown() -> None:
    h = _HOST
    if h is not None:
        try:
            h.stop()
        except Exception:  # noqa: BLE001
            pass

"""HaloFuse settings / account / GPU catalogue routes (owner WS1; contract C-1 section 7, input for C-4).

The lead registers them in routes.py:

    routes_account.register(PromptServer.instance.routes, prefix="/halofuse")

Every route answers loopback peers only (unless the file-only setting `allow_lan_clients` is true), checks the Host
header against an allowlist (DNS rebinding), and requires `X-HaloFuse-Request: 1` for anything that changes state or
signs in to the cloud service. No response ever carries the endpoint, a URL, an exception text, an email or a
password (THREAT_MODEL TM-001, TM-003, TM-010).
"""
from __future__ import annotations

import asyncio
import collections
import ipaddress
import logging
import threading
import time

from aiohttp import web

from . import __version__, brand, config, credentials, provider
from .credentials import CredentialError, Credentials
from .provider.backend import BackendError, validate_instance_type

_log = logging.getLogger("halofuse_bridge.routes_account")

REQUEST_HEADER = "X-HaloFuse-Request"
_LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1"}

_TEST_LIMIT = 5
_TEST_WINDOW_S = 60.0
_test_lock = threading.Lock()
_test_times: collections.deque = collections.deque()

_sku_lock = threading.Lock()
_known_skus: set[str] = set()

_TEST_CODES = {"ok", "auth_failed", "unreachable", "service_not_configured", "credentials_missing",
               "connector_missing", "rate_limited"}
_TEST_CODE_MAP = {"not_configured": "service_not_configured", "forbidden": "auth_failed",
                  "service_unavailable": "unreachable"}


def reset_rate_limit() -> None:
    """Clear the test-connection limiter (tests)."""
    with _test_lock:
        _test_times.clear()


async def _run(fn):
    return await asyncio.get_running_loop().run_in_executor(None, fn)


def _error(code: str, *, status: int | None = None, detail: str = "") -> web.Response:
    envelope = brand.error_envelope(code)
    _log.warning("request refused [%s] id=%s%s", code, envelope["error"]["id"], f" {detail}" if detail else "")
    return web.json_response(envelope, status=status or brand.status_for(code))


def _from_exception(exc: Exception) -> web.Response:
    if isinstance(exc, (BackendError, CredentialError)):
        return _error(exc.code, status=exc.status)
    _log.warning("unexpected %s in an account route", type(exc).__name__)
    return _error("unknown")


# ---- request guard ----------------------------------------------------------------------------

def _is_loopback(peer) -> bool:
    if not peer:
        return False
    try:
        ip = ipaddress.ip_address(peer.split("%", 1)[0])
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    return ip.is_loopback or bool(mapped and mapped.is_loopback)


def _host_name(host_header: str | None) -> str:
    host = (host_header or "").strip().lower()
    if host.startswith("["):
        return host[1:host.find("]")] if "]" in host else host[1:]
    if host.count(":") == 1:
        host = host.split(":", 1)[0]
    return host


def request_guard(request: web.Request, *, needs_header: bool) -> web.Response | None:
    """THE HaloFuse request policy (account, job and delivery routes): loopback TCP peer unless the owner set
    allow_lan_clients, Host in the allowlist (DNS rebinding), and X-HaloFuse-Request: 1 when *needs_header*.
    Returns the refusal response, or None when the request may proceed."""
    settings = config.load_settings()
    allow_lan = settings.get("allow_lan_clients") is True
    allowed_hosts = set(_LOOPBACK_NAMES)
    if allow_lan:
        names = settings.get("lan_hostnames") or []
        if isinstance(names, list):
            allowed_hosts |= {str(n).strip().lower() for n in names if isinstance(n, str) and n.strip()}
    # request.remote is the TCP peer; X-Forwarded-For is deliberately ignored.
    if not allow_lan and not _is_loopback(request.remote):
        return _error("forbidden_origin")
    if _host_name(request.headers.get("Host")) not in allowed_hosts:
        return _error("forbidden_origin")
    if needs_header and request.headers.get(REQUEST_HEADER) != "1":
        return _error("missing_request_header")
    return None


_guard = request_guard  # R-WS3-02: kept for older imports


async def _json_body(request: web.Request) -> dict:
    if not request.can_read_body:
        return {}
    text = await request.text()
    if not text.strip():
        return {}
    import json

    data = json.loads(text)  # ValueError -> invalid_input (caller)
    if not isinstance(data, dict):
        raise ValueError("body is not an object")
    return data


# ---- shared shapes -------------------------------------------------------------------------------

def _settings_payload() -> dict:
    store = credentials.default_store()
    prefs = config.load_settings()
    out = {k: prefs.get(k, config.DEFAULTS.get(k)) for k in config.PUBLIC_KEYS}
    out["version"] = __version__
    out.update(store.status())
    out["service_configured"] = provider.resolve_endpoint() is not None
    out["connector_installed"] = provider.connector_installed()
    out["settings_file_ok"] = config.settings_file_ok()
    return out


def _save_credentials_from(body: dict) -> None:
    """Route email/password found in a settings body through the credential rules (blank = keep)."""
    def meaningful(value) -> bool:
        return value is not None and (not isinstance(value, str) or value.strip() != "")

    if meaningful(body.get("email")) or meaningful(body.get("password")):
        credentials.default_store().save(email=body.get("email"), password=body.get("password"),
                                         store_unencrypted=body.get("store_unencrypted"))


# ---- handlers ------------------------------------------------------------------------------------

async def _get_settings(request):
    return web.json_response(await _run(_settings_payload))


async def _post_settings(request):
    try:
        body = await _json_body(request)
    except ValueError:
        return _error("invalid_input")

    prefs = {k: v for k, v in body.items() if k not in ("email", "password", "store_unencrypted")}
    rejected = config.invalid_prefs(prefs)
    if rejected:
        return _error("invalid_request", detail="rejected keys: " + ",".join(sorted(rejected)))

    def work():
        config.save_settings(prefs)
        _save_credentials_from(body)
        return _settings_payload()

    try:
        return web.json_response(await _run(work))
    except (BackendError, CredentialError) as exc:
        return _from_exception(exc)


async def _get_account(request):
    return web.json_response(await _run(lambda: credentials.default_store().status()))


async def _post_credentials(request):
    try:
        body = await _json_body(request)
    except ValueError:
        return _error("invalid_input")
    if any(k in body for k in ("host", "endpoint")):
        _log.warning("ignored a request to change the service endpoint (it is deployment configuration)")

    def work():
        return credentials.default_store().save(email=body.get("email"), password=body.get("password"),
                                                store_unencrypted=body.get("store_unencrypted"))

    try:
        return web.json_response(await _run(work))
    except (CredentialError, BackendError) as exc:
        return _from_exception(exc)


async def _delete_credentials(request):
    try:
        return web.json_response(await _run(lambda: credentials.default_store().clear()))
    except (CredentialError, BackendError) as exc:
        return _from_exception(exc)


def _rate_limited() -> bool:
    now = time.monotonic()
    with _test_lock:
        while _test_times and now - _test_times[0] > _TEST_WINDOW_S:
            _test_times.popleft()
        if len(_test_times) >= _TEST_LIMIT:
            return True
        _test_times.append(now)
        return False


def _test_result(code: str) -> dict:
    return {"ok": code == "ok", "code": code, "message": brand.message(code)}


async def _post_test(request):
    try:
        body = await _json_body(request)
    except ValueError:
        return _error("invalid_input")
    email, password = body.get("email"), body.get("password")
    given = [isinstance(v, str) and v.strip() != "" for v in (email, password)]
    if any(given) and not all(given):
        return _error("credentials_incomplete")
    explicit = Credentials(email.strip(), password) if all(given) else None
    if _rate_limited():
        return web.json_response(_test_result("rate_limited"))

    def work():
        with provider.open_backend(credentials=explicit) as be:
            be.login_check()

    try:
        await _run(work)
        code = "ok"
    except BackendError as exc:
        code = _TEST_CODE_MAP.get(exc.code, exc.code)
        if code not in _TEST_CODES:
            code = "unknown"
    except Exception as exc:  # noqa: BLE001 - never surface exception text
        _log.warning("test connection failed unexpectedly (%s)", type(exc).__name__)
        code = "unknown"
    return web.json_response(_test_result(code))


def _fetch_skus(be) -> list:
    skus = be.list_skus()
    with _sku_lock:
        _known_skus.clear()
        _known_skus.update(s.instance_type for s in skus)
    return skus


async def _get_skus(request):
    def work():
        with provider.open_backend() as be:
            return _fetch_skus(be)

    try:
        skus = await _run(work)
    except (BackendError, CredentialError) as exc:
        return _from_exception(exc)
    return web.json_response({"skus": [
        {"instanceType": s.instance_type, "gpuType": s.gpu_type, "gpuMemoryGb": s.gpu_memory_gb,
         "coreCount": s.core_count, "memoryGb": s.memory_gb} for s in skus]})


async def _get_estimate(request):
    name = request.query.get("instance_type", "")
    try:
        validate_instance_type(name)
    except BackendError as exc:
        return _from_exception(exc)

    def work():
        with provider.open_backend() as be:
            with _sku_lock:
                known = name in _known_skus
            if not known:
                known = any(s.instance_type == name for s in _fetch_skus(be))
            if not known:
                raise BackendError("invalid_request", detail="unknown instance type")
            return be.estimate(name).rate_per_hour_usd

    try:
        rate = await _run(work)
    except (BackendError, CredentialError) as exc:
        return _from_exception(exc)
    return web.json_response({"instanceType": name, "ratePerHourUsd": rate})


# ---- registration ------------------------------------------------------------------------------

_ROUTES = (
    # method, path, handler, X-HaloFuse-Request header required
    ("GET", "/settings", _get_settings, False),
    ("POST", "/settings", _post_settings, True),
    ("GET", "/account", _get_account, False),
    ("POST", "/account/credentials", _post_credentials, True),
    ("DELETE", "/account/credentials", _delete_credentials, True),
    ("POST", "/account/test", _post_test, True),
    ("GET", "/skus", _get_skus, True),
    ("GET", "/estimate", _get_estimate, True),
)


def _guarded(handler, needs_header: bool):
    async def wrapper(request):
        response = _guard(request, needs_header=needs_header)
        if response is None:
            response = await handler(request)
        return response

    wrapper.__name__ = handler.__name__
    return wrapper


def register(route_table, prefix: str = "/halofuse") -> None:
    """Add the WS1 routes under *prefix* to an aiohttp RouteTableDef (e.g. PromptServer.instance.routes)."""
    prefix = "/" + prefix.strip("/")
    for method, path, handler, needs_header in _ROUTES:
        route_table.route(method, prefix + path)(_guarded(handler, needs_header))

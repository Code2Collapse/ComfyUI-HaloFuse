"""Live ComfyUI sessions (L9.345): one interactive cloud ComfyUI per user, reached through the relay gateway.

OFF by default. It needs (1) settings live_comfy_enabled = true, (2) live_gateway_url = an https:// relay gateway the
owner hosts (halofuse_bridge/live_gateway.py) and (3) the shared secret in env HALOFUSE_LIVE_SECRET (or the setting
live_gateway_secret). The secret is never returned by any view or route.

Each session is its own cloud job (its own container, so sessions of different users never share a ComfyUI). The
Render path is untouched: a live session never runs on the warm machine, so renders never queue behind it. The idle
auto-shutdown reuses the warm machine's idle duration and warning window (warm_machine.idle_timeout_seconds,
warm_machine._warn_window); "activity" is the relay traffic the gateway reports.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request

from . import cost as costmod
from . import live_proto as proto
from . import warm_machine
from .job_store import HaloFuseError, JobStore, default_store
from .provider.backend import SubmitSpec

_log = logging.getLogger(__name__)

KIND = "live"
RENDER_PORT = 8188
PORT_RANGE = (8190, 8999)
DEFAULT_MAX_RUNTIME_S = 4 * 3600
AGENT_TOKEN_SLACK_S = 600
VIEWER_TTL_S = 120
BRIDGE_TTL_S = 60
ACTIVE = ("starting", "running", "stopping")
LIVE_COMMAND = ("python3.13", "/runner/halofuse_live.py")
_LOCK = threading.RLock()
_clock = time.time
_KEEPER: "_Keeper | None" = None


# ---- configuration ---------------------------------------------------------------------------------------------

def _settings(settings: dict | None) -> dict:
    if settings is not None:
        return settings
    from .jobs import _load_settings  # lazy, as warm_machine does
    return _load_settings()


def _store(store: JobStore | None) -> JobStore:
    return store or default_store()


def _secret(settings: dict) -> str:
    return os.environ.get("HALOFUSE_LIVE_SECRET") or str(settings.get("live_gateway_secret") or "")


def gateway_url(settings: dict) -> str:
    return str(settings.get("live_gateway_url") or "").strip().rstrip("/")


def availability(settings: dict) -> str | None:
    """None when Live ComfyUI can start, else the reason in words (naming the setting to change)."""
    if not settings.get("live_comfy_enabled"):
        # owner 2026-11-24: the old text named a setting that has no switch in the panel
        return ("Live ComfyUI is not available yet: it needs a HaloFuse relay gateway, which is not set up. "
                "Your administrator turns it on once the gateway exists; there is no switch for it in this panel.")
    url = gateway_url(settings)
    if not url:
        return "Live ComfyUI needs a relay gateway: set live_gateway_url to its https:// address."
    if not url.startswith("https://"):
        return "live_gateway_url must start with https:// (an unencrypted gateway is refused)."
    try:
        proto._key(_secret(settings))
    except proto.TokenError:
        return "Live ComfyUI needs the gateway secret: set HALOFUSE_LIVE_SECRET (16 characters or more)."
    return None


def _require(settings: dict) -> None:
    reason = availability(settings)
    if reason:
        raise HaloFuseError("live_disabled", reason, 409)


def max_runtime_s(settings: dict) -> int:
    try:
        value = int(settings.get("live_max_runtime_seconds") or DEFAULT_MAX_RUNTIME_S)
    except (TypeError, ValueError):
        value = DEFAULT_MAX_RUNTIME_S
    return max(600, min(value, 86400))


def _user(user) -> str:
    text = str(user or "default").strip()[:64]
    return "".join(c for c in text if c.isalnum() or c in "-_.@") or "default"


# ---- records ---------------------------------------------------------------------------------------------------

def _active_records(store: JobStore) -> list[dict]:
    out = []
    for s in store.list(kind=KIND, active_only=True, limit=None):
        rec = store.get(s["job_id"])
        if rec and (rec.get("live") or {}).get("state") in ACTIVE:
            out.append(rec)
    return out


def _record_of(store: JobStore, user: str) -> dict | None:
    for rec in _active_records(store):
        if rec["live"]["user"] == user:
            return rec
    return None


def allocate_port(store: JobStore) -> int:
    used = {rec["live"].get("port") for rec in _active_records(store)}
    for port in range(PORT_RANGE[0], PORT_RANGE[1] + 1):
        if port != RENDER_PORT and port not in used:
            return port
    raise HaloFuseError("live_busy", "Too many Live ComfyUI sessions are open. Stop one first.", 409)


def _end(store: JobStore, rid: str, reason: str, message: str) -> dict:
    now = _clock()

    def fn(r):
        r["live"].update(state="ended", ended_at=now, end_reason=reason, end_message=message)
        r.update(state="ended", terminal=True, ended_at=now, outcome="ended", status_note=message)
    return store.mutate(rid, fn)


# ---- actions ---------------------------------------------------------------------------------------------------

def start(user, instance_type: str | None = None, *, store: JobStore | None = None, settings: dict | None = None,
          backend=None) -> dict:
    """Provision a live ComfyUI for this user, or return the one already running (reuse). Returns view()."""
    store, settings, user = _store(store), _settings(settings), _user(user)
    _require(settings)
    sku = instance_type or settings.get("instance_type")
    if not sku:
        raise HaloFuseError("bad_request", "Choose a GPU first.", 400)
    with _LOCK:
        cur = _record_of(store, user)
        if cur is not None:
            return view(user, store=store, settings=settings)
        costmod.check_daily_cap(store, settings, _clock())
        port = allocate_port(store)
        now = _clock()
        limit = max_runtime_s(settings)
        rec = store.create(kind=KIND, label="Live ComfyUI", fields={
            "cloud": {"instance_type": sku}, "submitted_at": now,
            "live": {"user": user, "state": "starting", "port": port, "instance_type": sku, "job_id": None,
                     "requested_at": now, "ready_at": None, "ended_at": None, "end_reason": None,
                     "end_message": None, "max_runtime_s": limit, "last_activity_at": now,
                     "idle_warned": False, "gpu_name": None},
        })
        rid = rec["job_id"]
        own = backend is None
        try:
            if own:
                backend = warm_machine._open(settings)
            try:
                rate = getattr(backend.estimate(sku), "rate_per_hour_usd", None)
            except Exception:  # noqa: BLE001 - an unpriced GPU never blocks the session
                rate = None
            store.mutate(rid, lambda r: r["cost"].__setitem__("rate_per_hour_usd", rate))
            token = proto.mint(_secret(settings), rid, "agent", user, limit + AGENT_TOKEN_SLACK_S, now=now)
            env = {"LIVE_PORT": str(port), "GATEWAY_URL": _ws_url(gateway_url(settings)), "AGENT_TOKEN": token}
            from .config import container_inactivity_seconds
            spec = SubmitSpec(image=settings.get("image") or _default_image(), command=LIVE_COMMAND,
                              instance_type=sku, env=env, max_wall_clock_s=limit,
                              container_inactivity_s=container_inactivity_seconds(settings),
                              tags=("hf-live",))
            ref = backend.submit(spec)
        except HaloFuseError as exc:
            _end(store, rid, "failed", exc.message)
            raise
        except Exception as exc:  # noqa: BLE001
            err = warm_machine._err(exc)
            _end(store, rid, "failed", f"Live ComfyUI could not be started: {err.message}")
            raise err from None
        finally:
            if own and backend is not None:
                warm_machine._close(backend)
        store.mutate(rid, lambda r: (r["live"].__setitem__("job_id", ref.job_id),
                                     r["cloud"]["job_ids"].append(ref.job_id)))
        _ensure_keeper(store)
        return view(user, store=store, settings=settings)


def _default_image() -> str:
    from .provider import image_contract
    return image_contract.IMAGE


def _ws_url(https_url: str) -> str:
    return "wss://" + https_url[len("https://"):] + "/agent"


def open_url(user, *, store: JobStore | None = None, settings: dict | None = None) -> str:
    """A one-shot viewer link (2 minutes) for this user's running session. The gateway trades it for a cookie."""
    store, settings, user = _store(store), _settings(settings), _user(user)
    _require(settings)
    rec = _record_of(store, user)
    if rec is None or rec["live"]["state"] != "running":
        raise HaloFuseError("live_not_ready", "Live ComfyUI is not running yet. Start it and wait until it is ready.",
                            409)
    token = proto.mint(_secret(settings), rec["job_id"], "viewer", user, VIEWER_TTL_S, now=_clock())
    store.mutate(rec["job_id"], lambda r: r["live"].update(last_activity_at=_clock(), idle_warned=False))
    return f"{gateway_url(settings)}/live/open?t={urllib.parse.quote(token)}"


def stop(user, *, store: JobStore | None = None, settings: dict | None = None, backend=None,
         reason: str = "user") -> dict:
    store, settings, user = _store(store), _settings(settings), _user(user)
    rec = _record_of(store, user)
    if rec is not None:
        _stop_record(store, rec, settings, backend, reason)
    return view(user, store=store, settings=settings)


_REASONS = {"user": "You pressed Stop.", "idle": "Stopped after the idle duration with no use.",
            "expired": "The maximum runtime was reached.", "lost": "The cloud job ended."}


def _stop_record(store: JobStore, rec: dict, settings: dict, backend, reason: str) -> None:
    rid, job_id = rec["job_id"], rec["live"].get("job_id")
    store.mutate(rid, lambda r: r["live"].__setitem__("state", "stopping"))
    own = backend is None
    try:
        if job_id:
            if own:
                backend = warm_machine._open(settings)
            backend.cancel(job_id)
    except Exception:  # noqa: BLE001 - the keeper retries; the record says stopping
        _log.warning("HaloFuse could not stop a Live ComfyUI job yet; retrying")
        return
    finally:
        if own and backend is not None:
            warm_machine._close(backend)
    _end(store, rid, reason, _REASONS.get(reason, reason))


# ---- keeper: readiness, idle shutdown, cleanup, recovery ---------------------------------------------------------

def fetch_status(settings: dict, rec: dict) -> dict | None:
    """The gateway's view of one session (agent connected, last activity). None when unreachable."""
    url = f"{gateway_url(settings)}/status/{urllib.parse.quote(rec['job_id'])}"
    token = proto.mint(_secret(settings), rec["job_id"], "bridge", rec["live"]["user"], BRIDGE_TTL_S, now=_clock())
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - https enforced by availability()
            return json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError):
        return None


def tick(*, store: JobStore | None = None, settings: dict | None = None, backend=None, status_fn=None) -> None:
    """One keeper pass over every active session."""
    store, settings = _store(store), _settings(settings)
    status_fn = status_fn or fetch_status
    now = _clock()
    for rec in _active_records(store):
        live, rid = rec["live"], rec["job_id"]
        if live["state"] == "stopping":
            _stop_record(store, rec, settings, backend, live.get("end_reason") or "user")
            continue
        job = _job(settings, backend, live.get("job_id"))
        if job is not None and job.terminal:
            _end(store, rid, "lost", _REASONS["lost"])
            continue
        if job is not None and job.gpu_name and not live.get("gpu_name"):
            store.mutate(rid, lambda r, g=job.gpu_name: r["live"].__setitem__("gpu_name", g))
        if now - float(live["requested_at"]) >= float(live["max_runtime_s"]):
            _stop_record(store, rec, settings, backend, "expired")
            continue
        st = status_fn(settings, rec)
        if st is not None:
            if st.get("agent_connected") and live["state"] == "starting":
                store.mutate(rid, lambda r: r["live"].update(state="running", ready_at=now, last_activity_at=now))
                rec = store.get(rid)
            last = max(float(st.get("last_activity") or 0), float(rec["live"]["last_activity_at"] or 0))
            if st.get("viewers"):
                last = now  # an open ComfyUI tab keeps its WebSocket: the session is in use
            if last != rec["live"]["last_activity_at"]:
                store.mutate(rid, lambda r, v=last: r["live"].update(last_activity_at=v, idle_warned=False))
        rec = store.get(rid)
        if rec["live"]["state"] == "running" and _idle_expired(store, rec, now, settings):
            _stop_record(store, rec, settings, backend, "idle")
    _update_costs(store, now)


def _job(settings, backend, job_id):
    if not job_id:
        return None
    own = backend is None
    try:
        if own:
            backend = warm_machine._open(settings)
        return backend.get(job_id)
    except Exception:  # noqa: BLE001 - unknown state: keep the session, try again next pass
        return None
    finally:
        if own and backend is not None:
            warm_machine._close(backend)


def _idle_expired(store: JobStore, rec: dict, now: float, settings: dict) -> bool:
    timeout = warm_machine.idle_timeout_seconds(settings)
    remaining = timeout - (now - float(rec["live"]["last_activity_at"]))
    if remaining <= 0:
        return True
    if remaining <= warm_machine._warn_window(timeout) and not rec["live"].get("idle_warned"):
        store.mutate(rec["job_id"], lambda r: r["live"].__setitem__("idle_warned", True))
    return False


def _update_costs(store: JobStore, now: float) -> None:
    for rec in _active_records(store):
        rate = rec["cost"].get("rate_per_hour_usd")
        secs = now - float(rec["live"]["requested_at"])
        est = round(float(rate) * secs / 3600.0, 4) if rate not in (None, "") else None
        store.mutate(rec["job_id"], lambda r, s=secs, e=est: r["cost"].update(billable_seconds=s, estimate_usd=e))


def recover(*, store: JobStore | None = None, settings: dict | None = None, backend=None) -> int:
    """At startup: sessions the store still lists as active are re-checked; ended cloud jobs are closed."""
    store, settings = _store(store), _settings(settings)
    n = 0
    for rec in _active_records(store):
        job = _job(settings, backend, rec["live"].get("job_id"))
        if job is not None and job.terminal:
            _end(store, rec["job_id"], "lost", _REASONS["lost"])
        else:
            n += 1
    if n:
        _ensure_keeper(store)
    return n


class _Keeper:
    def __init__(self, store: JobStore, every_s: float = 30.0):
        self.store, self.every_s = store, every_s
        self.stop_evt = threading.Event()
        self.thread = threading.Thread(target=self.run, name="halofuse-live-keeper", daemon=True)

    def run(self) -> None:
        while not self.stop_evt.wait(self.every_s):
            try:
                tick(store=self.store)
                if not _active_records(self.store):
                    return
            except Exception:  # noqa: BLE001 - the keeper never dies on one bad pass
                _log.warning("HaloFuse Live ComfyUI keeper pass failed", exc_info=False)


def _ensure_keeper(store: JobStore) -> None:
    global _KEEPER
    with _LOCK:
        if _KEEPER is None or not _KEEPER.thread.is_alive():
            _KEEPER = _Keeper(store)
            _KEEPER.thread.start()


def stop_keeper(timeout: float = 5.0) -> None:
    global _KEEPER
    k, _KEEPER = _KEEPER, None
    if k is not None:
        k.stop_evt.set()
        k.thread.join(timeout)


# ---- view ------------------------------------------------------------------------------------------------------

def view(user, *, store: JobStore | None = None, settings: dict | None = None) -> dict:
    """The panel's view: no token, secret, URL, handle or port."""
    store, settings, user = _store(store), _settings(settings), _user(user)
    out = {"available": availability(settings) is None, "reason": availability(settings), "state": "off"}
    rec = _record_of(store, user)
    if rec is None:
        return out
    live, now = rec["live"], _clock()
    timeout = warm_machine.idle_timeout_seconds(settings)
    out.update(state=live["state"], instance_type=live["instance_type"], gpu_name=live.get("gpu_name"),
               runtime_s=round(now - float(live["requested_at"]), 1),
               cost_usd=rec["cost"].get("estimate_usd"), rate_per_hour_usd=rec["cost"].get("rate_per_hour_usd"),
               idle_timeout_s=timeout,
               idle_remaining_s=max(0.0, round(timeout - (now - float(live["last_activity_at"])), 1)),
               idle_warning=bool(live.get("idle_warned")), max_runtime_s=live["max_runtime_s"])
    return out

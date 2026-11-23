"""The warm cloud machine (owner 2026-11-23): one GPU machine kept up between renders until the artist shuts it down.

What the cloud port allows (C-1, read from the provider SDK; nothing else is assumed):

* A *session* (``prepare_session``) is a GPU machine held for this account. Jobs submitted with its handle run on that
  same machine with no provisioning and no image pull, and the machine's model-library cache stays warm.
* Every job is still its own container: there is no call that hands new work to a running container, so ComfyUI
  starts again for every render (models load again, from the machine's cache). Inside ONE job (a queue batch, up to
  ``queue_chunk_size`` workflows) ComfyUI stays up and models stay loaded between the workflows.
* The session's hold (``hold_s``) is an idle ceiling the cloud re-arms after each job; ``release_session`` ends it.

So "keep the machine warm" here means: a held session that HaloFuse never releases by itself. While ComfyUI runs, a
keeper thread re-arms the hold with a one-second keep-alive job whenever the machine has been idle for half the hold;
it stops only when the artist presses "Shut down the cloud machine", when the daily spending limit is reached, or when
the cloud ends it (then the panel says so). If ComfyUI is closed, nothing re-arms the hold and the cloud ends the
machine about ``hold_s`` after its last job: a billing backstop, shown in the panel.

The machine is a job-store record of kind ``warm`` (its own log, survives a restart, counted in the daily spend).
Cost is an estimate: the hourly rate times the time since HaloFuse asked for the machine (renders on it included;
the record itself carries only the part not already counted by those renders, so the daily total is not doubled).
A state the bridge cannot confirm is ``unknown`` and the panel shows "a cloud machine may still be running".
No provider name, URL or credential appears here or in anything it writes.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from . import cost as costmod
from .job_store import HaloFuseError, JobStore, default_store
from .provider.backend import SubmitSpec

_log = logging.getLogger(__name__)

KIND = "warm"
DEFAULT_HOLD_S = 1800          # the cloud ends the machine this long after its last job when nothing re-arms it
FALLBACK_HOLD_S = 600          # the hold the render queue has always used (accepted by the cloud)
MIN_HOLD_S, MAX_HOLD_S = 300, 86400
KEEPALIVE_TAG = "hf-keepalive"
ACTIVE_STATES = ("starting", "running", "stopping", "unknown")
STALE_WARNING = ("A HaloFuse cloud machine may still be running and billing, but HaloFuse cannot confirm it right "
                 "now. Check it, or shut it down.")


@dataclass
class WarmPolicy:
    poll_s: float = 5.0
    check_every_s: float = 30.0
    ready_timeout_s: float = 900.0
    keepalive_fraction: float = 0.5
    keepalive_max_wall_s: int = 300
    cost_note_every_s: float = 3600.0
    shutdown_wait_s: float = 30.0


_POLICY = WarmPolicy()
_LOCK = threading.RLock()
_KEEPER: "_Keeper | None" = None
_clock = time.time


def set_policy(policy: WarmPolicy | None) -> None:
    """Tests only."""
    global _POLICY
    _POLICY = policy or WarmPolicy()


def _store(store: JobStore | None) -> JobStore:
    return store or default_store()


def _settings(settings: dict | None) -> dict:
    if settings is not None:
        return settings
    from .jobs import _load_settings  # lazy: jobs imports this module
    return _load_settings()


def hold_seconds(settings: dict | None) -> int:
    try:
        value = int((settings or {}).get("warm_hold_seconds") or DEFAULT_HOLD_S)
    except (TypeError, ValueError):
        value = DEFAULT_HOLD_S
    return max(MIN_HOLD_S, min(value, MAX_HOLD_S))


def keep_warm(settings: dict | None) -> bool:
    return (settings or {}).get("keep_warm") is True


# ---- record helpers ----------------------------------------------------------------------------------------------

def current(store: JobStore | None = None) -> dict | None:
    """The warm machine record that is not over (at most one), else None."""
    store = _store(store)
    for s in store.list(kind=KIND, active_only=True, limit=None):
        rec = store.get(s["job_id"])
        if rec is not None and not rec.get("terminal"):
            return rec
    return None


def _latest(store: JobStore) -> dict | None:
    rows = store.list(kind=KIND, limit=None)
    rows = sorted(rows, key=lambda s: s.get("created_at") or 0, reverse=True)
    return store.get(rows[0]["job_id"]) if rows else None


def _note(store: JobStore, rid: str, text: str, level: str = "info", code: str | None = None,
          console: bool = False) -> None:
    store.append_event(rid, "note", text=text, level=level, data={"code": code} if code else None)
    if console:
        try:
            from . import activity
            activity.record(f"Warm machine: {text}", level=level, kind="note",
                            data={"code": code, "job_id": rid} if code else {"job_id": rid})
        except Exception:  # noqa: BLE001 - the console record is advisory
            pass


def _set_state(store: JobStore, rid: str, state: str, **fields) -> dict:
    def fn(r):
        r["warm"]["state"] = state
        r["warm"].update(fields)
        r["state"] = "running" if state in ACTIVE_STATES else r.get("state")
    return store.mutate(rid, fn)


def _end(store: JobStore, rid: str, outcome: str, reason: str, message: str, level: str = "info") -> dict:
    now = _clock()

    def fn(r):
        r["warm"].update(state="ended", ended_at=now, end_reason=reason, end_message=message, handle=None,
                         check_error=None)
        r["state"] = outcome
        r["outcome"] = outcome
        r["terminal"] = True
        r["ended_at"] = now
        _update_cost(r, now)
    rec = store.mutate(rid, fn)
    _note(store, rid, message, level, reason, console=True)
    return rec


def _job_seconds(store: JobStore, job_ids) -> float:
    total = 0.0
    for jid in job_ids or ():
        rec = store.get(jid) or {}
        secs = (rec.get("cost") or {}).get("billable_seconds")
        if isinstance(secs, (int, float)):
            total += float(secs)
    return total


def _update_cost(rec: dict, now: float, job_seconds: float | None = None) -> None:
    w = rec["warm"]
    end = w.get("ended_at") or now
    elapsed = max(0.0, float(end) - float(w.get("requested_at") or end))
    w["elapsed_s"] = round(elapsed, 1)
    if job_seconds is not None:
        w["job_seconds"] = round(job_seconds, 1)
    own = max(0.0, elapsed - float(w.get("job_seconds") or 0.0))
    rec["cost"]["billable_seconds"] = round(own, 1)
    rec["cost"]["estimate_usd"] = costmod.estimate_usd(rec["cost"].get("rate_per_hour_usd"), own)


# ---- backend ------------------------------------------------------------------------------------------------------

def _open(settings: dict):
    from . import lifecycle
    return lifecycle.open_backend(settings)


def _close(backend) -> None:
    from . import lifecycle
    lifecycle.close_backend(backend)


def _err(exc: BaseException) -> HaloFuseError:
    from . import lifecycle
    return lifecycle.as_halofuse_error(exc)


def _session_over(session) -> bool:
    return bool(getattr(session, "terminal", False)) or getattr(session, "status", None) in (
        "released", "expired", "failed")


def _session_ready(session) -> bool:
    return bool(getattr(session, "ready", False)) or getattr(session, "status", None) in ("ready", "running")


# ---- public API ---------------------------------------------------------------------------------------------------

def start(instance_type: str | None = None, *, store: JobStore | None = None, settings: dict | None = None,
          backend=None) -> dict:
    """Ask the cloud for a warm machine (returns at once; the keeper waits for it to be ready). Returns view()."""
    store = _store(store)
    settings = _settings(settings)
    sku = instance_type or settings.get("instance_type")
    if not sku:
        raise HaloFuseError("bad_request", "Choose a GPU first.", 400)
    with _LOCK:
        cur = current(store)
        if cur is not None:
            w = cur["warm"]
            if w.get("state") == "unknown":
                raise HaloFuseError("warm_state_unknown", STALE_WARNING, 409)
            if w.get("instance_type") != sku:
                raise HaloFuseError("warm_busy", f"A warm {w.get('instance_type')} machine is already running. Shut "
                                                 f"it down before starting a {sku} machine.", 409)
            _ensure_keeper(store)
            return view(store, settings)
        costmod.check_daily_cap(store, settings, _clock())
        hold = hold_seconds(settings)
        now = _clock()
        rec = store.create(kind=KIND, label="Warm machine", fields={
            "cloud": {"instance_type": sku, "instance_handle_set": True},
            "submitted_at": now,
            "warm": {"state": "starting", "handle": None, "instance_type": sku, "hold_s": hold, "requested_at": now,
                     "ready_at": None, "ended_at": None, "end_reason": None, "end_message": None,
                     "last_activity_at": now, "last_check_at": None, "check_error": None, "jobs": [],
                     "keepalive": {"count": 0, "failures": 0, "job_id": None, "last_at": None},
                     "last_cost_note_at": now, "elapsed_s": 0.0, "job_seconds": 0.0},
        })
        rid = rec["job_id"]
        _set_state(store, rid, "starting")
        own = backend is None
        try:
            if own:
                backend = _open(settings)
            try:
                rate = getattr(backend.estimate(sku), "rate_per_hour_usd", None)
            except Exception:  # noqa: BLE001 - an unpriced GPU must not block the machine; the cost stays empty
                rate = None
            store.mutate(rid, lambda r: r["cost"].__setitem__("rate_per_hour_usd", rate))
            if rate is None:
                _note(store, rid, "No price is available for this GPU type; the cost estimate stays empty.",
                      "warning", "rate_unavailable")
            session = _prepare(backend, sku, hold, store, rid)
        except Exception as exc:  # noqa: BLE001
            err = _err(exc)
            _end(store, rid, "failed", err.code, f"The warm machine could not be started: {err.message}", "error")
            raise err from None
        finally:
            if own and backend is not None:
                _close(backend)
        handle = getattr(session, "handle", None)
        store.mutate(rid, lambda r: r["warm"].update(handle=handle, hold_s=getattr(session, "hold_s", None) or
                                                      r["warm"]["hold_s"]))
        _note(store, rid, f"Asked the HaloFuse cloud for a warm {sku} machine. It stays on until you shut it down "
                          f"in the panel; while it runs it is billed, also when idle.", "info", "warm_started",
              console=True)
        _ensure_keeper(store)
        return view(store, settings)


def _prepare(backend, sku: str, hold: int, store: JobStore, rid: str):
    try:
        return backend.prepare_session(sku, hold)
    except Exception as exc:  # noqa: BLE001
        err = _err(exc)
        if err.code != "invalid_request" or hold == FALLBACK_HOLD_S:
            raise
    # The cloud refused this hold length (its maximum is not documented): fall back to the length it accepts.
    _note(store, rid, f"The cloud did not accept a {hold} s idle hold; using {FALLBACK_HOLD_S} s.", "warning",
          "hold_reduced")
    store.mutate(rid, lambda r: r["warm"].__setitem__("hold_s", FALLBACK_HOLD_S))
    return backend.prepare_session(sku, FALLBACK_HOLD_S)


def shutdown(*, store: JobStore | None = None, settings: dict | None = None, reason: str = "user",
             backend=None) -> dict:
    """Stop renders still running on the machine, release it, and confirm it is gone. Returns view()."""
    store = _store(store)
    settings = _settings(settings)
    with _LOCK:
        rec = current(store)
        if rec is None:
            return view(store, settings)
        rid = rec["job_id"]
        w = rec["warm"]
        handle = w.get("handle")
        prev = w.get("state")
        _set_state(store, rid, "stopping")
        why = {"user": "You pressed Shut down.",
               "daily_cap": "The daily spending limit was reached.",
               "start_timeout": "The machine did not become ready in time."}.get(reason, reason)
        _note(store, rid, f"Shutting down the warm machine. {why}", "warning" if reason != "user" else "info",
              "warm_shutdown", console=True)
        _stop_jobs(store, rec)
        own = backend is None
        try:
            if own:
                backend = _open(settings)
            ka = (w.get("keepalive") or {}).get("job_id")
            if ka:
                try:
                    backend.cancel(ka)
                except Exception:  # noqa: BLE001 - the release below ends it anyway
                    pass
            _wait_jobs_over(store, rec)
            if handle:
                backend.release_session(handle)
                gone = True
                try:
                    gone = _session_over(backend.get_session(handle))
                except Exception as exc:  # noqa: BLE001
                    gone = _err(exc).code == "not_found"
                if not gone:
                    raise HaloFuseError("release_unconfirmed", "The cloud has not confirmed the shutdown yet.", 502)
        except Exception as exc:  # noqa: BLE001
            err = _err(exc)
            _set_state(store, rid, "unknown", check_error=err.message, unknown_since=_clock(), last_try=prev)
            _note(store, rid, f"HaloFuse could not confirm the shutdown ({err.message}). The cloud machine may still "
                              "be running and billing. Press Shut down again, or Check.", "error",
                  "shutdown_unconfirmed", console=True)
            return view(store, settings)
        finally:
            if own and backend is not None:
                _close(backend)
        outcome = "cancelled" if reason == "user" else "failed"
        text = {"user": "Warm machine shut down.",
                "daily_cap": "Warm machine shut down: the daily spending limit was reached.",
                "start_timeout": "Warm machine shut down: it did not become ready in time."}.get(
            reason, "Warm machine shut down.")
        _end(store, rid, outcome, "shutdown_" + reason, text, "info" if reason == "user" else "warning")
        return view(store, settings)


def _stop_jobs(store: JobStore, rec: dict) -> None:
    from . import lifecycle
    for jid in rec["warm"].get("jobs") or []:
        job = store.get(jid)
        if job is None or job.get("terminal"):
            continue
        try:
            if job.get("kind") == "chunk" and job.get("parent_id"):
                from . import render_queue
                render_queue.cancel_queue(job["parent_id"], "now")
            else:
                lifecycle.request_cancel(jid, "now", "warm_shutdown", store=store)
            _note(store, rec["job_id"], f"Stopping {job.get('label') or 'a render'} that was still running on it.",
                  "warning", "render_stopped")
        except Exception:  # noqa: BLE001 - the release still happens
            _log.info("HaloFuse warm machine: could not stop job %s", jid)


def _wait_jobs_over(store: JobStore, rec: dict) -> None:
    deadline = time.monotonic() + _POLICY.shutdown_wait_s
    while time.monotonic() < deadline:
        if not _active_jobs(store, rec):
            return
        time.sleep(min(0.2, _POLICY.poll_s))


def _active_jobs(store: JobStore, rec: dict) -> list:
    out = []
    for jid in rec["warm"].get("jobs") or []:
        job = store.get(jid)
        if job is not None and not job.get("terminal"):
            out.append(jid)
    return out


def check(*, store: JobStore | None = None, settings: dict | None = None) -> dict:
    """Ask the cloud about the machine now (also clears a stale 'unknown' state when it answers)."""
    store = _store(store)
    settings = _settings(settings)
    rec = current(store)
    if rec is not None:
        backend = None
        try:
            backend = _open(settings)
            _check_session(store, rec, backend, force=True)
        except Exception as exc:  # noqa: BLE001
            _mark_unknown(store, rec["job_id"], _err(exc).message)
        finally:
            if backend is not None:
                _close(backend)
        _ensure_keeper(store)
    return view(store, settings)


def handle_for(instance_type: str | None, store: JobStore | None = None) -> str | None:
    """The handle of a running warm machine of this GPU type, else None."""
    rec = current(store)
    if rec is None:
        return None
    w = rec["warm"]
    if w.get("state") == "running" and w.get("handle") and (instance_type in (None, w.get("instance_type"))):
        return w["handle"]
    return None


def attach(job_id: str, store: JobStore | None = None) -> None:
    """A render or queue batch was sent to the warm machine: count it (cost, keep-alive, shutdown)."""
    store = _store(store)
    rec = current(store)
    if rec is None:
        return
    now = _clock()

    def fn(r):
        if job_id not in r["warm"]["jobs"]:
            r["warm"]["jobs"].append(job_id)
        r["warm"]["last_activity_at"] = now
    store.mutate(rec["job_id"], fn)
    job = store.get(job_id) or {}
    store.append_event(rec["job_id"], "note", text=f"{job.get('label') or 'Render'} sent to the warm machine.",
                       data={"code": "render_attached", "job_id": job_id})


def route(job_id: str, instance_type: str, *, store: JobStore | None = None, settings: dict | None = None):
    """Where a new render goes: ("now", handle) | ("wait", None) | ("cold", None). Notes go to the render's log."""
    store = _store(store)
    settings = _settings(settings)
    rec = current(store)
    if rec is None and keep_warm(settings):
        try:
            start(instance_type, store=store, settings=settings)
        except HaloFuseError as err:
            if str(settings.get("session_affinity") or "preferred").lower() == "required":
                raise
            store.append_event(job_id, "note", level="warning", data={"code": "warm_unavailable"},
                               text=f"The warm machine could not be started ({err.message}); this render runs on "
                                    "its own machine.")
            return "cold", None
        rec = current(store)
    if rec is None:
        return "cold", None
    w = rec["warm"]
    if w.get("instance_type") != instance_type:
        store.append_event(job_id, "note", level="warning", data={"code": "warm_other_gpu"},
                           text=f"This render asks for {instance_type}; the warm machine is a "
                                f"{w.get('instance_type')}, so the render runs on its own machine.")
        return "cold", None
    state = w.get("state")
    if state == "running" and w.get("handle"):
        return "now", w["handle"]
    if state == "starting":
        store.append_event(job_id, "note", data={"code": "warm_waiting"},
                           text="Waiting for the warm machine to be ready (usually a few minutes).")
        return "wait", None
    store.append_event(job_id, "note", level="warning", data={"code": "warm_unavailable"},
                       text=f"The warm machine is {state}; this render runs on its own machine.")
    return "cold", None


def queue_handle(instance_type: str, *, store: JobStore | None = None, settings: dict | None = None,
                 should_stop=None, note=None) -> str | None:
    """The warm machine for a render queue (started when "Keep the cloud machine warm" is on), else None: the
    queue then prepares its own session as before. Raises only when session affinity is "required"."""
    store = _store(store)
    settings = _settings(settings)
    note = note or (lambda *_a: None)
    rec = current(store)
    if rec is None and keep_warm(settings):
        try:
            start(instance_type, store=store, settings=settings)
        except HaloFuseError as err:
            if str(settings.get("session_affinity") or "preferred").lower() == "required":
                raise
            note(f"The warm machine could not be started ({err.message}).", "warning", "warm_unavailable")
            return None
        rec = current(store)
    if rec is None:
        return None
    w = rec["warm"]
    if w.get("instance_type") != instance_type:
        note(f"The warm machine is a {w.get('instance_type')}; this queue asks for {instance_type}, so it uses its "
             "own machine.", "warning", "warm_other_gpu")
        return None
    if w.get("state") == "starting":
        note("Waiting for the warm machine to be ready.", "info", "warm_waiting")
        wait_ready(store=store, should_stop=should_stop)
    return handle_for(instance_type, store)


def wait_ready(*, store: JobStore | None = None, should_stop=None, timeout: float | None = None) -> str | None:
    """Block until the warm machine runs (its handle) or is over / not wanted any more (None)."""
    store = _store(store)
    deadline = time.monotonic() + (timeout if timeout is not None else _POLICY.ready_timeout_s + 60)
    while time.monotonic() < deadline:
        if should_stop is not None and should_stop():
            return None
        rec = current(store)
        if rec is None:
            return None
        w = rec["warm"]
        if w.get("state") == "running" and w.get("handle"):
            return w["handle"]
        if w.get("state") != "starting":
            return None
        time.sleep(min(0.5, max(_POLICY.poll_s / 2, 0.005)))
    return None


# ---- view ---------------------------------------------------------------------------------------------------------

def _duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, m = seconds // 3600, (seconds % 3600) // 60
    return f"{h} h {m} min" if h else f"{m} min"


def view(store: JobStore | None = None, settings: dict | None = None, now: float | None = None) -> dict:
    store = _store(store)
    settings = _settings(settings)
    now = _clock() if now is None else now
    rec = current(store)
    out = {
        "state": "off", "record_id": None, "instance_type": None, "requested_at": None, "ready_at": None,
        "running_seconds": None, "rate_per_hour_usd": None, "cost_usd": None, "cost_basis": costmod.BASIS,
        "renders": 0, "active_renders": 0, "keepalives": 0, "hold_s": hold_seconds(settings), "warning": None,
        "message": None, "shutdown_confirm": None, "last": None,
        "settings": {"keep_warm": keep_warm(settings),
                     "clean_between_renders": settings.get("clean_between_renders") is not False,
                     "unload_models_between_renders": settings.get("unload_models_between_renders") is True,
                     "warm_hold_seconds": hold_seconds(settings)},
        "safety": {"daily_cap_usd": costmod.setting_number(settings, "daily_spend_cap_usd"),
                   "today_usd": costmod.daily_spend_usd(store, now),
                   "max_runtime_s": settings.get("max_runtime_seconds")},
        "cleaning": cleaning_status(store, settings),
    }
    if rec is None:
        last = _latest(store)
        if last is not None and last.get("warm"):
            lw = last["warm"]
            rate = costmod.rate_per_hour(last["cost"].get("rate_per_hour_usd"))
            out["last"] = {"record_id": last["job_id"], "instance_type": lw.get("instance_type"),
                           "ended_at": lw.get("ended_at"), "end_reason": lw.get("end_reason"),
                           "message": lw.get("end_message"),
                           "cost_usd": costmod.estimate_usd(rate, lw.get("elapsed_s"))}
        return out
    w = rec["warm"]
    rate = costmod.rate_per_hour(rec["cost"].get("rate_per_hour_usd"))
    elapsed = max(0.0, now - float(w.get("requested_at") or now))
    active = _active_jobs(store, rec)
    out.update({
        "state": w.get("state"), "record_id": rec["job_id"], "instance_type": w.get("instance_type"),
        "requested_at": w.get("requested_at"), "ready_at": w.get("ready_at"), "running_seconds": round(elapsed, 1),
        "rate_per_hour_usd": rate, "cost_usd": costmod.estimate_usd(rate, elapsed),
        "renders": len(w.get("jobs") or []), "active_renders": len(active),
        "keepalives": int((w.get("keepalive") or {}).get("count") or 0), "hold_s": w.get("hold_s"),
    })
    if w.get("state") == "unknown":
        out["warning"] = STALE_WARNING + (f" ({w['check_error']})" if w.get("check_error") else "")
    elif w.get("state") == "starting":
        out["message"] = "Starting the warm machine (usually a few minutes)."
    cost = f"about ${out['cost_usd']:.2f} so far (estimate)" if out["cost_usd"] is not None else \
        "its cost cannot be estimated (no price for this GPU)"
    stop = f" {len(active)} render(s) still running on it will be stopped." if active else ""
    out["shutdown_confirm"] = (f"Shut down the warm {w.get('instance_type')} machine? It has been on for "
                               f"{_duration(elapsed)}, {cost}.{stop} The next render will start a new machine.")
    return out


# ---- runtime cleaning capability --------------------------------------------------------------------------------

_FEATURES_NAME = "halofuse_runtime_features.json"


def _features_path(store: JobStore):
    return store.index_dir / _FEATURES_NAME


def observe_event(job_id: str, entry: dict) -> None:
    """Lifecycle event hook: the runner announces its housekeeping in every multi-workflow job (C-3 phase mark)."""
    if entry.get("kind") != "phase":
        return
    data = entry.get("data") or {}
    if data.get("phase") != "housekeeping" or data.get("detail") != "announce":
        return
    try:
        from .job_store import atomic_write_json
        store = default_store()
        atomic_write_json(_features_path(store), {"housekeeping": True, "seen_at": _clock(), "job_id": job_id})
    except Exception:  # noqa: BLE001 - advisory
        pass


def cleaning_status(store: JobStore, settings: dict) -> dict:
    """Whether the selected runtime image cleans between renders (the runner announces it); never a guess."""
    seen = False
    try:
        import json
        seen = bool(json.loads(_features_path(store).read_text(encoding="utf-8")).get("housekeeping"))
    except (OSError, ValueError):
        seen = False
    runtime = str(settings.get("runtime_image") or "")
    if seen:
        note = ("Cleaning between renders is active: between the workflows of one cloud job (a queue batch) the "
                "cloud machine clears temporary files and inputs no later workflow uses. Every single render starts "
                "in a fresh container on the machine, so its files never pile up.")
        supported = True
    elif runtime == "legacy":
        note = "The original cloud image does not clean between renders; the HaloFuse image does (next build)."
        supported = False
    else:
        note = ("Cleaning between renders needs the next HaloFuse runtime image; the image in use has not reported "
                "it yet. Every single render starts in a fresh container, so files never pile up across renders.")
        supported = None
    return {"supported": supported, "note": note}


# ---- keeper -------------------------------------------------------------------------------------------------------

def _mark_unknown(store: JobStore, rid: str, message: str) -> None:
    rec = store.get(rid)
    if rec is None or rec.get("terminal"):
        return
    was = rec["warm"].get("state")
    _set_state(store, rid, "unknown", check_error=message, unknown_since=rec["warm"].get("unknown_since") or _clock())
    if was != "unknown":
        _note(store, rid, f"HaloFuse lost track of the warm machine ({message}). It may still be running and "
                          "billing; HaloFuse keeps checking.", "error", "warm_unknown", console=True)


def _check_session(store: JobStore, rec: dict, backend, *, force: bool = False) -> None:
    rid = rec["job_id"]
    w = rec["warm"]
    now = _clock()
    if not force and w.get("last_check_at") and now - float(w["last_check_at"]) < _POLICY.check_every_s:
        return
    handle = w.get("handle")
    if not handle:
        _end(store, rid, "failed", "no_handle", "The warm machine has no cloud handle; it was not started.", "error")
        return
    try:
        session = backend.get_session(handle)
    except Exception as exc:  # noqa: BLE001
        err = _err(exc)
        if err.code == "not_found":
            _end(store, rid, "failed", "warm_gone", "The cloud no longer knows this warm machine; it has ended. "
                                                    "The next render starts a new machine.", "warning")
            return
        store.mutate(rid, lambda r: r["warm"].__setitem__("last_check_at", now))
        _mark_unknown(store, rid, err.message)
        return
    store.mutate(rid, lambda r: r["warm"].__setitem__("last_check_at", now))
    if _session_over(session):
        status = getattr(session, "status", None) or "ended"
        _end(store, rid, "failed", f"warm_{status}", f"The warm machine ended on the cloud side (status: {status}). "
                                                     "The next render starts a new machine.", "warning")
        return
    state = w.get("state")
    if _session_ready(session):
        if state in ("starting", "unknown"):
            fields = {"check_error": None, "unknown_since": None, "last_activity_at": now}
            if not w.get("ready_at"):
                fields["ready_at"] = now
            _set_state(store, rid, "running", **fields)
            _note(store, rid, "Warm machine ready: renders go to it until you shut it down." if state == "starting"
                  else "HaloFuse is back in touch with the warm machine.", "info",
                  "warm_ready" if state == "starting" else "warm_found", console=True)
        return
    if state == "unknown":  # it answers again but is still preparing
        _set_state(store, rid, "starting", check_error=None, unknown_since=None)
        state = "starting"
    if state == "starting" and now - float(w.get("requested_at") or now) > _POLICY.ready_timeout_s:
        shutdown(store=store, reason="start_timeout", backend=backend)


def _keepalive(store: JobStore, rec: dict, backend, settings: dict) -> None:
    rid = rec["job_id"]
    w = rec["warm"]
    now = _clock()
    ka = dict(w.get("keepalive") or {})
    if ka.get("job_id"):
        try:
            job = backend.get(ka["job_id"])
        except Exception:  # noqa: BLE001 - checked again next tick
            return
        if not getattr(job, "terminal", False):
            return
        ok = getattr(job, "status", None) == "succeeded"
        failures = 0 if ok else int(ka.get("failures") or 0) + 1

        def done(r):
            k = r["warm"]["keepalive"]
            k.update(job_id=None, count=int(k.get("count") or 0) + 1, failures=failures, last_at=now)
            r["warm"]["last_activity_at"] = now
        store.mutate(rid, done)
        if not ok:
            _note(store, rid, f"The keep-alive job ended {getattr(job, 'status', None)}. If this repeats, the cloud "
                              f"may end the machine {int(w.get('hold_s') or 0) // 60} min after its last job.",
                  "warning", "keepalive_failed", console=failures >= 3)
        return
    if _active_jobs(store, rec):
        store.mutate(rid, lambda r: r["warm"].__setitem__("last_activity_at", now))
        return
    hold = float(w.get("hold_s") or DEFAULT_HOLD_S)
    if now - float(w.get("last_activity_at") or now) < hold * _POLICY.keepalive_fraction:
        return
    command = list(settings.get("command") or ["python3"])
    spec = SubmitSpec(image=settings["image"], command=(command[0], "-c", "pass"),
                      instance_type=w.get("instance_type"), env={}, max_wall_clock_s=_POLICY.keepalive_max_wall_s,
                      tags=("halofuse", KEEPALIVE_TAG), instance_handle=w.get("handle"),
                      image_affinity=settings.get("image_affinity") or None)
    try:
        ref = backend.submit(spec)
    except Exception as exc:  # noqa: BLE001
        err = _err(exc)
        store.mutate(rid, lambda r: r["warm"]["keepalive"].__setitem__(
            "failures", int(r["warm"]["keepalive"].get("failures") or 0) + 1))
        _note(store, rid, f"The keep-alive job could not be sent ({err.message}); retrying.", "warning",
              "keepalive_failed")
        store.mutate(rid, lambda r: r["warm"].__setitem__("last_activity_at", now - hold * _POLICY.keepalive_fraction
                                                          + _POLICY.check_every_s))
        return
    store.mutate(rid, lambda r: r["warm"]["keepalive"].__setitem__("job_id", getattr(ref, "job_id", None)))


def _tick(store: JobStore, backend, settings: dict) -> bool:
    """One keeper step. Returns False when there is nothing left to keep."""
    rec = current(store)
    if rec is None:
        return False
    rid = rec["job_id"]
    state = rec["warm"].get("state")
    if state == "stopping":
        return True
    now = _clock()
    _check_session(store, rec, backend, force=state == "starting")
    rec = store.get(rid)
    if rec is None or rec.get("terminal"):
        return False
    secs = _job_seconds(store, rec["warm"].get("jobs"))
    rec = store.mutate(rid, lambda r: _update_cost(r, now, secs))
    if rec["warm"].get("state") == "running":
        _keepalive(store, rec, backend, settings)
        rec = store.get(rid)
    cap = costmod.setting_number(settings, "daily_spend_cap_usd")
    if cap is not None and costmod.daily_spend_usd(store, now) >= cap:
        shutdown(store=store, settings=settings, reason="daily_cap", backend=backend)
        return current(store) is not None
    w = rec["warm"]
    if now - float(w.get("last_cost_note_at") or now) >= _POLICY.cost_note_every_s:
        rate = costmod.rate_per_hour(rec["cost"].get("rate_per_hour_usd"))
        amount = costmod.estimate_usd(rate, w.get("elapsed_s"))
        store.mutate(rid, lambda r: r["warm"].__setitem__("last_cost_note_at", now))
        _note(store, rid, f"The warm machine is still on: {_duration(w.get('elapsed_s') or 0)}"
                          + (f", about ${amount:.2f} so far (estimate)." if amount is not None else ".")
                          + " Shut it down in the panel when you are done.", "warning", "warm_cost", console=True)
    return True


class _Keeper:
    def __init__(self, store: JobStore):
        self.store = store
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True, name="halofuse-warm-keeper")

    def run(self) -> None:
        global _KEEPER
        backend = None
        try:
            while not self.stop.is_set():
                try:
                    settings = _settings(None)
                    if backend is None:
                        backend = _open(settings)
                    if not _tick(self.store, backend, settings):
                        return
                except Exception as exc:  # noqa: BLE001 - never give up on a billable machine
                    err = _err(exc)
                    rec = current(self.store)
                    if rec is not None:
                        _mark_unknown(self.store, rec["job_id"], err.message)
                    if backend is not None:
                        _close(backend)
                        backend = None
                self.stop.wait(_POLICY.poll_s)
        finally:
            if backend is not None:
                _close(backend)
            with _LOCK:
                if _KEEPER is self:
                    _KEEPER = None


def _ensure_keeper(store: JobStore) -> None:
    global _KEEPER
    with _LOCK:
        if _KEEPER is not None and _KEEPER.thread.is_alive() and _KEEPER.store is store:
            return
        if _KEEPER is not None:
            _KEEPER.stop.set()
        _KEEPER = _Keeper(store)
        _KEEPER.thread.start()


def boot(store: JobStore | None = None) -> dict:
    """After a ComfyUI (re)start: a warm machine recorded as running is checked again, never assumed. Never raises."""
    store = _store(store)
    try:
        rec = current(store)
        if rec is None:
            return {"warm": None}
        _note(store, rec["job_id"], "ComfyUI restarted: checking the warm machine again.", "info", "warm_reattach")
        _ensure_keeper(store)
        return {"warm": rec["job_id"]}
    except Exception:  # noqa: BLE001
        _log.warning("HaloFuse could not re-check the warm machine at startup")
        return {"warm": None}


def stop_keeper(timeout: float = 5.0) -> None:
    """Tests only: stop the keeper thread."""
    global _KEEPER
    with _LOCK:
        keeper = _KEEPER
        _KEEPER = None
    if keeper is not None:
        keeper.stop.set()
        keeper.thread.join(timeout)

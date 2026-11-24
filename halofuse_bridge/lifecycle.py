"""Job lifecycle: state machine, real cancellation, runtime limit, cost cap, reattachment (contract C-2 §5-§6).

One ``JobWatcher`` thread per live cloud job polls the provider through the C-1 port, relays its log stream into the
job record, enforces the runtime / cost limits by *really* cancelling the cloud job, and finalises the record only
when the provider reports a terminal status. Watchers are daemon threads, but the record is durable and
``boot()`` re-creates a watcher for every non-terminal job after a ComfyUI restart (H1), so nothing depends on a
thread outliving the process.

The provider is reached only through ``provider.open_backend()`` (C-1); its event objects are read by attribute.
Nothing here imports the provider SDK.
"""
from __future__ import annotations

import json
from collections import deque
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable

from . import brand, provider
from . import cost as costmod
from .job_store import TERMINAL_STATES, HaloFuseError, JobStore, default_store
from .provider import BackendError
from .provider.redact import redact

_log = logging.getLogger(__name__)
_REJECTED_EVENT_KEEP = 300  # characters of an unvalidated [hf] line kept in the log

__all__ = [
    "HaloFuseError", "IllegalTransition", "JOB_STATES", "TERMINAL_STATES", "TRANSITIONS", "QUEUE_TRANSITIONS",
    "CANCEL_STATES", "CANCEL_ACTIVE", "LifecyclePolicy", "policy_from_settings", "open_backend",
    "transition", "queue_transition", "request_cancel", "JobWatcher", "start_watch",
    "active_watchers", "boot", "register_terminal_hook", "unregister_terminal_hook", "register_event_hook",
    "unregister_event_hook", "job_summary", "job_detail", "as_halofuse_error",
]

# ---- states ----------------------------------------------------------------------------------------------------

NON_TERMINAL_STATES = ("queued", "provisioning", "starting", "running", "preview_available", "cancelling",
                       "finalizing", "downloading")
JOB_STATES = NON_TERMINAL_STATES + ("succeeded", "failed", "cancelled", "timed_out")

TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"provisioning", "starting", "running", "cancelling", "finalizing", "failed",
                         "cancelled"}),
    "provisioning": frozenset({"starting", "running", "cancelling", "finalizing", "failed"}),
    "starting": frozenset({"running", "preview_available", "cancelling", "finalizing"}),
    "running": frozenset({"preview_available", "cancelling", "finalizing"}),
    "preview_available": frozenset({"running", "cancelling", "finalizing"}),
    "cancelling": frozenset({"running", "starting", "provisioning", "queued", "preview_available",
                             "finalizing"}),
    "finalizing": frozenset({"downloading", "succeeded", "failed", "cancelled", "timed_out"}),
    "downloading": frozenset({"finalizing", "succeeded", "failed", "cancelled", "timed_out"}),
    "succeeded": frozenset(), "failed": frozenset(), "cancelled": frozenset(), "timed_out": frozenset(),
}

QUEUE_STATES = ("queued", "running", "paused", "cancelling", "succeeded", "failed", "cancelled")
QUEUE_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"running", "paused", "cancelling", "succeeded", "failed", "cancelled"}),
    "running": frozenset({"paused", "cancelling", "succeeded", "failed", "cancelled"}),
    "paused": frozenset({"running", "cancelling", "succeeded", "failed", "cancelled"}),
    "cancelling": frozenset({"succeeded", "failed", "cancelled"}),
    "succeeded": frozenset(), "failed": frozenset(), "cancelled": frozenset(),
}

CANCEL_STATES = ("cancel_requested", "cancel_accepted", "cancel_confirmed", "cancel_failed", "already_completed")
CANCEL_ACTIVE = frozenset({"cancel_requested", "cancel_accepted", "cancel_confirmed"})
_LATE_STATES = frozenset({"finalizing", "downloading"}) | TERMINAL_STATES

_PROVIDER_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "smartcompute-interrupted"})
_PROMPT_RE = re.compile(r"^\[runner\] workflow queued, prompt_id=(\S+)")
_BOUNDARY_RE = re.compile(r"^\[runner\] (?:workflow completed in |workflow \d+/\d+ (?:succeeded|failed))")
_ERRORISH_RE = re.compile(r"(Error|Traceback|Exception|Killed)")
_SEEN_IDS_MAX = 20000

CANCEL_FAILED_MESSAGE = ("Stop could not be confirmed: the cloud job may still be running and billing. "
                         "Press Stop again, or check the job in Recent jobs.")


class IllegalTransition(Exception):
    pass


@dataclass
class LifecyclePolicy:
    poll_s: float = 5.0
    cancel_confirm_timeout_s: float = 90.0
    cancel_attempts: int = 2
    cancel_retry_sleep_s: float = 2.0
    cost_event_interval_s: float = 30.0
    poll_backoff_max_s: float = 60.0
    stream_retry_s: float = 5.0
    stream_max_retries: int = 20
    terminal_grace_s: float = 3.0
    progress_write_interval_s: float = 2.0


def policy_from_settings(settings: dict | None) -> LifecyclePolicy:
    policy = LifecyclePolicy()
    value = costmod.setting_number(settings, "cancel_confirm_timeout_seconds")
    if value is not None and value > 0:
        policy.cancel_confirm_timeout_s = value
    return policy


_POLICY = LifecyclePolicy()


def set_default_policy(policy: LifecyclePolicy | None) -> None:
    """Tests only."""
    global _POLICY
    _POLICY = policy or LifecyclePolicy()


def default_policy() -> LifecyclePolicy:
    return _POLICY


# ---- backend access (C-1) ---------------------------------------------------------------------------------------

def open_backend(settings: dict | None = None):
    """A new CloudBackend through the C-1 port (``provider.open_backend``); errors become branded HaloFuseErrors.

    Tests inject a fake with ``provider.use_backend(fake)``; there is no second injection point here.
    """
    try:
        return provider.open_backend(settings)
    except Exception as exc:  # noqa: BLE001
        raise as_halofuse_error(exc) from None


def close_backend(backend) -> None:
    close = getattr(backend, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001
            pass


_SHOW_REASON_CODES = {"input_upload_failed", "storage_error", "session_failed", "too_large", "unknown"}
_ANY_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S+")


def _visible_reason(detail) -> str:
    """The backend's own reason, safe to show: cloud hosts and storage addresses removed, URLs dropped, short."""
    if not detail:
        return ""
    text = _ANY_URL_RE.sub("<address>", redact(str(detail)))
    text = " ".join(text.split())
    return text[:300] + ("..." if len(text) > 300 else "")


def as_halofuse_error(exc: BaseException) -> HaloFuseError:
    """C-1 BackendError -> its code + branded user_message; anything else -> a generic branded message.

    Never ``str(exc)`` of a foreign exception: the redacted detail goes to the private bridge log only.
    """
    if isinstance(exc, HaloFuseError):
        return exc
    if isinstance(exc, BackendError):
        if exc.detail:
            _log.info("HaloFuse backend error %s: %s", exc.code, exc.detail)
        message = exc.user_message
        reason = _visible_reason(exc.detail)
        if reason and exc.code in _SHOW_REASON_CODES:
            # owner 2026-11-24: "could not upload the job inputs" alone is a dead end - say why (hosts removed)
            message = f"{message} Reason: {reason}"
        return HaloFuseError(exc.code, message, int(exc.status or 502))
    _log.warning("HaloFuse internal error: %s", redact(f"{type(exc).__name__}: {exc}"))
    return HaloFuseError("internal", brand.message("unknown"), 500)


# ---- hooks ------------------------------------------------------------------------------------------------------

_HOOK_LOCK = threading.RLock()
_TERMINAL_HOOKS: list[tuple[str, Callable]] = []
_EVENT_HOOKS: list[tuple[str, Callable]] = []
_LISTENING: set[int] = set()


def register_terminal_hook(fn: Callable[[str, str, JobStore], None], *, name: str) -> None:
    with _HOOK_LOCK:
        unregister_terminal_hook(name)
        _TERMINAL_HOOKS.append((name, fn))


def unregister_terminal_hook(name: str) -> None:
    with _HOOK_LOCK:
        _TERMINAL_HOOKS[:] = [(n, f) for n, f in _TERMINAL_HOOKS if n != name]


def register_event_hook(fn: Callable[[str, dict], None], *, name: str) -> None:
    with _HOOK_LOCK:
        unregister_event_hook(name)
        _EVENT_HOOKS.append((name, fn))


def unregister_event_hook(name: str) -> None:
    with _HOOK_LOCK:
        _EVENT_HOOKS[:] = [(n, f) for n, f in _EVENT_HOOKS if n != name]


def _dispatch_event(job_id: str, entry: dict) -> None:
    with _HOOK_LOCK:
        hooks = list(_EVENT_HOOKS)
    for _name, fn in hooks:
        try:
            fn(job_id, entry)
        except Exception:  # noqa: BLE001 - event hooks never break the watcher
            _log.debug("HaloFuse event hook %s failed", _name)


def _ensure_listener(store: JobStore) -> JobStore:
    with _HOOK_LOCK:
        if id(store) not in _LISTENING:
            store.add_listener(_dispatch_event)
            _LISTENING.add(id(store))
    return store


def _store(store: JobStore | None) -> JobStore:
    return _ensure_listener(store or default_store())


# ---- transitions ------------------------------------------------------------------------------------------------

def _check(table: dict, current: str, to: str) -> None:
    if to not in table:
        raise IllegalTransition(f"unknown state {to!r}")
    if current != to and to not in table.get(current, frozenset()):
        raise IllegalTransition(f"{current} -> {to}")


def _transition(store: JobStore, job_id: str, to: str, note: str | None, table: dict) -> dict:
    holder: dict = {}

    def fn(rec):
        current = rec.get("state") or "queued"
        _check(table, current, to)
        holder["from"] = current
        if current != to:
            rec["state"] = to
            if to in TERMINAL_STATES:
                rec["terminal"] = True
                rec["ended_at"] = time.time()

    rec = store.mutate(job_id, fn)
    if holder.get("from") != to:
        store.append_event(job_id, "state", text=f"Status: {holder['from']} -> {to}" + (f" ({note})" if note else ""),
                           data={"from": holder["from"], "to": to, "note": note})
    return rec


def transition(store: JobStore | None, job_id: str, to: str, note: str | None = None) -> dict:
    """Move a render/chunk record to ``to``; raises IllegalTransition. Same state = no-op."""
    return _transition(_store(store), job_id, to, note, TRANSITIONS)


def queue_transition(store: JobStore | None, job_id: str, to: str, note: str | None = None) -> dict:
    return _transition(_store(store), job_id, to, note, QUEUE_TRANSITIONS)


def _advance(store: JobStore, job_id: str, target: str | None) -> None:
    """Follow the provider forward only; never move a job backwards or out of cancelling/late states."""
    if not target:
        return
    rec = store.get(job_id)
    if rec is None:
        return
    current = rec.get("state")
    if current == target or current in _LATE_STATES or current in ("cancelling", "preview_available"):
        return
    if target in TRANSITIONS.get(current, frozenset()):
        try:
            transition(store, job_id, target)
        except IllegalTransition:
            pass  # another thread moved the job first; the provider poll converges on the next round


def _provider_state(status: str | None, saw_prompt: bool) -> str | None:
    if status == "queued":
        return "queued"
    if status == "provisioning":
        return "provisioning"
    if status == "running":
        return "running" if saw_prompt else "starting"
    return None


# ---- cancellation -----------------------------------------------------------------------------------------------

def _cancel_event(store: JobStore, job_id: str, state: str, rec: dict, text: str, level: str = "info") -> None:
    c = rec.get("cancel") or {}
    store.append_event(job_id, "cancel", text=text, level=level,
                       data={"state": state, "mode": c.get("mode"), "reason": c.get("reason")})


def request_cancel(job_id: str, mode: str = "now", reason: str = "user", *, store: JobStore | None = None,
                   now: float | None = None, backend=None) -> dict:
    """Stop a render or chunk (C-2 §5.2). Idempotent; calls the provider cancel at most once per stop request."""
    if mode not in ("now", "after_current"):
        raise HaloFuseError("bad_request", "Unknown stop mode.", 400)
    store = _store(store)
    now = time.time() if now is None else now
    decision: dict = {}

    def fn(rec):
        c = rec.setdefault("cancel", {})
        state = rec.get("state")
        if rec.get("terminal") or state in _LATE_STATES:
            if not c.get("state"):
                c.update(state="already_completed", mode=mode, reason=reason, requested_at=now)
                decision["action"] = "already_completed"
            else:
                decision["action"] = "noop"
            return
        if c.get("state") in CANCEL_ACTIVE:
            if mode == "now" and c.get("mode") == "after_current" and c.get("state") == "cancel_requested":
                c["mode"] = "now"  # escalate "after current" to "now"
                decision["action"] = "escalate"
                c["prev_state"] = state
                rec["state"] = "cancelling" if "cancelling" in TRANSITIONS.get(state, ()) else state
                decision["from"] = state
                return
            decision["action"] = "noop"
            return
        c.update(state="cancel_requested", mode=mode, reason=reason, requested_at=now, accepted_at=None,
                 confirmed_at=None, failed_at=None, detail=None, prev_state=state)
        decision["action"] = "requested"
        if mode == "now" and "cancelling" in TRANSITIONS.get(state, ()):
            rec["state"] = "cancelling"
            decision["from"] = state

    rec = store.mutate(job_id, fn)
    action = decision.get("action")
    if action == "already_completed":
        _cancel_event(store, job_id, "already_completed", rec, "Stop requested, but the render had already finished.")
        return rec
    if action == "noop":
        return rec
    if decision.get("from") and decision["from"] != rec.get("state"):
        store.append_event(job_id, "state", text=f"Status: {decision['from']} -> {rec['state']}",
                           data={"from": decision["from"], "to": rec["state"], "note": reason})
    if action == "requested":
        what = "Stop now" if mode == "now" else "Stop after the current render"
        _cancel_event(store, job_id, "cancel_requested", rec, f"{what} requested ({reason}).")
    cloud_id = (rec.get("cloud") or {}).get("current_job_id")
    if mode == "after_current" or not cloud_id:
        return rec  # the watcher (boundary) or the submit path issues the provider cancel
    return _issue_backend_cancel(store, job_id, backend=backend)


def _issue_backend_cancel(store: JobStore, job_id: str, *, backend=None, policy: LifecyclePolicy | None = None,
                          sleep: Callable = time.sleep) -> dict:
    policy = policy or default_policy()
    rec = store.get(job_id)
    cloud_id = (rec.get("cloud") or {}).get("current_job_id") if rec else None
    if not cloud_id:
        return rec
    last_err: HaloFuseError | None = None
    result = None
    be = backend
    try:
        if be is None:
            be = open_backend(None)
        for attempt in range(max(1, policy.cancel_attempts)):
            store.mutate(job_id, lambda r: r["cancel"].__setitem__("backend_calls",
                                                                   int(r["cancel"].get("backend_calls") or 0) + 1))
            try:
                result = be.cancel(cloud_id)
                last_err = None
                break
            except Exception as exc:  # noqa: BLE001
                last_err = as_halofuse_error(exc)
                if attempt + 1 < policy.cancel_attempts:
                    sleep(policy.cancel_retry_sleep_s)
    except HaloFuseError as exc:
        last_err = exc
    finally:
        if backend is None and be is not None:
            close_backend(be)
    if last_err is not None or result is None:
        return _mark_cancel_failed(store, job_id, (last_err.message if last_err else CANCEL_FAILED_MESSAGE))
    t = time.time()
    status = getattr(result, "status", None)
    terminal = bool(getattr(result, "terminal", status in _PROVIDER_TERMINAL))
    if terminal and status in ("succeeded", "failed") and not getattr(result, "cancel_requested_at", None):
        def done(r):
            r["cancel"].update(state="already_completed", accepted_at=t)
            r["status_note"] = "completed before cancellation"
        rec = store.mutate(job_id, done)
        _cancel_event(store, job_id, "already_completed", rec, "The render finished before the stop took effect.")
        return rec

    def accepted(r):
        if r["cancel"].get("state") == "cancel_requested":
            r["cancel"].update(state="cancel_accepted", accepted_at=t)
    rec = store.mutate(job_id, accepted)
    if rec["cancel"].get("state") == "cancel_accepted":
        _cancel_event(store, job_id, "cancel_accepted", rec,
                      "The cloud service accepted the stop request; waiting for the worker to end.")
    return rec


def _mark_cancel_failed(store: JobStore, job_id: str, detail: str) -> dict:
    t = time.time()
    holder: dict = {}

    def fn(r):
        c = r["cancel"]
        if c.get("state") not in ("cancel_requested", "cancel_accepted"):
            holder["skip"] = True
            return
        c.update(state="cancel_failed", failed_at=t, detail=detail)
        prev = c.get("prev_state") or "running"
        if r.get("state") == "cancelling" and prev in TRANSITIONS["cancelling"]:
            holder["from"] = "cancelling"
            r["state"] = prev
    rec = store.mutate(job_id, fn)
    if holder.get("skip"):
        return rec
    if holder.get("from"):
        store.append_event(job_id, "state", text=f"Status: cancelling -> {rec['state']}",
                           data={"from": "cancelling", "to": rec["state"], "note": "stop not confirmed"})
    _cancel_event(store, job_id, "cancel_failed", rec, detail, level="error")
    store.append_event(job_id, "error", text=CANCEL_FAILED_MESSAGE, level="error", data={"code": "cancel_failed"})
    return rec


# ---- the watcher ------------------------------------------------------------------------------------------------

_WATCHERS: dict[str, "JobWatcher"] = {}
_WATCH_LOCK = threading.Lock()


def _runner_events_parse():
    try:
        from .runner_events import parse  # type: ignore[attr-defined]  # WS4 (H-c)
        return parse
    except Exception:  # noqa: BLE001
        return None


class JobWatcher:
    """Watches one render/chunk record until the provider reports terminal, then finalises it."""

    def __init__(self, job_id: str, *, store: JobStore | None = None, policy: LifecyclePolicy | None = None,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 backend=None):
        self.job_id = job_id
        self.store = _store(store)
        self.policy = policy or default_policy()
        self.clock = clock
        self.sleep = sleep
        self.backend = backend
        self._own_backend = backend is None
        self.done = threading.Event()
        self._stream_thread: threading.Thread | None = None
        self._last_cost_event = 0.0
        self._last_progress_write = 0.0
        self._last_poll_warning = 0.0
        self._limit_cancel_at: dict[str, float] = {}
        self._parse = _runner_events_parse()
        self._nonce = (self.store.get(job_id) or {}).get("event_nonce")
        self._seen_ids: set = set()          # SSE event ids of stored lines (bounded), so a replay is dropped
        self._seen_order: deque = deque()

    # -- plumbing --
    def _be(self):
        if self.backend is None:
            self.backend = open_backend(None)
        return self.backend

    def _cloud_id(self) -> str | None:
        rec = self.store.get(self.job_id)
        return (rec.get("cloud") or {}).get("current_job_id") if rec else None

    # -- log stream --
    def handle_event(self, ev) -> None:
        if self.done.is_set():
            return
        if hasattr(ev, "line"):
            event_id = getattr(ev, "event_id", None)
            if event_id is not None:
                if event_id in self._seen_ids:
                    return  # replayed after a reconnect: already stored
                self._seen_ids.add(event_id)
                self._seen_order.append(event_id)
                if len(self._seen_order) > _SEEN_IDS_MAX:
                    self._seen_ids.discard(self._seen_order.popleft())
            self._handle_line(str(getattr(ev, "line", "")), getattr(ev, "stream", None))
        elif hasattr(ev, "dropped"):
            dropped = getattr(ev, "dropped", None)
            text = (f"The cloud log stream dropped {dropped} lines." if dropped
                    else "The cloud log stream dropped some lines.")
            self.store.append_event(self.job_id, "truncated", text=text, source="provider", level="warning",
                                    data={"dropped": dropped})
        elif hasattr(ev, "status"):
            rec = self.store.get(self.job_id)
            if rec is not None:
                _advance(self.store, self.job_id, _provider_state(getattr(ev, "status", None),
                                                                  bool(rec.get("prompt_ids"))))

    def _handle_line(self, line: str, stream) -> None:
        parsed = None
        if self._parse is not None and line.startswith("[hf]"):
            try:
                parsed = self._parse(line, nonce=self._nonce)
            except Exception:  # noqa: BLE001
                parsed = None
        if isinstance(parsed, dict) and parsed.get("t"):
            self._handle_run_event(parsed)
            return
        if line.startswith("[hf]") and len(line) > _REJECTED_EVENT_KEEP:
            # an event line that failed validation (foreign nonce, oversize): never store its payload (frame bytes)
            line = f"{line[:_REJECTED_EVENT_KEEP]}…[rejected event, {len(line) - _REJECTED_EVENT_KEEP} characters omitted]"
        source = "runner" if line.startswith("[runner]") else "comfyui"
        level = "error" if stream == "stderr" and _ERRORISH_RE.search(line) else "info"
        self.store.append_event(self.job_id, "line", text=line, source=source, level=level,
                                data={"stream": stream})
        m = _PROMPT_RE.match(line)
        if m:
            pid = m.group(1)
            rec = self.store.mutate(self.job_id, lambda r: r["prompt_ids"].append(pid)
                                    if pid not in r["prompt_ids"] else None)
            self.store.append_event(self.job_id, "prompt", text=f"ComfyUI prompt {pid} queued",
                                    data={"prompt_id": pid})
            if rec.get("state") == "starting":
                _advance(self.store, self.job_id, "running")
        if _BOUNDARY_RE.match(line):
            self._maybe_after_current()

    def _handle_run_event(self, ev: dict) -> None:
        t = str(ev.get("t"))
        data = ev
        if t == "preview":
            # live preview (C-4 6a): the frame goes to the job's previews/ folder now; the log keeps the record only
            try:
                from . import live_previews
                rec = live_previews.sink_for(self.job_id, self.store).accept(ev)
            except Exception:  # noqa: BLE001 - a preview never breaks the watcher
                rec = None
            data = {k: v for k, v in ev.items() if k not in ("data", "text")}
            data["preview"] = rec
            if rec is None:
                data["stored"] = False
        text = ev.get("text") or ev.get("message") or json.dumps(data, ensure_ascii=False, default=str)[:2000]
        level = "error" if t == "error" else "info"
        self.store.append_event(self.job_id, t, text=str(text), source="runner", level=level, data=data,
                                node_id=ev.get("node_id") or ev.get("node"), phase=ev.get("phase"))
        if t == "fingerprint":
            self.store.update(self.job_id, fingerprint={k: v for k, v in ev.items() if k not in ("t", "v", "ts")})
        elif t == "progress":
            now = self.clock()
            if now - self._last_progress_write >= self.policy.progress_write_interval_s:
                self._last_progress_write = now
                self.store.update(self.job_id, progress={k: v for k, v in ev.items() if k not in ("v",)})
        elif t == "preview":
            _advance(self.store, self.job_id, "preview_available")
        elif t in ("final",):
            self._maybe_after_current()

    def _maybe_after_current(self) -> None:
        rec = self.store.get(self.job_id)
        if not rec or rec.get("kind") == "chunk":
            return
        c = rec.get("cancel") or {}
        if c.get("mode") == "after_current" and c.get("state") == "cancel_requested":
            self.store.append_event(self.job_id, "cancel", text="Current render finished; stopping the rest.",
                                    data={"state": "cancel_requested", "mode": "after_current",
                                          "reason": c.get("reason")})
            _issue_backend_cancel(self.store, self.job_id, backend=self._be(), policy=self.policy,
                                  sleep=self.sleep)

    def stream_once(self) -> None:
        cloud_id = self._cloud_id()
        if not cloud_id:
            return
        for ev in self._be().events(cloud_id):
            if self.done.is_set():
                break
            self.handle_event(ev)

    def _stream_loop(self) -> None:
        retries = 0
        while not self.done.is_set():
            try:
                self.stream_once()
            except Exception:  # noqa: BLE001 - the log feed reconnects; the status poll is authoritative
                pass
            if self.done.is_set():
                return
            rec = self.store.get(self.job_id)
            if not rec or rec.get("terminal") or rec.get("state") in _LATE_STATES:
                return
            retries += 1
            if retries > self.policy.stream_max_retries:
                self.store.append_event(self.job_id, "note", level="warning",
                                        text="The live log stream stopped; the full log arrives with the outputs.",
                                        data={"code": "stream_gave_up"})
                return
            self.done.wait(self.policy.stream_retry_s)
            if self.done.is_set():
                return
            self.store.append_event(self.job_id, "note", level="warning",
                                    text="Live log reconnected; lines printed while disconnected are not "
                                         "replayed by the cloud service (they arrive with the outputs).",
                                    data={"code": "stream_reconnected"})

    # -- status poll --
    def poll_once(self) -> bool:
        """One status poll + limit checks. Returns True once the record is terminal."""
        rec = self.store.get(self.job_id)
        if rec is None or rec.get("terminal"):
            return True
        cloud_id = (rec.get("cloud") or {}).get("current_job_id")
        if not cloud_id:
            return False
        job = self._be().get(cloud_id)
        now = self.clock()
        status = getattr(job, "status", None)
        self._record_cloud(job, now)
        if status in _PROVIDER_TERMINAL or getattr(job, "terminal", False):
            self.finalize(job)
            return True
        rec = self.store.get(self.job_id)
        _advance(self.store, self.job_id, _provider_state(status, bool(rec.get("prompt_ids"))))
        self._check_limits(now)
        return False

    def _record_cloud(self, job, now: float) -> None:
        ts = {k: costmod.parse_ts(getattr(job, k, None)) for k in
              ("started_provisioning_at", "started_running_at", "terminal_at", "cancel_requested_at")}

        def fn(r):
            c = r["cloud"]
            c["status"] = getattr(job, "status", c.get("status"))
            for k in ("exit_code", "error_code", "error_message", "gpu_name", "cuda_version", "image_digest",
                      "image_cache_hit", "image_affinity"):
                v = getattr(job, k, None)
                if v is not None:
                    c[k] = v
            for k, v in ts.items():
                if v is not None:
                    c["timestamps"][k] = v
            secs = costmod.billable_seconds(c["timestamps"].get("started_running_at"),
                                            c["timestamps"].get("terminal_at"), now)
            r["cost"]["billable_seconds"] = None if secs is None else round(secs, 1)
            r["cost"]["estimate_usd"] = costmod.estimate_usd(r["cost"].get("rate_per_hour_usd"), secs)
        rec = self.store.mutate(self.job_id, fn)
        if now - self._last_cost_event >= self.policy.cost_event_interval_s and rec["cost"].get("estimate_usd") \
                is not None:
            self._last_cost_event = now
            self._cost_event(rec)

    def _cost_event(self, rec: dict) -> None:
        c = rec["cost"]
        self.store.append_event(self.job_id, "cost",
                                text=f"Estimated cost so far: ${c['estimate_usd']:.4f} "
                                     f"({c.get('billable_seconds') or 0:.0f} s at "
                                     f"${costmod.rate_per_hour(c.get('rate_per_hour_usd')) or 0:.2f}/h)",
                                data={"amount_usd": c["estimate_usd"], "billable_seconds": c.get("billable_seconds"),
                                      "rate_per_hour_usd": costmod.rate_per_hour(c.get("rate_per_hour_usd")),
                                      "basis": costmod.BASIS})

    def _check_limits(self, now: float) -> None:
        rec = self.store.get(self.job_id)
        c = rec.get("cancel") or {}
        limits = rec.get("limits") or {}
        retry_gap = self.policy.cancel_confirm_timeout_s
        # (3) confirmation timeout for an accepted stop
        if c.get("state") == "cancel_accepted" and c.get("accepted_at") is not None \
                and now - float(c["accepted_at"]) > self.policy.cancel_confirm_timeout_s:
            _mark_cancel_failed(self.store, self.job_id,
                                f"The cloud job did not confirm it stopped within "
                                f"{self.policy.cancel_confirm_timeout_s:.0f} s; it may still be running and billing.")
            rec = self.store.get(self.job_id)
            c = rec.get("cancel") or {}
        # (1) runtime limit, (2) per-job cost cap: both stop the cloud job for real
        max_rt = limits.get("max_runtime_s")
        started = rec.get("submitted_at")
        if max_rt and started is not None and now - float(started) > float(max_rt):
            if self._limit_due("timeout", c, now, retry_gap):
                self.store.append_event(self.job_id, "timeout", level="warning",
                                        text=f"Maximum runtime of {int(max_rt)} s reached; stopping the cloud job.",
                                        data={"max_runtime_s": max_rt, "elapsed_s": round(now - float(started), 1)})
                self._force_cancel("timeout")
            return
        cap = limits.get("max_job_cost_usd")
        amount = (rec.get("cost") or {}).get("estimate_usd")
        if costmod.exceeds(amount, cap):
            if self._limit_due("cost_cap", c, now, retry_gap):
                self.store.append_event(self.job_id, "note", level="warning",
                                        text=f"Cost limit ${float(cap):.2f} reached (estimate ${float(amount):.2f}); "
                                             "stopping the cloud job.", data={"code": "cost_cap"})
                self._force_cancel("cost_cap")

    def _limit_due(self, reason: str, c: dict, now: float, retry_gap: float) -> bool:
        """True when a limit stop must be issued now: first time, or a failed stop due for a retry."""
        last = self._limit_cancel_at.get(reason)
        state = c.get("state")
        if state in CANCEL_ACTIVE and c.get("mode") == "now":
            self._limit_cancel_at.setdefault(reason, now)  # a stop is already under way (e.g. after a restart)
            return False
        if last is not None and state != "cancel_failed" and not (state == "cancel_requested"
                                                                 and c.get("mode") == "after_current"):
            return False
        if last is not None and state == "cancel_failed" and now - last < retry_gap:
            return False
        self._limit_cancel_at[reason] = now
        return True

    def _force_cancel(self, reason: str) -> None:
        def fn(r):
            c = r["cancel"]
            if c.get("state") == "cancel_failed" or (c.get("state") == "cancel_requested"
                                                     and c.get("mode") == "after_current"):
                c["state"] = None  # a limit overrides a failed or deferred stop with a fresh "stop now"
        self.store.mutate(self.job_id, fn)
        request_cancel(self.job_id, "now", reason, store=self.store, backend=self._be())

    # -- terminal --
    def finalize(self, job) -> dict:
        """Outcome from the provider terminal status, then hooks (delivery), then the terminal state."""
        if self._stream_thread is not None and self._stream_thread.is_alive() and self.policy.terminal_grace_s > 0:
            self._stream_thread.join(self.policy.terminal_grace_s)  # trailing log lines / markers
        self.done.set()
        store, job_id = self.store, self.job_id
        status = getattr(job, "status", None)
        now = time.time()
        info: dict = {}

        def fn(r):
            c = r["cancel"]
            ours = c.get("state") in ("cancel_requested", "cancel_accepted", "cancel_confirmed", "cancel_failed")
            accepted = c.get("state") in ("cancel_accepted", "cancel_confirmed", "cancel_failed")
            if status == "succeeded":
                outcome = "succeeded"
                if ours:
                    c["state"] = "already_completed"
                    r["status_note"] = "completed before cancellation"
            elif status == "cancelled" or (status == "failed" and accepted and getattr(job, "cancel_requested_at",
                                                                                         None)):
                outcome = "timed_out" if c.get("reason") == "timeout" else "cancelled"
                if ours:
                    c.update(state="cancel_confirmed", confirmed_at=now)
                    if status == "failed":
                        r["status_note"] = "the cloud service reported the stopped job as failed"
                    elif c.get("reason") == "cost_cap":
                        r["status_note"] = "stopped at the cost limit"
                else:
                    r["status_note"] = "cancelled outside HaloFuse"
            else:
                outcome = "failed"
                if ours:
                    c["state"] = "already_completed"
                    r["status_note"] = "failed before cancellation"
                code = "interrupted" if status == "smartcompute-interrupted" else \
                    (getattr(job, "error_code", None) or "job_failed")
                info["error_code"] = code
            r["outcome"] = outcome
            info["outcome"] = outcome
            info["cancel_state"] = c.get("state")
        rec = store.mutate(job_id, fn)
        outcome = info["outcome"]
        if info.get("cancel_state") == "cancel_confirmed":
            _cancel_event(store, job_id, "cancel_confirmed", rec,
                          "The cloud service confirmed the job ended (billing end itself is not reported).")
        elif rec["cancel"].get("state") == "already_completed" and rec.get("status_note"):
            _cancel_event(store, job_id, "already_completed", rec, f"Stop request: {rec['status_note']}.")
        if outcome == "failed":
            message = self._failure_message(job)
            store.update(job_id, error={"code": info["error_code"], "message": message,
                                        "detail_ref": "logs/error.log"})
            store.append_event(job_id, "error", text=message, level="error", data={"code": info["error_code"]})
            self._diagnose(job)
        transition(store, job_id, "finalizing", note=outcome)
        try:
            from . import live_previews
            live_previews.finish(job_id, store)  # before delivery writes into the same previews/ folder
        except Exception:  # noqa: BLE001 - cleanup failure never changes the outcome
            _log.debug("HaloFuse live preview cleanup failed for %s", job_id)
        with _HOOK_LOCK:
            hooks = list(_TERMINAL_HOOKS)
        for name, fn_hook in hooks:
            try:
                fn_hook(job_id, outcome, store)
            except Exception:  # noqa: BLE001 - a hook failure never changes the outcome
                store.append_event(job_id, "error", level="error", text=f"Post-processing step '{name}' failed.",
                                   data={"code": "hook_failed", "hook": name})
        rec = store.get(job_id)
        if rec.get("state") == "downloading":
            transition(store, job_id, "finalizing")
        rec = transition(store, job_id, outcome)
        if rec["cost"].get("estimate_usd") is not None:
            self._cost_event(rec)
        if self._own_backend:
            close_backend(self.backend)
        return rec

    def _failure_message(self, job) -> str:
        try:
            from .jobs import _extract_validation_error
            detail = _extract_validation_error(self.store.tail_text(self.job_id, 2000))
        except Exception:  # noqa: BLE001
            detail = None
        if detail:
            return detail
        if getattr(job, "status", None) == "smartcompute-interrupted":
            return "The cloud machine was interrupted before the render finished."
        code = getattr(job, "exit_code", None)
        return f"The cloud render failed (exit code {code})." if code is not None else "The cloud render failed."

    def _diagnose(self, job) -> None:
        try:
            from .diagnose import classify  # type: ignore[attr-defined]  # WS4
        except Exception:  # noqa: BLE001
            return
        try:
            events = self.store.read_events(self.job_id, since=max(0, self.store.total_events(self.job_id) - 2000),
                                            limit=2000)["entries"]
            d = classify(events, job, exc=None)
            d = d.to_dict() if hasattr(d, "to_dict") else d
            get = (lambda k: d.get(k)) if isinstance(d, dict) else (lambda k: getattr(d, k, None))
            self.store.update(self.job_id, diagnosis={k: get(k) for k in ("code", "title", "cause", "next_action",
                                                                           "raw_ref")})
        except Exception:  # noqa: BLE001
            _log.debug("HaloFuse diagnosis failed for %s", self.job_id)

    def run(self) -> None:
        backoff = self.policy.poll_s
        try:
            self._stream_thread = threading.Thread(target=self._stream_loop, daemon=True,
                                                   name=f"halofuse-log-{self.job_id[:8]}")
            self._stream_thread.start()
            while True:
                try:
                    if self.poll_once():
                        return
                    backoff = self.policy.poll_s
                except Exception as exc:  # noqa: BLE001 - never give up on a live, billable job
                    err = as_halofuse_error(exc)
                    now = self.clock()
                    if now - self._last_poll_warning >= 60:
                        self._last_poll_warning = now
                        self.store.append_event(self.job_id, "note", level="warning",
                                                text=f"Status check failed ({err.message}); retrying.",
                                                data={"code": err.code})
                    backoff = min(max(backoff * 2, self.policy.poll_s), self.policy.poll_backoff_max_s)
                    self.sleep(backoff)
                    continue
                self.sleep(self.policy.poll_s)
        finally:
            self.done.set()
            if self._own_backend:
                close_backend(self.backend)
            with _WATCH_LOCK:
                if _WATCHERS.get(self.job_id) is self:
                    del _WATCHERS[self.job_id]


def start_watch(job_id: str, *, store: JobStore | None = None, policy: LifecyclePolicy | None = None) -> bool:
    """Start (once per job) the watcher thread for a render/chunk record. Returns False if already watched."""
    with _WATCH_LOCK:
        if job_id in _WATCHERS:
            return False
        watcher = JobWatcher(job_id, store=store, policy=policy)
        _WATCHERS[job_id] = watcher
    threading.Thread(target=watcher.run, daemon=True, name=f"halofuse-watch-{job_id[:8]}").start()
    return True


def active_watchers() -> list[str]:
    with _WATCH_LOCK:
        return list(_WATCHERS)


def wait_idle(timeout: float = 5.0) -> bool:
    """Tests only: wait until no watcher thread is running."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not active_watchers():
            return True
        time.sleep(0.01)
    return not active_watchers()


# ---- reattachment -----------------------------------------------------------------------------------------------

_BOOTED: set[int] = set()
_BOOT_LOCK = threading.Lock()


def reset_for_tests() -> None:
    with _BOOT_LOCK:
        _BOOTED.clear()
    with _HOOK_LOCK:
        _LISTENING.clear()


def boot(store: JobStore | None = None, settings: dict | None = None, *, policy: LifecyclePolicy | None = None,
         reconcile: bool = True) -> dict:
    """Reattach after a (re)start (C-2 §5.4). Idempotent per store. Never raises."""
    store = _store(store)
    with _BOOT_LOCK:
        if id(store) in _BOOTED:
            return {"skipped": True}
        _BOOTED.add(id(store))
    report = {"watching": [], "recovered": [], "interrupted": [], "queues": []}
    backend = None
    try:
        try:
            backend = open_backend(settings)
        except HaloFuseError as exc:
            _log.info("HaloFuse reattach without the cloud connector (%s)", exc.code)
        for s in store.list(active_only=True, limit=None):
            if s.get("kind") not in ("render", "chunk", "recovered"):
                continue
            job_id = s["job_id"]
            rec = store.get(job_id)
            if rec is None:
                continue
            if (rec.get("cloud") or {}).get("current_job_id"):
                start_watch(job_id, store=store, policy=policy)
                report["watching"].append(job_id)
                continue
            found = None
            if backend is not None:
                try:
                    found = next(iter(backend.list_jobs([f"hf-job-{job_id}"])), None)
                except Exception:  # noqa: BLE001
                    found = None
            if found is not None:
                cid = getattr(found, "job_id", None)
                store.mutate(job_id, lambda r: (r["cloud"]["job_ids"].append(cid), r["cloud"].__setitem__(
                    "current_job_id", cid), r.__setitem__("submitted_at", r.get("submitted_at") or time.time())))
                store.append_event(job_id, "submit", text="Re-attached to the cloud job after a restart.",
                                   data={"cloud_job_id": cid, "instance_type": rec["cloud"].get("instance_type")})
                start_watch(job_id, store=store, policy=policy)
                report["watching"].append(job_id)
            else:
                _fail_without_cloud(store, job_id, "submit_interrupted",
                                    "HaloFuse restarted while this render was being submitted; it did not start.")
                report["interrupted"].append(job_id)
        if reconcile and backend is not None:
            try:
                cloud_jobs = list(backend.list_jobs([store.install_tag]))
            except Exception:  # noqa: BLE001
                cloud_jobs = []
            for job in cloud_jobs:
                cid = getattr(job, "job_id", None)
                if not cid or store.find_by_cloud_id(cid):
                    continue
                tags = list(getattr(job, "tags", ()) or ())
                local = next((t[len("hf-job-"):] for t in tags if t.startswith("hf-job-")), None)
                new_id = local if local and not store.exists(local) else cid
                try:
                    if store.exists(new_id):
                        continue
                    store.create(kind="recovered", job_id=new_id, label="Recovered render",
                                 fields={"cloud": {"job_ids": [cid], "current_job_id": cid,
                                                   "instance_type": getattr(job, "instance_type", None)},
                                         "submitted_at": costmod.parse_ts(getattr(job, "created_at", None))})
                except (ValueError, FileExistsError):
                    continue
                store.append_event(new_id, "note", text="Job record rebuilt from the cloud service after the local "
                                                        "record was lost.", data={"code": "recovered"})
                report["recovered"].append(new_id)
                start_watch(new_id, store=store, policy=policy)
        try:
            from . import render_queue
            report["queues"] = render_queue.resume_all(store, policy=policy)
        except Exception:  # noqa: BLE001
            _log.warning("HaloFuse could not resume queues")
    finally:
        if backend is not None:
            close_backend(backend)
    return report


def _fail_without_cloud(store: JobStore, job_id: str, code: str, message: str) -> dict:
    """A record that never got a cloud job ends as failed (via finalizing when it is not plain queued)."""
    rec = store.get(job_id)
    store.update(job_id, error={"code": code, "message": message, "detail_ref": "logs/error.log"}, outcome="failed")
    store.append_event(job_id, "error", text=message, level="error", data={"code": code})
    if rec.get("state") not in ("queued",):
        if rec.get("state") not in ("finalizing", "downloading"):
            transition(store, job_id, "finalizing", note="failed")
    return transition(store, job_id, "failed")


# ---- views (C-4 §2 with C-2 §8 deviations) ----------------------------------------------------------------------

def job_summary(rec: dict, store: JobStore | None = None) -> dict:
    cloud = rec.get("cloud") or {}
    c = rec.get("cancel") or {}
    cost = rec.get("cost") or {}
    total = (store or default_store()).total_events(rec["job_id"]) if rec.get("job_id") else 0
    amount = cost.get("actual_usd") if cost.get("actual_usd") is not None else cost.get("estimate_usd")
    return {
        "job_id": rec.get("job_id"), "kind": rec.get("kind"), "parent_id": rec.get("parent_id"),
        "label": rec.get("label"), "status": rec.get("state"), "terminal": bool(rec.get("terminal")),
        "outcome": rec.get("outcome"),
        "created_at": rec.get("created_at"), "updated_at": rec.get("updated_at"), "ended_at": rec.get("ended_at"),
        "instance_type": cloud.get("instance_type"),
        "cloud_job_ids": list(cloud.get("job_ids") or []), "prompt_ids": list(rec.get("prompt_ids") or []),
        "cancel": {"state": c.get("state"), "mode": c.get("mode"), "reason": c.get("reason"),
                   "requested_at": c.get("requested_at"), "note": rec.get("status_note"),
                   "detail": c.get("detail")},
        "progress": rec.get("progress"),
        "cost": {"rate_per_hour_usd": costmod.rate_per_hour(cost.get("rate_per_hour_usd")), "amount_usd": amount,
                 "basis": "actual" if cost.get("actual_usd") is not None else costmod.BASIS},
        "delivery": rec.get("delivery"), "diagnosis": rec.get("diagnosis"),
        "error_report": _light_report(rec.get("error_report")),
        "error": ({"code": rec["error"].get("code"), "message": rec["error"].get("message")}
                  if isinstance(rec.get("error"), dict) else None),
        "log_total": total,
        "preview_mode": rec.get("preview_mode"), "preview_method": rec.get("preview_method"),
        "live_support": _live_support(rec),
    }


def _live_support(rec: dict) -> dict:
    """Whether the runtime this job ran on relays live previews (the Live tab's runtime label); never raises."""
    try:
        from . import live_previews
        return live_previews.runtime_support(rec.get("runtime"))
    except Exception:  # noqa: BLE001 - a summary never fails on a display hint
        return {"state": "unknown", "note": None}


def _light_report(report) -> dict | None:
    """The error report without its (possibly long) technical text, for job lists."""
    if not isinstance(report, dict):
        return None
    return {k: v for k, v in report.items() if k != "technical"}


def job_detail(rec: dict, store: JobStore | None = None) -> dict:
    out = job_summary(rec, store)
    cloud = rec.get("cloud") or {}
    out.update({
        "limits": dict(rec.get("limits") or {}),
        "render_repeats": rec.get("render_repeats"),
        "timestamps": dict(cloud.get("timestamps") or {}),
        "submitted_at": rec.get("submitted_at"),
        "exit_code": cloud.get("exit_code"),
        "gpu_name": cloud.get("gpu_name"),
        "fingerprint": rec.get("fingerprint") or {},
        "settings_snapshot": {"render_repeats": rec.get("render_repeats")},
    })
    if isinstance(rec.get("error_report"), dict):
        from .error_report import from_record  # L9.338: complete technical text only in the detail view
        out["error_report"] = from_record(rec["error_report"])
    from . import delivery  # lazy: delivery imports lifecycle
    out["outputs"] = delivery.outputs(rec["job_id"], store, limit=1000) if rec.get("job_id") else []
    return out


def new_error_id() -> str:
    return brand.new_error_id()

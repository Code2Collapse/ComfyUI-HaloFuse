"""Render queue on a warm session, persisted in the job store (B3, contract C-2).

A queue is a job record of kind ``queue`` whose ``items`` are queue items (one workflow snapshot each, stored as
``items/<item_id>.json``). A ``QueueRunner`` thread prepares a warm session, then repeatedly takes up to
``queue_chunk_size`` *pending* items in queue order, submits them as one manifest cloud job (a *chunk*, its own job
record of kind ``chunk``, watched by ``lifecycle.JobWatcher``), and derives per-item status from the runner's
``[runner] workflow M/T ...`` markers. Pending items can be reordered, edited, removed, and the queue paused or
stopped (after the current chunk, or now = real cancel of the active chunk). Items stop being "pending" once their
chunk is submitted. Everything is in the store, so a queue survives a ComfyUI restart (``resume_all``).
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import threading
import time
import uuid

from . import cost as costmod
from . import jobs, lifecycle
from .job_store import HaloFuseError, JobStore, default_store
from .provider import image_contract

_log = logging.getLogger(__name__)

_HOLD_SECONDS = 600            # warm-session idle hold (provider releases it after this long idle)
_READY_TIMEOUT = 900           # max seconds to wait for the session to report ready
_AFFINITY_RETRIES = 3          # prepare attempts on no_capacity before falling back / aborting
_AFFINITY_RETRY_SLEEP = 5
DEFAULT_CHUNK_SIZE = 10
DEFAULT_IDLE_WARN_S = 120
_ITEM_TERMINAL = frozenset({"succeeded", "failed", "cancelled"})

# Matches the runner's exact marker strings (the runner's run_manifest()) — compatibility contract:
#   [runner] workflow 2/5 started
#   [runner] workflow 2/5 succeeded (3 file(s))
#   [runner] workflow 2/5 failed: <reason>
#   [runner] workflow 2/5 failed: <reason> (2 partial file(s))
_MARKER_RE = re.compile(
    r"^\[runner\] workflow (?P<local>\d+)/(?P<total>\d+) "
    r"(?P<event>started|succeeded|failed)"
    r"(?:: (?P<reason>.*?))?"
    r"(?: \((?P<count>\d+) (?:partial )?file\(s\)\))?$"
)

_MANIFEST_REJECTED = ("internal error: the cloud runner rejected this batch's manifest (exit code 2) — this "
                      "indicates a bug in how HaloFuse built the request, not a workflow problem")


def _clamp(value) -> int:
    try:
        return max(1, min(int(value or 1), 100))
    except (TypeError, ValueError):
        return 1


def _new_item_id() -> str:
    return "i-" + secrets.token_hex(5)


# ---- in-memory helpers (rebuildable; never the source of truth) -------------------------------------------------

_CHUNK_PARENT: dict[str, str] = {}
_RUNNERS: dict[str, threading.Thread] = {}
_RUNNER_LOCK = threading.Lock()


def _on_event(job_id: str, entry: dict) -> None:
    """Event hook: runner markers on a chunk's log drive its queue items' status."""
    if entry.get("kind") != "line":
        return
    qid = _CHUNK_PARENT.get(job_id)
    if not qid:
        return
    m = _MARKER_RE.match(entry.get("text") or "")
    if not m:
        return
    local = int(m.group("local"))
    event = m.group("event")
    store = default_store() if _STORE_FOR.get(qid) is None else _STORE_FOR[qid]

    def fn(r):
        for it in r.get("items", []):
            if it.get("chunk_id") != job_id or it.get("local_index") != local:
                continue
            if it.get("status") in _ITEM_TERMINAL:
                return
            if event == "started":
                it["status"] = "running"
            elif event == "succeeded":
                it["status"] = "succeeded"
                it["outputs_available"] = True
            else:
                it["status"] = "failed"
                it["error"] = m.group("reason") or "workflow failed"
                it["outputs_available"] = m.group("count") is not None
            return
    try:
        store.mutate(qid, fn)
    except KeyError:
        pass


_STORE_FOR: dict[str, JobStore] = {}
lifecycle.register_event_hook(_on_event, name="queue_markers")


def _store(store: JobStore | None) -> JobStore:
    return lifecycle._store(store)


def _get_queue(store: JobStore, qid: str) -> dict:
    rec = store.get(qid) if store.exists(qid) else None
    if rec is None or rec.get("kind") != "queue":
        raise HaloFuseError("queue_not_found", "This HaloFuse queue was not found.", 404)
    return rec


# ---- submit + operations ----------------------------------------------------------------------------------------

def _make_items(store: JobStore, qid: str, items: list, start_index: int) -> list[dict]:
    out = []
    for n, it in enumerate(items):
        wf = it.get("workflow") or it.get("prompt")
        if not isinstance(wf, dict) or not wf:
            raise HaloFuseError("bad_request", "A queued item is missing its workflow.", 400)
        idx = start_index + n
        label = (str(it.get("label") or "").strip() or f"Job {idx + 1}")
        item_id = _new_item_id()
        rel = f"items/{item_id}.json"
        store.write_file(qid, rel, json.dumps(wf))
        out.append({"item_id": item_id, "index": idx, "label": label,
                    "render_repeats": _clamp(it.get("render_repeats", it.get("batch_count"))),
                    "status": "pending", "chunk_id": None, "local_index": None, "outputs_available": False,
                    "error": None, "workflow_path": rel})
    return out


def submit_queue(items: list, instance_type: str | None = None, *, client_ref: str | None = None,
                 store: JobStore | None = None, policy: lifecycle.LifecyclePolicy | None = None,
                 start: bool = True) -> str:
    """Create a persisted queue and start its runner. ``items`` = [{workflow, label?, render_repeats|batch_count?}]."""
    store = _store(store)
    if not items:
        raise HaloFuseError("bad_request", "No workflows in the queue.", 400)
    if client_ref:
        known = store.find_by_client_ref(client_ref)
        if known:
            return known
    settings = jobs._load_settings()
    costmod.check_daily_cap(store, settings, time.time())
    try:
        chunk_size = max(1, int(settings.get("queue_chunk_size") or DEFAULT_CHUNK_SIZE))
    except (TypeError, ValueError):
        chunk_size = DEFAULT_CHUNK_SIZE
    rec = store.create(kind="queue", label="Queue", fields={
        "client_ref": client_ref,
        "queue": {"chunk_size": chunk_size, "instance_type": instance_type or settings.get("instance_type")}})
    qid = rec["job_id"]
    store.update(qid, items=_make_items(store, qid, items, 0))
    store.append_event(qid, "queue", text=f"Queue created with {len(items)} workflow(s).",
                       data={"op": "create", "item_id": None})
    if start:
        start_runner(qid, store=store, policy=policy)
    return qid


def _pending_op(store: JobStore, qid: str, op: str, fn, item_id: str | None = None, text: str = "") -> dict:
    """Run ``fn(rec)`` under the queue lock after the common checks; emit a queue event."""
    _get_queue(store, qid)

    def guarded(r):
        if r.get("terminal"):
            raise HaloFuseError("job_terminal", "This queue has already finished.", 409)
        fn(r)
        for i, it in enumerate(r["items"]):
            it["index"] = i
    rec = store.mutate(qid, guarded)
    store.append_event(qid, "queue", text=text or f"Queue: {op}", data={"op": op, "item_id": item_id})
    return rec


def _find_item(rec: dict, item_id: str) -> dict:
    for it in rec["items"]:
        if it["item_id"] == item_id:
            return it
    raise HaloFuseError("item_not_found", "This queue item was not found.", 404)


def _require_pending(it: dict) -> None:
    if it.get("status") != "pending":
        raise HaloFuseError("item_not_pending", "Only items that have not been sent to the cloud can be changed.",
                            409)


def append_items(qid: str, items: list, *, store: JobStore | None = None) -> dict:
    store = _store(store)
    rec = _get_queue(store, qid)
    new = _make_items(store, qid, items, len(rec["items"]))
    return _pending_op(store, qid, "append", lambda r: r["items"].extend(new),
                       text=f"Added {len(new)} workflow(s) to the queue.")


def reorder(qid: str, item_ids: list, *, store: JobStore | None = None) -> dict:
    store = _store(store)

    def fn(r):
        pending = [it for it in r["items"] if it.get("status") == "pending"]
        if sorted(item_ids or []) != sorted(it["item_id"] for it in pending) or len(set(item_ids)) != len(item_ids):
            raise HaloFuseError("bad_request", "Reorder needs every pending item exactly once.", 400)
        by_id = {it["item_id"]: it for it in pending}
        r["items"] = [it for it in r["items"] if it.get("status") != "pending"] + [by_id[i] for i in item_ids]
    return _pending_op(store, qid, "reorder", fn, text="Pending items reordered.")


def patch_item(qid: str, item_id: str, *, label=None, render_repeats=None, workflow=None,
               store: JobStore | None = None) -> dict:
    store = _store(store)
    if workflow is not None and (not isinstance(workflow, dict) or not workflow):
        raise HaloFuseError("bad_request", "The replacement workflow is empty.", 400)

    def fn(r):
        it = _find_item(r, item_id)
        _require_pending(it)
        if label is not None:
            it["label"] = str(label).strip() or it["label"]
        if render_repeats is not None:
            it["render_repeats"] = _clamp(render_repeats)
        if workflow is not None:
            store.write_file(qid, it["workflow_path"], json.dumps(workflow))
    return _pending_op(store, qid, "replace" if workflow is not None else "edit", fn, item_id,
                       text="Pending item updated.")


def delete_item(qid: str, item_id: str, *, store: JobStore | None = None) -> dict:
    store = _store(store)

    def fn(r):
        it = _find_item(r, item_id)
        _require_pending(it)
        r["items"].remove(it)
    return _pending_op(store, qid, "remove", fn, item_id, text="Pending item removed.")


def pause(qid: str, *, store: JobStore | None = None) -> dict:
    store = _store(store)
    return _pending_op(store, qid, "pause", lambda r: r["queue"].__setitem__("paused", True),
                       text="Queue paused: no new batch starts until it is resumed (a running batch continues).")


def resume(qid: str, *, store: JobStore | None = None) -> dict:
    store = _store(store)
    rec = _pending_op(store, qid, "resume", lambda r: r["queue"].__setitem__("paused", False),
                      text="Queue resumed.")
    start_runner(qid, store=store)  # no-op when the runner is alive
    return rec


def cancel_queue(qid: str, mode: str = "after_current", *, store: JobStore | None = None) -> dict:
    """Stop after current (the active chunk finishes) or stop now (the active chunk is really cancelled)."""
    if mode not in ("now", "after_current"):
        raise HaloFuseError("bad_request", "Unknown stop mode.", 400)
    store = _store(store)
    rec = _get_queue(store, qid)
    if rec.get("terminal"):
        def done(r):
            if not r["cancel"].get("state"):
                r["cancel"].update(state="already_completed", mode=mode, requested_at=time.time())
        return store.mutate(qid, done)
    t = time.time()

    def fn(r):
        q = r["queue"]
        q["stop_mode"] = "now" if "now" in (mode, q.get("stop_mode")) else "after_current"
        r["cancel"].update(state="cancel_requested", mode=q["stop_mode"], reason="user", requested_at=t)
        for it in r["items"]:
            if it.get("status") == "pending":
                it.update(status="cancelled", error="Stopped before it was sent to the cloud.")
    rec = store.mutate(qid, fn)
    what = "Stop now" if rec["queue"]["stop_mode"] == "now" else "Stop after the current batch"
    store.append_event(qid, "cancel", text=f"{what} requested.",
                       data={"state": "cancel_requested", "mode": rec["queue"]["stop_mode"], "reason": "user"})
    if rec.get("state") in lifecycle.QUEUE_TRANSITIONS and "cancelling" in lifecycle.QUEUE_TRANSITIONS[rec["state"]]:
        rec = lifecycle.queue_transition(store, qid, "cancelling")
    active = rec["queue"].get("active_chunk_id")
    if rec["queue"]["stop_mode"] == "now" and active and store.exists(active):
        lifecycle.request_cancel(active, "now", "queue_stop", store=store)
    start_runner(qid, store=store)  # make sure someone finishes the record (e.g. a paused queue after a restart)
    return store.get(qid)


# ---- the runner -------------------------------------------------------------------------------------------------

def start_runner(qid: str, *, store: JobStore | None = None, policy: lifecycle.LifecyclePolicy | None = None) -> bool:
    store = _store(store)
    with _RUNNER_LOCK:
        th = _RUNNERS.get(qid)
        if th is not None and th.is_alive():
            return False
        rec = store.get(qid)
        if rec is None or rec.get("terminal"):
            return False
        _STORE_FOR[qid] = store
        runner = QueueRunner(qid, store=store, policy=policy)
        th = threading.Thread(target=runner.run, daemon=True, name=f"halofuse-queue-{qid[:8]}")
        _RUNNERS[qid] = th
    th.start()
    return True


def wait_runners(timeout: float = 10.0) -> bool:
    """Tests only."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _RUNNER_LOCK:
            alive = [t for t in _RUNNERS.values() if t.is_alive()]
        if not alive:
            return True
        time.sleep(0.01)
    return False


class QueueRunner:
    def __init__(self, qid: str, *, store: JobStore, policy: lifecycle.LifecyclePolicy | None = None,
                 clock=time.time, sleep=time.sleep):
        self.qid = qid
        self.store = store
        self.policy = policy or lifecycle.default_policy()
        self.clock = clock
        self.sleep = sleep
        self.backend = None
        self.handle: str | None = None
        self.settings: dict = {}
        self.stop_reason: str | None = None

    # -- helpers --
    def _note(self, text: str, level: str = "info", code: str | None = None) -> None:
        self.store.append_event(self.qid, "note", text=text, level=level, data={"code": code} if code else None)

    def _rec(self) -> dict:
        return self.store.get(self.qid)

    def _set_state(self, to: str) -> None:
        rec = self._rec()
        if rec["state"] != to and to in lifecycle.QUEUE_TRANSITIONS.get(rec["state"], ()):
            lifecycle.queue_transition(self.store, self.qid, to)

    def _poll_sleep(self) -> None:
        self.sleep(self.policy.poll_s)

    # -- main --
    def run(self) -> None:
        try:
            self._run()
        except Exception as exc:  # noqa: BLE001 - the queue record must always end, with a branded reason
            err = lifecycle.as_halofuse_error(exc)
            self.store.append_event(self.qid, "error", level="error", text=err.message, data={"code": err.code})

            def fail(r):
                r["error"] = {"code": err.code, "message": err.message, "detail_ref": "logs/error.log"}
                for it in r["items"]:
                    if it.get("status") == "pending":
                        it.update(status="failed", error=f"Queue stopped: {err.message}")
            self.store.mutate(self.qid, fail)
            self._finish(failed=True)
        finally:
            self._release()
            if self.backend is not None:
                lifecycle.close_backend(self.backend)

    def _run(self) -> None:
        rec = self._rec()
        if rec is None or rec.get("terminal"):
            return
        self.settings = jobs._load_settings()
        for it in rec["items"]:
            if it.get("chunk_id"):
                _CHUNK_PARENT[it["chunk_id"]] = self.qid
        active = rec["queue"].get("active_chunk_id")
        if active and self.store.exists(active):  # resumed after a restart with a batch in flight
            self._wait_chunk(active)
            self._after_chunk(active)
        if self._pending() and not self._rec()["queue"].get("stop_mode"):
            self.backend = lifecycle.open_backend(self.settings)
            self._ensure_session()
        while True:
            rec = self._rec()
            q = rec["queue"]
            if q.get("stop_mode") or self.stop_reason:
                break
            pending = [it for it in rec["items"] if it.get("status") == "pending"]
            if not pending:
                break
            if q.get("paused"):
                self._set_state("paused")
                self._idle_warning(rec)
                self._poll_sleep()
                continue
            self._set_state("running")
            if self.backend is None:
                self.backend = lifecycle.open_backend(self.settings)
            self._ensure_session()
            batch = pending[: max(1, int(q.get("chunk_size") or DEFAULT_CHUNK_SIZE))]
            chunk_id = self._submit_chunk(batch)
            if chunk_id is None:
                if self.stop_reason:
                    break
                continue  # the items were stopped / edited away concurrently; re-read the queue
            self._wait_chunk(chunk_id)
            self._after_chunk(chunk_id)
        self._finish()

    def _pending(self) -> list:
        return [it for it in self._rec()["items"] if it.get("status") == "pending"]

    # -- warm session --
    def _ensure_session(self) -> None:
        be = self.backend
        rec = self._rec()
        handle = self.handle or rec["queue"]["session"].get("handle")
        if handle:
            try:
                s = be.get_session(handle)
                if getattr(s, "ready", False) or getattr(s, "status", None) in ("ready", "running"):
                    self.handle = handle
                    return
            except Exception:  # noqa: BLE001 - expired / unknown: prepare a new one
                pass
            self.handle = None
            self.store.mutate(self.qid, lambda r: r["queue"]["session"].update(held=False, handle=None))
        sku = rec["queue"].get("instance_type") or self.settings.get("instance_type")
        affinity = str(self.settings.get("session_affinity") or "preferred").lower()
        session = None
        for attempt in range(1, _AFFINITY_RETRIES + 1):
            self._note(f"Preparing a warm {sku} machine (attempt {attempt}/{_AFFINITY_RETRIES}).")
            try:
                session = be.prepare_session(sku, _HOLD_SECONDS)
                break
            except Exception as exc:  # noqa: BLE001
                err = lifecycle.as_halofuse_error(exc)
                if err.code != "no_capacity":
                    raise err from None
                self._note("No warm machine available right now.", "warning", "no_capacity")
                if attempt < _AFFINITY_RETRIES:
                    self.sleep(min(_AFFINITY_RETRY_SLEEP, self.policy.poll_s * 10))
        if session is None:
            if affinity == "required":
                raise HaloFuseError("no_capacity", "No warm cloud machine was available (session affinity is "
                                                   "'required'), so the queue was stopped.", 409)
            self._note("Running without a warm machine (cold starts apply).", "warning", "no_session")
            return
        handle = getattr(session, "handle", None)
        waited = 0.0
        while not (getattr(session, "ready", False) or getattr(session, "status", None) == "ready"):
            if getattr(session, "terminal", False):
                raise HaloFuseError("session_failed", "The warm cloud machine could not be prepared.", 409)
            if self._rec()["queue"].get("stop_mode"):
                self.handle = handle
                return
            if waited >= _READY_TIMEOUT:
                raise HaloFuseError("session_failed", "The warm cloud machine did not become ready in time.", 409)
            self._poll_sleep()
            waited += max(self.policy.poll_s, 0.001)
            session = be.get_session(handle)
        self.handle = handle
        t = self.clock()
        self.store.mutate(self.qid, lambda r: r["queue"]["session"].update(held=True, handle=handle, idle_since=t,
                                                                         idle_warned=False))
        self._note("Warm machine ready.")

    def _idle_warning(self, rec: dict) -> None:
        sess = rec["queue"]["session"]
        if not sess.get("held"):
            return
        warn_s = costmod.setting_number(self.settings, "idle_session_warn_seconds", DEFAULT_IDLE_WARN_S)
        if costmod.idle_warning_due(sess.get("idle_since"), self.clock(), warn_s, bool(sess.get("idle_warned"))):
            self.store.mutate(self.qid, lambda r: r["queue"]["session"].__setitem__("idle_warned", True))
            self._note(f"A warm cloud machine has been held idle for over {int(warn_s)} s while the queue is "
                       "paused, and idle time is billed. Resume or stop the queue.", "warning", "idle_session")

    def _release(self) -> None:
        if not self.handle or self.backend is None:
            return
        try:
            self.backend.release_session(self.handle)
            self._note("Warm machine released.")
        except Exception as exc:  # noqa: BLE001
            err = lifecycle.as_halofuse_error(exc)
            self.store.append_event(self.qid, "error", level="error",
                                    text=f"The warm machine could not be released ({err.message}); it ends "
                                         "automatically after its idle hold.", data={"code": "release_failed"})
        self.handle = None
        try:
            self.store.mutate(self.qid, lambda r: r["queue"]["session"].update(held=False, handle=None))
        except KeyError:
            pass

    # -- chunks --
    def _submit_chunk(self, batch: list) -> str | None:
        rec = self._rec()
        sku = rec["queue"].get("instance_type") or self.settings.get("instance_type")
        max_rt = jobs.max_runtime_for(self.settings)
        n_chunk = 1 + len({it.get("chunk_id") for it in rec["items"] if it.get("chunk_id")})
        chunk_id = str(uuid.uuid4())
        wanted = [it["item_id"] for it in batch]
        taken: list[str] = []

        def mark(r):
            # Under the queue lock: only items that are STILL pending (a concurrent Stop / edit / delete wins).
            for iid in wanted:
                it = next((x for x in r["items"] if x["item_id"] == iid), None)
                if it is not None and it.get("status") == "pending":
                    taken.append(iid)
                    it.update(status="submitted", chunk_id=chunk_id, local_index=len(taken))
            if taken:
                r["queue"]["active_chunk_id"] = chunk_id
                r["queue"]["session"].update(idle_since=None, idle_warned=False)
        _CHUNK_PARENT[chunk_id] = self.qid
        rec = self.store.mutate(self.qid, mark)
        if not taken:
            return None
        batch = [next(x for x in rec["items"] if x["item_id"] == iid) for iid in taken]
        nonce = jobs.new_event_nonce()
        self.store.create(kind="chunk", job_id=chunk_id, label=f"Batch {n_chunk}", parent_id=self.qid, fields={
            "event_nonce": nonce,
            "items": [{"item_id": it["item_id"], "local_index": it["local_index"], "label": it["label"]}
                      for it in batch],
            "cloud": {"instance_type": sku, "instance_handle_set": bool(self.handle)},
            "limits": {"max_runtime_s": max_rt, "max_wall_clock_s_sent": max_rt + jobs.WALL_CLOCK_MARGIN_S,
                       "max_job_cost_usd": costmod.setting_number(self.settings, "max_job_cost_usd")},
        })
        labels = ", ".join(it["label"] for it in batch)
        self.store.append_event(self.qid, "queue", text=f"Batch {n_chunk}: submitting {len(batch)} workflow(s) "
                                                        f"({labels}).", data={"op": "submit", "item_id": None,
                                                                              "chunk_id": chunk_id})
        try:
            entries = []
            for it in batch:
                wf = json.loads(self.store.read_file(self.qid, it["workflow_path"]))
                jobs._normalize_model_paths(wf)
                entries.append({"workflow": wf, "batch_count": it["render_repeats"]})
            jobs._record_rate(self.store, chunk_id, self.backend, sku)
            manifest = {"workflows": entries}
            ref = jobs.stage_and_submit(
                self.backend, settings=self.settings, instance_type=sku,
                files={self.settings.get("manifest_name") or image_contract.MANIFEST_NAME: json.dumps(manifest)},
                workflows_for_inputs=[e["workflow"] for e in entries],
                env=jobs._job_env(self.settings, jobs._build_chunk_env(self.settings), max_runtime_s=max_rt,
                                  preview_mode=None, nonce=nonce),
                tags=jobs.build_tags(self.store, chunk_id), max_wall_clock_s=max_rt + jobs.WALL_CLOCK_MARGIN_S,
                instance_handle=self.handle,
                log=lambda m: self.store.append_event(self.qid, "note", text=m))
        except Exception as exc:  # noqa: BLE001
            err = lifecycle.as_halofuse_error(exc)
            lifecycle._fail_without_cloud(self.store, chunk_id, err.code, err.message)

            def failed(r):
                for it in r["items"]:
                    if it.get("chunk_id") == chunk_id or it.get("status") == "pending":
                        it.update(status="failed", error=f"Batch submission failed: {err.message}")
                r["queue"]["active_chunk_id"] = None
            self.store.mutate(self.qid, failed)
            self.store.append_event(self.qid, "error", level="error",
                                    text=f"Batch {n_chunk}: submission failed — {err.message}",
                                    data={"code": err.code})
            self.stop_reason = "submit_failed"
            return None
        jobs._attach_cloud_job(self.store, chunk_id, ref, sku, policy=self.policy)
        return chunk_id

    def _wait_chunk(self, chunk_id: str) -> None:
        while True:
            rec = self.store.get(chunk_id)
            if rec is None or rec.get("terminal"):
                return
            if (rec.get("cloud") or {}).get("current_job_id"):
                lifecycle.start_watch(chunk_id, store=self.store, policy=self.policy)  # idempotent
            self._poll_sleep()

    def _after_chunk(self, chunk_id: str) -> None:
        chunk = self.store.get(chunk_id) or {}
        outcome = chunk.get("outcome") or chunk.get("state")
        exit_code = (chunk.get("cloud") or {}).get("exit_code")
        gap_status = "cancelled" if outcome in ("cancelled", "timed_out") else "failed"
        if gap_status == "cancelled":
            gap_reason = "Stopped before this workflow finished."
        elif exit_code == 6:
            gap_reason = "batch aborted: the ComfyUI process died before this workflow could run"
        else:
            gap_reason = (f"no result reported for this workflow before the batch ended "
                          f"(batch status: {outcome!r})")
        slices = _item_log_slices(self.store, chunk_id)
        t = self.clock()

        def fn(r):
            for it in r["items"]:
                if it.get("chunk_id") != chunk_id:
                    continue
                if it.get("status") not in _ITEM_TERMINAL:
                    it.update(status=gap_status, error=gap_reason)
                if it.get("status") == "failed":
                    detail = jobs._extract_validation_error(slices.get(it.get("local_index"), []))
                    if detail:
                        it["error"] = detail
            r["queue"]["active_chunk_id"] = None
            if r["queue"]["session"].get("held"):
                r["queue"]["session"].update(idle_since=t, idle_warned=False)
            if exit_code == 2:
                for it in r["items"]:
                    if it.get("status") == "pending":
                        it.update(status="failed", error=_MANIFEST_REJECTED)
        self.store.mutate(self.qid, fn)
        if exit_code == 2:
            self.stop_reason = "manifest_rejected"
            self.store.append_event(self.qid, "error", level="error", text=_MANIFEST_REJECTED,
                                    data={"code": "manifest_rejected"})
        self.store.append_event(self.qid, "queue", text=f"{chunk.get('label', 'Batch')} finished ({outcome}).",
                                data={"op": "chunk_done", "item_id": None, "chunk_id": chunk_id})

    def _finish(self, failed: bool = False) -> None:
        rec = self._rec()
        if rec is None or rec.get("terminal"):
            return
        self._release()  # the warm machine is released before the queue is reported finished (billing)
        rec = self._rec()
        statuses = [it.get("status") for it in rec["items"]]
        stop = rec["queue"].get("stop_mode")
        if stop and "cancelled" in statuses:
            final = "cancelled"
        elif failed or "failed" in statuses:
            final = "failed"
        else:
            final = "succeeded"

        def fn(r):
            if stop:
                r["cancel"]["state"] = "cancel_confirmed" if final == "cancelled" else "already_completed"
                r["cancel"]["confirmed_at"] = time.time()
        self.store.mutate(self.qid, fn)
        n_ok = statuses.count("succeeded")
        self.store.append_event(self.qid, "queue", text=f"Queue finished: {n_ok} succeeded, "
                                                        f"{statuses.count('failed')} failed, "
                                                        f"{statuses.count('cancelled')} cancelled.",
                                data={"op": "finish", "item_id": None})
        current = self._rec()["state"]
        if final not in lifecycle.QUEUE_TRANSITIONS.get(current, ()) and current != final:
            lifecycle.queue_transition(self.store, self.qid, "cancelling")
        lifecycle.queue_transition(self.store, self.qid, final)


def _item_log_slices(store: JobStore, chunk_id: str, per_item: int = 200) -> dict[int, list[str]]:
    """Attribute a chunk's log lines to its items by the runner markers (bounded per item)."""
    slices: dict[int, list[str]] = {}
    current = None
    for e in store.iter_entries(chunk_id, 0):
        if e.get("kind") != "line":
            continue
        text = e.get("text") or ""
        m = _MARKER_RE.match(text)
        if m:
            current = int(m.group("local"))
            continue
        if current is not None:
            buf = slices.setdefault(current, [])
            buf.append(text)
            if len(buf) > per_item:
                del buf[0]
    return slices


def resume_all(store: JobStore | None = None, *, policy: lifecycle.LifecyclePolicy | None = None) -> list[str]:
    """Restart the runner of every non-terminal queue (called by ``lifecycle.boot``)."""
    store = _store(store)
    started = []
    for s in store.list(kind="queue", active_only=True, limit=None):
        rec = store.get(s["job_id"])
        if rec is None:
            continue
        for it in rec.get("items") or []:
            if it.get("chunk_id"):
                _CHUNK_PARENT[it["chunk_id"]] = rec["job_id"]
        if start_runner(rec["job_id"], store=store, policy=policy):
            started.append(rec["job_id"])
    return started


# ---- views ------------------------------------------------------------------------------------------------------

def queue_view(rec: dict, store: JobStore | None = None) -> dict:
    """C-4 §7 shape (C-2 §8 deviations)."""
    store = store or default_store()
    items = rec.get("items") or []
    amount = None
    for cid in {it.get("chunk_id") for it in items if it.get("chunk_id")}:
        chunk = store.get(cid)
        v = ((chunk or {}).get("cost") or {}).get("estimate_usd")
        if v is not None:
            amount = (amount or 0.0) + float(v)
    active = next((it["item_id"] for it in items if it.get("status") == "running"), None) or \
        next((it["item_id"] for it in items if it.get("status") == "submitted"), None)
    c = rec.get("cancel") or {}
    return {
        "queue_id": rec["job_id"], "status": rec.get("state"), "paused": bool(rec["queue"].get("paused")),
        "terminal": bool(rec.get("terminal")), "instance_type": rec["queue"].get("instance_type"),
        "created_at": rec.get("created_at"), "updated_at": rec.get("updated_at"),
        "cancel": {"state": c.get("state"), "mode": c.get("mode"), "requested_at": c.get("requested_at"),
                   "note": rec.get("status_note")},
        "items": [{"item_id": it["item_id"], "index": it.get("index"), "label": it.get("label"),
                   "render_repeats": it.get("render_repeats"), "status": it.get("status"),
                   "job_id": it.get("chunk_id"), "outputs_available": bool(it.get("outputs_available")),
                   "error": it.get("error")} for it in items],
        "active_item_id": active, "active_chunk_id": rec["queue"].get("active_chunk_id"),
        "cost": {"amount_usd": None if amount is None else round(amount, 4), "basis": costmod.BASIS},
        "error": ({"code": rec["error"].get("code"), "message": rec["error"].get("message")}
                  if isinstance(rec.get("error"), dict) else None),
        "log_total": store.total_events(rec["job_id"]),
    }

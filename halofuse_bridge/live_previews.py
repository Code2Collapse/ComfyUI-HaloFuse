"""Live preview writer: preview frames of a RUNNING cloud job -> the job's previews/ folder (C-4 section 6a).

PREVIEW_DESIGN sections 1 and 5. The runner sends small re-encoded frames inside its C-3 `preview` log events
(transport T1). `LivePreviewSink.accept(event)` validates one event and writes its bytes to
`<job_dir>/previews/live_<seq>_<kind>_<node>.<ext>` at once, so the browser can load it through the previews route
while the job is still running. The sink owns everything about those files: validation, naming, the per-job rate
limit, the ring of recent frames, the file cap, the metadata index and the cleanup at the end.

Security (THREAT_MODEL, PREVIEW_DESIGN section 5): the file extension and MIME come only from the decoded magic
bytes (JPEG, PNG, WebP; never SVG/HTML/GIF), decoded size <= 256 KiB, and no name, path or MIME from the event is
ever used. Nothing here decodes an image. Frame bytes never enter the job log (lifecycle stores the record only).

`PolledSource` is the second adapter at the same seam (transport T2: the runner's `previews/live/` files read from
the cloud output while the job runs). It stays switched off until probe P2 shows those files are visible during a
run: it starts only when the config key `live_preview_files` is true AND the backend offers
`list_live_previews(cloud_job_id)`. NOT VERIFIED against the real service.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

PREVIEWS_DIR = "previews"
INDEX_FILE = "live_previews.jsonl"
_KINDS = ("latent", "temp_output", "output_thumb")
_NODE_SAFE = re.compile(r"[^A-Za-z0-9-]")
_NAME_RE = re.compile(r"^live_(\d{6,})_([a-z_]+)_([A-Za-z0-9-]{1,24})\.(jpg|png|webp)$")


@dataclass(frozen=True)
class PreviewLimits:
    max_decoded: int = 262144
    ring: int = 120
    max_files: int = 300
    keep_after_finish: int = 24
    rate_per_s: float = 1.0
    burst: int = 6
    # node pictures (temp outputs, masks, output thumbnails) have their own, wider bucket: a burst of fast nodes
    # finishing in one second must not leave their cards empty, and step frames never starve them (or the reverse)
    picture_rate_per_s: float = 4.0
    picture_burst: int = 32


def sniff(data: bytes) -> tuple[str, str] | None:
    """(extension, mime) from the magic bytes, or None for anything that is not JPEG, PNG or WebP."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpg", "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png", "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    return None


def _node_part(node) -> str:
    return (_NODE_SAFE.sub("-", "" if node is None else str(node)) or "x")[:24]


def _int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _text(value, limit: int = 64):
    return value[:limit] if isinstance(value, str) and value else None


class LivePreviewSink:
    """One job's live preview files. Thread-safe; `accept` never raises."""

    def __init__(self, job_dir, *, limits: PreviewLimits | None = None, clock=time.time):
        self.job_dir = Path(job_dir)
        self.dir = self.job_dir / PREVIEWS_DIR
        self.limits = limits or PreviewLimits()
        self.clock = clock
        self._lock = threading.Lock()
        self._records: list[dict] = []
        self._seq = 0
        self._buckets = {"latent": [float(self.limits.burst), None],
                         "picture": [float(self.limits.picture_burst), None]}
        self.stats = {"accepted": 0, "rejected": 0, "rate_dropped": 0, "pruned": 0}
        self._load()

    # -- public ------------------------------------------------------------------------------------------------
    def accept(self, event) -> dict | None:
        try:
            if not isinstance(event, dict) or event.get("t") != "preview" or not isinstance(event.get("data"), str):
                return self._reject()
            raw = event["data"]
            if len(raw) > (self.limits.max_decoded * 4) // 3 + 8:
                return self._reject()
            try:
                data = base64.b64decode(raw, validate=True)
            except (binascii.Error, ValueError):
                return self._reject()
            return self.accept_bytes(data, event)
        except Exception:  # noqa: BLE001 - a bad frame never breaks the job watcher
            return self._reject()

    def accept_bytes(self, data: bytes, meta: dict) -> dict | None:
        if not data or len(data) > self.limits.max_decoded:
            return self._reject()
        sniffed = sniff(data)
        if sniffed is None:
            return self._reject()
        ext, mime = sniffed
        with self._lock:
            kind = meta.get("kind") if meta.get("kind") in _KINDS else "other"
            if not self._take_token("latent" if kind == "latent" else "picture"):
                self.stats["rate_dropped"] += 1
                return None
            if not self._make_room(kind):
                self.stats["rejected"] += 1
                return None
            self._seq += 1
            name = f"live_{self._seq:06d}_{kind}_{_node_part(meta.get('node'))}.{ext}"
            if not self._write(name, data):
                self._seq -= 1
                self.stats["rejected"] += 1
                return None
            rec = {
                "seq": self._seq, "name": name, "kind": kind, "mime": mime, "ts": self.clock(), "size": len(data),
                "node_id": _text(None if meta.get("node") is None else str(meta.get("node"))),
                "prompt_id": _text(meta.get("prompt_id")), "step": _int(meta.get("step")),
                "steps": _int(meta.get("steps")), "mask": meta.get("mask") is True, "w": _int(meta.get("w")),
                "h": _int(meta.get("h")), "method": _text(meta.get("method")), "source": "live",
                "source_kind": _text(meta.get("source_kind")), "frames": _int(meta.get("frames")),
                "runner_seq": _int(meta.get("seq")),
            }
            self._records.append(rec)
            self._append_index(rec)
            self._prune_ring()
            self.stats["accepted"] += 1
            return dict(rec)

    def index(self, since: int = 0, limit: int = 500) -> dict:
        with self._lock:
            page = [dict(r) for r in self._records if r["seq"] >= since][:max(0, int(limit))]
            total = len(self._records)
        nxt = page[-1]["seq"] + 1 if page else since
        return {"previews": page, "next": nxt, "total": total}

    @property
    def max_seq(self) -> int:
        with self._lock:
            return self._seq

    def finish(self) -> None:
        """Terminal cleanup: the last frames, the newest frame of every node and every thumbnail stay."""
        with self._lock:
            latents = [r for r in self._records if r["kind"] == "latent"]
            keep = {r["seq"] for r in latents[-self.limits.keep_after_finish:]} if self.limits.keep_after_finish else set()
            keep |= self._newest_per_node(latents)
            for rec in latents:
                if rec["seq"] not in keep:
                    self._delete(rec)
            self._records = [r for r in self._records if r["kind"] != "latent" or r["seq"] in keep]
            self._rewrite_index()

    # -- internals ---------------------------------------------------------------------------------------------
    def _reject(self):
        with self._lock:
            self.stats["rejected"] += 1
        return None

    def _take_token(self, bucket: str = "latent") -> bool:
        if bucket == "latent":
            burst, rate = float(self.limits.burst), self.limits.rate_per_s
        else:
            burst, rate = float(self.limits.picture_burst), self.limits.picture_rate_per_s
        state = self._buckets[bucket]
        now = self.clock()
        if state[1] is not None:
            state[0] = min(burst, state[0] + (now - state[1]) * rate)
        state[1] = now
        if state[0] >= 1.0:
            state[0] -= 1.0
            return True
        return False

    @staticmethod
    def _newest_per_node(latents) -> set:
        newest = {}
        for rec in latents:
            newest[rec["node_id"]] = rec["seq"]
        return set(newest.values())

    def _prune_ring(self) -> None:
        """The newest `ring` frames plus the newest frame of every node stay; older frames are deleted."""
        latents = [r for r in self._records if r["kind"] == "latent"]
        if len(latents) <= self.limits.ring:
            return
        keep = {r["seq"] for r in latents[-self.limits.ring:]} if self.limits.ring > 0 else set()
        keep |= self._newest_per_node(latents)
        for rec in latents:
            if rec["seq"] not in keep:
                self._delete(rec)
                self._records.remove(rec)

    def _make_room(self, kind: str) -> bool:
        if len(self._records) < self.limits.max_files:
            return True
        latents = [r for r in self._records if r["kind"] == "latent"]
        protected = self._newest_per_node(latents) if kind != "latent" else set()
        for rec in latents:
            if rec["seq"] not in protected:
                self._delete(rec)
                self._records.remove(rec)
                return True
        return False

    def _write(self, name: str, data: bytes) -> bool:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.dir / f".{name}.part"
            tmp.write_bytes(data)
            os.replace(tmp, self.dir / name)
            return True
        except OSError:
            return False

    def _delete(self, rec: dict) -> None:
        try:
            (self.dir / rec["name"]).unlink(missing_ok=True)
            self.stats["pruned"] += 1
        except OSError:
            pass

    def _append_index(self, rec: dict) -> None:
        try:
            with open(self.job_dir / INDEX_FILE, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
        except OSError:
            pass

    def _rewrite_index(self) -> None:
        path = self.job_dir / INDEX_FILE
        tmp = self.job_dir / (INDEX_FILE + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                for rec in self._records:
                    fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
            os.replace(tmp, path)
        except OSError:
            pass

    def _load(self) -> None:
        path = self.job_dir / INDEX_FILE
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        by_seq = {}
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict) or not isinstance(rec.get("name"), str) or not _NAME_RE.match(rec["name"]):
                continue
            seq = _int(rec.get("seq"))
            if seq is None:
                continue
            self._seq = max(self._seq, seq)
            if (self.dir / rec["name"]).is_file():
                by_seq[seq] = rec
        self._records = [by_seq[s] for s in sorted(by_seq)]


# ---- does the job's runtime relay previews at all ----------------------------------------------------------------

UNSUPPORTED_NOTE = ("Live preview needs the HaloFuse cloud runtime. This render used the Original cloud image, which "
                    "sends no live previews.")


def runtime_support(runtime_name) -> dict:
    """{"state": "yes" | "no" | "unknown", "note": str | None} for a runtime contract name (job record `runtime`).

    "halofuse": every HaloFuse runtime image relays previews (runtime/runner/hf/previews.py, since the first image;
    images built from now on also list "live_preview/1" in their node snapshot's runner_capabilities). "legacy": the Original image, built before
    HaloFuse, has no live relay. Any other value (a job submitted before the record kept its runtime, a recovered
    job) is "unknown": the Live tab then shows no runtime label at all."""
    if runtime_name == "legacy":
        return {"state": "no", "note": UNSUPPORTED_NOTE}
    if runtime_name == "halofuse":
        return {"state": "yes", "note": None}
    return {"state": "unknown", "note": None}


# ---- registry ------------------------------------------------------------------------------------------------------

_SINKS: dict[str, LivePreviewSink] = {}
_REG_LOCK = threading.Lock()


def _job_dir(job_id: str, store=None) -> Path:
    if store is None:
        from .job_store import default_store
        store = default_store()
    return store.job_dir(job_id)


def sink_for(job_id: str, store=None) -> LivePreviewSink:
    with _REG_LOCK:
        sink = _SINKS.get(job_id)
        if sink is None:
            sink = _SINKS[job_id] = LivePreviewSink(_job_dir(job_id, store))
        return sink


def peek(job_id: str, store=None) -> LivePreviewSink:
    """The registered sink, or a read-only view loaded from the index (finished jobs, after a restart)."""
    with _REG_LOCK:
        sink = _SINKS.get(job_id)
    return sink if sink is not None else LivePreviewSink(_job_dir(job_id, store))


def finish(job_id: str, store=None) -> None:
    with _REG_LOCK:
        sink = _SINKS.pop(job_id, None)
    if sink is None:
        index = _job_dir(job_id, store) / INDEX_FILE
        if not index.is_file():
            return
        sink = LivePreviewSink(index.parent)
    sink.finish()


def reset_for_tests() -> None:
    with _REG_LOCK:
        _SINKS.clear()


# ---- T2 adapter (off until probe P2) ---------------------------------------------------------------------------------

_RUNNER_LIVE_RE = re.compile(r"^f(\d{6,})_(latent|temp_output|output_thumb)_([A-Za-z0-9-]{1,24})\.(jpg|png|webp)$")


class PolledSource:
    """Reads the runner's `previews/live/` files from the cloud output while the job runs (transport T2).

    `lister()` returns [(name, size, fetch)] where fetch() -> bytes. Every new, well-formed name is fetched once and
    handed to the sink (which re-validates the bytes). Any error backs off from `interval` to `max_interval`."""

    def __init__(self, lister, sink: LivePreviewSink, *, interval: float = 5.0, max_interval: float = 60.0,
                 max_bytes: int = 262144):
        self.lister, self.sink = lister, sink
        self.interval = self.base_interval = interval
        self.max_interval = max_interval
        self.max_bytes = max_bytes
        self._seen: set[str] = set()

    def poll_once(self) -> int:
        try:
            entries = list(self.lister() or [])
        except Exception:  # noqa: BLE001 - storage errors and rate limits: back off to the long interval
            self.interval = self.max_interval
            return 0
        self.interval = self.base_interval
        added = 0
        for name, size, fetch in sorted(entries, key=lambda e: str(e[0])):
            m = _RUNNER_LIVE_RE.match(str(name))
            if not m or name in self._seen or not isinstance(size, int) or size > self.max_bytes:
                continue
            self._seen.add(name)
            try:
                data = fetch()
            except Exception:  # noqa: BLE001
                continue
            if self.sink.accept_bytes(data, {"kind": m.group(2), "node": m.group(3)}) is not None:
                added += 1
        return added


def polling_enabled(settings: dict | None, backend) -> bool:
    return bool((settings or {}).get("live_preview_files")) and callable(getattr(backend, "list_live_previews", None))

"""Live relay (A1, A2, A6, NEW_WORK §6 producer side): ComfyUI websocket messages -> C-3 node/progress/preview events.

- `node` start/done (with duration and a stage heuristic) and `cached`;
- `progress` at most once per second per node (always the final step), with rate/ETA only after two samples;
- latent preview frames: throttled (C-3: <= 1 per 2 s), written to /output/_halofuse/previews/latest_<node>.<ext>
  and sent inline, re-encoded to the byte cap;
- intermediate PreviewImage/PreviewMask (type "temp") outputs copied out of ComfyUI's temp dir as soon as the node
  finishes (they were discarded before), with an inline thumbnail in live mode;
- output thumbnails (images, and a 4-frame sheet for videos) in live mode.
The relay also tracks the currently executing node, which the supervisor uses to attribute a process death.

Live preview (C-3 section 11, PREVIEW_DESIGN sections 2-3): every frame that travels inline is re-encoded to the
HF_PREVIEW_MAX_BYTES cap (hf/frames.py). Latent frames, temp-output/mask thumbnails and output thumbnails each have
their own throttle, so frames never starve masks. Output thumbnails are made on one worker thread. A throttle or a
full worker queue drops (and counts) a preview; nothing here blocks the websocket thread on a slow encode.
"""
import base64
import os
import queue
import re
import threading
import time
from pathlib import Path

from . import events as hf_events
from . import frames as hf_frames
from . import outputs as hf_outputs

LIVE_LONG_EDGE = 640  # owner 2026-11-24: 384 px live frames looked too poor

_EXT = {"image/jpeg": "jpg", "image/png": "png"}
_METHODS = ("latent2rgb", "taesd", "none")
_NODE_SAFE = re.compile(r"[^A-Za-z0-9-]")
_FALLBACK_RE = re.compile(r"TAESD previews enabled, but could not find\s+(\S+)")
# core comfy/sd.py VAE(): a vae_approx decoder file whose weights match no known layout -> no previewer
_DECODER_FILE_RE = re.compile(r"vae_approx[/\\]([A-Za-z0-9_.-]{1,120})")
_NO_WEIGHTS = "No VAE weights detected"
_DECODER_WINDOW = 20   # lines between naming the decoder file and core's "no weights" warning


def stage_of(class_type) -> str:
    name = class_type or ""
    if name.startswith("VAEDecode"):
        return "decode"
    if name.startswith("VAEEncode") or "TextEncode" in name:
        return "encode"
    if "Sampler" in name or name.startswith("SamplerCustom"):
        return "sample"
    if "Loader" in name:
        return "load"
    if name.startswith(("Save", "Preview")):
        return "save"
    return "other"


def node_safe(node) -> str:
    """A node id as a file-name part: `12:5` (subgraph) -> `12-5`, at most 24 characters."""
    return (_NODE_SAFE.sub("-", str(node if node is not None else "")) or "x")[:24]


class TokenBucket:
    """`rate` previews per second with a burst of `burst`; over the rate a preview is dropped and counted."""

    def __init__(self, rate: float, burst: int, clock=time.monotonic):
        self.rate, self.burst, self._clock = float(rate), float(burst), clock
        self._tokens = float(burst)
        self._last = None
        self._dropped = 0
        self._lock = threading.Lock()

    def allow(self, force: bool = False) -> bool:
        """`force`: the preview goes out anyway (a node's first one), without counting a drop."""
        with self._lock:
            now = self._clock()
            if self._last is not None:
                self._tokens = min(self.burst, self._tokens + (now - self._last) * self.rate)
            self._last = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            if force:
                return True
            self._dropped += 1
            return False

    def take_dropped(self) -> int:
        with self._lock:
            n, self._dropped = self._dropped, 0
            return n


class LiveRelay:
    def __init__(self, emitter, *, output_dir, temp_root, previews_enabled, throttle, clock=time.monotonic,
                 progress_interval=1.0, max_bytes=None, preview_method=None, thumb_throttle=None,
                 output_throttle=None, ring_size=30, async_thumbs=True):
        self.em = emitter
        self.output_dir = Path(output_dir)
        self.preview_dir = self.output_dir / hf_outputs.RESERVED_DIR / "previews"
        self.temp_root = Path(temp_root)
        self.previews_enabled = previews_enabled
        self.throttle = throttle
        self.clock = clock
        self.progress_interval = progress_interval
        self._lock = threading.Lock()
        self._prompts = {}          # prompt_id -> API workflow (class_type lookup)
        self._context = {}          # prompt_id -> extra event fields (wf/of)
        self.current = {"prompt_id": None, "node": None, "class_type": None, "since": None}
        self._progress = {}         # (prompt_id, node) -> {"first": (t, v), "last_emit": t}
        self._copied = set()
        self._pending = None
        # live preview (C-3 section 11); every new option defaults from the job env, so the runner's existing
        # constructor call keeps working unchanged
        self.max_bytes = int(max_bytes) if max_bytes else hf_frames.max_bytes_from_env()
        method = preview_method if preview_method is not None else (os.environ.get("HF_PREVIEW_METHOD") or "")
        method = str(method).strip().lower()
        self.preview_method = method if method in _METHODS else None
        self.thumb_throttle = thumb_throttle or TokenBucket(0.5, 4, clock=clock)
        self.output_throttle = output_throttle or hf_events.PreviewThrottle(5.0, clock=clock)
        self.ring_size = int(ring_size)
        self.live_dir = self.preview_dir / "live"
        self._seq_frame = 0
        self._steps = {}            # (prompt_id, node) -> (value, max): latest progress, before the emit throttle
        self._fallback_sent = False
        self._async = bool(async_thumbs)
        self._jobs = queue.Queue(maxsize=16)
        # (prompt_id, node) that already sent a picture: every node's FIRST picture bypasses the throttles, so a
        # burst of fast nodes never leaves a card of the Live tab empty until delivery (owner 2026-11-24)
        self._pictured = set()
        self._worker = None
        self._output_dropped = 0
        self._seen_outputs = set()
        self._cached = {}           # prompt_id -> node ids core served from its cache (execution_cached)
        self._frames = {}           # (prompt_id, node) -> latent frames ComfyUI sent (before the throttle)
        self._no_frames_sent = set()
        self._decoder_seen = None   # (file stem, line number) of the last vae_approx file a line named
        self._decoder_failed = set()
        self._line_no = 0

    # -- registration --------------------------------------------------------------------------------------
    def expect_prompt(self, workflow, **context):
        """Called right before POST /prompt: ComfyUI may start executing (and send websocket messages) before
        the HTTP response with the prompt id arrives, so unknown prompt ids resolve to this pending workflow."""
        with self._lock:
            self._pending = (workflow, {k: v for k, v in context.items() if v is not None})

    def register_prompt(self, prompt_id, workflow, **context):
        with self._lock:
            self._prompts[prompt_id] = workflow
            self._context[prompt_id] = {k: v for k, v in context.items() if v is not None}
            self._pending = None

    def _workflow(self, prompt_id):
        if prompt_id in self._prompts:
            return self._prompts[prompt_id]
        pending = getattr(self, "_pending", None)
        return pending[0] if pending else {}

    def _class_type(self, prompt_id, node):
        node_def = (self._workflow(prompt_id) or {}).get(str(node)) if node is not None else None
        return node_def.get("class_type") if isinstance(node_def, dict) else None

    def _fold(self, prompt_id, node):
        """A node id as the workflow knows it. A node expansion runs ephemeral inner nodes named
        `<root>.<call>.<graph>.<inner>` (GraphBuilder prefix; an H2Vault vault is one): they are reported as their
        workflow node `<root>`, so an inner id, class or title never reaches an event (H2Vault zero-information
        rule; with H2Vault's own message filter installed the ids already arrive folded)."""
        if node is None:
            return None
        node = str(node)
        workflow = self._workflow(prompt_id) or {}
        if node in workflow or "." not in node:
            return node
        root = node.split(".", 1)[0]
        return root if root in workflow else node

    def _ctx(self, prompt_id):
        if prompt_id in self._context:
            return dict(self._context[prompt_id])
        pending = getattr(self, "_pending", None)
        return dict(pending[1]) if pending else {}

    # -- websocket callbacks --------------------------------------------------------------------------------
    def on_json(self, msg):
        kind, data = msg.get("type"), msg.get("data") or {}
        if not isinstance(data, dict):
            return
        prompt_id = data.get("prompt_id")
        if kind == "executing":
            self._on_executing(prompt_id, self._fold(prompt_id, data.get("node")))
        elif kind == "execution_cached":
            # inner ids of an expansion are never listed (folding them would mark the whole vault cached)
            nodes = [str(n) for n in data.get("nodes") or [] if "." not in str(n)]
            with self._lock:
                self._cached.setdefault(prompt_id, set()).update(nodes)
            if nodes:
                self.em.emit("node", prompt_id=prompt_id, **self._ctx(prompt_id), node=None, class_type=None,
                             state="cached", stage="other", nodes=nodes)
        elif kind == "progress":
            self._on_progress(prompt_id, data)
        elif kind == "executed":
            node = self._fold(prompt_id, data.get("node"))
            output = data.get("output")
            if node is not None:
                self._emit_ui(prompt_id, node, output)
            if isinstance(output, dict) and node is not None:
                self.collect_temp_outputs(prompt_id, {str(node): output})
                self._queue_output_thumbs(prompt_id, str(node), output)
        elif kind in ("execution_error", "execution_interrupted", "execution_success"):
            self._on_executing(prompt_id, None)

    def _on_executing(self, prompt_id, node):
        now = self.clock()
        with self._lock:
            prev = dict(self.current)
            if node is None:
                self.current.update(prompt_id=prompt_id, node=None, class_type=None, since=None)
            else:
                self.current.update(prompt_id=prompt_id, node=str(node),
                                    class_type=self._class_type(prompt_id, node), since=now)
        if prev["node"] is not None and prev["node"] != (None if node is None else str(node)):
            self.em.emit("node", prompt_id=prev["prompt_id"], **self._ctx(prev["prompt_id"]), node=prev["node"],
                         class_type=prev["class_type"], state="done", stage=stage_of(prev["class_type"]),
                         dur=round(now - (prev["since"] or now), 3))
            self._mark_no_frames(prev["prompt_id"], prev["node"], prev["class_type"])
        if node is not None and prev["node"] != str(node):
            ct = self._class_type(prompt_id, node)
            self.em.emit("node", prompt_id=prompt_id, **self._ctx(prompt_id), node=str(node), class_type=ct,
                         state="start", stage=stage_of(ct))

    def _on_progress(self, prompt_id, data):
        try:
            value, maximum = int(data.get("value")), int(data.get("max"))
        except (TypeError, ValueError):
            return
        node = self._fold(prompt_id, data.get("node"))
        now = self.clock()
        key = (prompt_id, node)
        with self._lock:
            self._steps[key] = (value, maximum)
            series = self._progress.get(key)
            if series is None or value < series["first"][1]:
                series = {"first": (now, value), "last_emit": None, "samples": 0}
                self._progress[key] = series
            series["samples"] += 1
            due = (value >= maximum or series["last_emit"] is None
                   or now - series["last_emit"] >= self.progress_interval)
            if not due:
                return
            series["last_emit"] = now
            fields = {"node": node, "value": value, "max": maximum}
            t0, v0 = series["first"]
            if series["samples"] >= 2 and value > v0 and now > t0:
                rate = (value - v0) / (now - t0)
                fields["rate"] = round(rate, 3)
                fields["eta_s"] = round(max(0, maximum - value) / rate, 1)
        self.em.emit("progress", prompt_id=prompt_id, **self._ctx(prompt_id), **fields)

    # -- latent frames ----------------------------------------------------------------------------------------
    def _next_frame_seq(self) -> int:
        with self._lock:
            self._seq_frame += 1
            return self._seq_frame

    @staticmethod
    def _write_atomic(path: Path, data: bytes) -> bool:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".part")
            tmp.write_bytes(data)
            tmp.replace(path)
            return True
        except OSError:
            return False

    def _prune_ring(self) -> None:
        try:
            names = sorted(p for p in self.live_dir.iterdir() if p.is_file() and not p.name.endswith(".part"))
        except OSError:
            return
        for old in names[:max(0, len(names) - self.ring_size)]:
            try:
                old.unlink()
            except OSError:
                pass

    def on_preview(self, mime, image, meta):
        if not self.previews_enabled:
            return
        with self._lock:
            pid = meta.get("prompt_id") or self.current["prompt_id"]
            raw_node = meta.get("display_node_id") or meta.get("node_id") or self.current["node"]
            key = (pid, None if raw_node is None else str(self._fold(pid, raw_node)))
            self._frames[key] = self._frames.get(key, 0) + 1
        if not self.throttle.allow():
            return
        dropped = self.throttle.take_dropped()
        with self._lock:
            prompt_id = meta.get("prompt_id") or self.current["prompt_id"]
            node = self._fold(prompt_id, meta.get("display_node_id") or meta.get("node_id") or self.current["node"])
            step = self._steps.get((prompt_id, None if node is None else str(node)))
        ext = _EXT.get(mime, "jpg")
        name = f"latest_{node or 'latent'}.{ext}"
        fields = {"kind": "latent", "node": None if node is None else str(node), "mime": mime, "bytes": len(image)}
        if self._write_atomic(self.preview_dir / name, image):  # full-quality last frame, for delivery
            fields["path"] = f"{hf_outputs.RESERVED_DIR}/previews/{name}"
        fitted = hf_frames.fit(image, mime, self.max_bytes, LIVE_LONG_EDGE)
        if fitted is not None:
            seq_frame = self._next_frame_seq()
            live_name = f"f{seq_frame:06d}_latent_{node_safe(node or 'latent')}.jpg"
            if self._write_atomic(self.live_dir / live_name, fitted.data):
                fields["name"] = live_name
                self._prune_ring()
            fields.update(mime=fitted.mime, bytes=len(fitted.data), data=base64.b64encode(fitted.data).decode("ascii"),
                          w=fitted.w, h=fitted.h, src_bytes=len(image), seq_frame=seq_frame)
        if step is not None:
            fields["step"], fields["steps"] = step
        if self.preview_method:
            fields["method"] = self.preview_method
        fields["dropped"] = dropped
        self.em.emit("preview", prompt_id=prompt_id, **self._ctx(prompt_id), **fields)

    def _mark_no_frames(self, prompt_id, node, class_type) -> None:
        """A sampler node that reported steps but never sent a step preview: the image has no preview decoder (and no
        colour factors) for this model family, or previews are off in core. One `no_step_previews` mark per node, so
        the Live tab says why it stays empty instead of showing nothing."""
        # the method must be known: with --preview-method given through extra args the relay cannot tell "none"
        if not self.previews_enabled or self.preview_method in (None, "none") or node is None:
            return
        if stage_of(class_type) != "sample":
            return  # segmentation, upscale, video loaders... report progress too but never send step previews
        key = (prompt_id, str(node))
        with self._lock:
            stepped = key in self._steps
            framed = self._frames.get(key, 0) > 0
            if not stepped or framed or key in self._no_frames_sent:
                return
            self._no_frames_sent.add(key)
        detail = f"{node} {class_type}" if class_type else str(node)
        self.em.emit("phase", phase="no_step_previews", state="mark", detail=detail[:120])

    def on_comfy_line(self, line) -> None:
        """Call for each ComfyUI output line: a missing preview decoder becomes one `preview_fallback` mark, a decoder
        file core could not load ("No VAE weights detected" right after a line naming vae_approx/<file>) one
        `preview_decoder_failed` mark per file."""
        if not isinstance(line, str):
            return
        failed = None
        with self._lock:  # stdout and stderr are pumped by two threads
            self._line_no += 1
            named = _DECODER_FILE_RE.search(line)
            if named:
                self._decoder_seen = (named.group(1).rsplit(".", 1)[0], self._line_no)
            if _NO_WEIGHTS in line and self._decoder_seen is not None:
                stem, at = self._decoder_seen
                if self._line_no - at <= _DECODER_WINDOW and stem not in self._decoder_failed:
                    self._decoder_failed.add(stem)
                    failed = stem
        if failed:
            self.em.emit("phase", phase="preview_decoder_failed", state="mark", detail=failed[:120])
        if self._fallback_sent:
            return
        m = _FALLBACK_RE.search(line)
        if not m:
            return
        self._fallback_sent = True
        missing = m.group(1).replace("\\", "/").rsplit("/", 1)[-1]
        self.em.emit("phase", phase="preview_fallback", state="mark", detail=missing[:120])

    # -- per-node output summary (C-3 `ui`) ----------------------------------------------------------------------
    def _emit_ui(self, prompt_id, node, output) -> None:
        """One small event per `executed` message, live mode or not: which output keys the node produced, its file
        items (names only: never VHS's `fullpath` or side `workflow` file), up to 3 short `texts`, and whether core
        served it from its cache (core 0.39-0.46 re-send `executed` for cached output nodes). Ids outside the
        workflow are dropped. H2Vault's merged `h2v_previews` travel with their public labels only."""
        workflow = self._workflow(prompt_id) or {}
        if workflow and node not in workflow:
            return
        keys, items, texts = {}, [], []
        for key, value in (output.items() if isinstance(output, dict) else ()):
            entries = value if isinstance(value, (list, tuple)) else [value]
            keys[str(key)[:64]] = len(entries)
            for entry in entries:
                if key == "h2v_previews" and isinstance(entry, dict):
                    if entry.get("kind") == "text" and isinstance(entry.get("text"), str):
                        texts.append(entry["text"])
                        continue
                    item = _file_item("h2v_previews", entry.get("file"))
                    if item is not None:
                        item = {"key": "h2v_previews", "label": str(entry.get("label") or "")[:40],
                                "kind": str(entry.get("kind") or "file")[:16],
                                **{k: v for k, v in item.items() if k != "key"}}
                elif isinstance(entry, str) and key in ("text", "string"):
                    texts.append(entry)
                    continue
                else:
                    item = _file_item(str(key)[:64], entry)
                if item is not None and len(items) < _UI_MAX_ITEMS:
                    items.append(item)
        with self._lock:
            cached = node in self._cached.get(prompt_id, ())
        fields = {"node": node, "cached": cached, "keys": keys, "items": items}
        if texts:
            fields["texts"] = [t[:_UI_MAX_TEXT] for t in texts[:_UI_MAX_TEXTS]]
        self.em.emit("ui", prompt_id=prompt_id, **self._ctx(prompt_id), **fields)

    # -- temp outputs -----------------------------------------------------------------------------------------
    def collect_temp_outputs(self, prompt_id, history_outputs):
        """Copy temp-type outputs once; emit a preview event each (inline thumbnail only when live and allowed)."""
        pending = {}
        for node_id, node_out in (history_outputs or {}).items():
            items = []
            for key, value in (node_out or {}).items():
                if not isinstance(value, list):
                    continue
                fresh = [i for i in value if isinstance(i, dict) and i.get("type") == "temp"
                         and (prompt_id, node_id, i.get("subfolder"), i.get("filename")) not in self._copied]
                if fresh:
                    items.append((key, fresh))
            if items:
                pending[node_id] = {k: v for k, v in items}
        if not pending:
            return []
        copied = hf_outputs.copy_temp_previews(pending, self.temp_root, self.preview_dir, prompt_id)
        for node_id, out in pending.items():
            for value in out.values():
                for item in value:
                    self._copied.add((prompt_id, node_id, item.get("subfolder"), item.get("filename")))
        for path, node_id in copied:
            fields = {"kind": "temp_output", "node": node_id, "mime": _mime_of(path), "bytes": path.stat().st_size,
                      "path": f"{hf_outputs.RESERVED_DIR}/previews/{path.name}"}
            if self.previews_enabled and not fields["mime"] and hf_frames.is_video(path):
                # a temp VIDEO preview (SAM3 Track Preview, VHS Video Combine with save_output off, GIF/WebM):
                # a 4-frame sheet of it travels now, instead of nothing until delivery
                self._queue_thumb(prompt_id, str(node_id), path, "temp_output")
            if self.previews_enabled and fields["mime"]:
                try:
                    raw = path.read_bytes()
                except OSError:
                    raw = b""
                mask = "Mask" in (self._class_type(prompt_id, node_id) or "") or hf_frames.is_grey(raw)
                fields["mask"] = bool(mask)
                if raw and self.thumb_throttle.allow(force=self._first_picture(prompt_id, node_id)):
                    fitted = hf_frames.fit(raw, fields["mime"], self.max_bytes, LIVE_LONG_EDGE, mask=mask)
                    if fitted is not None:
                        fields.update(mime=fitted.mime, bytes=len(fitted.data), w=fitted.w, h=fitted.h,
                                      data=base64.b64encode(fitted.data).decode("ascii"), src_bytes=len(raw),
                                      seq_frame=self._next_frame_seq())
                fields["dropped"] = self.thumb_throttle.take_dropped()
            self.em.emit("preview", prompt_id=prompt_id, **self._ctx(prompt_id),
                         **{k: v for k, v in fields.items() if v is not None})
        return copied

    # -- output thumbnails ------------------------------------------------------------------------------------
    def _output_path(self, item):
        filename, subfolder = item.get("filename"), item.get("subfolder") or ""
        if not isinstance(filename, str) or not filename or not isinstance(subfolder, str):
            return None
        try:
            base = self.output_dir.resolve()
            path = (self.output_dir / subfolder / filename).resolve()
        except OSError:
            return None
        if base not in path.parents or not path.is_file():
            return None
        return path

    def _queue_output_thumbs(self, prompt_id, node_id, output) -> None:
        if not self.previews_enabled:
            return
        for value in output.values():
            if not isinstance(value, list):
                continue
            for item in value:
                if not isinstance(item, dict) or item.get("type") != "output":
                    continue
                key = (prompt_id, node_id, item.get("subfolder"), item.get("filename"))
                if key in self._seen_outputs:
                    continue
                self._seen_outputs.add(key)
                path = self._output_path(item)
                if path is None or not (hf_frames.is_video(path) or hf_frames.is_image(path)):
                    continue
                if not self.output_throttle.allow(force=self._first_picture(prompt_id, node_id)):
                    continue
                self._queue_thumb(prompt_id, node_id, path, "output_thumb")

    def _first_picture(self, prompt_id, node_id) -> bool:
        """True the first time a node sends a picture in this prompt."""
        key = (prompt_id, str(node_id))
        with self._lock:
            if key in self._pictured:
                return False
            self._pictured.add(key)
            return True

    def _queue_thumb(self, prompt_id, node_id, path, kind) -> None:
        if not self._async:
            self._output_thumb(prompt_id, node_id, path, kind)
            return
        self._ensure_worker()
        try:
            self._jobs.put_nowait((prompt_id, node_id, path, kind))
        except queue.Full:
            with self._lock:
                self._output_dropped += 1

    def _ensure_worker(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._work, daemon=True, name="hf-output-thumbs")
        self._worker.start()

    def _work(self) -> None:
        while True:
            job = self._jobs.get()
            try:
                self._output_thumb(*job)
            except Exception:  # noqa: BLE001 - a thumbnail failure never stops the relay
                pass

    def _output_thumb(self, prompt_id, node_id, path, kind="output_thumb") -> None:
        video = hf_frames.is_video(path)
        fitted = (hf_frames.video_sheet(path, self.max_bytes, 512) if video
                  else hf_frames.image_thumb(path, self.max_bytes, LIVE_LONG_EDGE))
        if fitted is None:
            return
        with self._lock:
            dropped = self.output_throttle.take_dropped() + self._output_dropped
            self._output_dropped = 0
        try:
            src_bytes = path.stat().st_size
        except OSError:
            src_bytes = None
        fields = {"kind": kind, "node": node_id, "mime": fitted.mime, "bytes": len(fitted.data),
                  "data": base64.b64encode(fitted.data).decode("ascii"), "w": fitted.w, "h": fitted.h,
                  "source_kind": "video" if video else "image", "src_bytes": src_bytes,
                  "seq_frame": self._next_frame_seq(), "dropped": dropped}
        if video:
            fields["frames"] = 4
        self.em.emit("preview", prompt_id=prompt_id, **self._ctx(prompt_id),
                     **{k: v for k, v in fields.items() if v is not None})


_UI_MAX_ITEMS = 32
_UI_MAX_TEXTS = 3
_UI_MAX_TEXT = 500
_UI_FILE_FIELDS = ("filename", "subfolder", "type", "format")


def _file_item(key, entry):
    """{key, filename, subfolder, type, format} from one ComfyUI output entry, or None when it names no file."""
    if not isinstance(entry, dict) or not isinstance(entry.get("filename"), str) or not entry["filename"]:
        return None
    item = {"key": key}
    for field in _UI_FILE_FIELDS:
        value = entry.get(field)
        if isinstance(value, str):
            item[field] = value[:255]
    item.setdefault("subfolder", "")
    return item


def _mime_of(path):
    ext = Path(path).suffix.lower()
    return {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(ext)

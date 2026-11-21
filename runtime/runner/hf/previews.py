"""Live relay (A1, A2, A6, NEW_WORK §6 producer side): ComfyUI websocket messages -> C-3 node/progress/preview events.

- `node` start/done (with duration and a stage heuristic) and `cached`;
- `progress` at most once per second per node (always the final step), with rate/ETA only after two samples;
- latent preview frames: throttled (C-3: <= 1 per 2 s), written to /output/_halofuse/previews/latest_<node>.<ext>
  and sent inline when the line fits;
- intermediate PreviewImage/PreviewMask (type "temp") outputs copied out of ComfyUI's temp dir as soon as the node
  finishes (they were discarded before).
The relay also tracks the currently executing node, which the supervisor uses to attribute a process death.
"""
import base64
import threading
import time
from pathlib import Path

from . import outputs as hf_outputs

_EXT = {"image/jpeg": "jpg", "image/png": "png"}


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


class LiveRelay:
    def __init__(self, emitter, *, output_dir, temp_root, previews_enabled, throttle, clock=time.monotonic,
                 progress_interval=1.0):
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
            self._on_executing(prompt_id, data.get("node"))
        elif kind == "execution_cached":
            nodes = [str(n) for n in data.get("nodes") or []]
            if nodes:
                self.em.emit("node", prompt_id=prompt_id, **self._ctx(prompt_id), node=None, class_type=None,
                             state="cached", stage="other", nodes=nodes)
        elif kind == "progress":
            self._on_progress(prompt_id, data)
        elif kind == "executed":
            node = data.get("node")
            output = data.get("output")
            if isinstance(output, dict) and node is not None:
                self.collect_temp_outputs(prompt_id, {str(node): output})
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
        if node is not None and prev["node"] != str(node):
            ct = self._class_type(prompt_id, node)
            self.em.emit("node", prompt_id=prompt_id, **self._ctx(prompt_id), node=str(node), class_type=ct,
                         state="start", stage=stage_of(ct))

    def _on_progress(self, prompt_id, data):
        try:
            value, maximum = int(data.get("value")), int(data.get("max"))
        except (TypeError, ValueError):
            return
        node = None if data.get("node") is None else str(data.get("node"))
        now = self.clock()
        key = (prompt_id, node)
        with self._lock:
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

    def on_preview(self, mime, image, meta):
        if not self.previews_enabled:
            return
        if not self.throttle.allow():
            return
        dropped = self.throttle.take_dropped()
        with self._lock:
            node = meta.get("node_id") or meta.get("display_node_id") or self.current["node"]
            prompt_id = meta.get("prompt_id") or self.current["prompt_id"]
        ext = _EXT.get(mime, "jpg")
        rel = None
        try:
            self.preview_dir.mkdir(parents=True, exist_ok=True)
            name = f"latest_{node or 'latent'}.{ext}"
            tmp = self.preview_dir / (name + ".part")
            tmp.write_bytes(image)
            tmp.replace(self.preview_dir / name)
            rel = f"{hf_outputs.RESERVED_DIR}/previews/{name}"
        except OSError:
            pass
        fields = {"kind": "latent", "node": None if node is None else str(node), "mime": mime, "bytes": len(image)}
        if rel:
            fields["path"] = rel
        fields["data"] = base64.b64encode(image).decode("ascii")
        fields["dropped"] = dropped
        self.em.emit("preview", prompt_id=prompt_id, **self._ctx(prompt_id), **fields)

    # -- temp outputs -----------------------------------------------------------------------------------------
    def collect_temp_outputs(self, prompt_id, history_outputs):
        """Copy temp-type outputs once; emit a preview event each (inline data only within the throttle)."""
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
            if self.previews_enabled and fields["mime"] and self.throttle.allow():
                fields["data"] = base64.b64encode(path.read_bytes()).decode("ascii")
                fields["dropped"] = self.throttle.take_dropped()
            self.em.emit("preview", prompt_id=prompt_id, **self._ctx(prompt_id),
                         **{k: v for k, v in fields.items() if v is not None})
        return copied


def _mime_of(path):
    ext = Path(path).suffix.lower()
    return {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(ext)

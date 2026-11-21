"""Quick Draft (NEW_WORK 6.3) and the opt-in latent checkpoint transform (F2), both on a cloned workflow.

Every transform here is deterministic (same input -> same output), never mutates its input, reports every change it
made as `{node_id, input, from, to}` (or an added node), lists what it recognised but did not change and why, and can
be reverted exactly with `revert()`. Nothing here claims anything about image quality: a draft is a cheaper preview
variant, not a promise of a similar result.

What a widget means is decided from node definitions (contract C-5 snapshot first, local ComfyUI second), never from a
per-model table, so the rules work for every model family that follows ComfyUI's own conventions:

* resolution - INT `width` + `height` on a node that outputs LATENT: scaled together so the long side is at most
  `max_side`, keeping each value on the widget's own step grid (value = original mod step) and above its minimum;
* frames - INT `length` / `num_frames` / `frames` / `video_length` on a node that outputs LATENT: reduced to at most
  `frames`, on the same step grid (so a 4n+1 or 17k+5 length stays valid);
* batch size - INT `batch_size` on a node that outputs LATENT: reduced to at most `batch_size`;
* steps - INT `steps` on a node that outputs LATENT or SIGMAS: reduced to at most `steps`; step-range widgets of the
  same node (`start_at_step`/`end_at_step`, `start_step`/`end_step`) are scaled by the same ratio so multi-stage
  samplers keep their hand-over point, or the node is reported unsupported when that is not possible.

Seeds, cfg, denoise, prompts, models and every other widget are never touched.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

from .workflow_analysis import NodeDefs, Snapshot, _class_types, _default_snapshot, _nodes, local_node_defs

DEFAULT_OPTIONS = {"max_side": 768, "steps": 8, "batch_size": 1, "frames": 17}
_LIMITS = {"max_side": (64, 16384), "steps": (1, 10000), "batch_size": (1, 4096), "frames": (1, 100000)}
FRAME_WIDGETS = ("length", "num_frames", "frames", "video_length")
RANGE_WIDGETS = (("start_at_step", "end_at_step"), ("start_step", "end_step"))
RECOGNISED = ("width", "height", "batch_size", "steps") + FRAME_WIDGETS

# Decode nodes whose `samples` input is ComfyUI's core LATENT dict, which SaveLatent / LoadLatent round-trip
# (verified in ComfyUI 0.39.0 nodes.py: VAEDecode / VAEDecodeTiled take ("LATENT",), SaveLatent takes "samples"
# LATENT + "filename_prefix" STRING and is an OUTPUT_NODE).
CORE_DECODE_NODES = ("VAEDecode", "VAEDecodeTiled")
LATENT_PREFIX = "halofuse_latents/node_"


@dataclass
class DraftResult:
    draft_workflow: dict
    changes: list = field(default_factory=list)       # [{node_id, input, from, to}]
    unsupported: list = field(default_factory=list)   # [{node_id, class_type, input, reason}]
    options: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"draft_workflow": self.draft_workflow, "changes": self.changes, "unsupported": self.unsupported,
                "options": self.options}


def validate_options(options: dict | None) -> dict:
    """Merge *options* over DEFAULT_OPTIONS. A key set to None disables that reduction. Raises ValueError."""
    merged = dict(DEFAULT_OPTIONS)
    for key, value in (options or {}).items():
        if key not in DEFAULT_OPTIONS:
            raise ValueError(f"unknown draft option '{key}'")
        if value is None:
            merged[key] = None
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"draft option '{key}' must be a whole number")
        lo, hi = _LIMITS[key]
        if not lo <= value <= hi:
            raise ValueError(f"draft option '{key}' must be between {lo} and {hi}")
        merged[key] = value
    return merged


def _defs_for(workflow: dict, node_defs: NodeDefs | None, snapshot) -> NodeDefs:
    if node_defs is not None:
        return node_defs
    snap = _default_snapshot() if snapshot == "default" else snapshot
    return NodeDefs(local_node_defs(_class_types(workflow)), snap if isinstance(snap, Snapshot) else None)


def _outputs(defs: NodeDefs, class_type: str) -> list:
    node_def = defs.cloud_def(class_type) or defs.node_def(class_type) or {}
    return [o for o in node_def.get("output") or [] if isinstance(o, str)]


def _int_spec(defs: NodeDefs, class_type: str, name: str):
    spec = defs.specs(class_type).get(name)
    return spec if spec is not None and spec.kind == "INT" else None


def _on_grid_at_most(original: int, target: float, step: int, minimum: int) -> int:
    """Largest v <= target with v = original (mod step) and v >= minimum; falls back to the smallest such v."""
    step = max(1, int(step))
    if target >= original:
        return original
    k = -(-(original - target) // step)  # ceil
    value = int(original - k * step)
    if value < minimum:
        k_max = (original - minimum) // step
        value = int(original - max(0, k_max) * step)
    return value


def _step(spec, default: int = 1) -> int:
    step = spec.extra.get("step") if spec else None
    return int(step) if isinstance(step, (int, float)) and step >= 1 else default


def _minimum(spec, default: int = 1) -> int:
    lo = spec.extra.get("min") if spec else None
    return int(lo) if isinstance(lo, (int, float)) else default


def make_draft(workflow: dict, options: dict | None = None, *, node_defs: NodeDefs | None = None,
               snapshot="default") -> DraftResult:
    """Quick Draft: a cheaper clone of *workflow* (the input is never modified)."""
    opts = validate_options(options)
    draft = copy.deepcopy(workflow)
    result = DraftResult(draft, options=opts)
    if not isinstance(workflow, dict):
        raise ValueError("the workflow is not an API-format graph")
    defs = _defs_for(workflow, node_defs, snapshot)

    for node_id, node in sorted(_nodes(draft), key=lambda item: _sort_key(item[0])):
        class_type, inputs = node["class_type"], node["inputs"]
        present = [n for n in RECOGNISED if n in inputs]
        if not present:
            continue
        if defs.node_def(class_type) is None:
            for name in present:
                result.unsupported.append(_unsupported(node_id, class_type, name, "no node definition is available, "
                                                                                  "so the widget's meaning is unknown"))
            continue
        outputs = _outputs(defs, class_type)
        makes_latent = "LATENT" in outputs
        for name in present:
            if isinstance(inputs[name], list):
                result.unsupported.append(_unsupported(node_id, class_type, name, "the value comes from another node"))
        if makes_latent:
            _draft_resolution(node_id, class_type, inputs, opts, defs, result)
            _draft_frames(node_id, class_type, inputs, opts, defs, result)
            _draft_simple(node_id, class_type, inputs, "batch_size", opts["batch_size"], defs, result)
        else:
            for name in present:
                if name in ("width", "height", "batch_size") + FRAME_WIDGETS and not isinstance(inputs[name], list):
                    result.unsupported.append(_unsupported(
                        node_id, class_type, name, "not a latent-producing node; changing it alone could make the "
                                                   "graph inconsistent"))
        if "steps" in present and not isinstance(inputs["steps"], list):
            if makes_latent or "SIGMAS" in outputs:
                _draft_steps(node_id, class_type, inputs, opts, defs, result)
            else:
                result.unsupported.append(_unsupported(node_id, class_type, "steps",
                                                       "not a sampler or scheduler node"))
    return result


def _sort_key(node_id: str):
    return (0, int(node_id), "") if node_id.isdigit() else (1, 0, node_id)


def _unsupported(node_id, class_type, name, reason) -> dict:
    return {"node_id": node_id, "class_type": class_type, "input": name, "reason": reason}


def _set(result: DraftResult, node_id: str, inputs: dict, name: str, value) -> None:
    if inputs[name] != value:
        result.changes.append({"node_id": node_id, "input": name, "from": inputs[name], "to": value})
        inputs[name] = value


def _literal_int(inputs: dict, name: str) -> bool:
    return isinstance(inputs.get(name), int) and not isinstance(inputs.get(name), bool)


def _draft_resolution(node_id, class_type, inputs, opts, defs, result) -> None:
    max_side = opts["max_side"]
    if max_side is None or not (_literal_int(inputs, "width") and _literal_int(inputs, "height")):
        return
    w, h = inputs["width"], inputs["height"]
    w_spec, h_spec = _int_spec(defs, class_type, "width"), _int_spec(defs, class_type, "height")
    if w_spec is None or h_spec is None:
        return
    long_side = max(w, h)
    if long_side <= max_side:
        return
    factor = max_side / long_side
    _set(result, node_id, inputs, "width", _on_grid_at_most(w, w * factor, _step(w_spec), _minimum(w_spec)))
    _set(result, node_id, inputs, "height", _on_grid_at_most(h, h * factor, _step(h_spec), _minimum(h_spec)))


def _draft_frames(node_id, class_type, inputs, opts, defs, result) -> None:
    target = opts["frames"]
    if target is None:
        return
    for name in FRAME_WIDGETS:
        spec = _int_spec(defs, class_type, name)
        if spec is None or not _literal_int(inputs, name):
            continue
        _set(result, node_id, inputs, name, _on_grid_at_most(inputs[name], target, _step(spec), _minimum(spec)))


def _draft_simple(node_id, class_type, inputs, name, target, defs, result) -> None:
    spec = _int_spec(defs, class_type, name)
    if target is None or spec is None or not _literal_int(inputs, name):
        return
    _set(result, node_id, inputs, name, max(_minimum(spec), min(inputs[name], target)))


def _draft_steps(node_id, class_type, inputs, opts, defs, result) -> None:
    target = opts["steps"]
    spec = _int_spec(defs, class_type, "steps")
    if target is None or spec is None or not _literal_int(inputs, "steps") or inputs["steps"] <= target:
        return
    old = inputs["steps"]
    new = max(_minimum(spec), target)
    ranges = [(a, b) for a, b in RANGE_WIDGETS if a in inputs or b in inputs]
    planned = {"steps": new}
    for start_name, end_name in ranges:
        start, end = inputs.get(start_name, 0), inputs.get(end_name, old)
        if not (isinstance(start, int) and isinstance(end, int)) or isinstance(start, bool) or isinstance(end, bool):
            result.unsupported.append(_unsupported(node_id, class_type, "steps",
                                                   "the step range comes from another node"))
            return
        new_start = round(start * new / old)
        new_end = end if end >= old else round(end * new / old)
        if not (0 <= new_start < new and new_start < new_end):
            result.unsupported.append(_unsupported(
                node_id, class_type, "steps", f"{new} steps cannot keep the {start_name}..{end_name} hand-over"))
            return
        if start_name in inputs:
            planned[start_name] = new_start
        if end_name in inputs:
            planned[end_name] = new_end
    for name, value in planned.items():
        _set(result, node_id, inputs, name, value)


def revert(draft_workflow: dict, changes: list) -> dict:
    """Undo *changes* (from make_draft or insert_latent_checkpoints) on a copy of *draft_workflow*."""
    restored = copy.deepcopy(draft_workflow)
    for change in reversed(changes):
        if change.get("op") == "add_node":
            restored.pop(change["node_id"], None)
        else:
            restored[change["node_id"]]["inputs"][change["input"]] = copy.deepcopy(change["from"])
    return restored


# ---- F2: latent checkpoint before decode (opt-in) --------------------------------------------------------------

@dataclass
class LatentCheckpointResult:
    workflow: dict
    changes: list = field(default_factory=list)       # [{"op": "add_node", "node_id", "node", "before"}]
    latent_map: dict = field(default_factory=dict)    # decode node id -> SaveLatent filename_prefix
    unsupported: list = field(default_factory=list)
    problems: list = field(default_factory=list)      # validation against the snapshot

    def to_dict(self) -> dict:
        return {"workflow": self.workflow, "changes": self.changes, "latent_map": self.latent_map,
                "unsupported": self.unsupported, "problems": self.problems}


def insert_latent_checkpoints(workflow: dict, *, node_defs: NodeDefs | None = None,
                              snapshot="default") -> LatentCheckpointResult:
    """Add a SaveLatent next to every core VAE decode, fed by the same latent, so a decode failure after a long
    sampling run leaves the sampled latent in the job outputs (`halofuse_latents/node_<decode id>_00001_.latent`)
    for the runner's decode-from-latent recovery (C-3 recovery `decode_from_latent`) or a later local decode.

    Opt-in and reversible (`revert(result.workflow, result.changes)`); the original is not modified. Pack-specific
    decoders (a LATENT input and an IMAGE output, e.g. WanVideoDecode) are listed as unsupported: their latent dicts
    can carry keys SaveLatent does not keep. The transformed graph is validated against the snapshot (SaveLatent must
    exist in the runtime)."""
    if not isinstance(workflow, dict):
        raise ValueError("the workflow is not an API-format graph")
    defs = _defs_for(workflow, node_defs, snapshot)
    out = copy.deepcopy(workflow)
    result = LatentCheckpointResult(out)
    numeric = [int(k) for k in workflow if str(k).isdigit()]
    next_id = (max(numeric) + 1) if numeric else 1
    for node_id, node in sorted(_nodes(workflow), key=lambda item: _sort_key(item[0])):
        class_type, inputs = node["class_type"], node["inputs"]
        if class_type in CORE_DECODE_NODES:
            samples = inputs.get("samples")
            if not (isinstance(samples, list) and len(samples) == 2):
                result.unsupported.append(_unsupported(node_id, class_type, "samples", "the latent is not linked"))
                continue
            while str(next_id) in out:
                next_id += 1
            new_id = str(next_id)
            next_id += 1
            prefix = f"{LATENT_PREFIX}{node_id}"
            new_node = {"class_type": "SaveLatent", "inputs": {"samples": list(samples), "filename_prefix": prefix},
                        "_meta": {"title": f"HaloFuse latent checkpoint (before node {node_id})"}}
            out[new_id] = new_node
            result.changes.append({"op": "add_node", "node_id": new_id, "node": copy.deepcopy(new_node),
                                   "before": node_id})
            result.latent_map[node_id] = prefix
            continue
        specs = defs.specs(class_type)
        if any(s.kind == "LATENT" for s in specs.values()) and "IMAGE" in _outputs(defs, class_type):
            result.unsupported.append(_unsupported(node_id, class_type, None,
                                                   "not a core decode node; its latent format is pack-specific"))
    snap = defs.snapshot
    if result.changes and snap is not None:
        status, _ = snap.class_status("SaveLatent")
        if status != "known":
            result.problems.append({"code": "node_unavailable",
                                    "message": f"SaveLatent is not available in the '{snap.profile}' runtime"})
    return result

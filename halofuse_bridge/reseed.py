"""New seeds for "Stop and try a new seed" (PREVIEW_DESIGN section 4.3, C-4 section 6a).

Only inputs that are certainly seeds change: a literal integer (not a link, not a bool) whose node definition marks
the input with `control_after_generate` (ComfyUI's own seed widget flag in the object_info input options). Every
other value stays exactly as it was (NEW_WORK section 6: never blindly replace numeric widget values). Nodes without
a definition are reported, never guessed.
"""
from __future__ import annotations

import copy
import random
from dataclasses import dataclass, field

MAX_SAFE_INT = 2 ** 53 - 1  # the browser keeps widget values as JS numbers


@dataclass
class ReseedResult:
    workflow: dict
    changes: list = field(default_factory=list)
    skipped: list = field(default_factory=list)


def _input_specs(node_def) -> dict:
    inputs = (node_def or {}).get("input") or {}
    specs = {}
    for section in ("required", "optional"):
        part = inputs.get(section) or {}
        if isinstance(part, dict):
            specs.update(part)
    return specs


def _seed_range(spec):
    """(lo, hi) when the input spec is a seed widget, else None."""
    if not isinstance(spec, (list, tuple)) or len(spec) < 2 or spec[0] != "INT" or not isinstance(spec[1], dict):
        return None
    opts = spec[1]
    if not opts.get("control_after_generate"):
        return None
    lo = opts.get("min", 0)
    hi = opts.get("max", MAX_SAFE_INT)
    lo = lo if isinstance(lo, int) and not isinstance(lo, bool) else 0
    hi = hi if isinstance(hi, int) and not isinstance(hi, bool) else MAX_SAFE_INT
    lo, hi = max(lo, -MAX_SAFE_INT), min(hi, MAX_SAFE_INT)
    return (lo, hi) if hi > lo else None


def _order(node_id: str):
    return (0, int(node_id), "") if str(node_id).isdigit() else (1, 0, str(node_id))


def reseed(workflow: dict, node_defs: dict, rng=None) -> ReseedResult:
    """A deep copy of *workflow* with new values for its seed inputs, plus the exact list of changes."""
    rng = rng or random.SystemRandom()
    out = copy.deepcopy(workflow)
    result = ReseedResult(out)
    for node_id in sorted(out, key=_order):
        node = out[node_id]
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            continue
        class_type = node.get("class_type")
        node_def = node_defs.get(class_type) if isinstance(class_type, str) else None
        literal_ints = {k: v for k, v in node["inputs"].items() if isinstance(v, int) and not isinstance(v, bool)}
        if node_def is None:
            if literal_ints:
                result.skipped.append({"node_id": str(node_id), "class_type": class_type,
                                       "reason": "node definition not available"})
            continue
        specs = _input_specs(node_def)
        for name, value in literal_ints.items():
            bounds = _seed_range(specs.get(name))
            if bounds is None:
                continue
            new = value
            for _ in range(16):
                new = rng.randint(*bounds)
                if new != value:
                    break
            if new == value:
                continue
            node["inputs"][name] = new
            result.changes.append({"node_id": str(node_id), "class_type": class_type, "input": name,
                                   "from": value, "to": new})
    return result

"""Opt-in auto-preview of nodes without a preview (owner addition 4, 2026-11-23; NODE_PREVIEWS_DESIGN section 2).

`apply(workflow, mode, defs)` returns a COPY of the API workflow with small preview chains added for IMAGE / MASK
outputs that no output node shows: `[MaskToImage ->] ImageFromBatch(one frame) -> PreviewImage`. Only the copy the
cloud runs changes; the artist's graph (and the dict the panel sent) is never touched. All three classes are ComfyUI
core nodes, verified in core 0.39.0 and 0.46.0 (`ImageFromBatch` clamps the index and accepts -1 for the last frame).

Rules: a candidate output must already run (its node is upstream of an output node: previewing a dangling branch
would add compute), must feed no output node directly (it already has a preview), and its node is never an H2Vault
node (a vault shows only what its author allows). At most `cap` previews, in graph order. One frame per preview,
because an IMAGE batch (a video) is float32 in host RAM; every-Nth sampling needs a runtime node and is not offered.
"""
from __future__ import annotations

import copy

from . import node_board

MODES = ("off", "first", "last", "first_last")
DEFAULT_CAP = 8
_INDEX = {"first": (0,), "last": (-1,), "first_last": (0, -1)}


def _outputs(lookup, class_type) -> list:
    d = lookup(class_type) or {}
    outs = d.get("output") or []
    return list(outs) if isinstance(outs, (list, tuple)) else []


def candidates(workflow: dict, defs) -> list[tuple[str, int, str]]:
    """[(node id, output slot, 'IMAGE'|'MASK')] in graph order."""
    lookup = node_board._lookup(defs)
    nodes = {str(k): v for k, v in workflow.items() if isinstance(v, dict)}
    outputs = {k for k, n in nodes.items() if node_board.is_output_node(n.get("class_type"), lookup)
               and not node_board.is_vault(n.get("class_type"))}
    shown = set()                                      # (node, slot) feeding an output node directly
    for k in outputs:
        for value in (nodes[k].get("inputs") or {}).values():
            if node_board._is_link(value):
                shown.add((str(value[0]), value[1]))
    running = set()                                    # everything upstream of an output node
    for k in outputs:
        running.update(node_board_ancestors(nodes, k))
    out = []
    for k in node_board.graph_order(nodes):
        node = nodes[k]
        ct = node.get("class_type")
        if k in outputs or k not in running or node_board.is_vault(ct):
            continue
        meta = node.get("_meta") if isinstance(node.get("_meta"), dict) else {}
        if meta.get(node_board.AUTO_PREVIEW_KEY):
            continue
        for slot, t in enumerate(_outputs(lookup, ct)):
            if t in ("IMAGE", "MASK") and (k, slot) not in shown:
                out.append((k, slot, t))
    return out


def node_board_ancestors(nodes: dict, node_id: str) -> set:
    seen, stack = set(), [node_id]
    while stack:
        nid = stack.pop()
        if nid in seen or nid not in nodes:
            continue
        seen.add(nid)
        for value in (nodes[nid].get("inputs") or {}).values():
            if node_board._is_link(value):
                stack.append(str(value[0]))
    return seen


def apply(workflow: dict, mode: str, defs, *, cap: int = DEFAULT_CAP) -> tuple[dict, list[dict]]:
    """(cloud copy, [{node_id, slot, type, frame, preview_id}]). mode 'off' returns the workflow unchanged."""
    if mode not in MODES:
        raise ValueError(f"auto preview must be one of {', '.join(MODES)}")
    if mode == "off":
        return workflow, []
    out = copy.deepcopy(workflow)
    added = []
    for node_id, slot, t in candidates(workflow, defs):
        for index in _INDEX[mode]:
            if len(added) >= cap:
                return out, added
            tag = f"hf_ap_{node_id.replace(':', '-')}_{slot}_{'last' if index < 0 else 'first'}"
            title = node_board.node_title(workflow, node_id)
            meta = {node_board.AUTO_PREVIEW_KEY: node_id}
            source = [node_id, slot]
            if t == "MASK":
                out[f"{tag}_m"] = {"class_type": "MaskToImage", "inputs": {"mask": source},
                                   "_meta": {"title": f"HaloFuse mask of {title}", **meta}}
                source = [f"{tag}_m", 0]
            out[f"{tag}_f"] = {"class_type": "ImageFromBatch", "inputs": {"image": source, "batch_index": index,
                                                                         "length": 1},
                               "_meta": {"title": f"HaloFuse frame of {title}", **meta}}
            out[f"{tag}_p"] = {"class_type": "PreviewImage", "inputs": {"images": [f"{tag}_f", 0]},
                               "_meta": {"title": f"HaloFuse preview of {title}", **meta}}
            added.append({"node_id": node_id, "slot": slot, "type": t, "frame": "last" if index < 0 else "first",
                          "preview_id": f"{tag}_p"})
    return out, added

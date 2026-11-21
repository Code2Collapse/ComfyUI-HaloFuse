"""Decode-stage recovery transforms (F2, F4) on API-format prompts. Pure functions; inputs are never mutated.

Verified node signatures (ComfyUI 0.30.1 and 0.39.0 nodes.py): VAEDecodeTiled(samples, vae, tile_size=512,
overlap=64, temporal_size=64 [video VAEs: frames per chunk], temporal_overlap=8); LoadLatent(latent = a `.latent`
file name in the input directory); SaveLatent(samples, filename_prefix).

These change only HOW a latent is decoded (tiles / temporal chunks), never resolution, frame count, steps or seeds.
"""
import copy
from pathlib import Path

DECODE_CLASSES = ("VAEDecode", "VAEDecodeTiled")
LOAD_LATENT_ID = "hf_latent"
_MIN_TILE, _MIN_TEMPORAL = 64, 8


def _is_link(value) -> bool:
    return isinstance(value, list) and len(value) == 2 and isinstance(value[0], str)


def tile_decode(workflow, node_id, tile_size=512, overlap=64, temporal_size=64, temporal_overlap=8):
    node = workflow.get(node_id)
    if not isinstance(node, dict) or node.get("class_type") not in DECODE_CLASSES:
        raise ValueError(f"node {node_id} is not a VAE decode node")
    out = copy.deepcopy(workflow)
    target = out[node_id]
    inputs = target.setdefault("inputs", {})
    if target["class_type"] == "VAEDecode":
        target["class_type"] = "VAEDecodeTiled"
        inputs.update(tile_size=tile_size, overlap=overlap, temporal_size=temporal_size,
                      temporal_overlap=temporal_overlap)
        return out
    tile = inputs.get("tile_size", 512)
    temporal = inputs.get("temporal_size", 64)
    new_tile = max(_MIN_TILE, int(tile) // 2) if isinstance(tile, int) else tile
    new_temporal = max(_MIN_TEMPORAL, int(temporal) // 2) if isinstance(temporal, int) else temporal
    if new_tile == tile and new_temporal == temporal:
        return None
    inputs["tile_size"] = new_tile
    inputs["temporal_size"] = new_temporal
    if isinstance(inputs.get("overlap"), int) and inputs["overlap"] >= new_tile:
        inputs["overlap"] = new_tile // 4
    if isinstance(inputs.get("temporal_overlap"), int) and inputs["temporal_overlap"] >= new_temporal:
        inputs["temporal_overlap"] = max(4, new_temporal // 4)
    return out


def saved_latent_nodes(workflow, decode_node_id) -> list:
    decode = workflow.get(decode_node_id) or {}
    source = (decode.get("inputs") or {}).get("samples")
    if not _is_link(source):
        return []
    return sorted(nid for nid, node in workflow.items()
                  if isinstance(node, dict) and node.get("class_type") == "SaveLatent"
                  and (node.get("inputs") or {}).get("samples") == source)


def _consumers(workflow):
    out = {}
    for nid, node in workflow.items():
        for value in ((node or {}).get("inputs") or {}).values():
            if _is_link(value):
                out.setdefault(value[0], set()).add(nid)
    return out


def _ancestors(workflow, start_ids):
    seen, stack = set(), list(start_ids)
    while stack:
        nid = stack.pop()
        if nid in seen or nid not in workflow:
            continue
        seen.add(nid)
        for value in (workflow[nid].get("inputs") or {}).values():
            if _is_link(value):
                stack.append(value[0])
    return seen


def decode_from_latent(workflow, decode_node_id, latent_name, tiled=False) -> dict:
    """A prompt that decodes a saved latent with the original VAE and runs the decode's downstream nodes only."""
    decode = workflow.get(decode_node_id)
    if not isinstance(decode, dict) or decode.get("class_type") not in DECODE_CLASSES:
        raise ValueError(f"node {decode_node_id} is not a VAE decode node")
    source = (decode.get("inputs") or {}).get("samples")
    if not _is_link(source):
        raise ValueError(f"node {decode_node_id} has no linked samples input")
    work = copy.deepcopy(workflow)
    work[LOAD_LATENT_ID] = {"class_type": "LoadLatent", "inputs": {"latent": latent_name}}
    work[decode_node_id]["inputs"]["samples"] = [LOAD_LATENT_ID, 0]
    if tiled and work[decode_node_id]["class_type"] == "VAEDecode":
        work = tile_decode(work, decode_node_id)

    consumers = _consumers(work)
    downstream, stack = set(), [decode_node_id]
    while stack:
        nid = stack.pop()
        if nid in downstream:
            continue
        downstream.add(nid)
        stack.extend(consumers.get(nid, ()))
    downstream = {nid for nid in downstream if work[nid].get("class_type") != "SaveLatent"}
    keep = _ancestors(work, downstream)
    if source[0] in keep:
        raise ValueError("the decoded output also depends on the sampler through another path; "
                         "decoding the saved latent alone cannot reproduce it")
    return {nid: work[nid] for nid in work if nid in keep}


def new_latent_files(before, output_dir) -> list:
    output_dir = Path(output_dir)
    if not output_dir.is_dir():
        return []
    before = set(before)
    return sorted(p for p in output_dir.rglob("*.latent") if p.is_file() and p not in before)

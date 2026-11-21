"""Capacity check before a paid render (F1, NEW_WORK 11): weights, decoded frames and calibrated peaks against each
GPU type's memory and host RAM.

Three kinds of numbers, always labelled:

* facts (`basis: "fact"`) - sizes of the model files the workflow loads, and the exact size of the float32 IMAGE
  tensor a decode of the declared latent produces (frames x height x width x 3 x 4 bytes x batch);
* hard limits (`verdict: "blocked"`) - only physical impossibilities: a single model file larger than the GPU memory
  and host RAM together, or the decoded frames alone larger than host RAM (ComfyUI keeps IMAGE tensors in host RAM);
* estimates (`basis: "calibrated_estimate"`) - peaks scaled from earlier runs of the same model set on this
  installation (C-3 `final.peak`), linear in latent size and labelled as such (attention grows faster than linearly,
  so scaling up underestimates). Without calibration data activation memory is reported as unknown, never guessed.

Nothing here is model-family specific. No host or URL appears in any field.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .draft import FRAME_WIDGETS
from .workflow_analysis import NodeDefs, _nodes

_log = logging.getLogger(__name__)

GIB = 1024**3
MIB = 1024**2
CALIBRATION_FILE = "halofuse_calibration.jsonl"
_MAX_SAMPLES = 500
_TRIM_BYTES = 1024 * 1024
_cal_lock = threading.Lock()

VRAM_COMFORT = 0.85   # share of GPU memory weights may fill before we call it tight
RAM_COMFORT = 0.90


@dataclass(frozen=True)
class Workload:
    weights: tuple            # ((folder, name, bytes|None), ...)
    width: int | None
    height: int | None
    frames: int
    batch: int

    @property
    def weights_bytes(self) -> int:
        return sum(b for _, _, b in self.weights if isinstance(b, int))

    @property
    def largest_model_bytes(self) -> int:
        return max((b for _, _, b in self.weights if isinstance(b, int)), default=0)

    @property
    def unknown_sizes(self) -> list:
        return [f"{f}/{n}" for f, n, b in self.weights if not isinstance(b, int)]

    @property
    def latent_elems(self) -> int | None:
        if not self.width or not self.height:
            return None
        return self.width * self.height * max(1, self.frames) * max(1, self.batch)

    @property
    def decoded_bytes(self) -> int | None:
        elems = self.latent_elems
        return None if elems is None else elems * 3 * 4

    @property
    def model_set(self) -> str:
        return "|".join(sorted(f"{f}/{n}" for f, n, _ in self.weights))


def workload(workflows: list[dict], *, node_defs: NodeDefs, model_sizes: dict) -> Workload:
    """Facts from the workflows: model files (with sizes from *model_sizes* {(folder, name): bytes}) and the
    largest declared latent (width/height/frames/batch on LATENT-producing nodes)."""
    from .workflow_analysis import discover_models

    weights: dict = {}
    width = height = None
    frames, batch = 1, 1
    for wf in workflows:
        for ref in discover_models(wf, node_defs=node_defs, index=_index_from_sizes(model_sizes)):
            weights[(ref.folder, ref.name)] = model_sizes.get((ref.folder, ref.name))
        for _node_id, node in _nodes(wf):
            ct, inputs = node["class_type"], node["inputs"]
            node_def = node_defs.cloud_def(ct) or node_defs.node_def(ct) or {}
            if "LATENT" not in (node_def.get("output") or []):
                continue
            w, h = inputs.get("width"), inputs.get("height")
            if _int(w) and _int(h) and (width is None or w * h > width * height):
                width, height = w, h
            for name in FRAME_WIDGETS:
                if _int(inputs.get(name)):
                    frames = max(frames, inputs[name])
            if _int(inputs.get("batch_size")):
                batch = max(batch, inputs["batch_size"])
    return Workload(tuple((f, n, b) for (f, n), b in sorted(weights.items(), key=lambda kv: (str(kv[0][0]),
                                                                                              kv[0][1]))),
                    width, height, frames, batch)


def _index_from_sizes(model_sizes: dict):
    from .workflow_analysis import ModelIndex, local_model_index

    base = local_model_index()
    by_name = {k: list(v) for k, v in base.by_name.items()}
    for (folder, name) in model_sizes:
        if folder:
            by_name.setdefault(name, []).append(folder)
    return ModelIndex(by_name, base.folders)


def _int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


# ---- calibration (WS4 telemetry peaks) -------------------------------------------------------------------------

def _calibration_path() -> Path:
    from .config import settings_dir

    return Path(settings_dir()) / CALIBRATION_FILE


def load_calibration(path: Path | None = None) -> list[dict]:
    path = path or _calibration_path()
    samples = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines[-_MAX_SAMPLES:]:
        try:
            sample = json.loads(line)
        except ValueError:
            continue  # a torn last line after a crash is skipped
        if isinstance(sample, dict) and isinstance(sample.get("model_set"), str):
            samples.append(sample)
    return samples


def record_calibration(sample: dict, path: Path | None = None) -> None:
    """Append one measured run (thread-safe). The file is trimmed to the newest _MAX_SAMPLES lines only when it grows
    past _TRIM_BYTES, so an append is one small write; load_calibration() reads the newest _MAX_SAMPLES anyway."""
    path = path or _calibration_path()
    line = json.dumps(sample, sort_keys=True)
    with _cal_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        try:
            too_big = path.stat().st_size > _TRIM_BYTES
        except OSError:
            too_big = False
        if too_big:
            lines = path.read_text(encoding="utf-8").splitlines()[-_MAX_SAMPLES:]
            tmp = path.with_suffix(".tmp")
            tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
            tmp.replace(path)


def calibration_sample(final: dict, work: Workload, instance_type: str | None) -> dict | None:
    """A calibration record from a C-3 `final` payload (its `peak`) and the run's workload. None without peaks or
    for an unsuccessful run (a failed run's peak is not the workload's need)."""
    peak = final.get("peak") if isinstance(final, dict) else None
    if not isinstance(peak, dict) or final.get("status") != "succeeded":
        return None
    vram = peak.get("gpu_mem_used_mb")
    ram = peak.get("cgroup_peak_mb") or peak.get("ram_used_mb")
    if not isinstance(vram, (int, float)) and not isinstance(ram, (int, float)):
        return None
    return {"ts": time.time(), "instance_type": instance_type, "model_set": work.model_set,
            "weights_bytes": work.weights_bytes, "latent_elems": work.latent_elems,
            "peak_vram_mb": vram if isinstance(vram, (int, float)) else None,
            "peak_ram_mb": ram if isinstance(ram, (int, float)) else None}


def calibration_event_hook(job_id: str, entry: dict, *, store=None, node_defs: NodeDefs | None = None,
                           path: Path | None = None) -> None:
    """lifecycle.register_event_hook target: on a C-3 `final` event, record the run's measured peaks."""
    if not isinstance(entry, dict) or entry.get("kind") != "final":
        return
    try:
        if store is None:
            from .job_store import default_store
            store = default_store()
        record = store.get(job_id) or {}
        workflow = json.loads(store.read_file(job_id, "workflow.json"))
        from .workflow_analysis import _class_types, _default_snapshot, local_node_defs
        defs = node_defs or NodeDefs(local_node_defs(_class_types(workflow)), _default_snapshot())
        sizes = _local_sizes(workflow, defs)
        sample = calibration_sample(entry.get("data") or {}, workload([workflow], node_defs=defs, model_sizes=sizes),
                                    record.get("instance_type"))
        if sample is not None:
            record_calibration(sample, path)
    except Exception as exc:  # noqa: BLE001 - calibration must never disturb the job
        _log.debug("calibration not recorded for %s (%s)", job_id, type(exc).__name__)


def _local_sizes(workflow: dict, defs: NodeDefs) -> dict:
    from .workflow_analysis import discover_models, local_model_index

    sizes = {}
    try:
        import folder_paths  # ComfyUI-provided
    except Exception:  # noqa: BLE001
        return sizes
    for ref in discover_models(workflow, node_defs=defs, index=local_model_index()):
        if ref.folder is None:
            continue
        try:
            path = folder_paths.get_full_path(ref.folder, ref.name)
            if path:
                sizes[(ref.folder, ref.name)] = Path(path).stat().st_size
        except Exception:  # noqa: BLE001
            continue
    return sizes


# ---- assessment ------------------------------------------------------------------------------------------------

def _gb(value) -> float | None:
    return None if value is None else round(value / GIB, 2)


def assess_sku(work: Workload, sku, calibration: list[dict]) -> dict:
    """Verdict for one GPU type: blocked | likely_insufficient | tight | ok | unknown, with reasons and basis."""
    vram = sku.gpu_memory_gb * GIB if sku.gpu_memory_gb else None
    ram = sku.memory_gb * GIB if sku.memory_gb else None
    reasons, verdict, basis = [], "ok", "weights_only"
    largest, total, decoded = work.largest_model_bytes, work.weights_bytes, work.decoded_bytes
    est = _calibrated(work, calibration)

    if vram is None or ram is None:
        reasons.append("GPU memory or host RAM of this GPU type is not published, so it was not compared")
        verdict = "unknown"
    else:
        if largest > vram + ram:
            verdict, basis = "blocked", "hard_limit"
            reasons.append(f"the largest model ({_gb(largest)} GB) is bigger than GPU memory and host RAM together "
                           f"({_gb(vram + ram)} GB)")
        if decoded is not None and decoded > ram:
            verdict, basis = "blocked", "hard_limit"
            reasons.append(f"the decoded frames alone ({_gb(decoded)} GB as float32) exceed host RAM ({_gb(ram)} GB)")
        if verdict != "blocked":
            if total > VRAM_COMFORT * vram:
                verdict = "tight"
                if largest > vram:
                    reasons.append(f"the largest model ({_gb(largest)} GB) does not fit in GPU memory "
                                   f"({_gb(vram)} GB): ComfyUI must offload weights (slow)")
                else:
                    reasons.append(f"all models together ({_gb(total)} GB) exceed {int(VRAM_COMFORT * 100)}% of GPU "
                                   f"memory: they are loaded one after another")
            if total > RAM_COMFORT * ram:
                verdict = "tight"
                reasons.append(f"all models together ({_gb(total)} GB) are close to or above host RAM "
                               f"({_gb(ram)} GB): a host-RAM kill at model load is possible")
            if decoded is not None and decoded > 0.5 * ram:
                verdict = "tight"
                reasons.append(f"the decoded frames take {_gb(decoded)} GB of {_gb(ram)} GB host RAM")
            if est is not None:
                basis = "calibrated_estimate"
                if est["peak_vram_mb"] is not None and est["peak_vram_mb"] * MIB > vram:
                    verdict = "likely_insufficient"
                    reasons.append(f"estimated GPU peak {round(est['peak_vram_mb'] / 1024, 1)} GB > "
                                   f"{_gb(vram)} GB ({est['label']})")
                if est["peak_ram_mb"] is not None and est["peak_ram_mb"] * MIB > ram:
                    verdict = "likely_insufficient"
                    reasons.append(f"estimated host RAM peak {round(est['peak_ram_mb'] / 1024, 1)} GB > "
                                   f"{_gb(ram)} GB ({est['label']})")
    out = {"instance_type": sku.instance_type, "gpu_type": sku.gpu_type, "gpu_memory_gb": sku.gpu_memory_gb,
           "memory_gb": sku.memory_gb, "core_count": sku.core_count, "verdict": verdict, "basis": basis,
           "reasons": reasons, "estimate": est}
    return out


def _calibrated(work: Workload, calibration: list[dict]) -> dict | None:
    same = [s for s in calibration if s.get("model_set") == work.model_set and s.get("latent_elems")]
    if not same or not work.latent_elems:
        return None
    ref = max(same, key=lambda s: s.get("ts") or 0)
    ratio = work.latent_elems / ref["latent_elems"]

    def scale(value):
        return None if value is None else round(value * ratio, 1)

    label = (f"linear scaling from {len(same)} measured run(s) of the same models; attention grows faster than "
             "linearly, so scaling up underestimates")
    return {"peak_vram_mb": scale(ref.get("peak_vram_mb")), "peak_ram_mb": scale(ref.get("peak_ram_mb")),
            "ratio": round(ratio, 3), "runs": len(same), "label": label}


def assess(work: Workload, skus: list, *, calibration: list[dict] | None = None,
           instance_type: str | None = None) -> dict:
    """Capacity report for every GPU type, the selected one, and a recommendation (smallest that is "ok")."""
    calibration = calibration or []
    rows = [assess_sku(work, s, calibration) for s in sorted(
        skus, key=lambda s: (s.gpu_memory_gb or 0, s.memory_gb or 0, s.instance_type))]
    recommended = next((r["instance_type"] for r in rows if r["verdict"] == "ok"), None) or next(
        (r["instance_type"] for r in rows if r["verdict"] == "tight"), None)
    selected = next((r for r in rows if r["instance_type"] == instance_type), None)
    facts = {
        "weights_bytes": work.weights_bytes, "largest_model_bytes": work.largest_model_bytes,
        "models": [{"folder": f, "name": n, "size": b} for f, n, b in work.weights],
        "unknown_sizes": work.unknown_sizes,
        "latent": {"width": work.width, "height": work.height, "frames": work.frames, "batch": work.batch},
        "decoded_frames_bytes": work.decoded_bytes,
        "prefetch_disk_bytes": work.weights_bytes,
        "notes": ["Sizes are facts; verdicts other than 'blocked' are estimates.",
                  "Model prefetch copies the weights to the GPU machine's disk; that disk's size is not published, "
                  "so it is not checked."],
    }
    if work.unknown_sizes:
        facts["notes"].append("Some model sizes are unknown, so the weights total is a lower bound.")
    if not any(r["estimate"] for r in rows):
        facts["notes"].append("No measured run of these models exists yet, so activation memory (resolution and "
                              "frame count) is not estimated.")
    return {"facts": facts, "skus": rows, "selected": selected, "recommended": recommended,
            "blocked": bool(selected and selected["verdict"] == "blocked")}

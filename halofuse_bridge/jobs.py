"""Single-render orchestration on top of the durable job record (contract C-2).

``submit_render`` stages the workflow + its input files into a temporary folder, submits it through the C-1
provider port, removes the staging folder (X9), records the cloud job in the job store and hands the job to a
``lifecycle.JobWatcher``. Status, logs, cancellation, the runtime limit and cost are owned by ``lifecycle``; this
module keeps the workflow helpers shared with the queue and model sync.

No provider SDK, ComfyUI or bridge-config import at module import time (tests run without them).
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import NamedTuple

from . import cost as costmod
from . import lifecycle
from .job_store import HaloFuseError, JobStore
from .provider.backend import SubmitSpec

_log = logging.getLogger(__name__)

DEFAULT_MAX_RUNTIME_S = 21600          # 6 h; settings key max_runtime_seconds (C-2 §9)
WALL_CLOCK_MARGIN_S = 600              # provider-side backstop = local limit + 10 min (B2)
DEFAULT_CONTAINER_INACTIVITY_S = 1800  # same value as config.DEFAULT_CONTAINER_INACTIVITY_SECONDS

_MODEL_EXTS = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".sft", ".onnx")


def _normalize_model_paths(prompt: dict) -> None:
    """Convert Windows backslashes to forward slashes in model-path inputs.

    ComfyUI on Windows records sub-foldered model names with backslashes (e.g.
    'FLUX2\\model.safetensors'); the cloud runs Linux, where a backslash is a
    literal character, not a path separator. Rewrite those in place so a
    Windows-authored workflow resolves against the Linux /assets mount. The
    assets library must still mirror your local model folder layout.
    """
    if not isinstance(prompt, dict):
        return
    for node in prompt.values():
        inputs = node.get("inputs") if isinstance(node, dict) else None
        if not isinstance(inputs, dict):
            continue
        for key, value in inputs.items():
            if isinstance(value, str) and "\\" in value and value.lower().endswith(_MODEL_EXTS):
                inputs[key] = value.replace("\\", "/")
    # A model the library holds in another sub-folder of the same model folder (model_sync library search).
    from . import model_sync
    model_sync.apply_relocations(prompt)


def _batch_count(settings: dict) -> int:
    try:
        return max(1, min(int(settings.get("batch_count") or 1), 100))
    except (TypeError, ValueError):
        return 1


def _build_env(settings: dict, batch_count: int) -> dict:
    return {
        "MODEL_BASE_DIR": settings["model_base_dir"],
        "BATCH_COUNT": str(batch_count),
    }


def _build_chunk_env(settings: dict) -> dict:
    """Env for a manifest-mode (queue chunk) job. batch_count travels per entry
    inside the manifest itself; the runner's manifest branch never reads
    BATCH_COUNT (the runner's run_manifest() takes it from the parsed
    JSON, only the single-workflow fallback path reads the env var), so it is
    intentionally omitted here rather than sent as a value nothing will honour.
    """
    return {"MODEL_BASE_DIR": settings["model_base_dir"]}


# Add video/audio loaders here as one-line entries, e.g. "VHS_LoadVideo": ["video"].
INPUT_FILE_NODES: dict[str, list[str]] = {
    "LoadImage":            ["image"],
    "LoadImageMask":        ["image"],
    "VHS_LoadVideo":        ["video"],
    "VHS_LoadVideoFFmpeg":  ["video"],
    "LoadVideo":            ["file"],
}


class _StagedFile(NamedTuple):
    node_id: str
    class_type: str
    field: str
    src: Path
    dest_rel: str


def _collect_input_files(workflow: dict, input_dir: Path) -> list[_StagedFile]:
    """Pure: scan workflow for input-file references; return planned copies.

    Checks each node whose class_type is in INPUT_FILE_NODES, reads the listed
    widget fields, and builds a _StagedFile record per string value. Non-string
    values (node links such as ["12", 0]) are silently skipped. Values annotated
    [output]/[temp] are skipped with a warning. Subfolder paths are preserved.
    Deduped on dest_rel. No filesystem access — fully unit-testable.
    """
    seen: set[str] = set()
    result: list[_StagedFile] = []
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        class_type = node.get("class_type")
        fields = INPUT_FILE_NODES.get(class_type)
        if not fields:
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        for field in fields:
            value = inputs.get(field)
            if not isinstance(value, str):
                continue
            if value.endswith(" [output]") or value.endswith(" [temp]"):
                _log.warning(
                    "node %s (%s) field %r: %r has an [output]/[temp] annotation; skipping",
                    node_id, class_type, field, value,
                )
                continue
            filename = value[: -len(" [input]")] if value.endswith(" [input]") else value
            filename = filename.replace("\\", "/")
            if filename in seen:
                continue
            seen.add(filename)
            result.append(_StagedFile(
                node_id=node_id,
                class_type=class_type,
                field=field,
                src=input_dir / filename,
                dest_rel=filename,
            ))
    return result


def _stage_input_files(workflow: dict, staging_dir: Path) -> None:
    """Copy each planned input file into staging_dir, mirroring its relative path.

    Warns and skips files that do not exist locally; never raises. Calls
    folder_paths at runtime (ComfyUI-provided; not importable at module load time).
    """
    import folder_paths  # ComfyUI-provided; available at runtime
    planned = _plan_inputs(workflow, Path(folder_paths.get_input_directory()))
    staged = 0
    for sf in planned:
        if not sf.src.exists():
            _log.warning(
                "node %s (%s) field %r: %r not found locally; skipping",
                sf.node_id, sf.class_type, sf.field, str(sf.src),
            )
            continue
        dest = staging_dir / sf.dest_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sf.src, dest)
        _log.info(
            "staged node %s (%s) %s -> %s",
            sf.node_id, sf.class_type, sf.src, sf.dest_rel,
        )
        staged += 1
    _log.info("staged %d input file(s)", staged)


def _stage_chunk_input_files(entries: list[dict], staging_dir: Path, log=None) -> None:
    """Merge every entry's referenced input files into one staging pass for a
    queue chunk's single shared /input mount.

    A dest_rel referenced by more than one workflow in the chunk collapses to
    one copy: ComfyUI has one input directory (not one per workflow), so the
    same filename can only resolve to one physical file at the single moment
    this whole chunk is staged — restaging it twice would just copy the same
    source over itself. That said, this does remove a property the old
    one-job-per-item queue had: each item used to stage lazily, right before
    its own submission, so swapping a same-named input file between queue
    items (deliberately or not) would land the *new* file on the *later*
    item. Staging a whole chunk up front means every workflow in it now sees
    whatever was on disk at chunk-submit time, whichever one referenced it
    first or last. Not treated as an error — reusing one input image (e.g. a
    ControlNet pose) across every item in a batch is a normal, desirable
    pattern this cannot tell apart from an accidental collision — but always
    logged so it is visible.
    """
    if log is None:
        log = lambda _m: None  # noqa: E731
    import folder_paths  # ComfyUI-provided; available at runtime
    input_dir = Path(folder_paths.get_input_directory())

    planned: dict[str, _StagedFile] = {}
    referenced_by: dict[str, list[int]] = {}
    for idx, entry in enumerate(entries, start=1):
        for sf in _plan_inputs(entry["workflow"], input_dir):
            planned[sf.dest_rel] = sf
            referenced_by.setdefault(sf.dest_rel, []).append(idx)

    for dest_rel, item_numbers in referenced_by.items():
        if len(item_numbers) > 1:
            positions = ", ".join(str(n) for n in item_numbers)
            log(f"[bridge] note: {len(item_numbers)} workflows in this batch "
                f"(items {positions}) share input file {dest_rel!r}; each gets "
                "the same content")

    staged = 0
    for sf in planned.values():
        if not sf.src.exists():
            _log.warning(
                "node %s (%s) field %r: %r not found locally; skipping",
                sf.node_id, sf.class_type, sf.field, str(sf.src),
            )
            continue
        dest = staging_dir / sf.dest_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sf.src, dest)
        staged += 1
    _log.info("staged %d input file(s) for %d-workflow batch", staged, len(entries))


def _extract_validation_error(lines: list[str]) -> str | None:
    """Pull a human-readable reason out of a ComfyUI prompt-validation rejection.

    The runner prints the raw /prompt 400 body, which carries node_errors with the
    offending input and value (for example a model name not present on /assets).
    """
    for line in lines:
        s = line.strip()
        if '"node_errors"' not in s:
            continue
        try:
            data = json.loads(s)
        except ValueError:
            continue
        msgs = []
        for node_id, info in (data.get("node_errors") or {}).items():
            ctype = info.get("class_type", "node")
            for err in info.get("errors", []):
                detail = err.get("details") or err.get("message") or "invalid"
                if err.get("type") == "value_not_in_list":
                    detail += (" — not found on the cloud. Make sure this model exists "
                               "under /assets at that path; the HaloFuse cloud model library must "
                               "mirror your local model folders.")
                msgs.append(f"{ctype} {node_id}: {detail}")
        if msgs:
            return " | ".join(msgs)
    return None





def _close(client) -> None:
    close = getattr(client, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001
            pass


# ---- settings + submit ------------------------------------------------------------------------------------------

def _load_settings() -> dict:
    from .config import load_settings  # lazy: config imports the provider SDK on some branches
    return load_settings()


def _container_inactivity_s(settings: dict) -> int:
    from .config import container_inactivity_seconds
    return int(container_inactivity_seconds(settings))


def _plan_inputs(workflow: dict, input_dir: Path):
    """H-a: WS5 node-definition driven planning when present, else the built-in INPUT_FILE_NODES table."""
    try:
        from .workflow_analysis import plan_inputs  # type: ignore[attr-defined]  # WS5
    except Exception:  # noqa: BLE001
        return _collect_input_files(workflow, input_dir)
    return list(plan_inputs(workflow, input_dir))


def _job_env(settings: dict, base: dict, *, max_runtime_s: int, preview_mode: str | None, nonce: str,
             preview_method: str | None = None) -> dict:
    """Env for a cloud job: the base contract + the runner-side runtime backstop + WS4 launch preset (R-WS4-4)."""
    env = dict(base)
    env["JOB_TIMEOUT_SECONDS"] = str(int(max_runtime_s))
    try:
        from . import presets  # type: ignore[attr-defined]  # WS4
    except ImportError:
        return env
    env.update(presets.job_env(settings.get("launch_preset") or "safe",
                               extra_args=settings.get("comfy_extra_args") or None,
                               extra_env=settings.get("extra_env") or None,
                               preview=(preview_mode or settings.get("preview_mode")) == "live", nonce=nonce,
                               oom_ladder=settings.get("oom_ladder") or None,   # R-WS4-9
                               preview_method=preview_method or settings.get("preview_method") or None))
    return env


def new_event_nonce() -> str:
    """Per-job token that authenticates the runner's [hf] event lines (C-3); not a secret."""
    return secrets.token_hex(8)


def build_tags(store: JobStore, job_id: str) -> tuple:
    """Cloud job tags (C-2 §1): re-discovery after a lost store and the own-jobs filter."""
    return ("halofuse", store.install_tag, f"hf-job-{job_id}")


def max_runtime_for(settings: dict, override=None) -> int:
    value = override if override not in (None, "") else settings.get("max_runtime_seconds")
    try:
        value = int(float(value)) if value not in (None, "") else DEFAULT_MAX_RUNTIME_S
    except (TypeError, ValueError):
        value = DEFAULT_MAX_RUNTIME_S
    return max(1, value)


def stage_and_submit(backend, *, settings: dict, instance_type: str, files: dict, workflows_for_inputs: list,
                     env: dict, tags: tuple, max_wall_clock_s: int | None, instance_handle: str | None = None,
                     log=None):
    """Write ``files`` + referenced input files into a temp dir, submit through the port, always remove the dir.

    The temp dir holds copies of the artist's input images/videos, so it is deleted whether submit succeeds or
    fails (X9). The C-1 adapter uploads ``input_dir`` inside ``submit``; nothing reads it afterwards.
    """
    tmp = Path(tempfile.mkdtemp(prefix="halofuse-input-"))
    try:
        for name, text in files.items():
            (tmp / name).write_text(text, encoding="utf-8")
        if "workflow.json" in files:
            for wf in workflows_for_inputs:
                _stage_input_files(wf, tmp)
        else:
            _stage_chunk_input_files([{"workflow": wf} for wf in workflows_for_inputs], tmp, log=log)
        spec = SubmitSpec(
            image=settings["image"], command=tuple(settings["command"]),
            instance_type=instance_type, env=dict(env), input_dir=tmp,
            max_wall_clock_s=max_wall_clock_s, container_inactivity_s=_container_inactivity_s(settings),
            tags=tuple(tags), instance_handle=instance_handle,
            assets_path=settings.get("assets_library_path") or None,
            assets_space=settings.get("assets_library_space") or None,
            image_affinity=settings.get("image_affinity") or None,
        )
        return backend.submit(spec)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


_CLIENT_REFS: dict[str, str] = {}
_CLIENT_REF_LOCK = threading.Lock()


def submit_render(workflow: dict, *, instance_type: str | None = None, render_repeats=None, max_runtime_s=None,
                  max_job_cost_usd=None, label: str = "Render", client_ref: str | None = None,
                  preview_mode: str | None = None, store: JobStore | None = None, policy: lifecycle.LifecyclePolicy | None = None,
                  start_watcher: bool = True, preview_method: str | None = None) -> dict:
    """Submit one render. Returns the job record. Raises HaloFuseError (branded) on failure."""
    if not isinstance(workflow, dict) or not workflow:
        raise HaloFuseError("bad_request", "No workflow provided.", 400)
    store = lifecycle._store(store)
    if preview_method not in (None, "", "none", "latent2rgb", "taesd"):
        raise HaloFuseError("bad_request", "The step preview must be Off, Fast or Detailed.", 400)
    if client_ref:
        with _CLIENT_REF_LOCK:
            known = _CLIENT_REFS.get(client_ref) or store.find_by_client_ref(client_ref)
        if known and store.get(known):
            return store.get(known)
    settings = _load_settings()
    costmod.check_daily_cap(store, settings, time.time())
    _normalize_model_paths(workflow)
    sku = instance_type or settings.get("instance_type")
    repeats = _batch_count({"batch_count": render_repeats if render_repeats is not None
                            else settings.get("batch_count")})
    max_rt = max_runtime_for(settings, max_runtime_s)
    cap = costmod.setting_number({"v": max_job_cost_usd}, "v") if max_job_cost_usd not in (None, "") \
        else costmod.setting_number(settings, "max_job_cost_usd")
    wf_text = json.dumps(workflow)
    nonce = new_event_nonce()
    rec = store.create(kind="render", label=label or "Render", fields={
        "client_ref": client_ref, "render_repeats": repeats, "event_nonce": nonce,
        "preview_mode": preview_mode if preview_mode in ("none", "check", "draft", "live", "final") else None,
        "preview_method": preview_method or settings.get("preview_method") or "latent2rgb",
        "cloud": {"instance_type": sku},
        "workflow": {"sha256": hashlib.sha256(wf_text.encode("utf-8")).hexdigest(), "path": "workflow.json"},
        "limits": {"max_runtime_s": max_rt, "max_wall_clock_s_sent": max_rt + WALL_CLOCK_MARGIN_S,
                   "max_job_cost_usd": cap},
    })
    job_id = rec["job_id"]
    if client_ref:
        with _CLIENT_REF_LOCK:
            _CLIENT_REFS[client_ref] = job_id
    store.write_file(job_id, "workflow.json", wf_text)
    store.append_event(job_id, "note", text=f"Render created on {sku} ({repeats} render repeat(s), "
                                            f"maximum runtime {max_rt} s).")
    backend = None
    try:
        backend = lifecycle.open_backend(settings)
        _record_rate(store, job_id, backend, sku)
        ref = stage_and_submit(
            backend, settings=settings, instance_type=sku, files={"workflow.json": wf_text},
            workflows_for_inputs=[workflow],
            env=_job_env(settings, _build_env(settings, repeats), max_runtime_s=max_rt, preview_mode=preview_mode,
                         nonce=nonce, preview_method=preview_method),
            tags=build_tags(store, job_id),
            max_wall_clock_s=max_rt + WALL_CLOCK_MARGIN_S)
    except Exception as exc:  # noqa: BLE001
        err = lifecycle.as_halofuse_error(exc)
        lifecycle._fail_without_cloud(store, job_id, err.code, err.message)
        raise err from None
    finally:
        if backend is not None:
            lifecycle.close_backend(backend)
    return _attach_cloud_job(store, job_id, ref, sku, policy=policy, start_watcher=start_watcher)


def _record_rate(store: JobStore, job_id: str, backend, sku: str) -> None:
    try:
        est = backend.estimate(sku)
        rate = getattr(est, "rate_per_hour_usd", None)
    except Exception:  # noqa: BLE001 - an unpriced SKU must not block the render
        rate = None
    if rate is None:
        store.append_event(job_id, "note", level="warning", text="No price is available for this GPU type; "
                                                                 "the cost estimate stays empty.",
                           data={"code": "rate_unavailable"})
    store.update(job_id, cost={"rate_per_hour_usd": rate})


def _attach_cloud_job(store: JobStore, job_id: str, ref, sku: str, *, policy=None, start_watcher=True) -> dict:
    cid = getattr(ref, "job_id", None)

    def fn(r):
        r["cloud"]["job_ids"].append(cid)
        r["cloud"]["current_job_id"] = cid
        r["cloud"]["status"] = getattr(ref, "status", None)
        r["submitted_at"] = time.time()
    rec = store.mutate(job_id, fn)
    store.append_event(job_id, "submit", text="Submitted to the HaloFuse cloud.",
                       data={"cloud_job_id": cid, "instance_type": sku})
    if (rec.get("cancel") or {}).get("state") == "cancel_requested" and rec["cancel"].get("mode") == "now":
        lifecycle._issue_backend_cancel(store, job_id)  # Stop was pressed while the render was being submitted
    else:
        lifecycle._advance(store, job_id, lifecycle._provider_state(getattr(ref, "status", None), False))
    if start_watcher:
        lifecycle.start_watch(job_id, store=store, policy=policy)
    return store.get(job_id)

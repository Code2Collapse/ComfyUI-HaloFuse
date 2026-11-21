"""Pre-render model sync: detect the models a workflow references, check the HaloFuse cloud model library by
name+size, and upload missing ones after explicit consent.

Detection is the sibling of jobs.INPUT_FILE_NODES staging: a class_type -> field -> model-folder map walked over
API-format workflows, with local paths resolved via ComfyUI's folder_paths. The library check and the upload transfer
go through the CloudBackend port (`provider.open_backend()`, contract C-1: `library_stat` / `library_upload`), so this
module never imports the provider SDK and loads without it (R-WS1-03, R-WS2-08). This module owns the
ComfyUI-specific parts and the in-memory uploads registry the HTTP routes poll.

Errors reaching the registry (and so the browser) are the branded, URL-free `BackendError.user_message`; raw detail
goes to the private bridge log only (X3).
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from pathlib import Path
from typing import NamedTuple

from . import brand, jobs, provider
from .config import load_settings
from .provider.backend import BackendError

_log = logging.getLogger(__name__)

# Default single-file upload guard (50 GiB). Same value as the SDK's DEFAULT_MAX_PUT_BYTES in 0.5.0 (read from the
# wheel); kept local so this module never imports the SDK. It is a bridge-side practicality guard against huge
# non-resumable transfers, not a server limit.
DEFAULT_MAX_PUT_BYTES = 50 * 1024**3


class UploadCancelled(Exception):
    """Raised from inside the progress callback when a cancel is requested; it
    propagates through the backend's library_upload and aborts the transfer."""


# Folder keys the cloud image registers under /assets — a hardcoded copy of
# MODEL_SUBDIRS in the runner (runtime repo, runner/halofuse_run.py).
# The two lists are maintained by hand in their two repos; keep them identical.
# test_model_sync.py asserts every folder in MODEL_FILE_NODES stays within this
# list, so a map entry can never point at a folder the cloud never searches.
CLOUD_MODEL_SUBDIRS = [
    "checkpoints", "diffusion_models", "unet", "text_encoders", "clip",
    "clip_vision", "vae", "loras", "controlnet", "upscale_models",
    "embeddings", "configs",
    # Custom-node folders: the runner links any extra top-level folder
    # under MODEL_BASE_DIR into ComfyUI's models/ directory, so models
    # uploaded here are found in the cloud.
    "detection", "sams",
]

# class_type -> {input_field: folder_paths key}. Extend with one-line entries.
# Field names and folder keys verified against the installed ComfyUI core
# (nodes.py / comfy_extras) and ComfyUI-GGUF 1.1.10 on 2026-07-02. The GGUF
# loaders resolve through the legacy keys "unet"/"clip", which ComfyUI's
# folder_paths.map_legacy folds into "diffusion_models"/"text_encoders" — the
# canonical keys used here, so uploads land in the canonical subfolders
# (matching the live share layout) while local resolution still finds files
# under models/unet or models/clip.
MODEL_FILE_NODES: dict[str, dict[str, str]] = {
    "CheckpointLoaderSimple":  {"ckpt_name": "checkpoints"},
    "CheckpointLoader":        {"ckpt_name": "checkpoints"},
    "UNETLoader":              {"unet_name": "diffusion_models"},
    "VAELoader":               {"vae_name": "vae"},
    "LoraLoader":              {"lora_name": "loras"},
    "LoraLoaderModelOnly":     {"lora_name": "loras"},
    "CLIPLoader":              {"clip_name": "text_encoders"},
    "DualCLIPLoader":          {"clip_name1": "text_encoders", "clip_name2": "text_encoders"},
    "TripleCLIPLoader":        {"clip_name1": "text_encoders", "clip_name2": "text_encoders",
                                "clip_name3": "text_encoders"},
    "QuadrupleCLIPLoader":     {"clip_name1": "text_encoders", "clip_name2": "text_encoders",
                                "clip_name3": "text_encoders", "clip_name4": "text_encoders"},
    "ControlNetLoader":        {"control_net_name": "controlnet"},
    "DiffControlNetLoader":    {"control_net_name": "controlnet"},
    "CLIPVisionLoader":        {"clip_name": "clip_vision"},
    "UpscaleModelLoader":      {"model_name": "upscale_models"},
    "UnetLoaderGGUF":          {"unet_name": "diffusion_models"},
    "UnetLoaderGGUFAdvanced":  {"unet_name": "diffusion_models"},
    "CLIPLoaderGGUF":          {"clip_name": "text_encoders"},
    "DualCLIPLoaderGGUF":      {"clip_name1": "text_encoders", "clip_name2": "text_encoders"},
    "TripleCLIPLoaderGGUF":    {"clip_name1": "text_encoders", "clip_name2": "text_encoders",
                                "clip_name3": "text_encoders"},
    "QuadrupleCLIPLoaderGGUF": {"clip_name1": "text_encoders", "clip_name2": "text_encoders",
                                "clip_name3": "text_encoders", "clip_name4": "text_encoders"},
    # --- custom-node loaders used in team workflows ---
    # ComfyUI-WanVideoWrapper
    "WanVideoModelLoader":            {"model": "diffusion_models"},
    "WanVideoVAELoader":              {"model_name": "vae"},
    "WanVideoLoraSelect":             {"lora": "loras"},
    "WanVideoLoraSelectMulti":        {"lora_0": "loras", "lora_1": "loras", "lora_2": "loras",
                                       "lora_3": "loras", "lora_4": "loras"},
    "WanVideoTextEncodeCached":       {"model_name": "text_encoders"},
    "LoadWanVideoT5TextEncoder":      {"model_name": "text_encoders"},
    "LoadWanVideoClipTextEncoder":    {"model_name": "clip_vision"},
    "WanVideoUni3C_ControlnetLoader": {"model": "controlnet"},
    # ComfyUI-WanAnimatePreprocess (models/detection)
    "OnnxDetectionModelLoader":       {"vitpose_model": "detection", "yolo_model": "detection"},
    # Comfyui-SecNodes (models/sams)
    "SeCModelLoader":                 {"model_file": "sams"},
}


class _ModelRef(NamedTuple):
    node_id: str
    class_type: str
    field: str
    folder: str
    name: str  # forward-slash relative name, as the cloud workflow will reference it


def _collect_model_refs(workflow: dict) -> list[_ModelRef]:
    """Pure: scan an API-format workflow for model references; dedupe on
    (folder, name). Non-string values (node links such as ["12", 0]) are skipped,
    as are values without a model file extension (VAELoader's 'pixel_space' /
    taesd entries and other non-file combo options). No filesystem access."""
    seen: set[tuple[str, str]] = set()
    refs: list[_ModelRef] = []
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        class_type = node.get("class_type")
        fields = MODEL_FILE_NODES.get(class_type)
        if not fields:
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        for field, folder in fields.items():
            value = inputs.get(field)
            if not isinstance(value, str):
                continue
            if not value.lower().endswith(jobs._MODEL_EXTS):
                continue
            name = value.replace("\\", "/").strip("/")
            key = (folder, name)
            if key in seen:
                continue
            seen.add(key)
            refs.append(_ModelRef(node_id, class_type, field, folder, name))
    return refs


def _resolve_local(ref: _ModelRef) -> Path | None:
    """Resolve a model reference against the local install via folder_paths
    (runtime import — ComfyUI-provided, like jobs._stage_input_files)."""
    import folder_paths  # ComfyUI-provided; available at runtime
    try:
        path = folder_paths.get_full_path(ref.folder, ref.name)
    except Exception:  # noqa: BLE001 - an unknown folder key must not sink the check
        _log.warning("folder_paths could not resolve %s/%s", ref.folder, ref.name)
        return None
    return Path(path) if path else None


def _upload_guard_bytes(settings: dict) -> int | None:
    """The in-node single-upload guard in bytes, from the upload_guard_gb setting.

    A practicality guard against huge non-resumable transfers, not a server limit
    (the library allows 2 TB per file). <= 0 disables the guard entirely."""
    raw = settings.get("upload_guard_gb")
    try:
        gb = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_PUT_BYTES
    if gb <= 0:
        return None
    return int(gb * 1024**3)


def _remote_path(assets_path: str, folder: str, name: str) -> str | None:
    """The library path (relative to the space root, `/`-separated) for folder/name, or None when any part could
    escape its parent (TM-011: `..`, `.`, empty, backslash, NUL, drive letter). Leading/trailing slashes of the
    configured assets path are dropped: the SDK strips them as well (its upload call strips them)."""
    parts = [p for p in str(assets_path or "").split("/") if p]
    parts += [str(folder)] + str(name).split("/")
    for part in parts:
        if (part in ("", ".", "..") or "\x00" in part or "\\" in part
                or (len(part) >= 2 and part[1] == ":")):
            return None
    return "/".join(parts)


def _user_error(exc: BaseException) -> str:
    """Branded text for the registry / browser; the raw detail stays in the private log (X3)."""
    if isinstance(exc, BackendError):
        return exc.user_message
    return brand.message("unknown")


def check_models(workflows: list[dict], settings: dict | None = None, *, node_defs=None, index=None) -> dict:
    """Classify every model referenced by *workflows* against the cloud model library (see classify_models).

    References come from node-definition discovery (workflow_analysis.discover_models: local model file lists for
    any loader and folder, then the MODEL_FILE_NODES table, then a labelled extension heuristic). Raises
    BackendError (branded) when the service cannot be used."""
    from . import workflow_analysis

    settings = settings or load_settings()
    refs: list = []
    seen: set = set()
    for wf in workflows:
        # Same normalization the submit path applies, so the checked remote path
        # is exactly what the cloud workflow will request.
        jobs._normalize_model_paths(wf)
        for ref in workflow_analysis.discover_models(wf, node_defs=node_defs, index=index):
            if ref.key() not in seen:
                seen.add(ref.key())
                refs.append(ref)
    if not refs:
        return _empty_result()
    with provider.open_backend(settings) as be:
        return classify_models(refs, settings, be)


def _empty_result() -> dict:
    return {"present": [], "uploadable": [], "over_limit": [], "missing": [], "uploading": [], "invalid": [],
            "unverified": []}


def over_limit_message(size: int, guard: int) -> str:
    """C3: why a large model is not uploaded automatically, and what the artist can do instead."""
    gib = 1024**3
    return (f"This model is {size / gib:.1f} GB, larger than the {guard / gib:.0f} GB automatic upload limit in the "
            f"{brand.PRODUCT} settings. Uploads to the cloud library cannot resume: an interrupted upload starts "
            "again from zero, so very large files are held back on purpose. You can raise the limit under Advanced "
            "(0 removes it) and keep this computer online until the upload finishes, ask your administrator to add "
            "the file to the cloud library directly, or use a smaller variant of the model (for example a pruned or "
            "fp8 file).")


def classify_models(refs: list, settings: dict, be) -> dict:
    """Classify discovered model references against the library through backend *be* (C-1 library_stat).

    Returns {"present", "uploadable", "over_limit", "missing", "uploading", "invalid", "unverified"} — lists of
    dicts the UI renders directly. Present = name exists at the expected remote path with a matching size (name
    alone suffices when there is no local copy to compare). A name match with a size mismatch is uploadable with
    size_mismatch=True (overwrite on upload). Files in flight from an earlier consent land in "uploading" so no
    second PUT is ever offered. A name that could escape the library folder lands in "invalid" and is never sent to
    the service (TM-011); a reference whose folder could not be determined lands in "unverified"."""
    result = _empty_result()
    assets_path = (settings.get("assets_library_path") or "").rstrip("/")
    space = settings.get("assets_library_space") or None
    guard = _upload_guard_bytes(settings)
    in_flight = {(f["folder"], f["name"]) for f in _active_upload_files()}
    for ref in refs:
        item = {"node_id": ref.node_id, "class_type": ref.class_type, "field": ref.field, "folder": ref.folder,
                "name": ref.name, "size": None}
        if ref.folder is None:
            item["message"] = (f"{ref.name}: the model folder could not be determined from the node definition, so "
                               "it was not checked.")
            result["unverified"].append(item)
            continue
        item["remote_rel"] = f"{assets_path}/{ref.folder}/{ref.name}"
        remote_path = _remote_path(assets_path, ref.folder, ref.name)
        if remote_path is None:
            item["message"] = "This model name is not a valid library path, so it was not checked."
            result["invalid"].append(item)
            continue
        local = _resolve_local(_ModelRef(ref.node_id, ref.class_type, ref.field, ref.folder, ref.name))
        local_size = local.stat().st_size if local else None
        item["size"] = local_size
        if (ref.folder, ref.name) in in_flight:
            result["uploading"].append(item)
            continue
        entry = be.library_stat(remote_path, space)
        remote_size = entry.size if entry else None
        if entry is not None and (local_size is None or remote_size == local_size):
            item["size"] = remote_size if remote_size is not None else local_size
            result["present"].append(item)
        elif local is None:
            result["missing"].append(item)
        elif guard is not None and local_size > guard:
            item["guard_bytes"] = guard
            item["message"] = over_limit_message(local_size, guard)
            result["over_limit"].append(item)
        else:
            item["size_mismatch"] = entry is not None
            # Report the cloud size too, so the panel can show both and offer to render with the existing cloud copy.
            item["remote_size"] = remote_size
            result["uploadable"].append(item)
    return result


# ------------------------------------------------------------------
# Uploads registry — dict + lock + daemon thread
# ------------------------------------------------------------------

_UPLOADS: dict[str, dict] = {}
_UPLOAD_LOCK = threading.Lock()
_MAX_TRANSPORT_RETRIES = 1  # one automatic whole-file retry when the service was unreachable


def _set_upload(upload_id: str, **fields) -> None:
    with _UPLOAD_LOCK:
        state = _UPLOADS.get(upload_id)
        if state:
            state.update(fields)


def _set_file(upload_id: str, index: int, **fields) -> None:
    with _UPLOAD_LOCK:
        state = _UPLOADS.get(upload_id)
        if state:
            state["files"][index].update(fields)


def _upload_cancelled(upload_id: str) -> bool:
    with _UPLOAD_LOCK:
        state = _UPLOADS.get(upload_id)
        return bool(state and state.get("cancel"))


def get_upload_state(upload_id: str) -> dict | None:
    with _UPLOAD_LOCK:
        state = _UPLOADS.get(upload_id)
        if state is None:
            return None
        snapshot = dict(state)
        snapshot["files"] = [dict(f) for f in state["files"]]
        return snapshot


def list_uploads() -> dict[str, dict]:
    """All registry entries, keyed by id — lets a reloaded browser tab re-attach
    its progress badge to an upload that is still running server-side."""
    with _UPLOAD_LOCK:
        return {
            uid: {**state, "files": [dict(f) for f in state["files"]]}
            for uid, state in _UPLOADS.items()
        }


def cancel_upload(upload_id: str) -> None:
    _set_upload(upload_id, cancel=True)
    _log.info("upload %s: cancel requested", upload_id)


def _active_upload_files() -> list[dict]:
    """Files currently queued or transferring across all active uploads."""
    with _UPLOAD_LOCK:
        out: list[dict] = []
        for state in _UPLOADS.values():
            if state.get("status") in ("queued", "running"):
                out.extend(dict(f) for f in state["files"]
                           if f["status"] in ("queued", "uploading"))
        return out


def prepare_upload_files(files: list[dict], settings: dict | None = None) -> list[dict]:
    """Re-derive each requested {folder, name} server-side: local path via
    folder_paths, size, and the library-relative destination. The browser only
    nominates folder+name from a check result; it never dictates local paths.
    Raises ValueError when a nominated file cannot be resolved locally."""
    settings = settings or load_settings()
    assets_path = (settings.get("assets_library_path") or "").rstrip("/")
    prepared: list[dict] = []
    for f in files:
        folder = str(f.get("folder") or "")
        name = str(f.get("name") or "").replace("\\", "/").strip("/")
        if (folder not in _allowed_folders() or not name or ".." in name.split("/")
                or _remote_path(assets_path, folder, name) is None):
            raise ValueError(f"Invalid upload request entry: {f!r}")
        ref = _ModelRef("", "", "", folder, name)
        local = _resolve_local(ref)
        if local is None or not local.is_file():
            raise ValueError(f"{folder}/{name} not found locally; re-run the model check")
        prepared.append({
            "folder": folder,
            "name": name,
            "local_path": str(local),
            "remote_rel": f"{assets_path}/{folder}/{name}",
            "size": local.stat().st_size,
        })
    return prepared


def _allowed_folders() -> set[str]:
    """Upload destinations: the runner's fixed list plus the canonical names of this install's model folders
    (custom-node folders included). The local file is always re-derived through folder_paths for that folder."""
    from .workflow_analysis import allowed_cloud_folders

    return allowed_cloud_folders()


def _register_upload(files: list[dict]) -> str:
    upload_id = uuid.uuid4().hex[:12]
    state = {
        "status": "queued",
        "started_at": time.time(),
        "error": None,
        "cancel": False,
        "files": [{
            "folder": f["folder"], "name": f["name"], "local_path": f["local_path"],
            "remote_rel": f["remote_rel"], "size": f["size"],
            "sent": 0, "status": "queued", "error": None,
        } for f in files],
    }
    with _UPLOAD_LOCK:
        _UPLOADS[upload_id] = state
    return upload_id


def start_upload(files: list[dict], settings: dict | None = None) -> str:
    """Start uploading *prepared* files (see prepare_upload_files) in a daemon
    thread; returns the registry id the routes poll. The thread lives in the
    ComfyUI server process, so it survives the browser tab closing."""
    settings = settings or load_settings()
    upload_id = _register_upload(files)
    threading.Thread(target=_run_upload, args=(upload_id, settings), daemon=True).start()
    return upload_id


def _put_file(be, space: str | None, f: dict, progress) -> None:
    """One file, with one automatic whole-file retry when the service was unreachable (PUT overwrite is
    idempotent, so the retry restarts from zero: the server has no resume protocol). UploadCancelled is never
    retried, and neither is any other service error."""
    for attempt in range(_MAX_TRANSPORT_RETRIES + 1):
        try:
            be.library_upload(
                Path(f["local_path"]), f["remote_path"], space,
                on_progress=progress, max_bytes=None,  # guard already applied at check time
            )
            return
        except UploadCancelled:
            raise
        except BackendError as exc:
            if exc.code != "unreachable" or attempt >= _MAX_TRANSPORT_RETRIES:
                raise
            _log.warning("upload of %s could not reach the service; retrying once", f["name"])


SIZE_MISMATCH_MESSAGE = (f"The cloud copy has a different size than this computer's file after the upload. "
                         f"Upload it again; if it repeats, check the {brand.PRODUCT} log.")


def _verify_upload(be, space, upload_id: str, index: int, f: dict, remote_path: str) -> bool:
    """After a PUT: the library must report the local size (the service stores no checksum), then the file's
    SHA-256 is recorded in the local library manifest (model_library) so later checks can say "sha256 verified".
    Hashing is best effort: a failure there never fails an upload that arrived with the right size."""
    _set_file(upload_id, index, status="verifying")
    entry = be.library_stat(remote_path, space)
    if entry is None or (entry.size is not None and entry.size != f["size"]):
        _fail_upload(upload_id, index, f["name"], SIZE_MISMATCH_MESSAGE)
        _log.warning("upload %s: %s size check failed after upload", upload_id, f["name"])
        return False
    try:
        from . import model_library

        digest = model_library.hash_and_cache(Path(f["local_path"]), cancel=lambda: _upload_cancelled(upload_id))
        model_library.record_upload(remote_path, f["size"], digest)
        _set_file(upload_id, index, sha256=digest)
    except Exception as exc:  # noqa: BLE001 - verification metadata only
        _log.info("upload %s: %s hash not recorded (%s)", upload_id, f["name"], type(exc).__name__)
    return True


def _fail_upload(upload_id: str, index: int, name: str, message: str) -> None:
    _set_file(upload_id, index, status="failed", error=message)
    _set_upload(upload_id, status="failed", error=f"{name}: {message}")


def _run_upload(upload_id: str, settings: dict) -> None:
    space = settings.get("assets_library_space") or None
    assets_path = (settings.get("assets_library_path") or "").rstrip("/")
    try:
        with provider.open_backend(settings) as be:
            _set_upload(upload_id, status="running")
            state = get_upload_state(upload_id)
            cancelled = False
            for i, f in enumerate(state["files"]):
                if _upload_cancelled(upload_id):
                    cancelled = True
                    _set_file(upload_id, i, status="cancelled")
                    continue
                remote_path = _remote_path(assets_path, f["folder"], f["name"])
                if remote_path is None:
                    _fail_upload(upload_id, i, f["name"], brand.message("invalid_request"))
                    return
                _set_file(upload_id, i, status="uploading")

                def progress(sent: int, total=None, _i: int = i) -> None:
                    if _upload_cancelled(upload_id):
                        raise UploadCancelled()
                    if total is None:
                        _set_file(upload_id, _i, sent=sent)
                    else:
                        _set_file(upload_id, _i, sent=sent, size=total)

                try:
                    _put_file(be, space, dict(f, remote_path=remote_path), progress)
                except UploadCancelled:
                    cancelled = True
                    _set_file(upload_id, i, status="cancelled")
                    _log.info("upload %s: %s cancelled mid-transfer", upload_id, f["name"])
                    continue
                except Exception as exc:  # noqa: BLE001 - surface in the registry (branded), stop the batch
                    _fail_upload(upload_id, i, f["name"], _user_error(exc))
                    _log.warning("upload %s: %s failed [%s] %s", upload_id, f["name"],
                                 getattr(exc, "code", type(exc).__name__), getattr(exc, "detail", ""))
                    return
                if not _verify_upload(be, space, upload_id, i, f, remote_path):
                    return
                _set_file(upload_id, i, status="done", sent=f["size"])
                _log.info("upload %s: %s done (%d bytes)", upload_id, f["name"], f["size"])
            _set_upload(upload_id, status="cancelled" if cancelled else "done")
    except Exception as exc:  # noqa: BLE001 - surface in the UI, branded; no traceback to the console (X3)
        _set_upload(upload_id, status="failed", error=_user_error(exc))
        _log.warning("upload %s failed before transfer [%s] %s", upload_id,
                     getattr(exc, "code", type(exc).__name__), getattr(exc, "detail", ""))

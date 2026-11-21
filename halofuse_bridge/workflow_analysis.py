"""Workflow analysis: node-definition driven discovery of input files and model references, safe input planning
(X2), and the Workflow Check / preflight against a runtime-profile node snapshot (G3, C2, G2, F7).

The API-format workflow (`{node_id: {"class_type", "inputs"}}`) is the execution contract (NEW_WORK section 8). Nothing
here is specific to a model family: what a widget means comes from the node definitions -

* the local ComfyUI process (the node classes this bridge runs next to: real file lists, upload hints), and
* the runtime profile's node snapshot (contract C-5: what the cloud image can run; authoritative for class names and
  non-file combos, never for file lists).

The hand-written tables (`jobs.INPUT_FILE_NODES`, `model_sync.MODEL_FILE_NODES`) are only a fallback behind that.

Public interface:
    plan_inputs(workflow, input_dir, *, node_defs=None, require_exists=False) -> list[StagedInput]  (hook H-a)
    discover_models(workflow, *, node_defs=None, index=None) -> list[ModelRef]
    load_snapshot(profile=None, root=None) -> Snapshot | None
    preflight(workflows, *, snapshot=..., node_defs=None, input_dir=None, index=None, library=None) -> dict (C-4 s5)
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable

from . import brand

_log = logging.getLogger(__name__)

MODEL_EXTS = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".sft", ".onnx")
_MEDIA_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".exr", ".mp4", ".mov", ".mkv",
               ".webm", ".avi", ".wav", ".mp3", ".flac", ".ogg", ".m4a", ".latent")
# ComfyUI upload hints, verified in ComfyUI 0.39.0: nodes.py LoadImage {"image_upload": True};
# comfy_api/latest/_io.py UploadType image_upload / audio_upload / video_upload (serialised as {<hint>: True}).
UPLOAD_HINTS = ("image_upload", "video_upload", "audio_upload", "animated_image_upload")

# Fallback when no node definition is available (the class is not installed locally and not in the snapshot).
FALLBACK_INPUT_FILE_NODES: dict[str, list[str]] = {
    "LoadImage": ["image"],
    "LoadImageMask": ["image"],
    "LoadAudio": ["audio"],
    "LoadVideo": ["file"],
    # ComfyUI-VideoHelperSuite (mappings read from videohelpersuite/nodes.py)
    "VHS_LoadVideo": ["video"],
    "VHS_LoadVideoFFmpeg": ["video"],
    "VHS_LoadAudioUpload": ["audio"],
    "VHS_LoadImages": ["directory"],
}

# folder_paths keys that are not model folders.
_NOT_MODEL_FOLDERS = frozenset({"custom_nodes"})
# Legacy / pack-specific keys that resolve to a canonical cloud folder (ComfyUI folder_paths.map_legacy +
# ComfyUI-GGUF's unet_gguf/clip_gguf, which list models/diffusion_models|unet and models/text_encoders|clip).
FOLDER_ALIASES = {"unet": "diffusion_models", "clip": "text_encoders",
                  "unet_gguf": "diffusion_models", "clip_gguf": "text_encoders"}
_FOLDER_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_CANONICAL_ORDER = ("checkpoints", "diffusion_models", "text_encoders", "vae", "loras", "clip_vision", "controlnet",
                    "upscale_models", "embeddings")


# ---- value types ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class StagedInput:
    """One input file to copy into the job input (same shape as jobs._StagedFile, plus size)."""
    node_id: str
    class_type: str
    field: str
    src: Path
    dest_rel: str
    size: int | None = None


@dataclass(frozen=True)
class Problem:
    code: str
    message: str
    node_id: str | None = None
    class_type: str | None = None
    input: str | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items()}


class InputPlanError(ValueError):
    """Blocked input values (X2) or missing input files (G2). `str()` is a branded, URL-free sentence that names the
    nodes; `problems` carries the itemised list."""

    code = "input_blocked"

    def __init__(self, problems: list[Problem]):
        self.problems = list(problems)
        lines = [f"node {p.node_id} ({p.class_type}) '{p.input}': {p.message}" for p in self.problems[:10]]
        more = f" (+{len(self.problems) - 10} more)" if len(self.problems) > 10 else ""
        super().__init__(f"{brand.PRODUCT} blocked this render before it started: " + "; ".join(lines) + more)


@dataclass(frozen=True)
class ModelRef:
    node_id: str
    class_type: str
    field: str
    folder: str | None          # canonical cloud folder, None when it cannot be determined
    name: str                   # forward-slash relative name, as the cloud workflow references it
    source: str                 # "discovered" (local file lists), "table" (fallback map), "heuristic"
    local: bool = False         # a local file was found for it

    def key(self) -> tuple:
        return (self.folder, self.name)


# ---- node definitions ----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class InputSpec:
    name: str
    kind: str                   # "COMBO", "INT", "FLOAT", "STRING", "BOOLEAN" or a link type ("MODEL", "LATENT", ...)
    options: tuple | None       # combo options, None for non-combos
    extra: dict                 # the options dict ({"default", "min", "max", "step", "image_upload", ...})
    required: bool


def parse_input_specs(node_def: dict | None) -> dict[str, InputSpec]:
    """Normalise a V1 or V3 /object_info entry's inputs to InputSpec, keyed by input name."""
    out: dict[str, InputSpec] = {}
    if not isinstance(node_def, dict):
        return out
    inputs = node_def.get("input") or {}
    for group in ("required", "optional"):
        for name, spec in (inputs.get(group) or {}).items():
            if not isinstance(spec, (list, tuple)) or not spec:
                continue
            head = spec[0]
            extra = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
            if isinstance(head, (list, tuple)):
                out[name] = InputSpec(name, "COMBO", tuple(head), extra, group == "required")
            elif head == "COMBO":
                out[name] = InputSpec(name, "COMBO", tuple(extra.get("options") or ()), extra, group == "required")
            elif isinstance(head, str):
                out[name] = InputSpec(name, head, None, extra, group == "required")
    return out


class NodeDefs:
    """Merged node definitions: local ComfyUI first (real file lists, upload hints), then the snapshot (C-5)."""

    def __init__(self, local: dict | None = None, snapshot: "Snapshot | None" = None):
        self._local = dict(local or {})
        self._snapshot = snapshot
        self._cache: dict[str, dict[str, InputSpec]] = {}

    @property
    def snapshot(self) -> "Snapshot | None":
        return self._snapshot

    def node_def(self, class_type: str) -> dict | None:
        if class_type in self._local:
            return self._local[class_type]
        if self._snapshot is not None:
            return self._snapshot.object_info.get(class_type)
        return None

    def cloud_def(self, class_type: str) -> dict | None:
        """The snapshot's definition (what the cloud image runs); None without a snapshot entry."""
        return self._snapshot.object_info.get(class_type) if self._snapshot is not None else None

    def specs(self, class_type: str) -> dict[str, InputSpec]:
        if class_type not in self._cache:
            self._cache[class_type] = parse_input_specs(self.node_def(class_type))
        return self._cache[class_type]


def local_node_defs(class_types: Iterable[str]) -> dict:
    """Node info for *class_types* from the ComfyUI process this bridge runs in (same construction as ComfyUI 0.39
    server.py node_info: V3 classes via GET_NODE_INFO_V1, V1 via INPUT_TYPES). Empty outside ComfyUI. Only the
    workflow's own classes are queried, once per request (some INPUT_TYPES list a folder)."""
    try:
        import nodes  # ComfyUI-provided
    except Exception:  # noqa: BLE001 - not running inside ComfyUI (tests, tools)
        return {}
    mapping = getattr(nodes, "NODE_CLASS_MAPPINGS", {}) or {}
    out: dict = {}
    for ct in sorted(set(class_types)):
        cls = mapping.get(ct)
        if cls is None:
            continue
        try:
            if hasattr(cls, "GET_NODE_INFO_V1"):
                info = cls.GET_NODE_INFO_V1()
            else:
                info = {"input": cls.INPUT_TYPES(),
                        "output": list(getattr(cls, "RETURN_TYPES", ()) or ()),
                        "output_node": bool(getattr(cls, "OUTPUT_NODE", False))}
            out[ct] = json.loads(json.dumps(info, default=str))
        except Exception as exc:  # noqa: BLE001 - one broken node definition must not sink the analysis
            _log.warning("node definition of %s could not be read (%s)", ct, type(exc).__name__)
    return out


# ---- node snapshot (C-5) -------------------------------------------------------------------------------------

SNAPSHOT_DIR = Path(__file__).resolve().parent / "snapshots"
_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_snapshot_cache: dict[str, tuple[float, "Snapshot"]] = {}
_snapshot_lock = threading.Lock()


@dataclass
class Snapshot:
    profile: str
    source: str
    comfyui_version: str | None
    object_info_status: str
    object_info: dict
    core_classes: frozenset
    pack_of: dict                 # class_type -> pack dict
    packs: list
    catalog_sha256: str | None = None

    @classmethod
    def from_dict(cls, data: dict) -> "Snapshot":
        if not isinstance(data, dict) or not str(data.get("schema", "")).startswith("halofuse.node_snapshot/1"):
            raise ValueError("not a halofuse.node_snapshot/1 document")
        packs = [p for p in (data.get("packs") or []) if isinstance(p, dict)]
        pack_of: dict = {}
        for pack in packs:
            for ct in pack.get("node_classes") or []:
                pack_of.setdefault(ct, pack)
        return cls(profile=str(data.get("profile") or "unknown"), source=str(data.get("source") or "unknown"),
                   comfyui_version=data.get("comfyui_version"),
                   object_info_status=str(data.get("object_info_status") or "not_captured"),
                   object_info=dict(data.get("object_info") or {}),
                   core_classes=frozenset(data.get("core_node_classes") or ()),
                   pack_of=pack_of, packs=packs, catalog_sha256=data.get("catalog_sha256"))

    def class_status(self, class_type: str) -> tuple[str, dict | None]:
        """("known" | "failed" | "not_installed" | "unverified" | "unknown", pack)."""
        if class_type in self.core_classes:
            return "known", None
        pack = self.pack_of.get(class_type)
        if pack is not None:
            status = pack.get("status")
            if status == "loaded":
                return "known", pack
            if status in ("failed", "not_installed"):
                return status, pack
            return "unverified", pack
        if class_type in self.object_info:
            return "known", None
        if self.object_info_status == "not_captured" or (self.object_info_status != "captured" and any(
                p.get("status") not in ("failed", "not_installed") and p.get("node_classes") is None
                for p in self.packs)):
            # C-5 rule 3: without a complete capture the class may come from a pack whose class list is unknown,
            # so it is reported as not verified instead of blocked.
            return "unverified", None
        return "unknown", None

    def summary(self) -> dict:
        return {"profile": self.profile, "source": self.source, "comfyui_version": self.comfyui_version,
                "object_info_status": self.object_info_status}


def load_snapshot(profile: str | None = None, root: Path | None = None) -> Snapshot | None:
    """The node snapshot for *profile* (default: the index's default_profile) from `halofuse_bridge/snapshots/`
    (copied there by the lead from WS7's build). None when no snapshot is shipped. Cached by file mtime."""
    root = Path(root) if root is not None else SNAPSHOT_DIR
    if profile is not None and not _PROFILE_RE.match(str(profile)):
        raise ValueError("invalid profile name")
    index_path = root / "index.json"
    file_name = None
    if index_path.is_file():
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
            profile = profile or index.get("default_profile")
            entry = (index.get("profiles") or {}).get(profile) if profile else None
            if isinstance(entry, dict) and isinstance(entry.get("file"), str):
                file_name = entry["file"]
        except (OSError, ValueError):
            _log.warning("snapshot index unreadable")
    if profile is None:
        return None
    file_name = file_name or f"{profile}.json"
    if not _PROFILE_RE.match(Path(file_name).stem) or Path(file_name).name != file_name:
        return None
    path = root / file_name
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    key = str(path)
    with _snapshot_lock:
        cached = _snapshot_cache.get(key)
        if cached and cached[0] == mtime:
            return cached[1]
    try:
        snap = Snapshot.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        _log.warning("node snapshot %s unreadable (%s)", file_name, type(exc).__name__)
        return None
    with _snapshot_lock:
        _snapshot_cache[key] = (mtime, snap)
    return snap


# ---- input planning (X2 / G2 / F7) ---------------------------------------------------------------------------

_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def safe_relative_path(value: str) -> str | None:
    """A workflow file value as a clean forward-slash path relative to the input folder, or None when it could
    point anywhere else: absolute (`/x`, `\\x`), drive letter (`C:x`), UNC (`\\\\host\\share`, `//host`), any `..` or
    `.` part, empty parts, NUL, or a `:` (drive / NTFS alternate data stream) anywhere."""
    if not isinstance(value, str) or not value or "\x00" in value or ":" in value:
        return None
    text = value.replace("\\", "/")
    if text.startswith("/") or _DRIVE_RE.match(value):
        return None
    parts = text.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    if any(p != p.strip() or p.endswith(".") for p in parts):
        # Windows silently drops trailing dots/spaces: "a.png." would open "a.png" - refuse the ambiguity.
        return None
    return "/".join(parts)


def _is_link(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(os.path, "isjunction", None)  # Python 3.12+
        return bool(is_junction and is_junction(path))
    except OSError:
        return True


def _inside(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def _check_under(input_dir: Path, rel: str) -> tuple[Path | None, str | None]:
    """Resolve rel under input_dir. Returns (path, None) or (None, reason). Read-only filesystem checks."""
    base = input_dir.resolve()
    candidate = base.joinpath(*rel.split("/"))
    current = base
    for part in rel.split("/"):
        current = current / part
        if _is_link(current):
            return None, "is a link (symbolic link or junction); links are never followed"
    resolved = candidate.resolve()
    if not _inside(resolved, base):
        return None, "points outside the ComfyUI input folder"
    return resolved, None


def _annotated_name(value: str) -> tuple[str | None, str | None]:
    """ComfyUI annotations: 'x [input]' -> x; '[output]' / '[temp]' -> (None, kind)."""
    for kind in ("output", "temp"):
        if value.endswith(f" [{kind}]"):
            return None, kind
    if value.endswith(" [input]"):
        return value[: -len(" [input]")], None
    return value, None


def _input_file_fields(class_type: str, inputs: dict, defs: NodeDefs) -> list[tuple[str, str]]:
    """[(field, how)] of the node's widgets that name a file in the input folder.

    how = "upload_hint" (a combo with an image/video/audio upload hint in its node definition) or "table" (the
    fallback list, for loaders that upload from their own JS without a hint, e.g. VideoHelperSuite)."""
    specs = defs.specs(class_type)
    fields: list[tuple[str, str]] = []
    for name, spec in specs.items():
        if spec.kind != "COMBO" or not isinstance(inputs.get(name), str):
            continue
        if any(spec.extra.get(h) for h in UPLOAD_HINTS):
            folder = spec.extra.get("image_folder")
            if folder not in (None, "input"):
                continue  # LoadImageOutput etc.: the file lives in output/temp, not in the input folder
            fields.append((name, "upload_hint"))
    listed = FALLBACK_INPUT_FILE_NODES.get(class_type, [])
    for name in listed:
        if name not in {f for f, _ in fields} and isinstance(inputs.get(name), str):
            fields.append((name, "table"))
    return fields


def plan_inputs(workflow: dict, input_dir: Path, *, node_defs: NodeDefs | None = None,
                require_exists: bool = False) -> list[StagedInput]:
    """Hook H-a (REQUESTS R-WS2-03): the input files *workflow* references, as copies to stage into the job input.

    Never reads outside *input_dir*: a value that is absolute, has a drive letter or UNC prefix, contains `..`, or
    resolves (through a link) outside the folder is refused (X2), as is any link inside it. Refusals are collected
    and raised together as InputPlanError (a ValueError) before anything is read or copied.
    A referenced file that does not exist is refused with *require_exists* (what the Workflow Check uses: G2, verify
    before paying); without it (the default, the 0.3.x staging contract jobs.py relies on) it is returned with
    size None so the caller warns and skips it.
    A directory value (image-sequence loaders) stages every regular file directly inside it.
    Deduplicated on dest_rel; relative folder structure is preserved. Read-only: nothing is copied here."""
    if not isinstance(workflow, dict):
        raise InputPlanError([Problem("bad_workflow", "the workflow is not an API-format graph")])
    defs = node_defs if node_defs is not None else NodeDefs(local_node_defs(_class_types(workflow)),
                                                            _default_snapshot())
    input_dir = Path(input_dir)
    problems: list[Problem] = []
    planned: dict[str, StagedInput] = {}
    for node_id, node in _nodes(workflow):
        class_type = node["class_type"]
        inputs = node["inputs"]
        for field_name, _how in _input_file_fields(class_type, inputs, defs):
            value = inputs[field_name]
            name, kind = _annotated_name(value)
            if name is None:
                _log.warning("node %s (%s) field %r: %s file annotation; not staged", node_id, class_type,
                             field_name, kind)
                continue
            rel = safe_relative_path(name)
            if rel is None:
                problems.append(Problem("input_path_blocked",
                                        "only files inside the ComfyUI input folder can be sent (absolute paths, "
                                        "drive letters, network paths and '..' are not allowed)",
                                        node_id, class_type, field_name))
                continue
            path, reason = _check_under(input_dir, rel)
            if path is None:
                problems.append(Problem("input_path_blocked", f"the input file {reason}", node_id, class_type,
                                        field_name))
                continue
            if path.is_dir():
                for member in sorted(path.iterdir(), key=lambda p: p.name):
                    if _is_link(member):
                        problems.append(Problem("input_path_blocked",
                                                f"'{rel}/{member.name}' is a link; links are never followed",
                                                node_id, class_type, field_name))
                        continue
                    if member.is_file():
                        dest = f"{rel}/{member.name}"
                        planned.setdefault(dest, StagedInput(node_id, class_type, field_name, member, dest,
                                                             member.stat().st_size))
                continue
            if not path.is_file():
                if require_exists:
                    problems.append(Problem("input_missing",
                                            f"'{_short(rel)}' is not in the ComfyUI input folder",
                                            node_id, class_type, field_name))
                else:
                    planned.setdefault(rel, StagedInput(node_id, class_type, field_name, path, rel, None))
                continue
            planned.setdefault(rel, StagedInput(node_id, class_type, field_name, path, rel, path.stat().st_size))
    if problems:
        for p in problems:
            _log.warning("input refused: node %s (%s) %r [%s]", p.node_id, p.class_type, p.input, p.code)
        raise InputPlanError(problems)
    return list(planned.values())


def _local_path_widgets(class_type: str, inputs: dict, defs: NodeDefs) -> list[str]:
    """STRING widgets that take a path on this computer (VHS `vhs_path_extensions` hint), with a non-URL value."""
    out = []
    for name, spec in defs.specs(class_type).items():
        value = inputs.get(name)
        if spec.kind == "STRING" and "vhs_path_extensions" in spec.extra and isinstance(value, str) and value.strip():
            if not re.match(r"^[a-z][a-z0-9+.-]*://", value.strip(), re.I) or value.strip().lower().startswith("file:"):
                out.append(name)
    return out


# ---- model discovery (C2) ------------------------------------------------------------------------------------

def cloud_folder(key: str) -> str:
    return FOLDER_ALIASES.get(key, key)


class ModelIndex:
    """Reverse index of the local model folders: normalised file name -> folder_paths keys that list it."""

    def __init__(self, by_name: dict[str, list[str]] | None = None, folders: Iterable[str] = ()):
        self.by_name = {k: sorted(set(v)) for k, v in (by_name or {}).items()}
        self.folders = sorted(set(folders))

    def folders_for(self, name: str) -> list[str]:
        return self.by_name.get(_norm(name), [])


_index_lock = threading.Lock()
_index_cache: tuple[float, ModelIndex] | None = None
_INDEX_TTL_S = 30.0


def local_model_index(*, max_age_s: float = _INDEX_TTL_S) -> ModelIndex:
    """Build (or reuse, for up to *max_age_s*) the local model index from ComfyUI's folder_paths. get_filename_list
    is ComfyUI's own cached listing (the same one /object_info uses). Empty outside ComfyUI."""
    global _index_cache
    with _index_lock:
        if _index_cache and time.monotonic() - _index_cache[0] < max_age_s:
            return _index_cache[1]
    try:
        import folder_paths  # ComfyUI-provided
        keys = [k for k in dict(folder_paths.folder_names_and_paths) if k not in _NOT_MODEL_FOLDERS]
    except Exception:  # noqa: BLE001 - outside ComfyUI or a stub without the attribute
        return ModelIndex()
    by_name: dict[str, list[str]] = {}
    for key in keys:
        try:
            names = folder_paths.get_filename_list(key)
        except Exception:  # noqa: BLE001 - one broken custom folder must not sink the index
            continue
        for name in names or ():
            if isinstance(name, str) and name.lower().endswith(MODEL_EXTS):
                by_name.setdefault(_norm(name), []).append(key)
    index = ModelIndex(by_name, keys)
    with _index_lock:
        _index_cache = (time.monotonic(), index)
    return index


def _norm(name: str) -> str:
    return name.replace("\\", "/").strip("/")


def _pick_folder(candidates: list[str], preferred: str | None) -> str | None:
    folders = sorted({cloud_folder(k) for k in candidates})
    if not folders:
        return preferred
    if preferred in folders:
        return preferred
    for canonical in _CANONICAL_ORDER:
        if canonical in folders:
            return canonical
    return folders[0]


def discover_models(workflow: dict, *, node_defs: NodeDefs | None = None,
                    index: ModelIndex | None = None) -> list[ModelRef]:
    """Every model file the workflow references, deduplicated on (folder, name).

    A widget value is a model reference when (1) the local model index lists it (its folder comes from the folder
    that lists it: discovered), else (2) the fallback table names the node + field (table), else (3) it is a combo
    or unknown-definition value with a model file extension (heuristic; folder None unless the table knows it).
    Input-file widgets (upload hints) are never model references."""
    from .model_sync import MODEL_FILE_NODES  # fallback table; model_sync never imports this module at load

    defs = node_defs or NodeDefs(local_node_defs(_class_types(workflow)), _default_snapshot())
    index = index if index is not None else local_model_index()
    refs: list[ModelRef] = []
    seen: set = set()
    for node_id, node in _nodes(workflow):
        class_type, inputs = node["class_type"], node["inputs"]
        specs = defs.specs(class_type)
        table = MODEL_FILE_NODES.get(class_type, {})
        upload_fields = {f for f, _ in _input_file_fields(class_type, inputs, defs)}
        for field_name, value in inputs.items():
            if not isinstance(value, str) or field_name in upload_fields:
                continue
            name = _norm(value)
            spec = specs.get(field_name)
            candidates = index.folders_for(name)
            if candidates:
                ref = ModelRef(node_id, class_type, field_name, _pick_folder(candidates, table.get(field_name)),
                               name, "discovered", True)
            elif field_name in table and name.lower().endswith(MODEL_EXTS):
                ref = ModelRef(node_id, class_type, field_name, table[field_name], name, "table", False)
            elif name.lower().endswith(MODEL_EXTS) and (spec is None or spec.kind == "COMBO"):
                ref = ModelRef(node_id, class_type, field_name, None, name, "heuristic", False)
            else:
                continue
            if ref.key() not in seen:
                seen.add(ref.key())
                refs.append(ref)
    return refs


def allowed_cloud_folders(index: ModelIndex | None = None) -> set[str]:
    """Folders a model may be uploaded to: the runner's fixed list plus the canonical names of local model folders."""
    from .model_sync import CLOUD_MODEL_SUBDIRS

    index = index if index is not None else local_model_index()
    folders = set(CLOUD_MODEL_SUBDIRS)
    folders |= {cloud_folder(k) for k in index.folders if _FOLDER_NAME_RE.match(cloud_folder(k))}
    return folders - set(_NOT_MODEL_FOLDERS)


# ---- preflight / Workflow Check (G3) -------------------------------------------------------------------------

LibraryCheck = Callable[[ModelRef], "int | None | bool"]


def _nodes(workflow: dict):
    for node_id, node in workflow.items():
        if isinstance(node, dict) and isinstance(node.get("class_type"), str) and isinstance(node.get("inputs"), dict):
            yield str(node_id), node


def _class_types(workflow) -> set[str]:
    return {n["class_type"] for _, n in _nodes(workflow)} if isinstance(workflow, dict) else set()


def _short(text, limit: int = 120) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _is_file_combo(options: tuple) -> bool:
    return not options or any(isinstance(o, str) and o.lower().endswith(MODEL_EXTS + _MEDIA_EXTS) for o in options)


def _is_link_value(value) -> bool:
    return isinstance(value, list) and len(value) == 2 and isinstance(value[1], int)


def _default_snapshot() -> Snapshot | None:
    try:
        return load_snapshot()
    except ValueError:
        return None


def _check_values(node_id, class_type, inputs, cloud_specs, blocking) -> None:
    for name, spec in cloud_specs.items():
        value = inputs.get(name)
        if value is None or _is_link_value(value):
            continue
        if spec.kind == "COMBO" and spec.options and not _is_file_combo(spec.options):
            # A file value is checked by model / input discovery, never against a snapshot list: file combos can
            # keep non-file entries after the producer emptied the files (VAELoader keeps "pixel_space").
            looks_like_file = isinstance(value, str) and value.lower().endswith(MODEL_EXTS + _MEDIA_EXTS)
            if isinstance(value, (str, int, float)) and value not in spec.options and not looks_like_file:
                sample = ", ".join(str(o) for o in spec.options[:8])
                blocking.append(Problem("invalid_value", f"'{_short(value, 60)}' is not one of: {sample}"
                                        + (" ..." if len(spec.options) > 8 else ""), node_id, class_type, name))
        elif spec.kind in ("INT", "FLOAT") and isinstance(value, (int, float)) and not isinstance(value, bool):
            lo, hi = spec.extra.get("min"), spec.extra.get("max")
            if isinstance(lo, (int, float)) and value < lo:
                blocking.append(Problem("invalid_value", f"{value} is below the minimum {lo}", node_id, class_type,
                                        name))
            elif isinstance(hi, (int, float)) and value > hi:
                blocking.append(Problem("invalid_value", f"{value} is above the maximum {hi}", node_id, class_type,
                                        name))


def preflight(workflows: list[dict], *, snapshot: Snapshot | None | str = "default",
              node_defs: NodeDefs | None = None, input_dir: Path | None = None, index: ModelIndex | None = None,
              library: LibraryCheck | None = None, models_check: "ModelsCheck | None" = None) -> dict:
    """Workflow Check: validate *workflows* without starting any paid compute.

    Blocking: unknown node classes (with the node list), classes from a pack that failed / is not installed in the
    profile, missing required inputs, broken links, invalid enum values and out-of-range numbers (against the
    snapshot definitions, which are what the cloud runs), blocked / missing input files, local-path widgets, models
    found neither locally nor in the cloud library, workflows without an output node, UI-format JSON.
    *library(ref)* returns the cloud size, True/False (present without size / absent) or raises; None skips the cloud
    check (reported as a warning). *models_check(refs)* (the bridge uses model_sync.classify_models) replaces
    *library* with the full model-sync classification (sizes, upload limit, uploads in flight).
    Returns the C-4 section 5 shape (no host or URL in any field)."""
    started = time.perf_counter()
    if snapshot == "default":
        snapshot = _default_snapshot()
    blocking: list[Problem] = []
    warnings: list[Problem] = []
    models: dict[str, list] = {"missing": [], "uploadable": [], "over_limit": [], "uploading": [], "present": [],
                               "unverified": []}
    checked = {"nodes": 0, "models": 0, "inputs": 0}
    if snapshot is None:
        warnings.append(Problem("no_snapshot", "No runtime profile snapshot is installed, so node availability in "
                                               "the cloud was not verified."))
    all_types: set[str] = set()
    for wf in workflows:
        all_types |= _class_types(wf)
    defs = node_defs or NodeDefs(local_node_defs(all_types), snapshot if isinstance(snapshot, Snapshot) else None)
    index = index if index is not None else local_model_index()
    model_refs: dict[tuple, ModelRef] = {}

    for wf_no, wf in enumerate(workflows, start=1):
        if not isinstance(wf, dict) or not wf:
            blocking.append(Problem("bad_workflow", f"workflow {wf_no} is empty or not an object"))
            continue
        if "nodes" in wf and "links" in wf:
            blocking.append(Problem("ui_format", f"workflow {wf_no} is in the editor (UI) format; HaloFuse needs the "
                                                 "API format the panel sends"))
            continue
        node_ids = {str(k) for k in wf}
        unknown: list[tuple[str, str]] = []
        has_output = False
        for node_id, node in _nodes(wf):
            checked["nodes"] += 1
            class_type, inputs = node["class_type"], node["inputs"]
            if snapshot is not None:
                status, pack = snapshot.class_status(class_type)
                if status == "unknown":
                    unknown.append((node_id, class_type))
                elif status in ("failed", "not_installed"):
                    reason = _short((pack or {}).get("error") or status, 200)
                    blocking.append(Problem("node_unavailable",
                                            f"{class_type} comes from {pack.get('repo')}, which is {status.replace('_', ' ')} "
                                            f"in the '{snapshot.profile}' runtime ({reason})", node_id, class_type))
                elif status == "unverified":
                    warnings.append(Problem("node_unverified", f"{class_type}: availability in the "
                                                               f"'{snapshot.profile}' runtime is not verified yet",
                                            node_id, class_type))
            node_def = defs.node_def(class_type)
            if node_def and node_def.get("output_node"):
                has_output = True
            specs = defs.specs(class_type)
            for name, spec in specs.items():
                if spec.required and name not in inputs:
                    blocking.append(Problem("missing_input", "required input is not set", node_id, class_type, name))
            for name, value in inputs.items():
                if _is_link_value(value) and str(value[0]) not in node_ids:
                    blocking.append(Problem("broken_link", f"connected to node {value[0]}, which is not in the "
                                                           "workflow", node_id, class_type, name))
            cloud_def = defs.cloud_def(class_type)
            if cloud_def is not None:
                _check_values(node_id, class_type, inputs, parse_input_specs(cloud_def), blocking)
            for name in _local_path_widgets(class_type, inputs, defs):
                blocking.append(Problem("local_path_input", "this is a path on this computer, which the cloud cannot "
                                                            "read; use the upload variant of the loader",
                                        node_id, class_type, name))
        if unknown:
            listed = ", ".join(f"{ct} (node {nid})" for nid, ct in unknown[:20])
            for nid, ct in unknown:
                blocking.append(Problem("unknown_node", f"{ct} is not available in the '{snapshot.profile}' runtime",
                                        nid, ct))
            warnings.append(Problem("unknown_nodes_summary", f"Nodes not in the runtime: {listed}"))
        if not has_output and all(defs.node_def(n["class_type"]) for _, n in _nodes(wf)):
            blocking.append(Problem("no_output", f"workflow {wf_no} has no output node (Save / Preview), so it "
                                                 "would produce nothing"))
        if input_dir is not None:
            try:
                staged = plan_inputs(wf, input_dir, node_defs=defs, require_exists=True)
                checked["inputs"] += len(staged)
            except InputPlanError as exc:
                blocking.extend(exc.problems)
        for ref in discover_models(wf, node_defs=defs, index=index):
            model_refs.setdefault(ref.key(), ref)

    checked["models"] = len(model_refs)
    if models_check is not None:
        _apply_models_check(list(model_refs.values()), models_check, models, blocking, warnings)
        return _report(blocking, warnings, models, checked, snapshot, started)
    library_error = None
    for ref in model_refs.values():
        item = {"node_id": ref.node_id, "class_type": ref.class_type, "field": ref.field, "folder": ref.folder,
                "name": ref.name, "source": ref.source, "local": ref.local}
        remote = None
        if library is not None and ref.folder is not None and library_error is None:
            try:
                remote = library(ref)
            except Exception as exc:  # noqa: BLE001 - branded message only
                library_error = getattr(exc, "user_message", None) or brand.message("unknown")
        if remote not in (None, False):
            models["present"].append(item)
        elif ref.local:
            models["uploadable"].append(item)
            warnings.append(Problem("model_not_in_library", f"{ref.name} is on this computer but not in the cloud "
                                                            "model library yet", ref.node_id, ref.class_type,
                                    ref.field))
        elif ref.folder is None:
            models["unverified"].append(item)
            warnings.append(Problem("model_unverified", f"{ref.name}: model folder unknown, so it could not be "
                                                        "checked", ref.node_id, ref.class_type, ref.field))
        elif library is None or library_error is not None:
            models["unverified"].append(item)
            warnings.append(Problem("model_unverified", f"{ref.name} is not on this computer and the cloud library "
                                                        "was not checked", ref.node_id, ref.class_type, ref.field))
        else:
            models["missing"].append(item)
            blocking.append(Problem("missing_model", f"{ref.name} ({ref.folder}) is neither on this computer nor in "
                                                     "the cloud model library", ref.node_id, ref.class_type, ref.field))
    if library_error is not None:
        warnings.append(Problem("library_unavailable", library_error))
    elif library is None and model_refs:
        warnings.append(Problem("library_not_checked", "The cloud model library was not checked."))
    return _report(blocking, warnings, models, checked, snapshot, started)


ModelsCheck = Callable[[list], dict]


def _apply_models_check(refs: list, models_check: "ModelsCheck", models: dict, blocking: list,
                        warnings: list) -> None:
    """Merge a model_sync.classify_models-style result (sizes, upload guard, in-flight uploads) into the report."""
    try:
        result = models_check(refs) if refs else {}
    except Exception as exc:  # noqa: BLE001 - branded text only; the check degrades to a warning
        warnings.append(Problem("library_unavailable", getattr(exc, "user_message", None) or brand.message("unknown")))
        models["unverified"].extend({"node_id": r.node_id, "class_type": r.class_type, "field": r.field,
                                     "folder": r.folder, "name": r.name} for r in refs)
        return
    for key, items in result.items():
        models.setdefault(key, [])
        models[key].extend(items)
    for item in result.get("missing", []):
        blocking.append(Problem("missing_model", f"{item['name']} ({item['folder']}) is neither on this computer nor "
                                                 "in the cloud model library", item.get("node_id"),
                                item.get("class_type"), item.get("field")))
    for item in result.get("invalid", []):
        blocking.append(Problem("model_path_blocked", f"'{_short(item['name'], 80)}' is not a valid model library "
                                                      "path", item.get("node_id"), item.get("class_type"),
                                item.get("field")))
    for item in result.get("uploadable", []):
        text = ("differs from the cloud library copy" if item.get("size_mismatch")
                else "is on this computer but not in the cloud model library yet")
        warnings.append(Problem("model_not_in_library", f"{item['name']} {text}", item.get("node_id"),
                                item.get("class_type"), item.get("field")))
    for item in result.get("over_limit", []):
        warnings.append(Problem("model_over_upload_limit", item.get("message") or f"{item['name']} is larger than "
                                "the upload limit", item.get("node_id"), item.get("class_type"), item.get("field")))
    for item in result.get("unverified", []):
        warnings.append(Problem("model_unverified", item.get("message") or f"{item['name']} could not be checked",
                                item.get("node_id"), item.get("class_type"), item.get("field")))


def _report(blocking, warnings, models, checked, snapshot, started) -> dict:
    return {
        "ok": not blocking,
        "blocking": [p.to_dict() for p in blocking],
        "warnings": [p.to_dict() for p in warnings],
        "models": models,
        "checked": checked,
        "profile": snapshot.profile if isinstance(snapshot, Snapshot) else None,
        "snapshot": snapshot.summary() if isinstance(snapshot, Snapshot) else None,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
    }


__all__ = [
    "FALLBACK_INPUT_FILE_NODES", "InputPlanError", "InputSpec", "MODEL_EXTS", "ModelIndex", "ModelRef", "NodeDefs",
    "Problem", "Snapshot", "StagedInput", "allowed_cloud_folders", "cloud_folder", "discover_models",
    "load_snapshot", "local_model_index", "local_node_defs", "parse_input_specs", "plan_inputs", "preflight",
    "safe_relative_path",
]

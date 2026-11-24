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
    rewrite_input_values(workflow, *, node_defs=None) -> int      (the cloud workflow names the staged files)
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
    source: str                 # "discovered" (local file lists), "table" (fallback map), "heuristic",
                                # "node_download" (model_sync.NODE_DOWNLOADED_MODELS)
    local: bool = False         # a local file was found for it
    downloads: bool = False     # the node downloads the file itself in the cloud when it is not there

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
    runner_capabilities: frozenset = frozenset()   # what the image's runner can do (e.g. user_nodes/1)

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
                   pack_of=pack_of, packs=packs, catalog_sha256=data.get("catalog_sha256"),
                   runner_capabilities=frozenset(c for c in (data.get("runner_capabilities") or ())
                                                 if isinstance(c, str)))

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


# ComfyUI's annotated file names (folder_paths.annotated_filepath, read in ComfyUI 0.39.0): a value ending in
# "[output]" / "[input]" / "[temp]" loses that suffix plus the space before it (9 / 8 / 7 characters) and is
# resolved against that folder; any other value is resolved against the input folder (get_annotated_filepath's
# default). LoadImage, LoadImageOutput, VHS_LoadVideo(FFmpeg) and LoadVideo all load through this.
_ANNOTATIONS = (("[output]", "output", 9), ("[input]", "input", 8), ("[temp]", "temp", 7))
# The cloud job has one input folder. A file the workflow takes from the local output / temp folder is staged into
# it under this reserved sub-folder, and the cloud workflow names it there (rewrite_input_values). The mapping is
# reversible - planning the rewritten value finds the same local file - so submit can rewrite before it stages.
STAGED_FOLDERS = {"output": "halofuse-output", "temp": "halofuse-temp"}
_FROM_COMFY = object()     # plan_inputs default: take the output / temp folder from ComfyUI's folder_paths


def _split_annotation(value: str) -> tuple[str, str | None]:
    """(name, folder kind or None) exactly as ComfyUI's folder_paths.annotated_filepath splits it."""
    for suffix, kind, cut in _ANNOTATIONS:
        if value.endswith(suffix):
            return value[:-cut], kind
    return value, None


def _source_of(value: str) -> tuple[str, str, str] | None:
    """(folder kind, path relative to that folder, staged path relative to the cloud input folder) for a file
    widget value, or None when the value could point anywhere else (X2: see safe_relative_path)."""
    name, kind = _split_annotation(value)
    rel = safe_relative_path(name)
    if rel is None:
        return None
    if kind in ("output", "temp"):
        return kind, rel, f"{STAGED_FOLDERS[kind]}/{rel}"
    head, _, rest = rel.partition("/")
    for staged_kind, folder in STAGED_FOLDERS.items():
        if head == folder and rest:
            return staged_kind, rest, rel      # a value rewritten by rewrite_input_values
    return "input", rel, rel


def _comfy_dir(kind: str) -> Path | None:
    """ComfyUI's output / temp folder, None outside ComfyUI."""
    try:
        import folder_paths  # ComfyUI-provided
        getter = {"output": folder_paths.get_output_directory, "temp": folder_paths.get_temp_directory}[kind]
        value = getter()
    except Exception:  # noqa: BLE001 - outside ComfyUI (tests, tools)
        return None
    return Path(value) if isinstance(value, str) and value else None


def _input_file_fields(class_type: str, inputs: dict, defs: NodeDefs) -> list[tuple[str, str]]:
    """[(field, how)] of the node's widgets that name a file the node loads from ComfyUI's input folder (or, with
    an annotation, from the output / temp folder).

    how = "upload_hint" (a combo with an image/video/audio upload hint in its node definition) or "table" (the
    fallback list, for loaders that upload from their own JS without a hint, e.g. VideoHelperSuite)."""
    specs = defs.specs(class_type)
    fields: list[tuple[str, str]] = []
    for name, spec in specs.items():
        if spec.kind != "COMBO" or not isinstance(inputs.get(name), str):
            continue
        if any(spec.extra.get(h) for h in UPLOAD_HINTS):
            # LoadImageOutput (image_folder "output") lists "name [output]" values; core loads every one of them
            # through get_annotated_filepath, so the annotation - not the hint - says which folder holds the file.
            fields.append((name, "upload_hint"))
    listed = FALLBACK_INPUT_FILE_NODES.get(class_type, [])
    for name in listed:
        if name not in {f for f, _ in fields} and isinstance(inputs.get(name), str):
            fields.append((name, "table"))
    return fields


_UPLOAD_ACTION = "pick the file with the node's upload button, which copies it into the ComfyUI input folder"
_NEAR_MISS_MAX_DIRS = 200


def _near_miss(kind: str, rel: str, dirs: dict) -> str | None:
    """Why a missing input file is missing, when a quick bounded look finds it elsewhere (never an absolute path):
    in the output folder, under another upper/lower case, or in a sub-folder of the input folder."""
    if kind != "input":
        return None
    base = dirs.get("input")
    out = dirs.get("output")
    try:
        in_output = _check_under(out, rel)[0] if out is not None else None
        if in_output is not None and in_output.is_file():
            return (f"It is in the ComfyUI output folder, not in the input folder: {_UPLOAD_ACTION} "
                    "(or copy it into the input folder), then run the check again.")
        parent_rel, _, file_name = rel.rpartition("/")
        parent = base.resolve().joinpath(*parent_rel.split("/")) if parent_rel else base.resolve()
        if parent.is_dir() and not _is_link(parent):
            for entry in os.scandir(parent):
                if entry.name.lower() == file_name.lower() and entry.name != file_name and entry.is_file():
                    other = f"{parent_rel}/{entry.name}" if parent_rel else entry.name
                    return (f"The input folder has '{_short(other)}' (different upper/lower case): pick that "
                            "entry in the node, then run the check again.")
        if not parent_rel:
            subs = sorted(e.name for e in os.scandir(base.resolve())
                          if e.is_dir(follow_symlinks=False) and not _is_link(Path(e.path)))[:_NEAR_MISS_MAX_DIRS]
            for sub in subs:
                if (base.resolve() / sub / file_name).is_file():
                    return (f"The input folder has it as '{_short(sub + '/' + file_name)}': pick that entry in the "
                            "node, then run the check again.")
    except OSError:
        return None
    return None


def _missing_message(kind: str, rel: str, dirs: dict) -> str:
    text = f"'{_short(rel)}' is not in the ComfyUI {kind} folder."
    hint = _near_miss(kind, rel, dirs)
    if hint:
        return f"{text} {hint}"
    if kind == "input":
        # owner 2026-11-23: say plainly that it is nowhere on this computer (not in the output folder, not under
        # another case, not in a sub-folder), so ComfyUI here could not load it either - usually a workflow from
        # another computer
        return (f"{text} It is not anywhere in this ComfyUI's input or output folders, so ComfyUI on this computer "
                "could not load it either (the workflow was probably made on another computer). Get the file, then "
                f"{_UPLOAD_ACTION}, and run the check again.")
    return (f"{text} Copy it into the ComfyUI input folder and pick it in the node again (the node's upload "
            "button does both), then run the check again.")


def plan_inputs(workflow: dict, input_dir: Path, *, node_defs: NodeDefs | None = None,
                require_exists: bool = False, output_dir=_FROM_COMFY, temp_dir=_FROM_COMFY) -> list[StagedInput]:
    """Hook H-a (REQUESTS R-WS2-03): the input files *workflow* references, as copies to stage into the job input.

    A value is resolved exactly as ComfyUI resolves it (folder_paths.get_annotated_filepath): "name [output]" /
    "name [temp]" in the output / temp folder, anything else ("name", "name [input]", "sub/name", "sub\\name") in
    the input folder. Output / temp files are staged under STAGED_FOLDERS (rewrite_input_values renames the value in
    the cloud workflow to match); *output_dir* / *temp_dir* default to ComfyUI's folders.
    Never reads outside those folders: a value that is absolute, has a drive letter or UNC prefix, contains `..`, or
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
    dirs = {"input": Path(input_dir),
            "output": _comfy_dir("output") if output_dir is _FROM_COMFY else (Path(output_dir) if output_dir else None),
            "temp": _comfy_dir("temp") if temp_dir is _FROM_COMFY else (Path(temp_dir) if temp_dir else None)}
    problems: list[Problem] = []
    planned: dict[str, StagedInput] = {}
    for node_id, node in _nodes(workflow):
        class_type = node["class_type"]
        inputs = node["inputs"]
        for field_name, _how in _input_file_fields(class_type, inputs, defs):
            value = inputs[field_name]
            source = _source_of(value)
            if source is None:
                problems.append(Problem("input_path_blocked",
                                        "only files inside the ComfyUI input folder can be sent (absolute paths, "
                                        f"drive letters, network paths and '..' are not allowed): {_UPLOAD_ACTION}",
                                        node_id, class_type, field_name))
                continue
            kind, rel, dest_rel = source
            base = dirs[kind]
            if base is None:
                if require_exists:
                    problems.append(Problem("input_missing", f"'{_short(rel)}' is in the ComfyUI {kind} folder, "
                                            f"which {brand.PRODUCT} cannot read here: copy the file into the ComfyUI "
                                            "input folder and pick it in the node again", node_id, class_type,
                                            field_name))
                else:
                    _log.warning("node %s (%s) field %r: the %s folder is unknown; not staged", node_id, class_type,
                                 field_name, kind)
                continue
            path, reason = _check_under(base, rel)
            if path is None:
                problems.append(Problem("input_path_blocked", f"the file {reason}: copy the file itself into the "
                                        "ComfyUI input folder and pick it in the node", node_id, class_type,
                                        field_name))
                continue
            if path.is_dir():
                for member in sorted(path.iterdir(), key=lambda p: p.name):
                    if _is_link(member):
                        problems.append(Problem("input_path_blocked",
                                                f"'{rel}/{member.name}' is a link; links are never followed: copy "
                                                "the file itself into the folder", node_id, class_type, field_name))
                        continue
                    if member.is_file():
                        dest = f"{dest_rel}/{member.name}"
                        planned.setdefault(dest, StagedInput(node_id, class_type, field_name, member, dest,
                                                             member.stat().st_size))
                continue
            if not path.is_file():
                if require_exists:
                    problems.append(Problem("input_missing", _missing_message(kind, rel, dirs), node_id, class_type,
                                            field_name))
                else:
                    planned.setdefault(dest_rel, StagedInput(node_id, class_type, field_name, path, dest_rel, None))
                continue
            planned.setdefault(dest_rel, StagedInput(node_id, class_type, field_name, path, dest_rel,
                                                     path.stat().st_size))
    if problems:
        for p in problems:
            _log.warning("input refused: node %s (%s) %r [%s]", p.node_id, p.class_type, p.input, p.code)
        raise InputPlanError(problems)
    return list(planned.values())


def cloud_input_values(workflow: dict, *, node_defs: NodeDefs | None = None) -> dict[tuple[str, str], str]:
    """{(node_id, field): value} for each input-file widget the cloud must read under another name than the local
    value: the staged path plan_inputs gives it (forward slashes on the Linux runtime, no annotation, output / temp
    files under STAGED_FOLDERS). Pure: no filesystem access. Blocked values (X2) are left alone - plan_inputs
    refuses them before anything is sent."""
    if not isinstance(workflow, dict):
        return {}
    defs = node_defs if node_defs is not None else NodeDefs(local_node_defs(_class_types(workflow)),
                                                            _default_snapshot())
    out: dict[tuple[str, str], str] = {}
    for node_id, node in _nodes(workflow):
        for field_name, _how in _input_file_fields(node["class_type"], node["inputs"], defs):
            value = node["inputs"][field_name]
            source = _source_of(value)
            if source is not None and source[2] != value:
                out[(node_id, field_name)] = source[2]
    return out


def rewrite_input_values(workflow: dict, *, node_defs: NodeDefs | None = None) -> int:
    """Rename input-file values in *workflow* (in place) to the names plan_inputs stages them under, so the cloud
    loads exactly the staged file. Idempotent. Returns the number of values changed."""
    changes = cloud_input_values(workflow, node_defs=node_defs)
    for (node_id, field_name), value in changes.items():
        node = workflow.get(node_id)
        if node is None:                    # API ids are strings, but a caller may have built int keys
            node = next(n for k, n in workflow.items() if str(k) == node_id)
        node["inputs"][field_name] = value
    return len(changes)


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

    A widget value is a model reference when (0) the node downloads its own weights (model_sync
    NODE_DOWNLOADED_MODELS / NODE_DOWNLOAD_PACKS: node_download, with the models/ sub-folder the node reads first or
    None), else (1) the local model index lists it (its folder comes from the folder that lists it: discovered),
    else (2) the fallback table names the node + field (table), else (3) it is a combo or unknown-definition value
    with a model file extension (heuristic; folder None unless the table knows it).
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
        rule = node_download_rule(class_type, defs)
        upload_fields = {f for f, _ in _input_file_fields(class_type, inputs, defs)}
        for field_name, value in inputs.items():
            if not isinstance(value, str) or field_name in upload_fields:
                continue
            name = _norm(value)
            spec = specs.get(field_name)
            candidates = index.folders_for(name)
            if (rule is not None and name.lower().endswith(MODEL_EXTS)
                    and (rule.get("fields") is None or field_name in rule["fields"])):
                ref = ModelRef(node_id, class_type, field_name, rule.get("folder"), name, "node_download", False,
                               True)
            elif candidates:
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


def node_download_rule(class_type: str, defs: NodeDefs | None = None) -> dict | None:
    """The model_sync download rule ({"folder", "fields"}) of a node that fetches its own weights, by class name,
    else by the pack the runtime snapshot says the class comes from; None for every other node."""
    from .model_sync import NODE_DOWNLOAD_PACKS, NODE_DOWNLOADED_MODELS

    rule = NODE_DOWNLOADED_MODELS.get(class_type)
    snapshot = defs.snapshot if defs is not None else None
    if rule is None and snapshot is not None:
        pack = snapshot.pack_of.get(class_type)
        if isinstance(pack, dict):
            rule = NODE_DOWNLOAD_PACKS.get(pack.get("repo"))
    return rule


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


# V3 dynamic inputs (comfy_api io.Autogrow / DynamicCombo / DynamicSlot / DynamicGroup): the prompt stores their
# values under dotted names ("values.a", "values.b"), never under the declared name, and core expands them with
# get_finalized_class_inputs before it validates. Core Math Expression (ComfyMathExpression) is one.
DYNAMIC_INPUT_KINDS = frozenset({"COMFY_AUTOGROW_V3", "COMFY_DYNAMICCOMBO_V3", "COMFY_DYNAMICSLOT_V3",
                                 "COMFY_DYNAMICGROUP_V3"})


def _input_present(name: str, inputs: dict) -> bool:
    """A declared input is set when its own key or any of its dotted (V3 dynamic) sub-keys is in the prompt."""
    if name in inputs:
        return True
    prefix = name + "."
    return any(isinstance(k, str) and k.startswith(prefix) for k in inputs)


def _needed_nodes(workflow: dict, defs: "NodeDefs") -> set[str] | None:
    """The node ids core will validate and run: every output node plus everything that feeds one (core's
    validate_prompt checks only outputs and their inputs, recursively). A node whose definition is unknown counts
    as a possible output, so nothing is skipped on a guess. None when the workflow has no known output node (the
    caller then checks every node)."""
    nodes = dict(_nodes(workflow))
    roots = set()
    known_output = False
    for node_id, node in nodes.items():
        d = defs.node_def(node["class_type"])
        if d is None:
            roots.add(node_id)
        elif d.get("output_node"):
            roots.add(node_id)
            known_output = True
    if not known_output:
        return None
    needed: set[str] = set()
    stack = list(roots)
    while stack:
        node_id = stack.pop()
        if node_id in needed or node_id not in nodes:
            continue
        needed.add(node_id)
        for value in nodes[node_id]["inputs"].values():
            if _is_link_value(value):
                stack.append(str(value[0]))
    return needed


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
                                        + (" ..." if len(spec.options) > 8 else "")
                                        + ". Pick one of these values in the node.", node_id, class_type, name))
        elif spec.kind in ("INT", "FLOAT") and isinstance(value, (int, float)) and not isinstance(value, bool):
            lo, hi = spec.extra.get("min"), spec.extra.get("max")
            if isinstance(lo, (int, float)) and value < lo:
                blocking.append(Problem("invalid_value", f"{value} is below the minimum {lo}: set {lo} or more",
                                        node_id, class_type, name))
            elif isinstance(hi, (int, float)) and value > hi:
                blocking.append(Problem("invalid_value", f"{value} is above the maximum {hi}: set {hi} or less",
                                        node_id, class_type, name))


# The API prompt carries no wire for an input whose source is not sent: H2Vault's graphToPrompt hook removes a wire
# from a node INSIDE an open vault unless that node is one of the vault's outputs ("never send an inner id"), and the
# editor drops wires from muted nodes. Core then rejects the input exactly as this check does, so it stays blocking;
# the hint names the likely cause (owner 2026-11-24: GrowMaskWithBlur "mask" fed from inside a vault).
VAULT_CLASS_PREFIX = "H2Vault_ProtectedSubgraph"
VAULT_WIRE_HINT = (". This workflow has an H2Vault vault: a wire from a node inside the vault is not sent (only the "
                   "vault's outputs leave it), so connect this input to one of the vault's outputs, or add that value "
                   "to the vault's outputs. A wire from a muted node is not sent either")


def _node_label(node_id, class_type) -> str:
    """'Node 12 (DWPreprocessor)': warnings and notes are shown without the node column, so they name it."""
    if node_id is None:
        return str(class_type or "")
    return f"Node {node_id} ({class_type})" if class_type else f"Node {node_id}"


def preflight(workflows: list[dict], *, snapshot: Snapshot | None | str = "default",
              node_defs: NodeDefs | None = None, input_dir: Path | None = None, index: ModelIndex | None = None,
              library: LibraryCheck | None = None, models_check: "ModelsCheck | None" = None,
              extra_refs: Iterable[ModelRef] = (), output_dir=_FROM_COMFY, temp_dir=_FROM_COMFY) -> dict:
    """Workflow Check: validate *workflows* without starting any paid compute.

    Blocking: unknown node classes (with the node list), classes from a pack that failed / is not installed in the
    profile, missing required inputs, broken links, invalid enum values and out-of-range numbers (against the
    snapshot definitions, which are what the cloud runs), blocked / missing input files, local-path widgets, models
    found neither locally nor in the cloud library, workflows without an output node, UI-format JSON.
    *library(ref)* returns the cloud size, True/False (present without size / absent) or raises; None skips the cloud
    check (reported as a warning). *models_check(refs)* (the bridge uses model_sync.classify_models) replaces
    *library* with the full model-sync classification (sizes, upload limit, uploads in flight).
    An input file missing on a node that feeds no output is a warning, not a block: core never loads it, and submit
    skips a missing file (the same rule as required inputs, _needed_nodes).
    Models the node downloads itself in the cloud are listed in `notes` (information, nothing to do).
    Every message says what to do. Returns the C-4 section 5 shape (no host or URL in any field)."""
    started = time.perf_counter()
    if snapshot == "default":
        snapshot = _default_snapshot()
    blocking: list[Problem] = []
    warnings: list[Problem] = []
    notes: list[Problem] = []
    models: dict[str, list] = {"missing": [], "uploadable": [], "over_limit": [], "uploading": [], "present": [],
                               "unverified": [], "auto_download": [], "fetchable": [], "downloading": []}
    checked = {"nodes": 0, "models": 0, "inputs": 0}
    if snapshot is None:
        warnings.append(Problem("no_snapshot", "No runtime profile snapshot is installed, so node availability in "
                                               "the cloud was not verified. Nothing to do; if a node is missing in "
                                               "the cloud, the render stops with its name."))
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
                                                 "API format the panel sends: send it from the panel"))
            continue
        node_ids = {str(k) for k in wf}
        has_vault = any(n["class_type"].startswith(VAULT_CLASS_PREFIX) for _, n in _nodes(wf))
        unknown: list[tuple[str, str]] = []
        has_output = False
        needed = _needed_nodes(wf, defs)
        if needed is not None and len(needed) < len(node_ids):
            skipped = len([1 for _ in _nodes(wf)]) - len(needed)
            if skipped > 0:
                warnings.append(Problem("unused_nodes", f"{skipped} node(s) feed no output (Save / Preview), so they "
                                                        "will not run and were not checked. Nothing to do, unless "
                                                        "one of them should run: connect it to a Save or Preview "
                                                        "node."))
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
                                            f"in the '{snapshot.profile}' runtime ({reason}). Replace or remove the "
                                            "node, or ask your administrator to fix the pack in the runtime.",
                                            node_id, class_type))
                elif status == "unverified":
                    warnings.append(Problem("node_unverified", f"{_node_label(node_id, class_type)}: availability "
                                                               f"in the '{snapshot.profile}' runtime is not verified "
                                                               "yet. Nothing to do; if the node is missing there, the "
                                                               "render stops with its name.",
                                            node_id, class_type))
            node_def = defs.node_def(class_type)
            if node_def and node_def.get("output_node"):
                has_output = True
            for name in _local_path_widgets(class_type, inputs, defs):
                # every node, needed or not: submit stages the whole workflow's inputs (jobs._plan_inputs)
                blocking.append(Problem("local_path_input", "this is a path on this computer, which the cloud cannot "
                                                            "read; use the upload variant of the loader",
                                        node_id, class_type, name))
            if needed is not None and node_id not in needed:
                continue                # core never validates or runs it (see _needed_nodes)
            specs = defs.specs(class_type)
            for name, spec in specs.items():
                if spec.required and spec.kind not in DYNAMIC_INPUT_KINDS and not _input_present(name, inputs):
                    blocking.append(Problem("missing_input", "required input is not set: connect it or give it a "
                                                             "value" + (VAULT_WIRE_HINT if has_vault else ""),
                                            node_id, class_type, name))
            for name, value in inputs.items():
                if _is_link_value(value) and str(value[0]) not in node_ids:
                    blocking.append(Problem("broken_link", f"connected to node {value[0]}, which is not in the "
                                                           "workflow: connect the input again", node_id, class_type,
                                            name))
            cloud_def = defs.cloud_def(class_type)
            if cloud_def is not None:
                _check_values(node_id, class_type, inputs, parse_input_specs(cloud_def), blocking)
        if unknown:
            listed = ", ".join(f"{ct} (node {nid})" for nid, ct in unknown[:20])
            for nid, ct in unknown:
                blocking.append(Problem("unknown_node", f"{ct} is not available in the '{snapshot.profile}' runtime: "
                                                        "replace or remove the node, or ask your administrator to "
                                                        "add its pack to the runtime", nid, ct))
            warnings.append(Problem("unknown_nodes_summary", f"Nodes not in the runtime: {listed}"))
        if not has_output and all(defs.node_def(n["class_type"]) for _, n in _nodes(wf)):
            blocking.append(Problem("no_output", f"workflow {wf_no} has no output node (Save / Preview), so it "
                                                 "would produce nothing: add a Save or Preview node"))
        if input_dir is not None:
            try:
                staged = plan_inputs(wf, input_dir, node_defs=defs, require_exists=True, output_dir=output_dir,
                                     temp_dir=temp_dir)
                checked["inputs"] += len(staged)
            except InputPlanError as exc:
                for p in exc.problems:
                    if p.code == "input_missing" and needed is not None and p.node_id not in needed:
                        # core never loads it and submit skips a missing file: not worth stopping the render for
                        warnings.append(Problem("input_missing_unused",
                                                f"{_node_label(p.node_id, p.class_type)} feeds no output, so it will "
                                                f"not run: {p.message} Nothing to do unless the node should run.",
                                                p.node_id, p.class_type, p.input))
                    else:
                        blocking.append(p)
        for ref in discover_models(wf, node_defs=defs, index=index):
            model_refs.setdefault(ref.key(), ref)
    for ref in extra_refs:  # files the job needs beyond the graph (live preview decoders, C-4 6a)
        model_refs.setdefault(ref.key(), ref)

    checked["models"] = len(model_refs)
    if models_check is not None:
        _apply_models_check(list(model_refs.values()), models_check, models, blocking, warnings, notes)
        return _report(blocking, warnings, models, checked, snapshot, started, notes)
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
            warnings.append(Problem("model_not_in_library", model_not_in_library_message(ref.name), ref.node_id,
                                    ref.class_type, ref.field))
        elif ref.downloads:
            item["message"] = node_download_message(ref.node_id, ref.class_type, ref.name, ref.folder)
            models["auto_download"].append(item)
            notes.append(Problem("model_downloaded_by_node", item["message"], ref.node_id, ref.class_type, ref.field))
        elif ref.folder is None:
            models["unverified"].append(item)
            warnings.append(Problem("model_unverified", unknown_folder_message(ref.node_id, ref.class_type, ref.name),
                                    ref.node_id, ref.class_type, ref.field))
        elif library is None or library_error is not None:
            models["unverified"].append(item)
            warnings.append(Problem("model_unverified", f"{ref.name} is not on this computer and the cloud library "
                                                        "was not checked. Run the check again when the service is "
                                                        "reachable.", ref.node_id, ref.class_type, ref.field))
        else:
            models["missing"].append(item)
            blocking.append(Problem("missing_model", missing_model_message(ref.name, ref.folder), ref.node_id,
                                    ref.class_type, ref.field))
    if library_error is not None:
        warnings.append(Problem("library_unavailable", library_error))
    elif library is None and model_refs:
        warnings.append(Problem("library_not_checked", "The cloud model library was not checked. Run the check "
                                                       "again when the service is reachable."))
    return _report(blocking, warnings, models, checked, snapshot, started, notes)


# ---- artist-facing model messages: each one says what to do (shared with model_sync.classify_models) ----------

def node_download_message(node_id, class_type, name: str, folder: str | None) -> str:
    label = _node_label(node_id, class_type)
    if folder is None:
        return f"{label}: {name} - nothing to do: the node downloads it in the cloud when the render runs."
    return (f"{label}: {name} is not in the cloud model library ({folder}) - nothing to do: the node downloads it "
            f"in the cloud when the render runs. To save that download time on every render, put the file in "
            f"ComfyUI's models/{folder} folder on this computer; it is then uploaded once.")


def unknown_folder_message(node_id, class_type, name: str) -> str:
    return (f"{_node_label(node_id, class_type)}: {name} - the model folder could not be determined, so it was not "
            "checked. Nothing to do if the node downloads its own models; otherwise put the file in its ComfyUI "
            "models folder on this computer and run the check again, so it is found and uploaded.")


def missing_model_message(name: str, folder: str | None) -> str:
    return (f"{name} ({folder}) is neither on this computer nor in the cloud model library: put it in ComfyUI's "
            f"models/{folder} folder on this computer (it is then uploaded) or ask your administrator to add it to "
            "the cloud model library, then run the check again.")


def model_not_in_library_message(name: str, size_mismatch: bool = False) -> str:
    if size_mismatch:
        return (f"{name} differs from the cloud library copy: this computer's file is uploaded before the render "
                "starts (or you are asked first when automatic upload is off).")
    return (f"{name} is on this computer but not in the cloud model library yet: it is uploaded before the render "
            "starts (or you are asked first when automatic upload is off). Nothing else to do.")


ModelsCheck = Callable[[list], dict]


def _apply_models_check(refs: list, models_check: "ModelsCheck", models: dict, blocking: list,
                        warnings: list, notes: list | None = None) -> None:
    """Merge a model_sync.classify_models-style result (sizes, upload guard, in-flight uploads) into the report."""
    notes = notes if notes is not None else warnings
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
        # with source lookup (model_fetch) the item's own message names the sources searched and the fix
        blocking.append(Problem("missing_model", item.get("message") or missing_model_message(item["name"],
                                                                                              item["folder"]),
                                item.get("node_id"), item.get("class_type"), item.get("field")))
    for item in result.get("fetchable", []):
        warnings.append(Problem("model_will_download", item.get("message") or f"{item['name']} is downloaded first",
                                item.get("node_id"), item.get("class_type"), item.get("field")))
    for item in result.get("downloading", []):
        warnings.append(Problem("model_downloading", item.get("message") or f"{item['name']} is being downloaded",
                                item.get("node_id"), item.get("class_type"), item.get("field")))
    for item in result.get("invalid", []):
        blocking.append(Problem("model_path_blocked", f"'{_short(item['name'], 80)}' is not a valid model library "
                                                      "path: pick the model again in the node", item.get("node_id"),
                                item.get("class_type"), item.get("field")))
    for item in result.get("uploadable", []):
        warnings.append(Problem("model_not_in_library",
                                model_not_in_library_message(item["name"], bool(item.get("size_mismatch"))),
                                item.get("node_id"), item.get("class_type"), item.get("field")))
    for item in result.get("over_limit", []):
        warnings.append(Problem("model_over_upload_limit", item.get("message") or f"{item['name']} is larger than "
                                "the upload limit", item.get("node_id"), item.get("class_type"), item.get("field")))
    for item in result.get("unverified", []):
        warnings.append(Problem("model_unverified", item.get("message") or f"{item['name']} could not be checked",
                                item.get("node_id"), item.get("class_type"), item.get("field")))
    for item in result.get("auto_download", []):
        notes.append(Problem("model_downloaded_by_node", item.get("message") or node_download_message(
            item.get("node_id"), item.get("class_type"), item["name"], item.get("folder")),
            item.get("node_id"), item.get("class_type"), item.get("field")))


def _report(blocking, warnings, models, checked, snapshot, started, notes=()) -> dict:
    return {
        "ok": not blocking,
        "blocking": [p.to_dict() for p in blocking],
        "warnings": [p.to_dict() for p in warnings],
        "notes": [p.to_dict() for p in notes],      # information only: nothing for the artist to do
        "models": models,
        "checked": checked,
        "profile": snapshot.profile if isinstance(snapshot, Snapshot) else None,
        "snapshot": snapshot.summary() if isinstance(snapshot, Snapshot) else None,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
    }


__all__ = [
    "FALLBACK_INPUT_FILE_NODES", "InputPlanError", "InputSpec", "MODEL_EXTS", "ModelIndex", "ModelRef", "NodeDefs",
    "Problem", "STAGED_FOLDERS", "Snapshot", "StagedInput", "allowed_cloud_folders", "cloud_folder",
    "cloud_input_values", "discover_models", "load_snapshot", "local_model_index", "local_node_defs",
    "node_download_rule", "parse_input_specs", "plan_inputs", "preflight", "rewrite_input_values",
    "safe_relative_path",
]

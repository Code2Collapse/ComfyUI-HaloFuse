"""Output bookkeeping under /output (G1, X4, X6, A2).

`/output/_halofuse/` is reserved for runner files (render.log, events.jsonl, previews) and never counts as an
output. Manifest-mode collection keeps each file's relative subfolder (X4: `Path.rename` of basenames collided and
silently overwrote on Linux) and never overwrites an existing file.
"""
import os
import shutil
from pathlib import Path

RESERVED_DIR = "_halofuse"

_KINDS = {
    "image": {"png", "jpg", "jpeg", "webp", "gif", "bmp", "tif", "tiff", "exr", "hdr"},
    "video": {"mp4", "webm", "mov", "mkv", "avi"},
    "audio": {"wav", "mp3", "flac", "ogg", "m4a", "aac", "opus"},
    "latent": {"latent"},
    "mesh": {"glb", "gltf", "obj", "ply", "fbx", "stl"},
}


def kind_of(path) -> str:
    ext = Path(path).suffix.lower().lstrip(".")
    for kind, exts in _KINDS.items():
        if ext in exts:
            return kind
    return "other"


def _is_reserved(path: Path, output_dir: Path) -> bool:
    try:
        rel = path.relative_to(output_dir)
    except ValueError:
        return False
    return bool(rel.parts) and rel.parts[0] == RESERVED_DIR


def snapshot(output_dir) -> set:
    """Every output file under output_dir, excluding the reserved runner dir."""
    output_dir = Path(output_dir)
    found = set()
    if not output_dir.is_dir():
        return found
    for root, dirs, files in os.walk(output_dir):
        root_path = Path(root)
        if root_path == output_dir and RESERVED_DIR in dirs:
            dirs.remove(RESERVED_DIR)
        for name in files:
            found.add(root_path / name)
    return found


def list_outputs(output_dir) -> list:
    return sorted(snapshot(output_dir))


def _free_destination(dest: Path) -> Path:
    if not dest.exists():
        return dest
    n = 1
    while True:
        candidate = dest.with_name(f"{dest.stem}_{n}{dest.suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def _remove_empty_parents(start: Path, output_dir: Path) -> None:
    current = start
    while current != output_dir and output_dir in current.parents:
        if current.name == RESERVED_DIR and current.parent == output_dir:
            return
        try:
            current.rmdir()  # only succeeds when empty
        except OSError:
            return
        current = current.parent


def collect_new(before, output_dir, subdir_name) -> list:
    """Move files that appeared since `before` into output_dir/subdir_name/<relative path>."""
    output_dir = Path(output_dir)
    new_files = sorted(snapshot(output_dir) - set(before))
    if not new_files:
        return []
    subdir = output_dir / subdir_name
    moved = []
    for src in new_files:
        rel = src.relative_to(output_dir)
        dest = _free_destination(subdir / rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dest)  # dest was checked free just above; same filesystem
        moved.append(dest)
        _remove_empty_parents(src.parent, output_dir)
    return sorted(moved)


def windows_name_parts(name) -> list:
    """'SHOT\\SHOT_00001.mov' -> ['SHOT', 'SHOT_00001.mov']; [] when the name has no backslash or only unsafe parts."""
    if "\\" not in name:
        return []
    return [p for p in name.split("\\") if p not in ("", ".", "..")]


def fix_windows_names(output_dir) -> list:
    """Files whose NAME contains a backslash become the folders that were meant (owner 2026-11-25: a filename_prefix
    typed on Windows, "SHOT\\SHOT_V001", made one Linux file named 'SHOT\\SHOT_V001_00001.mov' in /output, which cloud
    storage and Windows downloads handle badly). Returns [(old, new)]; never raises."""
    output_dir = Path(output_dir)
    renamed = []
    for src in sorted(snapshot(output_dir)):
        parts = windows_name_parts(src.name)
        if not parts:
            continue
        try:
            dest = _free_destination(src.parent.joinpath(*parts))
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(src, dest)
            renamed.append((src, dest))
        except OSError:
            continue
    return renamed


UPLOAD_BYTES_PER_S = 4 * 1024 * 1024   # conservative cloud-storage upload rate for the post-render hold
MIN_HOLD_S, MAX_HOLD_S = 15.0, 900.0


def hold_seconds(total_bytes, env=None) -> float:
    """How long to keep the machine up after the outputs are written, so the storage sync can finish uploading
    them before the machine is shut down (owner 2026-11-25: a 193 MB .mov never reached storage, provider error
    output_drain_failed: "drain budget (60s) exhausted with pending writes still queued").
    HF_OUTPUT_HOLD_S overrides (0 = no hold)."""
    env = os.environ if env is None else env
    raw = str(env.get("HF_OUTPUT_HOLD_S") or "").strip()
    if raw:
        try:
            return max(0.0, min(MAX_HOLD_S, float(raw)))
        except ValueError:
            pass
    if total_bytes <= 0:
        return 0.0
    return max(MIN_HOLD_S, min(MAX_HOLD_S, total_bytes / UPLOAD_BYTES_PER_S))


def flush_outputs(output_dir, log=print, sleep=None, env=None) -> float:
    """fsync every output file, sync the filesystem, then hold for hold_seconds(). Returns the seconds held."""
    import time as _time
    sleep = sleep or _time.sleep
    files = list_outputs(output_dir)
    total = 0
    for f in files:
        try:
            total += f.stat().st_size
            with open(f, "rb") as fh:
                os.fsync(fh.fileno())
        except OSError:
            continue
    try:
        os.sync()
    except (AttributeError, OSError):
        pass
    hold = hold_seconds(total, env)
    if hold > 0:
        log(f"keeping the machine up {hold:.0f} s so cloud storage can receive {len(files)} output file(s) "
            f"({total / 1048576:.1f} MiB) before shutdown")
        sleep(hold)
    return hold


def _safe_part(value) -> bool:
    if not isinstance(value, str):
        return False
    if value.startswith(("/", "\\")) or (len(value) > 1 and value[1] == ":"):
        return False
    parts = value.replace("\\", "/").split("/")
    return ".." not in parts


def _items(history_outputs):
    for node_id, node_out in (history_outputs or {}).items():
        if not isinstance(node_out, dict):
            continue
        for value in node_out.values():
            if not isinstance(value, list):
                continue
            for item in value:
                if isinstance(item, dict) and "filename" in item:
                    yield str(node_id), item


def copy_temp_previews(history_outputs, temp_root, dest_dir, prompt_id) -> list:
    """Copy ComfyUI temp-type outputs (PreviewImage/PreviewMask/...) out of the temp dir before it is lost (A2)."""
    temp_root = Path(temp_root)
    dest_dir = Path(dest_dir)
    copied = []
    root_resolved = temp_root.resolve()
    for node_id, item in _items(history_outputs):
        if item.get("type") != "temp":
            continue
        filename, subfolder = item.get("filename"), item.get("subfolder") or ""
        if not _safe_part(filename) or not _safe_part(subfolder) or "/" in filename.replace("\\", "/"):
            continue
        src = temp_root / subfolder / filename
        try:
            resolved = src.resolve()
        except OSError:
            continue
        if root_resolved not in resolved.parents or not resolved.is_file():
            continue
        dest = dest_dir / f"{str(prompt_id)[:8]}_{node_id}_{filename}"
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(resolved, dest)
        except OSError:
            continue
        copied.append((dest, node_id))
    return copied


def node_for_outputs(history_outputs) -> dict:
    mapping = {}
    for node_id, item in _items(history_outputs):
        if item.get("type") != "output":
            continue
        subfolder = (item.get("subfolder") or "").replace("\\", "/").strip("/")
        key = f"{subfolder}/{item['filename']}" if subfolder else item["filename"]
        mapping[key] = node_id
    return mapping

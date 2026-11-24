"""Housekeeping between the workflows of one job (owner 2026-11-23: "auto-clean cache" on a warm cloud machine).

One cloud job runs every workflow of its manifest against ONE ComfyUI process. Between two workflows the runner
cleans what the next workflow cannot use, and keeps what makes it fast:

cleaned (``HF_CLEAN_BETWEEN``, default on)
  * ComfyUI's temp folder (PreviewImage & co.). The runner has already copied those files into
    /output/_halofuse/previews when the workflow finished, and ComfyUI clears the folder on every start anyway.
  * Staged input files that only finished workflows named (an input a later workflow still names is kept; a file
    no workflow names explicitly is kept too, because a node may read it by folder).
  * pip's download cache after the artist's own node packs were installed (the wheels are installed; the cache only
    fills the container disk) - only when this job's pip created it (it did not exist before the install), and
    never when HF_USER_NODES_CACHE points pip at a cache on purpose.
  * Partial model downloads (``*.incomplete`` in the Hugging Face cache) written during this job, only when the
    local disk is low.
kept
  * Loaded models (VRAM / RAM) and ComfyUI's execution cache: ComfyUI 0.39+ already drops cached node outputs under
    RAM pressure by itself (its default RAM-pressure cache).
  * Prefetched model files and the Hugging Face cache (models a node downloaded for a later workflow).
memory (``HF_MEMORY_MODE`` keep | auto | always; L9.340)
  * keep (default): models stay loaded for the next workflow.
  * auto: unload when GPU memory use (nvidia-smi, the fullest GPU) is at or above ``HF_VRAM_HIGH_PCT`` (default 90)
    after a workflow. Hysteresis: after an unload the next one is allowed only once a later check saw use below
    ``HF_VRAM_LOW_PCT`` (default 75), so a workflow that simply needs that much memory is not unloaded and reloaded
    after every workflow.
  * always (also ``HF_UNLOAD_BETWEEN=1``, the older switch): ``POST /free {"unload_models": true, "free_memory":
    true}`` after every workflow, so each workflow starts with empty VRAM (its models load again).
  * In keep and auto, ``/free`` is also sent when host RAM is critically low (measured from /proc/meminfo and the
    cgroup), because a host-RAM OOM kills the whole ComfyUI. ComfyUI's ``free_memory`` also unloads the models (core
    main.py: ``flags.get("unload_models", free_memory)``), so the log says the next workflow loads them again.
protected
  * The persistent model folders (``MODEL_BASE_DIR``, default /assets, and /models) are never cleaned: a removal
    below them is refused (an unfinished ``*.incomplete`` download excepted). Temporary-file cleanup never deletes
    model files.

Every action is printed as a ``[runner] housekeeping: ...`` line and one ``[hf]`` phase mark ``housekeeping``.
Nothing here raises: housekeeping never fails a job.
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path, PurePosixPath

ENV_CLEAN = "HF_CLEAN_BETWEEN"
ENV_UNLOAD = "HF_UNLOAD_BETWEEN"
ENV_PIP_CACHE = "HF_USER_NODES_CACHE"   # hf/user_nodes.py CACHE_ENV: a deliberate cache is never removed
ENV_MEMORY = "HF_MEMORY_MODE"
ENV_VRAM_HIGH = "HF_VRAM_HIGH_PCT"
ENV_VRAM_LOW = "HF_VRAM_LOW_PCT"
MEMORY_MODES = ("keep", "auto", "always")
DEFAULT_VRAM_HIGH, DEFAULT_VRAM_LOW = 90, 75
MODEL_ROOTS = ("/assets", "/models")     # persistent model folders (MODEL_BASE_DIR is added at run time)
PARTIAL_SUFFIX = ".incomplete"
# Host RAM is "critical" below this share of MemTotal (or of the cgroup limit), with an absolute floor.
RAM_CRITICAL_FRACTION = 0.05
RAM_CRITICAL_FLOOR_MB = 2048
# The local disk is "low" below this share of its size, with an absolute floor.
DISK_LOW_FRACTION = 0.05
DISK_LOW_FLOOR_MB = 5120
_MB = 2**20
_FALSE = ("0", "false", "no", "off")
_TRUE = ("1", "true", "yes", "on")


def flags_from_env(env=None) -> tuple:
    """(clean, unload) from HF_CLEAN_BETWEEN (default on) and HF_UNLOAD_BETWEEN (default off)."""
    env = os.environ if env is None else env
    clean = str(env.get(ENV_CLEAN, "1")).strip().lower() not in _FALSE
    unload = str(env.get(ENV_UNLOAD, "0")).strip().lower() in _TRUE
    return clean, unload


def _pct_env(env, key: str, default: int) -> int:
    try:
        value = int(str(env.get(key, "")).strip())
    except ValueError:
        return default
    return value if 10 <= value <= 99 else default


def memory_from_env(env=None) -> tuple:
    """(mode, high_pct, low_pct). HF_MEMORY_MODE wins when valid; else HF_UNLOAD_BETWEEN=1 means "always"."""
    env = os.environ if env is None else env
    mode = str(env.get(ENV_MEMORY, "")).strip().lower()
    if mode not in MEMORY_MODES:
        mode = "always" if flags_from_env(env)[1] else "keep"
    high = _pct_env(env, ENV_VRAM_HIGH, DEFAULT_VRAM_HIGH)
    low = _pct_env(env, ENV_VRAM_LOW, DEFAULT_VRAM_LOW)
    if low >= high:
        low = max(10, high - 5)
    return mode, high, low


def vram_used_pct(sample) -> float | None:
    """GPU memory use of the fullest GPU in a telemetry sample, in percent; None without nvidia-smi data."""
    best = None
    for gpu in (sample or {}).get("gpu") or ():
        used, total = gpu.get("mem_used_mb"), gpu.get("mem_total_mb")
        if isinstance(used, (int, float)) and isinstance(total, (int, float)) and total > 0:
            pct = 100.0 * used / total
            best = pct if best is None else max(best, pct)
    return best


def model_roots(env=None) -> list:
    env = os.environ if env is None else env
    roots = [Path(r) for r in MODEL_ROOTS]
    if env.get("MODEL_BASE_DIR"):
        roots.append(Path(env["MODEL_BASE_DIR"]))
    return roots


def _inside(path: Path, root: Path) -> bool:
    try:
        Path(os.path.abspath(path)).relative_to(os.path.abspath(root))
        return True
    except ValueError:
        return False


def protected(path, roots) -> bool:
    """True for a path in (or equal to) a persistent model folder, or a folder that holds one: never removed."""
    path = Path(path)
    return any(_inside(path, r) or _inside(r, path) for r in roots or ())


def _norm(value: str) -> str:
    text = value.strip().replace("\\", "/")
    for suffix in (" [input]", " [output]", " [temp]"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    return text.strip("/")


def named_values(workflows) -> set:
    """Every string input value of the workflows, normalised like a path below the input folder."""
    out = set()
    for wf in workflows or ():
        if not isinstance(wf, dict):
            continue
        for node in wf.values():
            inputs = node.get("inputs") if isinstance(node, dict) else None
            if not isinstance(inputs, dict):
                continue
            for value in inputs.values():
                if isinstance(value, str) and value.strip() and len(value) <= 4096:
                    out.add(_norm(value))
    return out


def _still_needed(rel: str, remaining: set) -> bool:
    if rel in remaining:
        return True
    name = PurePosixPath(rel).name
    parents = {str(p) for p in PurePosixPath(rel).parents if str(p) not in ("", ".")}
    for value in remaining:
        if value in parents:  # a later node reads the whole folder
            return True
        if PurePosixPath(value).name == name:  # same file name: a node may resolve it differently
            return True
    return False


def stale_inputs(staged_dir, done_workflows, remaining_workflows) -> list:
    """Staged input files a finished workflow named and no remaining workflow can still need."""
    root = Path(staged_dir)
    if not root.is_dir():
        return []
    done = named_values(done_workflows)
    remaining = named_values(remaining_workflows)
    stale = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        if rel in done and not _still_needed(rel, remaining):
            stale.append(path)
    return stale


def _size(path: Path) -> int:
    try:
        if path.is_file():
            return path.stat().st_size
        return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
    except OSError:
        return 0


def remove_files(paths, protect=()) -> tuple:
    """(count, bytes) of the files removed; a file that cannot be removed is skipped, and so is a file in a protected
    model folder (only an unfinished ``*.incomplete`` download may go there)."""
    count = freed = 0
    for path in paths:
        if protected(path, protect) and not str(path).endswith(PARTIAL_SUFFIX):
            continue
        size = _size(path)
        try:
            Path(path).unlink()
        except OSError:
            continue
        count += 1
        freed += size
    return count, freed


def clear_folder(folder, protect=()) -> tuple:
    """Remove everything inside *folder* (the folder itself stays). (count, bytes). A folder in, or holding, a
    protected model folder is never cleared."""
    root = Path(folder)
    if not root.is_dir() or protected(root, protect):
        return 0, 0
    count = freed = 0
    for child in list(root.iterdir()):
        if protected(child, protect):
            continue
        size = _size(child)
        try:
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        except OSError:
            continue
        count += 1
        freed += size
    return count, freed


def ram_critical(sample) -> str | None:
    """A reason when host RAM is critically low in a telemetry sample (hf.telemetry.Sampler.sample()), else None."""
    sample = sample or {}
    ram = sample.get("ram") or {}
    total, avail = ram.get("total_mb"), ram.get("available_mb")
    if isinstance(total, (int, float)) and isinstance(avail, (int, float)) and total > 0:
        floor = min(max(RAM_CRITICAL_FRACTION * total, RAM_CRITICAL_FLOOR_MB), 0.5 * total)
        if avail < floor:
            return f"host RAM {int(avail)} MB free of {int(total)} MB"
    cg = sample.get("cgroup") or {}
    cur, limit = cg.get("current_mb"), cg.get("max_mb")
    if isinstance(cur, (int, float)) and isinstance(limit, (int, float)) and limit > 0:
        floor = min(max(RAM_CRITICAL_FRACTION * limit, RAM_CRITICAL_FLOOR_MB), 0.5 * limit)
        if limit - cur < floor:
            return f"container memory {int(cur)} MB used of its {int(limit)} MB limit"
    return None


def disk_low(sample, label: str = "tmp") -> str | None:
    """A reason when the local disk (*label* in the sample) is low, else None."""
    disk = ((sample or {}).get("disk") or {}).get(label) or {}
    free, total = disk.get("free_mb"), disk.get("total_mb")
    if isinstance(free, (int, float)) and isinstance(total, (int, float)) and total > 0:
        floor = min(max(DISK_LOW_FRACTION * total, DISK_LOW_FLOOR_MB), 0.5 * total)
        if free < floor:
            return f"local disk {int(free)} MB free of {int(total)} MB"
    return None


def hf_cache_dirs(env=None, home=None) -> list:
    env = os.environ if env is None else env
    home = Path(home) if home is not None else Path.home()
    dirs = []
    if env.get("HF_HUB_CACHE"):
        dirs.append(Path(env["HF_HUB_CACHE"]))
    if env.get("HF_HOME"):
        dirs.append(Path(env["HF_HOME"]) / "hub")
    dirs.append(home / ".cache" / "huggingface" / "hub")
    seen, out = set(), []
    for d in dirs:
        if str(d) not in seen:
            seen.add(str(d))
            out.append(d)
    return out


def _mb(n: int) -> str:
    return f"{n / _MB:.1f} MB"


class Housekeeper:
    """Called by the runner between manifest entries. ``post_free(payload)`` sends ComfyUI's /free; ``sample()``
    returns a telemetry sample (or None); ``log(text)`` prints a [runner] line; ``mark(**fields)`` emits the
    [hf] phase mark."""

    def __init__(self, *, temp_dir, staged_dir, post_free, sample, log, mark, env=None, home=None):
        self.temp_dir = Path(temp_dir)
        self.staged_dir = Path(staged_dir)
        self.post_free = post_free
        self.sample = sample
        self.log = log
        self.mark = mark
        self.env = os.environ if env is None else env
        self.home = Path(home) if home is not None else Path.home()
        self.clean, self.unload = flags_from_env(self.env)
        self.memory, self.vram_high, self.vram_low = memory_from_env(self.env)
        self.unload = self.memory == "always"
        self._armed = True            # auto: an unload is allowed; re-armed only after use was seen below vram_low
        self.protect = model_roots(self.env)
        self.started = time.time()
        self._pip_existed = True   # unknown until before_user_nodes(): never remove a cache this job did not create

    def _pip_cache(self) -> Path:
        return self.home / ".cache" / "pip"

    def before_user_nodes(self) -> None:
        self._pip_existed = self._pip_cache().exists()

    def announce(self) -> None:
        """An [hf] phase mark only: the frozen [runner] line sequence of a manifest job stays as it was (the bridge
        learns from this mark that the image cleans between renders)."""
        self.mark(detail="announce", clean=self.clean, unload=self.unload, memory=self.memory)

    def _sample(self):
        try:
            return self.sample() if self.sample is not None else None
        except Exception:  # noqa: BLE001 - telemetry is optional
            return None

    def _free(self, payload) -> bool:
        try:
            self.post_free(payload)
            return True
        except Exception:  # noqa: BLE001 - an old ComfyUI without /free, or a busy server: never fail the job
            return False

    def after_user_nodes(self) -> dict:
        """pip's download cache after the artist's packs were installed (never a deliberate HF_USER_NODES_CACHE)."""
        report = {"actions": [], "freed_bytes": 0}
        if not self.clean or self.env.get(ENV_PIP_CACHE) or self._pip_existed:
            return report
        try:
            pip_cache = self._pip_cache()
            if pip_cache.is_dir():
                size = _size(pip_cache)
                shutil.rmtree(pip_cache, ignore_errors=True)
                if not pip_cache.exists():
                    report["actions"].append("pip_cache")
                    report["freed_bytes"] += size
                    self.log(f"housekeeping: removed pip's download cache ({_mb(size)})")
                    self.mark(detail="pip_cache", freed_bytes=size)
        except Exception:  # noqa: BLE001
            pass
        return report

    def _this_job(self, path: Path) -> bool:
        try:
            return path.stat().st_mtime >= self.started - 1
        except OSError:
            return False

    def between(self, idx: int, total: int, done_workflows, remaining_workflows) -> dict:
        """Clean after workflow *idx* of *total* finished, before the next one starts."""
        report = {"actions": [], "freed_bytes": 0, "free": None}
        try:
            self._between(report, idx, total, done_workflows, remaining_workflows)
        except Exception as err:  # noqa: BLE001 - housekeeping never fails a job
            self.log(f"housekeeping: skipped after an error ({type(err).__name__}: {err})")
        return report

    def _between(self, report, idx, total, done_workflows, remaining_workflows) -> None:
        parts = []
        if self.clean:
            n, freed = clear_folder(self.temp_dir, self.protect)
            if n:
                report["actions"].append("comfy_temp")
                report["freed_bytes"] += freed
                parts.append(f"{n} temporary file(s), {_mb(freed)}")
            n, freed = remove_files(stale_inputs(self.staged_dir, done_workflows, remaining_workflows), self.protect)
            if n:
                report["actions"].append("inputs")
                report["freed_bytes"] += freed
                parts.append(f"{n} input file(s) no later workflow uses, {_mb(freed)}")
        sample = self._sample()
        if self.clean:
            low = disk_low(sample)
            if low:
                n, freed = remove_files(p for d in hf_cache_dirs(self.env, self.home) if d.is_dir()
                                        for p in d.rglob("*" + PARTIAL_SUFFIX) if self._this_job(p))
                if n:
                    report["actions"].append("partial_downloads")
                    report["freed_bytes"] += freed
                    parts.append(f"{n} partial model download(s), {_mb(freed)} ({low})")
        pct = vram_used_pct(sample)
        report["vram_pct"] = None if pct is None else round(pct, 1)
        if self.unload:
            if self._free({"unload_models": True, "free_memory": True}):
                report["free"] = "unload"
                parts.append("models unloaded and ComfyUI's cache freed (your setting); the next workflow loads "
                             "its models again")
        elif self.memory == "auto" and pct is not None and self._auto_unload(pct):
            report["free"] = "vram_pressure"
            parts.append(f"models unloaded because GPU memory use was {pct:.0f}% (your limit {self.vram_high}%); the "
                         f"next workflow loads them again. No further automatic unload until use falls below "
                         f"{self.vram_low}%")
        else:
            critical = ram_critical(sample)
            if critical and self._free({"free_memory": True}):
                report["free"] = "pressure"
                parts.append(f"ComfyUI's cache freed because memory is critically low ({critical}); ComfyUI "
                             "unloads the models with it, so the next workflow loads them again")
        if parts:
            self.log(f"housekeeping after workflow {idx}/{total}: " + "; ".join(parts))
        self.mark(detail="between", wf=idx, of=total, actions=list(report["actions"]),
                  freed_bytes=report["freed_bytes"], free=report["free"], memory=self.memory,
                  vram_pct=report.get("vram_pct"))

    def _auto_unload(self, pct: float) -> bool:
        """auto: unload at or above vram_high when armed; re-arm only after a check below vram_low (hysteresis)."""
        if not self._armed:
            if pct < self.vram_low:
                self._armed = True
            return False
        if pct < self.vram_high:
            return False
        if self._free({"unload_models": True, "free_memory": True}):
            self._armed = False
            return True
        return False

#!/usr/bin/env python3
"""HaloFuse job runner — drives one or more headless ComfyUI workflows per
container, against a single ComfyUI process.

Job contract:
  /input/halofuse_job.json     optional manifest: {"workflows": [{"workflow":
                                {...API-format graph...}, "batch_count": N}, ...]}.
                                When present, every entry runs in manifest order
                                against ONE ComfyUI boot; outputs land under
                                /output/wf_01/, /output/wf_02/, ... (zero-padded,
                                manifest order), keeping each file's subfolder.
                                An entry may also carry "partial_execution_targets":
                                [node ids] (run only up to those output nodes).
                                The manifest name earlier bridges write is
                                accepted too (matched by SHA-256, see
                                _manifest_path()).
  /input/workflow.json         single-workflow fallback, used only when the
                                manifest above is absent. Exact pre-manifest
                                behaviour: one workflow, BATCH_COUNT repeats,
                                flat /output. This is what an old bridge submits,
                                so it must keep working unchanged.
  /input/models/...      model files (checkpoints/, loras/, vae/, ...)
  /output/               everything written here persists to the cloud job storage
  /output/_halofuse/     runner files (render.log, events.jsonl, previews/);
                         never counted as workflow outputs

The ComfyUI server only ever listens on container-internal loopback; the cloud
platform publishes no ports and none are needed.

Besides the [runner] lines below, the runner prints structured `[hf] {...}`
event lines (contract C-3: phase, progress, node, telemetry, preview, output,
error, fingerprint, recovery, item, final). The existing [runner] lines and
markers are unchanged.

Exit codes (single-workflow / fallback mode, unchanged):
  0  success — workflow completed, files written to /output
  1  workflow executed but ended in error (also: job cancelled by SIGTERM/SIGINT)
  2  bad input (workflow.json missing / unparsable / not API format,
     /output not writable, the input directory cannot be staged, or the
     launch options were rejected)
  3  ComfyUI rejected the workflow at submission
  4  JOB_TIMEOUT_SECONDS exceeded
  5  workflow completed but produced no files in /output
  6  ComfyUI server failed to start or died mid-run

Exit codes (manifest mode):
  0  every workflow in the manifest succeeded
  1  at least one workflow failed; the others still ran (see per-workflow
     [runner] markers in the log for which); also: job cancelled
  2  bad input (manifest missing/malformed, /output not writable, the
     input directory cannot be staged, or the launch options were rejected)
  6  ComfyUI server failed to start, or died mid-run and could not be
     restarted (HF_RESTART_POLICY; default "each": one restart per manifest
     entry, the dead entry is reported as failed and the remaining workflows
     run; a crash loop of 3 consecutive deaths stops the job)

  Codes 3 (submission rejected), 4 (JOB_TIMEOUT_SECONDS exceeded) and 5 (no
  files produced) are per-workflow conditions in manifest mode: that single
  workflow is marked failed and the job moves on to the next entry, then
  exits 1 overall rather than dying on the spot.
"""
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from hf import classify as hf_classify  # noqa: E402
from hf import events as hf_events  # noqa: E402
from hf import latents as hf_latents  # noqa: E402
from hf import launch as hf_launch  # noqa: E402
from hf import outputs as hf_outputs  # noqa: E402
from hf import previews as hf_previews  # noqa: E402
from hf import storage as hf_storage  # noqa: E402
from hf import supervisor as hf_sup  # noqa: E402
from hf import telemetry as hf_telemetry  # noqa: E402
from hf.comfy_ws import ComfyWS  # noqa: E402

COMFYUI_DIR = Path(os.environ.get("COMFYUI_DIR", "/default-comfyui-bundle/ComfyUI"))
INPUT_DIR = Path(os.environ.get("INPUT_DIR", "/input"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "/output"))
WORKFLOW_PATH = Path(os.environ.get("WORKFLOW_PATH", str(INPUT_DIR / "workflow.json")))
MANIFEST_PATH = Path(os.environ.get("MANIFEST_PATH", str(INPUT_DIR / "halofuse_job.json")))
# SHA-256 of the manifest file name that bridges written for the previous image write (the name is not spelled out:
# no provider name in this source). Accepted when halofuse_job.json is absent, so those bridges keep working.
LEGACY_MANIFEST_SHA256 = "eff5cf5368b583ec334d9f61f863aa3953d4cfbffc5ce463912db9c474626bd5"
PORT = int(os.environ.get("COMFY_PORT", "8188"))
BASE_URL = f"http://127.0.0.1:{PORT}"
STARTUP_TIMEOUT = int(os.environ.get("STARTUP_TIMEOUT_SECONDS", "300"))
JOB_TIMEOUT = int(os.environ.get("JOB_TIMEOUT_SECONDS", "0"))  # 0 = no limit
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL_SECONDS", "2"))
HEARTBEAT_EVERY = 60
# Render this many images in one job (one cold start + one model load amortised
# across all of them). Set per job via the BATCH_COUNT env var; clamped to 1..100.
try:
    BATCH_COUNT = max(1, min(int(os.environ.get("BATCH_COUNT", "1")), 100))
except ValueError:
    BATCH_COUNT = 1
# Inputs whose value is randomised between batch renders so each image differs.
SEED_KEYS = ("seed", "noise_seed")
# Where ComfyUI looks for models. Defaults to the per-job /input mount today;
# set MODEL_BASE_DIR=/assets/models to use the platform's persistent assets mount
# when it ships (one submit-time --env flag, no image rebuild, no workflow edits).
MODEL_BASE_DIR = os.environ.get("MODEL_BASE_DIR", str(INPUT_DIR / "models"))
MODEL_SUBDIRS = [
    "checkpoints", "diffusion_models", "unet", "text_encoders", "clip",
    "clip_vision", "vae", "loras", "controlnet", "upscale_models",
    "embeddings", "configs",
]
# Generated at runtime from MODEL_BASE_DIR (see write_extra_model_paths()).
EXTRA_MODEL_PATHS = Path("/tmp/halofuse_extra_model_paths.yaml")
# Model prefetch (Halo Hues). ComfyUI's dynamic-VRAM loader reads weights from
# /assets in many small random reads while sampling ("Model Initializing"),
# which is very slow on the platform's assets mount and its on-node cache. Copying the
# model files this job references to the node's local NVMe first, in large
# sequential reads, and pointing ComfyUI there makes every later read local.
# PREFETCH_MODELS=0 turns it off.
PREFETCH_MODELS = os.environ.get("PREFETCH_MODELS", "1").lower() not in ("0", "false", "no")
LOCAL_MODEL_DIR = Path(os.environ.get("LOCAL_MODEL_DIR", "/tmp/halofuse-models"))
PREFETCH_THREADS = max(1, int(os.environ.get("PREFETCH_THREADS", "4")))
MODEL_EXTS = (".safetensors", ".sft", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".onnx")
# ComfyUI's input directory. /input is read-only for the life of the job, and
# ComfyUI creates folders there at startup (Load3D's 3d/, uploads, and so on),
# so the runner copies the job files here and passes this path instead.
# Kept out of /tmp/comfyui because ComfyUI clears its temp directory on boot.
STAGED_INPUT_DIR = Path("/tmp/halofuse-input")
# ComfyUI's --temp-directory (ComfyUI appends "temp/"); PreviewImage & co. write there.
COMFY_TEMP_DIR = Path("/tmp/comfyui")
# HaloFuse: launch allowlist (D3), probes for telemetry (F5) and the fingerprint (D4).
ALLOWLIST_PATH = _HERE / "launch_allowlist.json"
NVIDIA_SMI = ["nvidia-smi"]
PROC_ROOT = Path("/proc")
CGROUP_ROOT = Path("/sys/fs/cgroup")
NODE_SNAPSHOT_PATH = Path(os.environ.get("HF_NODE_SNAPSHOT_PATH", "/etc/halofuse/node_snapshot.json"))
CLIENT_ID = "halofuse-runner"
INTERRUPT_GRACE_SECONDS = 5.0
# Heartbeat for the Docker HEALTHCHECK (scripts/healthcheck.py reads it).
HEALTH_FILE = Path(os.environ.get("HF_HEALTH_FILE", "/tmp/halofuse-health.json"))
try:
    HEARTBEAT_SECONDS = max(0.2, float(os.environ.get("HF_HEARTBEAT_SECONDS", "5")))
except ValueError:
    HEARTBEAT_SECONDS = 5.0
# A live ComfyUI that leaves /history unanswered this long is stopped and restarted (0 disables).
UNRESPONSIVE = hf_launch.unresponsive_seconds(os.environ.get("HF_UNRESPONSIVE_SECONDS"))
MAX_CONSECUTIVE_DEATHS = 3
_MEMORY_OOM_CODES = ("vram_oom", "vae_decode_oom", "host_ram_oom")
_DEFAULT_ERRORS = {1: "execution_error", 2: "bad_input", 3: "validation_failed", 4: "timeout",
                   5: "no_output", 6: "startup_failed"}
_CLASSIFY_BY_CODE = object()

# Run state (set up in main()).
_CTX = None          # hf.supervisor.RunContext
_LAUNCH = None       # hf.launch.LaunchConfig
_ALLOW = None        # launch allowlist dict
_RELAY = None        # hf.previews.LiveRelay
_WS = None           # hf.comfy_ws.ComfyWS
_TELEMETRY = None    # hf.telemetry.TelemetryThread
_SAMPLER = None      # hf.telemetry.Sampler
_OOM_BEFORE = None   # cgroup oom_kill count when the current ComfyUI process started
_PROC_ARGS = []      # extra args of the current ComfyUI process
_FINGERPRINTED = False
_HEARTBEAT = None    # hf.supervisor.Heartbeat
_PREVIEW_DISCARD_MARKED = False
_PREFETCH_STOP = threading.Event()  # set on exit: a running model copy stops between chunks
_PREFETCH_THREADS = []  # copy workers, joined briefly on exit so a stopped copy can remove its .part file


class WorkflowFailure(Exception):
    """One manifest entry's failure (exit codes 1, 3, 4 or 5 — never 2 or 6).

    Raised by submit()/wait_done()/_collect_new_outputs() instead of calling
    die() directly, so manifest mode can catch it, mark that entry failed, and
    continue to the next one. Fallback (single-workflow) mode catches it right
    back into die(code, message), reproducing today's immediate-exit behaviour
    exactly. `classification` is the C-3 error dict; `reported` is True once an
    [hf] error event was emitted for it.
    """

    def __init__(self, code, message, classification=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.classification = classification
        self.reported = False


def _em():
    return hf_events.get()


def log(msg):
    _em().line(f"[runner] {msg}")


def _raw(text):
    """Print diagnostic text exactly as before (ComfyUI JSON bodies), also into render.log."""
    _em().line(text)


def _wf_fields():
    if _CTX is not None and _CTX.manifest_total and getattr(_CTX, "current_wf", None):
        return {"wf": _CTX.current_wf, "of": _CTX.manifest_total}
    return {}


def die(code, msg, error=_CLASSIFY_BY_CODE):
    log(f"ERROR: {msg}")
    if _CTX is not None:
        if error is _CLASSIFY_BY_CODE:
            name = _DEFAULT_ERRORS.get(code, "runner_error")
            error = {"code": name, "message": hf_classify.message_for(name), "detail": msg}
        if error:
            _CTX.error(error, exit_code=code, **_wf_fields())
    sys.exit(code)


def http_json(path, payload=None, timeout=30):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(BASE_URL + path, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    return json.loads(body) if body else {}


def log_banner():
    log(f"HaloFuse runner (python {sys.version.split()[0]})")
    version_file = COMFYUI_DIR / "comfyui_version.py"
    if version_file.is_file():
        match = re.search(r'__version__\s*=\s*"([^"]+)"', version_file.read_text())
        if match:
            log(f"ComfyUI version: {match.group(1)}")
    try:
        smi = subprocess.run(
            NVIDIA_SMI + ["--query-gpu=name,driver_version,memory.total",
                          "--format=csv,noheader"],
            capture_output=True, text=True, timeout=20,
        )
        if smi.returncode == 0:
            log(f"GPU: {smi.stdout.strip()}")
    except (OSError, subprocess.TimeoutExpired):
        log("nvidia-smi not available")


def _validate_workflow_shape(workflow, label):
    """Shared API-format check for both workflow.json and each manifest entry.

    Always fatal (exit 2) on a bad shape, even from inside a manifest — a
    malformed graph means the job can never legitimately run, same severity
    as a bad workflow.json today.
    """
    if not isinstance(workflow, dict):
        die(2, f"{label} is not a JSON object")
    if "nodes" in workflow and "links" in workflow:
        die(2, f"{label} is a UI-format export; re-export with "
               "'Save (API Format)' (enable dev mode options in ComfyUI settings)")
    bad = [k for k, v in workflow.items()
           if not (isinstance(v, dict) and "class_type" in v)]
    if bad:
        die(2, f"{label} does not look like API format "
               f"(offending keys: {bad[:5]})")


def _validate_targets(value, label):
    """Optional partial_execution_targets: a list of node ids present in the workflow."""
    if value is None:
        return None
    if not isinstance(value, list) or not value or not all(isinstance(v, (str, int)) for v in value):
        die(2, f"{label} partial_execution_targets must be a non-empty list of node ids")
    return [str(v) for v in value]


def load_workflow():
    if not WORKFLOW_PATH.is_file():
        die(2, f"workflow not found at {WORKFLOW_PATH}")
    try:
        workflow = json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        die(2, f"cannot parse {WORKFLOW_PATH}: {err}")
    _validate_workflow_shape(workflow, "workflow.json")
    log(f"workflow loaded: {len(workflow)} nodes")
    return workflow


def _manifest_path() -> Path:
    """The manifest to run: MANIFEST_PATH (default /input/halofuse_job.json) when it exists or was set explicitly,
    else a manifest under the name earlier bridges write (found by SHA-256 of the name), else MANIFEST_PATH."""
    if MANIFEST_PATH.is_file() or os.environ.get("MANIFEST_PATH"):
        return MANIFEST_PATH
    try:
        names = sorted(os.listdir(INPUT_DIR))
    except OSError:
        return MANIFEST_PATH
    for name in names:
        if hashlib.sha256(name.encode("utf-8")).hexdigest() == LEGACY_MANIFEST_SHA256 and (INPUT_DIR / name).is_file():
            return INPUT_DIR / name
    return MANIFEST_PATH


def load_manifest():
    """Load and validate the manifest (/input/halofuse_job.json); return [(workflow, batch_count, targets), ...]
    in manifest order. Any structural problem is fatal (exit 2), checked entirely
    before ComfyUI starts — same severity and timing as a bad workflow.json today.
    """
    try:
        raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        die(2, f"cannot parse {MANIFEST_PATH}: {err}")
    entries = raw.get("workflows") if isinstance(raw, dict) else None
    if not isinstance(entries, list) or not entries:
        die(2, f"{MANIFEST_PATH} must be a JSON object with a non-empty 'workflows' array")
    parsed = []
    for i, entry in enumerate(entries):
        label = f"{MANIFEST_PATH.name} workflows[{i}]"
        if not isinstance(entry, dict) or not isinstance(entry.get("workflow"), dict):
            die(2, f"{label} missing a 'workflow' object")
        workflow = entry["workflow"]
        _validate_workflow_shape(workflow, f"{label}.workflow")
        try:
            batch_count = max(1, min(int(entry.get("batch_count") or 1), 100))
        except (TypeError, ValueError):
            batch_count = 1
        targets = _validate_targets(entry.get("partial_execution_targets"), label)
        parsed.append((workflow, batch_count, targets))
    log(f"manifest loaded: {len(parsed)} workflow(s)")
    return parsed


def check_output_writable():
    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        probe = OUTPUT_DIR / ".write-probe"
        probe.write_text("")
        probe.unlink()
    except OSError as err:
        die(2, f"{OUTPUT_DIR} is not writable: {err}")


def write_extra_model_paths():
    """Generate ComfyUI's extra_model_paths.yaml from MODEL_BASE_DIR.

    Done at runtime, not baked into the image, so the model library location is
    a submit-time choice: /input/models by default, or /assets/models once the
    platform's assets mount is available, with no rebuild and no workflow edits.
    """
    names = list(MODEL_SUBDIRS)
    extra = _extra_model_dirs()
    names += extra
    lines = []
    if PREFETCH_MODELS:
        # Listed first: ComfyUI returns the first match, so a prefetched local
        # copy wins over the /assets original. Files never prefetched are still
        # found under halofuse below.
        lines += ["halofuse_local:", f"  base_path: {LOCAL_MODEL_DIR}"]
        lines += [f"  {name}: {name}" for name in MODEL_SUBDIRS]
    lines += ["halofuse:", f"  base_path: {MODEL_BASE_DIR}"]
    lines += [f"  {name}: {name}" for name in names]
    EXTRA_MODEL_PATHS.write_text("\n".join(lines) + "\n")
    log(f"model search base: {MODEL_BASE_DIR}")
    if extra:
        log(f"extra model folders: {', '.join(extra)}")
    _link_extra_model_dirs(extra)


def _extra_model_dirs():
    """Top-level folders under MODEL_BASE_DIR beyond the standard MODEL_SUBDIRS.

    Custom nodes keep models in folders of their own (LLM/ for QwenVL,
    depthcrafter/, sams/, BiRefNet/, ...). Listing only the top level keeps this
    cheap on the lazily-mounted assets path.
    """
    base = Path(MODEL_BASE_DIR)
    try:
        entries = sorted(p.name for p in base.iterdir() if p.is_dir())
    except OSError:
        return []
    return [name for name in entries
            if name not in MODEL_SUBDIRS and not name.startswith(".")]


def _link_extra_model_dirs(names):
    """Symlink extra model folders into ComfyUI's own models/ directory.

    Many custom nodes ignore extra_model_paths.yaml and read
    folder_paths.models_dir/<folder> directly; without the link they download
    the model again on every job. An existing folder is replaced only when it
    holds nothing but ComfyUI's put_*_here placeholder files.
    """
    models_dir = COMFYUI_DIR / "models"
    for name in names:
        source = Path(MODEL_BASE_DIR) / name
        target = models_dir / name
        try:
            if target.is_symlink():
                continue
            if target.is_dir():
                if any(not p.name.startswith("put_") for p in target.iterdir()):
                    continue
                shutil.rmtree(target)
            elif target.exists():
                continue
            models_dir.mkdir(parents=True, exist_ok=True)
            target.symlink_to(source, target_is_directory=True)
            log(f"linked models/{name} -> {source}")
        except OSError as err:
            log(f"could not link models/{name}: {err}")


def _referenced_model_files(workflows):
    """Model files under MODEL_BASE_DIR that these API-format workflows name.

    Any string input ending in a model extension is looked up in each standard
    model folder; every match is returned as a path relative to MODEL_BASE_DIR.
    Extra custom-node folders are left alone: they are symlinked into ComfyUI's
    own models/ directory, which ComfyUI searches before any extra path.
    """
    base = Path(MODEL_BASE_DIR)
    names = set()
    for wf in workflows:
        for node in wf.values():
            if not isinstance(node, dict):
                continue
            for value in (node.get("inputs") or {}).values():
                if isinstance(value, str) and value.lower().endswith(MODEL_EXTS):
                    names.add(value.replace("\\", "/").lstrip("/"))
    found = {}
    for name in sorted(names):
        for folder in MODEL_SUBDIRS:
            rel = Path(folder) / name
            try:
                src = base / rel
                if ".." not in rel.parts and src.is_file():
                    found[str(rel)] = src.stat().st_size
            except OSError:
                pass
    return found


def _copy_one(rel, size):
    src = Path(MODEL_BASE_DIR) / rel
    dest = LOCAL_MODEL_DIR / rel
    if dest.is_file() and dest.stat().st_size == size:
        return 0.0
    # atomic (.part + rename): ComfyUI never sees a half-written model; None when the job is ending
    return hf_storage.copy_file(src, dest, _PREFETCH_STOP)


def _prefetch_decision(total):
    """C4: copy only from a remote/FUSE mount to a real local disk (hf/storage.py)."""
    try:
        mounts = hf_storage.parse_mounts((PROC_ROOT / "mounts").read_text(encoding="utf-8", errors="replace"))
    except OSError:
        mounts = []
    try:
        info = hf_telemetry.parse_meminfo((PROC_ROOT / "meminfo").read_text(encoding="utf-8", errors="replace"))
        mem = info.get("MemAvailable")  # kB
        mem = int(mem) * 1024 if mem is not None else None
    except (OSError, ValueError, TypeError):
        mem = None
    local_parent = LOCAL_MODEL_DIR.parent if LOCAL_MODEL_DIR.parent.exists() else Path("/tmp")
    try:
        free = shutil.disk_usage(local_parent).free
    except OSError:
        free = None
    return hf_storage.prefetch_plan(total, src_fs=hf_storage.fs_type_of(MODEL_BASE_DIR, mounts),
                                    dst_fs=hf_storage.fs_type_of(LOCAL_MODEL_DIR, mounts), disk_free=free,
                                    mem_available=mem)


def prefetch_models(workflows):
    """Copy the job's model files to local NVMe. Runs in a background thread
    while ComfyUI starts; main() waits for it before submitting a workflow.
    Copies run in daemon threads and stop between chunks when the job ends,
    so a cancelled or failed job never waits for a multi-GiB copy (the old
    ThreadPoolExecutor workers were joined at interpreter exit)."""
    try:
        files = _referenced_model_files(workflows)
        if not files:
            log("prefetch: no model files to copy")
            return
        total = sum(files.values())
        ok, reason = _prefetch_decision(total)
        if not ok:
            log(f"prefetch: skipped, {reason}")
            return
        log(f"prefetch: copying {len(files)} model file(s), {total / 2**30:.1f} GiB, "
            f"to local NVMe ({LOCAL_MODEL_DIR})")
        started = time.time()
        # Largest first so the long copies start early.
        pending = sorted(files.items(), key=lambda kv: kv[1], reverse=True)
        lock = threading.Lock()

        def worker():
            while not _PREFETCH_STOP.is_set():
                with lock:
                    if not pending:
                        return
                    rel, size = pending.pop(0)
                try:
                    secs = _copy_one(rel, size)
                    if secs:
                        log(f"prefetch: {rel} ({size / 2**30:.1f} GiB) in {secs:.0f}s "
                            f"({size / 2**20 / max(secs, 0.001):.0f} MiB/s)")
                except Exception as err:  # noqa: BLE001 - fall back to /assets for this file
                    log(f"prefetch: {rel} failed ({err}); ComfyUI will read it from /assets")

        threads = [threading.Thread(target=worker, name=f"hf-prefetch-{i}", daemon=True)
                   for i in range(min(PREFETCH_THREADS, len(pending)))]
        _PREFETCH_THREADS.extend(threads)
        for t in threads:
            t.start()
        for t in threads:
            while t.is_alive() and not _PREFETCH_STOP.is_set():
                t.join(0.5)
        if _PREFETCH_STOP.is_set():
            return
        elapsed = time.time() - started
        log(f"prefetch: done in {elapsed:.0f}s "
            f"({total / 2**20 / max(elapsed, 0.001):.0f} MiB/s overall)")
    except Exception as err:  # noqa: BLE001 - prefetch is an optimisation only
        log(f"prefetch: skipped after error ({err})")


def _prefetch_with_phase(workflows):
    if _CTX is not None:
        _CTX.phase("prefetch", "start")
    try:
        prefetch_models(workflows)
    finally:
        if _CTX is not None:
            _CTX.phase("prefetch", "end")


def stage_input_directory():
    """Copy the read-only input mount to a writable directory for ComfyUI.

    A real copy, not symlinks: ComfyUI resolves input paths with realpath and
    rejects anything that lands outside its input directory. The model tree is
    left on the mount when MODEL_BASE_DIR sits under the input directory;
    extra_model_paths.yaml reads it from there, so checkpoints are not copied.
    """
    input_root = INPUT_DIR.resolve()
    skip_rel = None
    try:
        model_base = Path(MODEL_BASE_DIR).resolve()
    except OSError:
        model_base = None
    if model_base is not None and model_base != input_root and input_root in model_base.parents:
        skip_rel = model_base.relative_to(input_root)

    def ignore_models(directory, names):
        if skip_rel is None:
            return []
        try:
            rel = Path(directory).resolve().relative_to(input_root)
        except (OSError, ValueError):
            return []
        if rel == skip_rel.parent and skip_rel.name in names:
            return [skip_rel.name]
        return []

    if STAGED_INPUT_DIR.exists():
        shutil.rmtree(STAGED_INPUT_DIR)
    try:
        shutil.copytree(input_root, STAGED_INPUT_DIR, ignore=ignore_models)
        # The mount is read-only, and copytree keeps those modes. ComfyUI has
        # to be able to create folders and files in the copy.
        for dirpath, dirnames, filenames in os.walk(STAGED_INPUT_DIR):
            os.chmod(dirpath, 0o755)
            for name in filenames:
                os.chmod(Path(dirpath) / name, 0o644)
    except OSError as err:
        die(2, f"cannot stage {INPUT_DIR} to {STAGED_INPUT_DIR}: {err}")
    files = [p for p in STAGED_INPUT_DIR.rglob("*") if p.is_file()]
    total = sum(p.stat().st_size for p in files)
    log(f"staged input: {len(files)} file(s), {total:,} bytes -> {STAGED_INPUT_DIR}")


def resolve_launch():
    """D3: validate HF_PRESET / COMFY_EXTRA_ARGS / EXTRA_ENV against the allowlist before ComfyUI starts."""
    global _LAUNCH, _ALLOW
    try:
        _ALLOW = hf_launch.load_allowlist(ALLOWLIST_PATH)
    except (OSError, ValueError) as err:
        die(2, f"launch allowlist unreadable: {err}",
            {"code": "bad_launch_args", "message": hf_classify.message_for("bad_launch_args"), "detail": str(err)})
    _LAUNCH = hf_launch.resolve(os.environ, _ALLOW)
    if not _LAUNCH.ok:
        detail = "; ".join(_LAUNCH.errors)
        die(2, f"launch options rejected: {detail}",
            {"code": "bad_launch_args", "message": hf_classify.message_for("bad_launch_args"), "detail": detail})
    gpu_present = hf_sup.gpu_driver_info(NVIDIA_SMI)[0] is not None
    cpu = hf_launch.cpu_fallback_args(os.environ, gpu_present)
    if cpu:
        _LAUNCH.args = list(_LAUNCH.args) + cpu  # deliberately outside the allowlist (--cpu stays denied there)
        log("no NVIDIA GPU visible and HF_ALLOW_CPU=1: ComfyUI runs on the CPU (smoke test / diagnostics only)")
    if _LAUNCH.args or _LAUNCH.env or _LAUNCH.preset != "safe":
        log(f"launch preset: {_LAUNCH.preset}; extra args: {' '.join(_LAUNCH.args) or 'none'}; "
            f"extra env: {', '.join(sorted(_LAUNCH.env)) or 'none'}")


def start_comfyui(extra_args=None):
    global _OOM_BEFORE, _PROC_ARGS
    args = list(_LAUNCH.args if extra_args is None else extra_args) if _LAUNCH is not None else []
    cmd = [
        sys.executable, "main.py",
        "--listen", "127.0.0.1",
        "--port", str(PORT),
        "--extra-model-paths-config", str(EXTRA_MODEL_PATHS),
        "--output-directory", str(OUTPUT_DIR),
        "--input-directory", str(STAGED_INPUT_DIR),
        "--temp-directory", str(COMFY_TEMP_DIR),
    ] + args
    log(f"starting ComfyUI: {' '.join(cmd)}")
    env = os.environ.copy()
    env.pop("HF_EVENT_NONCE", None)  # ComfyUI and custom nodes must not be able to sign [hf] events
    if _LAUNCH is not None:
        env.update(_LAUNCH.env)
    _PROC_ARGS = args
    _OOM_BEFORE = _SAMPLER.oom_kill_count() if _SAMPLER is not None else None
    _CTX.phase("comfy_start", "start")
    _mark_discarded_previews(args)
    # stdout/stderr are relayed line by line so ComfyUI's own logs still reach
    # the cloud log stream (same stream, same text) and render.log.
    return hf_sup.ComfyProcess(cmd, COMFYUI_DIR, env, _em(),
                               on_node_import=lambda d: _CTX.phase("node_import", "mark", dur=round(d, 3)),
                               on_line=_comfy_line).start()


def _comfy_line(text):
    """Every ComfyUI output line goes to the preview relay when it consumes them (R-PREVIEW-01)."""
    consume = getattr(_RELAY, "on_comfy_line", None)
    if consume is not None:
        consume(text)


def _mark_discarded_previews(args):
    """R-PREVIEW-01: ComfyUI renders preview frames (--preview-method other than none) that nobody relays."""
    global _PREVIEW_DISCARD_MARKED
    if _PREVIEW_DISCARD_MARKED or "--preview-method" not in args:
        return
    i = args.index("--preview-method")
    method = args[i + 1] if i + 1 < len(args) else ""
    if method != "none" and (os.environ.get("HF_PREVIEW") or "off").strip().lower() != "live":
        _PREVIEW_DISCARD_MARKED = True
        _CTX.phase("preview_discarded", "mark", detail=f"--preview-method {method} while HF_PREVIEW is off")


def _death(proc):
    """ComfyDied for a ComfyUI process that is gone, classified (cgroup oom_kill, signal, stderr tail)."""
    proc.join_output(2.0)
    proc.reap_tree()  # no orphaned child keeps memory into the restarted ComfyUI
    delta = None
    if _SAMPLER is not None and _OOM_BEFORE is not None:
        now = _SAMPLER.oom_kill_count()
        delta = None if now is None else now - _OOM_BEFORE
    cls = hf_classify.classify_exit(proc.returncode, oom_kill_delta=delta, tail=proc.tail())
    node = _RELAY.current["node"] if _RELAY is not None else None
    class_type = _RELAY.current["class_type"] if _RELAY is not None else None
    if node is not None:
        cls = dict(cls, node=node, class_type=class_type)
    return hf_sup.ComfyDied(proc.returncode, cls, node, class_type)


def wait_ready(proc):
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        _CTX.check_cancel()
        if proc.poll() is not None:
            proc.join_output(2.0)
            delta = None
            if _SAMPLER is not None and _OOM_BEFORE is not None:
                now = _SAMPLER.oom_kill_count()
                delta = None if now is None else now - _OOM_BEFORE
            die(6, f"ComfyUI exited during startup (code {proc.returncode})",
                hf_classify.classify_exit(proc.returncode, oom_kill_delta=delta, tail=proc.tail(), during="startup"))
        try:
            stats = http_json("/system_stats", timeout=5)
            names = ", ".join(d.get("name", "?") for d in stats.get("devices", []))
            log(f"ComfyUI ready (devices: {names or 'unknown'})")
            _CTX.phase("comfy_start", "end")
            _beat("ready")
            return stats
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            time.sleep(1)
    die(6, f"ComfyUI not ready after {STARTUP_TIMEOUT}s",
        {"code": "startup_failed", "message": hf_classify.message_for("startup_failed"),
         "detail": f"no answer on /system_stats within {STARTUP_TIMEOUT}s"})


def _after_ready(stats):
    """Fingerprint (D4, once) and the websocket relay (A1/A6, started once; it reconnects after a restart)."""
    global _FINGERPRINTED, _WS
    if not _FINGERPRINTED:
        _FINGERPRINTED = True
        system = stats.get("system") or {} if isinstance(stats, dict) else {}
        devices = stats.get("devices") or [] if isinstance(stats, dict) else []
        driver, caps = hf_sup.gpu_driver_info(NVIDIA_SMI)
        gpus = []
        for i, dev in enumerate(devices):
            total = dev.get("vram_total")
            gpus.append({"name": dev.get("name"),
                         "mem_total_mb": int(total) // 2**20 if isinstance(total, (int, float)) else None,
                         "compute_cap": caps[i] if i < len(caps) else None})
        sha, profile, image_ref = hf_sup.read_snapshot_info(NODE_SNAPSHOT_PATH)
        python_version = str(system.get("python_version") or "").split(" ")[0] or None
        _em().emit("fingerprint", comfyui_version=system.get("comfyui_version"), python=python_version,
                   torch=system.get("pytorch_version"), torch_cuda=hf_sup.torch_cuda_version(), driver=driver,
                   gpus=gpus, attention=hf_launch.attention_backend(_PROC_ARGS), preset=_LAUNCH.preset,
                   launch_args=list(_LAUNCH.args), launch_env=sorted(_LAUNCH.env),
                   image_ref=os.environ.get("HF_IMAGE_REF") or image_ref,
                   node_snapshot_sha256=sha, node_snapshot_profile=profile)
    if _WS is None and _RELAY is not None:
        _WS = ComfyWS("127.0.0.1", PORT, CLIENT_ID, on_json=_RELAY.on_json, on_preview=_RELAY.on_preview).start()
        if not _WS.connected.wait(3.0):
            _CTX.phase("ws_unavailable", "mark", detail="no node/progress/preview events")


def submit(workflow, targets=None):
    payload = {"prompt": workflow, "client_id": CLIENT_ID}
    if targets:
        payload["partial_execution_targets"] = targets
    if _RELAY is not None:
        _RELAY.expect_prompt(workflow, **_wf_fields())
    try:
        resp = http_json("/prompt", payload=payload, timeout=60)
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", errors="replace")
        log(f"ComfyUI rejected the workflow (HTTP {err.code}):")
        _raw(body)
        raise WorkflowFailure(3, "workflow validation failed", hf_classify.classify_rejection(err.code, body))
    except (urllib.error.URLError, OSError) as err:
        proc = _CTX.proc
        if proc is not None and proc.poll() is not None:
            raise _death(proc)
        raise WorkflowFailure(3, f"could not submit the workflow to ComfyUI ({err})",
                              {"code": "validation_failed", "message": hf_classify.message_for("validation_failed"),
                               "detail": str(err)})
    prompt_id = resp.get("prompt_id")
    if not prompt_id:
        raise WorkflowFailure(3, f"no prompt_id in /prompt response: {resp}")
    node_errors = resp.get("node_errors") or {}
    if node_errors:
        # ComfyUI accepts a prompt when at least one output validates and
        # silently drops the outputs that failed; the job would then "succeed"
        # with only part of the graph run (often ending in exit 5, no output).
        _log_node_errors(node_errors)
        if os.environ.get("ALLOW_PARTIAL_OUTPUTS", "") not in ("1", "true", "yes"):
            raise WorkflowFailure(
                3, f"{len(node_errors)} node(s) failed validation; their outputs "
                   "would be skipped (set ALLOW_PARTIAL_OUTPUTS=1 to run the rest anyway)",
                hf_classify.classify_node_errors(node_errors))
        log("ALLOW_PARTIAL_OUTPUTS set: running the outputs that validated")
    log(f"workflow queued, prompt_id={prompt_id}")
    if _RELAY is not None:
        _RELAY.register_prompt(prompt_id, workflow, **_wf_fields())
    return prompt_id


def _log_node_errors(node_errors):
    log("ComfyUI skipped part of the workflow; these nodes failed validation:")
    for node_id, info in node_errors.items():
        info = info if isinstance(info, dict) else {}
        log(f"  node #{node_id} ({info.get('class_type', '?')}):")
        for err in info.get("errors") or []:
            message = err.get("message", "")
            details = err.get("details", "")
            log(f"    - {message}" + (f": {details}" if details else ""))
        outputs = info.get("dependent_outputs") or []
        if outputs:
            log(f"    blocks output node(s): {', '.join(map(str, outputs))}")


def _interrupt_comfyui(prompt_id):
    """Stop the running prompt (X5 / NEW_WORK §7): /interrupt + clear the queue, then wait briefly for it to end."""
    for path, payload in (("/interrupt", {}), ("/queue", {"clear": True})):
        try:
            http_json(path, payload=payload, timeout=5)
        except (urllib.error.URLError, OSError, ValueError):
            pass
    deadline = time.monotonic() + INTERRUPT_GRACE_SECONDS
    proc = _CTX.proc
    while time.monotonic() < deadline and prompt_id:
        if proc is not None and proc.poll() is not None:
            return
        try:
            entry = http_json(f"/history/{prompt_id}", timeout=2).get(prompt_id)
        except (urllib.error.URLError, OSError, ValueError):
            entry = None
        if entry and entry.get("status"):
            _collect_temp(prompt_id, entry)
            return
        time.sleep(0.2)


def _collect_temp(prompt_id, entry):
    if _RELAY is not None and isinstance(entry, dict):
        try:
            _RELAY.collect_temp_outputs(prompt_id, entry.get("outputs") or {})
        except OSError:
            pass


def wait_done(proc, prompt_id):
    """Poll a submitted prompt to completion. start resets on every call, so
    JOB_TIMEOUT_SECONDS is (and always was) a per-render ceiling, not a whole-job
    one — calling this once per manifest entry preserves that scoping unchanged.

    Raises WorkflowFailure(1) on execution error or WorkflowFailure(4) on
    JOB_TIMEOUT_SECONDS — both recoverable, per-workflow conditions in manifest
    mode. A dead ComfyUI process raises hf.supervisor.ComfyDied (the caller
    decides between a supervised restart and exit 6); a stop signal interrupts
    ComfyUI and raises hf.supervisor.Cancelled.
    """
    start = time.monotonic()
    last_beat = start
    last_answer = start
    _CTX.phase("execute", "start", prompt_id=prompt_id, **_wf_fields())
    try:
        while True:
            elapsed = time.monotonic() - start
            if _CTX.cancel.is_set():
                log("stop signal received: interrupting ComfyUI")
                _interrupt_comfyui(prompt_id)
                _CTX.check_cancel()
            if JOB_TIMEOUT and elapsed > JOB_TIMEOUT:
                _CTX.timed_out = True
                if _CTX.manifest_total:
                    _interrupt_comfyui(prompt_id)  # the next entry must not queue behind a runaway prompt
                raise WorkflowFailure(4, f"job exceeded JOB_TIMEOUT_SECONDS={JOB_TIMEOUT}",
                                      {"code": "timeout", "message": hf_classify.message_for("timeout"),
                                       "detail": f"JOB_TIMEOUT_SECONDS={JOB_TIMEOUT}"})
            if proc.poll() is not None:
                raise _death(proc)
            try:
                history = http_json(f"/history/{prompt_id}", timeout=min(15, UNRESPONSIVE or 15))
                last_answer = time.monotonic()
            except (urllib.error.URLError, OSError):
                history = {}
                silent = time.monotonic() - last_answer
                if UNRESPONSIVE and silent > UNRESPONSIVE and proc.poll() is None:
                    raise _unresponsive(proc, silent)
            entry = history.get(prompt_id)
            if entry:
                status = entry.get("status") or {}
                if status.get("status_str") == "error":
                    cls = None
                    for item in status.get("messages", []):
                        event = item[0]
                        data = item[1] if len(item) > 1 else {}
                        if event in ("execution_error", "execution_interrupted"):
                            log(f"{event}:")
                            _raw(json.dumps(data, indent=2))
                            cls = (hf_classify.classify_execution_error(data) if event == "execution_error"
                                   else hf_classify.classify_interrupted(data))
                    _collect_temp(prompt_id, entry)
                    raise WorkflowFailure(1, "workflow execution failed", cls or {
                        "code": "execution_error", "message": hf_classify.message_for("execution_error")})
                if status.get("completed"):
                    log(f"workflow completed in {elapsed:.0f}s")
                    _collect_temp(prompt_id, entry)
                    return entry
            if time.monotonic() - last_beat >= HEARTBEAT_EVERY:
                log(f"still running ({elapsed:.0f}s elapsed)")
                last_beat = time.monotonic()
            time.sleep(POLL_INTERVAL)
    finally:
        _CTX.phase("execute", "end", prompt_id=prompt_id, **_wf_fields())


def _unresponsive(proc, silent):
    """Stop a live but silent ComfyUI; the caller handles it like a death (restart, next workflow)."""
    log(f"ComfyUI stopped answering for {silent:.0f}s; stopping it")
    proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    proc.reap_tree()
    cls = {"code": "process_exit", "message": hf_classify.message_for("process_exit"),
           "detail": f"ComfyUI did not answer HTTP for {silent:.0f}s while running a workflow; the runner stopped it",
           "signal": "SIGKILL"}
    node = _RELAY.current["node"] if _RELAY is not None else None
    class_type = _RELAY.current["class_type"] if _RELAY is not None else None
    if node is not None:
        cls = dict(cls, node=node, class_type=class_type)
    return hf_sup.ComfyDied(proc.returncode, cls, node, class_type)


def _beat(state):
    if _HEARTBEAT is not None:
        _HEARTBEAT.set_state(state)


def _recovery(action, reason, attempt, result, detail=None, **extra):
    fields = {"action": action, "reason": reason, "attempt": attempt, "result": result}
    if detail:
        fields["detail"] = detail
    fields.update({k: v for k, v in extra.items() if v is not None})
    _em().emit("recovery", **_wf_fields(), **fields)


def _report(exc):
    """Emit the [hf] error event of a WorkflowFailure once."""
    if not exc.reported and exc.classification:
        _CTX.error(exc.classification, **_wf_fields())
    exc.reported = True


def execute(workflow, targets=None):
    """submit + wait_done with the F4 out-of-memory ladder for execution errors (never changes resolution,
    frames, steps or seeds; every step is an [hf] recovery event)."""
    try:
        return wait_done(_CTX.proc, submit(workflow, targets))
    except WorkflowFailure as exc:
        cls = exc.classification or {}
        if exc.code != 1 or cls.get("code") not in _MEMORY_OOM_CODES or not _LAUNCH.ladder:
            raise
        _report(exc)
        last = exc
    attempt = 0
    for step in _LAUNCH.ladder:
        reason = (last.classification or {}).get("code")
        if step == "decode_from_latent":
            continue  # only after a process death (see _decode_from_latent)
        if step == "resubmit_cached":
            if hf_launch.cache_disabled(_PROC_ARGS):
                _recovery(step, reason, attempt + 1, "skipped",
                          "--cache-none is active: a resubmit would recompute the whole graph")
                continue
            candidate, detail = workflow, "unloading models and re-queueing; cached nodes are reused"
        else:  # decode_tiled
            node = (last.classification or {}).get("node")
            if (last.classification or {}).get("class_type") not in hf_latents.DECODE_CLASSES or node not in workflow:
                continue
            candidate = hf_latents.tile_decode(workflow, node)
            if candidate is None:
                _recovery(step, reason, attempt + 1, "skipped", "tiles are already at the minimum size")
                continue
            tiles = candidate[node]["inputs"]
            detail = (f"node {node} decoded with VAEDecodeTiled (tile {tiles['tile_size']}, "
                      f"temporal {tiles['temporal_size']} frames); resolution and frame count unchanged")
        attempt += 1
        _recovery(step, reason, attempt, "started", detail)
        try:
            http_json("/free", payload={"unload_models": True}, timeout=10)
        except (urllib.error.URLError, OSError, ValueError):
            pass
        try:
            entry = wait_done(_CTX.proc, submit(candidate, targets))
        except WorkflowFailure as exc:
            _recovery(step, reason, attempt, "failed", (exc.classification or {}).get("code"))
            _report(exc)
            last = exc
            if exc.code != 1 or (exc.classification or {}).get("code") not in _MEMORY_OOM_CODES:
                raise
            continue
        _recovery(step, reason, attempt, "succeeded")
        return entry
    raise last


def _restart_after_death(died):
    """F3: restart ComfyUI (budget per HF_RESTART_POLICY, default one per manifest entry) with memory-safe flags when
    the death was memory-related. A crash loop (MAX_CONSECUTIVE_DEATHS deaths with no succeeded entry between them)
    is not restarted again."""
    _CTX.consecutive_deaths += 1
    if _CTX.restarts >= hf_launch.restart_budget(_LAUNCH, _CTX.manifest_total or 1):
        return False
    if _CTX.consecutive_deaths >= MAX_CONSECUTIVE_DEATHS:
        log(f"ComfyUI died {MAX_CONSECUTIVE_DEATHS} consecutive times; not restarting again")
        return False
    code = died.classification.get("code")
    added = hf_launch.memory_safe_restart_args(_ALLOW) if hf_classify.is_memory_related(code) else []
    args = hf_launch.merge_args(list(_LAUNCH.args), added, _ALLOW) if added else list(_LAUNCH.args)
    _CTX.restarts += 1
    budget = hf_launch.restart_budget(_LAUNCH, _CTX.manifest_total or 1)
    _recovery("restart_comfyui", code, _CTX.restarts, "started",
              f"restart {_CTX.restarts} of {budget}", args=added)
    log(f"restarting ComfyUI after it died (restart {_CTX.restarts} of {budget})")
    _beat("restarting")
    _CTX.proc = start_comfyui(args)
    try:
        _after_ready(wait_ready(_CTX.proc))
    except SystemExit:
        _recovery("restart_comfyui", code, _CTX.restarts, "failed")
        raise
    _recovery("restart_comfyui", code, _CTX.restarts, "succeeded")
    return True


def _decode_candidate(workflow, died, before):
    """(decode node id, latent path) when a death hit a decode whose input latent was saved (F2), else None."""
    if "decode_from_latent" not in _LAUNCH.ladder:
        return None
    nodes = [nid for nid, node in workflow.items()
             if isinstance(node, dict) and node.get("class_type") in hf_latents.DECODE_CLASSES
             and hf_latents.saved_latent_nodes(workflow, nid)]
    if died.node in nodes:
        node = died.node
    elif died.node is None and len(nodes) == 1:
        node = nodes[0]
    else:
        return None
    latents = hf_latents.new_latent_files(before, OUTPUT_DIR)
    if not latents:
        return None
    return node, max(latents, key=lambda p: p.stat().st_mtime)


def _decode_from_latent(workflow, candidate, targets=None):
    node, latent = candidate
    reason = "decode_after_death"
    try:
        prompt = hf_latents.decode_from_latent(workflow, node, latent.name, tiled="decode_tiled" in _LAUNCH.ladder)
    except ValueError as err:
        _recovery("decode_from_latent", reason, 1, "skipped", str(err))
        return None
    shutil.copy2(latent, STAGED_INPUT_DIR / latent.name)  # LoadLatent reads from the input directory
    _recovery("decode_from_latent", reason, 1, "started",
              f"decoding the saved latent {latent.relative_to(OUTPUT_DIR).as_posix()} (node {node})")
    try:
        entry = wait_done(_CTX.proc, submit(prompt))
    except WorkflowFailure as exc:
        _recovery("decode_from_latent", reason, 1, "failed", (exc.classification or {}).get("code"))
        raise
    _recovery("decode_from_latent", reason, 1, "succeeded")
    return entry


def report_outputs():
    _CTX.phase("collect_outputs", "start")
    files = hf_outputs.list_outputs(OUTPUT_DIR)
    for f in files:
        size = f.stat().st_size
        log(f"output: {f.relative_to(OUTPUT_DIR)} ({size:,} bytes)")
        _CTX.output(f.relative_to(OUTPUT_DIR).as_posix(), size, hf_outputs.kind_of(f))
    _CTX.phase("collect_outputs", "end")
    if not files:
        die(5, "workflow completed but wrote nothing to /output — "
               "does the workflow contain a save node?")
    return files


def _output_snapshot():
    """Manifest mode only: every file currently under /output (minus /output/_halofuse), for before/after diffing."""
    return hf_outputs.snapshot(OUTPUT_DIR)


def _collect_new_outputs(before, subdir_name):
    """Move files that appeared under /output since *before* into /output/<subdir_name>/.

    Diffing (not a fresh-subfolder-per-entry render target) because ComfyUI's
    --output-directory is fixed for the whole process; this is the only way to
    tell one workflow's files apart from another's without touching ComfyUI's
    save nodes. run_manifest() calls this unconditionally, whether the entry
    succeeded, failed, died or was cancelled partway through, and always before
    the next entry's "before" snapshot is taken — so an already-moved prior
    entry's files (complete or partial) never look "new" here, and no manifest
    entry can steal another's outputs. Each file keeps its subfolder below
    wf_NN/ and nothing is ever overwritten (X4).

    Fallback (single-workflow) mode never calls this; report_outputs() above is
    untouched and keeps the flat /output layout old bridges expect.
    """
    moved = hf_outputs.collect_new(before, OUTPUT_DIR, subdir_name)
    for dest in moved:
        size = dest.stat().st_size
        log(f"output: {dest.relative_to(OUTPUT_DIR)} ({size:,} bytes)")
        _CTX.output(dest.relative_to(OUTPUT_DIR).as_posix(), size, hf_outputs.kind_of(dest), **_wf_fields())
    return moved


def run_manifest(proc, entries):
    """Run every manifest entry against the live ComfyUI process.

    Continue-on-failure: a WorkflowFailure (raised for codes 1/3/4) from one
    entry is logged and that entry is marked failed; the loop moves on to the
    next entry rather than aborting the job. A ComfyUI death marks the entry
    failed, keeps its partial files in wf_NN/ (X6) and — when HF_RESTART_POLICY
    allows — restarts ComfyUI and continues with the next entry (F3). The dead
    entry itself is not re-run (except a decode-only re-run from a saved latent,
    F2). With HF_RESTART_POLICY=off a death exits 6 exactly as before.

    _collect_new_outputs() runs unconditionally, so a failed entry's partial
    output (e.g. 2 of 3 batch renders done before it failed) still gets moved
    into that entry's wf_NN/ folder instead of being left unattributed at the
    flat /output root. An entry that produced nothing at all (success or
    failure) gets no wf_NN/ folder.

    Returns the job's overall exit code: 0 if every entry succeeded, 1 if any
    failed (including "ran clean but produced no files").
    """
    total = len(entries)
    any_failed = False
    for idx, (workflow, batch_count, targets) in enumerate(entries, start=1):
        _CTX.check_cancel()
        _CTX.current_wf = idx
        log(f"workflow {idx}/{total} started")
        _CTX.item(idx, "started")
        before = _output_snapshot()
        failure = None
        died = None
        cancelled = False
        try:
            if batch_count > 1:
                log(f"workflow {idx}/{total}: batch render, {batch_count} "
                    f"image(s) in this workflow (model already warm)")
            for b in range(batch_count):
                if b > 0:
                    _CTX.check_cancel()
                    n_seeds = randomize_seeds(workflow)
                    log(f"workflow {idx}/{total}: render {b + 1}/{batch_count} "
                        f"(randomised {n_seeds} seed input(s))")
                execute(workflow, targets)
        except WorkflowFailure as exc:
            failure = exc
        except hf_sup.ComfyDied as exc:
            died = exc
        except hf_sup.Cancelled:
            cancelled = True

        reason = None
        decoded = False
        if died is not None:
            reason = f"ComfyUI died mid-run (code {died.returncode})"
            _CTX.error(died.classification, **_wf_fields())
            if hf_launch.restart_budget(_LAUNCH, total) == 0:
                # Frozen pre-HaloFuse behaviour: the job ends here (exit 6). Partial files still move (X6).
                moved = _collect_new_outputs(before, f"wf_{idx:02d}")
                _CTX.item(idx, "failed", len(moved), died.classification.get("code"), reason)
                die(6, reason, None)
            restarted = False
            try:
                restarted = _restart_after_death(died)
                if restarted:
                    candidate = _decode_candidate(workflow, died, before)
                    if candidate is not None and _decode_from_latent(workflow, candidate, targets) is not None:
                        decoded = True
            except WorkflowFailure as exc:
                failure = exc
            except hf_sup.ComfyDied as again:
                _CTX.error(again.classification, **_wf_fields())
                restarted = _restart_after_death(again)
            except hf_sup.Cancelled:
                cancelled = True
            if not restarted and not cancelled:
                moved = _collect_new_outputs(before, f"wf_{idx:02d}")
                suffix = f" ({len(moved)} partial file(s))" if moved else ""
                log(f"workflow {idx}/{total} failed: {reason}{suffix}")
                _CTX.item(idx, "failed", len(moved), died.classification.get("code"), reason)
                die(6, reason, None)

        # Always collect whatever this entry wrote, success or failure, before
        # the next entry's "before" snapshot is taken.
        moved = _collect_new_outputs(before, f"wf_{idx:02d}")
        suffix = f" ({len(moved)} partial file(s))" if moved else ""

        if cancelled:
            log(f"workflow {idx}/{total} failed: cancelled{suffix}")
            _CTX.items[idx] = {"wf": idx, "status": "cancelled", "outputs": len(moved), "error": "cancelled"}
            _CTX.em.emit("item", wf=idx, of=total, state="failed", outputs=len(moved), error="cancelled",
                         reason="cancelled")
            raise hf_sup.Cancelled()

        if failure is not None:
            any_failed = True
            _report(failure)
            log(f"workflow {idx}/{total} failed: {failure.message}{suffix}")
            _CTX.item(idx, "failed", len(moved), (failure.classification or {}).get("code"), failure.message)
            continue

        if died is not None and not decoded:
            any_failed = True
            log(f"workflow {idx}/{total} failed: {reason}{suffix}")
            _CTX.item(idx, "failed", len(moved), died.classification.get("code"), reason)
            continue

        if not moved:
            any_failed = True
            no_output = "workflow completed but wrote nothing to /output — does the workflow contain a save node?"
            log(f"workflow {idx}/{total} failed: workflow completed but wrote "
                "nothing to /output — does the workflow contain a save node?")
            _CTX.error({"code": "no_output", "message": hf_classify.message_for("no_output")}, **_wf_fields())
            _CTX.item(idx, "failed", 0, "no_output", no_output)
            continue

        log(f"workflow {idx}/{total} succeeded ({len(moved)} file(s))")
        _CTX.consecutive_deaths = 0
        _CTX.item(idx, "succeeded", len(moved))
    _CTX.current_wf = None
    return 1 if any_failed else 0


def shutdown(proc):
    if proc is None or proc.poll() is not None:
        return
    _CTX.phase("shutdown", "start")
    _beat("finishing")
    log("shutting down ComfyUI")
    proc.terminate()  # the whole process group: no child outlives the job
    try:
        proc.wait(timeout=5 if _CTX.cancel.is_set() else 15)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    proc.reap_tree()
    _CTX.phase("shutdown", "end")


def randomize_seeds(workflow):
    """Give each batch render a fresh seed so the images differ. ComfyUI caches
    node outputs, so an identical re-submit would return the same image; changing
    the seed re-runs only sampling onward, reusing the already-loaded model."""
    changed = 0
    for node in workflow.values():
        inputs = node.get("inputs") if isinstance(node, dict) else None
        if not isinstance(inputs, dict):
            continue
        for key in inputs:
            value = inputs[key]
            if key in SEED_KEYS and isinstance(value, int) and not isinstance(value, bool):
                inputs[key] = random.randint(0, 2**32 - 1)
                changed += 1
    return changed


def _single_targets():
    raw = os.environ.get("HF_PARTIAL_TARGETS")
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        die(2, "HF_PARTIAL_TARGETS must be a JSON list of node ids")
    return _validate_targets(value, "HF_PARTIAL_TARGETS")


def _start_telemetry():
    global _SAMPLER, _TELEMETRY
    disk = {"output": OUTPUT_DIR}
    if Path("/tmp").is_dir():
        disk["tmp"] = Path("/tmp")
    _SAMPLER = hf_telemetry.Sampler(proc_root=PROC_ROOT, cgroup_root=CGROUP_ROOT, smi_cmd=NVIDIA_SMI,
                                    disk_paths=disk)
    _CTX.peaks = _SAMPLER.peaks
    raw = os.environ.get("HF_TELEMETRY_INTERVAL")
    if (raw or "").strip().lower() in ("off", "0"):
        return
    _TELEMETRY = hf_telemetry.TelemetryThread(
        _SAMPLER, emit=lambda payload: _em().emit("telemetry", **payload),
        interval=hf_telemetry.interval_from_env(raw)).start()


def _stop_background():
    _PREFETCH_STOP.set()
    deadline = time.monotonic() + 3.0  # a copy stops within one chunk; never wait for the whole file
    for t in list(_PREFETCH_THREADS):
        t.join(max(0.0, deadline - time.monotonic()))
    for worker in (_TELEMETRY, _WS):
        if worker is not None:
            try:
                worker.stop()
            except Exception:  # noqa: BLE001
                pass


def _run():
    global _CTX, _RELAY, _HEARTBEAT
    nonce = os.environ.get("HF_EVENT_NONCE") or None
    nonce_ok = nonce is None or hf_events.valid_nonce(nonce)
    em = hf_events.install(hf_events.Emitter(nonce=nonce if nonce_ok else None))
    _CTX = hf_sup.RunContext(em)
    hf_sup.install_signal_handlers(_CTX)
    _HEARTBEAT = hf_sup.Heartbeat(HEALTH_FILE, HEARTBEAT_SECONDS, port=PORT).start()
    _CTX.phase("boot", "start")
    log_banner()

    # Manifest presence decides the mode entirely: an old bridge never writes
    # a manifest, so it always takes the fallback branch below,
    # unchanged from before this file gained manifest support.
    global MANIFEST_PATH
    MANIFEST_PATH = _manifest_path()
    manifest = load_manifest() if MANIFEST_PATH.is_file() else None
    workflow = None if manifest is not None else load_workflow()
    if manifest is not None:
        _CTX.manifest_total = len(manifest)
    if not nonce_ok:
        die(2, "HF_EVENT_NONCE is malformed (16-64 characters of A-Z a-z 0-9 _ -)",
            {"code": "bad_launch_args", "message": hf_classify.message_for("bad_launch_args")})
    targets = _single_targets() if manifest is None else None
    resolve_launch()

    check_output_writable()
    em.attach_dir(OUTPUT_DIR / hf_outputs.RESERVED_DIR)
    _CTX.phase("boot", "end")
    _CTX.phase("model_paths", "start")
    write_extra_model_paths()
    _CTX.phase("model_paths", "end")
    _CTX.phase("stage_inputs", "start")
    stage_input_directory()
    _CTX.phase("stage_inputs", "end")
    _RELAY = hf_previews.LiveRelay(
        em, output_dir=OUTPUT_DIR, temp_root=COMFY_TEMP_DIR / "temp",
        previews_enabled=(os.environ.get("HF_PREVIEW") or "off").strip().lower() == "live",
        throttle=hf_events.PreviewThrottle(float(os.environ.get("HF_PREVIEW_MIN_INTERVAL") or 2.0)))
    _start_telemetry()
    prefetch = None
    if PREFETCH_MODELS:
        workflows = [entry[0] for entry in manifest] if manifest is not None else [workflow]
        prefetch = threading.Thread(target=_prefetch_with_phase, args=(workflows,), daemon=True)
        prefetch.start()
    proc = start_comfyui()
    _CTX.proc = proc
    exit_code = 0
    try:
        _after_ready(wait_ready(proc))
        if prefetch is not None and prefetch.is_alive():
            log("waiting for model prefetch to finish before submitting")
            _CTX.phase("prefetch_wait", "start")
            while prefetch.is_alive():
                prefetch.join(0.5)
                _CTX.check_cancel()
            _CTX.phase("prefetch_wait", "end")
        if manifest is not None:
            exit_code = run_manifest(proc, manifest)
        else:
            if BATCH_COUNT > 1:
                log(f"batch render: {BATCH_COUNT} images in one job (model loads once)")
            try:
                for i in range(BATCH_COUNT):
                    if i > 0:
                        _CTX.check_cancel()
                        n = randomize_seeds(workflow)
                        log(f"render {i + 1}/{BATCH_COUNT} (randomised {n} seed input(s))")
                    elif BATCH_COUNT > 1:
                        log(f"render 1/{BATCH_COUNT}")
                    before = _output_snapshot()
                    try:
                        execute(workflow, targets)
                    except hf_sup.ComfyDied as died:
                        reason = f"ComfyUI died mid-run (code {died.returncode})"
                        candidate = _decode_candidate(workflow, died, before)
                        if candidate is None:
                            die(6, reason, died.classification)
                        _CTX.error(died.classification)
                        if not _restart_after_death(died) or _decode_from_latent(workflow, candidate) is None:
                            die(6, reason, None)
            except WorkflowFailure as exc:
                die(exc.code, exc.message, None if exc.reported else exc.classification)
            files = report_outputs()
            log(f"job complete: {len(files)} file(s) in {OUTPUT_DIR}")
    except hf_sup.Cancelled:
        die(1, "job cancelled by a stop signal; partial outputs were kept",
            {"code": "cancelled", "message": hf_classify.message_for("cancelled")})
    finally:
        shutdown(_CTX.proc)

    if manifest is not None:
        if exit_code == 0:
            log(f"job complete: all {len(manifest)} workflow(s) succeeded")
        else:
            log("job complete: one or more workflows failed")
        sys.exit(exit_code)


def main():
    """Entry point. Guarantees exactly one [hf] final event and a flushed render.log for every exit path
    except SIGKILL."""
    try:
        _run()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        _finish(code)
        raise
    except BaseException:
        if _CTX is not None:
            _CTX.error({"code": "runner_error", "message": hf_classify.message_for("runner_error"),
                        "detail": traceback.format_exc()[-8000:]})
        _finish(1)
        raise
    _finish(0)


def _finish(code):
    _stop_background()
    if _CTX is not None:
        _CTX.finish(code)
    if _HEARTBEAT is not None:
        _HEARTBEAT.stop("finished", code)
    hf_events.get().close()


if __name__ == "__main__":
    main()

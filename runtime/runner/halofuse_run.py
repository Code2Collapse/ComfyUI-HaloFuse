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
from hf import error_report as hf_error_report  # noqa: E402
from hf import events as hf_events  # noqa: E402
from hf import housekeeping as hf_housekeeping  # noqa: E402
from hf import latents as hf_latents  # noqa: E402
from hf import launch as hf_launch  # noqa: E402
from hf import outputs as hf_outputs  # noqa: E402
from hf import prefetch as hf_prefetch  # noqa: E402
from hf import previews as hf_previews  # noqa: E402
from hf import storage as hf_storage  # noqa: E402
from hf import supervisor as hf_sup  # noqa: E402
from hf import telemetry as hf_telemetry  # noqa: E402
from hf import user_nodes as hf_user_nodes  # noqa: E402
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
# L9.338: the prompt is submitted as soon as ComfyUI is up; the copies keep running in first-use order and a model
# load that needs an unfinished copy waits for that copy only (hf/prefetch.py + hf/model_gate.py, shared state here).
PREFETCH_STATE_DIR = Path(os.environ.get("HF_PREFETCH_STATE", "/tmp/halofuse-prefetch"))
# Kill switch: HF_PREFETCH_BEFORE_SUBMIT=1 restores the old behaviour (every copy finishes before the prompt is sent).
PREFETCH_BEFORE_SUBMIT = os.environ.get("HF_PREFETCH_BEFORE_SUBMIT", "").lower() in ("1", "true", "yes")
GATE_FILE_NAME = "halofuse_model_gate.py"
GATE_SOURCE = _HERE / "hf" / "model_gate.py"
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
# The artist's own custom nodes (HF_USER_NODES, hf/user_nodes.py) are installed here before ComfyUI starts.
CUSTOM_NODES_DIR = Path(os.environ.get("HF_CUSTOM_NODES_DIR", str(COMFYUI_DIR / "custom_nodes")))
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
# Manifest mode: prompt fingerprint -> (entry index, files that entry delivered). An identical later entry that
# ComfyUI serves entirely from its cache writes no file; it gets copies of these (see _cached_success).
_DELIVERED = {}
_PREFETCH_STOP = threading.Event()  # set on exit: a running model copy stops between chunks
_PREFETCH_THREADS = []  # copy workers, joined briefly on exit so a stopped copy can remove its .part file
_PREFETCH_PLANNED = threading.Event()  # set once the copy plan is written (or there is none): the prompt may go
_PREFETCHER = None   # hf.prefetch.Prefetcher of this job, when models are copied
_MODEL_WAIT = {"open": None, "total": 0.0}  # gate waits (monotonic): JOB_TIMEOUT_SECONDS does not count them
_USER_NODES = None   # hf.user_nodes.UserNodes when the job brought a custom node list
_HOUSEKEEPER = None  # hf.housekeeping.Housekeeper: cleaning between manifest entries (HF_CLEAN_BETWEEN / _UNLOAD_)
_ERROR_REPORT = None  # hf.error_report.ErrorReport: /output/_halofuse/error.json (L9.338)
_COMFY_LOG = None     # hf.error_report.ComfyLog: /output/_halofuse/comfyui.log
_SUBMITTED = None     # the workflow last submitted (node titles for the error report)


class _StartupFailedWithUserNodes(Exception):
    """ComfyUI died during start-up while the artist's own packs were installed: start once more without them."""


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


def _runner_dir():
    return OUTPUT_DIR / hf_outputs.RESERVED_DIR


def _finish_error_file(code):
    """L9.338: the job's result goes into error.json (when the job failed or anything was recorded), so the bridge
    explains the failure that ENDED the job, not an earlier recovered one. Never raises."""
    global _ERROR_REPORT
    try:
        if _CTX is None or (code == 0 and _ERROR_REPORT is None):
            return
        if _ERROR_REPORT is None:
            _ERROR_REPORT = hf_error_report.ErrorReport(_runner_dir())
        status = _CTX.final_status(code)
        error = None if status == "succeeded" else ("cancelled" if status == "cancelled"
                                                    else (_CTX.last_error or {}).get("code"))
        _ERROR_REPORT.finish({"exit_code": code, "status": status, "error": error,
                              "detail": (_CTX.last_error or {}).get("detail") if error else None})
    except Exception:  # noqa: BLE001
        pass


def _error_file(kind, classification, data=None, returncode=None, prompt_id=None):
    """L9.338: write the complete error into /output/_halofuse/error.json (delivered even when the job fails), so the
    bridge never depends on the lossy cloud log stream for it. Never raises."""
    global _ERROR_REPORT
    try:
        if _ERROR_REPORT is None:
            _ERROR_REPORT = hf_error_report.ErrorReport(_runner_dir())
        sample = peaks = None
        if _SAMPLER is not None:
            try:
                sample = _SAMPLER.sample()
                peaks = _SAMPLER.peaks()
            except Exception:  # noqa: BLE001 - a broken probe never hides the error itself
                pass
        proc = _CTX.proc if _CTX is not None else None
        tail = proc.tail() if proc is not None and hasattr(proc, "tail") else ()
        wf = _wf_fields()
        entry = hf_error_report.build_entry(kind, classification, data=data, workflow=_SUBMITTED, tail=tail,
                                            sample=sample, peaks=peaks, returncode=returncode, prompt_id=prompt_id,
                                            wf=wf.get("wf"), of=wf.get("of"))
        _ERROR_REPORT.record(entry)
    except Exception:  # noqa: BLE001
        pass


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
    actual = _actual_model_dirs()
    lines = []
    if PREFETCH_MODELS:
        # Listed first: ComfyUI returns the first match, so a prefetched local
        # copy wins over the /assets original. Files never prefetched are still
        # found under halofuse below.
        lines += ["halofuse_local:", f"  base_path: {LOCAL_MODEL_DIR}"]
        lines += [f"  {name}: {actual.get(name, name)}" for name in names]
    lines += ["halofuse:", f"  base_path: {MODEL_BASE_DIR}"]
    lines += [f"  {name}: {actual.get(name, name)}" for name in names]
    for name, real in actual.items():
        if real != name:
            log(f"model folder {name} is spelled {real} in the model library; ComfyUI searches it under that name")
    EXTRA_MODEL_PATHS.write_text("\n".join(lines) + "\n")
    log(f"model search base: {MODEL_BASE_DIR}")
    if extra:
        log(f"extra model folders: {', '.join(extra)}")
    _link_extra_model_dirs(extra)


def _actual_model_dirs():
    """{standard folder: the library directory with that name in any letter case} (owner 2026-11-24: the library
    check matched names without regard to case, the Linux mount does not - 'VAE' was invisible as 'vae')."""
    try:
        present = [p.name for p in Path(MODEL_BASE_DIR).iterdir() if p.is_dir()]
    except OSError:
        return {}
    out = {}
    for name in MODEL_SUBDIRS:
        if name in present:
            continue
        match = next((d for d in present if d.lower() == name.lower()), None)
        if match:
            out[name] = match
    return out


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


def _resolve_model_names(manifest, workflow):
    """Point every model name at a file the cloud really has (hf/prefetch.resolve_model_names); log what changed and
    say plainly which names exist nowhere in the model library (owner 2026-11-24: check said OK, cloud said missing)."""
    workflows = [entry[0] for entry in manifest] if manifest is not None else [workflow]
    extra = [n for n in _extra_model_dirs() if n not in MODEL_SUBDIRS]
    try:
        changes, missing = hf_prefetch.resolve_model_names(workflows, Path(MODEL_BASE_DIR),
                                                           [_actual_model_dirs().get(n, n) for n in MODEL_SUBDIRS] + extra, MODEL_EXTS)
    except Exception as err:  # noqa: BLE001 - never stop a job over this lookup
        log(f"model name check skipped ({type(err).__name__})")
        return
    for node_id, key, old, new in changes:
        log(f"model path fixed: node #{node_id} {key}: {old} -> {new} (found by file name in the model library)")
    for node_id, key, name in missing:
        log(f"model not in the cloud model library: node #{node_id} {key}: {name}")


def _referenced_model_files(workflows):
    """{rel: size} of the model files under MODEL_BASE_DIR that these API-format workflows name, in first-use order.

    Any string input ending in a model extension is looked up in each standard model folder and in every extra
    (custom-node) folder at the top of MODEL_BASE_DIR (there only when its directory holds nothing but model files);
    every match is returned as a path relative to MODEL_BASE_DIR.
    """
    extra = [n for n in _extra_model_dirs() if n not in MODEL_SUBDIRS]
    return dict(hf_prefetch.plan_files(workflows, Path(MODEL_BASE_DIR), [_actual_model_dirs().get(n, n) for n in MODEL_SUBDIRS] + extra, MODEL_EXTS,
                                       extra_folders=extra))


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


def _remove_gate():
    try:
        (CUSTOM_NODES_DIR / GATE_FILE_NAME).unlink()
    except OSError:
        pass


def _install_gate():
    """hf/model_gate.py into ComfyUI's custom_nodes: a model load waits for its unfinished local copy."""
    try:
        source = GATE_SOURCE.read_text(encoding="utf-8")
        CUSTOM_NODES_DIR.mkdir(parents=True, exist_ok=True)
        target = CUSTOM_NODES_DIR / GATE_FILE_NAME
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(source + "\n_comfy_entry()\n", encoding="utf-8")
        os.replace(tmp, target)
    except OSError as err:
        log(f"prefetch: the model-load gate could not be installed ({err}); ComfyUI finds unfinished copies in the "
            "model library instead")


COMPAT_FILE_NAME = "halofuse_compat_shims.py"
COMPAT_SOURCE = _HERE / "hf" / "compat_shims.py"


def _install_compat():
    """hf/compat_shims.py into ComfyUI's custom_nodes: older node packs keep working on a newer core (owner 2026-11-24)."""
    try:
        if not CUSTOM_NODES_DIR.is_dir():  # a real ComfyUI always has it; never create one just for the shim
            return
        source = COMPAT_SOURCE.read_text(encoding="utf-8")
        target = CUSTOM_NODES_DIR / COMPAT_FILE_NAME
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(source, encoding="utf-8")
        os.replace(tmp, target)
    except OSError as err:
        log(f"compat shims could not be installed ({err})")


def _clear_plan():
    """No plan for this job: the gate (if ComfyUI loaded it) passes every lookup through; an earlier job's plan in a
    reused machine must not apply."""
    for name in ("plan.json", "plan.json.tmp"):
        try:
            (PREFETCH_STATE_DIR / name).unlink()
        except OSError:
            pass


def _prepare_prefetch(workflows):
    """Find the job's model files and write the copy plan in first-use order (the gate reads it). Runs in the prefetch
    thread while ComfyUI starts; the prompt is submitted only after this returns (_PREFETCH_PLANNED). Returns the
    hf.prefetch.Prefetcher, or None when nothing is copied."""
    try:
        _clear_plan()
        files = list(_referenced_model_files(workflows).items())
        if not files:
            log("prefetch: no model files to copy")
            return None
        total = sum(size for _, size in files)
        ok, reason = _prefetch_decision(total)
        if not ok:
            log(f"prefetch: skipped, {reason}")
            return None
        prefetcher = hf_prefetch.Prefetcher(Path(MODEL_BASE_DIR), LOCAL_MODEL_DIR, PREFETCH_STATE_DIR, files,
                                             threads=PREFETCH_THREADS, stop=_PREFETCH_STOP, log=log,
                                             workers=_PREFETCH_THREADS)
        prefetcher.write_plan()
        log(f"prefetch: copying {len(files)} model file(s), {total / 2**30:.1f} GiB, to local NVMe "
            f"({LOCAL_MODEL_DIR}) in first-use order")
        return prefetcher
    except Exception as err:  # noqa: BLE001 - prefetch is an optimisation only
        log(f"prefetch: skipped after error ({err})")
        _clear_plan()
        return None


def prefetch_models(prefetcher):
    """Run the planned copies. Copies run in daemon threads and stop between chunks when the job ends, so a
    cancelled or failed job never waits for a multi-GiB copy."""
    try:
        total = sum(size for _, size in prefetcher.files)
        started = time.time()
        if not prefetcher.run():
            return
        elapsed = time.time() - started
        log(f"prefetch: done in {elapsed:.0f}s "
            f"({total / 2**20 / max(elapsed, 0.001):.0f} MiB/s overall)")
    except Exception as err:  # noqa: BLE001 - prefetch is an optimisation only
        log(f"prefetch: stopped after error ({err})")


def _prefetch_with_phase(workflows):
    """The prefetch thread: plan (then _PREFETCH_PLANNED is set), then copy. `finished` is always written, so a model
    load waiting in the gate never waits for a prefetch that is gone."""
    global _PREFETCHER
    try:
        if _CTX is not None:
            _CTX.phase("prefetch", "start")
        try:
            _PREFETCHER = _prepare_prefetch(workflows)
        finally:
            _PREFETCH_PLANNED.set()
        if _PREFETCHER is not None:
            prefetch_models(_PREFETCHER)
    finally:
        try:
            PREFETCH_STATE_DIR.mkdir(parents=True, exist_ok=True)
            (PREFETCH_STATE_DIR / "finished").touch()
        except OSError:
            pass
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


def install_user_nodes():
    """The artist's own custom node packs (HF_USER_NODES), installed before ComfyUI starts. Never fails the job: a
    pack that cannot be installed is moved to custom_nodes_disabled/ and named in the log."""
    global _USER_NODES
    packs, problems = hf_user_nodes.parse(os.environ.get(hf_user_nodes.ENV) or "")
    for problem in problems:
        log(f"custom nodes: {problem}")
    if not packs:
        return
    _CTX.phase("user_nodes", "start")
    _USER_NODES = hf_user_nodes.UserNodes(CUSTOM_NODES_DIR, python=sys.executable,
                                          cache=os.environ.get(hf_user_nodes.CACHE_ENV) or None, log=log,
                                          existing=hf_user_nodes.image_packs(NODE_SNAPSHOT_PATH),
                                          stop=_CTX.cancel.is_set)
    try:
        results = _USER_NODES.install(packs)
    except Exception as err:  # noqa: BLE001 - an extra pack never ends the job
        log(f"custom nodes: the install step stopped ({type(err).__name__}: {err}); the job continues")
        results = []
    done = sum(1 for r in results if r["status"] == "installed")
    _CTX.phase("user_nodes", "end", detail=f"{done} of {len(packs)} installed")


def _ready_with_user_nodes(proc, args=None):
    """wait_ready(); when ComfyUI died during start-up with the artist's packs installed, they are all moved aside
    and ComfyUI starts once more without them (the job is never lost to an extra pack)."""
    try:
        return wait_ready(proc)
    except _StartupFailedWithUserNodes:
        proc.reap_tree()
        _USER_NODES.disable_all("ComfyUI did not start with your custom nodes, so they are disabled for this job")
        _CTX.proc = start_comfyui(args)
        return wait_ready(_CTX.proc)


def _check_user_nodes(workflows):
    """After ComfyUI is ready: user packs that did not import are moved aside, and the workflow's node classes that
    ComfyUI still lacks are named (instead of a bare 'node type not found' later)."""
    if _USER_NODES is None:
        return
    _USER_NODES.check_imports()
    try:
        info = http_json("/object_info", timeout=60)
    except (urllib.error.URLError, OSError, ValueError):
        return
    missing = hf_user_nodes.missing_classes(workflows, info)
    if missing:
        shown = ", ".join(missing[:20]) + (f" and {len(missing) - 20} more" if len(missing) > 20 else "")
        log(f"custom nodes: these node classes of the workflow are missing in this job: {shown}")


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
    global _COMFY_LOG
    if _COMFY_LOG is None:  # created before the pump threads start: they only ever write to it (L9.338)
        _COMFY_LOG = hf_error_report.ComfyLog(_runner_dir())
    _CTX.phase("comfy_start", "start")
    _mark_discarded_previews(args)
    # stdout/stderr are relayed line by line so ComfyUI's own logs still reach
    # the cloud log stream (same stream, same text) and render.log.
    return hf_sup.ComfyProcess(cmd, COMFYUI_DIR, env, _em(),
                               on_node_import=lambda d: _CTX.phase("node_import", "mark", dur=round(d, 3)),
                               on_line=_comfy_line).start()


def _comfy_line(text):
    """Every ComfyUI output line goes to the preview relay when it consumes them (R-PREVIEW-01), and to
    /output/_halofuse/comfyui.log (L9.338: the runner's own capture, independent of the cloud stream)."""
    if _COMFY_LOG is not None:
        _COMFY_LOG.write(text)
    consume = getattr(_RELAY, "on_comfy_line", None)
    if consume is not None:
        consume(text)
    if _USER_NODES is not None:
        _USER_NODES.on_comfy_line(text)
    _model_wait_line(text)


def _model_wait_seconds():
    """Seconds model loads have waited in the gate so far (an open wait counts up to now)."""
    opened = _MODEL_WAIT["open"]
    return _MODEL_WAIT["total"] + (time.monotonic() - opened if opened is not None else 0.0)


def _model_wait_line(text):
    """The gate's wait lines (hf/model_gate.py) become a model_wait phase, so slow starts are visible."""
    try:
        if text.startswith("[halofuse] model wait: "):
            _MODEL_WAIT["open"] = time.monotonic()
            _CTX.phase("model_wait", "start", detail=text[len("[halofuse] model wait: "):].split(" (", 1)[0])
        elif text.startswith("[halofuse] model wait done: "):
            if _MODEL_WAIT["open"] is not None:
                _MODEL_WAIT["total"] += time.monotonic() - _MODEL_WAIT["open"]
                _MODEL_WAIT["open"] = None
            _CTX.phase("model_wait", "end", detail=text[len("[halofuse] model wait done: "):])
    except Exception:  # noqa: BLE001 - relaying a line must never fail
        pass


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
    _error_file("process_died", cls, returncode=proc.returncode)
    return hf_sup.ComfyDied(proc.returncode, cls, node, class_type)


def wait_ready(proc):
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        _CTX.check_cancel()
        if proc.poll() is not None:
            proc.join_output(2.0)
            if _USER_NODES is not None and _USER_NODES.installed:
                log(f"ComfyUI exited during startup (code {proc.returncode}) with your custom nodes installed")
                raise _StartupFailedWithUserNodes()
            delta = None
            if _SAMPLER is not None and _OOM_BEFORE is not None:
                now = _SAMPLER.oom_kill_count()
                delta = None if now is None else now - _OOM_BEFORE
            cls = hf_classify.classify_exit(proc.returncode, oom_kill_delta=delta, tail=proc.tail(), during="startup")
            _error_file("startup_failed", cls, returncode=proc.returncode)
            die(6, f"ComfyUI exited during startup (code {proc.returncode})", cls)
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
    global _SUBMITTED
    _SUBMITTED = workflow
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
        cls = hf_classify.classify_rejection(err.code, body)
        _error_file("validation_failed", cls)
        raise WorkflowFailure(3, "workflow validation failed", cls)
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
            cls = hf_classify.classify_node_errors(node_errors)
            # owner 2026-11-24: the reason line ("Value not in list: vae_name: ...") was lost in the log stream and
            # the error file had no details - carry every node's exact message into error.json
            first_id, first = next(iter(node_errors.items()))
            lines = []
            for nid, info in node_errors.items():
                for err in (info or {}).get("errors") or []:
                    msg, det = err.get("message", ""), err.get("details", "")
                    lines.append(f"node #{nid} ({(info or {}).get('class_type', '?')}): {msg}" + (f": {det}" if det else ""))
            _error_file("validation_failed", cls, data={"node_id": first_id, "node_type": (first or {}).get("class_type"),
                                                        "exception_type": "ValidationError",
                                                        "exception_message": "\n".join(lines) or "validation failed"})
            raise WorkflowFailure(
                3, f"{len(node_errors)} node(s) failed validation; their outputs "
                   "would be skipped (set ALLOW_PARTIAL_OUTPUTS=1 to run the rest anyway)", cls)
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
    waited_before = _model_wait_seconds()
    _CTX.phase("execute", "start", prompt_id=prompt_id, **_wf_fields())
    try:
        while True:
            elapsed = time.monotonic() - start
            if _CTX.cancel.is_set():
                log("stop signal received: interrupting ComfyUI")
                _interrupt_comfyui(prompt_id)
                _CTX.check_cancel()
            # L9.338: time a model load spent waiting for its local copy is not render time (that wait used to happen
            # before submit, outside this per-render limit)
            if JOB_TIMEOUT and elapsed - (_model_wait_seconds() - waited_before) > JOB_TIMEOUT:
                _CTX.timed_out = True
                if _CTX.manifest_total:
                    _interrupt_comfyui(prompt_id)  # the next entry must not queue behind a runaway prompt
                cls = {"code": "timeout", "message": hf_classify.message_for("timeout"),
                       "detail": f"JOB_TIMEOUT_SECONDS={JOB_TIMEOUT}"}
                _error_file("timeout", cls, prompt_id=prompt_id)
                raise WorkflowFailure(4, f"job exceeded JOB_TIMEOUT_SECONDS={JOB_TIMEOUT}", cls)
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
                            cls = (hf_classify.classify_execution_error(data) if event == "execution_error"
                                   else hf_classify.classify_interrupted(data))
                            # L9.338: ONE line (a lossy stream cannot split it); the complete error is in error.json
                            log(f"{event}: {hf_error_report.oneline(data, cls)}")
                            if event == "execution_error":
                                _error_file("execution_error", cls, data=data, prompt_id=prompt_id)
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
    _error_file("unresponsive", cls, returncode=proc.returncode)
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
        _after_ready(_ready_with_user_nodes(_CTX.proc, args))
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
    for old, new in hf_outputs.fix_windows_names(OUTPUT_DIR):
        log(f"output name with a Windows backslash moved into its folder: {old.name} -> "
            f"{new.relative_to(OUTPUT_DIR).as_posix()}")
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


def _prompt_key(workflow):
    return hashlib.sha256(json.dumps(workflow, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _fully_cached(entry, workflow):
    """True when ComfyUI's history says every node of this prompt was served from its cache (execution_cached)."""
    if not isinstance(entry, dict):
        return False
    cached = set()
    for item in (entry.get("status") or {}).get("messages") or []:
        if (isinstance(item, (list, tuple)) and len(item) > 1 and item[0] == "execution_cached"
                and isinstance(item[1], dict)):
            cached.update(str(n) for n in item[1].get("nodes") or [])
    nodes = {str(k) for k in workflow}
    return bool(nodes) and nodes <= cached


def _cached_success(idx, total, workflow, entry):
    """An entry that wrote nothing because ComfyUI served the identical prompt from its cache: copy the files the
    earlier identical entry delivered into this entry's wf_NN/ (so its item `outputs` matches its folder, which the
    bridge delivers per wf_NN/). Returns the copies, or [] when this is not that case (then it stays no_output)."""
    earlier = _DELIVERED.get(_prompt_key(workflow))
    if earlier is None or not _fully_cached(entry, workflow):
        return []
    src_idx, files = earlier
    src_dir = OUTPUT_DIR / f"wf_{src_idx:02d}"
    copies = []
    for f in files:
        try:
            rel = Path(f).relative_to(src_dir)
        except ValueError:
            continue
        if not Path(f).is_file():
            continue
        dest = OUTPUT_DIR / f"wf_{idx:02d}" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dest)
        copies.append(dest)
        size = dest.stat().st_size
        log(f"output: {dest.relative_to(OUTPUT_DIR)} ({size:,} bytes, copy of {Path(f).relative_to(OUTPUT_DIR)})")
        _CTX.output(dest.relative_to(OUTPUT_DIR).as_posix(), size, hf_outputs.kind_of(dest), **_wf_fields())
    if copies:
        log(f"workflow {idx}/{total} succeeded: identical to workflow {src_idx}, served from ComfyUI's cache "
            f"({len(copies)} file(s) copied)")
    return copies


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

    An entry ComfyUI serves entirely from its cache (identical to an earlier
    entry: every node listed in execution_cached) writes no file; it succeeds
    with copies of that earlier entry's files in its own wf_NN/ and an item
    reason naming the cache. Anything else that writes nothing is no_output.

    Returns the job's overall exit code: 0 if every entry succeeded, 1 if any
    failed (including "ran clean but produced no files").
    """
    total = len(entries)
    any_failed = False
    for idx, (workflow, batch_count, targets) in enumerate(entries, start=1):
        _CTX.check_cancel()
        if idx > 1 and _HOUSEKEEPER is not None:  # the previous entry's outputs are collected: clean before this one
            _HOUSEKEEPER.between(idx - 1, total, [e[0] for e in entries[:idx - 1]], [e[0] for e in entries[idx - 1:]])
        _CTX.current_wf = idx
        log(f"workflow {idx}/{total} started")
        _CTX.item(idx, "started")
        before = _output_snapshot()
        failure = None
        died = None
        cancelled = False
        last_entry = None
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
                last_entry = execute(workflow, targets)
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
            copies = _cached_success(idx, total, workflow, last_entry)
            if copies:
                src_idx = _DELIVERED[_prompt_key(workflow)][0]
                _CTX.consecutive_deaths = 0
                _CTX.item(idx, "succeeded", len(copies),
                          reason=f"served from ComfyUI's cache: identical to workflow {src_idx}; "
                                 f"its {len(copies)} file(s) were copied")
                continue
            any_failed = True
            no_output = "workflow completed but wrote nothing to /output — does the workflow contain a save node?"
            log(f"workflow {idx}/{total} failed: workflow completed but wrote "
                "nothing to /output — does the workflow contain a save node?")
            _CTX.error({"code": "no_output", "message": hf_classify.message_for("no_output")}, **_wf_fields())
            _CTX.item(idx, "failed", 0, "no_output", no_output)
            continue

        log(f"workflow {idx}/{total} succeeded ({len(moved)} file(s))")
        _DELIVERED.setdefault(_prompt_key(workflow), (idx, list(moved)))
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


def _start_housekeeping(announce):
    """hf/housekeeping.py: cleaning between manifest entries, and pip's cache after the artist's packs install."""
    global _HOUSEKEEPER
    _HOUSEKEEPER = hf_housekeeping.Housekeeper(
        temp_dir=COMFY_TEMP_DIR / "temp", staged_dir=STAGED_INPUT_DIR,
        post_free=lambda payload: http_json("/free", payload=payload, timeout=10),
        sample=lambda: _SAMPLER.sample() if _SAMPLER is not None else None,
        log=log, mark=lambda **fields: _CTX.phase("housekeeping", "mark", **fields))
    _HOUSEKEEPER.before_user_nodes()
    if announce:
        _HOUSEKEEPER.announce()


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
    _start_housekeeping(manifest is not None)
    install_user_nodes()
    _HOUSEKEEPER.after_user_nodes()
    _RELAY = hf_previews.LiveRelay(
        em, output_dir=OUTPUT_DIR, temp_root=COMFY_TEMP_DIR / "temp",
        previews_enabled=(os.environ.get("HF_PREVIEW") or "off").strip().lower() == "live",
        throttle=hf_events.PreviewThrottle(float(os.environ.get("HF_PREVIEW_MIN_INTERVAL") or 2.0)))
    _start_telemetry()
    _install_compat()  # before ComfyUI starts: older node packs on a newer core
    _resolve_model_names(manifest, workflow)
    prefetch = None
    if PREFETCH_MODELS:
        workflows = [entry[0] for entry in manifest] if manifest is not None else [workflow]
        _install_gate()  # before ComfyUI starts; it waits only for files of the plan written below
        os.environ["HF_PREFETCH_STATE"] = str(PREFETCH_STATE_DIR)  # start_comfyui() copies os.environ
        prefetch = threading.Thread(target=_prefetch_with_phase, args=(workflows,), daemon=True)
        prefetch.start()
    else:
        _remove_gate()
    proc = start_comfyui()
    _CTX.proc = proc
    exit_code = 0
    try:
        _after_ready(_ready_with_user_nodes(proc))
        proc = _CTX.proc
        _check_user_nodes([entry[0] for entry in manifest] if manifest is not None else [workflow])
        if prefetch is not None and not PREFETCH_BEFORE_SUBMIT:
            while not _PREFETCH_PLANNED.wait(0.5):  # finding the model files; the copies themselves are not awaited
                _CTX.check_cancel()
            if prefetch.is_alive() and _PREFETCHER is not None:
                left, left_bytes = _PREFETCHER.remaining()
                log(f"submitting now; model prefetch continues in the background in first-use order ({left} file(s), "
                    f"{left_bytes / 2**30:.1f} GiB left). A model needed before its copy finishes waits for that copy "
                    "only.")
        elif prefetch is not None and prefetch.is_alive():
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
        try:  # after ComfyUI stopped: the outputs must reach cloud storage before the machine goes away
            if not _CTX.cancel.is_set():  # a stop request means: end now, do not keep billing
                _CTX.phase("output_sync", "start")
                hf_outputs.flush_outputs(OUTPUT_DIR, log)
                _CTX.phase("output_sync", "end")
        except Exception as err:  # noqa: BLE001 - never turns a finished render into a failure
            log(f"output sync wait skipped ({type(err).__name__}: {err})")

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
            cls = {"code": "runner_error", "message": hf_classify.message_for("runner_error"),
                   "detail": traceback.format_exc()[-8000:]}
            _CTX.error(cls)
            _error_file("runner_error", dict(cls, detail=traceback.format_exc()))
        _finish(1)
        raise
    _finish(0)


def _finish(code):
    _stop_background()
    if _COMFY_LOG is not None:
        _COMFY_LOG.close()
    _finish_error_file(code)
    if _CTX is not None:
        _CTX.finish(code)
    if _HEARTBEAT is not None:
        _HEARTBEAT.stop("finished", code)
    hf_events.get().close()


if __name__ == "__main__":
    main()

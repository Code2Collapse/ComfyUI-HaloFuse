"""A5: turn a failed HaloFuse job into a diagnosis an artist can act on.

`classify(events, cloud_job=None, exc=None)` reads, in order of trust:
1. the exception that ended local work (connection, storage, download, provider errors),
2. the runner's structured events (C-3 `error` / `final`, as parsed C-3 dicts or C-2 LogEntry dicts),
3. legacy runner text lines (runtime images older than the event protocol),
4. the cloud job's status and exit code (runner exit-code contract 0-6).
The raw detail is kept (redacted, URL-free) so the console can show it next to the explanation; `raw_ref` points at
the log entry that holds it. WS1/WS2/WS3 call this module and never re-implement the taxonomy.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

try:  # the redaction filter lives in the provider seam (WS1); keep diagnosis usable without it
    from .provider.redact import redact as _redact
except Exception:  # noqa: BLE001
    def _redact(text):
        return "" if text is None else str(text)

_CATALOGUE = {
    "bad_input": ("The job input was not usable",
                  "The workflow or job files could not be read, or the workflow is not in API format.",
                  "Re-submit from the HaloFuse panel; if it repeats, export the workflow again and check the log."),
    "bad_launch_args": ("Launch options rejected",
                        "A launch preset, extra ComfyUI flag or environment variable is not allowed by the cloud "
                        "runtime.",
                        "Choose the Safe preset or remove the extra flags in HaloFuse settings, then render again."),
    "startup_failed": ("ComfyUI did not start in the cloud",
                       "The cloud ComfyUI process exited or did not answer during start-up.",
                       "Render again; if it repeats, use the Debug preset and send the HaloFuse log to support."),
    "missing_node": ("A node is not available in the cloud",
                     "The workflow uses a custom node that is not installed in the selected cloud runtime profile.",
                     "Replace the node, or pick a runtime profile that includes it, then render again."),
    "missing_model": ("A model file is missing in the cloud",
                      "A loader references a model file that does not exist in your HaloFuse model library.",
                      "Upload or sync the model to the HaloFuse library (same folder and file name), then render again."),
    "invalid_model_path": ("A model path is not valid",
                           "A model reference points outside the model library or uses an unsupported path.",
                           "Select the model again in the loader node so it uses a library-relative name."),
    "invalid_value": ("A node setting was rejected",
                      "ComfyUI rejected an input value (out of range, wrong type or not one of the allowed choices).",
                      "Open the node named in the details, correct the value, and render again."),
    "validation_failed": ("The workflow did not pass validation",
                          "ComfyUI refused the workflow before running it.",
                          "Check the details for the node and input involved, fix it, and render again."),
    "vram_oom": ("Out of GPU memory",
                 "The GPU ran out of memory while running a node.",
                 "Pick a GPU with more memory, lower the batch size/frames/resolution yourself, or use the Max "
                 "Memory preset."),
    "vae_decode_oom": ("Out of GPU memory while decoding",
                       "Sampling finished but the VAE decode needed more GPU memory than was free.",
                       "Use the Max Memory preset (allows a tiled VAE decode retry) or a GPU with more memory; enable "
                       "latent saving so a finished sample is never lost."),
    "host_ram_oom": ("Out of system memory in a node",
                     "A node could not allocate host (CPU) memory.",
                     "Pick a GPU type with more system memory or use the Max Memory preset."),
    "host_ram_exhausted": ("The cloud machine ran out of system memory",
                           "The container's memory limit was reached and the system stopped ComfyUI.",
                           "Pick a GPU type with more system memory, or use the Max Memory preset (no pinned memory, "
                           "aggressive offloading)."),
    "process_killed": ("ComfyUI was stopped by the system",
                       "ComfyUI was killed without an error message; running out of system memory is the most likely "
                       "cause, but it was not confirmed.",
                       "Pick a GPU type with more system memory or use the Max Memory preset, then render again."),
    "native_crash": ("ComfyUI crashed",
                     "ComfyUI crashed in native code (for example a CUDA or driver fault in a custom node or kernel).",
                     "Render again with the Safe preset; if it repeats, use the Debug preset and send the log to "
                     "support."),
    "process_exit": ("ComfyUI stopped unexpectedly",
                     "The cloud ComfyUI process exited before the render finished.",
                     "Render again; if it repeats, check the details for the last messages before it stopped."),
    "execution_error": ("A node failed",
                        "A node raised an error while the workflow was running.",
                        "Read the error in the details, fix the node inputs, and render again."),
    "timeout": ("The render hit its time limit",
                "The job ran longer than the maximum runtime and was stopped.",
                "Raise the maximum runtime in HaloFuse settings or shorten the job, then render again."),
    "no_output": ("No output was produced",
                  "The workflow finished but no file was written.",
                  "Add a save node (Save Image, Save Video, ...) to the workflow and render again."),
    "cancelled": ("The render was cancelled",
                  "The job was stopped on request.",
                  "Partial outputs, if any, are in the job folder."),
    "runner_error": ("The cloud runner failed",
                     "The HaloFuse runner hit an internal error.",
                     "Render again; if it repeats, send the HaloFuse log to support."),
    "network_failure": ("Connection problem",
                        "HaloFuse could not reach the cloud service, or the connection dropped.",
                        "Check the internet connection; HaloFuse keeps watching the job and retries automatically."),
    "storage_failure": ("Cloud storage problem",
                        "The cloud storage reported an error while reading or writing job files.",
                        "Try again in a few minutes; outputs already delivered stay in the job folder."),
    "download_failure": ("Outputs could not be downloaded",
                         "The render finished but copying its outputs to this computer failed.",
                         "Use Retry download for this job; the cloud outputs are kept."),
    "provider_failure": ("The cloud service failed the job",
                         "The cloud service ended the job without a runner result (for example the machine was "
                         "interrupted, or no GPU was available).",
                         "Render again, or pick another GPU type."),
    "unknown": ("The cause is not known",
                "HaloFuse could not determine why the job ended this way.",
                "Open the full log in the HaloFuse console and check the last messages."),
}

CODES = tuple(_CATALOGUE)

_EXIT_CODES = {1: "execution_error", 2: "bad_input", 3: "validation_failed", 4: "timeout", 5: "no_output",
               6: "process_exit", 137: "process_killed", 139: "native_crash", 134: "native_crash"}
_NETWORK_CODES = {"unreachable", "rate_limited", "network_error", "timeout", "connection_failed"}
_STORAGE_CODES = {"storage_error", "too_large", "input_upload_failed"}
_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S+")
_LEGACY = [
    (re.compile(r"ComfyUI died mid-run \(code (?:137|-9)\)"), "process_killed"),
    (re.compile(r"ComfyUI died mid-run \(code (?:139|-11|134|-6)\)"), "native_crash"),
    (re.compile(r"ComfyUI died mid-run|ComfyUI exited during startup|ComfyUI not ready after"), "process_exit"),
    (re.compile(r"job exceeded JOB_TIMEOUT_SECONDS"), "timeout"),
    (re.compile(r"wrote nothing to /output"), "no_output"),
    (re.compile(r"missing_node_type|The custom node may not be installed"), "missing_node"),
    (re.compile(r"value_not_in_list"), "missing_model"),
    (re.compile(r"OutOfMemoryError|CUDA out of memory|Allocation on device"), "vram_oom"),
    (re.compile(r"DefaultCPUAllocator: can't allocate memory|MemoryError"), "host_ram_oom"),
]


@dataclass(frozen=True)
class Diagnosis:
    code: str
    title: str
    cause: str
    next_action: str
    raw_ref: dict | None = None
    detail: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _clean(text) -> str | None:
    if text in (None, ""):
        return None
    return _URL_RE.sub("<url>", _redact(str(text)))[:8000]


def describe(code: str, raw_ref=None, detail=None) -> Diagnosis:
    title, cause, action = _CATALOGUE.get(code, _CATALOGUE["unknown"])
    return Diagnosis(code if code in _CATALOGUE else "unknown", title, cause, action, raw_ref, _clean(detail))


def _get(obj, key):
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _from_exception(exc):
    code = getattr(exc, "code", None)
    detail = getattr(exc, "detail", None) or None
    if isinstance(code, str):
        if code in _NETWORK_CODES:
            return "network_failure", detail
        if code in _STORAGE_CODES:
            return "storage_failure", detail
        if "download" in code:
            return "download_failure", detail
        if code in _CATALOGUE and code != "unknown":
            return code, detail
        return "provider_failure", detail
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return "network_failure", type(exc).__name__
    return "unknown", type(exc).__name__


def _normalise(ev):
    """(type, payload, store seq or runner seq, text) for a C-2 LogEntry or a parsed C-3 event."""
    if not isinstance(ev, dict):
        return None, {}, None, ""
    if "kind" in ev:
        data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
        return ev.get("kind"), data, ev.get("seq"), str(ev.get("text") or "")
    return ev.get("t"), ev, ev.get("seq"), str(ev.get("text") or "")


def _error_detail(payload) -> str:
    parts = []
    for key in ("message", "class_type", "node", "exception_type", "detail"):
        value = payload.get(key)
        if value not in (None, ""):
            parts.append(f"node #{value}" if key == "node" else str(value))
    return " | ".join(parts)


def classify(events, cloud_job=None, exc=None) -> Diagnosis:
    if exc is not None:
        code, detail = _from_exception(exc)
        if code != "unknown" or not events:
            return describe(code, None, detail)

    errors, final, timeout_seen, cancel_confirmed, lines = [], None, False, False, []
    for ev in events or []:
        kind, payload, seq, text = _normalise(ev)
        if kind == "error" and payload.get("code"):
            errors.append((payload, seq))
        elif kind == "final":
            final = (payload, seq)
        elif kind == "timeout":
            timeout_seen = True
        elif kind == "cancel" and payload.get("state") in ("cancel_confirmed", "confirmed"):
            cancel_confirmed = True
        elif kind == "line":
            lines.append((text, seq))

    if final is not None:
        status = final[0].get("status")
        if status == "cancelled":
            return describe("cancelled", {"seq": final[1]})
        if status == "timed_out":
            return describe("timeout", {"seq": final[1]})
        wanted = final[0].get("error")
        if wanted:
            match = [e for e in errors if e[0].get("code") == wanted]
            payload, seq = (match[-1] if match else (final[0], final[1]))
            return describe(wanted, {"seq": seq}, _error_detail(payload) if match else None)
    if timeout_seen:
        return describe("timeout")
    if cancel_confirmed or _get(cloud_job, "status") == "cancelled":
        return describe("cancelled")
    if errors:
        payload, seq = errors[-1]
        return describe(payload["code"], {"seq": seq}, _error_detail(payload))

    legacy = _legacy(lines)
    if legacy is not None:
        return legacy

    status = _get(cloud_job, "status")
    exit_code = _get(cloud_job, "exit_code")
    if status == "smartcompute-interrupted":
        return describe("provider_failure", None, "the cloud machine was interrupted")
    if isinstance(exit_code, int) and exit_code in _EXIT_CODES:
        return describe(_EXIT_CODES[exit_code], None, f"runner exit code {exit_code}")
    if status == "failed":
        return describe("provider_failure", None, "the job failed without a runner exit code")
    if exc is not None:
        return describe("unknown", None, type(exc).__name__)
    return describe("unknown")


def _legacy(lines):
    """Old runtime images print only text. Scan from the end for the most specific known signature."""
    decode = any('"node_type": "VAEDecode' in text for text, _ in lines)
    for text, seq in reversed(lines):
        for pattern, code in _LEGACY:
            if pattern.search(text):
                if code == "vram_oom" and decode:
                    code = "vae_decode_oom"
                return describe(code, {"seq": seq} if seq is not None else None, text)
    return None

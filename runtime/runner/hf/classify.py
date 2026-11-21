"""Runner-side error taxonomy (A5, F3). Pure functions over ComfyUI error shapes and process exits.

Shapes verified against ComfyUI 0.30.1 / 0.39.0: POST /prompt 400 body `{"error": {...}, "node_errors": {...}}`
(server.py), validation error types (execution.py validate_inputs/validate_prompt), and `execution_error` message
data `{node_id, node_type, exception_message, exception_type, traceback, ...}`.
The bridge (`spark_fuse_bridge/diagnose.py`) owns the artist-facing wording; these messages are short and factual.
"""
import json
import signal as _signal

CODES = (
    "bad_input", "bad_launch_args", "startup_failed", "missing_node", "missing_model", "invalid_value",
    "validation_failed", "vram_oom", "vae_decode_oom", "host_ram_oom", "host_ram_exhausted", "process_killed",
    "native_crash", "process_exit", "execution_error", "timeout", "no_output", "cancelled", "runner_error",
)

_MESSAGES = {
    "bad_input": "The job input is missing or malformed.",
    "bad_launch_args": "The launch options were rejected before ComfyUI started.",
    "startup_failed": "ComfyUI did not start.",
    "missing_node": "The workflow uses a node that is not installed in the cloud runtime.",
    "missing_model": "The workflow references a model file that is not available in the cloud.",
    "invalid_value": "A node input has a value ComfyUI rejected.",
    "validation_failed": "ComfyUI rejected the workflow.",
    "vram_oom": "Out of GPU memory.",
    "vae_decode_oom": "Out of GPU memory while decoding (VAE decode).",
    "host_ram_oom": "Out of host (system) memory inside a node.",
    "host_ram_exhausted": "ComfyUI was killed because the container ran out of host memory.",
    "process_killed": "ComfyUI was killed (SIGKILL); host memory exhaustion is likely but unconfirmed.",
    "native_crash": "ComfyUI crashed in native code.",
    "process_exit": "ComfyUI exited unexpectedly.",
    "execution_error": "A node raised an error during execution.",
    "timeout": "The job exceeded its runtime limit.",
    "no_output": "The workflow completed but produced no output files.",
    "cancelled": "The job was cancelled.",
    "runner_error": "The runner hit an internal error.",
}

MEMORY_CODES = frozenset({"vram_oom", "vae_decode_oom", "host_ram_oom", "host_ram_exhausted", "process_killed"})
MODEL_EXTS = (".safetensors", ".sft", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".onnx")
_MODEL_INPUTS = ("ckpt_name", "unet_name", "vae_name", "lora_name", "clip_name", "model_name",
                 "control_net_name", "upscale_model_name", "style_model_name", "gligen_name", "clip_name1",
                 "clip_name2", "clip_name3", "clip_name4")
_GPU_OOM = ("outofmemoryerror", "cuda out of memory", "allocation on device", "hip out of memory",
            "cudnn_status_alloc_failed", "cublas_status_alloc_failed")
_HOST_OOM = ("memoryerror", "defaultcpuallocator: can't allocate memory", "not enough memory",
             "cannot allocate memory")
_NATIVE_TAIL = ("segmentation fault", "core dumped", "illegal memory access", "fatal python error",
                "illegal instruction", "bus error", "cuda error: an illegal")
_CRASH_SIGNALS = {"SIGSEGV", "SIGBUS", "SIGILL", "SIGABRT", "SIGFPE"}
_DETAIL_MAX = 8000


def message_for(code: str) -> str:
    return _MESSAGES.get(code, "The job failed.")


def is_memory_related(code: str) -> bool:
    return code in MEMORY_CODES


def _result(code, **extra):
    out = {"code": code, "message": message_for(code)}
    for key, value in extra.items():
        if value is not None:
            out[key] = value
    if "detail" in out and isinstance(out["detail"], str) and len(out["detail"]) > _DETAIL_MAX:
        out["detail"] = out["detail"][:_DETAIL_MAX] + "…[truncated]"
    return out


def _is_model_value(error: dict) -> bool:
    extra = error.get("extra_info") or {}
    value = extra.get("received_value")
    values = value if isinstance(value, list) else [value]
    if any(isinstance(v, str) and v.lower().endswith(MODEL_EXTS) for v in values):
        return True
    name = str(extra.get("input_name") or "")
    if not name:
        details = str(error.get("details") or "")
        name = details.split(":", 1)[0].strip()
        if any(ext in details.lower() for ext in MODEL_EXTS):
            return True
    return name in _MODEL_INPUTS


def _node_errors_code(node_errors: dict):
    """(code, node_id, class_type, detail) for the most specific error among node_errors."""
    best = None
    rank = {"missing_model": 0, "invalid_value": 1, "validation_failed": 2}
    lines = []
    for node_id, info in (node_errors or {}).items():
        info = info if isinstance(info, dict) else {}
        for err in info.get("errors") or []:
            if not isinstance(err, dict):
                continue
            etype = err.get("type", "")
            lines.append(f"node #{node_id} ({info.get('class_type', '?')}): {err.get('message', '')}: "
                         f"{err.get('details', '')}")
            if etype == "value_not_in_list" and _is_model_value(err):
                code = "missing_model"
            elif etype.startswith("value_") or etype in (
                    "invalid_input_type", "required_input_missing", "custom_validation_failed",
                    "bad_linked_input", "return_type_mismatch", "exception_during_inner_validation"):
                code = "invalid_value"
            else:
                code = "validation_failed"
            if best is None or rank[code] < rank[best[0]]:
                best = (code, str(node_id), info.get("class_type"))
    if best is None:
        return None
    return best + ("\n".join(lines),)


def classify_node_errors(node_errors: dict) -> dict:
    found = _node_errors_code(node_errors)
    if found is None:
        return _result("validation_failed", detail=json.dumps(node_errors)[:_DETAIL_MAX])
    code, node, class_type, detail = found
    return _result(code, node=node, class_type=class_type, detail=detail)


def classify_rejection(status, body) -> dict:
    text = body if isinstance(body, str) else (body.decode("utf-8", "replace") if body else "")
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return _result("validation_failed", detail=f"HTTP {status}: {text}")
    if not isinstance(data, dict):
        return _result("validation_failed", detail=f"HTTP {status}: {text}")
    error = data.get("error") if isinstance(data.get("error"), dict) else {}
    if error.get("type") == "missing_node_type":
        extra = error.get("extra_info") or {}
        return _result("missing_node", node=extra.get("node_id"), class_type=extra.get("class_type"),
                       detail=f"{error.get('message', '')} {error.get('details', '')}".strip())
    found = _node_errors_code(data.get("node_errors") or {})
    if found is not None:
        code, node, class_type, detail = found
        return _result(code, node=node, class_type=class_type, detail=detail)
    return _result("validation_failed",
                   detail=f"{error.get('type', '')}: {error.get('message', '')} {error.get('details', '')}".strip()
                   or text)


def _is_decode(class_type) -> bool:
    return isinstance(class_type, str) and class_type.startswith("VAEDecode")


def classify_execution_error(data: dict) -> dict:
    data = data if isinstance(data, dict) else {}
    exc_type = str(data.get("exception_type") or "")
    message = str(data.get("exception_message") or "")
    node_type = data.get("node_type")
    node = data.get("node_id")
    haystack = f"{exc_type} {message}".lower()
    tb = data.get("traceback")
    detail = f"{exc_type}: {message}"
    if isinstance(tb, list) and tb:
        detail += "\n" + "".join(str(x) for x in tb[-6:])
    extra = dict(node=None if node is None else str(node), class_type=node_type,
                 exception_type=exc_type or None, detail=detail)
    if any(p in haystack for p in _GPU_OOM):
        return _result("vae_decode_oom" if _is_decode(node_type) else "vram_oom", **extra)
    if any(p in haystack for p in _HOST_OOM):
        return _result("host_ram_oom", **extra)
    return _result("execution_error", **extra)


def classify_interrupted(data: dict) -> dict:
    data = data if isinstance(data, dict) else {}
    node = data.get("node_id")
    return _result("cancelled", node=None if node is None else str(node), class_type=data.get("node_type"))


def _signal_name(num: int):
    try:
        return _signal.Signals(num).name
    except (ValueError, AttributeError):
        return {9: "SIGKILL", 11: "SIGSEGV", 6: "SIGABRT", 7: "SIGBUS", 4: "SIGILL", 8: "SIGFPE",
                15: "SIGTERM"}.get(num)


def _decode_returncode(rc):
    if rc is None:
        return None
    if rc < 0:
        return _signal_name(-rc)
    if 128 < rc < 160:
        return _signal_name(rc - 128)
    return None


def classify_exit(returncode, *, oom_kill_delta=None, tail=(), during="run") -> dict:
    sig = _decode_returncode(returncode)
    tail_text = "\n".join(str(x) for x in tail)[-_DETAIL_MAX:]
    lowered = tail_text.lower()
    if sig == "SIGKILL" or (oom_kill_delta or 0) > 0:
        code = "host_ram_exhausted" if (oom_kill_delta or 0) > 0 else "process_killed"
    elif sig in _CRASH_SIGNALS or any(p in lowered for p in _NATIVE_TAIL):
        code = "native_crash"
    else:
        code = "process_exit"
    extra = dict(returncode=returncode, signal=sig, detail=tail_text or None)
    if during == "startup":
        return _result("startup_failed", cause=code, **extra)
    return _result(code, **extra)

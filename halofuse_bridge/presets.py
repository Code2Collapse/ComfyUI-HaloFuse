"""D3 launch presets for the cloud runner: Safe / Fast / Max Memory / Debug, plus early validation of extra ComfyUI
flags and environment variables.

The runtime image's `runner/launch_allowlist.json` is authoritative: the runner re-validates everything and refuses
to start ComfyUI (exit 2, `bad_launch_args`) on anything not allowed. This module mirrors that file (ALLOWLIST, kept
identical by tests/test_presets.py against a copy of the runtime file) so HaloFuse settings can reject a bad value
when it is entered instead of after a paid job starts. Nothing here builds a shell command: values travel as JSON in
the job environment and the runner passes them to ComfyUI as an argv list.
"""
from __future__ import annotations

import json
import re

# Mirror of the runtime runner/launch_allowlist.json (minus its comment). Update both together (contract C-3 s7).
_ALLOWLIST_JSON = r'''
{
 "schema": 1,
 "verified_against": [
  "ComfyUI 0.30.1 comfy/cli_args.py (image pin)",
  "ComfyUI 0.39.0 comfy/cli_args.py"
 ],
 "flags": {
  "--use-sage-attention": {
   "value": "none",
   "requires_module": "sageattention"
  },
  "--use-flash-attention": {
   "value": "none",
   "requires_module": "flash_attn"
  },
  "--use-pytorch-cross-attention": {
   "value": "none"
  },
  "--use-split-cross-attention": {
   "value": "none"
  },
  "--use-quad-cross-attention": {
   "value": "none"
  },
  "--disable-xformers": {
   "value": "none"
  },
  "--reserve-vram": {
   "value": "float",
   "min": 0,
   "max": 64
  },
  "--vram-headroom": {
   "value": "float",
   "min": 0,
   "max": 64
  },
  "--disable-pinned-memory": {
   "value": "none"
  },
  "--disable-async-offload": {
   "value": "none"
  },
  "--async-offload": {
   "value": "int_optional",
   "min": 1,
   "max": 8
  },
  "--disable-dynamic-vram": {
   "value": "none"
  },
  "--disable-smart-memory": {
   "value": "none"
  },
  "--disable-mmap": {
   "value": "none"
  },
  "--fast-disk": {
   "value": "none"
  },
  "--fast": {
   "value": "choices_multi",
   "choices": [
    "fp16_accumulation",
    "fp8_matrix_mult",
    "cublas_ops",
    "autotune"
   ]
  },
  "--cache-none": {
   "value": "none"
  },
  "--cache-classic": {
   "value": "none"
  },
  "--cache-lru": {
   "value": "int",
   "min": 0,
   "max": 10000
  },
  "--cache-ram": {
   "value": "floats",
   "min": 0,
   "max": 1024,
   "max_count": 2
  },
  "--preview-method": {
   "value": "choice",
   "choices": [
    "none",
    "auto",
    "latent2rgb",
    "taesd"
   ]
  },
  "--preview-size": {
   "value": "int",
   "min": 64,
   "max": 2048
  },
  "--lowvram": {
   "value": "none"
  },
  "--novram": {
   "value": "none"
  },
  "--highvram": {
   "value": "none"
  },
  "--gpu-only": {
   "value": "none"
  },
  "--verbose": {
   "value": "choice_optional",
   "choices": [
    "DEBUG",
    "INFO",
    "WARNING",
    "ERROR",
    "CRITICAL"
   ]
  },
  "--log-stdout": {
   "value": "none"
  },
  "--deterministic": {
   "value": "none"
  },
  "--fp32-vae": {
   "value": "none"
  },
  "--bf16-vae": {
   "value": "none"
  },
  "--fp16-vae": {
   "value": "none"
  },
  "--cpu-vae": {
   "value": "none"
  },
  "--force-fp16": {
   "value": "none"
  },
  "--force-channels-last": {
   "value": "none"
  },
  "--disable-cuda-malloc": {
   "value": "none"
  }
 },
 "denied": {
  "--listen": "set by the runner (loopback only)",
  "--port": "set by the runner",
  "--output-directory": "set by the runner",
  "--input-directory": "set by the runner",
  "--temp-directory": "set by the runner",
  "--extra-model-paths-config": "set by the runner",
  "--base-directory": "changes every directory the runner relies on",
  "--user-directory": "changes directories the runner relies on",
  "--models-directory": "model locations come from MODEL_BASE_DIR",
  "--enable-cors-header": "opens the ComfyUI API to other origins",
  "--tls-keyfile": "not used inside the container",
  "--tls-certfile": "not used inside the container",
  "--front-end-version": "downloads front-end code at start-up",
  "--front-end-root": "not used inside the container",
  "--enable-manager": "ComfyUI-Manager can install arbitrary code",
  "--database-url": "not configurable per job",
  "--cpu": "a GPU job must not silently run on the CPU",
  "--directml": "not supported in the runtime image",
  "--multi-user": "not used inside the container",
  "--auto-launch": "no browser inside the container",
  "--whitelist-custom-nodes": "the node set is fixed by the runtime profile",
  "--disable-all-custom-nodes": "the node set is fixed by the runtime profile"
 },
 "exclusive_groups": [
  [
   "--gpu-only",
   "--highvram",
   "--lowvram",
   "--novram"
  ],
  [
   "--use-split-cross-attention",
   "--use-quad-cross-attention",
   "--use-pytorch-cross-attention",
   "--use-sage-attention",
   "--use-flash-attention"
  ],
  [
   "--cache-classic",
   "--cache-lru",
   "--cache-none",
   "--cache-ram"
  ],
  [
   "--fp16-vae",
   "--fp32-vae",
   "--bf16-vae"
  ],
  [
   "--async-offload",
   "--disable-async-offload"
  ]
 ],
 "env": {
  "PYTORCH_CUDA_ALLOC_CONF": {
   "pattern": "^[a-z_]+:[A-Za-z0-9.]+(,[a-z_]+:[A-Za-z0-9.]+)*$"
  },
  "CUDA_LAUNCH_BLOCKING": {
   "choices": [
    "0",
    "1"
   ]
  },
  "CUDA_MODULE_LOADING": {
   "choices": [
    "LAZY",
    "EAGER"
   ]
  }
 },
 "presets": {
  "safe": {
   "label": "Safe",
   "description": "Stock ComfyUI settings.",
   "args": [],
   "env": {},
   "ladder": null
  },
  "fast": {
   "label": "Fast",
   "description": "ComfyUI --fast optimizations (ComfyUI calls them untested and potentially quality deteriorating).",
   "args": [
    "--fast"
   ],
   "env": {},
   "ladder": null
  },
  "max_memory": {
   "label": "Max Memory",
   "description": "Aggressive offloading, no pinned host memory, 1 GB VRAM reserve, expandable CUDA segments; tiled decode allowed as a recovery step.",
   "args": [
    "--disable-smart-memory",
    "--disable-pinned-memory",
    "--reserve-vram",
    "1.0"
   ],
   "env": {
    "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True"
   },
   "ladder": [
    "resubmit_cached",
    "decode_tiled",
    "decode_from_latent"
   ]
  },
  "debug": {
   "label": "Debug",
   "description": "Verbose ComfyUI logging and synchronous CUDA errors (slower).",
   "args": [
    "--verbose",
    "DEBUG"
   ],
   "env": {
    "CUDA_LAUNCH_BLOCKING": "1"
   },
   "ladder": null
  }
 },
 "default_ladder": [
  "resubmit_cached",
  "decode_from_latent"
 ],
 "memory_safe_restart_args": [
  "--disable-pinned-memory",
  "--cache-none"
 ]
}
'''
ALLOWLIST: dict = json.loads(_ALLOWLIST_JSON)
PRESETS: dict = ALLOWLIST["presets"]
PRESET_NAMES: tuple = tuple(PRESETS)
LADDER_STEPS = ("resubmit_cached", "decode_tiled", "decode_from_latent")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:=,+/-]+$")
_NUMBER_RE = re.compile(r"^-?\d+(\.\d+)?$")
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


def _number_problem(kind: str, raw: str, spec: dict):
    if not _NUMBER_RE.match(raw):
        return f"expects a number, got {raw!r}"
    value = float(raw)
    if kind.startswith("int") and not value.is_integer():
        return f"expects a whole number, got {raw!r}"
    if "min" in spec and value < spec["min"]:
        return f"must be at least {spec['min']}"
    if "max" in spec and value > spec["max"]:
        return f"must be at most {spec['max']}"
    return None


def _check_values(flag: str, spec: dict, values: list):
    kind, choices = spec.get("value", "none"), spec.get("choices", [])
    if kind == "none":
        return "takes no value" if values else None
    if kind in ("int", "float"):
        return "expects exactly one value" if len(values) != 1 else _number_problem(kind, values[0], spec)
    if kind == "int_optional":
        if len(values) > 1:
            return "expects at most one value"
        return _number_problem("int", values[0], spec) if values else None
    if kind == "floats":
        if len(values) > spec.get("max_count", 1):
            return f"expects at most {spec.get('max_count', 1)} values"
        for v in values:
            problem = _number_problem("float", v, spec)
            if problem:
                return problem
        return None
    if kind == "choice":
        return None if len(values) == 1 and values[0] in choices else f"expects one of {', '.join(choices)}"
    if kind == "choice_optional":
        ok = not values or (len(values) == 1 and values[0] in choices)
        return None if ok else f"expects nothing or one of {', '.join(choices)}"
    if kind == "choices_multi":
        bad = [v for v in values if v not in choices]
        return f"unknown option(s) {', '.join(bad)}" if bad else None
    return f"has an unknown value kind {kind!r}"


def validate_extra_args(args) -> bool:
    """True, or ValueError naming the first problem. Same rules as the runner, except that only the runner can check
    that optional modules (sageattention, flash_attn) are installed in the image."""
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise ValueError("extra ComfyUI flags must be a list of strings")
    tokens = []
    for tok in args:
        if not _TOKEN_RE.match(tok):
            raise ValueError(f"{tok[:40]!r} contains characters that are not allowed")
        if tok.startswith("--") and "=" in tok:
            tokens += tok.split("=", 1)
        else:
            tokens.append(tok)
    flags, denied = ALLOWLIST["flags"], ALLOWLIST["denied"]
    seen, i = [], 0
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        if not tok.startswith("--"):
            raise ValueError(f"unexpected value {tok!r} (no flag before it)")
        if tok in denied:
            raise ValueError(f"{tok} is not allowed: {denied[tok]}")
        spec = flags.get(tok)
        if spec is None:
            raise ValueError(f"{tok} is not an allowed ComfyUI launch flag")
        values = []
        while i < len(tokens) and not tokens[i].startswith("--"):
            values.append(tokens[i])
            i += 1
        problem = _check_values(tok, spec, values)
        if problem:
            raise ValueError(f"{tok} {problem}")
        seen.append(tok)
    for group in ALLOWLIST["exclusive_groups"]:
        present = sorted({f for f in group if f in seen})
        if len(present) > 1:
            raise ValueError(f"{' and '.join(present)} cannot be combined")
    return True


def validate_extra_env(env) -> bool:
    if not isinstance(env, dict):
        raise ValueError("extra environment must be an object of NAME: value")
    table = ALLOWLIST["env"]
    for name, value in env.items():
        spec = table.get(name)
        if spec is None:
            raise ValueError(f"environment variable {name!r} is not allowed")
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a string")
        if "choices" in spec and value not in spec["choices"]:
            raise ValueError(f"{name} must be one of {', '.join(spec['choices'])}")
        if "pattern" in spec and not re.fullmatch(spec["pattern"], value):
            raise ValueError(f"{name} has a value that is not allowed")
    return True


def job_env(name: str = "safe", extra_args=None, extra_env=None, preview: bool = False, nonce: str | None = None,
            oom_ladder: str | None = None) -> dict:
    """The runner environment for one cloud job (C-3 section 7). Raises ValueError for anything the runner would
    reject, so a misconfigured job is never submitted (and never billed)."""
    if name not in PRESETS:
        raise ValueError(f"unknown launch preset {name!r}")
    env = {"HF_PRESET": name}
    if extra_args:
        validate_extra_args(list(extra_args))
        env["COMFY_EXTRA_ARGS"] = json.dumps(list(extra_args))
    if extra_env:
        validate_extra_env(dict(extra_env))
        env["EXTRA_ENV"] = json.dumps(dict(extra_env))
    if preview:
        env["HF_PREVIEW"] = "live"
    if nonce is not None:
        if not _NONCE_RE.match(nonce):
            raise ValueError("event nonce must be 16-64 characters of A-Z a-z 0-9 _ -")
        env["HF_EVENT_NONCE"] = nonce
    if oom_ladder:
        ladder = oom_ladder.strip()
        steps = [s.strip() for s in ladder.split(",") if s.strip()]
        if ladder != "off" and any(s not in LADDER_STEPS for s in steps):
            raise ValueError(f"OOM ladder steps must be from {', '.join(LADDER_STEPS)} (or off)")
        env["HF_OOM_LADDER"] = ladder
    return env

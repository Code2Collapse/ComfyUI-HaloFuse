"""D3: controlled ComfyUI launch flags and environment passthrough.

Inputs come from the job env (set by the bridge): HF_PRESET, COMFY_EXTRA_ARGS, EXTRA_ENV, HF_PREVIEW,
HF_OOM_LADDER, HF_RESTART_POLICY. Everything is validated against runner/launch_allowlist.json before ComfyUI
starts; the result is an argv list (never a shell string) and a small env dict. Anything not allowlisted is
rejected with a reason, never silently dropped.

Run as a script to check the allowlist against a ComfyUI checkout (WS7 build step):
    python3.13 runner/hf/launch.py runner/launch_allowlist.json $COMFYUI_DIR/comfy/cli_args.py
"""
import ast
import importlib.util
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

LADDER_STEPS = ("resubmit_cached", "decode_tiled", "decode_from_latent")
RESTART_POLICIES = {"off": 0, "once": 1, "twice": 2}
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:=,+/-]+$")
_NUMBER_RE = re.compile(r"^-?\d+(\.\d+)?$")


@dataclass
class LaunchConfig:
    args: list = field(default_factory=list)
    env: dict = field(default_factory=dict)
    preset: str = "safe"
    ladder: list = field(default_factory=list)
    max_restarts: int = 1
    errors: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def load_allowlist(path) -> dict:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or not isinstance(data.get("flags"), dict):
        raise ValueError(f"{path}: not a launch allowlist")
    return data


def _default_module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def parse_extra_args(raw) -> list:
    """COMFY_EXTRA_ARGS -> tokens. JSON array of strings, or a plain whitespace-separated string."""
    if raw is None or not str(raw).strip():
        return []
    text = str(raw).strip()
    if text.startswith("[") or text.startswith("{") or text.startswith('"'):
        try:
            value = json.loads(text)
        except ValueError as err:
            raise ValueError(f"COMFY_EXTRA_ARGS is not valid JSON ({err.msg})") from None
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError("COMFY_EXTRA_ARGS must be a JSON array of strings")
        tokens = value
    else:
        if any(ch in text for ch in "\r\n\t\x00"):
            raise ValueError("COMFY_EXTRA_ARGS contains control characters")
        tokens = text.split(" ")
        tokens = [t for t in tokens if t != ""]
    out = []
    for tok in tokens:
        if not _TOKEN_RE.match(tok):
            raise ValueError(f"COMFY_EXTRA_ARGS token {tok[:40]!r} contains characters that are not allowed")
        if tok.startswith("--") and "=" in tok:
            flag, value = tok.split("=", 1)
            out += [flag, value]
        else:
            out.append(tok)
    return out


def _check_number(kind, raw, spec):
    if not _NUMBER_RE.match(raw):
        return f"expects a number, got {raw!r}"
    try:
        value = int(raw) if kind.startswith("int") else float(raw)
    except ValueError:
        return f"expects an {'integer' if kind.startswith('int') else 'number'}, got {raw!r}"
    if "min" in spec and value < spec["min"]:
        return f"must be >= {spec['min']}"
    if "max" in spec and value > spec["max"]:
        return f"must be <= {spec['max']}"
    return None


def validate_args(tokens, allowlist, module_available=None):
    """Return (normalised args, errors). Values are consumed per the flag's value kind."""
    module_available = module_available or _default_module_available
    flags = allowlist.get("flags", {})
    denied = allowlist.get("denied", {})
    out, errors, seen = [], [], []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        if not tok.startswith("--"):
            errors.append(f"unexpected value {tok!r} (no flag before it)")
            continue
        if tok in denied:
            errors.append(f"{tok} is not allowed: {denied[tok]}")
            continue
        spec = flags.get(tok)
        if spec is None:
            errors.append(f"{tok} is not an allowed ComfyUI launch flag")
            continue
        kind = spec.get("value", "none")
        values = []
        while i < len(tokens) and not tokens[i].startswith("--"):
            values.append(tokens[i])
            i += 1
        problem = None
        if kind == "none":
            if values:
                problem = "takes no value"
        elif kind in ("int", "float"):
            if len(values) != 1:
                problem = "expects exactly one value"
            else:
                problem = _check_number(kind, values[0], spec)
        elif kind == "int_optional":
            if len(values) > 1:
                problem = "expects at most one value"
            elif values:
                problem = _check_number("int", values[0], spec)
        elif kind == "floats":
            if len(values) > spec.get("max_count", 1):
                problem = f"expects at most {spec.get('max_count', 1)} values"
            for v in values:
                problem = problem or _check_number("float", v, spec)
        elif kind == "choice":
            if len(values) != 1 or values[0] not in spec.get("choices", []):
                problem = f"expects one of {', '.join(spec.get('choices', []))}"
        elif kind == "choice_optional":
            if len(values) > 1 or (values and values[0] not in spec.get("choices", [])):
                problem = f"expects nothing or one of {', '.join(spec.get('choices', []))}"
        elif kind == "choices_multi":
            bad = [v for v in values if v not in spec.get("choices", [])]
            if bad:
                problem = f"unknown option(s) {', '.join(bad)}; allowed: {', '.join(spec.get('choices', []))}"
        else:
            problem = f"has an unknown value kind {kind!r} in the allowlist"
        module = spec.get("requires_module")
        if problem is None and module and not module_available(module):
            problem = f"needs the Python module {module!r}, which is not installed in this runtime image"
        if problem:
            errors.append(f"{tok} {problem}")
            continue
        out.append(tok)
        out.extend(values)
        seen.append(tok)
    for group in allowlist.get("exclusive_groups", []):
        present = [f for f in group if f in seen]
        if len(set(present)) > 1:
            errors.append(f"{' and '.join(sorted(set(present)))} cannot be combined")
    return out, errors


def validate_env(raw, allowlist):
    if raw is None or not str(raw).strip():
        return {}, []
    try:
        value = json.loads(raw)
    except ValueError as err:
        return {}, [f"EXTRA_ENV is not valid JSON ({err.msg})"]
    if not isinstance(value, dict):
        return {}, ["EXTRA_ENV must be a JSON object of NAME: value"]
    table = allowlist.get("env", {})
    env, errors = {}, []
    for name, val in value.items():
        spec = table.get(name)
        if spec is None:
            errors.append(f"environment variable {name!r} is not allowed")
            continue
        if not isinstance(val, str):
            errors.append(f"{name} must be a string")
            continue
        if "choices" in spec and val not in spec["choices"]:
            errors.append(f"{name} must be one of {', '.join(spec['choices'])}")
            continue
        if "pattern" in spec and not re.fullmatch(spec["pattern"], val):
            errors.append(f"{name} has a value that is not allowed")
            continue
        env[name] = val
    return env, errors


def _flags_of(args):
    return [a for a in args if a.startswith("--")]


def resolve(env, allowlist, module_available=None) -> LaunchConfig:
    cfg = LaunchConfig()
    presets = allowlist.get("presets", {})
    name = (env.get("HF_PRESET") or "safe").strip()
    preset = presets.get(name)
    if preset is None:
        cfg.errors.append(f"unknown launch preset {name!r}; choose one of {', '.join(sorted(presets))}")
        preset = {"args": [], "env": {}, "ladder": None}
    cfg.preset = name

    try:
        user_tokens = parse_extra_args(env.get("COMFY_EXTRA_ARGS"))
    except ValueError as err:
        cfg.errors.append(str(err))
        user_tokens = []
    args, errors = validate_args(list(preset.get("args", [])) + user_tokens, allowlist, module_available)
    cfg.errors += errors
    if (env.get("HF_PREVIEW") or "off").strip().lower() == "live" and "--preview-method" not in args:
        args += ["--preview-method", "auto"]
    cfg.args = args

    extra_env, errors = validate_env(env.get("EXTRA_ENV"), allowlist)
    cfg.errors += errors
    merged = dict(preset.get("env") or {})
    merged.update(extra_env)
    cfg.env = merged

    ladder_raw = env.get("HF_OOM_LADDER")
    if ladder_raw is not None and ladder_raw.strip():
        if ladder_raw.strip().lower() == "off":
            cfg.ladder = []
        else:
            steps = [s.strip() for s in ladder_raw.split(",") if s.strip()]
            bad = [s for s in steps if s not in LADDER_STEPS]
            if bad:
                cfg.errors.append(f"HF_OOM_LADDER has unknown step(s) {', '.join(bad)}; allowed: "
                                  f"{', '.join(LADDER_STEPS)}")
            cfg.ladder = [s for s in steps if s in LADDER_STEPS]
    elif preset.get("ladder") is not None:
        cfg.ladder = list(preset["ladder"])
    else:
        cfg.ladder = list(allowlist.get("default_ladder", ["resubmit_cached", "decode_from_latent"]))

    policy = (env.get("HF_RESTART_POLICY") or "once").strip().lower()
    if policy not in RESTART_POLICIES:
        cfg.errors.append(f"HF_RESTART_POLICY must be one of {', '.join(RESTART_POLICIES)}")
        policy = "once"
    cfg.max_restarts = RESTART_POLICIES[policy]
    return cfg


def memory_safe_restart_args(allowlist) -> list:
    return list(allowlist.get("memory_safe_restart_args", []))


def merge_args(base, added, allowlist) -> list:
    """`base` + `added`, dropping base flags (and their values) that are in an exclusive group with an added flag
    or that are re-specified by `added`."""
    added_flags = set(_flags_of(added))
    drop = set(added_flags)
    for group in allowlist.get("exclusive_groups", []):
        if added_flags & set(group):
            drop |= set(group)
    out, skipping = [], False
    for tok in base:
        if tok.startswith("--"):
            skipping = tok in drop
        if not skipping:
            out.append(tok)
    return out + list(added)


def cache_disabled(args) -> bool:
    return "--cache-none" in args


def attention_backend(args) -> str:
    table = {"--use-sage-attention": "sage", "--use-flash-attention": "flash",
             "--use-pytorch-cross-attention": "pytorch", "--use-split-cross-attention": "split",
             "--use-quad-cross-attention": "quad"}
    for flag, name in table.items():
        if flag in args:
            return name
    return "default"


def flags_in_cli_args(source_text: str) -> set:
    """Every `--flag` declared by an `*.add_argument(...)` call in a ComfyUI cli_args.py (parsed, not executed)."""
    found = set()
    for node in ast.walk(ast.parse(source_text)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value.startswith("--"):
                    found.add(arg.value)
    return found


def check_allowlist_against(allowlist_path, cli_args_path) -> list:
    allowlist = load_allowlist(allowlist_path)
    declared = flags_in_cli_args(Path(cli_args_path).read_text(encoding="utf-8"))
    wanted = set(allowlist.get("flags", {}))
    for preset in allowlist.get("presets", {}).values():
        wanted |= set(_flags_of(preset.get("args", [])))
    wanted |= set(_flags_of(allowlist.get("memory_safe_restart_args", [])))
    return sorted(wanted - declared)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: launch.py <launch_allowlist.json> <comfy/cli_args.py>", file=sys.stderr)
        sys.exit(2)
    missing = check_allowlist_against(sys.argv[1], sys.argv[2])
    if missing:
        print("allowlisted flags missing from this ComfyUI: " + ", ".join(missing))
        sys.exit(1)
    print("launch allowlist OK")

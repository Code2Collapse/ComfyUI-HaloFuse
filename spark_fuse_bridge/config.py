"""Bridge preferences (non-secret) and the legacy client shim.

Settings live outside the installed node folder, under ComfyUI's system-user directory (see settings_dir), so they
survive a Manager uninstall or a delete-and-reinstall, and ComfyUI never serves that folder over HTTP.

Since HaloFuse 0.4 the settings file holds preferences only:
- the cloud endpoint is deployment configuration, resolved by the trusted layer (`provider.resolve_endpoint`), and
  can no longer be changed through the settings routes;
- the account email and password live in `credentials.py` (system keychain, or an explicitly chosen and honestly
  labelled unencrypted fallback). A plaintext password left by an older version is migrated there, never printed.
"""
from __future__ import annotations

import contextlib
import copy
import json
import math
import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

_log = logging.getLogger("spark_fuse_bridge.config")

_ROOT = Path(__file__).resolve().parent.parent
_LEGACY_SETTINGS_PATH = _ROOT / "spark_fuse_settings.json"
SETTINGS_NAME = "spark_fuse_settings.json"

# Cache so settings_dir() resolves (and migrates the pre-0.2.2 copy) only once per process.
_settings_dir_cache: Path | None = None
_override_dir: Path | None = None
_settings_ok = True

# image and command are node-version constants (always taken from here, never from a stale saved settings file).
# Cached-image affinity still resolves the image to a digest at submit time.
DEFAULTS = {
    "image": "ghcr.io/nithin-ml/spark-fuse-comfyui:sha-70e0ff4b38b431b2c77e90f2936f93d04516ccc9",
    "command": ["python3.13", "/runner/spark_fuse_run.py"],
    "instance_type": "g7e.2xlarge",
    "assets_share_sync_path": "/models",
    "assets_share_sync_space_name": "",
    "model_base_dir": "/assets",
    "image_affinity": "required",
    "batch_count": 1,
    # Pre-render model sync: largest single model the bridge will upload in-node, in GB. A practicality guard
    # against huge non-resumable transfers, not a server limit. 0 disables the guard.
    "upload_guard_gb": 50,
    # Job lifecycle and spend limits (WS2, REQUESTS R-WS2-01). Money values are estimates; None = no cap.
    "max_runtime_seconds": 21600,
    "cancel_confirm_timeout_seconds": 90,
    "max_job_cost_usd": None,
    "daily_spend_cap_usd": None,
    "idle_session_warn_seconds": 120,
    # Runner launch presets and allowlisted passthrough (WS4, REQUESTS R-WS4-5; TM-013).
    "launch_preset": "safe",
    "comfy_extra_args": [],
    "extra_env": {},
    "oom_ladder": "",
    # Panel preview mode (WS6, REQUESTS R-WS6-01).
    "preview_mode": "final",
}

# The only keys the browser may read or write. Never the endpoint, a credential or the runtime image.
PUBLIC_KEYS = [
    "instance_type", "assets_share_sync_path", "assets_share_sync_space_name",
    "model_base_dir", "image_affinity", "batch_count", "upload_guard_gb",
    "max_runtime_seconds", "max_job_cost_usd", "daily_spend_cap_usd",
    "launch_preset", "comfy_extra_args", "extra_env", "oom_ladder", "preview_mode",
]

# Public keys whose value may be explicitly cleared with null.
_NULLABLE_KEYS = ("max_job_cost_usd", "daily_spend_cap_usd")
_LAUNCH_PRESETS = ("safe", "fast", "max_memory", "debug")
_PREVIEW_MODES = ("none", "check", "draft", "live", "final")
_OOM_LADDER_RE = re.compile(r"^[a-z0-9_,]{0,64}$")

# Keys that are never returned by load_settings() and never written by save_settings().
_PRIVATE_KEYS = ("host", "endpoint", "email", "password")

_INSTANCE_TYPE_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# Queue chunking: the maximum number of workflows per manifest job (see render_queue.py and spark_fuse_run.py's
# multi-workflow mode). Deliberately NOT in PUBLIC_KEYS: it can only be changed by hand-editing the settings file.
# A user raising it risks losing a larger chunk's worth of completed work at once if its job is killed.
DEFAULT_QUEUE_CHUNK_SIZE = 10


def queue_chunk_size(settings: dict) -> int:
    try:
        return max(1, int(settings.get("queue_chunk_size") or DEFAULT_QUEUE_CHUNK_SIZE))
    except (TypeError, ValueError):
        return DEFAULT_QUEUE_CHUNK_SIZE


# Container-inactivity threshold, in seconds, sent on every job submission (single render and queue chunk alike).
# Fires when stdout/stderr, CPU and GPU are ALL quiet for the whole window: a wedged container. 1800 s matches the
# platform default. Deliberately NOT in PUBLIC_KEYS (same mechanism as DEFAULT_QUEUE_CHUNK_SIZE): 0 would disable
# the detector, the only automatic backstop while no maximum runtime is sent.
DEFAULT_CONTAINER_INACTIVITY_SECONDS = 1800


def container_inactivity_seconds(settings: dict) -> int:
    try:
        return int(settings.get("container_inactivity_seconds") or DEFAULT_CONTAINER_INACTIVITY_SECONDS)
    except (TypeError, ValueError):
        return DEFAULT_CONTAINER_INACTIVITY_SECONDS


# ---- location ---------------------------------------------------------------------------------------

def settings_dir() -> Path:
    """The bridge's private settings folder (created on first use)."""
    global _settings_dir_cache
    if _override_dir is not None:
        _override_dir.mkdir(parents=True, exist_ok=True)
        return _override_dir
    if _settings_dir_cache is not None:
        return _settings_dir_cache

    import folder_paths  # ComfyUI-provided; available at runtime

    if hasattr(folder_paths, "get_system_user_directory"):
        # System User directory: internal-only, never exposed through ComfyUI's HTTP /userdata endpoints.
        directory = Path(folder_paths.get_system_user_directory("spark_fuse_bridge"))
    else:
        # Older ComfyUI without the System User Protection API (Manager's legacy convention).
        directory = Path(folder_paths.get_user_directory()) / "default" / "spark_fuse_bridge"
    directory.mkdir(parents=True, exist_ok=True)
    _migrate_legacy_settings(directory / SETTINGS_NAME)
    _settings_dir_cache = directory
    return directory


def settings_path() -> Path:
    return settings_dir() / SETTINGS_NAME


# Backwards-compatible name used by older code.
_settings_path = settings_path


@contextlib.contextmanager
def override_settings_dir(path):
    """Point every settings/credential/deployment file at *path* (tests and tooling only)."""
    global _override_dir, _settings_ok
    previous, previous_ok = _override_dir, _settings_ok
    _override_dir = Path(path)
    _settings_ok = True
    _reset_credential_store()
    try:
        yield _override_dir
    finally:
        _override_dir, _settings_ok = previous, previous_ok
        _reset_credential_store()


def _reset_credential_store() -> None:
    from . import credentials

    credentials.set_default_store(None)


def legacy_settings_paths() -> list[Path]:
    """Every file that may hold a plaintext credential from an older version (CREDENTIALS_DESIGN s4 a-d)."""
    paths = [settings_path()]
    if _override_dir is None:
        try:
            import folder_paths

            if hasattr(folder_paths, "get_system_user_directory"):
                paths.append(Path(folder_paths.get_user_directory()) / "default" / "spark_fuse_bridge" / SETTINGS_NAME)
        except Exception:  # noqa: BLE001 - outside ComfyUI there is no old location
            pass
    paths.append(_LEGACY_SETTINGS_PATH)
    paths.append(_LEGACY_SETTINGS_PATH.with_name(_LEGACY_SETTINGS_PATH.name + ".migrated"))
    return paths


def _migrate_legacy_settings(new_path: Path) -> None:
    """Copy a pre-0.2.2 in-folder settings file into place, once (the original is renamed aside, never deleted).

    The credential migration (credentials.py) later scrubs the password from every copy.
    """
    if new_path.is_file() or not _LEGACY_SETTINGS_PATH.is_file():
        return
    try:
        shutil.copyfile(_LEGACY_SETTINGS_PATH, new_path)
        backup_path = _LEGACY_SETTINGS_PATH.with_name(_LEGACY_SETTINGS_PATH.name + ".migrated")
        _LEGACY_SETTINGS_PATH.rename(backup_path)
    except OSError:
        pass


# ---- read / write -------------------------------------------------------------------------------------

def _read_raw() -> tuple[dict, bool]:
    """(file dict, ok). A missing file is ok; unreadable or non-object JSON is not."""
    path = settings_path()
    if not path.is_file():
        return {}, True
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}, False
    if not isinstance(data, dict):
        return {}, False
    return data, True


def settings_file_ok() -> bool:
    """False after load_settings() found a corrupt settings file (reported, not silently replaced)."""
    return _settings_ok


def load_settings() -> dict:
    global _settings_ok
    raw, ok = _read_raw()
    if not ok and _settings_ok:
        _log.warning("the HaloFuse settings file is unreadable; defaults are used until it is saved again")
    _settings_ok = ok
    data = copy.deepcopy(DEFAULTS)
    data.update(raw)
    for key in _PRIVATE_KEYS:
        data.pop(key, None)
    # image/command are node-version constants; never honour a stale saved value.
    data["image"] = DEFAULTS["image"]
    data["command"] = DEFAULTS["command"]
    return data


def _valid_pref(key: str, value) -> bool:
    if key == "instance_type":
        return isinstance(value, str) and bool(_INSTANCE_TYPE_RE.match(value))
    if key in ("assets_share_sync_path", "assets_share_sync_space_name", "model_base_dir"):
        return isinstance(value, str) and len(value) <= 512 and "\x00" not in value
    if key == "image_affinity":
        return value in ("required", "preferred")
    if key == "batch_count":
        return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 1000
    if key == "upload_guard_gb":
        return _number(value) and value >= 0
    if key == "max_runtime_seconds":
        return isinstance(value, int) and not isinstance(value, bool) and 60 <= value <= 7 * 86400
    if key in _NULLABLE_KEYS:
        return value is None or (_number(value) and value >= 0)
    if key == "launch_preset":
        return value in _LAUNCH_PRESETS
    if key == "preview_mode":
        return value in _PREVIEW_MODES
    if key == "oom_ladder":
        return isinstance(value, str) and bool(_OOM_LADDER_RE.match(value))
    if key == "comfy_extra_args":
        if not (isinstance(value, list) and len(value) <= 64
                and all(isinstance(a, str) and 0 < len(a) <= 256 for a in value)):
            return False
        return not value or _runner_allowlist_accepts("validate_extra_args", value)
    if key == "extra_env":
        if not (isinstance(value, dict) and len(value) <= 64
                and all(isinstance(k, str) and isinstance(v, str) and len(k) <= 128 and len(v) <= 1024
                        for k, v in value.items())):
            return False
        return not value or _runner_allowlist_accepts("validate_extra_env", value)
    return False


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _runner_allowlist_accepts(validator: str, value) -> bool:
    """Free-form flags/env are only accepted when WS4's allowlist (presets.py) is installed and accepts them."""
    try:
        from . import presets  # owned by WS4; absent until it lands
    except ImportError:
        return False
    check = getattr(presets, validator, None)
    if check is None:
        return False
    try:
        result = check(value)
    except Exception:  # noqa: BLE001 - any rejection reason means "not allowed"
        return False
    return result is not False


def invalid_prefs(values: dict) -> list[str]:
    """Names of web-writable keys in *values* whose value would be rejected (unknown keys are ignored)."""
    if not isinstance(values, dict):
        return []
    return [k for k in PUBLIC_KEYS if k in values
            and not (values[k] is None and k not in _NULLABLE_KEYS)
            and not _valid_pref(k, values[k])]


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_settings(values: dict) -> dict:
    """Save the web-writable preferences. The endpoint and credentials are never taken from *values*."""
    global _settings_ok
    values = values if isinstance(values, dict) else {}
    path = settings_path()
    raw, ok = _read_raw()
    if not ok and path.exists():
        backup = path.with_name(f"{path.name}.corrupt-{time.strftime('%Y%m%d%H%M%S')}")
        os.replace(path, backup)
        _log.warning("kept the unreadable HaloFuse settings file as %s", backup.name)
        raw = {}
    if any(k in values for k in ("host", "endpoint")):
        _log.warning("ignored a request to change the service endpoint (it is deployment configuration)")
    for key in PUBLIC_KEYS:
        if key in values and (values[key] is not None or key in _NULLABLE_KEYS) and _valid_pref(key, values[key]):
            raw[key] = values[key]
    if raw.get("host") or raw.get("endpoint"):
        from . import credentials

        credentials.carry_over_endpoint(path.parent, raw.get("host") or raw.get("endpoint"))
    raw.pop("host", None)
    raw.pop("endpoint", None)
    _atomic_write(path, raw)
    _settings_ok = True
    return load_settings()


def public_settings() -> dict:
    """Non-secret preferences plus safe credential status flags. Never the endpoint, email or password."""
    s = load_settings()
    out = {k: s.get(k, DEFAULTS.get(k, "")) for k in PUBLIC_KEYS}
    try:
        from . import credentials

        status = credentials.default_store().status()
        out["password_set"] = bool(status.get("password_set"))
        out["credentials_configured"] = bool(status.get("credentials_configured"))
    except Exception:  # noqa: BLE001 - status is advisory; never break the settings route
        out["password_set"] = False
        out["credentials_configured"] = False
    return out


def make_client(settings: dict | None = None):
    """Deprecated legacy shim: the raw provider client behind a redacting proxy (contract C-1 section 6).

    *settings* is accepted for compatibility and ignored: the endpoint and credentials come from the trusted layer
    only. Raises a branded `provider.BackendError` (a RuntimeError) when HaloFuse is not configured.
    """
    from .provider import spark_adapter

    return spark_adapter.legacy_sdk_client()

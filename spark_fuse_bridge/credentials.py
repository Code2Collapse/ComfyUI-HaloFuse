"""Account credential storage for HaloFuse (design: _internal/CREDENTIALS_DESIGN.md).

Resolution order: deployment environment (both SPARK_EMAIL and SPARK_PASSWORD set) > system keychain (allowlisted
keyring backend, every write verified by reading it back) > the documented unencrypted fallback file (only after an
explicit opt-in) > a legacy plaintext settings file whose migration is still pending.

Nothing here prints or logs an email, password or endpoint. `keyring` is optional and imported lazily.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import brand

_log = logging.getLogger("spark_fuse_bridge.credentials")

SERVICE = "HaloFuse"
USERNAME = "account"
SETTINGS_NAME = "spark_fuse_settings.json"
FALLBACK_NAME = "credentials.json"
DEPLOYMENT_NAME = "deployment.json"
LOCK_NAME = ".credentials-migration.lock"
OPT_IN_KEY = "allow_plaintext_credentials"
MIGRATED_KEY = "credentials_migrated"

MAX_EMAIL = 254
MAX_PASSWORD = 512
MAX_BLOB = 1280  # measured WinVault limit (CREDENTIALS_DESIGN s2)

SECURE_BACKENDS = (
    "keyring.backends.Windows.WinVaultKeyring",
    "keyring.backends.macOS.Keyring",
    "keyring.backends.SecretService.Keyring",
    "keyring.backends.kwallet.DBusKeyring",
)
_LABELS = {
    "keyring.backends.Windows.WinVaultKeyring": "Windows Credential Manager",
    "keyring.backends.macOS.Keyring": "macOS Keychain",
    "keyring.backends.SecretService.Keyring": "Secret Service keyring",
    "keyring.backends.kwallet.DBusKeyring": "KWallet",
}
PLAINTEXT_LABEL = "Stored as plain text on this computer"

_AUTO = object()

# One-time endpoint carry-over (owner decision D-2) accepts ONLY the endpoint that 0.3.x shipped as its default,
# matched by SHA-256 of the normalised value so the hostname stays out of the source. Any other saved value may be
# the result of the X1 redirect attack (POST /settings {"host": ...}) and is never promoted to deployment config.
CARRY_OVER_SHA256 = frozenset({"9f0e80c4e1453eb74da8cd8facf9ad1dfc2b0ea1acce8c93b1346e6c750d9061"})


@dataclass(frozen=True)
class Credentials:
    email: str
    password: str

    def __repr__(self) -> str:
        return "Credentials(email=<account>, password=<secret>)"

    __str__ = __repr__


class CredentialError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        self.user_message = brand.message(code)
        self.status = brand.status_for(code)
        super().__init__(self.user_message)

    def __str__(self) -> str:
        return self.user_message


# ---- file helpers ---------------------------------------------------------------------------------

def _atomic_write_json(path: Path, data: dict, *, private: bool = False) -> None:
    """Write JSON atomically (temp file in the same folder, fsync, os.replace)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        if private and os.name == "posix":
            os.fchmod(fd, 0o600)
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


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _restrict_windows_acl(path: Path) -> None:
    """Best effort: only the current user may read the fallback file (owner decision D-7: tighten, warn on failure)."""
    user = os.environ.get("USERNAME")
    if os.name != "nt" or not user:
        return
    try:
        result = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
                                capture_output=True, timeout=10, check=False)
        if result.returncode != 0:
            _log.warning("could not restrict access to the unencrypted credential file")
    except (OSError, subprocess.SubprocessError):
        _log.warning("could not restrict access to the unencrypted credential file")


def carry_over_endpoint(directory: Path, host) -> None:
    """Copy an old user-saved endpoint once into the private deployment file (owner decision D-2). Never overwrites."""
    if not isinstance(host, str):
        return
    normalised = host.strip().rstrip("/").lower()
    if hashlib.sha256(normalised.encode("utf-8")).hexdigest() not in CARRY_OVER_SHA256:
        _log.warning("a saved custom service endpoint was not carried over; ask your administrator to configure "
                     "HaloFuse for this computer")
        return
    path = Path(directory) / DEPLOYMENT_NAME
    current = _read_json(path) if path.exists() else {}
    if current is None or current.get("endpoint"):
        return
    current["endpoint"] = normalised
    _atomic_write_json(path, current, private=True)
    _log.info("moved the saved service endpoint into the private deployment configuration")


def _backend_name(obj) -> str:
    cls = type(obj)
    return f"{cls.__module__}.{cls.__qualname__}"


# ---- the store -----------------------------------------------------------------------------------

class CredentialStore:
    def __init__(self, directory, *, keyring_backend=_AUTO, allowed_backends: Sequence[str] = SECURE_BACKENDS,
                 env: Mapping | None = None, legacy_sources: Sequence = (), service: str = SERVICE) -> None:
        self.directory = Path(directory)
        self._keyring_arg = keyring_backend
        self._allowed = tuple(allowed_backends)
        self._env = env
        self._legacy = [Path(p) for p in legacy_sources]
        self._service = service
        self._lock = threading.RLock()
        self._cache: Credentials | None = None
        self._cache_valid = False
        self._kr = _AUTO  # resolved backend (or None)
        self._kr_broken = False
        self._last_migration: str | None = None

    # paths
    @property
    def settings_path(self) -> Path:
        return self.directory / SETTINGS_NAME

    @property
    def fallback_path(self) -> Path:
        return self.directory / FALLBACK_NAME

    def _sources(self) -> list[Path]:
        seen, out = set(), []
        for p in [self.settings_path, *self._legacy]:
            key = os.path.normcase(str(p))
            if key not in seen:
                seen.add(key)
                out.append(p)
        return out

    # environment
    def _environ(self) -> Mapping:
        return os.environ if self._env is None else self._env

    def _env_credentials(self) -> Credentials | None:
        env = self._environ()
        email, password = env.get("SPARK_EMAIL") or "", env.get("SPARK_PASSWORD") or ""
        if email and password:
            return Credentials(email, password)
        return None

    def env_managed(self) -> bool:
        return self._env_credentials() is not None

    # keychain
    def _keyring(self):
        if self._kr is _AUTO:
            if self._keyring_arg is _AUTO:
                try:
                    import keyring  # optional dependency

                    self._kr = keyring.get_keyring()
                except Exception:  # noqa: BLE001 - missing or broken keyring = no keychain
                    self._kr = None
            else:
                self._kr = self._keyring_arg
        return self._kr

    def _secure_backend(self):
        """The allowlisted backend to use, or None."""
        if self._kr_broken:
            return None
        kr = self._keyring()
        if kr is None:
            return None
        candidates = [kr]
        if _backend_name(kr).endswith("chainer.ChainerBackend"):
            candidates = list(getattr(kr, "backends", []) or [])
        for backend in candidates:
            if _backend_name(backend) in self._allowed:
                return backend
        return None

    def _keychain_label(self, backend) -> str:
        return _LABELS.get(_backend_name(backend), "System keychain")

    def _keychain_read(self) -> Credentials | None:
        backend = self._secure_backend()
        if backend is None:
            return None
        try:
            raw = backend.get_password(self._service, USERNAME)
        except Exception:  # noqa: BLE001
            return None
        return self._parse_blob(raw)

    @staticmethod
    def _parse_blob(raw) -> Credentials | None:
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        if isinstance(data, dict) and isinstance(data.get("email"), str) and isinstance(data.get("password"), str) \
                and data["email"] and data["password"]:
            return Credentials(data["email"], data["password"])
        return None

    @staticmethod
    def _blob(creds: Credentials) -> str:
        return json.dumps({"v": 1, "email": creds.email, "password": creds.password})

    def _keychain_write(self, creds: Credentials) -> str | None:
        """Write + read back. Returns None on success, else a pending code."""
        backend = self._secure_backend()
        if backend is None:
            return "keychain_unavailable"
        blob = self._blob(creds)
        try:
            backend.set_password(self._service, USERNAME, blob)
            back = backend.get_password(self._service, USERNAME)
        except Exception:  # noqa: BLE001
            self._kr_broken = True
            return "keychain_unavailable"
        if back != blob:
            self._kr_broken = True
            return "readback_mismatch"
        return None

    # plaintext sources
    def _fallback_read(self) -> Credentials | None:
        if not self.fallback_path.is_file():
            return None
        data = _read_json(self.fallback_path) or {}
        email, password = data.get("email"), data.get("password")
        if isinstance(email, str) and isinstance(password, str) and email and password:
            return Credentials(email, password)
        return None

    def _legacy_read(self) -> tuple[Credentials | None, Path | None]:
        for path in self._sources():
            if not path.is_file():
                continue
            data = _read_json(path) or {}
            email, password = data.get("email"), data.get("password")
            if isinstance(email, str) and isinstance(password, str) and email and password:
                return Credentials(email, password), path
        return None, None

    def _opted_in(self) -> bool:
        data = _read_json(self.settings_path) if self.settings_path.is_file() else None
        return bool(data and data.get(OPT_IN_KEY) is True)

    def _set_opt_in(self) -> None:
        data = (_read_json(self.settings_path) if self.settings_path.is_file() else None) or {}
        if data.get(OPT_IN_KEY) is True:
            return
        data[OPT_IN_KEY] = True
        _atomic_write_json(self.settings_path, data)

    def _scrub_sources(self, *, main_keys: tuple, other_keys: tuple, mark: bool) -> None:
        for path in self._sources():
            if not path.is_file():
                continue
            data = _read_json(path)
            if data is None:
                continue
            is_main = os.path.normcase(str(path)) == os.path.normcase(str(self.settings_path))
            keys = main_keys if is_main else other_keys
            if "host" in keys and data.get("host"):
                carry_over_endpoint(self.directory, data.get("host"))
            changed = False
            for key in keys:
                if key in data:
                    del data[key]
                    changed = True
            if is_main and mark and data.get(MIGRATED_KEY) != "keychain":
                data[MIGRATED_KEY] = "keychain"
                changed = True
            if changed:
                _atomic_write_json(path, data)
                _log.info("plaintext copy scrubbed (%s)", path.name)

    def _invalidate(self) -> None:
        self._cache = None
        self._cache_valid = False

    # ---- public API ---------------------------------------------------------------------------

    def get(self) -> Credentials | None:
        env = self._env_credentials()
        if env is not None:
            return env
        with self._lock:
            if self._cache_valid:
                return self._cache
            creds = self._keychain_read() or self._fallback_read() or self._legacy_read()[0]
            self._cache, self._cache_valid = creds, True
            return creds

    def _source(self) -> tuple[str, dict]:
        if self.env_managed():
            return "environment", {"kind": "environment", "encrypted_at_rest": False, "label": "Managed by deployment"}
        backend = self._secure_backend()
        if backend is not None and self._keychain_read() is not None:
            return "keychain", {"kind": "system-keychain", "encrypted_at_rest": True,
                                "label": self._keychain_label(backend)}
        if self._fallback_read() is not None or self._legacy_read()[0] is not None:
            return "plaintext-file", {"kind": "plaintext-file", "encrypted_at_rest": False, "label": PLAINTEXT_LABEL}
        return "none", {"kind": "none", "encrypted_at_rest": False, "label": "Not configured"}

    def _migration_state(self) -> str:
        if self._legacy_read()[0] is not None and not self.env_managed():
            return "pending:" + (self._last_migration.split(":", 1)[1]
                                 if self._last_migration and self._last_migration.startswith("pending:")
                                 else "keychain_unavailable")
        data = _read_json(self.settings_path) if self.settings_path.is_file() else None
        if data and data.get(MIGRATED_KEY) == "keychain":
            return "done"
        return "none"

    def status(self) -> dict:
        with self._lock:
            source, storage = self._source()
            creds = self.get()
            return {
                "credentials_configured": creds is not None,
                "password_set": bool(creds and creds.password),
                "credential_source": source,
                "credential_storage": storage,
                "credentials_read_only": source == "environment",
                "migration": self._migration_state(),
            }

    def save(self, email=None, password=None, *, store_unencrypted=None) -> dict:
        if self.env_managed():
            raise CredentialError("credentials_managed_by_environment")
        if (email is not None and not isinstance(email, str)) or \
                (password is not None and not isinstance(password, str)) or \
                (store_unencrypted is not None and not isinstance(store_unencrypted, bool)):
            raise CredentialError("invalid_input")
        email = (email or "").strip() or None
        password = password if password is not None and password.strip() else None
        if (email and len(email) > MAX_EMAIL) or (password and len(password) > MAX_PASSWORD):
            raise CredentialError("credential_too_long")
        with self._lock:
            current = self.get()
            new_email = email or (current.email if current else None)
            new_password = password or (current.password if current else None)
            if not new_email or not new_password:
                raise CredentialError("credentials_incomplete")
            creds = Credentials(new_email, new_password)
            if len(self._blob(creds)) > MAX_BLOB:
                raise CredentialError("credential_too_long")
            if store_unencrypted is True:
                self._set_opt_in()
            if self._keychain_write(creds) is None:
                if self.fallback_path.exists():
                    self.fallback_path.unlink()
                self._scrub_sources(main_keys=("email", "password", "host"), other_keys=("password",),
                                    mark=self._legacy_read()[0] is not None)
            elif self._opted_in():
                _atomic_write_json(self.fallback_path, {"v": 1, "email": creds.email, "password": creds.password},
                                   private=True)
                if os.name == "posix":
                    try:
                        os.chmod(self.directory, 0o700)
                    except OSError:
                        pass
                _restrict_windows_acl(self.fallback_path)
                self._scrub_sources(main_keys=("email", "password"), other_keys=("password",), mark=False)
            else:
                raise CredentialError("no_secure_store")
            self._invalidate()
            return self.status()

    def clear(self) -> dict:
        if self.env_managed():
            raise CredentialError("credentials_managed_by_environment")
        with self._lock:
            kr = self._keyring()
            if kr is not None:
                try:
                    kr.delete_password(self._service, USERNAME)
                except Exception:  # noqa: BLE001 - "already clear" is success
                    pass
            try:
                self.fallback_path.unlink()
            except FileNotFoundError:
                pass
            self._scrub_sources(main_keys=("email", "password"), other_keys=("email", "password"), mark=False)
            self._invalidate()
            return self.status()

    # ---- migration (CREDENTIALS_DESIGN s4) --------------------------------------------------------

    def _acquire_lock(self) -> Path | None:
        path = self.directory / LOCK_NAME
        self.directory.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                return path
            except FileExistsError:
                try:
                    if time.time() - path.stat().st_mtime > 60:
                        path.unlink()
                        continue
                except OSError:
                    pass
                return None
        return None

    def migrate(self) -> str:
        if self.env_managed():
            if self._legacy_read()[0] is not None:
                _log.warning("a saved password is ignored because the deployment supplies credentials")
            return "none"
        lock = self._acquire_lock()
        if lock is None:
            return "pending:locked"
        try:
            with self._lock:
                result = self._migrate_locked()
                self._last_migration = result
                self._invalidate()
                return result
        finally:
            try:
                lock.unlink()
            except OSError:
                pass

    def _migrate_locked(self) -> str:
        # Step 5 first: keep existing installs rendering once the endpoint stops being a setting.
        for path in self._sources():
            data = _read_json(path) if path.is_file() else None
            if data and data.get("host"):
                carry_over_endpoint(self.directory, data.get("host"))
                break
        legacy, _src = self._legacy_read()
        if legacy is None:
            data = _read_json(self.settings_path) if self.settings_path.is_file() else None
            return "done" if data and data.get(MIGRATED_KEY) == "keychain" else "none"
        if self._keychain_read() != legacy:
            code = self._keychain_write(legacy)
            if code is not None:
                _log.warning("credential migration pending (%s)", code)
                return "pending:" + code
        self._scrub_sources(main_keys=("email", "password", "host"), other_keys=("password",), mark=True)
        _log.info("credentials migrated to the system keychain")
        return "done"


# ---- process-wide default store -------------------------------------------------------------------

_default_lock = threading.Lock()
_default: CredentialStore | None = None
_default_dir: str | None = None
_override: CredentialStore | None = None


def default_store() -> CredentialStore:
    """The store for this ComfyUI process (built once per settings dir; runs the migration on creation)."""
    global _default, _default_dir
    with _default_lock:
        if _override is not None:
            return _override
        from . import config  # lazy: config imports this module lazily too

        directory = config.settings_dir()
        if _default is None or _default_dir != str(directory):
            store = CredentialStore(directory, legacy_sources=config.legacy_settings_paths())
            try:
                store.migrate()
            except Exception as exc:  # noqa: BLE001 - never break ComfyUI start-up
                _log.warning("credential migration failed (%s); it will be retried", type(exc).__name__)
            _default, _default_dir = store, str(directory)
        return _default


def set_default_store(store: CredentialStore | None) -> None:
    """Replace the process store (tests). None clears both the replacement and the cached default."""
    global _override, _default, _default_dir
    with _default_lock:
        _override = store
        if store is None:
            _default, _default_dir = None, None

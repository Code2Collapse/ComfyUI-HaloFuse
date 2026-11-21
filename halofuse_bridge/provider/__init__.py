"""The trusted integration layer: the only place that knows the cloud endpoint and talks to the provider SDK.

Usage (contract C-1):

    from halofuse_bridge import provider
    with provider.open_backend() as be:
        be.list_skus()

The endpoint comes from deployment configuration only (env HALOFUSE_HOST, else the private deployment.json in the
bridge's system-user settings dir). It is never a user setting and never leaves this package.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import urllib.parse

from .. import legacy_names
from . import redact, sdk
from .backend import (  # noqa: F401  (re-exported port types)
    AccountCheck,
    BackendError,
    CloudBackend,
    CloudJob,
    CloudJobRef,
    Estimate,
    LibraryEntry,
    LogLine,
    RemoteFile,
    Session,
    Sku,
    StatusEvent,
    SubmitSpec,
    Truncated,
)

redact.install()

_log = logging.getLogger("halofuse_bridge.provider")
_factory_lock = threading.Lock()
_factory = None  # callable(endpoint, credentials) -> CloudBackend; None = the real SDK adapter

DEPLOYMENT_FILE = "deployment.json"


def connector_installed() -> bool:
    """True when the provider SDK is importable. Never imports it."""
    return sdk.installed()


def _valid_endpoint(value) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    if parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
        # Credentials are posted to the endpoint: plain HTTP only for a local development gateway.
        _log.warning("the configured service endpoint is not HTTPS and is ignored")
        return None
    return value.rstrip("/")


def resolve_endpoint() -> str | None:
    """The provider endpoint from deployment configuration, or None. Trusted layer only."""
    endpoint = _valid_endpoint(legacy_names.env(os.environ, "HALOFUSE_HOST"))
    if endpoint is None:
        from .. import config  # lazy: config imports provider lazily too

        path = config.settings_dir() / DEPLOYMENT_FILE
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                data = None
                _log.warning("the HaloFuse deployment file could not be read")
            if isinstance(data, dict):
                endpoint = _valid_endpoint(data.get("endpoint"))
                if endpoint is None and data.get("endpoint"):
                    _log.warning("the HaloFuse deployment file holds an invalid endpoint")
    if endpoint:
        redact.register_secret(endpoint, "endpoint")
    return endpoint


def set_backend_factory(factory) -> None:
    """Replace how open_backend() builds a backend (tests only). None restores the SDK adapter."""
    global _factory
    with _factory_lock:
        _factory = factory


@contextlib.contextmanager
def use_backend(backend):
    """Make every open_backend() in this process return *backend* (tests only)."""
    global _factory
    with _factory_lock:
        previous = _factory

    def factory(endpoint, creds):
        backend.last_credentials = creds
        return backend

    set_backend_factory(factory)
    try:
        yield backend
    finally:
        set_backend_factory(previous)


def open_backend(settings: dict | None = None, *, credentials=None):
    """A new CloudBackend for one operation. Raises BackendError before any network I/O when unconfigured."""
    endpoint = resolve_endpoint()
    if not endpoint:
        raise BackendError("not_configured", detail="no deployment endpoint")
    if credentials is None:
        from .. import credentials as _credentials

        credentials = _credentials.default_store().get()
    if credentials is None:
        raise BackendError("credentials_missing", detail="no stored or deployment credential")
    redact.register_secret(credentials.email, "email")
    redact.register_secret(credentials.password, "password")
    with _factory_lock:
        factory = _factory
    if factory is None:
        from .cloud_adapter import CloudAdapter

        factory = CloudAdapter
    return factory(endpoint, credentials)

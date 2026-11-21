"""Recognise the pre-0.4 names of HaloFuse's own files, folders, environment variables and setting keys.

The old names are not written in the source (owner order 2026-11-21: nothing in the shipped code may identify the
cloud provider). Each is recognised by the SHA-256 of the name, the same technique tests/fakes/leaks.py uses for the
provider domain. Every migration is one-time and never deletes user data: a folder is moved (so a stored secret
exists once), a file is renamed, a key is moved to its new name.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import threading
from collections.abc import Mapping
from pathlib import Path

_log = logging.getLogger("halofuse_bridge.legacy_names")

SETTINGS_FILE = "0b0cc60ecf2dc4bee1f41d99b770be31397f0028131335c9439548fcfb469135"  # 0.3 settings file name
SETTINGS_DIR = "80098b2b9241894e99af3effbb4b54c7227545ec2d1bd82672536d3586726746"  # 0.3 settings folder name
ENV = {  # new environment variable name -> SHA-256 of its 0.3 name
    "HALOFUSE_HOST": "d774f6b1f590a9a31d303e8e1f4e94fa63b4406668baecde08b17968ba097a84",
    "HALOFUSE_EMAIL": "3b2945f7dfaa9cefbf551449131a139554d2f27beaf7064a9b7b63ef2cb15422",
    "HALOFUSE_PASSWORD": "d17d39cba0217d07a6c6448110ee5a0e8c4fc1da5d8359960cd47ee000d7ec87",
}
SETTING_KEYS = {  # SHA-256 of a 0.3 setting key -> its HaloFuse name
    "ae2fc22e3cfef864a3abbc06a99e2e801df3e9407d41665829957baced7434a3": "assets_library_path",
    "a42c014165f6558796676f30a3b43e103f92788632405d71f1a3479e0dd2a21f": "assets_library_space",
}
SETTINGS_DIR_NAME = "halofuse_bridge"

_warned: set[str] = set()
_warn_lock = threading.Lock()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def matches(name: str, digest: str) -> bool:
    """True when *name* (or its lower case: Windows file names are case-insensitive) hashes to *digest*."""
    return isinstance(name, str) and (_sha(name) == digest or _sha(name.lower()) == digest)


def find(directory, digest: str, *, prefix: str = "", suffix: str = "") -> Path | None:
    """The first entry of *directory* named ``prefix + X + suffix`` where X hashes to *digest*, else None."""
    if directory is None:
        return None
    try:
        with os.scandir(directory) as entries:
            names = sorted(e.name for e in entries)
    except OSError:
        return None
    for name in names:
        if len(name) <= len(prefix) + len(suffix) or not name.startswith(prefix) or not name.endswith(suffix):
            continue
        core = name[len(prefix):len(name) - len(suffix)] if suffix else name[len(prefix):]
        if matches(core, digest):
            return Path(directory) / name
    return None


def env(environ: Mapping, new_name: str) -> str:
    """The value of environment variable *new_name*, else of its 0.3 name (with one warning per process), else ""."""
    value = environ.get(new_name) or ""
    if value:
        return value
    digest = ENV.get(new_name)
    if digest is None:
        return ""
    for key, old_value in list(environ.items()):
        if old_value and isinstance(key, str) and _sha(key.upper()) == digest:
            with _warn_lock:
                first = new_name not in _warned
                _warned.add(new_name)
            if first:
                _log.warning("a pre-0.4 environment variable name is in use; rename it to %s", new_name)
            return old_value
    return ""


def migrate_keys(data: dict) -> bool:
    """Move 0.3 setting keys to their HaloFuse names in place (an existing new key wins). True when changed."""
    if not isinstance(data, dict):
        return False
    changed = False
    for key in [k for k in data if isinstance(k, str) and _sha(k) in SETTING_KEYS]:
        new_key = SETTING_KEYS[_sha(key)]
        value = data.pop(key)
        data.setdefault(new_key, value)
        changed = True
    return changed


def adopt_dir(new_dir) -> None:
    """Move the 0.3 settings folder that sits next to *new_dir* into place, once. An existing *new_dir* is never
    touched. The old folder name differs only in the part after the shared prefix ("__" for ComfyUI system-user
    folders, "" for user/default)."""
    new_dir = Path(new_dir)
    if new_dir.exists() or not new_dir.name.endswith(SETTINGS_DIR_NAME):
        return
    old = find(new_dir.parent, SETTINGS_DIR, prefix=new_dir.name[:-len(SETTINGS_DIR_NAME)])
    if old is None or not old.is_dir():
        return
    try:
        os.replace(old, new_dir)
        _log.info("moved the HaloFuse settings folder to its new name")
    except OSError:
        try:
            shutil.copytree(old, new_dir)
            _log.warning("copied the HaloFuse settings folder to its new name (the old folder could not be moved)")
        except OSError as exc:
            _log.warning("could not migrate the HaloFuse settings folder (%s)", type(exc).__name__)


def adopt_file(directory, new_name: str, digest: str) -> None:
    """Inside *directory*, rename the file whose 0.3 name hashes to *digest* to *new_name*, unless that exists."""
    directory = Path(directory)
    target = directory / new_name
    if target.exists():
        return
    old = find(directory, digest)
    if old is None or not old.is_file():
        return
    try:
        os.replace(old, target)
    except OSError as exc:
        _log.warning("could not rename the HaloFuse settings file (%s)", type(exc).__name__)

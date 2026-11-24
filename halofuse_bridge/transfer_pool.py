"""Bounded model-transfer concurrency on this computer (L9.340).

One limiter per direction ("download", "upload"): at most N files transfer at the same time, N read from the settings
(transfer_max_downloads / transfer_max_uploads, 1-4, default 2) when a file asks for a slot and again about once a
second while it waits, so a changed setting applies to the next file that starts, also inside a batch that is already
running. A waiting file can be cancelled (acquire returns False and the file never starts).

Plus an atomic per-model claim registry: a request claims every model it transfers under one lock, so two requests
for the same model at the same time cannot both start it (download key: (folder, name); upload key: remote_rel).
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Hashable, Iterable

DIRECTIONS = ("download", "upload")
SETTING_KEYS = {"download": "transfer_max_downloads", "upload": "transfer_max_uploads"}
DEFAULT_LIMIT = 2
MIN_LIMIT, MAX_LIMIT = 1, 4
WAIT_S = 0.2                      # a waiter re-checks its cancel at least this often (never a busy spin)
REREAD_S = 1.0                    # a waiter re-reads the limit at most this often (the settings are a file)

_cond = threading.Condition()
_active = {d: 0 for d in DIRECTIONS}
_waiting = {d: 0 for d in DIRECTIONS}
_claims: dict[str, set] = {d: set() for d in DIRECTIONS}


def _check(direction: str) -> None:
    if direction not in DIRECTIONS:
        raise ValueError(f"unknown transfer direction {direction!r}")


def limit(direction: str, settings: dict | None = None) -> int:
    """The current limit for *direction*: the setting clamped to 1..4; missing or not an integer -> 2. Without
    *settings* the saved settings are read (unreadable -> 2)."""
    _check(direction)
    if settings is None:
        try:
            from .config import load_settings

            settings = load_settings()
        except Exception:  # noqa: BLE001 - no settings folder: the default
            return DEFAULT_LIMIT
    raw = settings.get(SETTING_KEYS[direction], DEFAULT_LIMIT) if isinstance(settings, dict) else DEFAULT_LIMIT
    if not isinstance(raw, int) or isinstance(raw, bool):
        return DEFAULT_LIMIT
    return min(MAX_LIMIT, max(MIN_LIMIT, raw))


def acquire(direction: str, cancel: Callable[[], bool] | None = None, settings: dict | None = None) -> bool:
    """Block until a *direction* slot is free and take it (True), or return False when *cancel()* is true before a
    slot was taken. The caller releases a taken slot with release(direction)."""
    n = limit(direction, settings)
    read_at = time.monotonic()
    with _cond:
        _waiting[direction] += 1
    try:
        while True:
            if cancel is not None and cancel():    # outside our lock: cancel() takes the caller's registry lock
                return False
            if time.monotonic() - read_at >= REREAD_S:
                n, read_at = limit(direction, settings), time.monotonic()
            with _cond:
                if _active[direction] < n:
                    _active[direction] += 1
                    return True
                _cond.wait(WAIT_S)
    finally:
        with _cond:
            _waiting[direction] -= 1


def release(direction: str) -> None:
    _check(direction)
    with _cond:
        _active[direction] = max(0, _active[direction] - 1)
        _cond.notify_all()


def claim_all(direction: str, keys: Iterable[Hashable]) -> list:
    """Claim every key, or none: returns the keys someone else already holds (empty list = all claimed now)."""
    _check(direction)
    keys = list(dict.fromkeys(keys))
    with _cond:
        taken = [k for k in keys if k in _claims[direction]]
        if not taken:
            _claims[direction].update(keys)
        return taken


def unclaim(direction: str, key: Hashable) -> None:
    _check(direction)
    with _cond:
        _claims[direction].discard(key)


def run_threads(target: Callable, args_list: list, name: str) -> None:
    """Run target(*args) for every entry, each in its own daemon thread, and wait for all of them. An entry whose
    thread cannot be started (the process is out of threads) runs in the calling thread instead."""
    started = []
    for i, args in enumerate(args_list):
        thread = threading.Thread(target=target, args=args, daemon=True, name=f"{name}-{i}")
        try:
            thread.start()
            started.append(thread)
        except RuntimeError:
            target(*args)
    for thread in started:
        thread.join()


def status(settings: dict | None = None) -> dict:
    """{"download": {"limit", "active", "waiting"}, "upload": {...}}; limit = the current setting."""
    limits = {d: limit(d, settings) for d in DIRECTIONS}
    with _cond:
        return {d: {"limit": limits[d], "active": _active[d], "waiting": _waiting[d]} for d in DIRECTIONS}


def reset_for_tests() -> None:
    with _cond:
        for d in DIRECTIONS:
            _active[d] = 0
            _waiting[d] = 0
            _claims[d].clear()
        _cond.notify_all()

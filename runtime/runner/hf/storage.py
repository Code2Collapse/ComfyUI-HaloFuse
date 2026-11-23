"""Model prefetch policy and a cancellable copy (C4).

The runner copies the models a job names from the model mount to local disk before the first render, because
ComfyUI's loader reads weights in many small random reads, which is slow on a network / FUSE mount. The copy is only
worth it when the source is remote and the destination is a real local disk: copying from a local disk doubles the
disk use for nothing, and copying into a RAM-backed /tmp (tmpfs) spends host memory the job needs. Decisions use the
container's /proc/mounts and /proc/meminfo; without them (unknown platform) the previous behaviour (copy) is kept.

`copy_file` checks a stop event between chunks, so a cancelled or failed job never waits for a multi-GiB copy.
"""
from __future__ import annotations

import os
import time
from pathlib import Path, PurePosixPath

LOCAL_DISK_FS = frozenset({"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs", "overlay"})
RAM_FS = frozenset({"tmpfs", "ramfs"})
_GIB = 2**30


def parse_mounts(text: str) -> list:
    """[(mountpoint, fstype)] from /proc/mounts text (octal escapes such as \\040 decoded)."""
    out = []
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        point = parts[1].encode("latin-1", "replace").decode("unicode_escape", "replace")
        out.append((point, parts[2]))
    return out


def fs_type_of(path, mounts) -> str | None:
    """File-system type of the longest mount point containing `path` (POSIX paths), or None without a table."""
    target = PurePosixPath(str(path).replace("\\", "/"))
    best, best_len = None, -1
    for point, fstype in mounts or ():
        mp = PurePosixPath(point)
        if mp == target or mp in target.parents:
            n = len(mp.parts)
            if n > best_len:
                best, best_len = fstype, n
    return best


def _gib(n) -> str:
    return f"{n / _GIB:.1f} GiB"


def prefetch_plan(total, *, src_fs, dst_fs, disk_free, mem_available) -> tuple:
    """(copy?, reason) for prefetching `total` bytes of models from a `src_fs` mount to a `dst_fs` local directory."""
    if not total:
        return False, "no model files to copy"
    if src_fs in LOCAL_DISK_FS:
        return False, f"the models are already on a local disk ({src_fs}); ComfyUI reads them in place"
    if src_fs in RAM_FS:
        return False, f"the models are already in RAM ({src_fs}); ComfyUI reads them in place"
    if dst_fs in RAM_FS:
        if mem_available is None or total > 0.5 * mem_available:
            avail = _gib(mem_available) if mem_available is not None else "an unknown amount of"
            return False, (f"/tmp is RAM-backed ({dst_fs}); a {_gib(total)} copy would use RAM the job needs "
                           f"({avail} available)")
    if disk_free is not None and total > 0.9 * disk_free:
        return False, f"{_gib(total)} needed but only {_gib(disk_free)} free on the local disk"
    return True, f"copying {_gib(total)} to local disk"


def copy_file(src, dest, stop, chunk: int = 64 * 2**20):
    """Copy src to dest via dest.part (atomic rename). Returns the seconds taken, or None when `stop` was set
    (the partial file is removed)."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    started = time.monotonic()
    stopped = False
    try:
        with open(src, "rb") as fin, open(tmp, "wb") as fout:
            while True:
                if stop.is_set():
                    stopped = True
                    break
                block = fin.read(chunk)
                if not block:
                    break
                fout.write(block)
    except BaseException:
        _unlink(tmp)
        raise
    if stopped:
        _unlink(tmp)
        return None
    os.replace(tmp, dest)
    return time.monotonic() - started


def _unlink(path):
    try:
        Path(path).unlink()
    except OSError:
        pass

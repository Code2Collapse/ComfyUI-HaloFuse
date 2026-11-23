"""HaloFuse model-load gate, loaded INSIDE ComfyUI (L9.338). Standard library only.

The runner submits the prompt while model copies to local NVMe are still running (hf/prefetch.py). ComfyUI's loaders
memory-map a model file when the loader node runs and read it lazily afterwards, so the path a loader gets is the
path it reads for the rest of the job. This gate wraps folder_paths.get_full_path:

* a model of the prefetch plan whose local copy is complete -> the local copy (also for custom-node folders whose
  search order would find the original first);
* a planned model whose copy is still running -> asks the runner for it first (want/<key>) and waits for that one
  copy, then returns the local copy. The copy is renamed into place only when complete (.part + os.replace), so the
  local path is never returned half-written;
* the copy failed, the prefetch ended without it, it made no progress for HF_PREFETCH_STALL_SECONDS (default 600)
  or ComfyUI was interrupted -> the original path (correct data, read from the model library);
* anything else -> exactly what ComfyUI returned.

Only ComfyUI's prompt-worker thread ever waits. A lookup on the main thread (the HTTP server's event loop: a node's
INPUT_TYPES or VALIDATE_INPUTS while a prompt is posted) gets the local copy if it is complete, else the original at
once, so the server never freezes.

The runner writes this file's source into ComfyUI's custom_nodes as halofuse_model_gate.py with `_comfy_entry()`
appended. It registers no nodes and never stops ComfyUI from starting.
"""
import hashlib
import json
import os
import threading
import time

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
POLL_SECONDS = 0.25


def key(rel):
    """Marker-file name (same function in hf/prefetch.py)."""
    return hashlib.sha1(rel.encode("utf-8")).hexdigest()[:16]


def _stall_default():
    try:
        return max(1.0, float(os.environ.get("HF_PREFETCH_STALL_SECONDS") or 600))
    except ValueError:
        return 600.0


def _interrupted():
    try:
        import comfy.model_management as mm  # noqa: PLC0415 - only inside ComfyUI
        return bool(mm.processing_interrupted())
    except Exception:  # noqa: BLE001
        return False


class _Plan:
    def __init__(self, state_dir):
        self.state = os.fspath(state_dir)
        self._mtime = None
        self.base = self.local = None
        self.files = {}

    def load(self):
        path = os.path.join(self.state, "plan.json")
        try:
            mtime = os.stat(path).st_mtime_ns
        except OSError:
            self.files = {}
            self._mtime = None
            return False
        if mtime != self._mtime:
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
                self.base = os.path.realpath(data["base"])
                self.local = data["local"]
                self.files = {f["rel"]: int(f["size"]) for f in data.get("files") or []}
                self._mtime = mtime
            except (OSError, ValueError, KeyError, TypeError):
                self.files = {}
                return False
        return bool(self.files)

    def rel_of(self, path):
        try:
            real = os.path.realpath(path)
            rel = os.path.relpath(real, self.base)
        except (OSError, ValueError):
            return None
        rel = rel.replace(os.sep, "/")
        if rel.startswith("../") or rel == ".." or os.path.isabs(rel):
            return None
        return rel if rel in self.files else None


def _print(msg):
    print(msg, flush=True)


def _complete(path, size):
    try:
        return os.path.isfile(path) and os.path.getsize(path) == size
    except OSError:
        return False


def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def install(fp, state_dir, *, sleep=time.sleep, clock=time.monotonic, stall_seconds=None, log=None):
    """Wrap fp.get_full_path (fp: ComfyUI's folder_paths module or a stand-in). Installing twice wraps once."""
    log = log or _print
    original = getattr(fp, "_halofuse_original_get_full_path", None) or fp.get_full_path
    plan = _Plan(state_dir)
    stall = float(stall_seconds) if stall_seconds is not None else _stall_default()
    state = os.fspath(state_dir)

    def wait_for(rel, src):
        size = plan.files[rel]
        local = os.path.join(plan.local, *rel.split("/"))
        if _complete(local, size):
            return local
        if threading.current_thread() is threading.main_thread():
            return src  # never block ComfyUI's HTTP server
        try:
            os.makedirs(os.path.join(state, "want"), exist_ok=True)
            with open(os.path.join(state, "want", key(rel)), "w", encoding="utf-8"):
                pass
        except OSError:
            pass
        part = local + ".part"
        try:
            have = os.path.getsize(part)
        except OSError:
            have = 0
        log(f"[halofuse] model wait: {rel} ({size / 2**30:.1f} GiB, {100 * have / max(size, 1):.0f}% copied) - "
            "waiting for its local copy so it is never read half-written")
        started = last_change = clock()
        last_progress = _read(os.path.join(state, "progress"))
        while True:
            if _complete(local, size):
                log(f"[halofuse] model wait done: {rel} local copy ready after {clock() - started:.0f} s")
                return local
            reason = None
            if os.path.exists(os.path.join(state, "failed", key(rel))):
                reason = "its copy failed"
            elif os.path.exists(os.path.join(state, "finished")):
                reason = "the prefetch ended without it"
            elif _interrupted():
                reason = "the render was interrupted"
            else:
                progress = _read(os.path.join(state, "progress"))
                if progress != last_progress:
                    last_progress, last_change = progress, clock()
                elif clock() - last_change >= stall:
                    reason = f"the prefetch made no progress for {stall:.0f} s"
            if reason:
                if _complete(local, size):  # finished right now
                    continue
                log(f"[halofuse] model wait done: {rel} read from the model library after {clock() - started:.0f} s "
                    f"({reason})")
                return src
            sleep(POLL_SECONDS)

    def gated(folder_name, filename):
        path = original(folder_name, filename)
        if path is None:
            return path
        try:
            if not plan.load():
                return path
            rel = plan.rel_of(path)
            if rel is None:
                return path
            return wait_for(rel, path)
        except Exception as err:  # noqa: BLE001 - the gate must never break a model lookup
            log(f"[halofuse] model gate skipped for {filename} ({type(err).__name__}: {err})")
            return path

    gated._halofuse_gate = True
    fp._halofuse_original_get_full_path = original
    fp.get_full_path = gated
    return gated


def _comfy_entry():
    """Called at the end of the copy the runner places in ComfyUI's custom_nodes."""
    try:
        state = os.environ.get("HF_PREFETCH_STATE")
        if not state:
            return
        import folder_paths  # noqa: PLC0415 - ComfyUI's module
        install(folder_paths, state)
        print("[halofuse] model-load gate active: a model whose local copy is still running waits for that copy",
              flush=True)
    except Exception as err:  # noqa: BLE001 - ComfyUI must always start
        _print(f"[halofuse] model-load gate not active ({type(err).__name__}: {err})")

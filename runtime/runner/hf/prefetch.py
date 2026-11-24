"""Model prefetch in first-use order while the workflow already runs (L9.338).

The runner used to copy every model the job names to local NVMe and only then submit the prompt (measured: 118.8 GiB,
931 s of waiting before the first node, although the first nodes needed only small files). Now the prompt is submitted
as soon as ComfyUI is up and the copies keep running in the order the workflow first uses the files. A model load that
reaches a file whose copy is not finished waits for that one copy (hf/model_gate.py, inside ComfyUI): ComfyUI's loaders
memory-map the file at loader time and read it lazily afterwards, so a loader must get either a complete local copy
or the original; copy_file's `.part` + rename means it never sees a half-written file.

Files shared with the gate (state_dir):
  plan.json   {"version": 1, "base", "local", "files": [{"rel", "size"}, ...]} in first-use order
  want/<key>  the gate asks for this file now (copied before anything else, by an extra priority worker)
  failed/<key> the copy of this file failed: the gate lets ComfyUI read the original
  progress    total bytes copied so far (the gate's stall check)
  finished    the prefetch has ended (done, stopped or crashed): the gate stops waiting
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

from hf import storage


def key(rel: str) -> str:
    """Marker-file name for a model path relative to the model base (same function in hf/model_gate.py)."""
    return hashlib.sha1(rel.encode("utf-8")).hexdigest()[:16]


def _is_link(value, nodes) -> bool:
    return (isinstance(value, (list, tuple)) and len(value) == 2 and isinstance(value[1], int)
            and str(value[0]) in nodes)


def execution_order(workflow: dict) -> list[str]:
    """Estimated ComfyUI execution order of an API-format graph (comfy_execution/graph.py ExecutionList): output
    nodes are added in id order with ComfyUI's stack walk, then ready nodes are picked the way
    ux_friendly_pick_node does. Nodes nothing consumes stand in for output nodes (the runner has no node
    definitions). An estimate: the gate's want/ markers correct a wrong guess at run time."""
    nodes = {str(k): v for k, v in (workflow or {}).items() if isinstance(v, dict)}
    deps: dict[str, list[str]] = {}
    consumers: dict[str, set[str]] = {n: set() for n in nodes}
    for n, node in nodes.items():
        inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
        seen = []
        for value in inputs.values():
            if _is_link(value, nodes) and str(value[0]) not in seen:
                seen.append(str(value[0]))
        deps[n] = seen
        for d in seen:
            consumers[d].add(n)
    sinks = sorted((n for n in nodes if not consumers[n]), key=lambda x: (len(x), x))
    pending: dict[str, bool] = {}
    for out in sinks:
        stack = [out]
        while stack:
            u = stack.pop()
            if u in pending:
                continue
            pending[u] = True
            stack.extend(deps[u])
    for n in nodes:  # nodes inside a cycle with no sink are never reached from one
        pending.setdefault(n, True)
    block = {n: len(deps[n]) for n in pending}
    sink_set = set(sinks)

    def blocks_sink(n, depth):
        frontier = {n}
        for _ in range(depth):
            frontier = set().union(*(consumers[x] for x in frontier)) if frontier else set()
        return bool(frontier & sink_set)

    order: list[str] = []
    while pending:
        ready = [n for n in pending if block[n] <= 0]
        if not ready:  # a cycle: keep the rest in insertion order
            order.extend(pending)
            break
        pick = (next((n for n in ready if n in sink_set), None)
                or next((n for n in ready if blocks_sink(n, 1)), None)
                or next((n for n in ready if blocks_sink(n, 2)), None)
                or ready[0])
        del pending[pick]
        order.append(pick)
        for c in consumers[pick]:
            block[c] -= 1
    return order


def _self_contained(src: Path, exts: tuple) -> bool:
    """True when every file beside `src` is a model file too. A custom-node model folder that also holds config.json,
    a tokenizer and the like is read as a whole by its loader: copying one file of it would break that loader."""
    try:
        return all(not p.is_file() or p.name.lower().endswith(exts) for p in src.parent.iterdir())
    except OSError:
        return False


def plan_files(workflows: list, base: Path, folders: list, exts: tuple, extra_folders=()) -> list:
    """[(rel, size)] of the model files the workflows name, in first-use order: workflows in manifest order, nodes in
    execution_order(), and every folder in `folders` where the name exists (all matches, as before). A file in one of
    `extra_folders` (custom-node folders) is planned only when its directory holds nothing but model files."""
    base = Path(base)
    extra = set(extra_folders)
    found: dict[str, int] = {}
    for wf in workflows:
        if not isinstance(wf, dict):
            continue
        nodes = {str(k): v for k, v in wf.items() if isinstance(v, dict)}
        for node_id in execution_order(wf):
            inputs = nodes[node_id].get("inputs")
            for value in inputs.values() if isinstance(inputs, dict) else ():
                if not (isinstance(value, str) and value.lower().endswith(exts)):
                    continue
                name = value.replace("\\", "/").lstrip("/")
                for folder in folders:
                    rel = Path(folder) / name
                    if ".." in rel.parts:
                        continue
                    key_rel = rel.as_posix()
                    if key_rel in found:
                        continue
                    try:
                        src = base / rel
                        if src.is_file() and (folder not in extra or _self_contained(src, exts)):
                            found[key_rel] = src.stat().st_size
                    except OSError:
                        pass
    return list(found.items())


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class Prefetcher:
    """Copies `files` [(rel, size)] from `base` to `local` in order; `want/` markers jump the queue."""

    def __init__(self, base, local, state_dir, files, *, threads: int, stop: threading.Event, log, workers=None):
        self.base, self.local, self.state = Path(base), Path(local), Path(state_dir)
        self.files = list(files)
        self.threads = max(1, int(threads))
        self.stop = stop
        self.log = log
        self.workers = workers if workers is not None else []  # the runner joins these on exit
        self._lock = threading.Lock()
        self._pending = list(self.files)
        self._done: set[str] = set()
        self._copied = 0
        self._last_write = 0.0

    # ---- plan / state files ----------------------------------------------------------------------------------
    def write_plan(self) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        for sub in ("want", "failed"):
            d = self.state / sub
            if d.is_dir():
                for f in d.iterdir():
                    try:
                        f.unlink()
                    except OSError:
                        pass
            d.mkdir(exist_ok=True)
        for name in ("finished", "progress"):
            try:
                (self.state / name).unlink()
            except OSError:
                pass
        for rel, size in self.files:  # a stale local file of another size would be found before its replacement
            dest = self.local / rel
            try:
                if dest.is_file() and dest.stat().st_size != size:
                    dest.unlink()
            except OSError:
                pass
        _atomic_write(self.state / "progress", "0")
        _atomic_write(self.state / "plan.json", json.dumps({
            "version": 1, "base": str(self.base), "local": str(self.local),
            "files": [{"rel": rel, "size": size} for rel, size in self.files]}))

    def remaining(self) -> tuple:
        with self._lock:
            left = [(r, s) for r, s in self.files if r not in self._done]
        return len(left), sum(s for _, s in left)

    def _progress(self, n: int) -> None:
        with self._lock:
            self._copied += n
            now = time.monotonic()
            if now - self._last_write < 1.0:
                return
            self._last_write = now
            total = self._copied
        try:
            _atomic_write(self.state / "progress", str(total))
        except OSError:
            pass

    def _wanted(self) -> set:
        try:
            return {p.name for p in (self.state / "want").iterdir()}
        except OSError:
            return set()

    def _take(self, wanted_only: bool):
        wanted = self._wanted()
        with self._lock:
            for i, (rel, size) in enumerate(self._pending):
                if key(rel) in wanted:
                    return self._pending.pop(i)
            if wanted_only or not self._pending:
                return None
            return self._pending.pop(0)

    # ---- copying ---------------------------------------------------------------------------------------------
    def _copy(self, rel: str, size: int) -> None:
        dest = self.local / rel
        try:
            if dest.is_file() and dest.stat().st_size == size:
                return
            secs = storage.copy_file(self.base / rel, dest, self.stop, on_chunk=self._progress)
            if secs:
                self.log(f"prefetch: {rel} ({size / 2**30:.1f} GiB) in {secs:.0f}s "
                         f"({size / 2**20 / max(secs, 0.001):.0f} MiB/s)")
        except Exception as err:  # noqa: BLE001 - ComfyUI reads this file from the model library instead
            self.log(f"prefetch: {rel} failed ({err}); ComfyUI reads it from the model library")
            try:
                (self.state / "failed" / key(rel)).touch()
            except OSError:
                pass
        finally:
            with self._lock:
                self._done.add(rel)

    def _worker(self, wanted_only: bool) -> None:
        while not self.stop.is_set():
            item = self._take(wanted_only)
            if item is None:
                if not wanted_only:
                    return
                with self._lock:
                    if not self._pending:
                        return
                self.stop.wait(0.25)
                continue
            self._copy(*item)

    def run(self) -> bool:
        """Copy everything (returns True) or stop early when `stop` is set (False). Always writes `finished`."""
        try:
            threads = [threading.Thread(target=self._worker, args=(False,), name=f"hf-prefetch-{i}", daemon=True)
                       for i in range(min(self.threads, max(1, len(self.files))))]
            threads.append(threading.Thread(target=self._worker, args=(True,), name="hf-prefetch-wanted", daemon=True))
            self.workers.extend(threads)
            for t in threads:
                t.start()
            for t in threads:
                while t.is_alive() and not self.stop.is_set():
                    t.join(0.5)
            return not self.stop.is_set()
        finally:
            try:
                _atomic_write(self.state / "progress", str(self._copied))
                (self.state / "finished").touch()
            except OSError:
                pass

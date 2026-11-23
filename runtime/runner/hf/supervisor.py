"""Process supervision and run state (F3, X5, A6).

- `ComfyProcess`: starts ComfyUI with piped stdout/stderr, relays every line unchanged (except spoofed runner
  prefixes, TM-008) to the same stream and to render.log, keeps a tail for crash classification, and notices the
  "Import times for custom nodes:" line (node-import phase mark).
- `RunContext`: phase timing, per-entry outcomes, the last classified error, restart count, cancellation flag
  set by the signal handler, and the single `final` event.
- `install_signal_handlers`: SIGTERM/SIGINT (+SIGBREAK on Windows) only set a flag; the runner's loops act on it.
- `Heartbeat`: a small JSON file (pid, time, runner state) the Docker HEALTHCHECK reads (scripts/healthcheck.py).

ComfyUI runs in its own process group (POSIX session). Stopping it stops the whole group, and after a death the
leftover group members are killed too, so no child of a custom node keeps GPU or host memory into a restarted
ComfyUI or keeps the container alive after the job.
"""
import collections
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import events as hf_events

NODE_IMPORT_MARK = "Import times for custom nodes:"


class Cancelled(Exception):
    """Raised by runner loops once a stop signal was received."""


class ComfyDied(Exception):
    def __init__(self, returncode, classification, node=None, class_type=None):
        super().__init__(f"ComfyUI died (code {returncode})")
        self.returncode = returncode
        self.classification = classification
        self.node = node
        self.class_type = class_type


class ComfyProcess:
    def __init__(self, cmd, cwd, env, emitter, on_node_import=None, tail_lines=200, on_line=None):
        self.cmd = list(cmd)
        self.cwd = cwd
        self.env = env
        self.em = emitter
        self.on_node_import = on_node_import
        self.on_line = on_line  # every ComfyUI line, unchanged (R-PREVIEW-01: preview fallback detection)
        self._tail = collections.deque(maxlen=tail_lines)
        self._threads = []
        self.proc = None
        self.started_at = None

    def start(self):
        flags = {}
        if os.name == "nt":
            flags["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            flags["start_new_session"] = True  # a stop signal to the runner is handled by the runner only
        self.started_at = time.monotonic()
        self.proc = subprocess.Popen(self.cmd, cwd=self.cwd, env=self.env, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, **flags)
        for pipe, name in ((self.proc.stdout, "stdout"), (self.proc.stderr, "stderr")):
            t = threading.Thread(target=self._pump, args=(pipe, name), name=f"hf-pump-{name}", daemon=True)
            t.start()
            self._threads.append(t)
        return self

    def _pump(self, pipe, stream):
        marked = False
        try:
            for raw in iter(pipe.readline, b""):
                text = raw.decode("utf-8", "replace")
                if text.endswith("\n"):
                    text = text[:-1]
                if text.endswith("\r"):
                    text = text[:-1]
                self._tail.append(text)
                self.em.line(hf_events.escape_child_line(text), stream=stream)
                if self.on_line is not None:
                    try:
                        self.on_line(text)
                    except Exception:  # noqa: BLE001 - a consumer error never stops the relay
                        pass
                if not marked and text.startswith(NODE_IMPORT_MARK):
                    marked = True
                    if self.on_node_import:
                        try:
                            self.on_node_import(time.monotonic() - self.started_at)
                        except Exception:  # noqa: BLE001
                            pass
        except (OSError, ValueError):
            pass
        finally:
            try:
                pipe.close()
            except OSError:
                pass

    # Popen-compatible surface used by the runner script.
    def poll(self):
        return self.proc.poll()

    @property
    def returncode(self):
        return self.proc.returncode

    def terminate(self):
        """SIGTERM to ComfyUI's whole process group (Windows: the whole tree, forcibly)."""
        self._signal_tree(getattr(signal, "SIGTERM", None))

    def kill(self):
        """SIGKILL to ComfyUI's whole process group (Windows: the whole tree)."""
        self._signal_tree(getattr(signal, "SIGKILL", None))

    def reap_tree(self):
        """After ComfyUI itself died: kill what is left of its process group / tree. Never raises."""
        if self.proc is None:
            return
        if os.name == "nt":
            for child in _windows_children(self.proc.pid):
                _taskkill(child)
            return
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except (OSError, AttributeError):
            pass

    def _signal_tree(self, sig):
        if self.proc is None:
            return
        if os.name == "nt":
            if self.proc.poll() is None:
                _taskkill(self.proc.pid)
            self.reap_tree()
            return
        try:
            os.killpg(self.proc.pid, sig)  # pgid == pid: started with start_new_session
        except (OSError, AttributeError):
            try:
                self.proc.send_signal(sig)
            except OSError:
                pass

    def wait(self, timeout=None):
        return self.proc.wait(timeout=timeout)

    def join_output(self, timeout=2.0):
        """Wait (bounded) until the pumps reached EOF, so the tail is complete after a death."""
        deadline = time.monotonic() + timeout
        for t in self._threads:
            t.join(max(0.0, deadline - time.monotonic()))

    def tail(self):
        return list(self._tail)


def _taskkill(pid):
    try:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        pass


def _windows_children(pid):
    """Pids whose parent is `pid` (orphans keep their ParentProcessId on Windows). Test platform only."""
    cmd = ("Get-CimInstance Win32_Process -Filter 'ParentProcessId=%d' | ForEach-Object { $_.ProcessId }" % int(pid))
    try:
        res = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                             capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(t) for t in res.stdout.split() if t.isdigit()]


class Heartbeat:
    """Writes {"pid", "ts", "state", "port", "exit_code"} to `path` (atomic replace) every `interval` seconds and on
    every state change. I/O errors are ignored: the heartbeat must never break a job."""

    def __init__(self, path, interval=5.0, port=None):
        self.path = Path(path)
        self.interval = max(0.2, float(interval))
        self.port = port
        self.state = "starting"
        self.exit_code = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._write()
        self._thread = threading.Thread(target=self._loop, name="hf-heartbeat", daemon=True)
        self._thread.start()
        return self

    def set_state(self, state):
        with self._lock:
            self.state = state
        self._write()

    def stop(self, state="finished", exit_code=None):
        self._stop.set()
        with self._lock:
            self.state = state
            self.exit_code = exit_code
        self._write()

    def _loop(self):
        while not self._stop.wait(self.interval):
            self._write()

    def _write(self):
        with self._lock:
            record = {"pid": os.getpid(), "ts": time.time(), "state": self.state, "port": self.port,
                      "exit_code": self.exit_code}
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_name(self.path.name + ".tmp")
                tmp.write_text(json.dumps(record), encoding="utf-8")
                os.replace(tmp, self.path)
            except OSError:
                pass


def install_signal_handlers(ctx):
    def handler(signum, _frame):
        if not ctx.cancel.is_set():
            ctx.cancel_signal = signum
            ctx.cancel.set()

    installed = []
    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, handler)
            installed.append(name)
        except (ValueError, OSError):
            pass  # not the main thread / not supported here
    return installed


class RunContext:
    def __init__(self, emitter, clock=time.monotonic):
        self.em = emitter
        self.clock = clock
        self.cancel = threading.Event()
        self.cancel_signal = None
        self.cancel_reported = False
        self.manifest_total = None
        self.items = {}            # wf -> {"wf", "status", "outputs", "error"}
        self.last_error = None     # last classification dict
        self.restarts = 0
        self.durations = {}
        self._phase_start = {}
        self.outputs = 0
        self.output_bytes = 0
        self.timed_out = False
        self.consecutive_deaths = 0  # ComfyUI deaths since the last succeeded entry (crash-loop breaker)
        self.peaks = lambda: {}
        self.proc = None

    # -- phases ------------------------------------------------------------------------------------------------
    def phase(self, name, state, **fields):
        now = self.clock()
        if state == "start":
            self._phase_start[name] = now
            self.em.emit("phase", phase=name, state="start", **fields)
            return
        if state == "end":
            started = self._phase_start.pop(name, None)
            dur = round(now - started, 3) if started is not None else None
            if dur is not None:
                self.durations[name] = round(self.durations.get(name, 0.0) + dur, 3)
            self.em.emit("phase", phase=name, state="end", dur=dur, **fields)
            return
        self.em.emit("phase", phase=name, state="mark", **fields)

    # -- errors / items --------------------------------------------------------------------------------------
    def error(self, classification, **fields):
        self.last_error = dict(classification)
        payload = dict(classification)
        payload.update({k: v for k, v in fields.items() if v is not None})
        self.em.emit("error", **payload)

    def item(self, wf, state, outputs=0, error=None, reason=None):
        status = state if state != "started" else "running"
        self.items[wf] = {"wf": wf, "status": status, "outputs": outputs, "error": error}
        fields = {"wf": wf, "of": self.manifest_total, "state": state, "outputs": outputs}
        if error:
            fields["error"] = error
        if reason:
            fields["reason"] = reason
        self.em.emit("item", **fields)

    def output(self, path_rel, size, kind, **fields):
        self.outputs += 1
        self.output_bytes += size
        self.em.emit("output", **fields, path=path_rel, bytes=size, kind=kind)

    def check_cancel(self):
        if self.cancel.is_set():
            if not self.cancel_reported:
                self.cancel_reported = True
                name = None
                try:
                    name = signal.Signals(self.cancel_signal).name
                except (ValueError, TypeError):
                    pass
                self.phase("cancel_received", "mark", detail=name)
            raise Cancelled()

    # -- final ------------------------------------------------------------------------------------------------
    def final_status(self, exit_code):
        if self.cancel_reported or (self.last_error or {}).get("code") == "cancelled":
            return "cancelled"
        if exit_code == 0:
            return "succeeded"
        if self.manifest_total:
            if any(i["status"] == "succeeded" for i in self.items.values()):
                return "partial"
            return "failed"
        if exit_code == 4 or self.timed_out:
            return "timed_out"
        return "failed"

    def finish(self, exit_code):
        if self.em.final_emitted:
            return None
        items = []
        if self.manifest_total:
            for wf in range(1, self.manifest_total + 1):
                item = self.items.get(wf)
                if item is None:
                    items.append({"wf": wf, "status": "not_run", "outputs": 0, "error": None})
                else:
                    status = item["status"]
                    if status == "running":
                        status = "cancelled" if self.cancel_reported else "failed"
                    items.append({**item, "status": status})
        status = self.final_status(exit_code)
        error = None if status == "succeeded" else (self.last_error or {}).get("code")
        if status == "cancelled":
            error = "cancelled"
        try:
            peak = self.peaks() or {}
        except Exception:  # noqa: BLE001
            peak = {}
        event = self.em.emit("final", status=status, exit_code=exit_code, error=error, outputs=self.outputs,
                             bytes=self.output_bytes, items=items, restarts=self.restarts, peak=peak,
                             durations=self.durations)
        self.em.sync()
        return event


# -- fingerprint helpers (D4) -------------------------------------------------------------------------------
_CUDA_RE = re.compile(r"^cuda\s*(?::\s*[^=]+)?=\s*['\"]([^'\"]+)['\"]", re.M)


def torch_cuda_version():
    """torch.version.cuda read from torch/version.py as text (importing torch here would cost seconds and RAM)."""
    try:
        import importlib.util
        spec = importlib.util.find_spec("torch")
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.submodule_search_locations:
        return None
    for loc in spec.submodule_search_locations:
        try:
            text = (Path(loc) / "version.py").read_text(encoding="utf-8")
        except OSError:
            continue
        m = _CUDA_RE.search(text)
        if m:
            return m.group(1)
    return None


def gpu_driver_info(smi_cmd, run=subprocess.run):
    try:
        res = run(list(smi_cmd) + ["--query-gpu=driver_version,compute_cap", "--format=csv,noheader"],
                  capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None, []
    if res.returncode != 0:
        return None, []
    driver, caps = None, []
    for line in res.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if parts and parts[0]:
            driver = driver or parts[0]
            caps.append(parts[1] if len(parts) > 1 else None)
    return driver, caps


def read_snapshot_info(path):
    """(sha256, profile, image_ref) of the C-5 node snapshot embedded in the image, or Nones."""
    import hashlib
    import json
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None, None, None
    digest = hashlib.sha256(raw).hexdigest()
    try:
        data = json.loads(raw)
    except ValueError:
        return digest, None, None
    if not isinstance(data, dict):
        return digest, None, None
    profile = data.get("profile")
    return digest, profile if isinstance(profile, str) else None, data.get("image_ref")

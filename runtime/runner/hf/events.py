"""C-3 runner event protocol: `[hf] {json}` lines on stdout, mirrored to /output/_halofuse/.

One Emitter per runner process. It owns the event sequence number, the 64 KiB line limit, the render.log copy of
everything the runner prints and events.jsonl. It is thread-safe: telemetry, websocket and output-pump threads emit
through the same instance.
"""
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

PREFIX = "[hf] "
PROTOCOL_VERSION = 1
MAX_LINE_BYTES = 65536
_LONG_STRING = 4096
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_SPOOFABLE = ("[hf]", "[runner]")
# ComfyUI 0.43 colours its log lines even into a pipe (ESC[32m[INFO]ESC[0m ...): strip CSI / 2-byte ESC sequences
# before matching anything in ComfyUI output.
ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|[@-Z\\-_])")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)



def valid_nonce(value) -> bool:
    return isinstance(value, str) and bool(_NONCE_RE.match(value))


def escape_child_line(line: str) -> str:
    """Neutralise ComfyUI/custom-node output that imitates runner lines (TM-008)."""
    if strip_ansi(line).lstrip().startswith(_SPOOFABLE):  # a coloured imitation is caught too
        return "[comfyui] " + line
    return line


def _dump(obj) -> str:
    return PREFIX + json.dumps(obj, separators=(",", ":"), ensure_ascii=True)


def _fits(line: str, max_bytes: int) -> bool:
    return len(line.encode("utf-8")) + 1 <= max_bytes


def _shrink_strings(obj: dict, budget_over: int) -> dict:
    """Truncate long top-level strings, longest first, until roughly `budget_over` bytes are removed."""
    out = dict(obj)
    longs = sorted((k for k, v in out.items() if isinstance(v, str) and len(v) > _LONG_STRING),
                   key=lambda k: len(out[k]), reverse=True)
    for key in longs:
        value = out[key]
        # JSON escaping can make one char cost up to 6 bytes; cut generously.
        keep = max(_LONG_STRING // 2, len(value) - budget_over * 6 - 64)
        keep = min(keep, _LONG_STRING)
        removed = len(value) - keep
        out[key] = value[:keep] + f"…[truncated {removed} chars]"
    return out


def build_event(t: str, fields: dict, *, seq: int, ts: float, nonce=None) -> dict:
    obj = {"v": PROTOCOL_VERSION, "t": t, "ts": round(float(ts), 3), "seq": int(seq)}
    if nonce:
        obj["n"] = nonce
    for key, value in fields.items():
        if key in obj or key == "n":
            continue
        obj[key] = value
    return obj


def fit_event(obj: dict, max_bytes: int = MAX_LINE_BYTES) -> dict:
    """Apply the C-3 §5 shrinking steps; return the object that will be written."""
    if _fits(_dump(obj), max_bytes):
        return obj
    if "data" in obj:
        obj = {k: v for k, v in obj.items() if k != "data"}
        obj["data_dropped"] = True
        if _fits(_dump(obj), max_bytes):
            return obj
    over = len(_dump(obj).encode("utf-8")) + 1 - max_bytes
    shrunk = _shrink_strings(obj, over)
    if _fits(_dump(shrunk), max_bytes):
        return shrunk
    marker = {k: obj[k] for k in ("v", "t", "ts", "seq", "n") if k in obj}
    marker["truncated"] = True
    return marker


def encode_event(t: str, fields: dict, *, seq: int, ts: float, nonce=None, max_bytes: int = MAX_LINE_BYTES) -> str:
    """The full `[hf] …` line (no newline) for one event, size-limited per C-3 §5."""
    return _dump(fit_event(build_event(t, fields, seq=seq, ts=ts, nonce=nonce), max_bytes))


class PreviewThrottle:
    """At most one preview per `min_interval` seconds (never below 2.0 until provider probe P1)."""

    FLOOR = 2.0

    def __init__(self, min_interval: float = 2.0, clock=time.monotonic):
        self.min_interval = max(self.FLOOR, float(min_interval or 0))
        self._clock = clock
        self._last = None
        self._dropped = 0
        self._lock = threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            now = self._clock()
            if self._last is None or now - self._last >= self.min_interval:
                self._last = now
                return True
            self._dropped += 1
            return False

    def take_dropped(self) -> int:
        with self._lock:
            n, self._dropped = self._dropped, 0
            return n


class Emitter:
    def __init__(self, stdout=None, stderr=None, nonce=None, clock=time.time, max_bytes: int = MAX_LINE_BYTES):
        if nonce is not None and not valid_nonce(nonce):
            raise ValueError("HF_EVENT_NONCE must be 16-64 characters of A-Z a-z 0-9 _ -")
        self._stdout = stdout
        self._stderr = stderr
        self.nonce = nonce
        self._clock = clock
        self.max_bytes = max_bytes
        self._lock = threading.RLock()
        self._seq = 0
        self._final = False
        self._log = None
        self._jsonl = None
        self._pending = []  # ("line", text) | ("event", obj, line) until attach_dir()
        self.dir = None

    # -- streams -----------------------------------------------------------------------------------------------
    def _stream(self, name):
        if name == "stderr":
            return self._stderr if self._stderr is not None else sys.stderr
        return self._stdout if self._stdout is not None else sys.stdout

    def _write_stream(self, name, text):
        stream = self._stream(name)
        try:
            stream.write(text + "\n")
            stream.flush()
        except (OSError, ValueError):
            pass  # a closed stdout must never kill the runner

    def _write_files(self, text, event=None):
        if self._log is None:
            self._pending.append((text, event))
            return
        try:
            self._log.write(text + "\n")
            if event is not None:
                self._jsonl.write(json.dumps(event, separators=(",", ":"), ensure_ascii=True) + "\n")
        except (OSError, ValueError):
            pass

    # -- public ------------------------------------------------------------------------------------------------
    @property
    def final_emitted(self) -> bool:
        return self._final

    def line(self, text: str, stream: str = "stdout") -> None:
        with self._lock:
            self._write_stream(stream, text)
            self._write_files(text)

    def emit(self, t: str, **fields):
        with self._lock:
            if t == "final":
                if self._final:
                    return None
                self._final = True
            self._seq += 1
            obj = fit_event(build_event(t, fields, seq=self._seq, ts=self._clock(), nonce=self.nonce), self.max_bytes)
            text = _dump(obj)
            self._write_stream("stdout", text)
            log_text = text
            if isinstance(obj.get("data"), str):
                # render.log is delivered with the outputs: never a second copy of the frame bytes (C-3 section 11)
                log_text = _dump({k: (f"<omitted {len(v)} bytes>" if k == "data" else v) for k, v in obj.items()})
            self._write_files(log_text, {k: v for k, v in obj.items() if k != "data"})
            return obj

    def attach_dir(self, path) -> bool:
        path = Path(path)
        with self._lock:
            try:
                path.mkdir(parents=True, exist_ok=True)
                log = open(path / "render.log", "a", encoding="utf-8", buffering=1)
                try:
                    jsonl = open(path / "events.jsonl", "a", encoding="utf-8", buffering=1)
                except OSError:
                    log.close()
                    raise
            except OSError:
                return False
            self._log, self._jsonl, self.dir = log, jsonl, path
            pending, self._pending = self._pending, []
            for text, event in pending:
                self._write_files(text, event)
            return True

    def close(self, fsync: bool = True) -> None:
        with self._lock:
            for handle in (self._log, self._jsonl):
                if handle is None:
                    continue
                try:
                    handle.flush()
                    if fsync:
                        os.fsync(handle.fileno())
                    handle.close()
                except (OSError, ValueError):
                    pass
            self._log = self._jsonl = None

    def sync(self) -> None:
        with self._lock:
            for handle in (self._log, self._jsonl):
                if handle is not None:
                    try:
                        handle.flush()
                        os.fsync(handle.fileno())
                    except (OSError, ValueError):
                        pass


_current = None
_current_lock = threading.Lock()


def install(emitter: Emitter) -> Emitter:
    global _current
    with _current_lock:
        _current = emitter
    return emitter


def get() -> Emitter:
    global _current
    with _current_lock:
        if _current is None:
            _current = Emitter()
        return _current

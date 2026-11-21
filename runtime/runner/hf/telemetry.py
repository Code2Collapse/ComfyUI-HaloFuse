"""F5: resource telemetry sampled every ~10 s (nvidia-smi, /proc/meminfo, cgroup v2 memory files, disk usage).

Every source is optional: a missing source leaves its key out of the sample, it is never estimated. The cgroup
`memory.events` oom_kill counter is also what turns "ComfyUI got SIGKILL" into "host RAM exhausted" (F3).
"""
import shutil
import subprocess
import threading
from pathlib import Path

SMI_QUERY = ["--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
             "--format=csv,noheader,nounits"]
DEFAULT_INTERVAL = 10.0
MIN_INTERVAL = 2.0


def interval_from_env(raw) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL
    return max(MIN_INTERVAL, value)


def _num(text, cast):
    text = text.strip()
    if not text or text.startswith("[") or text.upper() in ("N/A", "NOT SUPPORTED"):
        return None
    try:
        return cast(text)
    except ValueError:
        return None


def parse_smi(text) -> list:
    gpus = []
    for line in (text or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue
        index = _num(parts[0], int)
        if index is None:
            continue
        gpu = {"index": index, "name": parts[1]}
        for key, pos, cast in (("util_pct", 2, int), ("mem_used_mb", 3, int), ("mem_total_mb", 4, int),
                               ("temp_c", 5, int), ("power_w", 6, float)):
            if pos < len(parts):
                value = _num(parts[pos], cast)
                if value is not None:
                    gpu[key] = value
        gpus.append(gpu)
    return gpus


def parse_meminfo(text) -> dict:
    values = {}
    for line in (text or "").splitlines():
        if ":" not in line:
            continue
        key, rest = line.split(":", 1)
        fields = rest.split()
        if fields and fields[0].isdigit():
            values[key.strip()] = int(fields[0])  # kB
    return values


def read_memory_events(text) -> dict:
    out = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            out[parts[0]] = int(parts[1])
    return out


def _read(path: Path):
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


class Sampler:
    def __init__(self, proc_root=Path("/proc"), cgroup_root=Path("/sys/fs/cgroup"), smi_cmd=("nvidia-smi",),
                 disk_paths=None, run=subprocess.run):
        self.proc_root = Path(proc_root)
        self.cgroup_root = Path(cgroup_root)
        self.smi_cmd = list(smi_cmd)
        self.disk_paths = dict(disk_paths or {})
        self._run = run
        self._lock = threading.Lock()
        self._peak = {"gpu_mem_used_mb": None, "gpu_util_pct": None, "ram_used_mb": None, "cgroup_peak_mb": None}
        self._smi_broken = False

    def _gpus(self):
        if self._smi_broken:
            return None
        try:
            res = self._run(self.smi_cmd + SMI_QUERY, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            self._smi_broken = True  # not installed: stop trying every 10 s
            return None
        if res.returncode != 0:
            return None
        return parse_smi(res.stdout) or None

    def _ram(self):
        info = parse_meminfo(_read(self.proc_root / "meminfo"))
        if "MemTotal" not in info or "MemAvailable" not in info:
            return None
        return {"total_mb": info["MemTotal"] // 1024, "available_mb": info["MemAvailable"] // 1024}

    def _cgroup(self):
        out = {}
        for key, name in (("current_mb", "memory.current"), ("max_mb", "memory.max"), ("peak_mb", "memory.peak")):
            raw = _read(self.cgroup_root / name)
            if raw is not None and raw.strip().isdigit():
                out[key] = int(raw.strip()) // 2**20
        count = self.oom_kill_count()
        if count is not None:
            out["oom_kill"] = count
        return out or None

    def _disk(self):
        out = {}
        for label, path in self.disk_paths.items():
            try:
                usage = shutil.disk_usage(path)
            except OSError:
                continue
            out[label] = {"free_mb": usage.free // 2**20, "total_mb": usage.total // 2**20}
        return out or None

    def oom_kill_count(self):
        raw = _read(self.cgroup_root / "memory.events")
        if raw is None:
            return None
        return read_memory_events(raw).get("oom_kill")

    def sample(self) -> dict:
        sample = {}
        for key, getter in (("gpu", self._gpus), ("ram", self._ram), ("cgroup", self._cgroup), ("disk", self._disk)):
            try:
                value = getter()
            except Exception:  # noqa: BLE001 - one broken source must not hide the others
                value = None
            if value:
                sample[key] = value
        self._update_peaks(sample)
        return sample

    def _update_peaks(self, sample):
        def bump(key, value):
            if value is not None and (self._peak[key] is None or value > self._peak[key]):
                self._peak[key] = value

        with self._lock:
            for gpu in sample.get("gpu", []):
                bump("gpu_mem_used_mb", gpu.get("mem_used_mb"))
                bump("gpu_util_pct", gpu.get("util_pct"))
            ram = sample.get("ram")
            if ram:
                bump("ram_used_mb", ram["total_mb"] - ram["available_mb"])
            cg = sample.get("cgroup") or {}
            bump("cgroup_peak_mb", cg.get("peak_mb", cg.get("current_mb")))

    def peaks(self) -> dict:
        with self._lock:
            return dict(self._peak)


class TelemetryThread:
    def __init__(self, sampler, emit, interval=DEFAULT_INTERVAL, _min_interval=MIN_INTERVAL):
        self.sampler = sampler
        self._emit = emit
        self.interval = max(float(interval), _min_interval)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="hf-telemetry", daemon=True)

    def _loop(self):
        while not self._stop.is_set():
            try:
                payload = self.sampler.sample()
                if payload and not self._stop.is_set():
                    self._emit(payload)
            except Exception:  # noqa: BLE001 - telemetry must never break a render
                pass
            self._stop.wait(self.interval)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=15)

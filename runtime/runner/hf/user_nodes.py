"""The artist's own custom node packs, installed at job start before ComfyUI boots (owner 2026-11-23, Level 1).

The bridge sends the artist's list as HF_USER_NODES: a JSON list of pinned entries
    {"source": "github", "repo": "owner/repo", "commit": "<40 hex>"}  or
    {"source": "registry", "id": "<registry id>", "version": "<version>"}
(optional "folder"). The list is untrusted input: every field is checked again here.

Per pack: download the pinned archive (GitHub codeload zip at the commit, or the Comfy Registry version's
downloadUrl; no git), extract it safely, pip-install its requirements.txt under a constraints file that freezes
every installed package (torch, numpy, ... can never change), and report. A pack that fails is moved to
custom_nodes_disabled/ and named in the job log; the job always continues. After ComfyUI started, a user pack that
ComfyUI reports as IMPORT FAILED is moved aside the same way.

Limits: https to GitHub / the Comfy Registry only (redirects included), MAX_PACKS packs, archive / unpacked size and
file count caps, per-pack pip timeout and a total time budget. Nothing goes through a shell.
HF_USER_NODES_CACHE (optional, unset by default: the runtime has no writable persistent volume) caches archives
and pip wheels keyed by commit / version.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

CAPABILITY = "user_nodes/1"      # export_node_snapshot.RUNNER_CAPABILITIES carries it into the image's snapshot
ENV = "HF_USER_NODES"
CACHE_ENV = "HF_USER_NODES_CACHE"
MAX_PACKS = 20
MAX_ENV_BYTES = 16384
MAX_ARCHIVE_BYTES = 200 * 2**20
MAX_EXTRACTED_BYTES = 2**30
MAX_FILES = 20000
META_BYTES = 2**20
DOWNLOAD_TIMEOUT = 120
PIP_TIMEOUT = 600
TOTAL_BUDGET = 1200
ALLOWED_HOSTS = frozenset({"api.github.com", "github.com", "codeload.github.com", "api.comfy.org", "cdn.comfy.org"})
# Same set as runtime/scripts/install_nodes.py REFUSED_REPO_SHA256 (SHA-256 of lowercase owner/repo; the names are
# deliberately not written in public files).
REFUSED_REPO_SHA256 = frozenset({"cd86c77eade6596dffd7d42838ae75189b4ec746dcbf4cde1f5d0d7bb85cabc3"})
# A batch container must never install or update code by itself.
REFUSED_NAMES = frozenset({"comfyui-manager"})
REGISTRY_VERSION_API = "https://api.comfy.org/nodes/{id}/versions/{version}"
GITHUB_ZIP = "https://codeload.github.com/{repo}/zip/{commit}"

_REPO_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]{0,99})/([A-Za-z0-9][A-Za-z0-9._-]{0,99})$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}$")
_FREEZE_RE = re.compile(r"^[A-Za-z0-9_.\-]+==[^\s;]+$")
_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|[@-Z\\-_])")
_IMPORT_RE = re.compile(r"^\s*(?:\[[A-Z]+\]\s*)?[\d.]+ seconds( \(IMPORT FAILED\))?: (.+?)\s*$")
MAX_DETAIL = 300


class UserNodeError(RuntimeError):
    """One pack could not be used (the message is shown in the job log)."""


@dataclass(frozen=True)
class Pack:
    source: str      # "github" | "registry"
    name: str        # owner/repo or the registry id
    pin: str         # commit or registry version
    folder: str      # folder under custom_nodes


def _short(text) -> str:
    t = re.sub(r"([A-Za-z]:)?([\\/][^\s\\/:'\"]+)+[\\/]", "", str(text))  # no absolute paths in the job log
    return t.strip()[-MAX_DETAIL:]


def _refused(name: str) -> bool:
    return (hashlib.sha256(name.lower().encode("utf-8")).hexdigest() in REFUSED_REPO_SHA256
            or name.lower().rsplit("/", 1)[-1] in REFUSED_NAMES)


def _folder(value: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value) or ".." in value:
        raise UserNodeError("the folder name is not valid")
    return value


def _entry(e) -> Pack:
    if not isinstance(e, dict):
        raise UserNodeError("not an object")
    source = e.get("source")
    if source == "github":
        repo, commit = e.get("repo"), e.get("commit")
        m = _REPO_RE.fullmatch(repo) if isinstance(repo, str) else None
        if not m or ".." in repo:
            raise UserNodeError("the repository must be owner/repo")
        if not isinstance(commit, str) or not _SHA_RE.fullmatch(commit):
            raise UserNodeError(f"{repo}: needs an exact 40-character commit")
        if _refused(repo):
            raise UserNodeError(f"{repo} cannot be installed in a HaloFuse job")
        return Pack("github", repo, commit, _folder(e.get("folder") or m.group(2)))
    if source == "registry":
        rid, version = e.get("id"), e.get("version")
        if not isinstance(rid, str) or not _ID_RE.fullmatch(rid) or ".." in rid:
            raise UserNodeError("the registry id is not valid")
        if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
            raise UserNodeError(f"{rid}: needs an exact registry version")
        if _refused(rid):
            raise UserNodeError(f"{rid} cannot be installed in a HaloFuse job")
        return Pack("registry", rid, version, _folder(e.get("folder") or rid))
    raise UserNodeError("source must be github or registry")


def parse(text) -> tuple[list, list]:
    """(packs, problems) from the HF_USER_NODES value. A bad entry is a problem, never an exception."""
    if not text or not str(text).strip():
        return [], []
    if len(text) > MAX_ENV_BYTES:
        return [], [f"the custom node list is longer than {MAX_ENV_BYTES} bytes; it was ignored"]
    try:
        data = json.loads(text)
    except ValueError:
        return [], ["the custom node list is not valid JSON; it was ignored"]
    if not isinstance(data, list):
        return [], ["the custom node list is not a list; it was ignored"]
    packs, problems, seen = [], [], set()
    for i, e in enumerate(data):
        if len(packs) >= MAX_PACKS:
            problems.append(f"at most {MAX_PACKS} packs are installed; {len(data) - i} more were ignored")
            break
        try:
            p = _entry(e)
        except UserNodeError as exc:
            problems.append(f"entry {i + 1}: {exc}")
            continue
        key = (p.source, p.name.lower())
        if key not in seen:
            seen.add(key)
            packs.append(p)
    return packs, problems


# ---- downloads ---------------------------------------------------------------------------------------------

def allowed_url(url) -> bool:
    try:
        u = urllib.parse.urlsplit(str(url))
        port = u.port
    except ValueError:
        return False
    return (u.scheme == "https" and (u.hostname or "") in ALLOWED_HOSTS and u.username is None
            and u.password is None and port in (None, 443))


class _CheckedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed_url(newurl):
            raise UserNodeError("the download was redirected to a host that is not allowed")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def https_get(url: str, max_bytes: int, timeout: float) -> bytes:
    """GET over https from an allowed host only, at most max_bytes."""
    if not allowed_url(url):
        raise UserNodeError("refusing a download from a host that is not allowed")
    opener = urllib.request.build_opener(_CheckedRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": "halofuse-runner", "Accept": "*/*"})
    with opener.open(req, timeout=timeout) as resp:
        length = resp.headers.get("Content-Length")
        if length and length.isdigit() and int(length) > max_bytes:
            raise UserNodeError(f"the archive is larger than {max_bytes // 2**20} MiB")
        chunks, total = [], 0
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise UserNodeError(f"the archive is larger than {max_bytes // 2**20} MiB")
            chunks.append(chunk)
    return b"".join(chunks)


def safe_extract(blob: bytes, dest: Path) -> None:
    """Extract a zip into dest (one wrapping top folder stripped). Refuses absolute paths, '..', drive letters,
    symbolic links, and archives above the unpacked size / file count limits (sizes are counted while writing)."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile:
        raise UserNodeError("the archive is not a valid zip file") from None
    with zf:
        names, files, declared = [], 0, 0
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            p = PurePosixPath(name)
            if name.startswith("/") or ".." in p.parts or re.match(r"^[A-Za-z]:", name):
                raise UserNodeError("the archive contains an unsafe path")
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise UserNodeError("the archive contains a symbolic link")
            if not info.is_dir():
                files += 1
                declared += info.file_size
            names.append((info, p))
        if files > MAX_FILES:
            raise UserNodeError(f"the pack has more than {MAX_FILES} files")
        if declared > MAX_EXTRACTED_BYTES:
            raise UserNodeError(f"the pack unpacks larger than the {MAX_EXTRACTED_BYTES // 2**20} MiB limit")
        tops = {p.parts[0] for _, p in names if p.parts}
        strip = len(tops) == 1 and all(len(p.parts) > 1 or info.is_dir() for info, p in names)
        dest = Path(dest)
        dest.mkdir(parents=True)
        budget = MAX_EXTRACTED_BYTES
        for info, p in names:
            parts = p.parts[1:] if strip else p.parts
            if not parts or info.is_dir():
                continue
            target = dest.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                while True:
                    chunk = src.read(1 << 16)
                    if not chunk:
                        break
                    budget -= len(chunk)
                    if budget < 0:
                        raise UserNodeError(
                            f"the pack unpacks larger than the {MAX_EXTRACTED_BYTES // 2**20} MiB limit")
                    out.write(chunk)


def run_pip(argv, timeout, env, stop=None):
    """(exit code, stdout, stderr) of a pip command; an argv list, never a shell. Killed after *timeout* seconds or
    as soon as *stop()* is true (a stopped job does not wait for an install)."""
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                errors="replace", env=env)
    except OSError as exc:
        return 127, "", f"pip could not be started ({exc})"
    deadline = time.monotonic() + timeout
    while True:
        try:
            out, err = proc.communicate(timeout=0.5)
            return proc.returncode, out, err
        except subprocess.TimeoutExpired:
            stopped = bool(stop and stop())
            if stopped or time.monotonic() > deadline:
                proc.kill()
                out, err = proc.communicate()
                why = "the job was stopped" if stopped else f"pip did not finish within {int(timeout)} s"
                return 124, out, (err or "") + chr(10) + why


def _cache_key(p: Pack) -> str:
    return f"github-{p.name.replace('/', '__')}-{p.pin}" if p.source == "github" else f"registry-{p.name}-{p.pin}"


def image_packs(snapshot_path) -> set:
    """Lowercase repos and folders of the packs the image already has (its node snapshot); empty when unreadable."""
    try:
        data = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    out = set()
    for pack in data.get("packs") or [] if isinstance(data, dict) else []:
        if isinstance(pack, dict) and pack.get("status") == "loaded":
            for key in ("repo", "folder"):
                if isinstance(pack.get(key), str):
                    out.add(pack[key].lower())
    return out


def missing_classes(workflows, object_info) -> list:
    """The workflows' node classes that ComfyUI does not have (after the user packs loaded)."""
    if not isinstance(object_info, dict):
        return []
    wanted = set()
    for wf in workflows or []:
        for node in (wf or {}).values() if isinstance(wf, dict) else []:
            if isinstance(node, dict) and isinstance(node.get("class_type"), str):
                wanted.add(node["class_type"])
    return sorted(wanted - set(object_info))


# ---- installer ---------------------------------------------------------------------------------------------

class UserNodes:
    def __init__(self, custom_nodes, *, python=None, opener=https_get, pip=None, cache=None, log=print,
                 clock=time.monotonic, existing=(), stop=None):
        self.custom_nodes = Path(custom_nodes)
        self.disabled_dir = self.custom_nodes.parent / "custom_nodes_disabled"
        self.python = python or sys.executable
        self.stop = stop or (lambda: False)      # true once the job received a stop signal
        self.opener, self.log, self.clock = opener, log, clock
        self.pip = pip or (lambda argv, timeout, env: run_pip(argv, timeout, env, stop=self.stop))
        self.cache = Path(cache) if cache else None
        self.existing = {str(e).lower() for e in existing}
        self.installed = {}            # folder (lowercase) -> result record of a pack now in custom_nodes
        self._import_failed = set()
        self._work = None
        env = {k: v for k, v in os.environ.items() if k != "HF_EVENT_NONCE"}  # pack code must not sign [hf] events
        env.update(PIP_NO_INPUT="1", PIP_DISABLE_PIP_VERSION_CHECK="1")
        self.env = env

    # -- install --
    def install(self, packs) -> list:
        results = []
        if not packs:
            return results
        self.log(f"custom nodes: installing {len(packs)} pack(s) from your list before ComfyUI starts "
                 "(installed in every job: nothing is cached between jobs)")
        deadline = self.clock() + TOTAL_BUDGET
        for p in packs:
            t0 = self.clock()
            rec = {"name": p.name, "source": p.source, "pin": p.pin, "folder": p.folder, "status": "failed",
                   "detail": "", "seconds": 0.0}
            if self.stop():
                rec.update(status="skipped", detail="the job was stopped")
            elif t0 >= deadline:
                rec.update(status="skipped",
                           detail=f"the {TOTAL_BUDGET // 60}-minute time limit for custom nodes was used up")
            elif p.name.lower() in self.existing or p.folder.lower() in self.existing:
                rec.update(status="skipped", detail="already in the image")
            elif self._folder_taken(p.folder):
                rec.update(status="skipped", detail=f"a pack folder named {p.folder} is already in the image")
            else:
                try:
                    self._install_one(p, rec, deadline)
                    rec["status"] = "installed"
                except UserNodeError as exc:
                    rec["detail"] = str(exc)
                except Exception as exc:  # noqa: BLE001 - one pack never ends the job
                    rec["detail"] = _short(f"{type(exc).__name__}: {exc}")
                if rec["status"] == "failed":
                    self.installed.pop(p.folder.lower(), None)
                    self._move_aside(p.folder)
            rec["seconds"] = round(self.clock() - t0, 1)
            self._report(rec)
            results.append(rec)
        return results

    def _folder_taken(self, folder: str) -> bool:
        try:
            return any(c.name.lower() == folder.lower() for c in self.custom_nodes.iterdir())
        except OSError:
            return False

    def _install_one(self, p: Pack, rec: dict, deadline: float) -> None:
        blob = self._archive(p)
        dest = self.custom_nodes / p.folder
        self.custom_nodes.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.custom_nodes.parent, prefix=".hf-user-node-") as tmp:
            staged = Path(tmp) / p.folder
            safe_extract(blob, staged)
            if not (staged / "__init__.py").is_file():
                raise UserNodeError("not a ComfyUI custom node pack (no __init__.py at its top level)")
            shutil.move(str(staged), str(dest))
        self.installed[p.folder.lower()] = rec
        req = dest / "requirements.txt"
        if req.is_file():
            self._requirements(p, req, deadline)

    def _source_url(self, p: Pack) -> str:
        if p.source == "github":
            return GITHUB_ZIP.format(repo=p.name, commit=p.pin)
        meta_url = REGISTRY_VERSION_API.format(id=p.name, version=p.pin)
        try:
            meta = json.loads(self.opener(meta_url, META_BYTES, 30))
        except UserNodeError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise UserNodeError(f"registry lookup failed ({_short(exc)})") from None
        url = meta.get("downloadUrl") if isinstance(meta, dict) else None
        if not url:
            raise UserNodeError(f"the registry has no download for version {p.pin}")
        if not allowed_url(url):
            raise UserNodeError("the registry gave a download address on a host that is not allowed")
        return url

    def _archive(self, p: Pack) -> bytes:
        cached = self.cache / "archives" / f"{_cache_key(p)}.zip" if self.cache else None
        if cached is not None and cached.is_file() and cached.stat().st_size <= MAX_ARCHIVE_BYTES:
            return cached.read_bytes()
        url = self._source_url(p)
        if not allowed_url(url):
            raise UserNodeError("refusing a download from a host that is not allowed")
        try:
            blob = self.opener(url, MAX_ARCHIVE_BYTES, DOWNLOAD_TIMEOUT)
        except UserNodeError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise UserNodeError(f"download failed ({_short(exc)})") from None
        if cached is not None:
            try:
                cached.parent.mkdir(parents=True, exist_ok=True)
                part = cached.with_suffix(".part")
                part.write_bytes(blob)
                os.replace(part, cached)
            except OSError:
                pass
        return blob

    def _requirements(self, p: Pack, req: Path, deadline: float) -> None:
        keep, dropped = [], 0
        for raw in req.read_text(encoding="utf-8", errors="replace").splitlines():
            line = re.split(r"\s+#", raw, maxsplit=1)[0].strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith(("-", ".", "/", "~")) or "://" in line or "\\" in line or " @ " in line:
                dropped += 1    # options (-r, -e, --index-url ...), direct URLs and paths are not allowed
                continue
            keep.append(line)
        if dropped:
            self.log(f"custom nodes: {p.name}: {dropped} requirement line(s) ignored "
                     "(options, URLs and paths are not allowed)")
        if not keep:
            return
        if self._work is None:
            self._work = Path(tempfile.mkdtemp(prefix="hf-user-nodes-"))
        code, out, err = self.pip([self.python, "-m", "pip", "freeze", "--all"], 120, self.env)
        if code != 0:
            raise UserNodeError("could not list the installed Python packages (pip freeze failed)")
        frozen = [l.strip() for l in out.splitlines() if _FREEZE_RE.match(l.strip())]
        constraints = self._work / "constraints.txt"
        constraints.write_text("\n".join(frozen) + "\n", encoding="utf-8")
        reqs = self._work / f"{p.folder}-requirements.txt"
        reqs.write_text("\n".join(keep) + "\n", encoding="utf-8")
        argv = [self.python, "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
                "--progress-bar", "off", "-r", str(reqs), "-c", str(constraints)]
        if self.cache is not None:
            argv += ["--cache-dir", str(self.cache / "pip")]
        timeout = max(30.0, min(float(PIP_TIMEOUT), deadline - self.clock()))
        code, out, err = self.pip(argv, timeout, self.env)
        if code != 0:
            lines = [l for l in (err or out or "").splitlines() if l.strip()]
            raise UserNodeError("its Python requirements could not be installed without changing the image's "
                                f"packages: {_short(lines[-1] if lines else f'pip exit {code}')}")

    def _move_aside(self, folder: str) -> None:
        src = self.custom_nodes / folder
        if not src.exists():
            return
        try:
            self.disabled_dir.mkdir(parents=True, exist_ok=True)
            target, n = self.disabled_dir / folder, 1
            while target.exists():
                n += 1
                target = self.disabled_dir / f"{folder}.{n}"
            shutil.move(str(src), str(target))
        except OSError as exc:
            self.log(f"custom nodes: could not move {folder} aside ({_short(exc)})")

    def _report(self, rec: dict) -> None:
        pin = rec["pin"][:12]
        if rec["status"] == "installed":
            self.log(f"custom nodes: {rec['name']} @ {pin} installed ({rec['seconds']} s)")
        elif rec["status"] == "skipped":
            self.log(f"custom nodes: {rec['name']} skipped: {rec['detail']}")
        else:
            self.log(f"custom nodes: {rec['name']} @ {pin} FAILED: {rec['detail']}; moved aside, the job continues "
                     "without it")

    # -- after ComfyUI started --
    def on_comfy_line(self, text: str) -> None:
        """Feed every ComfyUI output line; remembers user packs that ComfyUI reports as IMPORT FAILED."""
        if not self.installed:
            return
        m = _IMPORT_RE.match(_ANSI_RE.sub("", str(text)))
        if m and m.group(1):
            folder = re.split(r"[\\/]", m.group(2).rstrip("\\/"))[-1].lower()
            if folder in self.installed:
                self._import_failed.add(folder)

    def check_imports(self) -> list:
        """Move the user packs ComfyUI could not import aside; returns their result records."""
        failed = []
        for key in sorted(self._import_failed):
            rec = self.installed.pop(key, None)
            if rec is None:
                continue
            rec.update(status="failed", detail="ComfyUI could not import it (IMPORT FAILED)")
            self._move_aside(rec["folder"])
            self.log(f"custom nodes: {rec['name']} did not import in ComfyUI (IMPORT FAILED, see the ComfyUI lines "
                     "above); moved aside, its nodes are missing in this job")
            failed.append(rec)
        self._import_failed.clear()
        return failed

    def disable_all(self, reason: str) -> list:
        """Move every installed user pack aside (ComfyUI did not start with them). Returns their names."""
        names = []
        for key in sorted(self.installed):
            rec = self.installed[key]
            rec.update(status="failed", detail=reason)
            self._move_aside(rec["folder"])
            names.append(rec["name"])
        self.installed.clear()
        if names:
            self.log(f"custom nodes: {reason}; moved aside: {', '.join(names)}")
        return names

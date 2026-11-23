"""Get missing models automatically (owner 2026-11-23): a model the workflow needs that is neither on this computer nor
in the cloud model library is looked up at a public source, downloaded into the right ComfyUI model folder on this
computer, and then uploaded to the cloud model library by the existing upload path (model_sync, auto_upload_models).

Source order - all public and keyless; a request carries only the model's FILE NAME (never a folder, node, path,
account detail or credential):
  1. the workflow itself: node ``properties.models`` [{name, url, directory}] (official templates; the panel sends them
     as ``model_links``) and URL widgets in the API graph;
  2. ComfyUI-Manager's public model list (model-list.json, cached on this computer for a day);
  3. Hugging Face, EXACT file name only: the file lists of known organisations, then the public model search. Several
     different files with that name are listed for the artist to choose from; nothing is guessed.
  Civitai is not searched (HaloFuse has no Civitai client): a pasted Civitai download link works.

Every model that stays missing names itself and offers a fix (paste a link, pick a candidate, put the file in its
folder). Downloads respect the C2C "Never download models" switch (env C2C_NO_DOWNLOADS, setting
c2c.models.neverDownload - read here the same way CustomNodePacks nodes/_download_policy.py does, without importing
it), check free disk space first, resume a partial file after a crash, and verify the size and SHA-256 when the
source states them. Models inside an H2Vault vault are never searched, logged or shown: the vault provisions its own.
Errors that reach the browser carry no URL (the C-4 rule): sources are shown by label ("Hugging Face: org/repo").
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .workflow_analysis import MODEL_EXTS

_log = logging.getLogger(__name__)

MANAGER_LIST_URL = "https://raw.githubusercontent.com/ltdrdata/ComfyUI-Manager/main/model-list.json"
HF_HOST = "huggingface.co"
CIVITAI_HOSTS = ("civitai.com", "civitai.red")
# Organisations whose whole file lists are read (one request each, cached a day): the ComfyUI repackaged models and
# the most used ComfyUI wrapper models. Measured 2026-11-23: 99 repos / 936 files and 78 repos / 988 files.
LISTED_ORGS = ("Comfy-Org", "Kijai")
# A file from one of these is downloaded without asking; a copy found only in somebody else's upload asks first.
KNOWN_ORGS = frozenset(LISTED_ORGS + (
    "comfyanonymous", "city96", "QuantStack", "lllyasviel", "stabilityai", "black-forest-labs", "Wan-AI",
    "Lightricks", "Kwai-Kolors", "InstantX", "xinsir", "Qwen", "tencent", "ByteDance", "lodestones"))
USER_AGENT = "HaloFuse"
LIST_TTL_S = 24 * 3600
LOOKUP_TTL_S = 3600
LOOKUP_BUDGET_S = 25.0            # one Workflow Check spends at most this long searching sources
LOOKUP_TIMEOUT_S = 15.0
DOWNLOAD_TIMEOUT_S = 60.0
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_PROBES = 6                    # candidate files probed per model (HEAD requests)
MAX_LINKS = 1000
DISK_MARGIN = 1024**3             # keep 1 GiB free beyond the file
CHUNK = 1 << 20
PART_SUFFIX = ".halofuse-part"    # not a model extension: ComfyUI never lists a partial file in a loader
DEFAULT_ASK_ABOVE_GB = 20
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_HF_PATH_RE = re.compile(r"^/([^/]+)/([^/]+)/(?:resolve|blob)/([^/]+)/(.+)$")
_CIVITAI_PATH_RE = re.compile(r"^/api/download/models/\d+/?$")


class FetchError(Exception):
    """A refusal or failure in plain words for the artist (never a URL)."""


class LookupUnavailable(Exception):
    """A source could not be read (offline, rate limited, unexpected answer)."""


# ---- HTTP (the one seam tests replace) ---------------------------------------------------------------------------

class Response:
    def __init__(self, status: int, headers=None, stream=None) -> None:
        self.status = int(status)
        self.headers = {str(k).lower(): str(v) for k, v in dict(headers or {}).items()}
        self._stream = stream

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())

    def read(self, n: int = -1) -> bytes:
        return self._stream.read(n) if self._stream is not None else b""

    def close(self) -> None:
        close = getattr(self._stream, "close", None)
        if close:
            try:
                close()
            except Exception:  # noqa: BLE001
                pass


def url_problem(url: str, *, resolve: bool = True) -> str | None:
    """Why *url* may not be fetched, or None: https only, port 443, no user name or password in it, a host name (not an
    IP literal) that resolves to public addresses only (no loopback, private or link-local target)."""
    try:
        u = urllib.parse.urlsplit(str(url))
        port = u.port
    except ValueError:
        return "it is not a valid address"
    host = (u.hostname or "").lower()
    if u.scheme != "https":
        return "only https links are accepted"
    if not host or u.username is not None or u.password is not None or port not in (None, 443):
        return "the link has no plain host name (no user name, password or port allowed)"
    try:
        ipaddress.ip_address(host)
        return "a link to a bare IP address is not accepted"
    except ValueError:
        pass
    if resolve:
        try:
            infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        except OSError:
            return "its host name could not be resolved (no connection?)"
        for info in infos:
            try:
                ip = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])
            except ValueError:
                return "its host name resolves to an unreadable address"
            if not ip.is_global or ip.is_multicast:
                return "its host name points into a private network"
    return None


class _Blocked(Exception):
    pass


class _CheckedRedirect(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to another public https address. The Hugging Face token is an unredirected header, so
    urllib never forwards it to the storage host a download redirects to."""
    max_redirections = 8

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        problem = url_problem(newurl)
        if problem:
            raise _Blocked(f"a redirect was refused: {problem}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # the 3xx answer itself is returned (Hugging Face puts size and SHA-256 on it)


def _request(url: str, *, method: str = "GET", headers: dict | None = None, token: str | None = None,
             follow: bool = True, timeout: float = LOOKUP_TIMEOUT_S) -> Response:
    """One public HTTPS request. Raises LookupUnavailable when the address is refused or cannot be reached; any HTTP
    status (also 4xx/5xx/3xx) comes back as a Response."""
    problem = url_problem(url)
    if problem:
        raise LookupUnavailable(problem)
    req = urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT, **(headers or {})})
    if token:
        req.add_unredirected_header("Authorization", f"Bearer {token}")
    opener = urllib.request.build_opener(_CheckedRedirect if follow else _NoRedirect)
    try:
        resp = opener.open(req, timeout=timeout)
        return Response(resp.status, resp.headers.items(), resp)
    except urllib.error.HTTPError as exc:
        return Response(exc.code, (exc.headers or {}).items() if exc.headers else {}, exc)
    except _Blocked as exc:
        raise LookupUnavailable(str(exc)) from None
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        raise LookupUnavailable(type(exc).__name__) from None


def _get_json(url: str):
    resp = _request(url, headers={"Accept": "application/json"})
    try:
        if resp.status == 429:
            raise LookupUnavailable("rate limited")
        if resp.status != 200:
            raise LookupUnavailable(f"HTTP {resp.status}")
        body = resp.read(MAX_JSON_BYTES + 1)
    finally:
        resp.close()
    if len(body) > MAX_JSON_BYTES:
        raise LookupUnavailable("answer too large")
    try:
        return json.loads(body)
    except ValueError:
        raise LookupUnavailable("unreadable answer") from None


# ---- policy and settings ------------------------------------------------------------------------------------------

def _c2c_saved_never_download() -> bool:
    try:
        import folder_paths  # ComfyUI-provided
        path = Path(folder_paths.get_user_directory()) / "default" / "comfy.settings.json"
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - no ComfyUI / no settings file yet
        return False
    return isinstance(data, dict) and data.get("c2c.models.neverDownload") is True


def downloads_blocked() -> str | None:
    """The C2C "Never download models" switch, read as CustomNodePacks nodes/_download_policy.never_download() reads
    it outside its own registry: C2C_NO_DOWNLOADS wins when set ("1" = never), else the saved setting
    c2c.models.neverDownload. Returns the reason text, or None when downloads are allowed."""
    env = os.environ.get("C2C_NO_DOWNLOADS")
    if env is not None and env != "":
        blocked = env == "1"
        where = "the C2C_NO_DOWNLOADS environment variable"
    else:
        blocked = _c2c_saved_never_download()
        where = "the C2C setting 'Never download models'"
    return f"downloading models is turned off on this computer ({where})" if blocked else None


def ask_above_bytes(settings: dict) -> int | None:
    """Models larger than this ask before they are downloaded; None = never ask (setting 0)."""
    try:
        gb = float(settings.get("download_ask_above_gb", DEFAULT_ASK_ABOVE_GB))
    except (TypeError, ValueError):
        gb = DEFAULT_ASK_ABOVE_GB
    return None if gb <= 0 else int(gb * 1024**3)


def _hf_token() -> str | None:
    """The optional Hugging Face token (gated repositories): HF_TOKEN in the environment, else the system keychain
    entry the panel saves (credentials.py). Never logged, never sent to any host but huggingface.co."""
    try:
        from . import credentials

        return credentials.default_store().hf_token()
    except Exception:  # noqa: BLE001 - no settings folder / keychain: the environment only
        return os.environ.get("HF_TOKEN", "").strip() or None


# ---- sources ------------------------------------------------------------------------------------------------------

@dataclass
class Source:
    kind: str                 # workflow | manager | huggingface | link
    url: str                  # never sent to the browser
    label: str                # shown to the artist; never contains "://"
    size: int | None = None
    sha256: str | None = None
    trusted: bool = True      # False: a copy found only in someone's own upload -> ask first
    gated: bool = False       # Hugging Face: needs a login and an accepted licence
    repo: str | None = None
    downloads: int = 0

    def public(self) -> dict:
        return {"kind": self.kind, "label": self.label, "size": self.size, "verified": bool(self.sha256),
                "trusted": self.trusted}


@dataclass
class Lookup:
    status: str                       # found | none | ambiguous | gated | unavailable
    source: Source | None = None
    candidates: list = field(default_factory=list)
    searched: list = field(default_factory=list)
    unavailable: list = field(default_factory=list)


def _base(name: str) -> str:
    return str(name).replace("\\", "/").rsplit("/", 1)[-1]


def _hf_parts(url: str):
    u = urllib.parse.urlsplit(url)
    if (u.hostname or "").lower() not in (HF_HOST, "www." + HF_HOST):
        return None
    m = _HF_PATH_RE.match(u.path)
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3), urllib.parse.unquote(m.group(4))


def hf_url(repo: str, path: str, revision: str = "main") -> str:
    return f"https://{HF_HOST}/{repo}/resolve/{urllib.parse.quote(revision, safe='')}/{urllib.parse.quote(path)}"


def check_link(url, name: str, *, kind: str = "link") -> Source:
    """Validate a download link for model *name* (a pasted link, a workflow link or a Manager entry). Accepts https
    Hugging Face file links (blob links become resolve links), Civitai download links (/api/download/models/<id>) and
    direct links whose file name is exactly the model's file name. Raises FetchError saying why not."""
    if not isinstance(url, str) or not url.strip() or len(url) > 2048:
        raise FetchError("Paste a download link (https) for this model.")
    url = url.strip()
    problem = url_problem(url, resolve=False)
    if problem:
        raise FetchError(f"This link cannot be used: {problem}.")
    u = urllib.parse.urlsplit(url)
    host = (u.hostname or "").lower()
    want = _base(name)
    whose = {"link": "your link", "workflow": "the workflow's link", "manager": "the ComfyUI-Manager model list"}[kind]
    hf = _hf_parts(url)
    if host in (HF_HOST, "www." + HF_HOST):
        if not hf:
            raise FetchError("This Hugging Face link does not point at one file: open the file on huggingface.co and "
                             "copy its download link (it contains /resolve/ or /blob/).")
        org, repo, rev, path = hf
        if _base(path) != want:
            raise FetchError(f"This link is for '{_base(path)}', but the workflow needs '{want}'.")
        repo_id = f"{org}/{repo}"
        return Source(kind, hf_url(repo_id, path, rev), f"Hugging Face: {repo_id} ({whose})", repo=repo_id)
    if host in CIVITAI_HOSTS or host.endswith(tuple("." + h for h in CIVITAI_HOSTS)):
        if not _CIVITAI_PATH_RE.match(u.path):
            raise FetchError("Use the model's download link from Civitai (it looks like "
                             "civitai.com/api/download/models/<number>).")
        return Source(kind, urllib.parse.urlunsplit(("https", u.netloc, u.path, u.query, "")),
                      f"Civitai ({whose})")
    got = _base(urllib.parse.unquote(u.path))
    if got != want:
        raise FetchError(f"This link is for '{got or 'no file'}', but the workflow needs '{want}': use a link that "
                         "ends in the model's file name, a Hugging Face file link or a Civitai download link.")
    return Source(kind, urllib.parse.urlunsplit(("https", u.netloc, u.path, u.query, "")), f"{host} ({whose})")


def clean_links(raw) -> list[dict]:
    """The panel's model_links (node properties.models of the editor graph): [{name, url, directory}], bounded. Entries
    that are not plain https links are dropped here; check_link decides per model later."""
    out = []
    if not isinstance(raw, list):
        return out
    for entry in raw[:MAX_LINKS]:
        if not isinstance(entry, dict):
            continue
        name, url = entry.get("name"), entry.get("url")
        if isinstance(name, str) and isinstance(url, str) and 0 < len(name) <= 512 and 0 < len(url) <= 2048 \
                and url.startswith("https://"):
            directory = entry.get("directory")
            out.append({"name": name, "url": url, "directory": directory if isinstance(directory, str) else None})
    return out


def links_from_prompt(workflow: dict) -> list[dict]:
    """URL widgets in an API graph that point at a model file (some loaders take a download URL)."""
    out = []
    for node in (workflow or {}).values():
        inputs = node.get("inputs") if isinstance(node, dict) else None
        if not isinstance(inputs, dict) or str(node.get("class_type", "")).startswith("H2Vault"):
            continue
        for value in inputs.values():
            if isinstance(value, str) and value.startswith("https://") and len(value) <= 2048:
                path = urllib.parse.urlsplit(value).path
                if urllib.parse.unquote(path).lower().endswith(MODEL_EXTS):
                    out.append({"name": _base(urllib.parse.unquote(path)), "url": value, "directory": None})
    return out


# ---- cached public lists ------------------------------------------------------------------------------------------

_CACHE_LOCK = threading.Lock()
_MEM: dict[str, tuple[float, object]] = {}


def _cache_path(name: str) -> Path | None:
    try:
        from .config import settings_dir

        return Path(settings_dir()) / name
    except Exception:  # noqa: BLE001 - outside ComfyUI: memory cache only
        return None


def _cached_list(key: str, fetch) -> object:
    """A public list cached in memory and on disk for LIST_TTL_S; a stale copy is used when the source is offline."""
    now = time.time()
    with _CACHE_LOCK:
        hit = _MEM.get(key)
    if hit and now - hit[0] < LIST_TTL_S:
        return hit[1]
    path = _cache_path(f"halofuse_{key}_cache.json")
    disk = None
    if path is not None and path.is_file():
        try:
            disk = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            disk = None
    if isinstance(disk, dict) and now - float(disk.get("fetched", 0)) < LIST_TTL_S:
        with _CACHE_LOCK:
            _MEM[key] = (float(disk["fetched"]), disk.get("data"))
        return disk.get("data")
    try:
        data = fetch()
    except LookupUnavailable:
        if isinstance(disk, dict):
            _log.info("model source list '%s' is offline; using the copy from earlier", key)
            return disk.get("data")
        raise
    with _CACHE_LOCK:
        _MEM[key] = (now, data)
    if path is not None:
        try:
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps({"fetched": now, "data": data}), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass
    return data


def manager_entries() -> dict:
    """{file name: [url, ...]} from ComfyUI-Manager's model-list.json (https URLs only)."""
    def fetch():
        data = _get_json(MANAGER_LIST_URL)
        models = data.get("models") if isinstance(data, dict) else None
        if not isinstance(models, list):
            raise LookupUnavailable("unexpected model list format")
        out: dict[str, list] = {}
        for m in models:
            if isinstance(m, dict) and isinstance(m.get("filename"), str) and isinstance(m.get("url"), str) \
                    and m["url"].startswith("https://"):
                urls = out.setdefault(m["filename"], [])
                if m["url"] not in urls:
                    urls.append(m["url"])
        return out
    return _cached_list("model_list", fetch)


def org_files(org: str) -> dict:
    """{file name: [[repo, path, downloads], ...]} for every public model repo of *org* (one listing request)."""
    def fetch():
        q = urllib.parse.urlencode({"author": org, "limit": 1000, "full": "true"})
        data = _get_json(f"https://{HF_HOST}/api/models?{q}")
        if not isinstance(data, list):
            raise LookupUnavailable("unexpected listing format")
        out: dict[str, list] = {}
        for repo in data:
            if not isinstance(repo, dict) or repo.get("private") or not isinstance(repo.get("id"), str):
                continue
            for s in repo.get("siblings") or []:
                path = s.get("rfilename") if isinstance(s, dict) else None
                if isinstance(path, str) and path.lower().endswith(MODEL_EXTS):
                    out.setdefault(_base(path), []).append([repo["id"], path, int(repo.get("downloads") or 0)])
        return out
    return _cached_list(f"hf_{org.lower()}", fetch)


def _search_terms(base: str) -> list[str]:
    stem = base.rsplit(".", 1)[0]
    terms = [stem]
    shorter = re.split(r"[._-](?=[^._-]*$)", stem)[0]
    if shorter != stem and len(shorter) >= 4:
        terms.append(shorter)
    return terms


def hf_search(base: str) -> list[list]:
    """[[repo, path, downloads]] of public repos holding a file named exactly *base* (search by the file's stem)."""
    found: dict[tuple, list] = {}
    for term in _search_terms(base):
        q = urllib.parse.urlencode({"search": term, "limit": 30, "full": "true", "sort": "downloads"})
        data = _get_json(f"https://{HF_HOST}/api/models?{q}")
        for repo in data if isinstance(data, list) else []:
            if not isinstance(repo, dict) or repo.get("private") or not isinstance(repo.get("id"), str):
                continue
            for s in repo.get("siblings") or []:
                path = s.get("rfilename") if isinstance(s, dict) else None
                if isinstance(path, str) and _base(path) == base:
                    found.setdefault((repo["id"], path), [repo["id"], path, int(repo.get("downloads") or 0)])
        if found:
            break
    return list(found.values())


# ---- probing and choosing -----------------------------------------------------------------------------------------

def probe(src: Source, token: str | None = None) -> Source | None:
    """Fill size / SHA-256 / gated from a HEAD request. None = the source does not have the file (404/410)."""
    hf = _hf_parts(src.url)
    try:
        resp = _request(src.url, method="HEAD", follow=not hf, token=token if hf else None)
        for _ in range(2):
            # A renamed Hugging Face repository answers 307 with a path on huggingface.co itself (measured
            # 2026-11-24: ComfyUI-Manager still lists Comfy-Org/Wan_2.1_ComfyUI_repackaged, now Comfy-Org/Wan-2.1);
            # size and SHA-256 are only on the answer of the new address. The storage redirect (302) has them.
            location = resp.header("location") or ""
            if not (hf and resp.status in (301, 307, 308) and not resp.header("x-linked-size")
                    and location.startswith("/") and _hf_parts(f"https://{HF_HOST}{location}")):
                break
            resp.close()
            src.url = f"https://{HF_HOST}{location}"
            resp = _request(src.url, method="HEAD", follow=False, token=token)
    except LookupUnavailable:
        return src  # unknown size; the download tells
    resp.close()
    if resp.status in (404, 410):
        return None
    if resp.status in (401, 403):
        src.gated = True
        return src
    linked = resp.header("x-linked-size")
    length = linked or (resp.header("content-length") if resp.status == 200 else None)
    try:
        src.size = int(length) if length else src.size
    except ValueError:
        pass
    etag = (resp.header("x-linked-etag") or "").strip().strip('"').lower()
    if hf and _SHA_RE.match(etag):
        src.sha256 = etag
    return src


def _choose(cands: list, token: str | None, deadline: float) -> Lookup | None:
    """Probe up to MAX_PROBES candidates; identical files (same SHA-256, else same size) count once. One file -> found
    (a public copy before a gated one, a known organisation first); several different files -> ambiguous (listed);
    none reachable -> None (try the next source)."""
    groups: dict[str, list] = {}
    for src in cands[:MAX_PROBES]:
        if time.monotonic() > deadline:
            break
        got = probe(src, token)
        if got is None:
            continue
        key = got.sha256 or (f"size:{got.size}" if got.size else f"url:{got.url}")
        groups.setdefault(key, []).append(got)
    if not groups:
        return None
    ranked = []
    for members in groups.values():
        members.sort(key=lambda s: (s.gated, not s.trusted, -s.downloads))
        ranked.append(members[0])
    if len(ranked) == 1:
        best = ranked[0]
        return Lookup("gated" if best.gated else "found", best)
    ranked.sort(key=lambda s: (s.gated, not s.trusted, -s.downloads))
    return Lookup("ambiguous", None, ranked)


def _hf_sources(hits: list) -> list[Source]:
    out = []
    for repo, path, downloads in sorted(hits, key=lambda h: (h[0].split("/")[0] not in KNOWN_ORGS, -h[2])):
        org = repo.split("/")[0]
        out.append(Source("huggingface", hf_url(repo, path), f"Hugging Face: {repo}", repo=repo,
                          trusted=org in KNOWN_ORGS, downloads=downloads))
    return out


_LOOKUP_LOCK = threading.Lock()
_LOOKUPS: dict[tuple, tuple[float, Lookup]] = {}     # (folder, name) -> (time, Lookup) for the public sources
_PASTED: dict[tuple, Source] = {}                    # (folder, name) -> the artist's link or chosen candidate
_CANDIDATES: dict[tuple, list] = {}                  # (folder, name) -> the files listed for the artist to pick
_FOUND: dict[tuple, tuple[float, Source]] = {}       # (folder, name) -> the source the last check found (any tier)


def find_source(folder: str, name: str, links=(), *, token: str | None = None,
                deadline: float | None = None) -> Lookup:
    """The download source for one model, in the documented order (see the module docstring)."""
    result = _find_source(folder, name, links, token=token, deadline=deadline)
    with _LOOKUP_LOCK:
        if result.status == "ambiguous":
            _CANDIDATES[(folder, name)] = list(result.candidates)
        elif result.status == "found":
            _FOUND[(folder, name)] = (time.time(), result.source)   # the download request names folder + name only
    return result


def _find_source(folder, name, links, *, token, deadline) -> Lookup:
    deadline = deadline if deadline is not None else time.monotonic() + LOOKUP_BUDGET_S
    key = (folder, name)
    with _LOOKUP_LOCK:
        chosen = _PASTED.get(key)
    if chosen is not None:
        return Lookup("found", chosen, searched=["your link"])
    base = _base(name)
    searched, unavailable = [], []
    # 1. the workflow's own links
    wl, seen = [], set()
    for entry in links or ():
        if _base(entry.get("name", "")) != base:
            continue
        try:
            src = check_link(entry["url"], name, kind="workflow")
        except FetchError:
            continue
        if src.url not in seen:
            seen.add(src.url)
            wl.append(src)
    searched.append("the workflow's links")
    if wl:
        got = _choose(wl, token, deadline)
        if got is not None:
            got.searched = searched
            return got
    with _LOOKUP_LOCK:
        hit = _LOOKUPS.get(key)
    if hit and time.time() - hit[0] < LOOKUP_TTL_S:
        return hit[1]
    result = _public_lookup(name, token, deadline, searched, unavailable)
    if result.status != "unavailable":
        with _LOOKUP_LOCK:
            _LOOKUPS[key] = (time.time(), result)
    return result


def _public_lookup(name, token, deadline, searched, unavailable) -> Lookup:
    base = _base(name)
    # 2. ComfyUI-Manager's model list
    try:
        urls = manager_entries().get(base) or []
        searched.append("the ComfyUI-Manager model list")
        cands = []
        for url in urls:
            try:
                cands.append(check_link(url, name, kind="manager"))
            except FetchError:
                continue
        if cands:
            got = _choose(cands, token, deadline)
            if got is not None:
                got.searched, got.unavailable = searched, unavailable
                return got
    except LookupUnavailable:
        unavailable.append("the ComfyUI-Manager model list")
    # 3. Hugging Face, exact file name: known organisations, then the public search
    try:
        hits = []
        for org in LISTED_ORGS:
            hits += org_files(org).get(base) or []
        if not hits and time.monotonic() < deadline:
            hits = hf_search(base)
        searched.append("Hugging Face (exact file name)")
        if hits:
            got = _choose(_hf_sources(hits), token, deadline)
            if got is not None:
                got.searched, got.unavailable = searched, unavailable
                return got
    except LookupUnavailable:
        unavailable.append("Hugging Face")
    status = "unavailable" if unavailable and time.monotonic() <= deadline else "none"
    if time.monotonic() > deadline:
        status = "unavailable"
        unavailable.append("the remaining sources (the search took too long)")
    return Lookup(status, None, searched=searched, unavailable=unavailable)


def choose_candidate(folder: str, name: str, index: int) -> Source:
    with _LOOKUP_LOCK:
        cands = list(_CANDIDATES.get((folder, name)) or [])
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(cands):
        raise FetchError(f"{name}: that choice is no longer offered; run the Workflow Check again.")
    src = cands[index]
    with _LOOKUP_LOCK:
        _PASTED[(folder, name)] = src
    return src


def reset_for_tests() -> None:
    with _LOOKUP_LOCK:
        _LOOKUPS.clear()
        _PASTED.clear()
        _CANDIDATES.clear()
        _FOUND.clear()
    with _CACHE_LOCK:
        _MEM.clear()
    with _FETCH_LOCK:
        _FETCHES.clear()


# ---- local target -------------------------------------------------------------------------------------------------

def _safe_parts(name: str) -> list[str] | None:
    parts = str(name).replace("\\", "/").split("/")
    for p in parts:
        if p in ("", ".", "..") or "\x00" in p or ":" in p:
            return None
    return parts if parts[-1].lower().endswith(MODEL_EXTS) else None


def target_path(folder: str, name: str) -> Path | None:
    """models/<folder>/<name> on this computer, with the workflow value's sub-folders. The folder is ComfyUI's own
    path for that model folder (models/<folder> when it is one of them), never a path from the browser."""
    parts = _safe_parts(name)
    if parts is None:
        return None
    try:
        import folder_paths  # ComfyUI-provided
    except ImportError:
        return None
    models_dir = getattr(folder_paths, "models_dir", None)
    try:
        dirs = [str(d) for d in folder_paths.get_folder_paths(folder)]
    except Exception:  # noqa: BLE001 - not a folder_paths key (a node-read folder such as models/depthanything)
        dirs = []
    preferred = os.path.join(models_dir, folder) if isinstance(models_dir, str) else None
    norm = [os.path.normcase(os.path.abspath(d)) for d in dirs]
    if preferred and os.path.normcase(os.path.abspath(preferred)) in norm:
        base = preferred
    elif dirs:
        base = dirs[0]
    else:
        base = preferred
    if not base:
        return None
    root = Path(base).resolve()
    target = root.joinpath(*parts).resolve()
    if root not in target.parents:
        return None
    return target


def partial_bytes(folder: str, name: str, src: "Source | None" = None) -> int:
    """Bytes a crashed or cancelled download left that the next one resumes from (same source, same size)."""
    target = target_path(folder, name)
    part = target.with_name(target.name + PART_SUFFIX) if target else None
    try:
        size = part.stat().st_size if part and part.is_file() else 0
    except OSError:
        return 0
    meta = _partials().get(os.path.normcase(str(target))) if size else None
    if src is not None and not (isinstance(meta, dict) and meta.get("url") == _url_key(src.url)
                                and meta.get("size") == src.size):
        return 0
    return size


def free_bytes(path: Path) -> int | None:
    probe_dir = Path(path)
    while not probe_dir.exists() and probe_dir.parent != probe_dir:
        probe_dir = probe_dir.parent
    try:
        return shutil.disk_usage(probe_dir).free
    except OSError:
        return None


def _gb(n: int | None) -> str:
    return "unknown size" if n is None else f"{n / 1024**3:.1f} GB" if n >= 1024**3 / 10 else f"{n / 1024**2:.0f} MB"


# ---- the Workflow Check: annotate missing models ------------------------------------------------------------------

def _searched_text(lookup: Lookup) -> str:
    return ", ".join(lookup.searched) if lookup.searched else "nothing"


def resolve_missing(result: dict, settings: dict, links=(), *, deadline: float | None = None) -> None:
    """For every model_sync 'missing' item (not on this computer, not in the cloud model library): find a download
    source and move it to 'fetchable' (downloaded, then uploaded, before the render) or 'downloading' (in flight); an
    item that stays 'missing' gets a message naming it and the fix. Mutates *result*."""
    from . import model_sync

    result.setdefault("fetchable", [])
    result.setdefault("downloading", [])
    deadline = deadline if deadline is not None else time.monotonic() + LOOKUP_BUDGET_S
    in_flight = {(f["folder"], f["name"]): f for f in active_fetch_files()}
    blocked = downloads_blocked()
    auto = settings.get("auto_download_models", True) is not False
    cap = ask_above_bytes(settings)
    token = _hf_token()
    keep = []
    for item in result.get("missing", []):
        folder, name = item.get("folder"), item.get("name")
        label = f"{name} ({folder})"
        if str(item.get("class_type") or "").startswith("H2Vault") or not folder:
            keep.append(item)
            continue
        live = in_flight.get((folder, name))
        if live is not None:
            item.update(source={"label": live.get("label")}, received=live.get("received", 0),
                        size=live.get("size"), message=f"{label} is being downloaded from {live.get('label')}.")
            result["downloading"].append(item)
            continue
        if blocked:
            item["source_status"] = "blocked"
            item["message"] = (f"{label} is neither on this computer nor in the cloud model library, and {blocked}: "
                               f"put the file in ComfyUI's models/{folder} folder (it is then uploaded), or allow "
                               "model downloads, then run the check again.")
            keep.append(item)
            continue
        lookup = find_source(folder, name, links, token=token, deadline=deadline)
        item["source_status"] = lookup.status
        if lookup.status == "found":
            src = lookup.source
            item["source"] = src.public()
            item["size"] = src.size
            target = target_path(folder, name)
            part = partial_bytes(folder, name, src)
            free = free_bytes(target.parent) if target else None
            if target is None:
                item["message"] = (f"{label}: this computer has no ComfyUI models/{folder} folder to download it "
                                   "into. Put the file there yourself, then run the check again.")
                keep.append(item)
                continue
            if src.size is not None and free is not None and free < src.size - part + DISK_MARGIN:
                item["message"] = (f"{label} ({_gb(src.size)}) can be downloaded from {src.label}, but the drive of "
                                   f"models/{folder} has only {_gb(free)} free. Free up space (it needs "
                                   f"{_gb(src.size - part + DISK_MARGIN)} including 1 GB to spare), then run the "
                                   "check again.")
                keep.append(item)
                continue
            reasons = []
            if not auto:
                reasons.append("automatic download is off in the HaloFuse settings")
            if src.size is None:
                reasons.append("the source does not state the size")
            elif cap is not None and src.size > cap:
                reasons.append(f"it is larger than the {_gb(cap)} limit for automatic downloads")
            if not src.trusted:
                reasons.append("it was found only in a personal upload on Hugging Face, not from the model's makers "
                               "or a known organisation")
            item["ask"] = bool(reasons)
            if part:
                item["partial"] = part
            verify = "size and SHA-256 are checked" if src.sha256 else "the size is checked" if src.size else \
                "the source states no size or checksum"
            resume = f"; {_gb(part)} were downloaded earlier and it resumes from there" if part else ""
            guard = model_sync._upload_guard_bytes(settings)
            over = (" It is larger than the automatic upload limit, so you are asked again before it is uploaded."
                    if src.size and guard is not None and src.size > guard else "")
            item["message"] = (f"{label} is neither on this computer nor in the cloud model library: it is downloaded "
                               f"from {src.label} ({_gb(src.size)}; {verify}{resume}), then uploaded to the cloud "
                               f"model library, before the render starts"
                               + (f". You are asked first: {'; '.join(reasons)}." if reasons else ".") + over)
            result["fetchable"].append(item)
            continue
        if lookup.status == "ambiguous":
            item["candidates"] = [c.public() for c in lookup.candidates]
            listed = "; ".join(f"{i + 1}. {c.label} ({_gb(c.size)})" for i, c in enumerate(lookup.candidates))
            item["message"] = (f"{label} is not on this computer or in the cloud model library, and "
                               f"{len(lookup.candidates)} different files with this name were found: {listed}. "
                               "Pick the right one below (it is then downloaded and uploaded), or paste a download "
                               "link.")
        elif lookup.status == "gated":
            src = lookup.source
            item["source"] = src.public()
            repo = src.repo or "its repository"
            item["message"] = (f"{label} is on Hugging Face in {repo}, which needs a Hugging Face login and its "
                               f"licence accepted (huggingface.co/{repo}). Accept it there, then add a Hugging Face "
                               "token under Advanced (it is kept in this computer's keychain) and run the check "
                               f"again; or download the file yourself into ComfyUI's models/{folder} folder, or paste "
                               "another download link below.")
        elif lookup.status == "unavailable":
            item["message"] = (f"{label} is not on this computer or in the cloud model library, and "
                               f"{', '.join(lookup.unavailable)} could not be searched (no connection?). Run the check "
                               f"again, paste a download link below, or put the file in ComfyUI's models/{folder} "
                               "folder.")
        else:
            item["message"] = (f"{label} is not on this computer or in the cloud model library, and no download was "
                               f"found (searched: {_searched_text(lookup)}; Civitai is not searched). Paste a download "
                               "link below (Hugging Face, Civitai or a direct link ending in the file name), or put "
                               f"the file in ComfyUI's models/{folder} folder, then run the check again.")
        keep.append(item)
    result["missing"] = keep


# ---- downloads registry -------------------------------------------------------------------------------------------

_FETCHES: dict[str, dict] = {}
_FETCH_LOCK = threading.Lock()


def _set_fetch(fid: str, **fields) -> None:
    with _FETCH_LOCK:
        if fid in _FETCHES:
            _FETCHES[fid].update(fields)


def _set_file(fid: str, i: int, **fields) -> None:
    with _FETCH_LOCK:
        if fid in _FETCHES:
            _FETCHES[fid]["files"][i].update(fields)


def _cancelled(fid: str) -> bool:
    with _FETCH_LOCK:
        return bool(_FETCHES.get(fid, {}).get("cancel"))


def get_fetch_state(fid: str) -> dict | None:
    """A copy without the source URLs and local paths (what the browser may see)."""
    with _FETCH_LOCK:
        state = _FETCHES.get(fid)
        if state is None:
            return None
        out = {k: v for k, v in state.items() if k not in ("cancel", "_sources")}
        out["files"] = [{k: v for k, v in f.items() if not k.startswith("_")} for f in state["files"]]
        return out


def list_fetches() -> dict:
    with _FETCH_LOCK:
        ids = list(_FETCHES)
    return {fid: get_fetch_state(fid) for fid in ids}


def cancel_fetch(fid: str) -> None:
    _set_fetch(fid, cancel=True)


def active_fetch_files() -> list[dict]:
    with _FETCH_LOCK:
        out = []
        for state in _FETCHES.values():
            if state.get("status") in ("queued", "running"):
                out.extend({k: v for k, v in f.items() if not k.startswith("_")} for f in state["files"]
                           if f["status"] in ("queued", "downloading", "verifying"))
        return out


def prepare_fetch(requests: list, settings: dict | None = None) -> list[dict]:
    """Re-derive every requested {folder, name} server side: the source (the artist's link, the chosen candidate or
    the check's lookup), the target path and the policy. The browser never names a local path. Raises FetchError."""
    from . import model_sync

    if not isinstance(requests, list) or not requests:
        raise FetchError("Choose the models to download.")
    blocked = downloads_blocked()
    if blocked:
        raise FetchError(f"{blocked[0].upper()}{blocked[1:]}. Put the files in their ComfyUI models folders "
                         "yourself, or allow model downloads.")
    allowed = model_sync._allowed_folders()
    busy = {(f["folder"], f["name"]) for f in active_fetch_files()}
    out, seen = [], set()
    for req in requests[:200]:
        if not isinstance(req, dict):
            raise FetchError("A download request entry is malformed.")
        folder = str(req.get("folder") or "")
        name = str(req.get("name") or "").replace("\\", "/").strip("/")
        if folder not in allowed or _safe_parts(name) is None:
            raise FetchError(f"'{name[:80]}' in '{folder[:40]}' is not a model this computer can download into.")
        if (folder, name) in seen:
            continue                     # the same model referenced by two nodes: one download
        seen.add((folder, name))
        if (folder, name) in busy:
            raise FetchError(f"{name} ({folder}) is already being downloaded.")
        target = target_path(folder, name)
        if target is None:
            raise FetchError(f"{name} ({folder}): this computer has no ComfyUI models/{folder} folder to download "
                             "into.")
        if target.is_file():
            raise FetchError(f"{name} ({folder}) is already on this computer; run the Workflow Check again.")
        if req.get("url"):
            src = probe(check_link(req["url"], name), _hf_token() if _hf_parts(str(req["url"])) else None)
            if src is None:
                raise FetchError(f"{name} ({folder}): that link answers 'not found'; check it and paste it again.")
            with _LOOKUP_LOCK:
                _PASTED[(folder, name)] = src
        elif req.get("choice") is not None:
            src = choose_candidate(folder, name, req.get("choice"))
        else:
            with _LOOKUP_LOCK:
                hit = _FOUND.get((folder, name))
            fresh = hit is not None and time.time() - hit[0] < LOOKUP_TTL_S
            lookup = Lookup("found", hit[1]) if fresh else find_source(folder, name, (), token=_hf_token())
            if lookup.status != "found":
                raise FetchError(f"{name} ({folder}): no download source is known; paste a download link for it.")
            src = lookup.source
        out.append({"folder": folder, "name": name, "_target": str(target), "_source": src})
    return out


def start_fetch(prepared: list[dict], settings: dict | None = None) -> str:
    fid = uuid.uuid4().hex[:12]
    state = {"status": "queued", "started_at": time.time(), "error": None, "cancel": False, "files": [
        {"folder": p["folder"], "name": p["name"], "label": p["_source"].label, "size": p["_source"].size,
         "verified": bool(p["_source"].sha256), "received": 0, "status": "queued", "error": None,
         "_target": p["_target"], "_source": p["_source"]} for p in prepared]}
    with _FETCH_LOCK:
        _FETCHES[fid] = state
    threading.Thread(target=_run_fetch, args=(fid,), daemon=True, name=f"halofuse-fetch-{fid}").start()
    return fid


def _record(text: str, level: str = "info") -> None:
    try:
        from . import activity

        activity.record(text, level=level, kind="note")
    except Exception:  # noqa: BLE001 - the panel still shows it
        pass


def _run_fetch(fid: str) -> None:
    with _FETCH_LOCK:
        files = list(_FETCHES[fid]["files"])
    _set_fetch(fid, status="running")
    cancelled = failed = False
    for i, f in enumerate(files):
        if _cancelled(fid):
            cancelled = True
            _set_file(fid, i, status="cancelled")
            continue
        src: Source = f["_source"]
        _set_file(fid, i, status="downloading")
        _record(f"Downloading model {f['name']} ({f['folder']}) from {src.label} ({_gb(src.size)}).")
        try:
            size, digest = download(src, Path(f["_target"]),
                                    progress=lambda n, total, _i=i: _set_file(fid, _i, received=n, size=total),
                                    cancel=lambda: _cancelled(fid))
        except _Cancelled:
            cancelled = True
            _set_file(fid, i, status="cancelled", error="Cancelled; the part downloaded so far is kept and resumes.")
            continue
        except FetchError as exc:
            failed = True
            _set_file(fid, i, status="failed", error=str(exc))
            _set_fetch(fid, error=f"{f['name']}: {exc}")
            _record(f"Model download failed: {f['name']} ({f['folder']}): {exc}", level="error")
            continue
        except Exception as exc:  # noqa: BLE001 - reported in words; detail to the private log
            failed = True
            msg = "the download stopped unexpectedly; try again (it resumes), or see the HaloFuse log"
            _set_file(fid, i, status="failed", error=msg)
            _set_fetch(fid, error=f"{f['name']}: {msg}")
            _log.warning("model download %s failed [%s]", fid, type(exc).__name__)
            continue
        _set_file(fid, i, status="done", received=size, size=size, sha256=digest)
        _record(f"Downloaded model {f['name']} into models/{f['folder']} ({_gb(size)}"
                f"{', SHA-256 verified' if src.sha256 else ''}).")
    _set_fetch(fid, status="failed" if failed else "cancelled" if cancelled else "done")


class _Cancelled(Exception):
    pass


def _url_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]


def _partials_path() -> Path | None:
    return _cache_path("halofuse_partial_downloads.json")


def _partials() -> dict:
    path = _partials_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path and path.is_file() else {}
    except (OSError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def _save_partial(key: str, value: dict | None) -> None:
    path = _partials_path()
    if path is None:
        return
    with _CACHE_LOCK:
        data = _partials()
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
        try:
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass


def download(src: Source, target: Path, *, progress=None, cancel=None) -> tuple[int, str]:
    """Download *src* to *target* through ``<target>.halofuse-part``: resumes a partial file from the same source
    (HTTP Range), restarts when the server ignores the range, checks free space first, caps at the stated size and
    verifies size + SHA-256 when known. Returns (size, sha256). Raises FetchError / _Cancelled."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + PART_SUFFIX)
    key = os.path.normcase(str(target))
    meta = _partials().get(key)
    expected = src.size
    offset = 0
    if part.is_file():
        same = isinstance(meta, dict) and meta.get("url") == _url_key(src.url) and meta.get("size") == expected
        if same and (expected is None or part.stat().st_size <= expected):
            offset = part.stat().st_size
        else:
            part.unlink()
    free = free_bytes(target.parent)
    if expected is not None and free is not None and free < expected - offset + DISK_MARGIN:
        raise FetchError(f"not enough free disk space: it needs {_gb(expected - offset + DISK_MARGIN)} "
                         f"(including 1 GB to spare) and the drive has {_gb(free)}. Free up space and try again.")
    _save_partial(key, {"url": _url_key(src.url), "size": expected, "started": time.time()})
    token = _hf_token() if _hf_parts(src.url) else None
    digest = hashlib.sha256()
    if offset:
        with part.open("rb") as fh:
            for chunk in iter(lambda: fh.read(CHUNK), b""):
                digest.update(chunk)
    headers = {"Range": f"bytes={offset}-"} if offset else {}
    try:
        resp = _request(src.url, headers=headers, token=token, timeout=DOWNLOAD_TIMEOUT_S)
    except LookupUnavailable as exc:
        raise FetchError(f"the source could not be reached ({exc}); try again (it resumes)") from None
    try:
        if resp.status == 416 and offset and expected == offset:
            pass                                     # the earlier run had already received every byte
        elif resp.status in (401, 403):
            raise FetchError("the source needs a login (a gated Hugging Face repository or a private link): accept "
                             "the licence and add a Hugging Face token under Advanced, or paste another link")
        elif resp.status in (404, 410):
            raise FetchError("the source no longer has this file; paste another download link")
        elif resp.status == 206 and offset:
            start = re.match(r"bytes (\d+)-", resp.header("content-range") or "")
            if not start or int(start.group(1)) != offset:
                raise FetchError("the source answered the resume request with the wrong part; try again")
            _stream(resp, part, "ab", offset, expected, digest, progress, cancel)
        elif resp.status == 200:
            if offset:                               # the server ignored the range: start again from zero
                digest = hashlib.sha256()
                offset = 0
            length = resp.header("content-length")
            if expected is None and length and length.isdigit():
                expected = int(length)
            _stream(resp, part, "wb", 0, expected, digest, progress, cancel)
        else:
            raise FetchError(f"the source answered HTTP {resp.status}; try again later or paste another link")
    finally:
        resp.close()
    size = part.stat().st_size
    if expected is not None and size != expected:
        if size > expected:
            part.unlink()
            raise FetchError(f"the file is larger than the source stated ({size} > {expected} bytes); it was "
                             "deleted")
        raise FetchError(f"the download stopped at {_gb(size)} of {_gb(expected)}; try again (it resumes)")
    value = digest.hexdigest()
    if src.sha256 and value != src.sha256:
        part.unlink()
        _save_partial(key, None)
        raise FetchError("the downloaded file's SHA-256 does not match the source's; it was deleted. Try again, or "
                         "paste another link")
    if target.exists():
        part.unlink()                                # placed there meanwhile: keep the artist's copy
    else:
        os.replace(part, target)
    _save_partial(key, None)
    _after_download(target, value)
    return size, value


def _stream(resp, part: Path, mode: str, offset: int, expected, digest, progress, cancel) -> None:
    received = offset
    last = 0.0
    with part.open(mode) as fh:
        while True:
            if cancel is not None and cancel():
                raise _Cancelled()
            chunk = resp.read(CHUNK)
            if not chunk:
                break
            received += len(chunk)
            if expected is not None and received > expected:
                fh.close()
                part.unlink()
                raise FetchError("the source sent more data than it stated; the file was deleted")
            fh.write(chunk)
            digest.update(chunk)
            now = time.monotonic()
            if progress is not None and now - last > 0.25:
                last = now
                progress(received, expected)
    if progress is not None:
        progress(received, expected)


def _after_download(target: Path, sha256: str) -> None:
    """The new file is listed at once (local model index) and its SHA-256 is cached for the upload's record."""
    try:
        from . import workflow_analysis

        with workflow_analysis._index_lock:
            workflow_analysis._index_cache = None
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import model_library

        model_library.remember_hash(target, sha256)
    except Exception:  # noqa: BLE001 - the upload hashes it again
        pass

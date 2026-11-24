"""The artist's own custom nodes (owner 2026-11-23, Level 1; design: _internal/USER_NODES_DESIGN.md).

The artist lists packs in the panel (*My custom nodes*), one per line: `owner/repo`, `owner/repo@ref`, a
https://github.com/owner/repo URL, or a Comfy Registry id (`id`, `id@version`). `resolve_text` checks each line
against the public GitHub / Comfy Registry APIs (no token, only the repository / id / class name is sent), pins it
to an exact commit or registry version and reads its licence (informational: the artist decides for their own jobs).
The resolved list is the setting `user_nodes`; `job_env` sends its pins as HF_USER_NODES with every job when the
selected runtime's runner can install them (its node snapshot names `user_nodes/1`; the Original image never can).
`apply_to_report` lets the Workflow Check count classes of the artist's packs as available (a note, not verified)
and offer "Add <pack> to my custom nodes" for a class the runtime lacks (Comfy Registry class -> pack lookup).

No URL is ever returned to the browser or logged: entries hold repository names, ids and pins only.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_log = logging.getLogger("halofuse_bridge.user_nodes")

CAPABILITY = "user_nodes/1"     # runtime/runner/hf/user_nodes.py CAPABILITY
ENV = "HF_USER_NODES"
MAX_PACKS = 20                  # the runner installs at most this many (runner/hf/user_nodes.py MAX_PACKS)
MAX_LINE = 300
MAX_TEXT = 8000
MAX_JSON = 2 * 2**20
API_TIMEOUT = 10.0
LOOKUP_TIMEOUT = 4.0
MAX_LOOKUPS = 10                # Workflow Check: registry lookups per check
LOOKUP_BUDGET_S = 12.0
RESOLVE_TTL_S = 600.0
CLASS_TTL_S = 3600.0
API_HOSTS = frozenset({"api.github.com", "api.comfy.org"})
# Same set as runtime/scripts/install_nodes.py REFUSED_REPO_SHA256 (SHA-256 of lowercase owner/repo).
REFUSED_REPO_SHA256 = frozenset({"cd86c77eade6596dffd7d42838ae75189b4ec746dcbf4cde1f5d0d7bb85cabc3"})
REFUSED_NAMES = frozenset({"comfyui-manager"})   # a batch container never installs or updates code by itself
LICENCE_CLASSES = ("permissive", "copyleft", "network-copyleft", "non-commercial", "no-licence", "unknown")
COST_NOTE = ("Installed at the start of every job, before ComfyUI starts: about 20-90 s per pack (estimate; longer "
             "when a requirement has to be compiled). Nothing is cached between jobs.")
LEGACY_NOTE = ("The Original image cannot install custom nodes. Choose the HaloFuse image under Cloud runtime to "
               "use them.")
NOT_YET_NOTE = ("The published HaloFuse image cannot install custom nodes yet: support arrives with the next "
                "HaloFuse image.")

_REPO = r"([A-Za-z0-9][A-Za-z0-9._-]{0,99})/([A-Za-z0-9][A-Za-z0-9._-]{0,99})"
_REF = r"[A-Za-z0-9][A-Za-z0-9._/+-]{0,199}"
_GH_URL_RE = re.compile(rf"^(?:https://)?(?:www\.)?github\.com/{_REPO}(?:/tree/({_REF}))?/?(?:@({_REF}))?$")
_PLAIN_RE = re.compile(rf"^{_REPO}(?:@({_REF}))?$")
_REGISTRY_URL_RE = re.compile(r"^https://registry\.comfy\.org/nodes/([A-Za-z0-9][A-Za-z0-9._-]{0,99})/?$")
_ID_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]{0,99})(?:@([A-Za-z0-9][A-Za-z0-9.+_-]{0,63}))?$")
_REPO_RE = re.compile(rf"^{_REPO}$")
_REF_RE = re.compile(rf"^{_REF}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}$")
_SPDX_RE = re.compile(r"^[A-Za-z0-9.+-]{1,64}$")
_PERMISSIVE = {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "Unlicense", "0BSD", "Zlib", "CC0-1.0",
               "BSL-1.0", "Python-2.0"}


class UserNodesError(ValueError):
    """A line or list that cannot be used; the message is shown to the artist (no URL)."""


class _ApiError(Exception):
    pass


class _NotFound(_ApiError):
    pass


class _RateLimited(_ApiError):
    pass


class _Unreachable(_ApiError):
    pass


# ---- parsing ------------------------------------------------------------------------------------------------

def _refused(name: str) -> bool:
    low = name.lower()
    return hashlib.sha256(low.encode("utf-8")).hexdigest() in REFUSED_REPO_SHA256 or \
        low.rsplit("/", 1)[-1] in REFUSED_NAMES


def _strip_git(repo: str) -> str:
    return repo[:-4] if repo.lower().endswith(".git") else repo


def parse_line(line) -> dict:
    """{"source": "github", "repo", "ref"} or {"source": "registry", "id", "version"}; UserNodesError otherwise."""
    s = str(line or "").strip()
    if not s:
        raise UserNodesError("empty line")
    if len(s) > MAX_LINE or ".." in s:
        raise UserNodesError("not a GitHub repository or Comfy Registry id")
    spec = None
    m = _GH_URL_RE.fullmatch(s)
    if m:
        spec = {"source": "github", "repo": _strip_git(f"{m.group(1)}/{m.group(2)}"), "ref": m.group(4) or m.group(3)}
    elif "://" in s or s.lower().startswith(("github.com", "www.")):
        m = _REGISTRY_URL_RE.fullmatch(s)
        if not m:
            raise UserNodesError("only https GitHub addresses or Comfy Registry ids are accepted")
        spec = {"source": "registry", "id": m.group(1), "version": None}
    else:
        m = _PLAIN_RE.fullmatch(s)
        if m:
            spec = {"source": "github", "repo": f"{m.group(1)}/{m.group(2)}", "ref": m.group(3)}
        else:
            m = _ID_RE.fullmatch(s)
            if not m:
                raise UserNodesError("not a GitHub repository (owner/repo) or Comfy Registry id")
            spec = {"source": "registry", "id": m.group(1), "version": m.group(2)}
    name = spec.get("repo") or spec.get("id")
    if _refused(name):
        raise UserNodesError(f"{name} cannot be installed in a HaloFuse job")
    return spec


def _key(spec: dict) -> tuple:
    return (spec["source"], (spec.get("repo") or spec.get("id")).lower())


def _items(text) -> list:
    """[{"line", "spec" | None, "error" | None}] in input order (comments and blank lines skipped)."""
    out, seen, count = [], set(), 0
    lines = [l.strip() for l in str(text or "").splitlines()]
    lines = [l for l in lines if l and not l.startswith("#")]
    for i, line in enumerate(lines):
        if count >= MAX_PACKS:
            out.append({"line": "", "spec": None,
                        "error": f"at most {MAX_PACKS} packs; {len(lines) - i} more line(s) ignored"})
            break
        try:
            spec = parse_line(line)
        except UserNodesError as exc:
            out.append({"line": line[:MAX_LINE], "spec": None, "error": str(exc)})
            continue
        if _key(spec) in seen:
            continue
        seen.add(_key(spec))
        count += 1
        out.append({"line": line, "spec": spec, "error": None})
    return out


def parse_text(text) -> tuple[list, list]:
    """(specs, problems [{"line", "message"}])."""
    items = _items(text)
    return ([i["spec"] for i in items if i["spec"]],
            [{"line": i["line"], "message": i["error"]} for i in items if i["error"]])


def licence_class(spdx) -> str:
    if not spdx:
        return "no-licence"
    s = str(spdx)
    if "-NC" in s.upper():
        return "non-commercial"
    if s in _PERMISSIVE:
        return "permissive"
    if s.startswith("AGPL-"):
        return "network-copyleft"
    if s.startswith(("GPL-", "LGPL-")) or s in ("MPL-2.0", "EPL-2.0"):
        return "copyleft"
    return "unknown"


# ---- public APIs ----------------------------------------------------------------------------------------------

def _allowed(url) -> bool:
    try:
        u = urllib.parse.urlsplit(str(url))
        port = u.port
    except ValueError:
        return False
    return (u.scheme == "https" and (u.hostname or "") in API_HOSTS and u.username is None and u.password is None
            and port in (None, 443))


class _CheckedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _allowed(newurl):
            raise _Unreachable("redirect to a host that is not allowed")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _get_json(url: str, timeout: float = API_TIMEOUT):
    """GET a public JSON API (GitHub / Comfy Registry only). No credentials, no personal data, generic agent."""
    if not _allowed(url):
        raise _Unreachable("host not allowed")
    github = urllib.parse.urlsplit(url).hostname == "api.github.com"
    req = urllib.request.Request(url, headers={
        "User-Agent": "HaloFuse", "Accept": "application/vnd.github+json" if github else "application/json"})
    try:
        with urllib.request.build_opener(_CheckedRedirect).open(req, timeout=timeout) as resp:
            body = resp.read(MAX_JSON + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise _NotFound() from None
        if exc.code == 429 or (exc.code == 403 and (exc.headers or {}).get("X-RateLimit-Remaining") == "0"):
            raise _RateLimited() from None
        raise _Unreachable(f"HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise _Unreachable(type(exc).__name__) from None
    if len(body) > MAX_JSON:
        raise _Unreachable("response too large")
    try:
        return json.loads(body)
    except ValueError:
        raise _Unreachable("unreadable response") from None


def _q(value: str) -> str:
    return urllib.parse.quote(value, safe="/")


def _github_repo(url) -> str | None:
    m = _GH_URL_RE.fullmatch(str(url or "").strip().rstrip("/"))
    return _strip_git(f"{m.group(1)}/{m.group(2)}") if m and not (m.group(3) or m.group(4)) else None


def _clean(text, limit=100) -> str:
    return re.sub(r"[\x00-\x1f\x7f<>]", "", str(text or ""))[:limit].strip()


def _api_message(exc: _ApiError, name: str, service: str) -> str:
    """Artist-facing text for a failed public API call (never the URL)."""
    if isinstance(exc, _NotFound):
        return f"{name} was not found {'on GitHub' if service == 'GitHub' else 'in the Comfy Registry'}"
    if isinstance(exc, _RateLimited):
        return "GitHub's public API limit (60 lookups per hour) is used up; try again later."
    return f"{service} could not be reached; check the connection and try again."


def _github_meta(repo: str) -> dict:
    meta = _get_json(f"https://api.github.com/repos/{_q(repo)}")
    if not isinstance(meta, dict):
        raise _Unreachable("unexpected answer")
    return meta


def _licence(meta: dict) -> tuple:
    lic = meta.get("license") if isinstance(meta.get("license"), dict) else {}
    spdx = lic.get("spdx_id") if isinstance(lic.get("spdx_id"), str) and _SPDX_RE.fullmatch(lic["spdx_id"]) else None
    return spdx, ("unknown" if spdx == "NOASSERTION" else licence_class(spdx))


def _resolve_github(line: str, spec: dict) -> tuple[dict, list]:
    repo, ref = spec["repo"], spec["ref"]
    try:
        meta = _github_meta(repo)
    except _NotFound:
        raise UserNodesError(f"{repo} was not found on GitHub (or it is private)") from None
    except _ApiError as exc:
        raise UserNodesError(_api_message(exc, repo, "GitHub")) from None
    if meta.get("private"):
        raise UserNodesError(f"{repo} is private: only public repositories can be installed")
    full = meta.get("full_name") if isinstance(meta.get("full_name"), str) and _REPO_RE.fullmatch(meta["full_name"]) \
        else repo
    if _refused(full):
        raise UserNodesError(f"{full} cannot be installed in a HaloFuse job")
    branch = meta.get("default_branch") if isinstance(meta.get("default_branch"), str) and \
        _REF_RE.fullmatch(meta["default_branch"]) else "HEAD"
    try:
        commit = _get_json(f"https://api.github.com/repos/{_q(full)}/commits/{_q(ref or branch)}")
    except _NotFound:
        raise UserNodesError(f"{full}: {ref or branch} is not a branch, tag or commit of the repository") from None
    except _ApiError as exc:
        raise UserNodesError(_api_message(exc, full, "GitHub")) from None
    sha = commit.get("sha") if isinstance(commit, dict) else None
    if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
        raise UserNodesError(f"{full}: GitHub did not return an exact commit")
    spdx, cls = _licence(meta)
    notes = []
    if meta.get("archived"):
        notes.append("archived on GitHub (no longer maintained)")
    if isinstance(meta.get("size"), int) and meta["size"] > 200 * 1024:
        notes.append("a large repository: its download may exceed the 200 MB limit")
    canonical = f"{full}@{ref}" if ref else full    # stored and shown without any URL
    return ({"source": "github", "repo": full, "ref": ref, "commit": sha, "licence": spdx, "licence_class": cls,
             "line": canonical}, notes)


def _active(ver: dict) -> bool:
    return not ver.get("deprecated") and ver.get("status") in (None, "NodeVersionStatusActive")


def _resolve_registry(line: str, spec: dict) -> tuple[dict, list]:
    rid, version = spec["id"], spec["version"]
    try:
        node = _get_json(f"https://api.comfy.org/nodes/{_q(rid)}")
    except _ApiError as exc:
        raise UserNodesError(_api_message(exc, rid, "the Comfy Registry")) from None
    if not isinstance(node, dict):
        raise UserNodesError(f"{rid}: the Comfy Registry gave an unexpected answer")
    nid = node.get("id") if isinstance(node.get("id"), str) and _ID_RE.fullmatch(node["id"]) else rid
    if version:
        try:
            ver = _get_json(f"https://api.comfy.org/nodes/{_q(nid)}/versions/{_q(version)}")
        except _ApiError as exc:
            raise UserNodesError(_api_message(exc, f"{nid} version {version}", "the Comfy Registry")) from None
    else:
        ver = node.get("latest_version")
    notes = []
    if not version and isinstance(ver, dict) and not _active(ver):
        # the registry flags or deprecates versions; like the image build (update_pins.py), take the newest active one
        newest = ver.get("version")
        try:
            active = _get_json(f"https://api.comfy.org/nodes/{_q(nid)}/versions?statuses=NodeVersionStatusActive")
        except _ApiError as exc:
            raise UserNodesError(_api_message(exc, nid, "the Comfy Registry")) from None
        ver = next((v for v in active if isinstance(v, dict) and _active(v)), None) if isinstance(active, list) \
            else None
        if ver is not None:
            notes.append(f"the newest version {newest} is flagged in the Comfy Registry; pinned to the newest active "
                         f"version {ver.get('version')}")
    if not isinstance(ver, dict) or not isinstance(ver.get("version"), str) or not _VERSION_RE.fullmatch(ver["version"]):
        raise UserNodesError(f"{nid} has no active published version in the Comfy Registry")
    if not _active(ver):
        raise UserNodesError(f"version {ver['version']} of {nid} is deprecated or not active in the Comfy Registry")
    repo = _github_repo(node.get("repository"))
    spdx, cls = None, "unknown"
    if repo:
        try:
            spdx, cls = _licence(_github_meta(repo))
        except _ApiError:
            notes.append("licence not checked: GitHub could not be reached")
    else:
        notes.append("licence not checked: the pack's source is not on GitHub")
    canonical = f"{nid}@{version}" if version else nid
    return ({"source": "registry", "id": nid, "version": ver["version"], "repo": repo, "licence": spdx,
             "licence_class": cls, "line": canonical}, notes)


_resolve_cache: dict = {}
_class_cache: dict = {}
_cache_lock = threading.Lock()


def clear_caches() -> None:
    with _cache_lock:
        _resolve_cache.clear()
        _class_cache.clear()


def resolve_text(text) -> dict:
    """Check every line. {"rows": [{"line", "ok", "entry", "message", "notes"}], "entries": [...], "ok"}."""
    rows = []
    for item in _items(text):
        if item["error"]:
            rows.append({"line": item["line"], "ok": False, "entry": None, "message": item["error"], "notes": []})
            continue
        key = item["line"].lower()
        with _cache_lock:
            hit = _resolve_cache.get(key)
        if hit and time.monotonic() - hit[0] < RESOLVE_TTL_S:
            rows.append(dict(hit[1]))
            continue
        spec = item["spec"]
        try:
            entry, notes = (_resolve_github if spec["source"] == "github" else _resolve_registry)(item["line"], spec)
        except UserNodesError as exc:
            rows.append({"line": item["line"], "ok": False, "entry": None, "message": str(exc), "notes": []})
            continue
        row = {"line": entry["line"], "ok": True, "entry": entry, "message": "", "notes": notes}
        with _cache_lock:
            _resolve_cache[key] = (time.monotonic(), row)
        rows.append(dict(row))
    seen = {}
    for r in rows:      # the same pack as owner/repo and as its registry id would be installed twice
        repo = ((r.get("entry") or {}).get("repo") or "").lower() if r["ok"] else ""
        if repo and repo in seen:
            r.update(ok=False, entry=None, notes=[], message=f"the same pack as {seen[repo]}: list it once")
        elif repo:
            seen[repo] = r["line"]
    entries = [r["entry"] for r in rows if r["ok"]]
    return {"rows": rows, "entries": entries, "ok": all(r["ok"] for r in rows)}


# ---- the stored list ------------------------------------------------------------------------------------------

_GH_KEYS = {"source", "repo", "ref", "commit", "licence", "licence_class", "line"}
_REG_KEYS = {"source", "id", "version", "repo", "licence", "licence_class", "line"}


def _valid_entry(e) -> bool:
    if not isinstance(e, dict):
        return False
    lic, cls, line = e.get("licence"), e.get("licence_class"), e.get("line")
    if lic is not None and not (isinstance(lic, str) and _SPDX_RE.fullmatch(lic)):
        return False
    if cls is not None and cls not in LICENCE_CLASSES:
        return False
    try:
        spec = parse_line(line) if isinstance(line, str) else None
    except UserNodesError:
        return False
    if spec is None or spec["source"] != e.get("source"):
        return False
    if e["source"] == "github":
        ref = e.get("ref")
        return (set(e) <= _GH_KEYS and isinstance(e.get("repo"), str) and bool(_REPO_RE.fullmatch(e["repo"]))
                and ".." not in e["repo"] and not _refused(e["repo"])
                and isinstance(e.get("commit"), str) and bool(_SHA_RE.fullmatch(e["commit"]))
                and (ref is None or (isinstance(ref, str) and bool(_REF_RE.fullmatch(ref)) and ".." not in ref)))
    repo = e.get("repo")
    return (set(e) <= _REG_KEYS and isinstance(e.get("id"), str) and bool(_ID_RE.fullmatch(e["id"]))
            and "@" not in e["id"] and ".." not in e["id"] and not _refused(e["id"])
            and isinstance(e.get("version"), str) and bool(_VERSION_RE.fullmatch(e["version"]))
            and (repo is None or (isinstance(repo, str) and bool(_REPO_RE.fullmatch(repo)) and ".." not in repo)))


def valid_entries(value) -> bool:
    """The `user_nodes` setting: at most MAX_PACKS resolved entries, strict shapes, no duplicates."""
    if not isinstance(value, list) or len(value) > MAX_PACKS or not all(_valid_entry(e) for e in value):
        return False
    keys = [(e["source"], (e.get("repo") if e["source"] == "github" else e["id"]).lower()) for e in value]
    repos = [e["repo"].lower() for e in value if e.get("repo")]
    return len(keys) == len(set(keys)) and len(repos) == len(set(repos))


def entries_of(settings: dict) -> list:
    value = (settings or {}).get("user_nodes") or []
    return value if valid_entries(value) else []


def runtime_support(settings: dict) -> tuple[bool, str]:
    """(True, cost note) when the selected runtime's runner installs custom nodes, else (False, why)."""
    from . import workflow_analysis as wa
    from .provider import image_contract

    contract = image_contract.select(settings=settings or {})
    if contract.name == "legacy":
        return False, LEGACY_NOTE
    try:
        snap = wa.load_snapshot(contract.snapshot_profile)
    except ValueError:
        snap = None
    if snap is None or CAPABILITY not in getattr(snap, "runner_capabilities", ()):
        return False, NOT_YET_NOTE
    return True, COST_NOTE


def job_env(settings: dict) -> dict:
    """{"HF_USER_NODES": <pins>} for a job whose runtime installs custom nodes and whose list is not empty."""
    entries = entries_of(settings)
    if not entries or not runtime_support(settings)[0]:
        return {}
    pins = [{"source": "github", "repo": e["repo"], "commit": e["commit"]} if e["source"] == "github"
            else {"source": "registry", "id": e["id"], "version": e["version"]} for e in entries]
    return {ENV: json.dumps(pins, separators=(",", ":"))}


# ---- Workflow Check -------------------------------------------------------------------------------------------

def registry_pack_for_class(class_type: str):
    """{"id", "name", "repo", "line"} of the Comfy Registry pack that provides *class_type*, None when unknown or
    unreachable. Only the class name is sent (verified live 2026-11-23: 200 with the pack, 404 when unknown)."""
    if not isinstance(class_type, str) or not 0 < len(class_type) <= 200:
        return None
    with _cache_lock:
        hit = _class_cache.get(class_type)
    if hit and time.monotonic() - hit[0] < CLASS_TTL_S:
        return hit[1]
    try:
        data = _get_json(f"https://api.comfy.org/comfy-nodes/{urllib.parse.quote(class_type, safe='')}/node",
                         timeout=LOOKUP_TIMEOUT)
    except _NotFound:
        data = None
    except _ApiError as exc:
        _log.info("custom node lookup for a missing class failed (%s)", type(exc).__name__)
        return None
    pack = None
    if isinstance(data, dict) and isinstance(data.get("id"), str) and _ID_RE.fullmatch(data["id"]) \
            and "@" not in data["id"] and not _refused(data["id"]):
        pack = {"id": data["id"], "name": _clean(data.get("name")) or data["id"],
                "repo": _github_repo(data.get("repository")), "line": data["id"]}
    with _cache_lock:
        _class_cache[class_type] = (time.monotonic(), pack)
    return pack


def local_pack_folder(class_type: str):
    """The custom_nodes folder that provides *class_type* in this (the artist's) ComfyUI, or None."""
    try:
        import nodes  # ComfyUI-provided
        cls = (getattr(nodes, "NODE_CLASS_MAPPINGS", {}) or {}).get(class_type)
    except Exception:  # noqa: BLE001 - outside ComfyUI
        return None
    mod = str(getattr(cls, "RELATIVE_PYTHON_MODULE", "") or "")
    return mod.split(".")[1] if mod.startswith("custom_nodes.") and mod.count(".") >= 1 else None


# ---- which pack provides a class (owner 2026-11-24: "add more and more custom nodes") ------------------------
# Order: (a) this ComfyUI's own folder for the class (object_info python_module = custom_nodes.<folder>) and that
# folder's git remote + commit; (b) the runtime catalog (image snapshots name the pack of every class they hold,
# also packs outside the chosen profile; catalog.json gives the pin and licence); (c) ComfyUI-Manager's
# extension-node-map, read from disk only (offline / its cache); then (d) the Comfy Registry class lookup.
# Nothing is ever uploaded from this computer: a local folder without a public GitHub remote stays blocked.

_RUNTIME_DIR = Path(__file__).resolve().parents[1] / "runtime"
_GITHUB_REMOTE_RE = re.compile(r"^(?:https://(?:[^@/\s]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)"
                               r"([A-Za-z0-9][A-Za-z0-9._-]{0,99})/([A-Za-z0-9][A-Za-z0-9._-]{0,99}?)(?:\.git)?/?$")
_ALLOWED_FALLBACK = frozenset({"permissive"})
_catalog_cache: dict = {}
_map_cache: dict = {}


def github_repo_of_remote(url) -> str | None:
    """owner/repo for a GitHub remote URL (https, ssh or scp form), None for any other host. Credentials in an https
    remote are dropped, never returned."""
    m = _GITHUB_REMOTE_RE.fullmatch(str(url or "").strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def _git_dir(folder: Path) -> Path | None:
    g = folder / ".git"
    if g.is_dir():
        return g
    if g.is_file():                     # worktree / submodule: "gitdir: <path>"
        try:
            line = g.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
        if line.startswith("gitdir:"):
            p = Path(line[7:].strip())
            p = p if p.is_absolute() else (folder / p)
            return p if p.is_dir() else None
    return None


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def git_origin_and_commit(folder) -> tuple:
    """(origin URL or None, HEAD commit or None) read from the folder's .git files (no git process is started)."""
    gd = _git_dir(Path(folder))
    if gd is None:
        return None, None
    common = gd
    cd = _read(gd / "commondir")
    if cd and cd.strip():
        common = (gd / cd.strip()).resolve()
    url = None
    section = None
    for raw in (_read(common / "config") or "").splitlines():
        line = raw.strip()
        if line.startswith("["):
            section = line
        elif section == '[remote "origin"]' and line.replace(" ", "").startswith("url="):
            url = line.split("=", 1)[1].strip()
            break
    commit = None
    head = (_read(gd / "HEAD") or "").strip()
    if _SHA_RE.fullmatch(head):
        commit = head
    elif head.startswith("ref:"):
        ref = head[4:].strip()
        value = None
        for base in (gd, common):
            value = _read(base / ref)
            if value is not None:
                break
        if value is not None:
            commit = value.strip() if _SHA_RE.fullmatch(value.strip()) else None
        else:
            for line in (_read(common / "packed-refs") or "").splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1] == ref and _SHA_RE.fullmatch(parts[0]):
                    commit = parts[0]
                    break
    return url, commit


def _custom_nodes_dirs() -> list:
    try:
        import folder_paths  # ComfyUI-provided
        return [Path(p) for p in folder_paths.get_folder_paths("custom_nodes")]
    except Exception:  # noqa: BLE001 - outside ComfyUI
        return []


def local_pack_source(class_type: str, *, folder_of=None, roots=None):
    """{"folder", "repo" (GitHub owner/repo or None), "commit" (or None), "remote" (True when origin is any URL)}
    for the custom_nodes folder that provides *class_type* in this ComfyUI, or None. No URL is returned."""
    folder = (folder_of or local_pack_folder)(class_type)
    if not folder or "/" in folder or "\\" in folder or folder in (".", ".."):
        return None
    for root in (roots if roots is not None else _custom_nodes_dirs()):
        path = Path(root) / folder
        if path.is_dir():
            url, commit = git_origin_and_commit(path)
            repo = github_repo_of_remote(url)
            return {"folder": folder, "repo": repo if repo and not _refused(repo) else None, "commit": commit,
                    "remote": bool(url)}
    return {"folder": folder, "repo": None, "commit": None, "remote": False}


def _catalog(path=None) -> dict:
    """{lower repo: {"repo", "commit", "licence", "licence_class"}} from runtime/catalog/catalog.json (cached)."""
    path = Path(path) if path else _RUNTIME_DIR / "catalog" / "catalog.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    with _cache_lock:
        hit = _catalog_cache.get(str(path))
    if hit and hit[0] == mtime:
        return hit[1]
    out = {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        for e in data.get("entries") or []:
            repo = e.get("repo")
            if not isinstance(repo, str) or not _REPO_RE.fullmatch(repo) or e.get("install_target") != "cloud":
                continue
            pin = e.get("pin") or {}
            lic = e.get("license") or {}
            commit = pin.get("commit") if isinstance(pin.get("commit"), str) and _SHA_RE.fullmatch(pin["commit"]) \
                else None
            spdx = lic.get("spdx") if isinstance(lic.get("spdx"), str) and _SPDX_RE.fullmatch(lic["spdx"]) else None
            cls = lic.get("class") if lic.get("class") in LICENCE_CLASSES else licence_class(spdx)
            out[repo.lower()] = {"repo": repo, "commit": commit, "licence": spdx, "licence_class": cls}
    except (OSError, ValueError, AttributeError):
        _log.warning("runtime catalog unreadable")
    with _cache_lock:
        _catalog_cache[str(path)] = (mtime, out)
    return out


def allowed_licence_classes(path=None) -> frozenset:
    """Licence classes the catalog's licence policy installs (runtime/catalog/profiles.json `licence_policy`:
    include / include-flagged): the same policy that decides what goes into the HaloFuse image."""
    path = Path(path) if path else _RUNTIME_DIR / "catalog" / "profiles.json"
    try:
        policy = json.loads(path.read_text(encoding="utf-8")).get("licence_policy") or {}
        allowed = frozenset(k for k, v in policy.items() if k in LICENCE_CLASSES and str(v).startswith("include"))
        return allowed or _ALLOWED_FALLBACK
    except (OSError, ValueError, AttributeError):
        return _ALLOWED_FALLBACK


def _snapshot_profiles() -> list:
    from . import workflow_analysis as wa
    names = []
    try:
        index = json.loads((wa.SNAPSHOT_DIR / "index.json").read_text(encoding="utf-8"))
        names = [n for n in (index.get("profiles") or {}) if isinstance(n, str)]
    except (OSError, ValueError, AttributeError):
        pass
    return names or ["default", "full"]


def catalog_pack_for_class(class_type: str, *, snapshots=None):
    """{"repo", "commit", "licence", "licence_class"} of the catalog pack that provides *class_type* in any shipped
    image snapshot (every profile, so also packs outside the selected one), or None."""
    from . import workflow_analysis as wa

    if snapshots is None:
        snapshots = []
        for profile in _snapshot_profiles():
            try:
                snap = wa.load_snapshot(profile)
            except ValueError:
                snap = None
            if snap is not None:
                snapshots.append(snap)
    for snap in snapshots:
        pack = snap.pack_of.get(class_type)
        repo = (pack or {}).get("repo")
        if isinstance(repo, str) and _REPO_RE.fullmatch(repo) and not _refused(repo):
            hit = _catalog().get(repo.lower())
            return dict(hit) if hit else {"repo": repo, "commit": None, "licence": None, "licence_class": None}
    return None


def _manager_map_files() -> list:
    files = []
    for root in _custom_nodes_dirs():
        files.append(root / "ComfyUI-Manager" / "extension-node-map.json")
        files.append(root / "comfyui-manager" / "extension-node-map.json")
    try:
        import folder_paths  # ComfyUI-provided
        user = Path(folder_paths.get_user_directory())
        for cache in (user / "__manager" / "cache", user / "default" / "ComfyUI-Manager" / "cache"):
            if cache.is_dir():
                files.extend(sorted(cache.glob("*extension-node-map.json"), reverse=True))
    except Exception:  # noqa: BLE001 - outside ComfyUI
        pass
    return [f for f in files if f.is_file()]


def manager_repo_for_class(class_type: str, *, files=None):
    """owner/repo (GitHub only) that ComfyUI-Manager's extension-node-map names for *class_type*. Files on disk only
    (the Manager's own copy or its cache); never fetched."""
    for f in (files if files is not None else _manager_map_files()):
        f = Path(f)
        try:
            key = (str(f), f.stat().st_mtime)
        except OSError:
            continue
        with _cache_lock:
            index = _map_cache.get(key)
        if index is None:
            index = {}
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                for url, value in (data.items() if isinstance(data, dict) else ()):
                    repo = github_repo_of_remote(url)
                    if not repo or not isinstance(value, list) or not value or not isinstance(value[0], list):
                        continue
                    for ct in value[0]:
                        if isinstance(ct, str):
                            index.setdefault(ct, repo)
            except (OSError, ValueError):
                index = {}
            with _cache_lock:
                _map_cache[key] = index
        repo = index.get(class_type)
        if repo and not _refused(repo):
            return repo
    return None


def _offer(repo: str, commit, source: str, licence_cls, **extra) -> dict:
    return {"id": repo, "name": repo.rsplit("/", 1)[-1], "repo": repo, "line": f"{repo}@{commit}" if commit else repo,
            "source": source, "licence_class": licence_cls, **extra}


def find_pack_for_class(class_type: str, *, local_source=None, catalog_lookup=None, manager_lookup=None,
                        registry_lookup=None):
    """The pack that provides *class_type* as an add offer {"id", "name", "repo", "line", "source", ...}: source
    "local" (this ComfyUI's folder with a GitHub remote, pinned to the local commit), "catalog", "manager", or the
    registry's shape; {"source": "local-only", "folder"} for a local folder without a GitHub remote (its code is never
    uploaded); None when nothing knows the class."""
    local = (local_source or local_pack_source)(class_type)
    if local and local.get("repo"):
        known = _catalog().get(local["repo"].lower()) or {}
        return _offer(local["repo"], local.get("commit"), "local", known.get("licence_class"), folder=local["folder"])
    cat = (catalog_lookup or catalog_pack_for_class)(class_type)
    if cat:
        return _offer(cat["repo"], cat.get("commit"), "catalog", cat.get("licence_class"))
    repo = (manager_lookup or manager_repo_for_class)(class_type)
    if repo:
        known = _catalog().get(repo.lower()) or {}
        return _offer(repo, known.get("commit"), "manager", known.get("licence_class"))
    if local:
        return {"source": "local-only", "folder": local["folder"]}
    return (registry_lookup or registry_pack_for_class)(class_type)


def _user_keys(entries: list) -> dict:
    keys = {}
    for e in entries:
        label = e.get("repo") if e["source"] == "github" else e["id"]
        names = [e.get("repo"), e.get("id")]
        for name in filter(None, names):
            keys[name.lower()] = label
            keys[name.lower().rsplit("/", 1)[-1]] = label
    return keys


def _problem(code, message, class_type=None, node_id=None) -> dict:
    return {"code": code, "message": message, "node_id": node_id, "class_type": class_type, "input": None}


LOCAL_ONLY_MESSAGE = ("{ct} comes from the folder {folder} in this computer's ComfyUI, which has no GitHub address. "
                      "HaloFuse never uploads code from your computer: publish the pack as a public GitHub repository "
                      "and add it under My custom nodes, or replace the node.")


def _auto_add(packs: dict, entries: list, save) -> tuple:
    """Setting auto_add_packs: resolve each offered pack through the public GitHub / Registry APIs (a private or
    missing repository fails there) and save it to `user_nodes` when its licence class is one the licence policy
    installs. Returns (class -> label added, refused [{"name", "why"}])."""
    allowed = allowed_licence_classes()
    added, refused, new = {}, [], list(entries)
    by_line: dict = {}
    for ct, pack in packs.items():
        by_line.setdefault(pack["line"], (pack, []))[1].append(ct)
    for line, (pack, classes) in by_line.items():
        if len(new) >= MAX_PACKS:
            refused.append({"name": pack["name"], "why": f"My custom nodes is full ({MAX_PACKS} packs)"})
            continue
        row = resolve_text(line)["rows"][0]
        if not row["ok"]:
            refused.append({"name": pack["name"], "why": row.get("message") or "it could not be checked"})
            continue
        entry = row["entry"]
        if entry.get("licence_class") not in allowed:
            refused.append({"name": pack["name"], "why": f"its licence ({entry.get('licence') or 'none found'}) is "
                                                         "not one HaloFuse adds by itself; add it yourself if its "
                                                         "terms suit you"})
            continue
        trial = new + [entry]
        if not valid_entries(trial):
            refused.append({"name": pack["name"], "why": "it is already in My custom nodes in another form"})
            continue
        new = trial
        for ct in classes:
            added[ct] = entry.get("repo") or entry.get("id")
    if added:
        save({"user_nodes": new})
    return added, refused


def apply_to_report(report: dict, workflows, settings: dict, *, lookup=None, local_folder=None, find=None,
                    save=None) -> dict:
    """Workflow Check: classes the runtime lacks but the artist's packs provide become notes (not verified). For the
    others the providing pack is found (find_pack_for_class) and offered (`add_packs`, one click, the blocking item
    marked fixable) or, with the setting auto_add_packs and a *save* callable, added to My custom nodes at once when
    it is public and its licence is allowed. A local folder without a GitHub remote stays blocked with the reason.
    Returns *report* (changed in place)."""
    local_folder = local_folder or local_pack_folder
    if find is None:
        registry = lookup or registry_pack_for_class

        def find(ct):
            return find_pack_for_class(ct, registry_lookup=registry,
                                       local_source=lambda c: local_pack_source(c, folder_of=local_folder))
    entries = entries_of(settings)
    supported, note = runtime_support(settings)
    if entries and not supported:
        report.setdefault("warnings", []).append(_problem(
            "user_nodes_unsupported", f"Your custom nodes are not installed in this runtime. {note}"))
    if not supported:
        return report
    unknown = [b for b in report.get("blocking", []) if b.get("code") == "unknown_node" and b.get("class_type")]
    if not unknown:
        return report
    keys = _user_keys(entries)
    provided, packs, local_only, added, refused = {}, {}, {}, {}, []
    deadline = time.monotonic() + LOOKUP_BUDGET_S
    for ct in list(dict.fromkeys(b["class_type"] for b in unknown))[:MAX_LOOKUPS]:
        folder = str(local_folder(ct) or "").lower()
        if folder and folder in keys:
            provided[ct] = keys[folder]
            continue
        if time.monotonic() > deadline:
            break
        pack = find(ct)
        if not pack:
            continue
        if pack.get("source") == "local-only":
            local_only[ct] = pack["folder"]
            continue
        repo = (pack.get("repo") or "").lower()
        label = keys.get(pack["id"].lower()) or (keys.get(repo) or keys.get(repo.rsplit("/", 1)[-1]) if repo else None)
        if label:
            provided[ct] = label
        else:
            packs[ct] = pack
    if packs and save is not None and settings.get("auto_add_packs") is True:
        added, refused = _auto_add(packs, entries, save)
        for ct in added:
            packs.pop(ct, None)
    if not (provided or packs or local_only or added or refused):
        return report
    blocking = []
    for b in report.get("blocking", []):
        ct = b.get("class_type")
        if b.get("code") == "unknown_node" and (ct in provided or ct in added):
            continue
        if b.get("code") == "unknown_node" and ct in packs:
            b = dict(b, message=f"{b['message']} It comes from the pack {packs[ct]['name']}: add it under My custom "
                                "nodes (one click below).", fixable=True,
                     fix={"kind": "add_pack", "line": packs[ct]["line"]})
        elif b.get("code") == "unknown_node" and ct in local_only:
            b = dict(b, code="node_local_only", message=LOCAL_ONLY_MESSAGE.format(ct=ct, folder=local_only[ct]),
                     fixable=False)
        blocking.append(b)
    report["blocking"] = blocking
    for ct, label in provided.items():
        report.setdefault("notes", []).append(_problem(
            "node_user_pack", f"{ct} comes from your custom node {label}: it is installed when the job starts (not "
                              "verified until the job runs).", ct))
    for ct, label in added.items():
        report.setdefault("notes", []).append(_problem(
            "node_pack_added", f"{ct}: its pack {label} was added to My custom nodes (setting: always add the packs "
                               "a workflow needs); it is installed when the job starts (not verified until the job "
                               "runs).", ct))
    for r in refused:
        report.setdefault("warnings", []).append(_problem(
            "pack_not_auto_added", f"{r['name']} was not added automatically: {r['why']}."))
    offers: dict = {}
    for ct, pack in packs.items():
        offers.setdefault(pack["id"], dict(pack, class_types=[]))["class_types"].append(ct)
    if offers:
        report["add_packs"] = list(offers.values())
    if provided or added:
        remaining = [b for b in blocking if b.get("code") in ("unknown_node", "node_local_only")]
        report["warnings"] = [w for w in report.get("warnings", []) if w.get("code") != "unknown_nodes_summary"]
        if remaining:
            listed = ", ".join(f"{b['class_type']} (node {b['node_id']})" for b in remaining[:20])
            report["warnings"].append(_problem("unknown_nodes_summary", f"Nodes not in the runtime: {listed}"))
    report["ok"] = not report["blocking"]
    return report

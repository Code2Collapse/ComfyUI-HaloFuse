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


def apply_to_report(report: dict, workflows, settings: dict, *, lookup=None, local_folder=None) -> dict:
    """Workflow Check: classes the runtime lacks but the artist's packs provide become notes (not verified); for
    the others the Comfy Registry pack is offered (`add_packs`). Returns *report* (changed in place)."""
    lookup = lookup or registry_pack_for_class
    local_folder = local_folder or local_pack_folder
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
    provided, packs = {}, {}
    deadline = time.monotonic() + LOOKUP_BUDGET_S
    for ct in list(dict.fromkeys(b["class_type"] for b in unknown))[:MAX_LOOKUPS]:
        folder = str(local_folder(ct) or "").lower()
        if folder and folder in keys:
            provided[ct] = keys[folder]
            continue
        if time.monotonic() > deadline:
            break
        pack = lookup(ct)
        if not pack:
            continue
        label = keys.get(pack["id"].lower()) or (keys.get(pack["repo"].lower()) if pack.get("repo") else None)
        if label:
            provided[ct] = label
        else:
            packs[ct] = pack
    if not provided and not packs:
        return report
    blocking = []
    for b in report.get("blocking", []):
        ct = b.get("class_type")
        if b.get("code") == "unknown_node" and ct in provided:
            continue
        if b.get("code") == "unknown_node" and ct in packs:
            b = dict(b, message=f"{b['message']} It comes from the pack {packs[ct]['name']}: add it under My custom "
                                "nodes.")
        blocking.append(b)
    report["blocking"] = blocking
    for ct, label in provided.items():
        report.setdefault("notes", []).append(_problem(
            "node_user_pack", f"{ct} comes from your custom node {label}: it is installed when the job starts (not "
                              "verified until the job runs).", ct))
    offers = {}
    for ct, pack in packs.items():
        offers.setdefault(pack["id"], dict(pack, class_types=[]))["class_types"].append(ct)
    if offers:
        report["add_packs"] = list(offers.values())
    if provided:
        remaining = [b for b in blocking if b.get("code") == "unknown_node"]
        report["warnings"] = [w for w in report.get("warnings", []) if w.get("code") != "unknown_nodes_summary"]
        if remaining:
            listed = ", ".join(f"{b['class_type']} (node {b['node_id']})" for b in remaining[:20])
            report["warnings"].append(_problem("unknown_nodes_summary", f"Nodes not in the runtime: {listed}"))
    report["ok"] = not report["blocking"]
    return report

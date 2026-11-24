#!/usr/bin/env python3
"""Install the custom nodes of one catalog profile at image build time.

Two steps, so the Dockerfile can cache them in separate layers:

  install_nodes.py fetch --profile P --dest DIR [--catalog-dir catalog] [--cache DIR]
                         [--report FILE] [--require-hashes]
      Downloads each pack of profile P at its pinned version (Comfy Registry
      version or GitHub archive at an exact commit), extracts it safely,
      checks the content hash recorded in the catalog, and writes a report.

  install_nodes.py deps --profile P --custom-nodes DIR --report FILE [--catalog-dir catalog]
      pip-installs each fetched pack's requirements.txt.

  install_nodes.py check-extras [--catalog-dir catalog]
      Validates catalog/extra_nodes.txt offline (syntax, packs the catalog keeps out of cloud images, refused
      repositories). CI runs it before any build.

Failure handling
  * strict profiles (`default`, the production image) fail the build on any
    error, exactly like the previous installer.
  * other profiles isolate a failing pack: it is left out of custom_nodes
    (moved to custom_nodes_disabled/) and marked "failed" in the report, and the
    build continues. Each optional pack is installed under a constraints file
    that freezes every package already installed, so a pack can add packages
    but can never change the version of one the runtime (or an earlier pack)
    already has. pip resolves before installing, so a refused pack leaves the
    environment as it was.

  * per-pack catalog fixes, non-strict profiles only (`install.requirement_overrides` rewrites one upstream
    requirement line, e.g. a numpy<2 cap the image moved past; `install.extra_requirements` adds a package the
    pack imports but never declared). pip installs a rewritten copy under the work dir; the pack's own files are
    never edited. A stale override (upstream changed the line) is not applied and is reported.

torch/torchvision/torchaudio are always constrained to the base image build.
No git binary is needed for the packs themselves; nothing here runs at
container start.
"""
from __future__ import annotations

import argparse
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
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hf_catalog  # noqa: E402  (same directory, stdlib only)

REGISTRY_API = "https://api.comfy.org/nodes/{id}/versions/{version}"
GITHUB_ZIP = "{url}/archive/{commit}.zip"
BASE_CONSTRAINTS = Path("/builder-scripts/constraints.txt")  # ships in the base image
TORCH_PKGS = ("torch", "torchvision", "torchaudio")
RETRIES = 3
MAX_ERROR = 300


class InstallError(RuntimeError):
    """A strict-profile pack failed, or the catalog is unusable."""


def log(msg):
    print(f"[install-nodes] {msg}", flush=True)


# ------------------------------------------------------------------ download

def urlopen_bytes(url: str) -> bytes:
    """Default opener: HTTPS GET with retries (build time only)."""
    if not url.startswith("https://"):
        raise OSError(f"refusing non-HTTPS download: {url}")
    last = None
    for attempt in range(1, RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "halofuse-runtime-build"})
            with urllib.request.urlopen(req, timeout=300) as resp:
                return resp.read()
        except Exception as err:  # noqa: BLE001 - retried, then re-raised with context
            last = err
            log(f"attempt {attempt}/{RETRIES} failed for {url}: {err}")
            time.sleep(5 * attempt)
    raise OSError(f"download failed after {RETRIES} attempts: {url}: {last}")


def _short(err) -> str:
    text = re.sub(r"(/[^\s:'\"]+)+/", "", str(err)).strip()  # strip absolute paths
    return text[-MAX_ERROR:]


# ------------------------------------------------------------------ archives

def extract_zip(blob: bytes, dest: Path) -> None:
    """Extract a zip into dest, stripping one wrapping top-level directory.

    Rejects absolute paths, '..' components, drive letters and symlinks.
    """
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = []
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            p = PurePosixPath(name)
            if name.startswith("/") or ".." in p.parts or re.match(r"^[A-Za-z]:", name):
                raise InstallError(f"unsafe path in archive: {info.filename!r}")
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise InstallError(f"symlink in archive: {info.filename!r}")
            names.append((info, p))
        tops = {p.parts[0] for _, p in names if p.parts}
        strip = len(tops) == 1 and all(len(p.parts) > 1 or info.is_dir() for info, p in names)
        if dest.exists():
            raise InstallError(f"destination already exists: {dest.name}")
        dest.mkdir(parents=True)
        for info, p in names:
            parts = p.parts[1:] if strip else p.parts
            if not parts or info.is_dir():
                continue
            target = dest.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)


def content_hash(folder: Path) -> str:
    """sha256 over the sorted (relative posix path, file sha256) list.

    Independent of archive compression, member order and timestamps, so it is
    stable even if GitHub regenerates archive zips.
    """
    h = hashlib.sha256()
    files = sorted(p for p in Path(folder).rglob("*") if p.is_file())
    for f in sorted(files, key=lambda q: q.relative_to(folder).as_posix()):
        rel = f.relative_to(folder).as_posix()
        h.update(rel.encode("utf-8") + b"\0" + hashlib.sha256(f.read_bytes()).hexdigest().encode() + b"\n")
    return h.hexdigest()


def cache_key(entry: dict) -> str:
    inst = entry["install"]
    if inst["method"] == "registry":
        return f"registry-{inst['registry_id']}-{entry['pin']['version']}"
    return f"git-{entry['repo'].replace('/', '__')}-{entry['pin']['commit']}"


def source_url(entry: dict, opener) -> str:
    """Archive URL for an installable entry (registry entries need one metadata request)."""
    inst = entry["install"]
    if inst["method"] == "registry":
        version = entry["pin"]["version"]
        meta = json.loads(opener(REGISTRY_API.format(id=inst["registry_id"], version=version)))
        url = meta.get("downloadUrl")
        if not url:
            raise InstallError(f"registry has no downloadUrl for {inst['registry_id']}@{version}")
        return url
    if inst["method"] == "git-archive":
        return GITHUB_ZIP.format(url=entry["url"], commit=entry["pin"]["commit"])
    raise InstallError(f"{entry['repo']}: install method {inst['method']} is not fetchable")


def _archive(entry, cache: Path | None, opener) -> bytes:
    """The pinned archive, from the BuildKit cache mount when present (content is re-verified anyway)."""
    key = cache_key(entry)
    if cache is not None and (cache / f"{key}.zip").is_file():
        return (cache / f"{key}.zip").read_bytes()
    blob = opener(source_url(entry, opener))
    if cache is not None:
        cache.mkdir(parents=True, exist_ok=True)
        tmp = cache / f".{key}.part"
        tmp.write_bytes(blob)
        os.replace(tmp, cache / f"{key}.zip")
    return blob


# ------------------------------------------------------------------ extra_nodes.txt (owner additions)

EXTRA_FILE = "extra_nodes.txt"
GITHUB_COMMIT_API = "https://api.github.com/repos/{repo}/commits/{ref}"
GITHUB_REPO_API = "https://api.github.com/repos/{repo}"
# SHA-256 of lowercase owner/repo; names are deliberately not written in public files.
REFUSED_REPO_SHA256 = frozenset({"cd86c77eade6596dffd7d42838ae75189b4ec746dcbf4cde1f5d0d7bb85cabc3"})
_PERMISSIVE = {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "Unlicense", "0BSD", "Zlib", "CC0-1.0",
               "BSL-1.0", "Python-2.0"}
_COPYLEFT = {"MPL-2.0", "EPL-2.0"}
_EXTRA_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]{0,99})/([A-Za-z0-9._-]{1,100})(?:@([A-Za-z0-9._/+-]{1,200}))?$")


def parse_extra_nodes(path) -> list:
    """`owner/repo[@ref]` per line; '#' starts a comment. Returns [(repo, ref or None)]."""
    out = []
    p = Path(path)
    if not p.is_file():
        return out
    for i, raw in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = _EXTRA_RE.match(line)
        if not m or ".." in line or m.group(2) in (".", ".."):
            raise InstallError(f"{EXTRA_FILE} line {i}: expected owner/repo or owner/repo@ref, got {line!r}")
        out.append((f"{m.group(1)}/{m.group(2)}", m.group(3)))
    return out


def _refused(repo: str) -> bool:
    return hashlib.sha256(repo.lower().encode("utf-8")).hexdigest() in REFUSED_REPO_SHA256


def _extra_lines(path) -> list:
    """[(line number, repo, ref)] of the active lines (parse_extra_nodes keeps its (repo, ref) contract)."""
    p = Path(path)
    if not p.is_file():
        return []
    numbered = [i for i, raw in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
                if raw.split("#", 1)[0].strip()]
    return [(n, repo, ref) for n, (repo, ref) in zip(numbered, parse_extra_nodes(p))]


def check_extras(catalog_dir) -> list:
    """Offline problems in extra_nodes.txt: syntax, refused repositories, catalog packs that never go to the cloud."""
    try:
        lines = _extra_lines(Path(catalog_dir) / EXTRA_FILE)
    except InstallError as exc:
        return [str(exc)]
    catalog = json.loads((Path(catalog_dir) / "catalog.json").read_text(encoding="utf-8"))
    by_repo = {e["repo"].lower(): e for e in catalog.get("entries", [])}
    problems = []
    for n, repo, _ref in lines:
        if _refused(repo):
            problems.append(f"{EXTRA_FILE} line {n}: this repository is not allowed in a HaloFuse image")
            continue
        e = by_repo.get(repo.lower())
        if e is None:
            continue
        comp = e.get("compatibility") or {}
        if e.get("install_target") != "cloud" or comp.get("compatible") is not True:
            problems.append(f"{EXTRA_FILE} line {n}: {e['repo']} is {e.get('install_target')} in the catalog "
                            f"({comp.get('notes') or e.get('category')}); it is never installed in a cloud image")
    return problems


def licence_class(spdx) -> str:
    if spdx is None:
        return "no-licence"
    s = str(spdx)
    if "-NC" in s.upper():
        return "non-commercial"
    if s in _PERMISSIVE:
        return "permissive"
    if s.startswith("AGPL-"):
        return "network-copyleft"
    if s.startswith(("GPL-", "LGPL-")) or s in _COPYLEFT:
        return "copyleft"
    return "unknown"


def _extra_licence(repo: str, opener) -> dict:
    """Licence of an extra pack from GitHub metadata; a failed lookup is recorded as unknown, never fatal."""
    try:
        meta = json.loads(opener(GITHUB_REPO_API.format(repo=repo)))
    except Exception:  # noqa: BLE001 - optional metadata
        return {"spdx": None, "class": "unknown"}
    lic = meta.get("license") if isinstance(meta, dict) else None
    spdx = lic.get("spdx_id") if isinstance(lic, dict) else None
    return {"spdx": spdx, "class": licence_class(spdx)}


def _resolve_commit(repo: str, ref, opener) -> str:
    if ref and re.fullmatch(r"[0-9a-f]{40}", ref):
        return ref
    meta = json.loads(opener(GITHUB_COMMIT_API.format(repo=repo, ref=ref or "HEAD")))
    sha = str(meta.get("sha") or "")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise InstallError(f"could not resolve {repo}@{ref or 'HEAD'} to a commit")
    return sha


def _fetch_extras(catalog_dir, dest: Path, report: dict, cache, opener) -> None:
    """Extras are always optional: a failure is recorded and the build goes on (also in strict profiles)."""
    have = {p["repo"].lower() for p in report["packs"]}
    for repo, ref in parse_extra_nodes(Path(catalog_dir) / EXTRA_FILE):
        if repo.lower() in have:
            log(f"extra {repo}: already in the profile, skipped")
            continue
        folder = repo.split("/", 1)[1]
        rec = {"repo": repo, "folder": folder, "method": "git-archive", "commit": None, "version": None,
               "status": "fetched", "content_sha256": None, "hash_verified": False, "error": None,
               "extra": True, "requested_ref": ref}
        try:
            commit = _resolve_commit(repo, ref, opener)
            rec["commit"] = commit
            fake = {"repo": repo, "url": f"https://github.com/{repo}", "install": {"method": "git-archive"},
                    "pin": {"commit": commit}}
            log(f"fetch extra {repo} -> {folder} @ {commit}")
            rec["license"] = _extra_licence(repo, opener)
            if rec["license"]["class"] != "permissive":
                # listing a pack in extra_nodes.txt is the owner's decision: installed, but flagged for the record
                report.setdefault("flagged", []).append(
                    {"repo": repo, "flag": f"extra pack licence {rec['license']['class']} "
                                           f"({rec['license']['spdx'] or 'none'})"})
            blob = _archive(fake, cache, opener)
            with tempfile.TemporaryDirectory(dir=dest.parent) as tmp:
                staged = Path(tmp) / folder
                extract_zip(blob, staged)
                rec["content_sha256"] = content_hash(staged)
                if (dest / folder).exists():
                    raise InstallError(f"folder {folder} already exists in custom_nodes")
                shutil.move(str(staged), str(dest / folder))
        except Exception as exc:  # noqa: BLE001
            rec["status"] = "failed"
            rec["error"] = _short(exc)
            log(f"FAILED extra {repo}: {rec['error']} (optional: the build continues)")
        report["packs"].append(rec)
        have.add(repo.lower())


# ------------------------------------------------------------------ fetch step

def _load(catalog_dir) -> tuple[dict, dict]:
    catalog, profiles = hf_catalog.load(catalog_dir)
    try:
        hf_catalog.validate(catalog, profiles)
    except hf_catalog.CatalogError as exc:
        raise InstallError(str(exc)) from exc
    return catalog, profiles


def fetch(catalog_dir, profile: str, dest: Path, cache: Path | None = None, opener=urlopen_bytes,
          require_hashes: bool = False) -> dict:
    catalog, profiles = _load(catalog_dir)
    problems = check_extras(catalog_dir)  # before any download
    if problems:
        raise InstallError("; ".join(problems))
    res = hf_catalog.resolve(catalog, profiles, profile)
    if res.target != "cloud":
        raise InstallError(f"profile {profile} is a local profile; it is never installed into an image")
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    report = {"schema": "halofuse.install_report/1", "profile": profile, "strict": res.strict,
              "catalog_sha256": hf_catalog.catalog_sha256(catalog_dir), "packs": [],
              "excluded": [{"repo": e["repo"], "reason": r} for e, r in res.excluded],
              "flagged": [{"repo": e["repo"], "flag": f} for e, f in res.flagged]}
    for entry in res.installed:
        folder = hf_catalog.folder_of(entry)
        rec = {"repo": entry["repo"], "folder": folder, "method": entry["install"]["method"],
               "commit": entry["pin"].get("commit"), "version": entry["pin"].get("version"),
               "status": "fetched", "content_sha256": None, "hash_verified": False, "error": None}
        log(f"fetch {entry['repo']} -> {folder} @ {rec['commit'] or rec['version']}")
        try:
            expected = entry["pin"].get("content_sha256")
            if require_hashes and not expected:
                raise InstallError("no content_sha256 recorded in the catalog (--require-hashes)")
            blob = _archive(entry, cache, opener)
            with tempfile.TemporaryDirectory(dir=dest.parent) as tmp:
                staged = Path(tmp) / folder
                extract_zip(blob, staged)
                digest = content_hash(staged)
                if expected and digest != expected:
                    raise InstallError(f"content hash mismatch (expected {expected[:12]}, got {digest[:12]})")
                rec["content_sha256"] = digest
                rec["hash_verified"] = bool(expected)
                shutil.move(str(staged), str(dest / folder))
        except Exception as exc:  # noqa: BLE001 - recorded per pack; strict profiles re-raise
            rec["status"] = "failed"
            rec["error"] = _short(exc)
            log(f"FAILED {entry['repo']}: {rec['error']}")
            if res.strict:
                raise InstallError(f"{entry['repo']}: {rec['error']}") from exc
        report["packs"].append(rec)
    if profiles["profiles"][profile].get("extras", True) is False:
        # the production-parity profile stays exactly the production node set (its snapshot describes that image)
        log(f"{EXTRA_FILE}: not applied to profile {profile} (extras: false)")
    else:
        _fetch_extras(catalog_dir, dest, report, cache, opener)
    return report


# ------------------------------------------------------------------ deps step

def run_pip(argv: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(argv, capture_output=True, text=True)
    sys.stdout.write(proc.stdout[-4000:])
    sys.stderr.write(proc.stderr[-4000:])
    return proc.returncode, proc.stdout, proc.stderr


def _freeze(pip) -> list[str]:
    code, out, err = pip([sys.executable, "-m", "pip", "freeze", "--all"])
    if code != 0:
        raise InstallError(f"pip freeze failed: {_short(err)}")
    return [l.strip() for l in out.splitlines() if re.match(r"^[A-Za-z0-9_.\-]+==[^\s;]+$", l.strip())]


def _torch_only(lines: list[str]) -> list[str]:
    return [l for l in lines if l.split("==")[0].lower() in TORCH_PKGS]


_req_key = hf_catalog.req_key


def apply_requirement_overrides(lines: list[str], overrides: list[dict]) -> tuple[list[str], list[str], list[str]]:
    """Rewrite the requirement lines named by catalog `install.requirement_overrides`.

    An override applies only when the pack still writes exactly its `upstream` line (comments, whitespace, name case
    and separators ignored); otherwise it is stale: not applied, reported, and the upstream line is installed as is.
    Returns (lines, applied, stale)."""
    out = list(lines)
    applied, stale = [], []
    for ov in overrides:
        name = re.sub(r"[-_.]+", "-", ov["name"]).lower()
        want = _req_key(ov["upstream"])
        hits = [i for i, l in enumerate(out) if l is not None and hf_catalog.req_name(l) == name]
        match = [i for i in hits if _req_key(out[i]) == want]
        if not match:
            found = "; ".join(out[i].split("#", 1)[0].strip() for i in hits) or "no such line"
            stale.append(f"{ov['upstream']} (requirements.txt has: {found})")
            continue
        for i in match:
            out[i] = ov["use"]
        applied.append(f"{ov['upstream']} -> {ov['use'] if ov['use'] is not None else '(dropped)'}")
    return [l for l in out if l is not None], applied, stale


def _requirements_for(rec: dict, req: Path, entry: dict | None, strict: bool, work: Path) -> Path:
    """The requirements file pip installs: the pack's own, or (non-strict profiles with catalog
    `requirement_overrides` / `extra_requirements`) a rewritten copy under work/<folder>/. The pack's files are never
    edited (its content hash stays valid). Strict profiles (`default`, production parity) always install upstream."""
    inst = (entry or {}).get("install") or {}
    overrides = inst.get("requirement_overrides") or []
    extras = inst.get("extra_requirements") or []
    if strict or not (overrides or extras):
        return req
    lines, applied, stale = apply_requirement_overrides(req.read_text(encoding="utf-8").splitlines(), overrides)
    added = []
    for ex in extras:
        have = [hf_catalog.req_name(l) for l in lines]
        if hf_catalog.req_name(ex["requirement"]) in have:
            stale.append(f"{ex['requirement']} (already required upstream: {hf_catalog.req_name(ex['requirement'])})")
            continue
        lines.append(ex["requirement"])
        added.append(ex["requirement"])
    rec["requirement_overrides"] = {"applied": applied, "added": added, "stale": stale}
    for s in stale:
        log(f"override for {rec['repo']} is STALE, installed as upstream wrote it: {s}")
    if not (applied or added):
        return req
    log(f"requirement overrides for {rec['repo']}: {', '.join(applied + [f'+ {a}' for a in added])}")
    out = work / rec["folder"] / "requirements.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def install_deps(catalog_dir, profile: str, custom_nodes: Path, report: dict, pip=run_pip,
                 work: Path | None = None, base_constraints: Path = BASE_CONSTRAINTS) -> dict:
    catalog, profiles = _load(catalog_dir)
    res = hf_catalog.resolve(catalog, profiles, profile)
    by_repo = hf_catalog.entries_by_repo(catalog)
    custom_nodes = Path(custom_nodes)
    disabled = custom_nodes.parent / "custom_nodes_disabled"
    work = Path(work or tempfile.mkdtemp(prefix="hf-deps-"))
    work.mkdir(parents=True, exist_ok=True)
    constraints = work / "constraints.txt"

    if base_constraints.is_file():
        torch_pins = _torch_only(base_constraints.read_text().split())
    else:
        torch_pins = _torch_only(_freeze(pip))
    log(f"torch constraints: {', '.join(torch_pins) or 'NONE FOUND'}")

    for rec in report["packs"]:
        if rec["status"] != "fetched":
            continue
        req = custom_nodes / rec["folder"] / "requirements.txt"
        entry = None if rec.get("extra") else by_repo.get(rec["repo"])
        if not req.is_file():
            # a pack without requirements.txt (its install.py, which archive installs never run, names the packages):
            # catalog `extra_requirements` still apply in non-strict profiles, from an empty upstream list
            if res.strict or not ((entry or {}).get("install") or {}).get("extra_requirements"):
                rec["status"] = "installed"
                continue
            req = work / rec["folder"] / "requirements.upstream-none.txt"
            req.parent.mkdir(parents=True, exist_ok=True)
            req.write_text("", encoding="utf-8")
        frozen = torch_pins if res.strict else sorted(set(_freeze(pip)) | set(torch_pins))
        constraints.write_text("\n".join(frozen) + "\n")
        req = _requirements_for(rec, req, entry, res.strict, work)
        argv = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
                "--progress-bar", "off", "-r", str(req), "-c", str(constraints)]
        log(f"pip install -r {rec['folder']}/requirements.txt")
        code, _out, err = pip(argv)
        if code == 0:
            rec["status"] = "installed"
            continue
        rec["status"] = "failed"
        rec["error"] = _short(err.strip().splitlines()[-1] if err.strip() else f"pip exit {code}")
        if res.strict and not rec.get("extra"):              # extras are optional even in strict profiles
            raise InstallError(f"{rec['repo']} ({rec['folder']}): requirements failed: {rec['error']}")
        disabled.mkdir(parents=True, exist_ok=True)
        shutil.move(str(custom_nodes / rec["folder"]), str(disabled / rec["folder"]))
        log(f"isolated {rec['repo']}: moved to custom_nodes_disabled/ ({rec['error']})")
    installed = sorted(p.name for p in custom_nodes.iterdir() if p.is_dir())
    log(f"installed nodes: {', '.join(installed)}")
    return report


# ------------------------------------------------------------------ CLI

def main(argv=None) -> int:
    default_catalog = str(Path(__file__).resolve().parents[1] / "catalog")
    ap = argparse.ArgumentParser(description="Install the custom nodes of one catalog profile.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--catalog-dir", default=default_catalog)
    f.add_argument("--profile", required=True)
    f.add_argument("--dest", required=True)
    f.add_argument("--cache")
    f.add_argument("--report", required=True)
    f.add_argument("--require-hashes", action="store_true")
    d = sub.add_parser("deps")
    d.add_argument("--catalog-dir", default=default_catalog)
    d.add_argument("--profile", required=True)
    d.add_argument("--custom-nodes", required=True)
    d.add_argument("--report", required=True)
    h = sub.add_parser("content-hash")
    h.add_argument("folder")
    c = sub.add_parser("check-extras")
    c.add_argument("--catalog-dir", default=default_catalog)
    args = ap.parse_args(argv)
    if args.cmd == "check-extras":
        problems = check_extras(args.catalog_dir)
        for prob in problems:
            log(f"ERROR: {prob}")
        if problems:
            return 1
        n = len(_extra_lines(Path(args.catalog_dir) / EXTRA_FILE))
        log(f"{EXTRA_FILE} OK: {n} line(s)")
        return 0
    try:
        if args.cmd == "fetch":
            rep = fetch(args.catalog_dir, args.profile, Path(args.dest),
                        cache=Path(args.cache) if args.cache else None, require_hashes=args.require_hashes)
            Path(args.report).write_text(json.dumps(rep, indent=1))
        elif args.cmd == "deps":
            rep = json.loads(Path(args.report).read_text())
            rep = install_deps(args.catalog_dir, args.profile, Path(args.custom_nodes), rep)
            Path(args.report).write_text(json.dumps(rep, indent=1))
        else:
            print(content_hash(Path(args.folder)))
            return 0
    except InstallError as exc:
        log(f"ERROR: {exc}")
        return 1
    failed = [p["repo"] for p in rep["packs"] if p["status"] == "failed"]
    log(f"{len(rep['packs']) - len(failed)} ok, {len(failed)} failed{': ' + ', '.join(failed) if failed else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

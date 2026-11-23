#!/usr/bin/env python3
"""Add a custom node pack to the HaloFuse image in one command (maintainer tool; Python stdlib, network).

  python runtime/scripts/add_node.py <github url | owner/repo[@ref] | registry id[@version]> [--profile full]
                                     [--catalog-dir runtime/catalog] [--dry-run]

Uses the catalog's own tooling (install_nodes.py / hf_catalog.py, not modified): the pack is pinned to an exact
commit and its licence read from GitHub exactly as the image build does for extras, then appended as
`owner/repo@<commit>` (with a dated licence comment) to catalog/extra_nodes.txt - the catalog's place for additions
without editing JSON. Then check_extras and the catalog validation run; on any problem the file is restored.
A Comfy Registry id is mapped to its GitHub repository (extras are GitHub archives). A pack the profile already has,
or a line already in the file, is reported and nothing changes. Commit the file; CI builds the image on push.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hf_catalog  # noqa: E402
import install_nodes  # noqa: E402

REGISTRY_NODE_API = "https://api.comfy.org/nodes/{id}"
_REPO = r"([A-Za-z0-9][A-Za-z0-9._-]{0,99})/([A-Za-z0-9][A-Za-z0-9._-]{0,99})"
_REF = r"[A-Za-z0-9][A-Za-z0-9._/+-]{0,199}"
_GH_URL_RE = re.compile(rf"^(?:https://)?(?:www\.)?github\.com/{_REPO}?(?:\.git)?(?:/tree/({_REF}))?/?(?:@({_REF}))?$")
_PLAIN_RE = re.compile(rf"^{_REPO}(?:@({_REF}))?$")
_REGISTRY_URL_RE = re.compile(r"^https://registry\.comfy\.org/nodes/([A-Za-z0-9][A-Za-z0-9._-]{0,99})/?$")
_ID_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]{0,99})(?:@([A-Za-z0-9][A-Za-z0-9.+_-]{0,63}))?$")


class AddNodeError(RuntimeError):
    pass


def parse_spec(spec: str):
    """("github", "owner/repo", ref or None) or ("registry", id, version or None)."""
    s = (spec or "").strip()
    if not s or ".." in s or len(s) > 400:
        raise AddNodeError(f"not a GitHub repository or registry id: {spec!r}")
    m = _GH_URL_RE.fullmatch(s)
    if m:
        repo = f"{m.group(1)}/{m.group(2)}"
        if repo.endswith(".git"):
            repo = repo[:-4]
        return "github", repo, m.group(4) or m.group(3)
    if "://" in s or s.startswith(("github.com", "www.")):
        m = _REGISTRY_URL_RE.fullmatch(s)
        if m:
            return "registry", m.group(1), None
        raise AddNodeError(f"only https GitHub or Comfy Registry addresses are accepted: {spec!r}")
    m = _PLAIN_RE.fullmatch(s)
    if m:
        return "github", f"{m.group(1)}/{m.group(2)}", m.group(3)
    m = _ID_RE.fullmatch(s)
    if m:
        return "registry", m.group(1), m.group(2)
    raise AddNodeError(f"not a GitHub repository or registry id: {spec!r}")


def registry_repo(rid: str, opener) -> str:
    try:
        meta = json.loads(opener(REGISTRY_NODE_API.format(id=urllib.parse.quote(rid, safe=""))))
    except Exception as exc:  # noqa: BLE001
        raise AddNodeError(f"registry id {rid}: not found in the Comfy Registry ({exc})") from None
    url = str((meta or {}).get("repository") or "") if isinstance(meta, dict) else ""
    m = _GH_URL_RE.fullmatch(url.rstrip("/"))
    if not m:
        raise AddNodeError(f"registry id {rid}: its repository is not on GitHub; extras are GitHub archives")
    repo = f"{m.group(1)}/{m.group(2)}"
    return repo[:-4] if repo.endswith(".git") else repo


def _pin(repo: str, refs, opener) -> str:
    last = None
    for ref in refs:
        try:
            return install_nodes._resolve_commit(repo, ref, opener)
        except Exception as exc:  # noqa: BLE001 - try the next candidate ref
            last = exc
    raise AddNodeError(f"{repo}: could not resolve {' / '.join(r or 'HEAD' for r in refs)} to a commit "
                       f"(repository missing or private, or the ref does not exist): {last}")


def add(spec: str, catalog_dir, profile: str = "full", *, opener=None, today: str | None = None,
        dry_run: bool = False) -> dict:
    opener = opener or install_nodes.urlopen_bytes
    catalog_dir = Path(catalog_dir)
    kind, name, ref = parse_spec(spec)
    try:
        catalog, profiles = install_nodes._load(catalog_dir)
    except install_nodes.InstallError as exc:
        raise AddNodeError(f"the catalog is invalid before the change: {exc}") from None
    prof = (profiles.get("profiles") or {}).get(profile)
    if prof is None:
        raise AddNodeError(f"unknown profile {profile!r}")
    if prof.get("extras", True) is False:
        raise AddNodeError(f"profile {profile} takes no extras (extras: false); it is frozen to its own node set")
    if prof.get("target") != "cloud":
        raise AddNodeError(f"profile {profile} is not built into an image")

    registry_id = name if kind == "registry" else None
    repo = registry_repo(name, opener) if kind == "registry" else name
    refs = [ref] if kind == "github" else ([ref, f"v{ref}"] if ref else [None])
    if install_nodes._refused(repo):
        raise AddNodeError("this repository is not allowed in a HaloFuse image")
    result = {"repo": repo, "profile": profile, "commit": None, "licence": None, "licence_class": None}

    res = hf_catalog.resolve(catalog, profiles, profile)
    if repo.lower() in {r.lower() for r in res.repos()}:
        return dict(result, action="already_in_profile")
    extra_file = catalog_dir / install_nodes.EXTRA_FILE
    for _n, listed, listed_ref in install_nodes._extra_lines(extra_file):
        if listed.lower() == repo.lower():
            return dict(result, action="already_listed", commit=listed_ref)
    entry = hf_catalog.entries_by_repo(catalog).get(repo) or next(
        (e for e in catalog.get("entries", []) if e["repo"].lower() == repo.lower()), None)
    if entry is not None:
        comp = entry.get("compatibility") or {}
        if entry.get("install_target") != "cloud" or comp.get("compatible") is not True:
            raise AddNodeError(f"{entry['repo']} is {entry.get('install_target')} in the catalog "
                               f"({comp.get('notes') or entry.get('category')}); it is never installed in a cloud image")

    commit = _pin(repo, refs, opener)
    lic = install_nodes._extra_licence(repo, opener)
    result.update(commit=commit, licence=lic["spdx"], licence_class=lic["class"])
    notes = [f"{lic['spdx'] or 'no licence detected'} ({lic['class']})"]
    if ref:
        notes.append(f"requested {ref}")
    if registry_id:
        notes.append(f"registry id {registry_id}")
    when = today or dt.date.today().isoformat()
    block = f"# add_node.py {when}: {'; '.join(notes)}\n{repo}@{commit}\n"
    if dry_run:
        return dict(result, action="dry_run", line=f"{repo}@{commit}")

    original = extra_file.read_text(encoding="utf-8") if extra_file.is_file() else ""
    extra_file.write_text((original.rstrip("\n") + "\n\n" if original.strip() else "") + block, encoding="utf-8")
    try:
        problems = install_nodes.check_extras(catalog_dir)
        install_nodes._load(catalog_dir)
    except install_nodes.InstallError as exc:
        problems = [str(exc)]
    if problems:
        extra_file.write_text(original, encoding="utf-8")
        raise AddNodeError("validation failed, extra_nodes.txt restored: " + "; ".join(problems))
    return dict(result, action="added", line=f"{repo}@{commit}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Add a custom node pack to the HaloFuse image (extra_nodes.txt).")
    ap.add_argument("spec", help="https://github.com/owner/repo, owner/repo[@ref] or a Comfy Registry id[@version]")
    ap.add_argument("--profile", default="full", help="image profile that must take the pack (default: full)")
    ap.add_argument("--catalog-dir", default=str(Path(__file__).resolve().parents[1] / "catalog"))
    ap.add_argument("--dry-run", action="store_true", help="resolve and print, write nothing")
    args = ap.parse_args(argv)
    try:
        res = add(args.spec, args.catalog_dir, args.profile, opener=install_nodes.urlopen_bytes,
                  dry_run=args.dry_run)
    except AddNodeError as exc:
        print(f"[add-node] ERROR: {exc}")
        return 1
    if res["action"] == "already_in_profile":
        print(f"[add-node] {res['repo']} is already in profile {res['profile']}; nothing to do")
    elif res["action"] == "already_listed":
        print(f"[add-node] {res['repo']} is already in {install_nodes.EXTRA_FILE} (@{res['commit']}); nothing to do")
    else:
        verb = "would add" if res["action"] == "dry_run" else "added to"
        print(f"[add-node] {verb} {install_nodes.EXTRA_FILE}: {res['line']}")
        print(f"[add-node] licence: {res['licence'] or 'none detected'} ({res['licence_class']})"
              + ("" if res["licence_class"] == "permissive" else " - flagged in the install report; your decision"))
        if res["action"] == "added":
            print("[add-node] extra_nodes.txt and the catalog validate. Commit it; CI builds the image on push, then "
                  "scripts/update_snapshot.py --write-contract installs its node list.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

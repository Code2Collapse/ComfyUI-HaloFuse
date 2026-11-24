#!/usr/bin/env python3
"""Custom-node catalog: load, validate and resolve runtime profiles.

Stdlib only: this module runs inside the image build (python3.13 of the base
image, no extra packages) and in the unit tests.

Files (catalog/):
  catalog.json   every repository from the work order, one entry each
  profiles.json  profile definitions + the licence policy

A profile resolves to the entries that are installed into an image built for
it, plus the entries that were excluded and why. Nothing here touches the
network or the filesystem beyond reading the two JSON files.

CLI:
  hf_catalog.py validate [--catalog-dir catalog]
  hf_catalog.py resolve <profile> [--catalog-dir catalog] [--json]
  hf_catalog.py summary [--catalog-dir catalog]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA = "halofuse.catalog/1"
PROFILES_SCHEMA = "halofuse.profiles/1"

# The 16 fields NEW_WORK section 9 requires for every entry.
REQUIRED_FIELDS = (
    "repo", "url", "category", "backend_frontend", "install_target", "install",
    "requirements", "system_deps", "model_download", "license", "compatibility",
    "conflicts", "profiles", "enabled_by_default", "pin", "verified",
)
CATEGORIES = (
    "backend",          # backend custom-node extension
    "frontend",         # frontend-only extension
    "combined",         # combined frontend/backend extension
    "core",             # ComfyUI core
    "research",         # standalone research/model repository
    "workflows",        # workflow or recipe collection
    "external-tool",    # optional external tool
    "incompatible",     # incompatible or unsupported
    "reference-only",   # reference-only
)
BACKEND_FRONTEND = ("backend", "frontend", "both", "none", "unknown")
INSTALL_TARGETS = ("cloud", "local", "reference-only")
INSTALL_METHODS = ("registry", "git-archive", "base-image", "local-user", "none")
LICENCE_CLASSES = (
    "permissive", "copyleft", "network-copyleft", "non-commercial",
    "restrictive-custom", "no-licence", "unknown",
)
POLICY_ACTIONS = ("include", "include-flagged", "exclude-pending-owner")
COMPATIBLE_VALUES = (True, False, "unknown")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
FOLDER_RE = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_.-]*$")
INSTALLABLE_CATEGORIES = ("backend", "combined")
REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
PROTECTED_REQS = ("torch", "torchvision", "torchaudio")  # always the base image build; never overridden


def req_name(line: str) -> str | None:
    """PEP 503 normalised project name of a plain requirement line (None for options, URLs, paths, blanks)."""
    text = str(line).split("#", 1)[0].strip()
    m = REQ_NAME_RE.match(text)
    if not m or "://" in text or text.startswith(("-", ".", "/")):
        return None
    return re.sub(r"[-_.]+", "-", m.group(1)).lower()


def req_key(line: str) -> str:
    """Comparable form of a requirement line: comment and whitespace removed, the name normalised."""
    text = str(line).split("#", 1)[0]
    text = re.sub(r"\s+", "", text)
    name = req_name(text)
    if name is None:
        return text
    return name + text[REQ_NAME_RE.match(text).end():].lower()


def _validate_requirement_overrides(overrides) -> list[str]:
    """`install.requirement_overrides`: each rewrites ONE upstream requirement line of the pack, in non-strict
    profiles only (install_nodes.py). {name, upstream, use: str | null (drop), reason}."""
    if not isinstance(overrides, list):
        return ["install.requirement_overrides must be a list"]
    errs = []
    for i, ov in enumerate(overrides):
        where = f"install.requirement_overrides[{i}]"
        if not isinstance(ov, dict):
            errs.append(f"{where} must be an object")
            continue
        name = ov.get("name")
        norm = re.sub(r"[-_.]+", "-", str(name or "")).lower()
        if not name or req_name(str(name)) != norm:
            errs.append(f"{where}: name must be a plain package name")
            continue
        if norm in PROTECTED_REQS:
            errs.append(f"{where}: {norm} is never overridden (torch stays the base image build)")
        if len(str(ov.get("reason") or "").strip()) < 10:
            errs.append(f"{where}: a reason (why the upstream requirement can be changed) is required")
        if req_name(str(ov.get("upstream") or "")) != norm:
            errs.append(f"{where}: upstream must be the pack's own requirement line for {name}")
        if "use" not in ov:
            errs.append(f"{where}: use is required (a requirement, or null to drop the line)")
        elif ov["use"] is not None:
            use = str(ov["use"])
            if not _plain_requirement(use):
                errs.append(f"{where}: use must be a plain requirement (no options, URLs or markers)")
            elif req_name(use) != norm:
                errs.append(f"{where}: use must name the same package as name ({name}), not {use}")
    return errs


def _plain_requirement(text: str) -> bool:
    return req_name(text) is not None and not any(t in text for t in ("://", "@", " -", ";", "#"))


def _validate_extra_requirements(extras) -> list[str]:
    """`install.extra_requirements`: packages the pack imports at load time but its requirements.txt omits; added in
    non-strict profiles only (install_nodes.py). {requirement, reason}."""
    if not isinstance(extras, list):
        return ["install.extra_requirements must be a list"]
    errs = []
    for i, ex in enumerate(extras):
        where = f"install.extra_requirements[{i}]"
        if not isinstance(ex, dict):
            errs.append(f"{where} must be an object")
            continue
        req = str(ex.get("requirement") or "")
        if not _plain_requirement(req):
            errs.append(f"{where}: requirement must be a plain requirement (no options, URLs or markers)")
        elif req_name(req) in PROTECTED_REQS:
            errs.append(f"{where}: {req_name(req)} is never added (torch stays the base image build)")
        if len(str(ex.get("reason") or "").strip()) < 10:
            errs.append(f"{where}: a reason (which import needs it) is required")
    return errs


class CatalogError(ValueError):
    """The catalog or profile file is invalid. The message lists every problem."""


@dataclass
class Resolved:
    profile: str
    target: str                                   # "cloud" or "local"
    installed: list = field(default_factory=list)  # catalog entries, catalog order
    excluded: list = field(default_factory=list)   # (entry, reason)
    flagged: list = field(default_factory=list)    # (entry, flag) installed but flagged for the owner
    strict: bool = False                           # any install failure fails the build

    def repos(self):
        return [e["repo"] for e in self.installed]


# ---------------------------------------------------------------- loading

def load(catalog_dir) -> tuple[dict, dict]:
    catalog_dir = Path(catalog_dir)
    catalog = json.loads((catalog_dir / "catalog.json").read_text(encoding="utf-8"))
    profiles = json.loads((catalog_dir / "profiles.json").read_text(encoding="utf-8"))
    return catalog, profiles


def catalog_sha256(catalog_dir) -> str:
    """sha256 of catalog.json with LF line endings (a Windows checkout may hold CRLF)."""
    data = (Path(catalog_dir) / "catalog.json").read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def entries_by_repo(catalog: dict) -> dict:
    return {e["repo"]: e for e in catalog["entries"]}


# ---------------------------------------------------------------- validation

def _is_unknown(value) -> bool:
    return isinstance(value, dict) and value.get("value") == "unknown" and bool(value.get("reason"))


def validate_entry(entry: dict) -> list[str]:
    """Return a list of problems for one entry (empty when valid)."""
    name = entry.get("repo", "<missing repo>")
    errs = []

    def err(msg):
        errs.append(f"{name}: {msg}")

    for key in REQUIRED_FIELDS:
        if key not in entry:
            err(f"missing field {key!r}")
    if errs:
        return errs
    if not REPO_RE.match(entry["repo"]):
        err("repo must be owner/name")
    if not str(entry["url"]).startswith("https://github.com/"):
        err("url must be an https://github.com/ URL")
    if entry["category"] not in CATEGORIES:
        err(f"category {entry['category']!r} not in {CATEGORIES}")
    if entry["backend_frontend"] not in BACKEND_FRONTEND:
        err(f"backend_frontend {entry['backend_frontend']!r} invalid")
    if entry["install_target"] not in INSTALL_TARGETS:
        err(f"install_target {entry['install_target']!r} invalid")

    inst = entry["install"]
    method = inst.get("method") if isinstance(inst, dict) else None
    if method not in INSTALL_METHODS:
        err(f"install.method {method!r} invalid")
    elif method == "registry":
        if (not inst.get("registry_id") or not (entry["pin"] or {}).get("version")
                or not FOLDER_RE.match(inst.get("folder", ""))):
            err("registry install needs install.registry_id, pin.version and a safe folder")
    elif method == "git-archive":
        if not COMMIT_RE.match(entry["pin"].get("commit") or ""):
            err("git-archive install needs pin.commit (40 hex)")
        if not FOLDER_RE.match(inst.get("folder", "")):
            err("git-archive install needs a safe folder name")
    if entry["install_target"] == "cloud" and method not in ("registry", "git-archive", "base-image"):
        err("a cloud entry must be installable (registry, git-archive or base-image)")
    if isinstance(inst, dict) and "requirement_overrides" in inst:
        for msg in _validate_requirement_overrides(inst["requirement_overrides"]):
            err(msg)
    if isinstance(inst, dict) and "extra_requirements" in inst:
        for msg in _validate_extra_requirements(inst["extra_requirements"]):
            err(msg)

    for key in ("requirements", "system_deps", "model_download"):
        val = entry[key]
        if val in (None, "", []) and key != "system_deps":
            err(f"{key} is empty; use {{'value': 'unknown', 'reason': ...}}")
        if isinstance(val, dict) and val.get("value") == "unknown" and not val.get("reason"):
            err(f"{key} is unknown without a reason")

    lic = entry["license"]
    if not isinstance(lic, dict) or lic.get("class") not in LICENCE_CLASSES:
        err(f"license.class must be one of {LICENCE_CLASSES}")
    elif not lic.get("source"):
        err("license.source (where the licence was read) is required")

    comp = entry["compatibility"]
    if not isinstance(comp, dict) or comp.get("compatible") not in COMPATIBLE_VALUES:
        err("compatibility.compatible must be true, false or 'unknown'")
    elif comp.get("compatible") is not True and not comp.get("notes"):
        err("compatibility.notes must explain a false/unknown verdict")

    if not isinstance(entry["conflicts"], dict):
        err("conflicts must be an object {conflicts_with, overlaps, notes}")
    if not isinstance(entry["profiles"], list):
        err("profiles must be a list")
    if not isinstance(entry["enabled_by_default"], bool):
        err("enabled_by_default must be a boolean")

    pin = entry["pin"]
    if not isinstance(pin, dict):
        err("pin must be an object")
    else:
        if pin.get("commit") is not None and not COMMIT_RE.match(pin["commit"]):
            err("pin.commit must be 40 hex or null")
        if pin.get("content_sha256") is not None and not SHA256_RE.match(pin["content_sha256"]):
            err("pin.content_sha256 must be 64 hex or null")
        if entry["install_target"] != "reference-only" and method in ("registry", "git-archive"):
            if not (pin.get("commit") or pin.get("version")):
                err("an installable entry needs pin.commit or pin.version")
        if pin.get("commit") is None and pin.get("version") is None and not pin.get("reason"):
            err("pin without commit/version needs a reason")
    ver = entry["verified"]
    if not isinstance(ver, dict) or not ver.get("status"):
        err("verified.status is required")
    return errs


def _validate_frozen_pins(name: str, d: dict, by_repo: dict) -> list[str]:
    """`pins` freezes a members profile at exact pins (the `default` = production-parity image), so the catalog
    entries themselves can move to newer pins for every other profile."""
    pins = d.get("pins")
    if pins is None:
        return []
    errs = []
    if not isinstance(pins, dict) or d.get("members") is None:
        return [f"profile {name}: pins needs an explicit members list and must be an object"]
    for repo, pin in pins.items():
        if repo not in (d.get("members") or []):
            errs.append(f"profile {name}: pin for {repo!r}, which is not a member")
            continue
        method = ((by_repo.get(repo) or {}).get("install") or {}).get("method")
        if not isinstance(pin, dict):
            errs.append(f"profile {name}: pin for {repo} must be an object")
            continue
        if pin.get("commit") is not None and not COMMIT_RE.match(str(pin["commit"])):
            errs.append(f"profile {name}: pin.commit of {repo} must be 40 hex or null")
        if pin.get("content_sha256") is not None and not SHA256_RE.match(str(pin["content_sha256"])):
            errs.append(f"profile {name}: pin.content_sha256 of {repo} must be 64 hex or null")
        if method == "git-archive" and not pin.get("commit"):
            errs.append(f"profile {name}: frozen git-archive pin of {repo} needs a commit")
        if method == "registry" and not pin.get("version"):
            errs.append(f"profile {name}: frozen registry pin of {repo} needs a version")
    return errs


def _with_pin(entry: dict, frozen: dict | None, profile: str) -> dict:
    """The entry as a profile installs it: a frozen profile pin replaces the catalog pin (a copy; the catalog is
    never changed)."""
    if not frozen:
        return entry
    pin = {**entry["pin"], "commit": frozen.get("commit"), "version": frozen.get("version"),
           "content_sha256": frozen.get("content_sha256"), "pin_source": f"frozen by profile {profile}"}
    return {**entry, "pin": pin}


def validate(catalog: dict, profiles: dict) -> None:
    """Raise CatalogError listing every problem, or return None."""
    errs = []
    if catalog.get("schema") != SCHEMA:
        errs.append(f"catalog schema must be {SCHEMA!r}")
    if profiles.get("schema") != PROFILES_SCHEMA:
        errs.append(f"profiles schema must be {PROFILES_SCHEMA!r}")
    entries = catalog.get("entries") or []
    seen = set()
    folders = {}
    for e in entries:
        errs.extend(validate_entry(e))
        r = e.get("repo")
        if r in seen:
            errs.append(f"{r}: duplicate entry")
        seen.add(r)
        folder = (e.get("install") or {}).get("folder")
        if folder and e.get("install_target") == "cloud":
            key = folder.lower()
            if key in folders:
                errs.append(f"{r}: folder {folder!r} also used by {folders[key]}")
            folders[key] = r
    defs = profiles.get("profiles") or {}
    for e in entries:
        for p in e.get("profiles") or []:
            if p not in defs:
                errs.append(f"{e.get('repo')}: unknown profile {p!r}")
    by_repo = {e.get("repo"): e for e in entries if isinstance(e, dict)}
    for name, d in defs.items():
        errs.extend(_validate_frozen_pins(name, d, by_repo))
        for ref in d.get("include_members_of") or []:
            if ref == name or (defs.get(ref) or {}).get("members") is None:
                errs.append(f"profile {name}: include_members_of {ref!r} must name another profile with members")
        if "try_unknown" in d:
            tries = d["try_unknown"]
            if not isinstance(tries, list) or not all(isinstance(r, str) for r in tries):
                errs.append(f"profile {name}: try_unknown must be a list of owner/repo strings")
            else:
                if d.get("members") is not None:
                    errs.append(f"profile {name}: try_unknown is not allowed on a members profile")
                if d.get("target") != "cloud":
                    errs.append(f"profile {name}: try_unknown needs target cloud")
                for r in tries:
                    if r not in by_repo:
                        errs.append(f"profile {name}: try_unknown {r!r} is not in the catalog")
    policy = profiles.get("licence_policy") or {}
    for cls in LICENCE_CLASSES:
        if policy.get(cls) not in POLICY_ACTIONS:
            errs.append(f"licence_policy[{cls!r}] must be one of {POLICY_ACTIONS}")
    for name, d in defs.items():
        if d.get("target") not in ("cloud", "local"):
            errs.append(f"profile {name}: target must be cloud or local")
        for m in d.get("members") or []:
            if m not in seen:
                errs.append(f"profile {name}: member {m!r} is not in the catalog")
    if errs:
        raise CatalogError("catalog invalid:\n  " + "\n  ".join(errs))


# ---------------------------------------------------------------- resolution

def _licence_action(entry: dict, policy: dict) -> str:
    return policy.get(entry["license"]["class"], "exclude-pending-owner")


def resolve(catalog: dict, profiles: dict, name: str) -> Resolved:
    """Resolve profile `name` to installed / excluded entries.

    Rules (documented in catalog/README.md):
      * `members` (explicit list, used by `default`) installs exactly those
        entries; the licence policy is reported as flags, not applied, because
        that list reproduces the image already in production (owner decision).
        Its optional `pins` freeze those members at the production pins
        while the catalog entries move on for every other profile.
      * `rule: "all-compatible-backend"` (`full`) = every cloud entry of an
        installable category whose compatibility is true.
      * `include_members_of` adds the members of the named profiles first
        (catalog pins, that profile's licence exemption, flagged), so an
        image that replaces another never loses one of its packs.
      * otherwise members are the entries listing the profile in `profiles`.
      * `try_unknown` (trial profiles) admits the listed entries whose compatibility is "unknown" for a build
        test (flagged); compatibility false still excludes; licence and conflicts apply as usual.
      * cloud profiles keep only cloud, compatible entries; the licence policy
        then includes, flags or excludes; conflicts exclude the later entry.
      * local profiles (frontend-extensions) list local entries; never imaged.
    """
    defs = profiles["profiles"]
    if name not in defs:
        raise CatalogError(f"unknown profile {name!r}; known: {', '.join(defs)}")
    d = defs[name]
    policy = profiles["licence_policy"]
    target = d["target"]
    res = Resolved(profile=name, target=target, strict=bool(d.get("strict")))
    by_repo = entries_by_repo(catalog)

    if d.get("members") is not None:
        frozen = d.get("pins") or {}
        for repo in d["members"]:
            e = _with_pin(by_repo[repo], frozen.get(repo), name)
            res.installed.append(e)
            action = _licence_action(e, policy)
            if action != "include":
                res.flagged.append((e, f"licence class {e['license']['class']} ({e['license'].get('spdx') or 'see notes'})"))
        return res

    if d.get("rule") == "all-compatible-backend":
        candidates = [e for e in catalog["entries"]
                      if e["install_target"] == "cloud" and e["category"] in INSTALLABLE_CATEGORIES]
    else:
        candidates = [e for e in catalog["entries"] if name in e["profiles"]]

    taken = {}
    tries = set(d.get("try_unknown") or [])
    # `include_members_of`: the packs an earlier image already ships stay in this one (at the catalog's current
    # pins), under the same licence exemption as that profile, so switching images never loses a node.
    for ref in d.get("include_members_of") or []:
        for repo in defs[ref]["members"]:
            e = by_repo[repo]
            if repo in taken:
                continue
            res.installed.append(e)
            taken[repo] = e
            if _licence_action(e, policy) != "include":
                res.flagged.append((e, f"licence class {e['license']['class']} "
                                       f"({e['license'].get('spdx') or 'see notes'}; kept: shipped in the {ref} image)"))
    for e in candidates:
        if e["repo"] in taken:
            continue
        if target == "local":
            if e["install_target"] != "local":
                res.excluded.append((e, f"install target is {e['install_target']}, not local"))
            else:
                res.installed.append(e)
            continue
        if e["install_target"] != "cloud":
            res.excluded.append((e, f"install target is {e['install_target']} (never installed in a cloud image)"))
            continue
        if e["category"] not in INSTALLABLE_CATEGORIES and e["install"]["method"] != "base-image":
            res.excluded.append((e, f"category {e['category']} is not a backend extension"))
            continue
        if e["install"]["method"] == "base-image":
            continue  # ComfyUI core comes with the base image
        comp = e["compatibility"]["compatible"]
        trial = comp == "unknown" and e["repo"] in tries
        if comp is not True and not trial:
            res.excluded.append((e, f"compatibility {comp}: {e['compatibility'].get('notes', '')}".strip()))
            continue
        action = _licence_action(e, policy)
        if action == "exclude-pending-owner":
            res.excluded.append((e, f"licence class {e['license']['class']} excluded pending owner decision "
                                    f"({e['license'].get('spdx') or e['license'].get('notes', '')})"))
            continue
        clash = [c for c in e["conflicts"].get("conflicts_with", []) if c in taken]
        if clash:
            res.excluded.append((e, f"conflicts with {', '.join(clash)} already in this profile"))
            continue
        res.installed.append(e)
        taken[e["repo"]] = e
        if trial:
            res.flagged.append((e, "trial: compatibility unknown, installed for a build test"))
        if action == "include-flagged":
            res.flagged.append((e, f"licence class {e['license']['class']} ({e['license'].get('spdx')})"))
    return res


def folder_of(entry: dict) -> str | None:
    return (entry.get("install") or {}).get("folder")


# ---------------------------------------------------------------- CLI

def _cmd_validate(args) -> int:
    catalog, profiles = load(args.catalog_dir)
    validate(catalog, profiles)
    print(f"catalog OK: {len(catalog['entries'])} entries, {len(profiles['profiles'])} profiles")
    return 0


def _cmd_resolve(args) -> int:
    catalog, profiles = load(args.catalog_dir)
    validate(catalog, profiles)
    res = resolve(catalog, profiles, args.profile)
    if args.json:
        print(json.dumps({
            "profile": res.profile, "target": res.target, "strict": res.strict,
            "installed": res.repos(),
            "excluded": [{"repo": e["repo"], "reason": r} for e, r in res.excluded],
            "flagged": [{"repo": e["repo"], "flag": f} for e, f in res.flagged],
        }, indent=2))
    else:
        print(f"{res.profile} ({res.target}): {len(res.installed)} installed, {len(res.excluded)} excluded")
        for e in res.installed:
            print(f"  + {e['repo']}")
        for e, r in res.excluded:
            print(f"  - {e['repo']}: {r}")
    return 0


def _cmd_summary(args) -> int:
    catalog, profiles = load(args.catalog_dir)
    validate(catalog, profiles)
    for name in profiles["profiles"]:
        res = resolve(catalog, profiles, name)
        print(f"{name:22s} {res.target:5s} installed={len(res.installed):3d} excluded={len(res.excluded):3d} "
              f"flagged={len(res.flagged):3d}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for cmd in ("validate", "resolve", "summary"):
        p = sub.add_parser(cmd)
        p.add_argument("--catalog-dir", default=str(Path(__file__).resolve().parents[1] / "catalog"))
        if cmd == "resolve":
            p.add_argument("profile")
            p.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        return {"validate": _cmd_validate, "resolve": _cmd_resolve, "summary": _cmd_summary}[args.cmd](args)
    except CatalogError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

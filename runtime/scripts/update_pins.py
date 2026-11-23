#!/usr/bin/env python3
"""Move catalog pins to the latest upstream release / commit (maintainer tool, needs network).

  update_pins.py [--catalog-dir catalog] [--repo owner/name ...] [--write] [--report FILE.json] [--markdown FILE.md]

For every cloud entry that the image build can install:
  registry     the newest Comfy Registry version whose status is active (versions the registry flags or
               deprecates are skipped and named in the report)
  git-archive  the HEAD commit of the repository's default branch

A moved pin clears `pin.content_sha256` (the old digest belongs to the old archive); run
`lock_catalog.py --profile <P> --write` afterwards so the build verifies the new archives again. The requirement
list is refreshed from the new pin. A GitHub-detected licence that differs from the catalog's keeps the old pin
(status `held`) until a maintainer reviews it. Frozen profile pins (profiles.json `pins`, the production-parity
`default`) are never touched. Without --write nothing is changed. GITHUB_TOKEN, when set, raises the API rate limit;
it is sent only to api.github.com and never printed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hf_catalog  # noqa: E402

REGISTRY_VERSIONS = "https://api.comfy.org/nodes/{id}/versions"
GITHUB_REPO = "https://api.github.com/repos/{repo}"
GITHUB_COMMIT = "https://api.github.com/repos/{repo}/commits/{ref}"
RAW_REQUIREMENTS = "https://raw.githubusercontent.com/{repo}/{sha}/requirements.txt"
ACTIVE = "NodeVersionStatusActive"


class NotFound(OSError):
    """HTTP 404 from the opener."""


def urlopen_bytes(url: str) -> bytes:
    if not url.startswith("https://"):
        raise OSError(f"refusing non-HTTPS URL: {url}")
    headers = {"User-Agent": "halofuse-update-pins"}
    token = os.environ.get("GITHUB_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as resp:
            return resp.read()
    except urllib.error.HTTPError as err:
        if err.code == 404:
            raise NotFound(url) from None
        raise OSError(f"HTTP {err.code} for {url}") from None


def _json(opener, url):
    return json.loads(opener(url))


def parse_requirements(text: str) -> list[str]:
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "-")):
            continue
        out.append(re.split(r"\s+#", line, maxsplit=1)[0].strip())
    return out


def _version_key(v: str):
    return tuple(int(p) if p.isdigit() else -1 for p in re.split(r"[.+-]", str(v)))


def latest_registry(entry: dict, opener) -> dict:
    versions = _json(opener, REGISTRY_VERSIONS.format(id=entry["install"]["registry_id"]))
    versions = sorted((v for v in versions if isinstance(v, dict) and v.get("version")),
                      key=lambda v: (v.get("createdAt") or "", _version_key(v["version"])), reverse=True)
    current = entry["pin"].get("version")
    skipped = []
    for v in versions:
        if v["version"] == current:
            # never a downgrade: the pinned version is already the accepted one, even if the registry flags it now
            return {"version": current, "date": (v.get("createdAt") or "")[:10], "pip": None, "skipped": skipped}
        if v.get("status") == ACTIVE and not v.get("deprecated"):
            return {"version": v["version"], "date": (v.get("createdAt") or "")[:10],
                    "pip": [d for d in (v.get("dependencies") or []) if isinstance(d, str)], "skipped": skipped}
        skipped.append(f"{v['version']} ({str(v.get('status') or 'unknown').replace('NodeVersionStatus', '').lower()})")
    raise OSError("no active registry version")


def latest_commit(entry: dict, opener) -> dict:
    repo = entry["repo"]
    meta = _json(opener, GITHUB_REPO.format(repo=repo))
    branch = meta.get("default_branch") or "main"
    commit = _json(opener, GITHUB_COMMIT.format(repo=repo, ref=branch))
    sha = str(commit.get("sha") or "")
    if not hf_catalog.COMMIT_RE.match(sha):
        raise OSError(f"no commit for {repo}@{branch}")
    lic = meta.get("license") if isinstance(meta.get("license"), dict) else None
    try:
        pip = parse_requirements(opener(RAW_REQUIREMENTS.format(repo=repo, sha=sha)).decode("utf-8", "replace"))
        has_req = True
    except NotFound:
        pip, has_req = [], False
    return {"commit": sha, "date": ((commit.get("commit") or {}).get("committer") or {}).get("date", ""),
            "spdx": lic.get("spdx_id") if lic else None, "archived": bool(meta.get("archived")),
            "pip": pip, "has_requirements": has_req, "branch": branch}


def _licence_note(entry: dict, spdx) -> tuple[bool, str]:
    """(hold, note). Only GitHub-detected licences are compared; manual readings stay a maintainer's job."""
    lic = entry["license"]
    if str(lic.get("source", "")).startswith("GitHub licence detection") and spdx != lic.get("spdx"):
        return True, f"licence changed upstream ({lic.get('spdx')} -> {spdx}): pin kept, review the licence"
    if lic.get("class") == "no-licence" and spdx:
        return False, f"upstream now declares {spdx}: reclassify the licence to let the policy include it"
    return False, ""


def update(catalog: dict, opener=urlopen_bytes, repos=None, today=None) -> list[dict]:
    """Move pins in *catalog* (in place). Returns one report row per examined entry."""
    today = today or dt.date.today().isoformat()
    rows = []
    for e in catalog["entries"]:
        method = (e.get("install") or {}).get("method")
        if e.get("install_target") != "cloud" or method not in ("registry", "git-archive"):
            continue
        if repos and e["repo"] not in repos:
            continue
        pin = e["pin"]
        old = pin.get("version") if method == "registry" else pin.get("commit")
        row = {"repo": e["repo"], "method": method, "old": old, "new": old, "status": "unchanged", "note": ""}
        rows.append(row)
        try:
            if method == "registry":
                got = latest_registry(e, opener)
                new = got["version"]
                if got["skipped"]:
                    row["note"] = "skipped (registry status): " + ", ".join(got["skipped"][:4]) + \
                                  (" ..." if len(got["skipped"]) > 4 else "")
            else:
                got = latest_commit(e, opener)
                new = got["commit"]
                hold, note = _licence_note(e, got["spdx"])
                row["note"] = "; ".join(n for n in (note, "upstream archived" if got["archived"] else "") if n)
                up = e.get("upstream")
                if isinstance(up, dict):
                    up.update(head_commit=new, head_date=got["date"], archived=got["archived"], probed_at=today)
                if hold:
                    row["status"] = "held"
                    continue
        except Exception as exc:  # noqa: BLE001 - reported per entry, the pin stays
            row["status"] = "error"
            row["note"] = str(exc)[:200]
            continue
        row["new"] = new
        if new == old:
            continue
        row["status"] = "updated"
        if method == "registry":
            pin["version"] = new
            pin["pin_source"] = f"latest active Comfy Registry version on {today} (update_pins.py)"
        else:
            pin["commit"] = new
            pin["pin_source"] = f"default branch HEAD on {today} (update_pins.py)"
        pin["pinned_at"] = today
        pin["content_sha256"] = None
        req = e.get("requirements")
        if isinstance(req, dict) and "pip" in req:
            req["pip"] = got["pip"]
            if method == "registry":
                req["source"] = f"Comfy Registry dependencies of version {new}"
            else:
                req["source"] = (f"requirements.txt at {new[:12]}" if got["has_requirements"]
                                 else f"no requirements.txt at {new[:12]}")
        ver = e.get("verified")
        if isinstance(ver, dict):
            ver["status"] = "NOT VERIFIED"
            ver["evidence"] = (f"pin moved from {str(old)[:12]} to {str(new)[:12]} on {today} (update_pins.py); "
                               "no build or import test at the new pin yet")
    return rows


def markdown(rows: list[dict], today: str) -> str:
    def short(v):
        return "" if v is None else (v[:12] if hf_catalog.COMMIT_RE.match(str(v)) else str(v))
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    lines = [f"# Pin update {today}", "",
             "Generated by `scripts/update_pins.py` (latest active Comfy Registry version / default-branch HEAD). "
             "Rows are the catalog pins (used by `full`, the HaloFuse image, and the other new profiles); "
             "the `default` profile keeps its frozen production pins (profiles.json `pins`). "
             "Counts: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) + ".", "",
             "| Pack | Method | Old | New | Status | Note |", "|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda r: r["repo"].lower()):
        note = r["note"].replace("|", "/")
        lines.append(f"| {r['repo']} | {r['method']} | {short(r['old'])} | {short(r['new'])} | {r['status']} | {note} |")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--catalog-dir", default=str(Path(__file__).resolve().parents[1] / "catalog"))
    ap.add_argument("--repo", action="append")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--report")
    ap.add_argument("--markdown")
    args = ap.parse_args(argv)
    path = Path(args.catalog_dir) / "catalog.json"
    catalog = json.loads(path.read_text(encoding="utf-8"))
    today = dt.date.today().isoformat()
    rows = update(catalog, repos=set(args.repo or ()) or None, today=today)
    if args.write:
        _, profiles = hf_catalog.load(args.catalog_dir)
        hf_catalog.validate(catalog, profiles)
        path.write_text(json.dumps(catalog, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    if args.report:
        Path(args.report).write_text(json.dumps(rows, indent=1) + "\n", encoding="utf-8")
    if args.markdown:
        Path(args.markdown).write_text(markdown(rows, today), encoding="utf-8", newline="\n")
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print("pins: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) + ("" if args.write else " (dry run)"))
    return 1 if counts.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())

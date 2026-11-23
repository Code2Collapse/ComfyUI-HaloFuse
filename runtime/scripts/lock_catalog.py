#!/usr/bin/env python3
"""Record pin.content_sha256 for the packs of a profile (maintainer tool, needs network).

  lock_catalog.py --profile default [--catalog-dir catalog] [--write] [--locks-out FILE] [--stats-out FILE]

Downloads each pack's pinned archive exactly as the image build does (install_nodes.fetch path),
hashes the extracted tree (install_nodes.content_hash) and, with --write, stores the digest in
catalog.json. The build then refuses any archive whose content differs (--require-hashes makes a
missing digest fatal too). Already-locked entries are re-verified, never silently changed: a
different digest for the same pin is reported as an error.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hf_catalog  # noqa: E402
import install_nodes  # noqa: E402


def lock(catalog_dir: Path, profile: str, opener=install_nodes.urlopen_bytes,
         stats: dict | None = None) -> tuple[dict, list]:
    """Digests of the profile's pinned archives. *stats* (optional dict) receives per pack the archive size, the
    extracted size and file count (input for image-size estimates)."""
    catalog, profiles = hf_catalog.load(catalog_dir)
    hf_catalog.validate(catalog, profiles)
    res = hf_catalog.resolve(catalog, profiles, profile)
    digests, errors = {}, []
    for entry in res.installed:
        try:
            blob = opener(install_nodes.source_url(entry, opener))
            with tempfile.TemporaryDirectory() as tmp:
                folder = Path(tmp) / "pack"
                install_nodes.extract_zip(blob, folder)
                digest = install_nodes.content_hash(folder)
                if stats is not None:
                    files = [f for f in folder.rglob("*") if f.is_file()]
                    stats[entry["repo"]] = {"archive_bytes": len(blob), "files": len(files),
                                            "extracted_bytes": sum(f.stat().st_size for f in files)}
        except Exception as exc:  # noqa: BLE001 - reported per pack
            errors.append(f"{entry['repo']}: {exc}")
            continue
        old = entry["pin"].get("content_sha256")
        if old and old != digest:
            errors.append(f"{entry['repo']}: recorded {old[:12]} but the pinned archive now hashes to {digest[:12]}")
            continue
        digests[entry["repo"]] = digest
    return digests, errors


def apply(catalog_dir: Path, digests: dict, profile: str | None = None) -> int:
    """Store digests where the pin lives: a profile's frozen `pins` (profiles.json) for its frozen members, the
    catalog entry otherwise."""
    digests = dict(digests)
    changed = 0
    if profile is not None:
        ppath = Path(catalog_dir) / "profiles.json"
        profiles = json.loads(ppath.read_text(encoding="utf-8"))
        frozen = (profiles["profiles"].get(profile) or {}).get("pins") or {}
        for repo in [r for r in digests if r in frozen]:
            digest = digests.pop(repo)
            if frozen[repo].get("content_sha256") != digest:
                frozen[repo]["content_sha256"] = digest
                changed += 1
        if frozen:
            ppath.write_text(json.dumps(profiles, indent=1, ensure_ascii=False) + "\n", encoding="utf-8",
                             newline="\n")
    path = Path(catalog_dir) / "catalog.json"
    catalog = json.loads(path.read_text(encoding="utf-8"))
    for e in catalog["entries"]:
        if e["repo"] in digests and e["pin"].get("content_sha256") != digests[e["repo"]]:
            e["pin"]["content_sha256"] = digests[e["repo"]]
            changed += 1
    path.write_text(json.dumps(catalog, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    return changed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--catalog-dir", default=str(Path(__file__).resolve().parents[1] / "catalog"))
    ap.add_argument("--profile", required=True)
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--locks-out")
    ap.add_argument("--stats-out", help="write per-pack archive / extracted sizes (JSON)")
    args = ap.parse_args(argv)
    stats = {} if args.stats_out else None
    digests, errors = lock(Path(args.catalog_dir), args.profile, stats=stats)
    if args.stats_out:
        Path(args.stats_out).write_text(json.dumps(stats, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    for err in errors:
        print("ERROR", err, file=sys.stderr)
    if args.locks_out:
        Path(args.locks_out).write_text(json.dumps(digests, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    if args.write:
        print(f"updated {apply(Path(args.catalog_dir), digests, args.profile)} entries")
    print(f"{len(digests)} hashed, {len(errors)} errors")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())

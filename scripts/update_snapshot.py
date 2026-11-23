#!/usr/bin/env python3
"""Install the node snapshot of a CI-built HaloFuse image into the bridge (maintainer tool; Python stdlib + gh CLI).

After a push to main that touches runtime/, the "Runtime image" workflow builds one image per profile, pushes it as
ghcr.io/code2collapse/halofuse-runtime:<profile>-sha-<commit> and uploads the artifact runtime-<profile>-<commit>,
which holds node_snapshot.json (contract C-5). This script:

  1. finds the newest SUCCESSFUL run of that workflow on main (or the run given with --run),
  2. downloads the artifact of --profile (default: full) with `gh run download`,
  3. refuses a snapshot that is not halofuse.node_snapshot/1 for that profile, or that leaks: the cloud provider's
     domain, an internal image namespace, or an absolute path (tests/fakes/leaks.py, which must be present),
  4. installs it as halofuse_bridge/snapshots/<profile>.json and rebuilds index.json
     (runtime/scripts/export_node_snapshot.py build_index; default profile unchanged),
  5. with --write-contract: checks that the image tag is anonymously pullable from GHCR and points the `halofuse`
     contract (provider/image_contract.py: HALOFUSE_IMAGE + HALOFUSE_SNAPSHOT_PROFILE) at that image and profile,
     so the image and the snapshot the Workflow Check uses always move together.

  python scripts/update_snapshot.py [--profile full] [--run RUN_ID] [--write-contract] [--dry-run]

Run it only after CI has pushed the image; it never builds anything and never contacts the cloud provider.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_DIR = ROOT / "halofuse_bridge" / "snapshots"
CONTRACT_FILE = ROOT / "halofuse_bridge" / "provider" / "image_contract.py"
WORKFLOW = "runtime-image.yml"
GITHUB_REPO = "Code2Collapse/ComfyUI-HaloFuse"
IMAGE_REPO = "code2collapse/halofuse-runtime"
SCHEMA = "halofuse.node_snapshot/1"
PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# absolute paths that identify a machine or a person: home folders (POSIX, macOS, Windows), CI workspaces, UNC
# shares. Generic defaults that packs ship (C:/Program Files/..., the base image's /default-comfyui-bundle/...) pass.
ABS_PATH_RE = re.compile(r"/home/[\w.-]+/|/Users/[\w.-]+/|\b[A-Za-z]:[\\/]+Users[\\/]+[\w.-]+|/root/|"
                         r"/github/workspace|/runner/work/|/actions-runner/|\\\\[\w.-]+\\[\w$.-]+")


class SnapshotError(RuntimeError):
    pass


def run_gh(args: list[str]) -> str:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        raise SnapshotError(f"gh {' '.join(args[:2])} failed: {proc.stderr.strip()[-300:]}")
    return proc.stdout


def _leaks_module():
    fakes = ROOT / "tests" / "fakes"
    if not (fakes / "leaks.py").is_file():
        raise SnapshotError("tests/fakes/leaks.py is missing: the leak check cannot run, so nothing is installed")
    if str(fakes) not in sys.path:
        sys.path.insert(0, str(fakes))
    import leaks  # noqa: PLC0415 - local developer helper, not shipped

    return leaks


def leak_problems(text: str) -> list[str]:
    leaks = _leaks_module()
    problems = []
    if leaks.provider_domain_hits(text):
        problems.append("names the cloud provider's domain")
    low = text.lower()
    problems += ["names an internal image namespace" for ns in leaks.INTERNAL_IMAGE_NAMESPACES if ns.lower() in low]
    m = ABS_PATH_RE.search(text)
    if m:
        problems.append(f"contains an absolute path near offset {m.start()}")
    return problems


def check_snapshot(text: str, profile: str) -> dict:
    try:
        snap = json.loads(text)
    except ValueError as exc:
        raise SnapshotError(f"node_snapshot.json is not JSON ({exc})") from None
    if not isinstance(snap, dict) or snap.get("schema") != SCHEMA:
        raise SnapshotError(f"not a {SCHEMA} document")
    if snap.get("profile") != profile:
        raise SnapshotError(f"snapshot is for profile {snap.get('profile')!r}, not {profile!r}")
    if snap.get("object_info_status") != "captured":
        raise SnapshotError(f"object_info was not captured ({snap.get('object_info_status')}): not installed")
    problems = leak_problems(text)
    if problems:
        raise SnapshotError("snapshot refused: " + "; ".join(problems))
    return snap


def latest_run(gh=run_gh) -> dict:
    rows = json.loads(gh(["run", "list", "--repo", GITHUB_REPO, "--workflow", WORKFLOW, "--branch", "main",
                          "--status", "success", "--limit", "1", "--json", "databaseId,headSha,createdAt"]))
    if not rows:
        raise SnapshotError("no successful run of the runtime image workflow on main yet")
    return rows[0]


def run_info(run_id: str, gh=run_gh) -> dict:
    info = json.loads(gh(["run", "view", str(run_id), "--repo", GITHUB_REPO, "--json",
                          "databaseId,headSha,conclusion,headBranch"]))
    if info.get("conclusion") != "success":
        raise SnapshotError(f"run {run_id} did not succeed ({info.get('conclusion')})")
    return info


def download_snapshot(run_id, head_sha: str, profile: str, dest: Path, gh=run_gh) -> str:
    name = f"runtime-{profile}-{head_sha}"
    gh(["run", "download", str(run_id), "--repo", GITHUB_REPO, "--name", name, "--dir", str(dest)])
    found = sorted(Path(dest).rglob("node_snapshot.json"))
    if len(found) != 1:
        raise SnapshotError(f"artifact {name} holds {len(found)} node_snapshot.json files, expected 1")
    return found[0].read_text(encoding="utf-8")


# Keys the Workflow Check never reads (node and input help text). The full image's node list is 16.8 MB pretty-printed
# with them and 8.3 MB compact without; the pre-push gate refuses files over 10 MB.
_UNUSED_KEYS = frozenset({"description", "tooltip", "search_aliases"})


def _strip_unused(obj):
    if isinstance(obj, dict):
        return {k: _strip_unused(v) for k, v in obj.items() if k not in _UNUSED_KEYS}
    if isinstance(obj, list):
        return [_strip_unused(v) for v in obj]
    return obj


def compact_snapshot(text: str) -> str:
    """The snapshot as shipped: help text removed from object_info, compact JSON, one trailing newline."""
    snap = json.loads(text)
    if isinstance(snap.get("object_info"), dict):
        snap["object_info"] = _strip_unused(snap["object_info"])
    return json.dumps(snap, separators=(",", ":"), ensure_ascii=False) + "\n"


def install(text: str, profile: str, snapshot_dir: Path = SNAPSHOT_DIR) -> Path:
    sys.path.insert(0, str(ROOT / "runtime" / "scripts"))
    import export_node_snapshot  # noqa: PLC0415 - runtime tool, stdlib only

    snapshot_dir = Path(snapshot_dir)
    target = snapshot_dir / f"{profile}.json"
    target.write_text(compact_snapshot(text), encoding="utf-8", newline="\n")
    old = {}
    if (snapshot_dir / "index.json").is_file():
        old = json.loads((snapshot_dir / "index.json").read_text(encoding="utf-8"))
    index = export_node_snapshot.build_index(snapshot_dir, old.get("default_profile") or "default")
    (snapshot_dir / "index.json").write_text(json.dumps(index, indent=1) + "\n", encoding="utf-8", newline="\n")
    return target


def image_ref(profile: str, head_sha: str) -> str:
    return f"ghcr.io/{IMAGE_REPO}:{profile}-sha-{head_sha}"


def ghcr_digest(tag: str, opener=urllib.request.urlopen) -> str:
    """Manifest digest of a public GHCR tag (anonymous pull token), or SnapshotError."""
    try:
        with opener(f"https://ghcr.io/token?scope=repository:{IMAGE_REPO}:pull", timeout=30) as resp:
            token = json.loads(resp.read())["token"]
        req = urllib.request.Request(
            f"https://ghcr.io/v2/{IMAGE_REPO}/manifests/{tag}", method="HEAD",
            headers={"Authorization": f"Bearer {token}", "Accept": ", ".join((
                "application/vnd.oci.image.index.v1+json", "application/vnd.oci.image.manifest.v1+json",
                "application/vnd.docker.distribution.manifest.v2+json",
                "application/vnd.docker.distribution.manifest.list.v2+json"))})
        with opener(req, timeout=30) as resp:
            digest = resp.headers.get("Docker-Content-Digest")
    except (urllib.error.URLError, OSError, KeyError, ValueError) as exc:
        raise SnapshotError(f"image {tag} is not anonymously pullable from GHCR ({exc})") from None
    if not digest:
        raise SnapshotError(f"image {tag}: no manifest digest returned")
    return digest


def write_contract(image: str, profile: str, digest: str, today: str, path: Path = CONTRACT_FILE) -> None:
    src = path.read_text(encoding="utf-8")
    new, n1 = re.subn(r'^HALOFUSE_IMAGE = "[^"]*"$', f'HALOFUSE_IMAGE = "{image}"', src, flags=re.M)
    new, n2 = re.subn(r'^HALOFUSE_SNAPSHOT_PROFILE = "[^"]*"$', f'HALOFUSE_SNAPSHOT_PROFILE = "{profile}"', new,
                      flags=re.M)
    new, n3 = re.subn(r"^# CI build of commit .*\n# sha256:[0-9a-f]{64} \(.*\)$",
                      f"# CI build of commit {image.rsplit('-sha-', 1)[1][:7]} (profile {profile}); manifest digest\n"
                      f"# {digest} (checked anonymously pullable {today})", new, flags=re.M)
    if (n1, n2, n3) != (1, 1, 1):
        raise SnapshotError("image_contract.py layout changed: HALOFUSE_IMAGE / HALOFUSE_SNAPSHOT_PROFILE / digest "
                            "comment not found exactly once; edit it by hand")
    path.write_text(new, encoding="utf-8", newline="\n")


def main(argv=None, gh=run_gh) -> int:
    import datetime as dt

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--profile", default="full")
    ap.add_argument("--run", help="a specific workflow run id (default: newest successful run on main)")
    ap.add_argument("--write-contract", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="download and check only")
    args = ap.parse_args(argv)
    if not PROFILE_RE.match(args.profile):
        ap.error("invalid profile name")
    try:
        run = run_info(args.run, gh) if args.run else latest_run(gh)
        head = str(run.get("headSha") or "")
        if not SHA_RE.match(head):
            raise SnapshotError("the run has no commit sha")
        with tempfile.TemporaryDirectory(prefix="hf-snapshot-") as tmp:
            text = download_snapshot(run["databaseId"], head, args.profile, Path(tmp), gh)
        snap = check_snapshot(text, args.profile)
        loaded = sum(1 for p in snap.get("packs", []) if p.get("status") == "loaded")
        print(f"run {run['databaseId']} ({head[:7]}): {args.profile} snapshot OK, ComfyUI "
              f"{snap.get('comfyui_version')}, {loaded}/{len(snap.get('packs', []))} packs loaded")
        if args.dry_run:
            return 0
        image = image_ref(args.profile, head)
        digest = ghcr_digest(image.split(":", 1)[1]) if args.write_contract else None  # before anything is written
        print(f"installed {install(text, args.profile).name} (+ index.json) in halofuse_bridge/snapshots")
        if digest:
            write_contract(image, args.profile, digest, dt.date.today().isoformat())
            print(f"halofuse contract -> {args.profile}-sha-{head[:7]} ({digest[:19]}...)")
    except SnapshotError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

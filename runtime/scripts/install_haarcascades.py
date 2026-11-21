#!/usr/bin/env python3
"""Install the vendored OpenCV Haar cascades into cv2's data folder (image build).

  install_haarcascades.py <vendored-dir> [--target DIR] [--no-load-check]

Every file listed in <vendored-dir>/SHA256SUMS is checksum-verified before it is
copied; a missing or altered file fails the build. No network access (audit
finding X13: the old build downloaded these from a mutable tag every time).
By default the target is cv2.data.haarcascades (or cv2/data/), and the build
fails if the frontal-face cascade does not load.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
from pathlib import Path


class CascadeError(RuntimeError):
    pass


def read_sums(vendored: Path) -> dict:
    sums = {}
    for line in (vendored / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, name = line.split(maxsplit=1)
        name = name.lstrip("*").strip()
        if "/" in name or "\\" in name or name.startswith("."):
            raise CascadeError(f"unsafe name in SHA256SUMS: {name!r}")
        sums[name] = digest.lower()
    if not sums:
        raise CascadeError("SHA256SUMS lists no files")
    return sums


def verify(vendored: Path) -> dict:
    sums = read_sums(vendored)
    for name, digest in sums.items():
        f = vendored / name
        if not f.is_file():
            raise CascadeError(f"missing vendored cascade: {name}")
        got = hashlib.sha256(f.read_bytes()).hexdigest()
        if got != digest:
            raise CascadeError(f"checksum mismatch for {name}: expected {digest[:12]}, got {got[:12]}")
    return sums


def default_target() -> Path:
    import cv2  # noqa: PLC0415 - only inside the image

    target = getattr(getattr(cv2, "data", None), "haarcascades", None)
    return Path(target) if target else Path(cv2.__file__).parent / "data"


def install(vendored: Path, target: Path) -> list[str]:
    sums = verify(vendored)
    target.mkdir(parents=True, exist_ok=True)
    copied = []
    for name in sorted(sums):
        dest = target / name
        if dest.is_file() and hashlib.sha256(dest.read_bytes()).hexdigest() == sums[name]:
            continue  # the wheel already ships this exact file
        shutil.copyfile(vendored / name, dest)
        copied.append(name)
    return copied


def load_check(target: Path) -> None:
    import cv2  # noqa: PLC0415

    if cv2.CascadeClassifier(str(target / "haarcascade_frontalface_default.xml")).empty():
        raise CascadeError("haarcascade_frontalface_default.xml did not load")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("vendored")
    ap.add_argument("--target")
    ap.add_argument("--no-load-check", action="store_true")
    args = ap.parse_args(argv)
    try:
        target = Path(args.target) if args.target else default_target()
        copied = install(Path(args.vendored), target)
        print(f"[haarcascades] {len(copied)} copied, {len(os.listdir(target))} files in {target}", flush=True)
        if not args.no_load_check:
            load_check(target)
            print("[haarcascades] face cascade loads OK", flush=True)
    except CascadeError as exc:
        print(f"[haarcascades] ERROR: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""D5 drift check for the runtime tree: Dockerfile, CI, catalog and README must agree.

  check_consistency.py [ROOT]    exit 1 and list findings when anything disagrees

Rules
  copy-source     every Dockerfile COPY source exists and is inside the .dockerignore allowlist
  namespace       no hardcoded image registry namespace in build files (owner decision D-4)
  flux-only       no "Flux-only" / "Flux test image" claim (the runtime is model-agnostic)
  res4lyf         README's RES4LYF statement matches the catalog `default` profile
  nodes-json      no reference to nodes.json (replaced by catalog/)
"""
from __future__ import annotations

import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hf_catalog  # noqa: E402

# Registry namespaces that must not be hard-coded in build files, as SHA-256 of the lower-case name (this public
# script does not spell them out; owner decision D-4).
NAMESPACE_SHA256 = frozenset({"a8cc6d6767ce34c26b8e95f5f44822be2b9e1d6a39f1e1b972d90c6426dd2cd3", "57e9f891304a9efece2564bc54afaf7463218e0627eb47898f438a42c3345aad"})
BUILD_FILES = ("Dockerfile", ".dockerignore", ".github/workflows", "scripts", "catalog/profiles.json")


@dataclass(frozen=True)
class Finding:
    file: str
    rule: str
    message: str

    def __str__(self):
        return f"{self.file}: [{self.rule}] {self.message}"


def _files(root: Path, spec: str):
    p = root / spec
    if p.is_dir():
        yield from (f for f in sorted(p.rglob("*")) if f.is_file() and f.suffix in (".py", ".yml", ".yaml", ".json", ""))
    elif p.is_file():
        yield p


def _allowlist(root: Path) -> list[str]:
    di = root / ".dockerignore"
    if not di.is_file():
        return []
    return [l[1:].strip().rstrip("/") for l in di.read_text(encoding="utf-8").splitlines() if l.startswith("!")]


def check(root) -> list[Finding]:
    root = Path(root)
    out: list[Finding] = []
    dockerfile = root / "Dockerfile"
    allowed = _allowlist(root)
    if dockerfile.is_file():
        text = dockerfile.read_text(encoding="utf-8")
        for m in re.finditer(r"^COPY\s+(?!--from)(?:--\S+\s+)*(.+)$", text, re.M):
            *sources, _dest = m.group(1).split()
            for src in sources:
                src = src.rstrip("/")
                if not (root / src).exists():
                    out.append(Finding("Dockerfile", "copy-source", f"COPY source {src} does not exist"))
                elif allowed and not any(src == a or src.startswith(a + "/") for a in allowed):
                    out.append(Finding("Dockerfile", "copy-source", f"COPY source {src} is excluded by .dockerignore"))
    for spec in BUILD_FILES:
        for f in _files(root, spec):
            rel = f.relative_to(root).as_posix()
            if rel.endswith("check_consistency.py"):
                continue
            body = f.read_text(encoding="utf-8", errors="replace")
            for m in re.finditer(r"[A-Za-z0-9][A-Za-z0-9-]*", body):
                if hashlib.sha256(m.group(0).lower().encode()).hexdigest() in NAMESPACE_SHA256:
                    out.append(Finding(rel, "namespace", "hardcoded registry namespace (owner decision D-4)"))
                    break
    for f in [dockerfile, root / "README.md"]:
        if f.is_file() and re.search(r"flux[- ]only|flux test image", f.read_text(encoding="utf-8"), re.I):
            out.append(Finding(f.name, "flux-only", "claims a Flux-only image; the runtime is model-agnostic"))
    readme = root / "README.md"
    if readme.is_file():
        rtext = readme.read_text(encoding="utf-8")
        if "nodes.json" in rtext and not (root / "nodes.json").exists():
            out.append(Finding("README.md", "nodes-json", "refers to nodes.json, which catalog/ replaced"))
        try:
            catalog, profiles = hf_catalog.load(root / "catalog")
            in_default = "ClownsharkBatwing/RES4LYF" in hf_catalog.resolve(catalog, profiles, "default").repos()
        except (OSError, KeyError, hf_catalog.CatalogError):
            in_default = None
        omitted = re.search(r"RES4LYF[^.\n]*(omitted|excluded|not installed)", rtext, re.I)
        if in_default and omitted:
            out.append(Finding("README.md", "res4lyf", "says RES4LYF is omitted, but the default profile installs it "
                                                       "(owner decision 11, lead default 'as in main')"))
    for spec in BUILD_FILES:
        for f in _files(root, spec):
            rel = f.relative_to(root).as_posix()
            if rel.endswith("check_consistency.py"):
                continue
            if "nodes.json" in f.read_text(encoding="utf-8", errors="replace"):
                out.append(Finding(rel, "nodes-json", "refers to nodes.json, which catalog/ replaced"))
    return out


def main(argv=None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    root = Path(args[0]) if args else Path(__file__).resolve().parents[1]
    findings = check(root)
    for f in findings:
        print(f)
    print(f"{len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())

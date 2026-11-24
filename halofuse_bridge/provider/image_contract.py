"""The runtime image the bridge submits and the file names that image reads: the DEPLOYED contract.

Two contracts exist, each a matching triple (image, command, manifest file name):

* ``legacy``   - the image deployed before HaloFuse, byte for byte as it was built (its names are its own). It is
                 the DEFAULT until a render on the HaloFuse image has been verified on the real cloud.
* ``halofuse`` - the HaloFuse runtime built from ``runtime/`` by this repository's CI and published to
                 ghcr.io/code2collapse/halofuse-runtime (public). Runner entry ``/runner/halofuse_run.py``, manifest
                 ``halofuse_job.json`` (its runner also accepts the old manifest name).

Selection: the environment variable ``HALOFUSE_RUNTIME`` (``halofuse`` / ``legacy``), else the setting
``runtime_image`` in halofuse_settings.json (the panel's Advanced "Cloud runtime" choice writes it, by contract NAME
only: an image reference never comes from the browser), else the default. The image is pinned to an immutable build
tag, so a bridge never runs an image it was not written for.

Each contract also names the node snapshot (``halofuse_bridge/snapshots/<profile>.json``) that describes the node
set of its image; the Workflow Check checks against the snapshot of the selected runtime. The original image's node
set is the catalog's ``default`` profile (production parity). ``scripts/update_snapshot.py`` moves the HaloFuse
image and its snapshot together after CI built and pushed a new image.
"""
from __future__ import annotations

import os
from typing import NamedTuple


class Contract(NamedTuple):
    name: str
    image: str
    command: tuple
    manifest_name: str
    snapshot_profile: str = "default"   # node snapshot of this image (halofuse_bridge/snapshots/<profile>.json)


# The HaloFuse image and the snapshot of its node set move TOGETHER (scripts/update_snapshot.py rewrites these two
# lines after CI pushed an image; the tag prefix is the catalog profile the image was built from).
# CI build of commit 3fe302f (profile full); manifest digest
# sha256:2917b70b43cb5ff2a034cf6e3e401e124126ef0ac470ddfddbb3dbe81d15a388 (checked anonymously pullable 2026-11-24)
HALOFUSE_IMAGE = "ghcr.io/code2collapse/halofuse-runtime:full-sha-3fe302fc2b0e663905140eeff1bf437e95727b11"
HALOFUSE_SNAPSHOT_PROFILE = "full"

CONTRACTS = {
    "legacy": Contract("legacy", "ghcr.io/nithin-ml/spark-fuse-comfyui:sha-70e0ff4b38b431b2c77e90f2936f93d04516ccc9",
                       ("python3.13", "/runner/spark_fuse_run.py"), "spark_fuse_job.json", "default"),
    "halofuse": Contract("halofuse", HALOFUSE_IMAGE, ("python3.13", "/runner/halofuse_run.py"), "halofuse_job.json",
                         HALOFUSE_SNAPSHOT_PROFILE),
}
# Flip to "halofuse" once one real render on the HaloFuse image has passed (probe session with the owner).
DEFAULT = "legacy"


def env_override(env=None) -> str | None:
    """The contract name forced by the environment (then the panel choice has no effect), else None."""
    env = os.environ if env is None else env
    return select(env=env).name if str(env.get("HALOFUSE_RUNTIME") or "").strip() else None


def halofuse_ready() -> bool:
    """True once the HaloFuse contract runs an image built from its own profile (not the original node set)."""
    return CONTRACTS["halofuse"].snapshot_profile != CONTRACTS["legacy"].snapshot_profile


def select(env=None, settings=None) -> Contract:
    """The active contract. Unknown names fall back to the default (a typo never submits a mismatched triple)."""
    env = os.environ if env is None else env
    name = str(env.get("HALOFUSE_RUNTIME") or (settings or {}).get("runtime_image") or DEFAULT).strip().lower()
    return CONTRACTS.get(name, CONTRACTS[DEFAULT])


# Back-compatible module constants = the contract chosen at import (environment / default).
_ACTIVE = select()
IMAGE = _ACTIVE.image
COMMAND = _ACTIVE.command
MANIFEST_NAME = _ACTIVE.manifest_name

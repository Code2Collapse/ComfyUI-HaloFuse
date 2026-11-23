"""The runtime image the bridge submits and the file names that image reads: the DEPLOYED contract.

Two contracts exist, each a matching triple (image, command, manifest file name):

* ``legacy``   - the image deployed before HaloFuse, byte for byte as it was built (its names are its own). It is
                 the DEFAULT until a render on the HaloFuse image has been verified on the real cloud.
* ``halofuse`` - the HaloFuse runtime built from ``runtime/`` by this repository's CI and published to
                 ghcr.io/code2collapse/halofuse-runtime (public). Runner entry ``/runner/halofuse_run.py``, manifest
                 ``halofuse_job.json`` (its runner also accepts the old manifest name).

Selection (never from the browser): the environment variable ``HALOFUSE_RUNTIME`` (``halofuse`` / ``legacy``), else
the file-only setting ``runtime_image`` in halofuse_settings.json, else the default. The image is pinned to an
immutable build tag, so a bridge never runs an image it was not written for.
"""
from __future__ import annotations

import os
from typing import NamedTuple


class Contract(NamedTuple):
    name: str
    image: str
    command: tuple
    manifest_name: str


CONTRACTS = {
    "legacy": Contract("legacy", "ghcr.io/nithin-ml/spark-fuse-comfyui:sha-70e0ff4b38b431b2c77e90f2936f93d04516ccc9",
                       ("python3.13", "/runner/spark_fuse_run.py"), "spark_fuse_job.json"),
    # CI build of commit dbd07af (HaloFuse 0.5.0); manifest digest
    # sha256:9e2d98c4a5235bfb1ba68d91750b1222f65f5cf2412fac4f6ac276f3f9e2c695 (checked anonymously pullable 2026-11-23)
    "halofuse": Contract("halofuse",
                         "ghcr.io/code2collapse/halofuse-runtime:default-sha-dbd07af093a81a79ee83da552662f67476b17144",
                         ("python3.13", "/runner/halofuse_run.py"), "halofuse_job.json"),
}
# Flip to "halofuse" once one real render on the HaloFuse image has passed (probe session with the owner).
DEFAULT = "legacy"


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

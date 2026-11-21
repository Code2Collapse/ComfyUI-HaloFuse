"""The runtime image the bridge submits and the file names that image reads: the DEPLOYED contract.

The values below are what the currently published runtime image expects, byte for byte; they still carry the names
that image was built with. A future HaloFuse-built image changes only this file (image, command and manifest name
together). The image is pinned by digest, so a bridge never runs an image it was not written for.
"""
from __future__ import annotations

IMAGE = "ghcr.io/nithin-ml/spark-fuse-comfyui:sha-70e0ff4b38b431b2c77e90f2936f93d04516ccc9"
COMMAND = ("python3.13", "/runner/spark_fuse_run.py")
# The queue-chunk manifest file the runner reads from its input folder (multi-workflow mode).
MANIFEST_NAME = "spark_fuse_job.json"

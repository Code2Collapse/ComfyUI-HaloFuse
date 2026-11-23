# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
#
# HaloFuse runtime image: headless ComfyUI that renders API-format workflows as batch jobs.
# Model-agnostic: no model weights are baked in or downloaded at build; models arrive per job under
# MODEL_BASE_DIR (default /input/models, models mount).
#
# The custom-node set is a catalog profile (catalog/profiles.json), chosen with the PROFILE build arg:
#   default  = exactly the 31 packs of the image in production before the catalog (saved workflows keep working)
#   full     = the HaloFuse image: every pack of `default` plus every pack the licence policy allows, + extra_nodes.txt
#   core | image | video | audio | depth-segmentation | vfx | prompting-utilities | research-experimental
#   docker buildx build --build-arg PROFILE=video -t halofuse-runtime:video .
# Optional attention layer: --build-arg ATTENTION=sage (SageAttention 1.0.6; launch preset "sage"; experimental).
#
# Layer order (least to most frequently changing): base -> tini -> node sources -> node dependencies ->
# OpenCV/onnxruntime normalisation -> system libraries -> attention -> Haar cascades -> bytecode -> snapshot -> runner.
# Only the scripts a layer needs are copied before it, so editing the healthcheck, snapshot or smoke scripts never
# re-runs the dependency layers. BuildKit cache mounts keep downloaded archives and pip wheels between builds; they
# never enter the image.
# Stages after `runtime` (smoke, smoke-artifact) only test the image: `--target smoke-artifact` boots the real
# entrypoint (tini + runner) on CPU without network and fails the build when ComfyUI or a strict pack crashes.

# Base pinned by digest: the moving cu130-slim-v2 tag silently changed ComfyUI between rebuilds. The ComfyUI
# version inside this pin is recorded by the snapshot step (/etc/halofuse/node_snapshot.json, comfyui_version);
# it was not inspected outside a build (no Docker on the machine that wrote this file).
# 20261119 = ComfyUI v0.46.0 (the newest stable release; the base checks out the newest v* tag) with the
# comfyui-frontend-package that release pins (1.59.6). The cloud runtime is headless, so the frontend is never served
# to an artist; it stays at the version core pins. Digest from Docker Hub, 2026-11-23.
# Rollback targets (previous production pins):
#   yanwk/comfyui-boot:cu130-slim-v2-20261027@sha256:9c222bf018d44b9490a1e21f832e6555f4845ebd34c57cb2371a6fb246f99e98 (0.43.0)
#   yanwk/comfyui-boot:cu130-slim-v2-20260803@sha256:9cde099f3fa5b39a58ceadf4562d4e157b4f5e3008d1690d6c5bba9ffd0cffbb
#   yanwk/comfyui-boot:cu130-slim-v2-20260717@sha256:e2fada7c3db5ed043d99b336ab2e1b68d100cac74e094ad39a28831e1bb7fffb
ARG BASE_IMAGE=yanwk/comfyui-boot:cu130-slim-v2-20261119@sha256:1ebd87fc7a22c0edc53e54620d4f93915a09bbb3ac99df466ac3a086d058a412

# ------------------------------------------------------------------------------------------------ base
FROM ${BASE_IMAGE} AS base
# The base's entrypoint copies the bundle to /root on first start; we skip all of that: nodes are installed into
# the bundle itself and the runner launches ComfyUI from the bundle path directly.
# ComfyUI's Python stack (torch, pip) lives under python3.13; plain `python3` in this base is a bare 3.12 with no
# pip. Everything here must use python3.13. (No PIP_NO_CACHE_DIR: the pip cache is a BuildKit mount, not a layer.)
ENV COMFYUI_DIR=/default-comfyui-bundle/ComfyUI \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ------------------------------------------------------------------------------------------------ fetch
# Downloads every pack of the profile at its pinned version and verifies the catalog content hash. Only the
# catalog and two scripts are inputs, so runner or Dockerfile edits below never re-download node sources.
FROM base AS fetch
ARG PROFILE=default
ARG REQUIRE_HASHES=0
COPY catalog/catalog.json catalog/profiles.json catalog/extra_nodes.txt /build/catalog/
COPY scripts/hf_catalog.py scripts/install_nodes.py /build/scripts/
RUN --mount=type=cache,id=halofuse-archives,target=/var/cache/halofuse/archives \
    python3.13 /build/scripts/install_nodes.py fetch \
      --catalog-dir /build/catalog --profile "$PROFILE" --dest /build/custom_nodes \
      --cache /var/cache/halofuse/archives --report /build/install_report.json \
      $( [ "$REQUIRE_HASHES" = "1" ] && echo --require-hashes )

# ------------------------------------------------------------------------------------------------ nodes
FROM base AS nodes
ARG PROFILE=default

# Init process for PID 1 (WS4 request R-WS4-1, audit X5): forwards SIGTERM/SIGINT to the runner and reaps zombies.
# Static tini 0.19.0, SHA-256 verified before it is installed (matches the release's published .sha256sum). Fetched in
# a RUN step, not `ADD --checksum`: the CI linter (hadolint) does not parse that flag. A mismatch fails the build.
ARG TINI_URL=https://github.com/krallin/tini/releases/download/v0.19.0/tini-static-amd64
ARG TINI_SHA256=c5b0666b4cb676901f90dfcb37106783c5fe2077b04590973b885950611b30ee
RUN python3.13 -c "import hashlib, os, sys, urllib.request; \
data = urllib.request.urlopen(os.environ['TINI_URL'], timeout=120).read(); \
got = hashlib.sha256(data).hexdigest(); \
sys.exit('tini checksum mismatch: ' + got) if got != os.environ['TINI_SHA256'] else None; \
open('/usr/local/bin/tini', 'wb').write(data); os.chmod('/usr/local/bin/tini', 0o755)"

# The base ships ComfyUI-Manager as a pip package with its startup cache-update already patched out; this rm only
# guards against bundle layouts that drop a self-updating Manager into custom_nodes (it is never in a profile).
RUN rm -rf "$COMFYUI_DIR/custom_nodes/ComfyUI-Manager"

COPY catalog/catalog.json catalog/profiles.json /opt/halofuse/catalog/
COPY scripts/hf_catalog.py scripts/install_nodes.py scripts/install_haarcascades.py /opt/halofuse/scripts/
COPY --from=fetch /build/custom_nodes/ ${COMFYUI_DIR}/custom_nodes/
COPY --from=fetch /build/install_report.json /etc/halofuse/install_report.json

# Node dependencies. torch/torchvision/torchaudio stay at the base build. Strict profiles (default) fail on any
# error; other profiles isolate a failing pack (moved to custom_nodes_disabled/, marked in the install report).
# The base image ships gcc/g++, cmake, ninja and python3.13-devel, so source-only requirements compile here.
# CMAKE_ARGS only affects CMake-built sdists (llama-cpp-python): a portable x86-64 build (AVX2/FMA/F16C) instead of
# -march=native of the CI machine, which could crash on a cloud host with a different CPU.
RUN --mount=type=cache,id=halofuse-pip,target=/root/.cache/pip \
    CMAKE_ARGS="-DGGML_NATIVE=OFF -DGGML_AVX=ON -DGGML_AVX2=ON -DGGML_FMA=ON -DGGML_F16C=ON" \
    python3.13 /opt/halofuse/scripts/install_nodes.py deps \
      --catalog-dir /opt/halofuse/catalog --profile "$PROFILE" \
      --custom-nodes "$COMFYUI_DIR/custom_nodes" --report /etc/halofuse/install_report.json

# Custom nodes pull different OpenCV wheels (opencv-python, -headless and -contrib). All four distributions write
# the same cv2 package, so the last install overwrites the others' files and leaves a mixed cv2. Keep exactly one:
# contrib (LayerStyle needs cv2.ximgproc), headless (no GUI in a batch container), at the newest version any node
# pulled in. --no-deps keeps numpy.
RUN --mount=type=cache,id=halofuse-pip,target=/root/.cache/pip python3.13 - <<'PY'
import importlib.metadata as md
import subprocess
import sys

DISTS = ("opencv-python", "opencv-python-headless",
         "opencv-contrib-python", "opencv-contrib-python-headless")

def version_key(v):
    return tuple(int(p) if p.isdigit() else 0 for p in v.split("."))

found = {}
for dist in DISTS:
    try:
        found[dist] = md.version(dist)
    except md.PackageNotFoundError:
        pass
print("opencv distributions before:", found, flush=True)
if found:
    target = max(found.values(), key=version_key)
    subprocess.check_call([sys.executable, "-m", "pip", "uninstall", "-y", *found])
    subprocess.check_call([
        sys.executable, "-m", "pip", "install", "--no-deps",
        f"opencv-contrib-python-headless=={target}",
    ])
subprocess.check_call([sys.executable, "-c",
    "import cv2; cv2.ximgproc.guidedFilter; print('cv2', cv2.__version__, 'with contrib', flush=True)"])
PY

# Benign noise from the base image, cleaned up without moving the ComfyUI pin.
# - pynvml 13 registers a meta-path finder from a .pth file at interpreter startup and warns on every import
#   pynvml. Uninstalling that distribution drops the finder and leaves the pynvml module from nvidia-ml-py.
# - PyOpenGL logs a missing accelerator. Install it at PyOpenGL's own version.
# - matplotlib builds its font cache on first use, which a fresh container does on every job: build it once here.
# - The base installs onnxruntime (CPU) and a CUDA 13 nightly over the same module path. 1.30.0 is the PyPI CUDA 13
#   build whose GPU discovery skips DRM cards that have no PCI vendor file, instead of warning about them.
RUN --mount=type=cache,id=halofuse-pip,target=/root/.cache/pip python3.13 - <<'PY'
import importlib.metadata
import os
import subprocess
import sys

def installed(dist):
    try:
        importlib.metadata.version(dist)
        return True
    except importlib.metadata.PackageNotFoundError:
        return False

subprocess.call([sys.executable, "-m", "pip", "show", "pynvml", "onnxruntime", "onnxruntime-gpu"])

if installed("pynvml"):
    subprocess.check_call([sys.executable, "-m", "pip", "uninstall", "-y", "pynvml"])

pyopengl = importlib.metadata.version("PyOpenGL")
subprocess.check_call([sys.executable, "-m", "pip", "install", f"PyOpenGL-accelerate=={pyopengl}"])

for dist in ("onnxruntime", "onnxruntime-gpu"):
    if installed(dist):
        subprocess.check_call([sys.executable, "-m", "pip", "uninstall", "-y", dist])
subprocess.check_call([sys.executable, "-m", "pip", "install", "--no-deps", "onnxruntime-gpu==1.30.0"])

subprocess.check_call([sys.executable, "-c", r"""
import sys
import warnings
from pathlib import Path

leftover = [
    path
    for root in map(Path, sys.path)
    for name in ("_pynvml_redirector.pth", "_pynvml_redirector.py")
    if (path := root / name).is_file()
]
if leftover:
    raise SystemExit("pynvml redirector still installed: " + ", ".join(map(str, leftover)))

with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    import pynvml
bad = [str(item.message) for item in caught if "pynvml package is deprecated" in str(item.message)]
if bad:
    raise SystemExit(bad[0])
import OpenGL_accelerate
import onnxruntime as ort
providers = ort.get_available_providers()
print("onnxruntime", ort.__version__, providers, flush=True)
if "CUDAExecutionProvider" not in providers:
    raise SystemExit("CUDAExecutionProvider missing from onnxruntime")
"""])

os.environ["MPLBACKEND"] = "Agg"
import matplotlib.font_manager as fm
fm.fontManager
PY

# System libraries a pack loads at import time that the base image lacks (L9.323); after the pip layers, so editing
# this list never re-runs them. Never in `default`: that image stays the original image (frozen production parity).
# portaudio (EPEL 10, which the base already enables): the sounddevice module ComfyUI_AudioTools imports at load
# raises "PortAudio library not found" without it. The check resolves the library the way sounddevice does
# (ctypes.util.find_library), so a missing one fails the build.
# DL3040: the dnf cache is a BuildKit mount, not a layer. DL3041: EPEL keeps only its newest build, so an exact
# version pin would break the next rebuild.
# hadolint ignore=DL3040,DL3041
RUN --mount=type=cache,id=halofuse-dnf,target=/var/cache/dnf,sharing=locked \
    if [ "$PROFILE" = "default" ]; then echo "extra system packages: none (default profile)"; \
    else dnf install -y --setopt=install_weak_deps=False portaudio && rpm -q portaudio \
      && python3.13 -c "import ctypes.util, sys; sys.exit(0 if ctypes.util.find_library('portaudio') else 'portaudio not found by ctypes')" \
      && { { dnf install -y --setopt=install_weak_deps=False sox && rpm -q sox && command -v sox; } \
           || echo "TRIAL sox: not installable from the enabled repositories"; }; fi

# Optional attention backend (D2). Only one prebuilt wheel fits this base (torch 2.13 / CUDA 13 / Python 3.13):
# SageAttention 1.0.6, a pure-Python Triton implementation (Apache-2.0 upstream), hash-pinned. SageAttention 2.x
# publishes no Linux wheels and FlashAttention 2.8.3 wheels stop at torch 2.8 / CUDA 12, so neither is installable
# without compiling (no compiler in this image). ATTENTION=none (default) leaves the image unchanged.
ARG ATTENTION=none
# The pip cache is a BuildKit cache mount (never a layer), as in the other pip steps.
# hadolint ignore=DL3042
RUN --mount=type=cache,id=halofuse-pip,target=/root/.cache/pip \
    if [ "$ATTENTION" = "sage" ]; then \
      echo "sageattention==1.0.6 --hash=sha256:fafc66569bed62a16839e820c2612141b5a20accf55b876d941bab9c0ac5d888" \
        > /tmp/attention-requirements.txt \
      && python3.13 -m pip install --no-deps --require-hashes -r /tmp/attention-requirements.txt \
      && python3.13 -c "import triton; from sageattention import sageattn; print('attention layer: sage', flush=True)" \
      && rm -f /tmp/attention-requirements.txt; \
    elif [ "$ATTENTION" = "none" ]; then echo "attention layer: none"; \
    else echo "unknown ATTENTION=$ATTENTION (none or sage)"; exit 1; fi

# OpenCV 5 wheels no longer ship the Haar cascade XML files. EasyColorCorrector and WAS load them from
# cv2.data.haarcascades and silently detect nothing without them. Vendored copies (OpenCV 4.12.0, licence notice in
# catalog/vendor/opencv-haarcascades/NOTICE.md) are checksum-verified, copied, and must load (no network: X13).
COPY catalog/vendor/opencv-haarcascades/ /opt/halofuse/haarcascades/
RUN python3.13 /opt/halofuse/scripts/install_haarcascades.py /opt/halofuse/haarcascades

# Cold start: precompile custom-node bytecode once instead of on every container start. Files that do not compile
# under this Python are reported, not fatal (ComfyUI reports their import failure the same way at boot).
RUN python3.13 -m compileall -q -j 0 "$COMFYUI_DIR/custom_nodes" > /tmp/compileall.log 2>&1 \
    || { echo "compileall reported errors (first lines):"; head -n 20 /tmp/compileall.log; }; \
    rm -f /tmp/compileall.log

# ------------------------------------------------------------------------------------------------ snapshot
# Contract C-5: boot ComfyUI once on CPU (loopback only), record which packs loaded and their node classes.
# Runs in its own stage so files ComfyUI writes while booting never reach the final image; only the JSON is copied.
FROM nodes AS snapshot
ARG PROFILE=default
ARG SNAPSHOT_BOOT=1
COPY scripts/export_node_snapshot.py /opt/halofuse/scripts/
RUN mkdir -p /out && if [ "$SNAPSHOT_BOOT" = "1" ]; then \
      python3.13 /opt/halofuse/scripts/export_node_snapshot.py export --boot \
        --comfyui-dir "$COMFYUI_DIR" --python python3.13 --profile "$PROFILE" \
        --catalog-dir /opt/halofuse/catalog --install-report /etc/halofuse/install_report.json \
        --image-ref "halofuse-runtime:$PROFILE" --out /out/node_snapshot.json; \
    else \
      python3.13 /opt/halofuse/scripts/export_node_snapshot.py export --catalog-only \
        --profile "$PROFILE" --catalog-dir /opt/halofuse/catalog \
        --install-report /etc/halofuse/install_report.json \
        --image-ref "halofuse-runtime:$PROFILE" --comfyui-dir "$COMFYUI_DIR" --out /out/node_snapshot.json; \
    fi

# `docker buildx build --target snapshot-artifact --output type=local,dest=out .` exports just the snapshot.
FROM scratch AS snapshot-artifact
COPY --from=snapshot /out/node_snapshot.json /node_snapshot.json

# ------------------------------------------------------------------------------------------------ runtime
FROM nodes AS runtime
ARG PROFILE=default
ARG ATTENTION=none
COPY --from=snapshot /out/node_snapshot.json /etc/halofuse/node_snapshot.json
COPY scripts/healthcheck.py /opt/halofuse/scripts/
ENV HF_PROFILE=${PROFILE} \
    HF_IMAGE_REF=halofuse-runtime:${PROFILE} \
    HF_NODE_SNAPSHOT_PATH=/etc/halofuse/node_snapshot.json \
    HF_ATTENTION=${ATTENTION} \
    HF_HEALTH_FILE=/tmp/halofuse-health.json

COPY runner/ /runner/
# Cold start: the runner's own bytecode is compiled once here (the build context carries no __pycache__).
RUN python3.13 -m compileall -q /runner

# D3 / R-WS4-3: every flag the runner's launch allowlist (and its presets) uses must exist in the pinned ComfyUI's
# cli_args.py, so a base-image bump cannot silently break a preset. `python -m hf.launch` exits 1 listing the missing
# flags (check_allowlist_against() alone only returns the list, so it is not used directly here).
RUN cd /runner && python3.13 -m hf.launch /runner/launch_allowlist.json "$COMFYUI_DIR/comfy/cli_args.py"

# The runner is PID 1's only child; tini forwards SIGTERM (no -g: the runner interrupts ComfyUI itself and collects
# partial outputs before exiting, C-3).
STOPSIGNAL SIGTERM
# Health = the runner's heartbeat (HF_HEALTH_FILE) is fresh, and ComfyUI answers /system_stats whenever the runner
# says it is up. Restarts after a crash, prefetch and shutdown are healthy as long as the runner is alive.
HEALTHCHECK --interval=15s --timeout=5s --start-period=900s --retries=3 \
    CMD ["python3.13", "/opt/halofuse/scripts/healthcheck.py"]
# tini is the ENTRYPOINT and the runner the CMD. -s registers tini as a child subreaper, so zombies are still reaped
# when the platform injects its own init and tini is not PID 1. A bridge written for this image submits command
# ["python3.13", "/runner/halofuse_run.py"] (bridges pin the image they were written for by digest, together with its
# command): with Docker CMD semantics that becomes `tini -- python3.13 /runner/...`; with entrypoint-override
# semantics it is the runner alone (as before, the runner handles SIGTERM itself). Which one the platform uses is
# provider probe P6.
ENTRYPOINT ["/usr/local/bin/tini", "-s", "--"]
CMD ["python3.13", "/runner/halofuse_run.py"]

# ------------------------------------------------------------------------------------------------ smoke
# Must not crash: run the real entrypoint (tini + runner) on CPU with no network and a model-free 3-entry manifest
# (ok / unknown node / ok). Fails the build when ComfyUI does not boot, the runner hangs or exits wrongly, or a pack of
# a strict profile does not import. A second boot with --disable-partner-nodes is timed for comparison. Never part
# of the runtime image; `--target smoke-artifact` exports the report.
FROM runtime AS smoke
COPY scripts/smoke_run.py /opt/halofuse/scripts/
RUN --network=none mkdir -p /out && python3.13 /opt/halofuse/scripts/smoke_run.py \
      --tini /usr/local/bin/tini --runner /runner/halofuse_run.py --comfyui-dir "$COMFYUI_DIR" --python python3.13 \
      --install-report /etc/halofuse/install_report.json --out /out/smoke_report.json --timeout 900 \
      --compare-args=--disable-partner-nodes

FROM scratch AS smoke-artifact
COPY --from=smoke /out/smoke_report.json /smoke_report.json

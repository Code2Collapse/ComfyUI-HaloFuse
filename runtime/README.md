# HaloFuse Cloud Runtime

The Docker image that runs [ComfyUI](https://github.com/comfyanonymous/ComfyUI) headlessly on HaloFuse cloud GPUs.
One container = one cloud job: the job delivers an API-format workflow (or a manifest of several), the container
renders it, and the results are written to `/output`, from where HaloFuse brings them back to the artist's ComfyUI.

This folder is `runtime/` of the [ComfyUI-HaloFuse](../README.md) repository. ComfyUI does not load it; artists never
build or run it. It is for whoever builds and maintains the cloud runtime. Artist documentation: the
[user manual](../docs/manual.md) and the [getting-started guide](USER-GUIDE.md).

> **Release status: 0.4.0, in development.** The 0.4.0 runner below is in the code and tested against a fake
> ComfyUI. The 0.4.0 **image has not been built**: no Docker build, cold-start or GPU measurement has been run, so
> image size, build time and GPU behaviour are **not verified**. Items marked *in development* are not in the code yet.

## How it works

```
cloud GPU machine
└── this image (one job per container)
    ├── /input    workflow.json (API format) or halofuse_job.json (manifest), plus staged input files
    ├── /assets   the artist's cloud model library, mounted read-only (MODEL_BASE_DIR=/assets)
    ├── runner    runner/halofuse_run.py (the entry point) + runner/hf/ (supervisor, events, telemetry, ...)
    │     1. start ComfyUI on 127.0.0.1:8188 (loopback only, no published ports)
    │     2. wait until it is ready; relay its websocket progress as run events
    │     3. queue each workflow (render repeats with fresh seeds) and follow it to completion
    │     4. on a crash or out-of-memory error: classify it, recover per policy, keep what finished
    │     5. collect outputs (subfolders kept), print the final summary, exit 0..6
    └── /output   ComfyUI writes results here; persisted for delivery
```

All ComfyUI and runner output goes to stdout, which HaloFuse streams to the artist's console.

## Job contract (stable; older extensions depend on it)

| Path / name | Meaning |
| --- | --- |
| `/input/workflow.json` | one **API-format** workflow (UI-format exports are rejected with a clear error) |
| `/input/halofuse_job.json` | manifest `{"workflows": [{"workflow": {...}, "batch_count": N}]}` for a queue chunk; each entry's outputs go to `/output/wf_NN/` with their subfolders |
| `MODEL_BASE_DIR` | model root (`/input/models` by default, `/assets` for the cloud model library); the runner writes `extra_model_paths.yaml` from it |
| `/output/` | results; `/output/_halofuse/` holds the runner's own files (render log, events, previews) and is never an output |
| stdout markers | `[runner] workflow M/T started`, `succeeded (N file(s))`, `failed: reason (N partial file(s))`, unchanged |
| run events | `[hf] {"v":1,"t":...}` lines next to the markers: phases, progress, node timing, telemetry, previews, recovery, final summary |

Runner exit codes: `0` success · `1` workflow execution error, some manifest entry failed, or cancelled · `2` bad
input or a rejected launch option · `3` workflow rejected at submission · `4` job timeout · `5` completed but no
output files · `6` ComfyUI failed to start or died (and no restart was allowed or it failed).

Environment: `STARTUP_TIMEOUT_SECONDS` (default 300), `JOB_TIMEOUT_SECONDS` (default 0 = unlimited), `BATCH_COUNT`
(render repeats, 1–100), `ALLOW_PARTIAL_OUTPUTS`, `PREFETCH_MODELS` / `PREFETCH_THREADS` (copy the referenced models
from `/assets` to local disk first), `COMFY_PORT` (default 8188), and for 0.4.0: `HF_PRESET` (launch preset),
`COMFY_EXTRA_ARGS` / `EXTRA_ENV` (checked against `runner/launch_allowlist.json`; anything else is rejected before
ComfyUI starts, and the argument list never goes through a shell), `HF_RESTART_POLICY` (`off`, `once` (default),
`twice`), `HF_OOM_LADDER` (out-of-memory recovery steps; `off` disables), `HF_PREVIEW` (live previews, experimental).

## What the 0.4.0 runner adds

- **Supervision.** Every failure is classified (for example `host_ram_exhausted`, `vram_oom`, `native_crash`).
  When ComfyUI dies mid-run (for example killed for lack of memory) it is restarted once by default, with
  memory-safe options when memory was the cause, so one failed workflow in a queue chunk no longer stops the rest.
  `HF_RESTART_POLICY=off` keeps the previous behaviour.
- **Clean stops.** On SIGTERM the runner interrupts ComfyUI, keeps the partial outputs, prints a final `cancelled`
  summary and exits within 10 seconds.
- **Run events and telemetry.** Progress, node timing, GPU and memory samples and previews are emitted as bounded
  `[hf]` lines (at most 64 KiB each, previews throttled).
- **Outputs keep their subfolders**, also in manifest mode (two outputs with the same name no longer collide).

The runner's tests (kept in the development repository) cover these cases against a fake ComfyUI; they are
not a GPU run.

## Models are not in the image

Models live in the artist's cloud model library and are mounted at `/assets` per job. The image never contains model
weights, and the build never downloads models.

## Custom nodes

The custom-node set comes from the catalog in [catalog/](catalog/README.md): one entry per pack with its pinned
version, content hash, licence and status, and named profiles built as separate images (`PROFILE` build argument).
`python scripts/hf_catalog.py summary` lists the profiles. To add packs without editing JSON, list them in
[`catalog/extra_nodes.txt`](catalog/extra_nodes.txt) (`owner/repo` or `owner/repo@ref` per line): each is pinned to
an exact commit at build time and is optional - one that fails to install is moved aside and the build continues. The `default` profile is exactly the 31 packs of the image
the extension uses today, so saved workflows keep working; any install failure fails that build. In the other
profiles a pack that fails to install is moved aside and reported, and the build continues. ComfyUI-Manager is never
installed (a batch container must not install or update code by itself).

Licences travel with each pack inside the image. The catalog flags packs whose licence needs a decision before
commercial use; in `default` that includes RES4LYF (its LICENSE forbids running it as a commercial AI-image service
without permission), DepthCrafter-Nodes (research only) and the LTX-2 community licence. Flags are reported, not
applied, in `default`; the newer profiles leave out non-commercial, custom-licence and unlicensed packs.

*Not verified yet:* image build time, size and cold start (`scripts/measure.py` records them on a real build), and an
import test of every pack in each profile.

## Base image pin

The `FROM` line in the [Dockerfile](Dockerfile) pins the base image by digest, not by tag, because the upstream tag is
rebuilt for each ComfyUI release. Changing the pin is the deliberate way to upgrade ComfyUI: check the custom nodes
against the new version and test on a branch first. The comment above the `FROM` line records the previous pin.

## Build

From this folder:

```
docker build -t halofuse-runtime:local .
```

The CI workflow builds `linux/amd64`. The registry that HaloFuse images are published to is configured by the HaloFuse
administrator; nothing is pushed by this repository's tooling. After publishing, the extension's image pin must be
updated so renders use the new image.

## Local test run

With Docker and an NVIDIA GPU:

```powershell
docker run --rm --gpus all `
  -v "C:\path\to\job\input:/input:ro" `
  -v "C:\path\to\job\output:/output" `
  halofuse-runtime:local
```

where `input\` contains `workflow.json` and `models\checkpoints\...`.

## Tests

The runner suite (the real runner against a fake ComfyUI: no Docker, GPU or network) lives in the development
repository and is not part of this distribution.

## Licence

AGPL-3.0, see [../LICENSE](../LICENSE); the original MIT notice is in [../LICENSE-MIT](../LICENSE-MIT) and
[NOTICE](NOTICE). The built image contains third-party software under its own
licences (NOTICE).

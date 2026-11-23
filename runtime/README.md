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
ComfyUI starts, and the argument list never goes through a shell), `HF_RESTART_POLICY` (`each` (default: one
restart per manifest entry), `once`, `twice`, `off`), `HF_UNRESPONSIVE_SECONDS` (default 300: a live ComfyUI that
stops answering for this long is stopped and restarted; `0` disables), `HF_OOM_LADDER` (out-of-memory recovery steps;
`off` disables), `HF_PREVIEW` (live previews, experimental), `HF_HEALTH_FILE` (runner heartbeat read by the Docker
HEALTHCHECK) and `HF_ALLOW_CPU=1` (adds `--cpu` only when no NVIDIA GPU is visible; used by the image smoke test).

## What the 0.4.0 runner adds

- **Supervision.** Every failure is classified (for example `host_ram_exhausted`, `vram_oom`, `native_crash`).
  When ComfyUI dies mid-run (for example killed for lack of memory) or stops answering, it is restarted - with
  memory-safe options when memory was the cause - and the job continues with the next workflow. By default every
  manifest entry may use one restart; three deaths in a row with no successful workflow between them end the job
  (exit 6), so a crash loop cannot run up a bill. ComfyUI runs in its own process group: stopping it stops its
  child processes too, so nothing keeps memory into the restarted ComfyUI or keeps the container alive.
  `HF_RESTART_POLICY=off` keeps the previous behaviour.
- **Model prefetch only when it helps.** Models are copied from the model mount to local disk only when the mount is
  remote and the destination is a real disk (not a RAM-backed `/tmp`); a stopped job never waits for a copy.
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
an exact commit at build time and is optional - one that fails to install is moved aside and the build continues.
Lines naming a pack the catalog keeps out of cloud images (UI-only packs, ComfyUI-Manager, research repositories) are
rejected with the catalog's reason (`python scripts/install_nodes.py check-extras` checks the file offline; CI runs
it). **One command:** `python scripts/add_node.py <https://github.com/owner/repo | owner/repo[@ref] | registry-id>
[--profile full]` pins the pack to a commit, reads its licence, appends it to `extra_nodes.txt` and validates the
catalog (file restored on any problem; `--dry-run` writes nothing); commit the file and CI builds the image. Artists
can also add packs for their own jobs only (panel: *My custom nodes*, job env `HF_USER_NODES`, installed at job start
by `runner/hf/user_nodes.py`; the image's node snapshot then names `user_nodes/1`; never the Original image).
Each extra's licence is recorded in the install report and anything other than a permissive licence is
flagged: listing a pack there is the owner's decision. The `default` profile is exactly the 31 packs of the original
image at their original pins (frozen, no extras), so its snapshot describes that image; any install failure fails
that build. The `full` profile is the HaloFuse image: the 31 original packs at current pins plus every catalog pack
the licence policy allows plus the extras (the artist chooses it in the extension under Advanced > Cloud runtime).
CI builds both on every push. In every profile but `default` a pack that fails to install is moved aside and
reported, and the build continues. Pins are moved to the newest releases with `scripts/update_pins.py`
([catalog/PIN_UPDATES.md](catalog/PIN_UPDATES.md) lists the last update). ComfyUI-Manager is never
installed (a batch container must not install or update code by itself).

Licences travel with each pack inside the image. The catalog flags packs whose licence needs a decision before
commercial use; in `default` that includes RES4LYF (its LICENSE forbids running it as a commercial AI-image service
without permission), DepthCrafter-Nodes (research only) and the LTX-2 community licence. Flags are reported, not
applied, in `default`; the newer profiles leave out non-commercial, custom-licence and unlicensed packs.

Every CI build runs the `smoke` stage: the real entrypoint (tini + runner) boots ComfyUI on the CPU without network
and renders a model-free 3-workflow manifest (one entry fails on purpose). The build fails when ComfyUI does not
start, the runner hangs or exits with the wrong code, or a pack of a strict profile does not import; the report
(`smoke_report.json`, per-pack import times included) is uploaded with the build. GPU cold start on the cloud node is
not measured by it.

## Base image pin

The `FROM` line in the [Dockerfile](Dockerfile) pins the base image by digest, not by tag, because the upstream tag is
rebuilt for each ComfyUI release. Changing the pin is the deliberate way to upgrade ComfyUI: check the custom nodes
against the new version and test on a branch first. The comment above the `FROM` line records the previous pin.
Current pin (2026-11-23): ComfyUI 0.46.0, the newest stable release, with the frontend package it pins (1.59.6).

## Build

From this folder:

```
docker build -t halofuse-runtime:local .
```

Optional attention layer: `--build-arg ATTENTION=sage` adds SageAttention 1.0.6 (the only prebuilt wheel for this
base: Triton-based, hash-pinned) and enables the `sage` launch preset; speed-up on GPUs is not verified.
FlashAttention has no wheel for this torch/CUDA build.

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

# Custom-node catalog

`catalog.json` lists every repository of the work order (246 entries; one more is kept in a private list) with the 16 required fields; `profiles.json`
defines the runtime profiles and the licence policy. `catalog.schema.json` is the JSON Schema;
`python scripts/hf_catalog.py validate | summary | resolve <profile>` checks and explains them.

## Reading an entry

| Field | Meaning |
|---|---|
| `category` | backend, frontend (UI only), combined, core, research, workflows, external-tool, incompatible, reference-only |
| `install_target` | `cloud` (may go into an image), `local` (artist's own ComfyUI only), `reference-only` |
| `install` | `registry` (Comfy Registry version), `git-archive` (GitHub archive at `pin.commit`), `base-image`, `local-user`, `none` |
| `compatibility.compatible` | `true` = no blocker found by static inspection (not an import test); `"unknown"` = needs a build/import test first; `false` = cannot work as listed |
| `verified.status` | `NOT VERIFIED` unless an import test ran; image snapshots (`/etc/halofuse/node_snapshot.json`) carry the real per-pack result |
| `pin.content_sha256` | sha256 of the extracted tree; the build refuses a different archive. `null` = not locked yet (`scripts/lock_catalog.py --profile P --write`, needs network) |
| `license.class` | permissive, copyleft, network-copyleft (AGPL), non-commercial, restrictive-custom, no-licence, unknown |
| `model_download` | evidence from GitHub code search at the pin; the image never downloads models at build time |

Unknown values are written as `{"value": "unknown", "reason": "..."}`, never omitted.

## Profiles

`default` reproduces the image in production before the catalog (same 31 pins), so saved workflows keep working; it
is strict (any install failure fails the build) and its licence flags are reported, not applied. The other cloud
profiles (`core image video audio depth-segmentation vfx prompting-utilities research-experimental full`) take the
entries that list them, keep only cloud entries with `compatible: true`, apply the licence policy and drop the later
of two conflicting packs. A failing optional pack is isolated at build time (moved to `custom_nodes_disabled/`,
marked in `/etc/halofuse/install_report.json`). `full` is every compatible backend entry. `frontend-extensions` is a
local list and is never built into an image. ComfyUI-Manager is in no cloud profile.

## Licence policy (owner decision 12 is open)

Lead default in `profiles.json`: GPL/AGPL packs are installed but flagged (publishing an image creates source-offer
obligations; AGPL also for users of a hosted service); non-commercial, custom-restrictive and unlicensed packs are left
out of the new profiles until the owner decides. Change `licence_policy` to change that. The production `default` set
contains packs in those classes (RES4LYF, DepthCrafter-Nodes: non-commercial; LTXVideo: LTX-2 Community License;
Comfyroll, ComfyLiterals: no licence file): they stay because they are already shipped, and are flagged.

## Building

`docker buildx build --build-arg PROFILE=<profile> -t halofuse-runtime:<profile> .` from this runtime folder (the
build context is the folder holding the Dockerfile, so it works unchanged from a `runtime/` subfolder:
`docker buildx build runtime/`). `--build-arg REQUIRE_HASHES=1` makes an unlocked pack fatal.

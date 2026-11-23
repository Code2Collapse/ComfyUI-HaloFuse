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
| `install.requirement_overrides` | non-strict profiles only (`full` etc.; never `default`): rewrite one upstream requirement line, e.g. a `numpy<2` cap the image moved past (`use: null` drops the line); `upstream` must match the pack's line exactly or the override is reported stale and not applied. Each has a `reason` |
| `install.extra_requirements` | non-strict profiles only: a package the pack imports at load time but its requirements.txt omits, with a `reason` |
| `install.submodules` | git submodules an archive install lacks (GitHub archives and registry zips leave the gitlink empty): `{path, repo, commit, reason}`, fetched at that exact commit into the pack folder after the pack's own content hash (so `pin.content_sha256` stays the upstream tree); optional `content_sha256` per submodule |
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

A trial profile may list `try_unknown: [owner/repo, ...]`: those entries are installed even though their
compatibility is still `unknown`, so an image build measures them (flagged "trial" in the install report).
`compatible: false`, the licence policy and conflicts still apply. `full-candidates` is such a profile (CI
builds it on request with push=false); a pack that installs and imports there gets `compatible: true` with the
CI run as evidence, and one that does not keeps a measured reason.

Since 2026-11-23 (owner: "add all the nodes ... updating all nodes to the latest"):

- **`full` is the HaloFuse image** (the bridge's `halofuse` runtime, Advanced > Cloud runtime). Its
  `include_members_of: ["default"]` puts every pack of the original image in first, at the catalog's current pins and
  under the same licence exemption (flagged), so switching an artist to the HaloFuse image never loses a node. Then
  every compatible backend entry the licence policy allows, then `extra_nodes.txt`. CI builds `default` and `full` on
  every push (`<profile>-sha-<commit>` tags).
- **`default` is frozen**: its `pins` (profiles.json) hold the production pins, so moving catalog pins to newer
  releases never changes it, and `extras: false` keeps `extra_nodes.txt` out of it. Its snapshot therefore describes
  the original image, which is what the Workflow Check uses for the "Original image" runtime.

## Updating pins

`python scripts/update_pins.py --write --markdown catalog/PIN_UPDATES.md` (network; `GITHUB_TOKEN` optional) moves
every installable cloud entry to the newest Comfy Registry version whose registry status is *active* (versions the
registry flags or deprecates are skipped and named; a pin is never moved backwards) or to the default branch HEAD,
clears the old content hash and refreshes the requirement list. A GitHub-detected licence that changed upstream holds
the old pin for review. Then `python scripts/lock_catalog.py --profile full --write` records the new content hashes.
The last run's old -> new table is [PIN_UPDATES.md](PIN_UPDATES.md). Pins move; compatibility verdicts were made
at the older pins (static inspection) and are re-checked only by the image build's install and smoke stages.

## Size and start-up of the HaloFuse image (2026-11-23)

MEASURED offline (`lock_catalog.py --profile full --stats-out`): the 147 `full` packs are 1276 MB of archives and
1781 MB / 21,575 files extracted; the 31 `default` packs at the same pins are 371 MB / 508 MB. The `full` packs name
130 distinct pip requirements against 81 for `default` (new heavy ones: openai-whisper / faster-whisper, librosa,
ultralytics, open-clip-torch, ai-edge-litert, moderngl). ESTIMATE (not built yet): custom_nodes layer about 3.5x the
measured 388 MiB, so 1.3-1.6 GiB, plus 0.5-2 GiB of dependencies: a compressed image of roughly 7.5-9 GiB instead of
5.9 GiB, a longer first pull on a cold machine, and a ComfyUI start that imports about 4.7x as many packs (12.4 s for
31 packs on the CI CPU). Artists who need speed keep the Original image; a smaller profile can be published as its
own image later. The first CI build of `full` records the real numbers (smoke_report.json, measurement.json).

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

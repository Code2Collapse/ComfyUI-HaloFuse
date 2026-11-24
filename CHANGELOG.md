# Changelog

## Unreleased — 2026-11-23

- **Warm machine: modes, warm duration and costs (2026-11-24).** The panel's warm-machine section chooses *Off*,
  *Always Warm* (starts the machine when chosen) or *Smart Warm* (starts it only when you are about to render: the
  Workflow Check passed after Render, or models are being prepared for a render - never on opening the panel), and a
  warm duration (30 min to 24 h, or custom): the machine shuts itself down after that long without a render. The
  countdown pauses while renders or a queue run, is never reset by keep-alive jobs, survives a ComfyUI restart and
  warns about 5 minutes before; *Keep it warm* restarts it. An automatic shutdown (idle or the daily spending limit)
  never stops a running render. The section shows the remaining warm time, the hourly rate, the idle cost of the
  chosen duration, the cost so far and the estimated total, the GPU memory of the last render on it, model transfers
  in progress and the model cache. A settings file with *Keep the cloud machine warm* on means Always Warm with its
  saved hold. Each render still starts ComfyUI again on the warm machine (models load from its disk cache).
- **Model transfers in parallel, never twice.** Downloads and uploads of models run two at a time by default (1-4,
  separately for each direction); the same model is never downloaded or uploaded twice at once. Unfinished downloads
  untouched for 7 days can be removed from the panel; complete model files are never deleted.
- **GPU memory between workflows** of a queue batch: keep models loaded, unload when GPU memory is high (with a lower
  limit so a large workflow is not unloaded and reloaded every time), or always unload. Temporary-file cleanup never
  touches the model folders.

- **The whole error of a failed render is always visible.** The cloud log stream drops and reorders lines, so a
  failure could end with only "The cloud render failed (exit code 1)". The cloud runner now writes the complete error
  to `_halofuse/error.json` (node, class and title, the full ComfyUI error message with the memory figures, the full
  traceback, RAM / GPU memory at the failure, the last 200 lines ComfyUI printed; for a crash also the exit code and
  signal) and every ComfyUI line to `_halofuse/comfyui.log`; both arrive with the outputs, also for failed jobs. The
  console's Problems tab leads with a plain-English headline that names the node (for example "Node 1458 (KSampler)
  ran out of GPU memory"), what happened with the numbers, and concrete next steps (bigger GPU or more system RAM,
  lower resolution or frames, VAE Decode (Tiled), fewer frames per window), plus *Show node on canvas*, *Copy error*
  and the complete technical detail in a scrollable block. Error and warning lines wrap instead of being cut. With
  an older runtime image the error is rebuilt from the runner's complete log file, or from what the live stream
  delivered (the console says how many lines the cloud dropped). The full report needs the next runtime image.
- **Missing models are fetched automatically.** A model that is neither on this computer nor in the cloud model library
  no longer just blocks the render: the Workflow Check looks for a public download, in this order - the workflow's own
  links (official templates carry them), the ComfyUI-Manager model list, then Hugging Face by exact file name (known
  organisations first; when different files share the name, they are listed for you to pick from, never guessed).
  Civitai is not searched; a pasted Civitai link works. The file is downloaded into ComfyUI's model folder (same
  sub-folders as in the workflow; resumes after an interruption; free disk space, size and SHA-256 checked), uploaded
  to the cloud model library, and the render starts. Each model shows its state in the check (in the cloud library /
  uploading / downloading from <source> / no source found), and a missing one offers *Paste a download link*. New
  settings under Advanced: *Download missing models automatically* (on) and *Ask before downloading models larger
  than* (20 GB); models of unknown size or found only in a personal upload also ask first. The C2C *Never download
  models* setting (or `C2C_NO_DOWNLOADS=1`) turns all of this off. Gated Hugging Face models need an optional token,
  kept in this computer's keychain. Models inside an H2Vault vault are left to the vault.
- **Previews by node.** The console's Live tab (and the panel's job card, compactly) shows one section per node of
  the running workflow that shows something: samplers with their latest step frame, PreviewImage, mask previews,
  video, audio and text outputs, each under the node's title with a state badge (Waiting, Running, Done, Cached,
  Failed, Not run). A node served from ComfyUI's cache shows its last result, from this render or an earlier one on
  this computer. Output kinds HaloFuse cannot show are named in plain words. An H2Vault vault shows one card and only
  what its author allows; nothing inside it is listed. Audio, text and exact file-to-node matching need the next
  HaloFuse runtime image; with the current one, files are matched to nodes by their file name prefix (the console says
  so).
- **Outputs saved by name.** After delivery, every output is also saved in
  `output/HaloFuse/by-name/<input name>/<date>/<time>_<job>/<node title>/` (hard links when the disk allows, copies
  otherwise) with `job.log`, `metadata.json`, the workflow (API and UI) and a small `.json` next to each file (node,
  seeds, models, GPU, sha256). The job folder `output/HaloFuse/<job_id>/` is unchanged; older jobs are not moved. The
  folder is a setting (empty switches it off).
- **Two new render options (Advanced, off by default):** *Ignore ComfyUI's cache* (`--cache-none`) and *Preview nodes
  that have no preview* (adds a one-frame preview to the cloud copy of the workflow for image and mask outputs; your
  graph is not changed; at most 8).
- **My custom nodes.** A new panel section takes extra custom node packs for your own jobs, one per line
  (`owner/repo`, `owner/repo@tag`, a GitHub address or a Comfy Registry id). *Check and save* pins each one to an exact
  version and shows its licence; the list is installed at the start of every job on a HaloFuse image whose runner
  supports it (not the Original image; the panel says which applies). A pack that fails to install is left out and
  named in the job log; the job continues. The Workflow Check counts their nodes as available (not verified) and, for a
  node the runtime lacks, offers *Add <pack> to my custom nodes*. Maintainers add a pack to the image itself with
  `python runtime/scripts/add_node.py <repo or registry id>`.
- **The console always has something to show.** Opening it loads the last job's saved log (also after a reload), and
  a new **Activity** tab lists every failure the panel hit with its time: a blocked Workflow Check (one line per
  problem), a refused submit, a model upload error, a sign-in error. A submit that failed after its job was created now
  opens that job's log. Before, the console said "No job attached" after any failed or finished render.
- **The floating window appears.** Choosing "Floating window" opens it straight away and it comes back after a reload
  if it was open (before, only a small bolt button appeared). The console opened from the floating panel now comes to
  the front instead of opening behind it. A window saved off-screen comes back on screen.
- **Workflow Check reads input files the way ComfyUI does.** Files marked [output] or [temp] are sent to the cloud
  (they used to pass the check and then fail there), Windows sub-folder names like `shots\plate.mov` reach the cloud
  under the name the file was sent with, and a missing file on a node that feeds no output is a warning, not a block.
  When a file is in the output folder, the message says so.
- **Models the nodes download themselves are not reported as unknown.** ControlNet-aux annotators (yolox, dw-ll,
  depth_anything) and Depth Anything V2 show as "nothing to do: the node downloads it in the cloud"; an uploaded copy in
  `depthanything/` is used. Every check message now ends with what to do.
- **Workflow Check no longer reports false "required input is not set".** Core's Math Expression node (and any node
  with growing inputs: a, b, c ...) stores its inputs under names like `values.a`; the check now reads them the way
  ComfyUI does. A node that feeds no Save / Preview output is no longer checked either: ComfyUI never runs it (the
  check says how many such nodes it skipped). A node that does feed an output and misses an input still blocks.
- **Node availability is checked against the real cloud image.** The extension now ships the node list of the cloud
  runtime image (31 packs, ComfyUI 0.43.0, captured by the image build), so "No runtime profile snapshot is installed"
  is gone and a node the cloud cannot run is named before anything starts.
- **The sidebar panel scrolls.** It used to stop at the bottom of the window with Account / Queue / Advanced out of
  reach.
- **Models are found wherever they sit in the cloud library.** A model that is not at the expected path is looked up
  by file name and size anywhere in the library. Found in another sub-folder of the same model folder, the render uses
  that copy as it is. Found under a different model folder, it is still offered for upload (the cloud loads a model
  from its own folder only) and the check says where the other copy is.
- **Missing models upload by themselves.** A model the workflow needs that is on this computer but not in the cloud
  library is uploaded into the same folders as on this computer, then the render starts. Models larger than the
  automatic upload limit still ask first. Switch it off under Advanced > "Upload missing models automatically".
- **Choose the cloud runtime under Advanced.** "Cloud runtime": *Original image* (default) or *HaloFuse image*
  (adds the newer node packs; first renders on it are not yet verified). The Workflow Check checks nodes against the
  runtime you chose. The HaloFuse image is built from every node pack of the owner's list that may legally go into a
  cloud image, updated to the newest releases, on ComfyUI 0.46.0; it keeps every pack of the original image and adds
  newer KJNodes (Model Preview Override) and DepthAnythingV2. It is used once its build has been published.

## 0.5.0 — 2026-11-21

- **One name, inside and out.** Every internal name is HaloFuse now: the Python package (`halofuse_bridge`), the
  runner (`runtime/runner/halofuse_run.py`), the job manifest, settings and data files, environment variables
  (`HALOFUSE_HOST`, `HALOFUSE_EMAIL`, `HALOFUSE_PASSWORD`; the old ones still work and log one warning), the front-end
  extension id. Existing settings, credentials and job history move over automatically on first start.
- **Breaking:** the one-release `/spark_fuse/*` route aliases are gone; the panel has used `/halofuse/*` since 0.4.0.
- The cloud-provider SDK and the deployed runtime image are now referenced in one place each
  (`halofuse_bridge/provider/`), so a HaloFuse-built runtime image is a one-file change.
- Every route sits behind the request guard; the extension loads without the cloud SDK installed.
- Published as one repository: the ComfyUI extension at the root, the cloud runtime in `runtime/`.

## 0.4.0 — Unreleased (HaloFuse)

The extension becomes **HaloFuse** and is published as `ComfyUI-HaloFuse`, with the cloud runtime in `runtime/`.
Each entry says whether it is in the code (**landed**), needs the 0.4.0 runtime image (**needs runtime image**: the
runner code has landed but the image has not been built), or is still being built (**in development**). Nothing that
needs a real cloud GPU (stop timing, live previews, billing, GPU memory) is verified yet.

### Changed

- **New product name: HaloFuse, everywhere.** Panel, console, messages, documentation, the job folder and every
  internal name use the HaloFuse name: the Python package is `halofuse_bridge`, the front-end extension id is
  `HaloFuse.Bridge`. *(landed)*
- **Settings migrate by themselves.** The settings folder (`halofuse_bridge` in ComfyUI's system user folder) and the
  settings file (`halofuse_settings.json`) have new names; on the first start the 0.3 folder is moved to the new name
  with everything in it (settings, deployment file, job index, install id), and a settings file under the 0.3 name is
  renamed. The library settings are now `assets_library_path` and `assets_library_space`; the 0.3 keys are read and
  rewritten under the new names. *(landed)*
- **Deployment environment variables:** `HALOFUSE_HOST`, `HALOFUSE_EMAIL`, `HALOFUSE_PASSWORD`. The 0.3 variable
  names are still read, with a warning in the ComfyUI log asking to rename them; the new names win when both are set.
  *(landed)*
- **The 0.3 route aliases are removed.** Only `/halofuse/*` routes are served (the shipped panel never used the old
  ones). A script that called the 0.3 routes must move to `/halofuse/*`. *(landed)*
- **New install folder and registry id: `ComfyUI-HaloFuse`** (publisher `code2collapse`). *(landed)*
- **The cloud service address is deployment configuration.** It is no longer shown, saved from or returned to the
  browser, and a request can no longer change it. It comes from the deployment environment or a private deployment
  file in ComfyUI's system user folder. An install whose saved address equals the 0.3.x default is carried over once;
  any other saved value is not carried over (it may have been planted) and needs provisioning; a fresh install needs
  provisioning. Plain `http://` addresses are refused unless they point at this computer. *(landed)*
- **Password storage.** The password moves into the system keychain (Windows Credential Manager, macOS Keychain,
  Linux Secret Service). Without a keychain, plain-text storage needs an explicit opt-in that the panel labels.
  Existing passwords are migrated automatically and removed from the old settings file only after the keychain copy
  is verified. *(landed)*
- **Deployment credentials win.** When the deployment supplies both email and password, they take precedence over
  panel-saved ones and the account section is read-only. *(landed)*
- **Downgrade note:** after the migration an older version finds no saved password; enter it again there or use
  deployment-managed credentials.
- **New dependency: `keyring`** (pure Python) for the system keychain. *(landed)*

### Added

- **Sidebar panel** with GPU and price estimate, preview mode, render repeats, account, queue, recent jobs and
  advanced settings. *(landed)*
- **Floating console:** move, resize, near-full-screen, minimise; Logs with search, filters, copy all and save;
  Progress, Telemetry, Problems and Summary tabs; reattach after a reload or a ComfyUI restart. *(landed; Progress and
  Telemetry need runtime image)*
- **Job records** in `output/HaloFuse/<job id>/` (record, workflow, complete event log, bridge, render and error
  logs), surviving restarts. *(landed)*
- **Real cancellation:** Stop now and Stop after current, with every step shown and the completion race handled.
  *(landed; cloud-side timing not verified)*
- **Limits and cost:** maximum runtime per job (6 hours by default, status "timed out"), optional per-job and daily
  cost caps, a running cost estimate. *(landed; panel controls for the limits in development)*
- **Queue:** reorder, remove, pause, resume, persisted across restarts. *(landed)*
- **Diagnosis:** a title, likely cause and next step for every failure category. *(landed; most precise with the
  runtime image)*
- **Cloud runner 0.4.0:** structured progress events, telemetry, previews, launch presets from an allowlist, one
  ComfyUI restart after a crash (the rest of a queue chunk still runs), out-of-memory recovery, clean stops that keep
  partial outputs, outputs that keep their subfolders. *(needs runtime image)*
- **Live preview** (experimental). *(needs runtime image; not verified)*
- **Automatic delivery of every output** and the logs into `output/HaloFuse/<job id>/`, size + SHA-256 checked,
  resumable, **Retry** and **Recover outputs**; images are no longer copied loose into the output root.
  *(available; partial outputs after a stop need runtime image)*
- **Workflow Check** (unknown or unavailable nodes, missing models / input files / required inputs, broken links,
  invalid values, GPU-memory facts; no GPU started) and **Quick Draft** (reduced copy on each widget's own grid).
  Input files are planned safely: absolute, `..`, drive, UNC and linked paths are refused before anything is copied.
  *(available)*
- **Node profiles** and the optimised runtime build. *(in development)*
- **Documentation:** user manual, operations runbook, getting-started guide, screenshots. *(landed)*

### Security

- Account and settings requests are accepted only from this computer, and changes must come from the HaloFuse panel.
  *(landed)*
- Logs, error messages and job folders are scrubbed of passwords, tokens and the service address. *(landed)*
- Workflow input values that point outside ComfyUI's `input` folder are blocked before upload. *(in development)*

### Tests

- The extension's suite runs without the cloud connector, ComfyUI, a GPU or the network, from any checkout folder
  name: a shared fake ComfyUI, leak scanners, a security regression suite, and a browser-test harness for an isolated
  test machine. The runtime has its own suite in `runtime/tests`. *(landed)*

## 0.3.4 and earlier (before the HaloFuse name)

Short summaries; the full notes of these releases are kept with the project's private records.

- **0.3.4 — 2026-10-23.** Long renders are no longer cut off at one hour (no fixed wall-clock ceiling is sent);
  the 30-minute inactivity detector remains.
- **0.3.3 — 2026-10-22.** Inactivity detector set to 30 minutes; the requirements file now pins the connector
  version that model sync needs.
- **0.3.2 — 2026-07-27.** An explicit one-hour wall-clock ceiling on every job (removed again in 0.3.4).
- **0.3.1 — 2026-07-27.** Render queue and submission reliability fixes (queue waits no longer fail silently).
- **0.3.0 — 2026-07-25.** Multi-workflow render queue on one warm machine; seeds honour `control_after_generate`.
- **0.2.4 — 2026-07-22.** Clearer render queue panel header and documentation.
- **0.2.3 — 2026-07-18.** The GPU list shows only GPU machine types; UI fixes.
- **0.2.2 — 2026-07-12.** Settings survive an uninstall or reinstall (moved to ComfyUI's user folder).
- **0.2.1 — 2026-07-07.** Version indicator in the panel title; fixes from tester feedback.
- **0.2.0 — 2026-07-02.** Pre-render model check against the cloud model library with consented upload.
- **0.1.2 — 2026-06-30.** Warm-machine affinity for the queue; safer input-file staging.
- **0.1.1 — 2026-06-29.** Registry metadata; Python 3.12 minimum.
- **0.1.0 — 2026-06-21.** First release: panel, single render with live progress, render repeats, render queue,
  sequential output naming.

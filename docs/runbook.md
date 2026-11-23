# HaloFuse — Troubleshooting and Operations Runbook

For artists who need to fix a failed render, and for whoever looks after a HaloFuse installation. Version 0.4.0 (in
development). Labels as in the [manual](manual.md): *available*, *needs the 0.4.0 runtime image*, *in development*.

## 1. First steps for any failure

1. Read the diagnosis in the panel, or in the console's **Problems** tab. It names a category from section 3 and a
   next step.
2. Open the job's folder: `ComfyUI/output/HaloFuse/<job id>/` (the job id is in the console title and its **Summary**
   tab). `job.json` has the status, times and diagnosis; `logs/error.log` has the errors; `logs/render.log` has the
   cloud ComfyUI log.
3. If the render did not start at all, look at the ComfyUI server log (section 2).
4. Before sharing a log, read it. HaloFuse removes passwords, tokens and the service address, but your own workflow
   can contain private paths or prompts.

## 2. Where the logs are

| What | Where | Status |
|---|---|---|
| Job record | `output/HaloFuse/<job id>/job.json` | available; written atomically, survives restarts |
| Workflow as submitted | `output/HaloFuse/<job id>/workflow.json` | available |
| Complete event log | `output/HaloFuse/<job id>/logs/events.jsonl` | available; the console reads it |
| HaloFuse messages | `output/HaloFuse/<job id>/logs/bridge.log` | available |
| Cloud ComfyUI and runner log | `output/HaloFuse/<job id>/logs/render.log` | available; relayed lines, scrubbed |
| Errors only | `output/HaloFuse/<job id>/logs/error.log` | available; created on the first error |
| All outputs of a job | `output/HaloFuse/<job id>/outputs/` | available; size + SHA-256 checked, resumable |
| Older manual recoveries (version 0.3) | their 0.3 output folder | readable, never moved |
| ComfyUI server log | the ComfyUI console window, or ComfyUI's log panel | available |
| Browser side | the browser developer console (F12) | available |
| Settings | the HaloFuse settings file in ComfyUI's `user` folder (a system folder the web UI cannot read) | available |

## 3. Failure categories

The code in backticks is what the diagnosis shows (and what `job.json` stores). Runner exit codes (0–6) appear in
`render.log`. Categories that rely on structured runner events (memory exhaustion, crashes, recovery) are precise only
with the 0.4.0 runtime image; with the previous image HaloFuse diagnoses from the log text.

### 3.1 Before the render starts (no GPU billed)

| Code | Likely cause | What to do |
|---|---|---|
| `service_not_configured` | the cloud service is not configured on this computer | ask your HaloFuse administrator; the panel cannot set it |
| `connector_missing` | HaloFuse's Python dependencies are not installed in ComfyUI's Python | run the install step again with the Python that runs ComfyUI (manual section 3) |
| `credentials_missing` | no account is saved | enter it in the Account section |
| `auth_failed` | the email or password was not accepted | re-enter them; use **Test connection** |
| `network_failure` | no internet, a proxy or firewall, or the service is down | check the connection; HaloFuse keeps watching a running job and retries |
| `rate_limited` | too many requests in a short time | wait a minute and retry |
| `no_capacity` | no warm machine available for a queue | retry later; by default the queue falls back to separate jobs |
| `invalid_model_path` | a model reference points outside the model library or uses an unsupported path | select the model again in its loader node |
| model check "missing everywhere" | the model is neither local nor in the cloud model library | fix the loader selection, or add the file locally so HaloFuse can upload it |
| `preflight_blocked` | Workflow Check found a blocking problem | fix the listed items |

### 3.2 While the render runs

| Code | Runner exit | Likely cause | What to do |
|---|---|---|---|
| `bad_input` | 2 | the workflow is not in API format, or the job input is unreadable | report it with the job id if it came from the panel |
| `bad_launch_args` | 2 | a launch option outside the allowlist | use a preset; remove custom options |
| `startup_failed` | 6 | the cloud ComfyUI did not start | render again; report it with the job id if it repeats |
| `missing_node` | 3 | a custom node is not in the cloud runtime image | remove the node or ask your administrator |
| `missing_model` | 3 | a model name is not in the cloud model library | sync the model (manual section 8) |
| `invalid_value` | 3 | ComfyUI rejected an input value (range, type, choice) | correct the value named in the details |
| `validation_failed` | 3 | ComfyUI refused the workflow before running it | check the node and input in the details |
| `vram_oom` | 1 | the GPU ran out of memory while running a node | larger GPU; lower batch size, frames or resolution; Max Memory preset |
| `vae_decode_oom` | 1 | sampling finished but the VAE decode needed more GPU memory | Max Memory preset (tiled decode retry) or a larger GPU |
| `host_ram_oom` | 1 | a node could not allocate system memory | a GPU type with more system memory; Max Memory preset |
| `host_ram_exhausted` | 6 | the cloud machine ran out of system memory and ComfyUI was stopped | a GPU type with more system memory; Max Memory preset |
| `process_killed` | 6 | ComfyUI was killed without an error message; most likely system memory (not confirmed) | as above, then render again |
| `native_crash` | 6 | a crash in native code (a custom node, a GPU kernel or the driver) | render again with the Safe preset; if it repeats, use the Debug preset and send the log |
| `process_exit` | 6 | the cloud ComfyUI process exited before the render finished | render again; check the last messages before it stopped |
| `execution_error` | 1 | a node raised an error while running | read the node error in the details |
| `runner_error` | 1 | the HaloFuse cloud runner hit an internal error | render again; if it repeats, send the HaloFuse log |
| `timeout` | 4 | the render ran longer than its maximum runtime and was stopped | raise the maximum runtime or shorten the job |
| `no_output` | 5 | the workflow finished but saved nothing | add a save node (Save Image, a video combine node, Save Audio) |
| `cancelled` | 1 | you (or a limit) stopped the render | partial outputs, if any, are delivered to the job folder (keeping them after a stop needs the 0.4.0 runtime image) |

Queue chunks run several workflows in one cloud job; the log marks each with
`[runner] workflow M/T started|succeeded|failed`. A failed workflow never removes earlier results. With the 0.4.0
runtime image, a crash restarts ComfyUI once and the rest of the chunk still runs.

### 3.2a Live preview (experimental)

| What you see | Likely cause | What to do |
|---|---|---|
| *Live preview is off for this render.* | the render was started with another preview mode | choose **Live preview** before rendering |
| *Waiting for the first preview…* for the whole render, then *This render sent no previews.* | the cloud runtime image predates live preview (no preview producer) | ask your administrator for the new runtime image (with live preview) |
| the frame dims, *Waiting for the next preview (the cloud log can drop lines).* | frames are sent at most every 2 s and the cloud log can drop lines | nothing; the next frame replaces it. The full-quality last frame is delivered with the outputs |
| *The cloud dropped N log lines; some previews are missing.* | the cloud log stream dropped lines | nothing; the outputs and the delivered logs are complete |
| *Detailed preview unavailable for this model; showing the fast preview.* | no decoder for this model family in the cloud model library | upload the matching decoder from `models/vae_approx`, or use Fast |
| **Detailed** greyed out in the panel | no decoder file in the local `models/vae_approx` folder | put the decoder files there, or use Fast |
| *No seed in this workflow can be changed safely.* | the workflow has no number-valued seed input that ComfyUI marks as a seed (a seed fed by a link, or a node not installed locally) | change the seed in the graph and render again; nothing was stopped |
| *The old render was stopped, but the new one could not start: …* | the stop went through, the new submission failed | read the reason; render again from the panel |

### 3.3 After the render

| Code | Likely cause | What to do |
|---|---|---|
| `storage_failure` | cloud storage reported an error while reading or writing job files | retry in a few minutes |
| `download_failure` | the render finished but copying results to this computer failed | press **Retry** on the console's Outputs row; it resumes where the download stopped |
| `provider_failure` | the cloud service ended the job without a runner result (machine interrupted, no GPU available) | render again, or pick another GPU type |
| `unknown` | HaloFuse could not determine why the job ended this way | open the full log in the console and check the last messages |

## 4. Recovery procedures

**Browser reloaded or closed during a render.** Open ComfyUI again. The console reattaches to running and recent jobs
and restores the complete log from disk (*available*).

**ComfyUI restarted during a render.** The job record is on disk; on start HaloFuse resumes watching the job
(*available*). If the local records were lost, HaloFuse finds this installation's jobs again by their cloud tags.

**A render looks stuck.** Open the console. If the job is still queued or starting, the cloud is preparing a machine
(a cold start can take minutes). If it is running with no new log lines for a long time, use **Stop now**. The
maximum runtime (6 hours by default) stops it in any case.

**A stop shows "could not confirm".** Treat the job as possibly still billing. Click **Stop now** again. If it still
does not confirm, contact your HaloFuse administrator with the job id from the console's **Summary** tab.

**Outputs are missing.** Look in `output/HaloFuse/<job id>/outputs/` (not the output root). If the console's Outputs
row says *partial* or *failed*, press **Retry**; for a job from before this version, use **Recover outputs** in the
panel with its job id.

**Out of memory.** Change one thing at a time: a GPU type with more GPU memory (`vram_oom`, `vae_decode_oom`) or more
system memory (`host_ram_*`, `process_killed`), then the Max Memory preset, then a lower resolution, frame count or
batch size.

**Saved password missing after switching back to an older version.** 0.4.0 moves the password into the system
keychain and removes it from the old settings file. Enter it again in the older version, or use deployment-managed
credentials.

## 5. Operations notes (for whoever looks after the installation)

- **Configuration.** The cloud service address is deployment configuration, provided by your HaloFuse administrator.
  It is never entered in the browser, never returned by a request, and a request cannot change it. A plain `http://`
  address is refused unless it is on this computer. On managed computers the account can also come from the
  deployment, and then it takes precedence over a panel-saved one.
- **Upgrading from 0.3.x.** On first start, a saved password moves into the keychain (verified before the old copy is
  removed). A saved service address equal to the old default is carried over once; any other saved value is not
  (it could have been planted by a third party) and needs the deployment configuration.
- **Network exposure.** HaloFuse's account and settings requests are accepted only from this computer, and changes
  must come from the HaloFuse panel. Running ComfyUI with `--listen` on a shared network is not recommended; ComfyUI
  itself has no login.
- **Updating.** `git pull` in `custom_nodes/ComfyUI-HaloFuse`, re-run the dependency install, restart ComfyUI.
  Settings and job folders live outside the extension folder.
- **Disk use.** Every job keeps its folder under `output/HaloFuse/`. Delete old job folders when you no longer need
  them; HaloFuse never deletes them for you.
- **Credentials on shared machines.** Prefer the system keychain or deployment-managed credentials. A password stored
  "as plain text on this computer" is protected only by the permissions of ComfyUI's `user` folder.
- **Uninstall.** Use **Clear saved credentials** first (or remove the HaloFuse entry from the system keychain), then
  delete the extension folder.
- **Cloud runtime.** The cloud renders on the runtime image built from `runtime/`. Publishing a new image is an
  administrator task ([runtime/README.md](../runtime/README.md)); the 0.4.0 image has not been built yet.

### 5a. Live preview storage and limits

- Frames are written into `output/HaloFuse/<job id>/previews/live_<n>_<kind>_<node>.<ext>` while the job runs; their
  metadata is in `output/HaloFuse/<job id>/live_previews.jsonl`. Only JPEG, PNG and WebP bytes are accepted (checked
  by their content, never by a name or type the cloud sends), at most 256 KB each, one per second per job (bursts of
  six), at most 300 files per job; 24 frames plus the newest frame per node and every mask/intermediate/result
  picture stay after the job ends. The job log never holds frame bytes; a cloud line that fails validation is stored
  shortened.
- Preview files are served with `X-Content-Type-Options: nosniff` and a sandboxing Content-Security-Policy.
- Reading preview files straight from cloud storage during a run (instead of from the log stream) is built but
  switched off (`live_preview_files` in the settings file, default `false`); it stays off until it has been measured
  that the cloud makes output files visible while a job runs.

## 6. Known limitations

- Features marked *in development* are not in this release's code yet; features that need the 0.4.0 runtime image
  stay inactive until it is published.
- Not verified on real cloud hardware: how fast **Stop now** ends billing, whether outputs written just before a stop
  are kept by the cloud, live previews, actual cost per job, GPU memory and telemetry values.
- Costs are estimates (hourly price times billed time).
- Model uploads are not resumable.
- Residual exposure: HaloFuse's connector runs on your computer. The cloud service address, the installed connector
  package and your account are visible to anyone with administrator access to the computer or its network traffic;
  the keychain protects the password from other users, not from your own account. A hosted HaloFuse gateway that
  removes this is designed but not deployed.
- Recent jobs lists only jobs started from this ComfyUI installation.

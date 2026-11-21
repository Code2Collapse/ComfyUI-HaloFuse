# HaloFuse: Getting Started

This guide takes you from a bare machine to your first HaloFuse cloud render. It assumes no prior knowledge. You stage
your models once, install the extension, and render with a button inside ComfyUI.

You do not need Docker or a GPU: the ComfyUI runtime runs on HaloFuse's cloud GPUs, not on your machine.

> **Version 0.4.0 (in development).** Steps marked *in development* are not in this release's code yet. The complete
> reference is the [user manual](../docs/manual.md); failures are covered by the [runbook](../docs/runbook.md).

## Contents

1. How it works
2. Before you start
3. Stage your model library (once)
4. Install the extension (once)
5. Sign in and configure
6. Your first render
7. Choosing a GPU and seeing the cost
8. If something goes wrong
9. How it works under the hood

## 1. How it works

Heavy ComfyUI workflows run on rented cloud GPUs instead of your own machine. The parts are:

- **The HaloFuse extension** in your local ComfyUI: a sidebar panel and a floating console that send the open
  workflow to the cloud, show its progress and bring the results back.
- **Your cloud model library**: your models, stored once in HaloFuse cloud storage.
- **The runtime image**: the ComfyUI installation that runs on the cloud GPU. You never build or run it.

```
Your machine                                   HaloFuse cloud GPU
  author a workflow in ComfyUI
  Render in the cloud  ──────────────►  workflow + its input files
  panel and console  ◄───────────────  progress and log stream
                                        models read from your cloud model library (cached)
  results in ComfyUI  ◄──────────────  results and logs
```

Your local ComfyUI and the cloud ComfyUI never talk directly. Your machine is the authoring canvas.

## 2. Before you start

- A **HaloFuse account** (email and password) from your HaloFuse administrator.
- A working **local ComfyUI** on Python 3.12 or newer. Note the folder where ComfyUI lives and which Python runs it.
- Windows 10/11, macOS or Linux, and Git.

## 3. Stage your model library (once)

HaloFuse reads your models from your cloud model library, so you upload each one once and reuse it on every render.
Lay the library out to **mirror your local ComfyUI `models` folder**, including any subfolders (for example
`diffusion_models/FLUX2/...`). A workflow refers to a model by its path, so a model in a subfolder locally must be in
the same subfolder in the library, or the cloud will not find it. Windows `\` separators are converted for you.

```
comfy-models/
├── diffusion_models/
├── text_encoders/
├── vae/
├── checkpoints/
├── loras/
└── controlnet/
```

Use only the subfolders your workflows need. Two ways to fill it:

- **Let HaloFuse upload for you.** Before every render the extension checks each referenced model by name and exact
  size, and offers to upload the missing ones (only with your consent). Files above the upload guard (default 50 GB)
  are not uploaded this way.
- **Use your administrator's storage tool** for very large files or a first bulk upload.

## 4. Install the extension (once)

1. Clone it into your ComfyUI `custom_nodes` folder:
   ```powershell
   cd <your ComfyUI folder>\custom_nodes
   git clone https://github.com/Code2Collapse/ComfyUI-HaloFuse
   ```
   This creates `custom_nodes\ComfyUI-HaloFuse` (ComfyUI Manager creates the same folder).
2. Install its dependencies into **the same Python that runs your ComfyUI**:

   **Standard install (a venv or system Python):**
   ```powershell
   python -m pip install -r ComfyUI-HaloFuse\requirements.txt
   ```

   **Portable or desktop ComfyUI (embedded Python):** the portable build ships its own Python in a `python_embeded`
   folder. If pip stops with `Cannot import 'hatchling.build'`, install the build backend first and skip build
   isolation. Open a terminal in the `python_embeded` folder and run:
   ```powershell
   .\python.exe -m pip install hatchling
   .\python.exe -m pip install --no-build-isolation -r "..\ComfyUI\custom_nodes\ComfyUI-HaloFuse\requirements.txt"
   ```
3. Restart ComfyUI. A **HaloFuse** tab appears in the left sidebar.

## 5. Sign in and configure

1. Open the **HaloFuse** tab.
2. Expand **Account**, enter your email and password, click **Save credentials**, then **Test connection**. The
   password goes into your system keychain and is never shown again; leaving the field blank later keeps it. There is
   no address to enter: your administrator configures the cloud service.
3. Expand **Advanced**, set the **cloud model library path** (for example `/comfy-models`; it must start with `/`)
   and click **Save settings**.
4. Pick a **GPU**; its memory and an hourly price estimate are shown.

## 6. Your first render

1. Build or open a workflow that ends in a save node.
2. Click **Render in the cloud**. HaloFuse checks your models (and asks before uploading any), then submits.
3. Click **Open console** to watch the log. Keep ComfyUI running; if you reload the browser or restart ComfyUI, the
   console reattaches and restores the full log.
4. When the render ends, the result appears in the panel and is saved in ComfyUI's output folder. The job's record and
   logs are in `ComfyUI\output\HaloFuse\<job id>\`. (*In development:* every output type, not only images, delivered
   into that folder automatically.)
5. To stop a render, click **Stop now**; the panel shows when the cloud has confirmed the stop.

**Render repeats** renders the workflow several times in one job with fresh seeds: the start-up cost and model load
are paid once. **Queue** runs several different workflows back to back on one warm cloud machine; it can be
reordered, paused and resumed, and is billed for the whole session, so run it back to back.

## 7. Choosing a GPU and seeing the cost

The panel lists the GPU types with their memory and an hourly price estimate. Pick the smallest GPU that fits your
models to keep cost down, or a larger one for the fastest single render. As a guide, a model set of around 17 GB runs
comfortably on a 24 GB card. While a render runs, the panel shows a cost **estimate** (hourly price times billed time,
including the start-up). Each render stops after 6 hours by default.

## 8. If something goes wrong

The panel and the console's **Problems** tab show a diagnosis with a next step. The most common:

- **"HaloFuse is not configured on this computer."** Ask your administrator.
- **Sign-in fails.** Re-enter your email and password and use **Test connection**.
- **A model is not found.** Check that the file sits in the correct subfolder of your library and that the name in
  the workflow matches exactly.
- **A custom node is missing in the cloud.** The runtime image does not contain it; remove it or ask your
  administrator.
- **Out of GPU memory.** Pick a GPU with more memory, or lower the resolution, frames or batch size.
- **The first run is slow.** That is the expected cold start; later runs on a warm machine are faster.

The [runbook](../docs/runbook.md) lists every category, where the logs are, and how to recover.

## 9. How it works under the hood

- **Models load lazily and stay cached.** Your library is mounted read-only in the cloud. Only the bytes a render reads
  are pulled, and they are cached on the cloud machine for the next job. Nothing is re-uploaded from your machine.
- **Image affinity** steers repeat runs onto a machine that already holds the runtime image, so the large image
  download is skipped. The first run on a new runtime image is a cold start.
- **One workflow per job, or one queue chunk per job.** The whole graph runs in the cloud; your machine only authors it
  and collects the results.
- **The runtime image** is built from the `runtime/` folder of the repository ([details](README.md)). The 0.4.0 image,
  which adds the progress timeline, telemetry and crash recovery, has not been built yet.

## Licence

MIT, see [LICENSE](LICENSE) and [NOTICE](NOTICE).

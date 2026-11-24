// HaloFuse compact control panel (rendered inside the ComfyUI sidebar tab): GPU + price, account, preview
// mode, render / stop, job status, Open console, queue, recent jobs, advanced settings.
// The panel element is built once and re-attached when the sidebar tab re-mounts, so closing the tab
// loses nothing. Side-effect-free ES module.

import { bindH, clear } from "./dom.js";
import { AccountSection } from "./account_ui.js";
import { LibrarySection } from "./library_ui.js";
import { UserNodesSection } from "./user_nodes_ui.js";
import { WarmSection } from "./warm_ui.js";
import { PREVIEW_METHODS, PREVIEW_MODES } from "./controller.js";
import { previewPath } from "./api.js";
import { LIVE_TEXT, createImageCache } from "./live_preview.js";
import { cancelActions, cancelInfo, capacityAskText, capacityNotice, costText, deliveryText, isTerminal, progressText, statusInfo } from "./job_view.js";
import { fmtBytes, fmtLocal, fmtUsd } from "./format.js";
import { outputKind } from "./console_panes.js";
import { AUTO_PREVIEW_MODES, renderNodeSections } from "./node_sections.js";

function H_field(H, label, input) {
  return H("label", { className: "hf-field", htmlFor: input.id }, H("span", { text: label }), input);
}

export class Panel {
  /**
   * @param {{doc: Document, win: Window, api: object, controller: import("./controller.js").Controller,
   *   session: import("./session.js").JobSession, openConsole: (tab?: string) => void,
   *   viewUrl: (ref: object) => string, version: string}} o
   */
  constructor(o) {
    Object.assign(this, { doc: o.doc, win: o.win, api: o.api, ctl: o.controller, session: o.session, openConsole: o.openConsole, viewUrl: o.viewUrl });
    const H = bindH(this.doc);
    this.H = H;
    const uid = Math.random().toString(36).slice(2, 8); // the panel can show twice (sidebar + floating): unique ids
    this.version = H("span", { className: "hf-version" });
    this.account = new AccountSection({ doc: this.doc, win: this.win, api: this.api,
      onStatus: (s) => { if (s.credentials_configured && !this.ctl.state.skus.length) this.ctl.loadSkus(); } });
    this.library = new LibrarySection({ doc: this.doc, api: this.api });
    // warm cloud machine (owner 2026-11-23): state, cost, "Shut down the cloud machine"
    this.warm = new WarmSection({ doc: this.doc, win: this.win, api: this.api,
      context: () => ({ instanceType: this.ctl.state.instanceType, rate: this.ctl.state.rate }),
      save: (prefs) => this.ctl.saveAdvanced(prefs) });
    this.userNodes = new UserNodesSection({ doc: this.doc, api: this.api, save: (prefs) => this.ctl.saveAdvanced(prefs) });

    this.skuSel = H("select", { className: "hf-select", id: `hf-sku-${uid}`, onchange: () => this.ctl.setInstanceType(this.skuSel.value) });
    this.rate = H("div", { className: "hf-hint", attrs: { "aria-live": "polite" } });
    this.modeSel = H("select", { className: "hf-select", id: `hf-preview-mode-${uid}`, onchange: () => this.ctl.setPreviewMode(this.modeSel.value) },
      ...PREVIEW_MODES.map(([k, label]) => H("option", { value: k, text: label })));
    this.modeHint = H("div", { className: "hf-hint" });
    // live preview step method (the "model preview override", C-4 6a); shown only for Live preview
    this.methodSel = H("select", { className: "hf-select", id: `hf-preview-method-${uid}`, onchange: () => this.ctl.setPreviewMethod(this.methodSel.value) },
      ...PREVIEW_METHODS.map(([k, label]) => H("option", { value: k, text: label })));
    this.methodHint = H("div", { className: "hf-hint" });
    this.methodBox = H("div", { hidden: true });
    // preview files are bridge URLs; halofuse.js may pass previewUrl, else it is derived from the /view URL base
    const previewUrl = o.previewUrl || ((jobId, name) => {
      const v = this.viewUrl({ filename: "", subfolder: "", type: "output" });
      const i = v.indexOf("/view?");
      return `${i >= 0 ? v.slice(0, i) : ""}${previewPath(jobId, name)}`;
    });
    this._previewUrl = previewUrl;
    this._imgs = createImageCache(this.doc, (name) => previewUrl(this.session.id, name), 8);
    this._imgsFor = null;
    this.repeats = H("input", { className: "hf-input", type: "number", id: `hf-repeats-${uid}`, min: "1", max: "100", step: "1",
      onchange: () => this.ctl.setRenderRepeats(this.repeats.value) });
    this.renderBtn = H("button", { className: "hf-btn hf-primary", type: "button", style: { flex: "1" }, onclick: () => this.ctl.render() });
    this.consoleBtn = H("button", { className: "hf-btn", type: "button", text: "Open console", onclick: () => this.openConsole() });
    this.message = H("div", { className: "hf-status", attrs: { role: "status", "aria-live": "polite" } });
    this.checkBox = H("div");
    this.consentBox = H("div");
    this.jobBox = H("div", { attrs: { "aria-live": "off" } });
    this.queueBox = H("div");
    this.recentBox = H("div");
    this.recoverInput = H("input", { className: "hf-input", type: "text", placeholder: "Job id…", attrs: { "aria-label": "Job id to recover" } });

    const lib = H("input", { className: "hf-input", type: "text", id: `hf-lib-${uid}`, placeholder: "/models" });
    const affinity = H("select", { className: "hf-select", id: `hf-affinity-${uid}` },
      H("option", { value: "required", text: "Required (wait for a cached machine)" }),
      H("option", { value: "preferred", text: "Preferred (start sooner)" }));
    const guard = H("input", { className: "hf-input", type: "number", id: `hf-guard-${uid}`, min: "1", step: "1" });
    const autoUp = H("input", { type: "checkbox", id: `hf-autoup-${uid}` });
    const autoDown = H("input", { type: "checkbox", id: `hf-autodown-${uid}` });
    const askAbove = H("input", { className: "hf-input", type: "number", id: `hf-askabove-${uid}`, min: "0", step: "1" });
    const hfToken = H("input", { className: "hf-input", type: "password", id: `hf-hftoken-${uid}`, autocomplete: "off",
      placeholder: "hf_…", attrs: { spellcheck: "false" } });
    const hfTokenHint = H("div", { className: "hf-hint" });
    // owner 2026-11-23 (node previews): cache, auto-preview of the cloud copy, organised saving
    const noCache = H("input", { type: "checkbox", id: `hf-nocache-${uid}` });
    const autoPrev = H("select", { className: "hf-select", id: `hf-autoprev-${uid}` },
      ...AUTO_PREVIEW_MODES.map(([k, label]) => H("option", { value: k, text: label })));
    const byName = H("input", { className: "hf-input", type: "text", id: `hf-byname-${uid}`, placeholder: "HaloFuse/by-name" });
    const runtime = H("select", { className: "hf-select", id: `hf-runtime-${uid}` },
      H("option", { value: "legacy", text: "Original image (default)" }),
      H("option", { value: "halofuse", text: "HaloFuse image (adds the newer node packs; first renders not yet verified)" }));
    const runtimeHint = H("div", { className: "hf-hint" });
    const libHint = H("div", { className: "hf-hint" });
    lib.addEventListener("input", () => {
      const v = lib.value.trim();
      libHint.textContent = v && !v.startsWith("/") ? "The path must start with “/”." : "";
      libHint.className = `hf-hint${v && !v.startsWith("/") ? " hf-tone-warning" : ""}`;
    });
    this.adv = { lib, affinity, guard, autoUp, autoDown, askAbove, hfTokenHint, runtime, runtimeHint, noCache, autoPrev, byName };
    const saveAdv = H("button", { className: "hf-btn", type: "button", text: "Save settings", onclick: () => {
      const v = lib.value.trim();
      if (v && !v.startsWith("/")) { this.ctl.say("The cloud model library path must start with “/”.", "warning"); return; }
      const g = parseInt(guard.value, 10);
      const folder = byName.value.trim().replace(/\\/g, "/");
      if (folder && (folder.startsWith("/") || /^[A-Za-z]:/.test(folder) || folder.split("/").includes(".."))) {
        this.ctl.say("The by-name folder must be a folder inside ComfyUI's output folder (or empty to switch it off).", "warning");
        return;
      }
      const a = Number(askAbove.value);
      const prefs = { assets_library_path: v, image_affinity: affinity.value, upload_guard_gb: g > 0 ? g : 50,
                      auto_upload_models: autoUp.checked, auto_download_models: autoDown.checked,
                      download_ask_above_gb: Number.isFinite(a) && a >= 0 ? a : 20,
                      ignore_cache: noCache.checked, auto_preview: autoPrev.value, organized_output_dir: folder };
      if (!runtime.disabled) prefs.runtime_image = runtime.value;
      this.ctl.saveAdvanced(prefs);
    } });

    const field = (label, input, hint) => H("label", { className: "hf-field", htmlFor: input.id }, H("span", { text: label }), input, hint || null);
    this.el = H("div", { className: "hf-root", attrs: { "aria-label": "HaloFuse" } },
      H("div", { className: "hf-head" }, H("h2", {}, H("span", { className: "hf-mark", attrs: { "aria-hidden": "true" } }), "HaloFuse"), this.version),
      field("GPU", this.skuSel), this.rate,
      field("Preview", this.modeSel), this.modeHint, this.methodBox,
      field("Render repeats", this.repeats),
      H("div", { className: "hf-row", style: { margin: "4px 0 6px" } }, this.renderBtn, this.consoleBtn),
      this.message, this.checkBox, this.consentBox, this.jobBox,
      this.warm.el,
      this.account.el,
      H("details", { className: "hf-section" }, H("summary", { text: "Queue" }), this.queueBox),
      H("details", { className: "hf-section", ontoggle: (e) => { if (e.target.open) this.ctl.loadRecent(); } },
        H("summary", { text: "Recent jobs" }), this.recentBox,
        H("div", { className: "hf-row", style: { marginTop: "8px" } }, H("div", { className: "hf-grow" }, this.recoverInput),
          H("button", { className: "hf-btn", type: "button", text: "Recover outputs", title: "Download an earlier job's outputs and logs into the HaloFuse output folder",
            onclick: () => this.ctl.recover(this.recoverInput.value) }))),
      H("details", { className: "hf-section", ontoggle: (e) => { if (e.target.open) this.library.ensureLoaded(); } },
        H("summary", { text: "Cloud model library" }), this.library.el),
      H("details", { className: "hf-section" }, H("summary", { text: "My custom nodes" }), this.userNodes.el),
      H("details", { className: "hf-section" }, H("summary", { text: "Advanced" }),
        field("Cloud model library path", lib, libHint), field("Machine placement", affinity),
        field("Largest automatic model upload (GB)", guard),
        field("Cloud runtime", runtime, runtimeHint),
        H("label", { className: "hf-field hf-check", htmlFor: autoUp.id }, autoUp,
          H("span", { text: "Upload missing models automatically (same folders as on this computer)" })),
        H("label", { className: "hf-field hf-check", htmlFor: autoDown.id }, autoDown,
          H("span", { text: "Download missing models automatically (from the workflow’s links, the ComfyUI-Manager model list or Hugging Face), then upload them" })),
        field("Ask before downloading models larger than (GB, 0 = never ask)", askAbove),
        H("label", { className: "hf-field hf-check", htmlFor: noCache.id }, noCache,
          H("span", { text: "Ignore ComfyUI's cache for renders (every node runs again; slower)" })),
        H("div", { className: "hf-hint", text: "A fresh cloud machine has no cache anyway; this matters for render repeats, queues and warm machines." }),
        field("Preview nodes that have no preview", autoPrev,
          H("div", { className: "hf-hint", text: "Adds a one-frame preview to the cloud copy for image and mask outputs no preview node shows (at most 8). Your graph is not changed. H2Vault nodes are never previewed." })),
        field("Save outputs by name in (inside the ComfyUI output folder)", byName,
          H("div", { className: "hf-hint", text: "<input name>/<date>/<time>_<job>/<node title>/ with job.log and metadata.json. The job folder is kept. Empty = off." })),
        saveAdv,
        field("Hugging Face token (only for gated models; kept in this computer’s keychain)", hfToken, hfTokenHint),
        H("div", { className: "hf-row" },
          H("button", { className: "hf-btn", type: "button", text: "Save token", onclick: () => { this.ctl.saveHfToken(hfToken.value); hfToken.value = ""; } }),
          H("button", { className: "hf-btn", type: "button", text: "Remove token", onclick: () => this.ctl.clearHfToken() }))));

    this._unsubs = [this.ctl.subscribe(() => this.update()), this.session.subscribe((w) => { if (w !== "log") this.update(); })];
    this._advLoaded = false;
    this.update();
  }

  mount(container) {
    container.append(this.el);
    this.account.load();
    this.warm.start();
    this.update();
  }

  unmount() { this.el.remove(); }

  update() {
    const st = this.ctl.state;
    const H = this.H;
    this.version.textContent = st.settings?.version ? `v${st.settings.version}` : "";
    if (st.settings) this.userNodes.setSettings(st.settings);
    if (st.settings && !this._advLoaded) {
      this._advLoaded = true;
      this.adv.lib.value = st.settings.assets_library_path || "";
      this.adv.affinity.value = st.settings.image_affinity === "preferred" ? "preferred" : "required";
      this.adv.guard.value = st.settings.upload_guard_gb || 50;
      this.adv.autoUp.checked = st.settings.auto_upload_models !== false;
      this.adv.autoDown.checked = st.settings.auto_download_models !== false;
      this.adv.askAbove.value = st.settings.download_ask_above_gb ?? 20;
      this.adv.noCache.checked = st.settings.ignore_cache === true;
      this.adv.autoPrev.value = AUTO_PREVIEW_MODES.some(([k]) => k === st.settings.auto_preview) ? st.settings.auto_preview : "off";
      this.adv.byName.value = typeof st.settings.organized_output_dir === "string" ? st.settings.organized_output_dir : "HaloFuse/by-name";
      this.adv.runtime.value = st.settings.runtime_image === "halofuse" ? "halofuse" : "legacy";
      this.adv.runtime.disabled = st.settings.runtime_image_locked === true;
      const halo = this.adv.runtime.querySelector('option[value="halofuse"]');
      const ready = st.settings.runtime_halofuse_ready === true;
      if (halo) halo.disabled = !ready && st.settings.runtime_image !== "halofuse";
      this.adv.runtimeHint.textContent = this.adv.runtime.disabled
        ? "Set by this installation; it cannot be changed here."
        : ready ? "The Workflow Check checks nodes against the runtime chosen here."
          : "The HaloFuse image is not published yet; renders use the original image.";
    }
    this.adv.hfTokenHint.textContent = st.settings?.hf_token_set ? "A token is saved." : "No token saved.";
    // GPU list
    const want = st.skus.map((s) => s.instance_type).join("|");
    if (this.skuSel.dataset.list !== want) {
      this.skuSel.dataset.list = want;
      clear(this.skuSel);
      if (!st.skus.length) this.skuSel.append(H("option", { value: "", text: st.skuError ? "GPU list unavailable" : "Loading GPUs…" }));
      for (const s of st.skus) {
        const mem = s.gpu_memory_gb ? ` ${s.gpu_memory_gb} GB` : "";
        this.skuSel.append(H("option", { value: s.instance_type, text: `${s.instance_type}${s.gpu_type ? ` (${s.gpu_type}${mem})` : ""}` }));
      }
    }
    this.skuSel.value = st.instanceType;
    this.rate.className = `hf-hint${st.skuError ? " hf-tone-danger" : ""}`;
    this.rate.textContent = st.skuError ? st.skuError : st.rateLoading ? "Checking the price…"
      : st.rate != null ? `${fmtUsd(st.rate)} per hour (provider estimate)` : st.instanceType ? "Price not available for this GPU." : "";
    this.modeSel.value = st.previewMode;
    this.modeHint.textContent = PREVIEW_MODES.find(([k]) => k === st.previewMode)?.[2] || "";
    this._renderMethod(st);
    if (this.doc.activeElement !== this.repeats) this.repeats.value = String(st.renderRepeats);

    const active = this.ctl.active;
    const check = st.previewMode === "check";
    this.renderBtn.textContent = st.busy ? "Working…" : check ? "Check workflow" : st.previewMode === "draft" ? "Render quick draft" : "Render in the cloud";
    this.renderBtn.disabled = st.busy || (!check && (!st.instanceType || active));
    this.renderBtn.title = active && !check ? "A render is running. Stop it or wait for it to finish." : "";
    this.message.className = `hf-status hf-tone-${st.message?.tone || "neutral"}`;
    this.message.textContent = st.message?.text || "";

    this._renderCheck(st);
    this._renderConsent(st);
    this._renderJob();
    this._renderQueue(st);
    this._renderRecent(st);
  }

  _renderMethod(st) {
    const live = st.previewMode === "live";
    this.methodBox.hidden = !live;
    if (!live) return;
    if (!this.methodBox.firstChild) {
      this.methodBox.append(H_field(this.H, "Step preview", this.methodSel), this.methodHint);
    }
    const detailed = st.previewMethods?.methods?.find((m) => m.id === "taesd");
    const unavailable = !detailed || detailed.available === false;
    const opt = [...this.methodSel.options].find((o) => o.value === "taesd");
    opt.disabled = unavailable;
    opt.title = unavailable ? (detailed?.reason || "") : "";
    opt.textContent = unavailable ? "Detailed (unavailable)" : "Detailed";
    this.methodSel.value = st.previewMethod;
    const desc = PREVIEW_METHODS.find(([k]) => k === st.previewMethod)?.[2] || "";
    const reason = unavailable && st.previewMethod === "taesd"
      ? `${detailed?.reason || "Detailed preview is not available on this computer."} This render will use Fast.` : "";
    const why = unavailable && st.previewMethod !== "taesd" ? "Detailed needs preview decoder files in models/vae_approx." : "";
    this.methodHint.className = `hf-hint${reason ? " hf-tone-warning" : ""}`;
    // the runtime note only when the chosen cloud runtime really lacks live preview (bridge settings payload)
    const lacking = st.settings?.live_preview_supported === false
      ? (typeof st.settings.live_preview_note === "string" && st.settings.live_preview_note) || LIVE_TEXT.experimental : "";
    this.methodHint.textContent = [reason || desc, why, lacking].filter(Boolean).join(" ");
  }

  _renderCheck(st) {
    // rebuilt only when the check result, the draft or the mode changes: a progress tick must not wipe a link being
    // pasted into a "Paste a download link" field
    const key = [st.preflight, st.draft, st.previewMode];
    if (this._checkKey && key.every((v, i) => v === this._checkKey[i])) return;
    this._checkKey = key;
    const H = this.H;
    const box = this.checkBox;
    clear(box);
    const p = st.preflight;
    if (p && (p.blocking?.length || p.warnings?.length || p.notes?.length || st.previewMode === "check")) {
      const list = H("div", { className: "hf-list" });
      for (const b of p.blocking || []) list.append(H("div", { className: "hf-item hf-tone-danger" }, H("span", { className: "hf-grow", title: b.message, text: `${b.node_id ? `Node ${b.node_id}${b.class_type ? ` (${b.class_type})` : ""}${b.input ? `, input "${b.input}"` : ""}: ` : ""}${b.message}` })));
      for (const w of p.warnings || []) list.append(H("div", { className: "hf-item hf-tone-warning" }, H("span", { className: "hf-grow", title: w.message, text: w.message })));
      // information only (e.g. a model the node downloads itself in the cloud): a neutral line, nothing to do
      for (const n of p.notes || []) list.append(H("div", { className: "hf-item hf-tone-neutral" }, H("span", { className: "hf-grow", title: n.message, text: n.message })));
      // owner 2026-11-23: nodes the selected runtime lacks but the other cloud image has: one click switches and re-checks
      for (const r of p.runtime_alternatives || []) {
        list.append(H("div", { className: "hf-item hf-tone-neutral" },
          H("span", { className: "hf-grow", text: `The ${r.label} has ${r.covers.join(", ")}.${r.still_missing?.length ? ` Still missing there: ${r.still_missing.join(", ")}.` : ""}` }),
          H("button", { className: "hf-btn hf-primary", type: "button", text: `Use the ${r.label}`,
            onclick: () => this.ctl.switchRuntimeAndCheck(r.runtime) })));
      }
      // L9.338: the selected GPU type is likely too small: one click switches to one with enough memory and re-checks
      const cap = capacityNotice(p);
      if (cap) {
        list.append(H("div", { className: `hf-item hf-tone-${cap.verdict === "blocked" ? "danger" : "warning"}` },
          H("span", { className: "hf-grow", style: { whiteSpace: "normal" }, text: cap.text }),
          H("button", { className: "hf-btn hf-primary", type: "button", text: cap.buttonText,
            onclick: () => this.ctl.switchGpuAndCheck(cap.instanceType) })));
      }
      // a class the runtime lacks but a Comfy Registry pack provides: one click adds it to My custom nodes
      for (const a of p.add_packs || []) {
        list.append(H("div", { className: "hf-item" },
          H("span", { className: "hf-grow", style: { whiteSpace: "normal" }, text: `${(a.class_types || []).join(", ")} comes from ${a.name}${a.repo ? ` (${a.repo})` : ""}.` }),
          H("button", { className: "hf-btn", type: "button", text: `Add ${a.name} to my custom nodes`,
            onclick: () => this.userNodes.addLine(a.line) })));
      }
      const models = this._modelRows(p.models || {});
      if (models) list.append(models);
      const c = p.checked || {};
      box.append(H("div", { className: "hf-hint", style: { margin: "4px 0" }, text: `Checked ${c.nodes ?? "?"} nodes, ${c.models ?? "?"} models, ${c.inputs ?? "?"} input files${p.profile ? ` against the “${p.profile}” runtime` : ""}.` }), list);
    }
    const d = st.draft;
    if (d) {
      box.append(H("div", { className: "hf-hint", style: { margin: "4px 0" }, text: `Quick draft: ${d.changes.length} value(s) reduced${d.unsupported.length ? `, ${d.unsupported.length} not adjustable` : ""}.` }),
        H("button", { className: "hf-btn", type: "button", text: "Inspect the draft workflow", onclick: () => this._downloadJson("halofuse-quick-draft.json", d) }));
    }
  }

  /** Owner 2026-11-23: the Workflow Check shows each model's state (in the cloud library / uploading / downloading
   *  from <source> / no source found) and, for a missing one, the fix: pick a listed file or paste a download link. */
  _modelRows(m) {
    const H = this.H;
    const rows = [];
    const row = (x, state, tone) => rows.push(H("div", { className: `hf-item hf-tone-${tone}` },
      H("span", { className: "hf-grow", title: `${x.folder || ""}/${x.name}`, text: `${x.folder ? `${x.folder}/` : ""}${x.name}` }),
      H("span", { text: state })));
    const size = (x) => (x.size ? ` (${fmtBytes(x.size)})` : "");
    for (const x of m.present || []) row(x, "in the cloud library", "neutral");
    for (const x of m.uploading || []) row(x, "uploading", "active");
    for (const x of m.uploadable || []) row(x, "on this computer: uploaded first", "neutral");
    for (const x of m.over_limit || []) row(x, "over the upload limit: asks first", "warning");
    for (const x of m.downloading || []) row(x, `downloading from ${x.source?.label || "its source"}`, "active");
    for (const x of m.fetchable || []) row(x, `downloads from ${x.source?.label || "its source"}${size(x)}${x.ask ? ", asks first" : ""}`, x.ask ? "warning" : "neutral");
    const MISSING = { none: "no download source found", ambiguous: "several files with this name: pick one",
      gated: "needs a Hugging Face login", unavailable: "sources could not be searched", blocked: "downloads are turned off" };
    for (const x of m.missing || []) {
      row(x, MISSING[x.source_status] || "not found", "danger");
      (x.candidates || []).forEach((c, i) => rows.push(H("div", { className: "hf-item" },
        H("span", { className: "hf-grow", style: { whiteSpace: "normal" }, text: `${i + 1}. ${c.label}${c.size ? ` (${fmtBytes(c.size)})` : ""}${c.verified ? ", SHA-256 known" : ""}` }),
        H("button", { className: "hf-btn", type: "button", text: "Use this file", onclick: () => this.ctl.fetchOne(x, { choice: i }) }))));
      if (x.source_status === "blocked") continue;
      const input = H("input", { className: "hf-input", type: "url", placeholder: "Paste a download link (Hugging Face, Civitai or direct)…",
        attrs: { "aria-label": `Download link for ${x.name}` } });
      rows.push(H("div", { className: "hf-row" }, H("div", { className: "hf-grow" }, input),
        H("button", { className: "hf-btn", type: "button", text: "Download", onclick: () => this.ctl.fetchOne(x, { url: input.value }) })));
    }
    for (const x of m.auto_download || []) row(x, "the node downloads it in the cloud", "neutral");
    if ((m.fetchable || []).length) {
      rows.push(H("div", { className: "hf-row" }, H("button", { className: "hf-btn", type: "button",
        text: `Download ${m.fetchable.length} missing model(s) now`, onclick: () => this.ctl.downloadFetchable(m.fetchable) })));
    }
    if (!rows.length) return null;
    return H("div", { attrs: { role: "group", "aria-label": "Models" } }, H("div", { className: "hf-hint", style: { margin: "6px 0 2px" }, text: "Models" }), ...rows);
  }

  _renderConsent(st) {
    const H = this.H;
    clear(this.consentBox);
    if (st.fetch) {
      const files = st.fetch.files || [];
      const total = files.reduce((s, f) => s + (f.size || 0), 0);
      const got = files.reduce((s, f) => s + (f.status === "done" ? (f.size || 0) : (f.received || 0)), 0);
      const cur = files.find((f) => f.status === "downloading") || files.find((f) => f.status === "queued");
      this.consentBox.append(H("div", { className: "hf-consent" },
        H("div", { text: `Downloading models: ${fmtBytes(got)} of ${total ? fmtBytes(total) : "unknown size"}` }),
        cur ? H("div", { className: "hf-hint", text: `${cur.name} from ${cur.label}` }) : null,
        H("div", { className: `hf-bar${total ? "" : " hf-indeterminate"}` }, H("div", { style: { width: `${total ? Math.floor((got / total) * 100) : 0}%` } })),
        H("div", {}, H("button", { className: "hf-btn hf-danger", type: "button", text: "Cancel download", onclick: () => this.ctl.cancelFetch() }))));
      return;
    }
    if (st.capacityAsk) {
      const t = capacityAskText(st.capacityAsk);
      const btns = H("div", { className: "hf-row" });
      if (t.switchText) btns.append(H("button", { className: "hf-btn hf-primary", type: "button", text: t.switchText, onclick: () => this.ctl.capacitySwitch() }));
      btns.append(H("button", { className: `hf-btn${t.switchText ? "" : " hf-primary"}`, type: "button", text: t.anywayText, onclick: () => this.ctl.capacityAnyway() }),
        H("button", { className: "hf-btn", type: "button", text: "Cancel", onclick: () => this.ctl.capacityCancel() }));
      this.consentBox.append(H("div", { className: "hf-consent hf-tone-warning", attrs: { role: "group", "aria-label": "Machine memory" } },
        H("strong", { text: t.title }), H("div", { className: "hf-hint", style: { whiteSpace: "normal" }, text: t.detail }), btns));
      return;
    }
    if (st.fetchConsent) {
      const c = st.fetchConsent;
      const total = c.items.reduce((s, m) => s + (m.size || 0), 0);
      const list = H("div", { className: "hf-list" });
      for (const m of c.items) {
        list.append(H("div", { className: `hf-item${m.ask ? " hf-tone-warning" : ""}` },
          H("span", { className: "hf-grow", style: { whiteSpace: "normal" }, title: m.message || "", text: `${m.folder}/${m.name} from ${m.source?.label || "its source"}` }),
          H("span", { text: m.size ? fmtBytes(m.size) : "size unknown" })));
        if (m.ask && m.message) list.append(H("div", { className: "hf-hint", style: { whiteSpace: "normal" }, text: m.message }));
      }
      const btns = H("div", { className: "hf-row" });
      if (c.again) btns.append(H("button", { className: "hf-btn hf-primary", type: "button", text: `Download ${c.items.length}${total ? ` (${fmtBytes(total)})` : ""}, then render`, onclick: () => this.ctl.consentFetch(true) }));
      btns.append(H("button", { className: `hf-btn${c.again ? "" : " hf-primary"}`, type: "button", text: "Download only", onclick: () => this.ctl.consentFetch(false) }),
        H("button", { className: "hf-btn", type: "button", text: "Cancel", onclick: () => this.ctl.fetchCancel() }));
      this.consentBox.append(H("div", { className: "hf-consent", attrs: { role: "group", "aria-label": "Models to download" } },
        H("strong", { text: "These models are missing here and in the cloud library: download them?" }), list, btns));
      return;
    }
    if (st.upload) {
      const files = st.upload.files || [];
      const total = files.reduce((s, f) => s + (f.size || 0), 0);
      const sent = files.reduce((s, f) => s + (f.status === "done" ? (f.size || 0) : (f.sent || 0)), 0);
      this.consentBox.append(H("div", { className: "hf-consent" },
        H("div", { text: `Uploading models: ${fmtBytes(sent)} of ${fmtBytes(total)}` }),
        H("div", { className: "hf-bar" }, H("div", { style: { width: `${total ? Math.floor((sent / total) * 100) : 0}%` } })),
        H("div", {}, H("button", { className: "hf-btn hf-danger", type: "button", text: "Cancel upload", onclick: () => this.ctl.cancelUpload() }))));
      return;
    }
    const c = st.consent;
    if (!c) return;
    const total = c.uploadable.reduce((s, m) => s + (m.size || 0), 0);
    const onlyMismatch = c.uploadable.length > 0 && !c.overLimit.length && c.uploadable.every((m) => m.size_mismatch);
    const list = H("div", { className: "hf-list" });
    for (const m of c.uploadable) {
      list.append(H("div", { className: "hf-item" }, H("span", { className: "hf-grow", title: `${m.folder}/${m.name}`, text: `${m.folder}/${m.name}` }),
        H("span", { text: m.size_mismatch ? `local ${fmtBytes(m.size)} ≠ cloud ${fmtBytes(m.remote_size)}` : fmtBytes(m.size) })));
    }
    for (const m of c.overLimit) {
      list.append(H("div", { className: "hf-item hf-tone-warning" }, H("span", { className: "hf-grow", style: { whiteSpace: "normal" },
        text: `${m.folder}/${m.name} (${fmtBytes(m.size)}) is larger than the ${fmtBytes(m.guard_bytes)} automatic upload limit. Raise the limit under Advanced, or add it to the cloud library another way.` })));
    }
    const btns = H("div", { className: "hf-row" });
    if (c.uploadable.length) {
      btns.append(H("button", { className: "hf-btn hf-primary", type: "button", text: `Upload ${c.uploadable.length} (${fmtBytes(total)}), then render`, onclick: () => this.ctl.consentUpload(true) }));
      if (onlyMismatch) btns.append(H("button", { className: "hf-btn", type: "button", text: "Render with the cloud copy", onclick: () => this.ctl.consentProceed() }));
      btns.append(H("button", { className: "hf-btn", type: "button", text: "Upload only", onclick: () => this.ctl.consentUpload(false) }));
    } else {
      btns.append(H("button", { className: "hf-btn hf-primary", type: "button", text: "Render anyway", onclick: () => this.ctl.consentProceed() }));
    }
    btns.append(H("button", { className: "hf-btn", type: "button", text: "Cancel", onclick: () => this.ctl.consentCancel() }));
    this.consentBox.append(H("div", { className: "hf-consent", attrs: { role: "group", "aria-label": "Models need attention" } },
      H("strong", { text: onlyMismatch ? "These models differ from the cloud library copy" : "These models are not in the cloud library yet" }),
      list, btns));
  }

  _renderJob() {
    const H = this.H;
    const s = this.session;
    clear(this.jobBox);
    if (!s.id) return;
    const job = s.job;
    const isQueue = s.kind === "queue";
    const st = job ? statusInfo(job.status) : { label: "Loading…", tone: "neutral" };
    const ci = job ? cancelInfo(job.cancel) : null;
    const run = s.run.snapshot();
    const prog = run.progress || job?.progress;
    const frac = prog?.fraction;
    const acts = cancelActions(job);
    const bar = H("div", { className: `hf-bar${frac == null && job && !isTerminal(job) ? " hf-indeterminate" : ""}` }, H("div"));
    if (frac != null) bar.firstChild.style.width = `${Math.round(frac * 100)}%`;
    else if (job && isTerminal(job)) bar.firstChild.style.width = "100%";
    const card = H("div", { className: "hf-jobcard", attrs: { role: "group", "aria-label": isQueue ? "Current queue" : "Current render" } },
      H("div", { className: "hf-jobtitle" }, H("span", { text: isQueue ? "Queue" : job?.label || "Render" }),
        H("span", { className: `hf-badge hf-tone-${st.tone}`, text: st.label })),
      bar,
      job && !isTerminal(job) ? H("div", { className: "hf-meta", text: progressText(prog, job.status) }) : null,
      ci ? H("div", { className: `hf-meta hf-tone-${ci.tone}`, text: `${ci.label}${ci.detail ? ` — ${ci.detail}` : ""}` }) : null,
      ci && /confirmed/.test(job.cancel.state) ? H("div", { className: "hf-meta", text: "Billing end is not yet independently confirmed by HaloFuse." }) : null,
      job?.status_note ? H("div", { className: "hf-meta", text: job.status_note }) : null,
      job ? H("div", { className: "hf-meta", text: [costText(job.cost), deliveryText(job.delivery)].filter(Boolean).join(" · ") }) : null,
      job?.error_report?.headline
        ? H("div", { className: "hf-meta hf-tone-danger", text: `${job.error_report.headline}.${job.error_report.next_steps?.length ? ` ${job.error_report.next_steps[0]}` : ""} Full error: console, Problems tab.` })
        : job?.diagnosis ? H("div", { className: "hf-meta hf-tone-danger", text: `${job.diagnosis.title || job.diagnosis.code}: ${job.diagnosis.next_action || job.diagnosis.cause || ""}` }) : null,
      s.error && !job ? H("div", { className: "hf-meta hf-tone-danger", text: s.error.message }) : null);
    if (this._imgsFor !== s.id) { this._imgs.clear(); this._imgsFor = s.id; }
    const lv = !isQueue && job ? s.live.view({ nowS: Date.now() / 1000, job, progress: prog }) : null;
    const latest = run.previews.slice().reverse().find((p) => p.dataUrl); // logs written by an older bridge
    const out = (s.outputs || []).find((o) => outputKind(o.filename || o.rel_path, o.kind) === "image");
    if (lv?.frame && !(isTerminal(job) && out)) {
      const img = this._imgs.get(lv.frame.name, "card");
      img.className = "hf-thumb";
      img.alt = lv.stepText ? `Live preview, ${lv.stepText.toLowerCase()}` : "Latest preview";
      const fig = H("figure", { className: `hf-thumbwrap${lv.stale ? " hf-live-stale" : ""}` }, img);
      if (lv.fraction != null && !isTerminal(job)) {
        const step = H("div", { className: "hf-live-step" }, H("div", { className: "hf-live-step-fill" }),
          H("span", { text: lv.stepText.replace(/^Step (\d+) of (\d+)$/, "$1/$2") }));
        step.firstChild.style.width = `${Math.round(lv.fraction * 100)}%`;
        fig.append(step);
      }
      card.append(fig);
    } else if (this.ctl.state.previewMode !== "none" && (latest || out)) {
      card.append(H("img", { className: "hf-thumb", src: latest && !out ? latest.dataUrl : this.viewUrl(out), alt: latest && !out ? "Latest preview" : "Latest output" }));
    }
    if (!isQueue && job && s.nodes?.sections?.length) {
      card.append(renderNodeSections(H, { board: s.nodes, jobId: s.id, compact: true,
        previewUrl: (jobId, name) => this._previewUrl(jobId, name), viewUrl: this.viewUrl }));
    }
    const btns = H("div", { className: "hf-row" });
    const stopping = s.stopping || job?.cancel?.state === "cancel_requested" || job?.cancel?.state === "cancel_accepted";
    if (acts.now) btns.append(H("button", { className: "hf-btn hf-danger hf-solid", type: "button", text: stopping ? "Stopping…" : "Stop now", disabled: stopping, title: "Cancel the cloud job now; partial outputs are kept", onclick: () => this.ctl.stop("now") }));
    if (acts.now && !isQueue) btns.append(H("button", { className: "hf-btn hf-danger", type: "button", text: "Stop and try a new seed", disabled: stopping, title: "Stop this render and start it again with new seeds (same GPU and settings)", onclick: () => this.ctl.stopAndReseed() }));
    if (acts.afterCurrent) btns.append(H("button", { className: "hf-btn hf-danger", type: "button", text: "Stop after current", title: "Let the current item finish, then stop", onclick: () => this.ctl.stop("after_current") }));
    btns.append(H("button", { className: "hf-btn", type: "button", text: "Console", onclick: () => this.openConsole() }));
    if (job && isTerminal(job)) btns.append(H("button", { className: "hf-btn hf-ghost", type: "button", text: "Dismiss", onclick: () => s.detach() }));
    card.append(btns);
    this.jobBox.append(card);
  }

  _renderQueue(st) {
    const H = this.H;
    const s = this.session;
    clear(this.queueBox);
    const live = s.kind === "queue" ? s.job : null;
    if (live) {
      const items = live.items || [];
      const list = H("div", { className: "hf-list" });
      const pendingIds = items.filter((it) => it.status === "pending" || it.status === "queued").map((it) => it.item_id);
      for (const it of items) {
        const pend = pendingIds.includes(it.item_id);
        const sti = statusInfo(it.status);
        list.append(H("div", { className: "hf-item" },
          H("span", { className: "hf-grow", title: it.error || it.label, text: `${it.index + 1}. ${it.label} · ×${it.render_repeats}${it.outputs_available ? " · outputs ready" : ""}` }),
          H("span", { className: `hf-tone-${sti.tone}`, text: sti.label }),
          pend ? H("button", { className: "hf-btn hf-icon hf-ghost", type: "button", text: "↑", attrs: { "aria-label": `Move ${it.label} up` }, onclick: () => this.ctl.queueOp("move", it.item_id, -1) }) : null,
          pend ? H("button", { className: "hf-btn hf-icon hf-ghost", type: "button", text: "↓", attrs: { "aria-label": `Move ${it.label} down` }, onclick: () => this.ctl.queueOp("move", it.item_id, 1) }) : null,
          pend ? H("button", { className: "hf-btn hf-icon hf-ghost", type: "button", text: "⟳", title: "Replace with the current graph", attrs: { "aria-label": `Replace ${it.label} with the current graph` }, onclick: () => this.ctl.queueOp("replace", it.item_id) }) : null,
          pend ? H("button", { className: "hf-btn hf-icon hf-ghost", type: "button", text: "✕", attrs: { "aria-label": `Remove ${it.label}` }, onclick: () => this.ctl.queueOp("remove", it.item_id) }) : null));
      }
      const paused = live.paused || live.status === "paused";
      this.queueBox.append(list, H("div", { className: "hf-row" },
        !isTerminal(live) ? H("button", { className: "hf-btn", type: "button", text: paused ? "Resume queue" : "Pause queue", onclick: () => this.ctl.queueOp(paused ? "resume" : "pause") }) : null));
      return;
    }
    const q = st.queueDraft;
    const list = H("div", { className: "hf-list" });
    if (!q.length) list.append(H("div", { className: "hf-hint", style: { margin: 0 }, text: "Add the open workflow, switch workflows and add more, then run them back to back on one GPU." }));
    q.forEach((it, i) => list.append(H("div", { className: "hf-item" },
      H("span", { className: "hf-grow", text: `${i + 1}. ${it.label} · ×${it.render_repeats}` }),
      H("button", { className: "hf-btn hf-icon hf-ghost", type: "button", text: "↑", disabled: i === 0, attrs: { "aria-label": `Move ${it.label} up` }, onclick: () => this.ctl.moveDraft(i, -1) }),
      H("button", { className: "hf-btn hf-icon hf-ghost", type: "button", text: "↓", disabled: i === q.length - 1, attrs: { "aria-label": `Move ${it.label} down` }, onclick: () => this.ctl.moveDraft(i, 1) }),
      H("button", { className: "hf-btn hf-icon hf-ghost", type: "button", text: "✕", attrs: { "aria-label": `Remove ${it.label}` }, onclick: () => this.ctl.removeDraft(i) }))));
    this.queueBox.append(list, H("div", { className: "hf-row" },
      H("button", { className: "hf-btn", type: "button", text: "Add open workflow", disabled: st.busy, onclick: () => this.ctl.addToQueue() }),
      H("button", { className: "hf-btn hf-primary", type: "button", text: q.length ? `Run queue (${q.length})` : "Run queue", disabled: !q.length || st.busy || this.ctl.active, onclick: () => this.ctl.runQueue() }),
      q.length ? H("button", { className: "hf-btn hf-ghost", type: "button", text: "Clear", onclick: () => this.ctl.clearDraft() }) : null));
  }

  _renderRecent(st) {
    const H = this.H;
    clear(this.recentBox);
    if (st.recentError) { this.recentBox.append(H("div", { className: "hf-hint hf-tone-danger", text: st.recentError })); return; }
    if (!st.recent.length) { this.recentBox.append(H("div", { className: "hf-hint", style: { margin: 0 }, text: "No HaloFuse jobs on this computer yet." })); return; }
    const list = H("div", { className: "hf-list" });
    for (const j of st.recent.slice(0, 20)) {
      const sti = statusInfo(j.status);
      list.append(H("div", { className: "hf-item" },
        H("span", { className: "hf-grow", title: j.job_id, text: `${fmtLocal(j.created_at)} · ${j.label || "Render"}` }),
        H("span", { className: `hf-tone-${sti.tone}`, text: sti.label }),
        H("button", { className: "hf-btn hf-icon", type: "button", text: "Open", attrs: { "aria-label": `Open ${j.label || "render"} in the console` },
          onclick: () => { this.ctl.openJob(j.job_id); this.openConsole(); } })));
    }
    this.recentBox.append(list);
  }

  _downloadJson(name, data) {
    const blob = new this.win.Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const url = this.win.URL.createObjectURL(blob);
    const a = this.doc.createElement("a");
    a.href = url; a.download = name; a.style.display = "none";
    this.doc.body.append(a); a.click(); a.remove();
    this.win.setTimeout(() => this.win.URL.revokeObjectURL(url), 30000);
  }

  destroy() { for (const u of this._unsubs) u(); this.warm.destroy(); this.el.remove(); }
}

// HaloFuse panel controller: settings, GPU list and price, preview modes, render / stop, queue, recent jobs,
// model consent. No DOM here (panel_ui.js renders this state). ComfyUI access goes through an injected
// adapter, so this module stays testable and side-effect-free.

import { advanceSeeds, seedSummary } from "./seeds.js";
import { isTerminal } from "./job_view.js";

export const PREVIEW_MODES = [
  ["final", "Final result", "Shows the outputs here when the render is done."],
  ["none", "No preview", "Renders without previews in this panel. Outputs are still saved to the output folder."],
  ["check", "Workflow check", "Checks nodes, models, input files and values against the cloud runtime. No GPU is started and nothing is billed."],
  ["draft", "Quick draft", "Renders a reduced copy (smaller size, fewer steps or frames). Your graph is not changed; the draft workflow can be inspected."],
  ["live", "Live preview (experimental)", "Shows the render while it runs (step previews, masks, results) so you can stop it early. Needs the new HaloFuse runtime image; not yet verified end to end. Final outputs are unaffected."],
];
// Step preview method of a live render (the "model preview override", C-4 6a): ComfyUI --preview-method.
export const PREVIEW_METHODS = [
  ["none", "Off", "No step previews; masks, intermediate images and results still arrive."],
  ["latent2rgb", "Fast", "A blurry colour approximation of every step. No extra files needed."],
  ["taesd", "Detailed", "Close to the final image. May slow sampling slightly. Decoder files are uploaded to your cloud model library."],
];
export const DRAFT_OPTIONS = Object.freeze({ max_side: 768, steps: 10, batch_size: 1, frames: 17 });

const uid = () => (globalThis.crypto?.randomUUID ? globalThis.crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}`);

export class Controller {
  /**
   * @param {{api: object, session: import("./session.js").JobSession, comfy: {graphToPrompt: () => Promise<object>,
   *   graphNodes: () => any[], toast: (severity: string, summary: string, detail?: string) => void},
   *   timers?: {setTimeout: Function, clearTimeout: Function}}} o
   */
  constructor({ api, session, comfy, timers }) {
    this.api = api;
    this.session = session;
    this.comfy = comfy;
    this.timers = timers || { setTimeout: (f, ms) => setTimeout(f, ms), clearTimeout: (h) => clearTimeout(h) };
    this.state = {
      settings: null, skus: [], skuError: null, instanceType: "", rate: null, rateLoading: false,
      previewMode: "final", previewMethod: "latent2rgb", previewMethods: null, renderRepeats: 1, busy: false, message: null,
      preflight: null, consent: null, draft: null, upload: null,
      queueDraft: [], recent: [], recentError: null,
    };
    this._subs = new Set();
    session.subscribe((what) => { if (what === "job" || what === "attach" || what === "detach") this._emit(); });
  }

  subscribe(fn) { this._subs.add(fn); return () => this._subs.delete(fn); }
  _emit() { for (const fn of this._subs) fn(this.state); }
  _set(patch) { Object.assign(this.state, patch); this._emit(); }
  say(text, tone = "neutral") { this._set({ message: text ? { text, tone } : null }); }

  // ---------- settings / GPU ----------

  async init() {
    await this.loadSettings();
    await Promise.all([this.loadSkus(), this.reattach(), this.loadRecent(), this.loadPreviewMethods()]);
  }

  async loadSettings() {
    try {
      const s = await this.api.getSettings();
      this._set({ settings: s, instanceType: s.instance_type || this.state.instanceType,
        renderRepeats: Number(s.batch_count) > 0 ? Number(s.batch_count) : 1,
        previewMode: PREVIEW_MODES.some(([k]) => k === s.preview_mode) ? s.preview_mode : this.state.previewMode,
        previewMethod: PREVIEW_METHODS.some(([k]) => k === s.preview_method) ? s.preview_method : this.state.previewMethod });
    } catch (err) { this.say(`Could not load HaloFuse settings: ${err.message}`, "danger"); }
  }

  async loadSkus() {
    try {
      const { skus } = await this.api.listSkus();
      const keep = skus.some((s) => s.instance_type === this.state.instanceType);
      this._set({ skus, skuError: null, instanceType: keep ? this.state.instanceType : (skus[0]?.instance_type || "") });
      await this.loadRate();
    } catch (err) {
      this._set({ skus: [], skuError: err.message });
    }
  }

  async loadRate() {
    const sku = this.state.instanceType;
    if (!sku) { this._set({ rate: null }); return; }
    this._set({ rateLoading: true });
    try {
      const r = await this.api.estimate(sku);
      if (this.state.instanceType === sku) this._set({ rate: r.rate_per_hour_usd, rateLoading: false });
    } catch { this._set({ rate: null, rateLoading: false }); }
  }

  async setInstanceType(sku) {
    this._set({ instanceType: sku });
    this.loadRate();
    try { await this.api.saveSettings({ instance_type: sku }); } catch { /* kept for this session */ }
  }

  async setRenderRepeats(n) {
    const v = Math.min(Math.max(Math.floor(Number(n)) || 1, 1), 100);
    this._set({ renderRepeats: v });
    try { await this.api.saveSettings({ batch_count: v }); } catch { /* kept for this session */ }
  }

  async setPreviewMode(mode) {
    if (!PREVIEW_MODES.some(([k]) => k === mode)) return;
    this._set({ previewMode: mode, preflight: null, draft: null });
    try { await this.api.saveSettings({ preview_mode: mode }); } catch { /* R-WS6-01: in memory until the key exists */ }
  }

  /** Which step preview methods this computer can offer (Detailed needs local decoder files). */
  async loadPreviewMethods() {
    try { this._set({ previewMethods: await this.api.previewMethods() }); } catch { this._set({ previewMethods: null }); }
  }

  async setPreviewMethod(method) {
    if (!PREVIEW_METHODS.some(([k]) => k === method)) return;
    this._set({ previewMethod: method, preflight: null });
    try { await this.api.saveSettings({ preview_method: method }); } catch { /* kept for this session */ }
  }

  /** The method a live render will use; Detailed falls back to Fast when this computer has no decoder files. */
  get effectivePreviewMethod() {
    const m = this.state.previewMethod;
    const info = this.state.previewMethods?.methods?.find((x) => x.id === m);
    return m === "taesd" && info && info.available === false ? "latent2rgb" : m;
  }

  async saveAdvanced(prefs) {
    try {
      const s = await this.api.saveSettings(prefs);
      this._set({ settings: s });
      this.say("Settings saved.", "success");
    } catch (err) { this.say(err.message, "danger"); }
  }

  // ---------- reattach / recent ----------

  async reattach() {
    try {
      const [jobs, queues] = await Promise.all([
        this.api.listJobs({ active: true, limit: 10 }).catch(() => ({ jobs: [] })),
        this.api.listQueues({ active: true }).catch(() => ({ queues: [] })),
      ]);
      const q = (queues.queues || []).find((x) => !isTerminal(x));
      if (q) { this.session.attach(q.queue_id, "queue"); return; }
      const j = (jobs.jobs || []).find((x) => !isTerminal(x) && x.kind !== "chunk");
      if (j) { this.session.attach(j.job_id, "job"); this.say("Reattached to a running render.", "active"); }
    } catch { /* nothing to reattach */ }
  }

  async loadRecent() {
    try {
      const r = await this.api.listJobs({ limit: 20 });
      this._set({ recent: (r.jobs || []).filter((j) => j.kind !== "chunk"), recentError: null });
    } catch (err) { this._set({ recentError: err.message }); }
  }

  /** Opening the console with nothing followed shows the last job from the bridge's persisted record (history, not
   *  only live; owner 2026-11-23: the console showed no data at all). Returns that job's summary, or null. */
  async attachLatest() {
    if (this.session.id) return this.session.job;
    try {
      const r = await this.api.listJobs({ limit: 5 });
      const j = (r.jobs || []).find((x) => x.kind !== "chunk");
      if (j && !this.session.id) this.session.attach(j.job_id, "job");
      return j || null;
    } catch { return null; } // the console's Activity tab reports an unreachable bridge
  }

  openJob(id) { this.session.attach(id, "job"); }

  async recover(id) {
    const clean = String(id || "").trim();
    if (!clean) { this.say("Enter a job id to recover.", "warning"); return; }
    try {
      await this.api.recoverJob(clean);
      this.say("Recovering outputs into the HaloFuse output folder…", "active");
      this.session.attach(clean, "job");
    } catch (err) { this.say(err.message, "danger"); }
  }

  async retryDelivery(id) {
    try { await this.api.retryDelivery(id); this.session.poke(); this.say("Retrying the output download…", "active"); }
    catch (err) { this.say(err.message, "danger"); }
  }

  // ---------- render ----------

  get active() {
    const j = this.session.job;
    return !!(this.session.id && (!j || !isTerminal(j)));
  }

  async _capture() {
    const prompt = await this.comfy.graphToPrompt();
    const summary = seedSummary(advanceSeeds(this.comfy.graphNodes()));
    return { workflow: prompt.output, summary };
  }

  async render() {
    if (this.state.busy) return;
    if (!this.state.instanceType && this.state.previewMode !== "check") { this.say("Pick a GPU first.", "warning"); return; }
    this._set({ busy: true, preflight: null, consent: null, draft: null });
    try {
      const { workflow, summary } = await this._capture();
      if (summary) this.say(summary, "neutral");
      const mode = this.state.previewMode;
      const check = await this._preflight([workflow]);
      if (mode === "check") {
        this.say(check ? (check.ok ? "Workflow check passed. Nothing was started." : "Workflow check found problems. Nothing was started.")
          : "Workflow check is not available on this bridge version.", check?.ok ? "success" : "warning");
        return;
      }
      if (!this._gate(check, () => this._submit(workflow))) return;
      await this._submit(workflow);
    } catch (err) {
      this._failed(err);
    } finally { this._set({ busy: false }); }
  }

  /** A failed render: say it, and when the bridge kept a failed job for it, follow that job so the console shows its
   *  persisted log (the failure is in it). */
  _failed(err) {
    if (err?.jobId) {
      this.session.attach(err.jobId, "job");
      this.loadRecent();
      this.say(`${err.message || String(err)} The console has the details.`, "danger");
    } else this.say(err?.message || String(err), "danger");
  }

  /** Returns the preflight result, or null when the route does not exist yet. */
  async _preflight(workflows) {
    this.say("Checking the workflow…", "active");
    try {
      const body = { workflows, instance_type: this.state.instanceType || undefined };
      if (this.state.previewMode === "live") body.preview_method = this.effectivePreviewMethod;
      const r = await this.api.preflight(body);
      this._set({ preflight: r });
      return r;
    } catch (err) {
      if (err.status === 404) return null;
      throw err;
    }
  }

  /** Block on errors; ask consent for uploads. Returns true when submit may continue now. */
  _gate(check, proceed) {
    if (!check) return true;
    if (check.blocking?.length) { this.say(`${check.blocking.length} problem(s) must be fixed first. Nothing was started.`, "danger"); return false; }
    const m = check.models || {};
    if (m.missing?.length) { this.say(`${m.missing.length} model(s) were not found locally or in the cloud library. Nothing was started.`, "danger"); return false; }
    if (m.uploading?.length) { this.say("A model upload is still running. Render again when it has finished.", "warning"); return false; }
    if (m.uploadable?.length && !m.over_limit?.length && this.state.settings?.auto_upload_models !== false) {
      // Owner 2026-11-23: upload what the cloud library lacks (same folder layout as this computer), then render.
      this._set({ consent: { uploadable: m.uploadable, overLimit: [], proceed } });
      this.say(`Uploading ${m.uploadable.length} model(s) to the cloud library, then rendering…`, "active");
      this.consentUpload(true);
      return false;
    }
    if (m.uploadable?.length || m.over_limit?.length) {
      this._set({ consent: { uploadable: m.uploadable || [], overLimit: m.over_limit || [], proceed } });
      this.say("Some models need your decision before rendering.", "warning");
      return false;
    }
    return true;
  }

  async _submit(workflow) {
    let wf = workflow;
    const mode = this.state.previewMode;
    if (mode === "draft") {
      this.say("Preparing the quick draft…", "active");
      const d = await this.api.draft({ workflow, options: DRAFT_OPTIONS });
      this._set({ draft: { changes: d.changes || [], unsupported: d.unsupported || [], workflow: d.draft_workflow } });
      wf = d.draft_workflow;
    }
    this.say("Submitting…", "active");
    const body = {
      workflow: wf, instance_type: this.state.instanceType, render_repeats: this.state.renderRepeats,
      batch_count: this.state.renderRepeats, preview_mode: mode, label: mode === "draft" ? "Quick draft" : "Render",
      client_ref: uid(),
    };
    if (mode === "live") body.preview_method = this.effectivePreviewMethod;
    const res = await this.api.submitJob(body);
    const job = res.job || { job_id: res.jobId };
    this.session.attach(job.job_id, "job");
    this.say("Submitted. Follow it here or in the console.", "active");
    this.loadRecent();
  }

  // ---------- model consent / upload ----------

  async consentUpload(thenRender) {
    const c = this.state.consent;
    if (!c) return;
    this._set({ consent: null, busy: true });
    try {
      const files = c.uploadable.map((m) => ({ folder: m.folder, name: m.name }));
      const { upload_id: id, uploadId } = await this.api.startModelUpload(files);
      this._watchUpload(id || uploadId, thenRender ? c.proceed : null);
    } catch (err) { this.say(err.message, "danger"); this._set({ busy: false }); }
  }

  async consentProceed() {
    const c = this.state.consent;
    if (!c) return;
    this._set({ consent: null, busy: true });
    try { await c.proceed(); } catch (err) { this._failed(err); } finally { this._set({ busy: false }); }
  }

  consentCancel() {
    this._set({ consent: null });
    this.say("Cancelled. Nothing was uploaded or started.", "neutral");
  }

  _watchUpload(id, then) {
    const tick = async () => {
      let st;
      try { st = await this.api.getModelUpload(id); } catch { this.timers.setTimeout(tick, 4000); return; }
      this._set({ upload: st });
      if (st.status === "done") {
        this._set({ upload: null, busy: !!then });
        if (then) { try { await then(); } catch (err) { this._failed(err); } finally { this._set({ busy: false }); } }
        else this.say("Model upload complete.", "success");
      } else if (st.status === "failed" || st.status === "cancelled") {
        this._set({ upload: null, busy: false });
        this.say(st.status === "failed" ? `Model upload failed: ${st.error || "see the console"}` : "Model upload cancelled.", "danger");
      } else this.timers.setTimeout(tick, 2000);
    };
    this.say("Uploading models to the cloud library…", "active");
    tick();
  }

  async cancelUpload() {
    const id = this.state.upload?.upload_id;
    if (id) { try { await this.api.cancelModelUpload(id); } catch { /* reported by the poll */ } }
  }

  // ---------- stop ----------

  async stop(mode) {
    const s = this.session;
    if (!s.id) return;
    try {
      const res = s.kind === "queue" ? await this.api.cancelQueue(s.id, mode) : await this.api.cancelJob(s.id, mode);
      if (res?.job || res?.queue) { s.job = res.job || res.queue; s._emit("job"); }
      this.say(mode === "now" ? "Stop requested. Waiting for the cloud to confirm." : "Will stop after the current item.", "warning");
      s.poke();
    } catch (err) { this.say(err.message, "danger"); }
  }

  /** "Looks wrong": stop the running render and start it again with new seeds, same GPU and preview settings. */
  async stopAndReseed() {
    const s = this.session;
    if (!s.id || s.kind !== "job" || s.stopping) return;
    this.say("Stopping and preparing new seeds…", "warning");
    try {
      const res = await s.stopAndReseed();
      const changes = res?.changes || [];
      const shown = changes.slice(0, 3).map((c) => `Node ${c.node_id}${c.class_type ? ` (${c.class_type})` : ""} ${c.input} ${c.from} → ${c.to}`);
      const more = changes.length > 3 ? ` and ${changes.length - 3} more` : "";
      this.say(`${res?.stopped ? "Stopped the old render. " : ""}New render started with new seeds: ${shown.join(", ")}${more}`, "active");
      this.loadRecent();
    } catch (err) {
      this.say(err.message || String(err), err.code === "reseed_not_possible" ? "warning" : "danger");
    }
  }

  // ---------- queue ----------

  async addToQueue() {
    try {
      const { workflow, summary } = await this._capture();
      const n = this.state.queueDraft.length + 1;
      this._set({ queueDraft: [...this.state.queueDraft, { key: uid(), workflow, render_repeats: this.state.renderRepeats, label: `Item ${n}` }] });
      this.say(`Added to the queue (${n} item${n > 1 ? "s" : ""}).${summary ? ` ${summary}` : ""}`, "success");
    } catch (err) { this.say(`Could not add to the queue: ${err.message}`, "danger"); }
  }

  moveDraft(i, delta) {
    const q = this.state.queueDraft.slice();
    const j = i + delta;
    if (j < 0 || j >= q.length) return;
    [q[i], q[j]] = [q[j], q[i]];
    this._set({ queueDraft: q });
  }

  removeDraft(i) { this._set({ queueDraft: this.state.queueDraft.filter((_, k) => k !== i) }); }
  clearDraft() { this._set({ queueDraft: [] }); }

  async runQueue() {
    const items = this.state.queueDraft;
    if (!items.length || this.state.busy) return;
    if (!this.state.instanceType) { this.say("Pick a GPU first.", "warning"); return; }
    this._set({ busy: true });
    try {
      const check = await this._preflight(items.map((it) => it.workflow));
      const go = async () => {
        this.say(`Starting a queue of ${items.length} item(s)…`, "active");
        const res = await this.api.createQueue({ instance_type: this.state.instanceType, client_ref: uid(),
          items: items.map((it) => ({ workflow: it.workflow, render_repeats: it.render_repeats, batch_count: it.render_repeats, label: it.label })) });
        this._set({ queueDraft: [] });
        this.session.attach(res.queue?.queue_id || res.queueId, "queue");
      };
      if (!this._gate(check, go)) return;
      await go();
    } catch (err) { this.say(err.message, "danger"); } finally { this._set({ busy: false }); }
  }

  async queueOp(op, ...args) {
    const s = this.session;
    if (s.kind !== "queue" || !s.id) return;
    try {
      let res;
      if (op === "pause") res = await this.api.pauseQueue(s.id);
      else if (op === "resume") res = await this.api.resumeQueue(s.id);
      else if (op === "remove") res = await this.api.deleteQueueItem(s.id, args[0]);
      else if (op === "rename") res = await this.api.patchQueueItem(s.id, args[0], { label: args[1] });
      else if (op === "replace") {
        const { workflow } = await this._capture();
        res = await this.api.patchQueueItem(s.id, args[0], { workflow });
        this.say("Replaced the item with the current graph.", "success");
      } else if (op === "move") {
        const pending = (s.job?.items || []).filter((it) => it.status === "pending" || it.status === "queued").map((it) => it.item_id);
        const i = pending.indexOf(args[0]);
        const j = i + args[1];
        if (i < 0 || j < 0 || j >= pending.length) return;
        [pending[i], pending[j]] = [pending[j], pending[i]];
        res = await this.api.reorderQueue(s.id, pending);
      }
      if (res?.queue) { s.job = res.queue; s._emit("job"); }
      s.poke();
    } catch (err) { this.say(err.message, "danger"); }
  }
}

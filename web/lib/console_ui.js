// HaloFuse floating console: window chrome + tabs (Logs, Progress, Telemetry, Problems, Summary) bound to a
// JobSession. Lives on document.body, independent of the compact panel. Side-effect-free ES module.

import { bindH, copyText } from "./dom.js";
import { FloatingWindow } from "./console_window.js";
import { LogView } from "./console_logs.js";
import { problemsSignature, renderProblems, renderProgress, renderSummary, renderTelemetry } from "./console_panes.js";
import { cancelActions, cancelInfo, isTerminal, progressText, statusInfo } from "./job_view.js";
import { createImageCache, renderLive } from "./live_preview.js";
import { formatEntry } from "./log_model.js";
import { renderNodeSections } from "./node_sections.js";

const TABS = [
  ["live", "Live"], ["logs", "Logs"], ["activity", "Activity"], ["progress", "Progress"], ["telemetry", "Telemetry"], ["problems", "Problems"],
  ["summary", "Summary"],
];
const ANNOUNCE_MIN_MS = 5000; // the step text is read out at most every 5 s
const PANE_MIN_INTERVAL_MS = 500;
const ACTIVITY_POLL_MS = 2000;
// a bridge failure this much newer than the followed job's last change is news the console opens on
const NEWER_THAN_JOB_S = 2;

export class HaloFuseConsole {
  /**
   * @param {{doc: Document, win: Window, storage: Storage|null, session: import("./session.js").JobSession,
   *   viewUrl: (ref: object) => string, previewUrl: (jobId: string, name: string) => string,
   *   onFocusNode: (nodeId: string) => void, onRetryDelivery: (jobId: string) => void,
   *   activity?: import("./activity.js").ActivityFeed, onOpen?: () => Promise<object|null>}} o
   *   activity: bridge failures outside a job log (Activity tab). onOpen: attach the last job when nothing is
   *   followed (history), resolving to that job's summary.
   */
  constructor(o) {
    this.doc = o.doc;
    this.win = o.win;
    this.session = o.session;
    this.o = o;
    const H = bindH(this.doc);
    this.H = H;
    this.window = new FloatingWindow({ doc: this.doc, win: this.win, storage: o.storage, title: "HaloFuse console" });
    this.statusBadge = H("span", { className: "hf-badge hf-tone-neutral", text: "No job" });
    this.window.statusSlot.append(this.statusBadge);

    this.tabButtons = {};
    this.panels = {};
    const tablist = H("div", { className: "hf-tabs", attrs: { role: "tablist", "aria-label": "Console sections" } });
    for (const [id, label] of TABS) {
      const count = H("span", { className: "hf-count" });
      const btn = H("button", { className: "hf-tab", type: "button", id: `hf-tab-${id}`,
        attrs: { role: "tab", "aria-controls": `hf-panel-${id}`, "aria-selected": "false" }, onclick: () => this.selectTab(id) }, label, count);
      btn.addEventListener("keydown", (ev) => this._tabKey(ev, id));
      btn._count = count;
      this.tabButtons[id] = btn;
      tablist.append(btn);
      this.panels[id] = H("div", { className: "hf-tabpanel", id: `hf-panel-${id}`, hidden: true,
        attrs: { role: "tabpanel", "aria-labelledby": `hf-tab-${id}` } });
    }
    this.logView = new LogView({
      doc: this.doc, win: this.win, store: this.session.store,
      prefs: () => this.window.prefs(), setPrefs: (p) => this.window.setPrefs(p),
      getFullText: () => this._fullText(),
      fileName: () => `halofuse-${this.session.id || "log"}.log`,
      onTimestamps: (on) => this.window.el.classList.toggle("hf-no-ts", !on),
      hasJob: () => !!this.session.id,
      emptyText: () => (this.session.id ? null
        : "No render has been followed yet. Failures before a job exists (Workflow Check, submit, model upload, "
          + "sign-in) are listed under Activity."),
    });
    this.panels.logs.append(this.logView.el);
    this.activity = o.activity || null;
    if (this.activity) {
      const feed = this.activity;
      this.activityView = new LogView({
        doc: this.doc, win: this.win, store: feed.store, label: "HaloFuse activity",
        prefs: () => this.window.prefs(), setPrefs: (p) => this.window.setPrefs(p),
        getFullText: async () => {
          const lines = [];
          for (let s = 0; s < feed.store.size; s++) lines.push(formatEntry(feed.store.get(s)));
          return lines.length ? `${lines.join("\n")}\n` : "";
        },
        fileName: () => "halofuse-activity.log",
        onTimestamps: (on) => this.window.el.classList.toggle("hf-no-ts", !on),
        hasJob: () => true,
        emptyText: () => feed.emptyText(),
      });
      this.panels.activity.append(this.activityView.el);
      this._unfeed = feed.subscribe(() => { this.activityView.refreshStatus(); this._refreshChrome(); });
    } else {
      this.tabButtons.activity.hidden = true;
    }
    // Live view state (PREVIEW_DESIGN 4.1); images are reused across renders so a frame never re-downloads
    // a file that fails to load re-renders the pane once, which then shows a placeholder with the reason
    this._imgs = createImageCache(this.doc, (name) => o.previewUrl(this.session.id, name), 160, () => this._renderPane(true));
    this._live = { selected: null, mode: "overlay", message: null, autoPending: false, userPicked: false };
    // Problems tab: the technical-detail toggle survives the pane's re-renders (L9.338)
    this._problemsUi = { techOpen: false, onChange: () => this._renderPane(true) };
    this._announce = H("p", { className: "hf-sr-only", attrs: { "aria-live": "polite" } });
    this._announcedAt = 0;
    this.window.el.classList.toggle("hf-no-ts", this.window.prefs().timestamps === false);
    this.window.body.append(tablist, ...Object.values(this.panels));

    this._paneTimer = 0;
    this._lastPane = 0;
    this._unsub = this.session.subscribe((what) => this._onSession(what));
    this._unwin = this.window.onChange(() => this._onWindow());
    this.selectTab(this.window.prefs().tab || "logs", false);
    this._refreshChrome();
  }

  get isOpen() { return this.window.isOpen; }

  /** Show the console. It always has data to show: the followed job, else the last job from the bridge's record,
   *  and the bridge activity (failures outside a job). With no job, or a bridge failure newer than the job, it opens
   *  on Activity unless a tab was asked for or picked meanwhile. Resolves when the history is loaded. */
  async open(tab) {
    const opened = Date.now();
    this.window.open();
    if (tab) this.selectTab(tab);
    this._renderPane(true);
    const [att] = await Promise.allSettled([this.o.onOpen ? this.o.onOpen() : null, this.activity?.refresh()]);
    if (tab || this._pickedAt >= opened || !this.window.isOpen || !this.activity) return;
    const job = this.session.job || (att.status === "fulfilled" ? att.value : null);
    const failedAt = this.activity.lastErrorTs;
    const jobAt = Number(job?.updated_at ?? job?.created_at) || 0;
    if (!this.session.id ? this.activity.store.size > 0 : failedAt != null && failedAt > jobAt + NEWER_THAN_JOB_S) {
      this.selectTab("activity", false, false);
    }
  }

  close() { this.window.close(); }
  toggle() { if (this.window.isOpen) this.window.close(); else this.open(); }

  selectTab(id, focus = true, byUser = focus) {
    if (!this.panels[id]) id = "logs";
    if (byUser) { this._live.userPicked = true; this._pickedAt = Date.now(); }
    this.tab = id;
    for (const [tid, btn] of Object.entries(this.tabButtons)) {
      const on = tid === id;
      btn.setAttribute("aria-selected", String(on));
      btn.tabIndex = on ? 0 : -1;
      this.panels[tid].hidden = !on;
    }
    if (focus) this.tabButtons[id].focus({ preventScroll: true });
    this.window.setPrefs({ tab: id });
    this._renderPane(true);
  }

  /** Jump to a log line (from Problems / diagnosis). */
  jumpTo(seq) {
    this.selectTab("logs", false);
    this.logView.jumpToSeq(seq);
  }

  destroy() {
    this._unsub?.();
    this._unwin?.();
    this._unfeed?.();
    this.activity?.stop();
    this.activityView?.destroy();
    this.logView.destroy();
    this.window.destroy();
  }

  _tabKey(ev, id) {
    const ids = TABS.map(([t]) => t);
    let i = ids.indexOf(id);
    if (ev.key === "ArrowRight") i = (i + 1) % ids.length;
    else if (ev.key === "ArrowLeft") i = (i - 1 + ids.length) % ids.length;
    else if (ev.key === "Home") i = 0;
    else if (ev.key === "End") i = ids.length - 1;
    else return;
    ev.preventDefault();
    this.selectTab(ids[i]);
  }

  /** One <img> per node item URL, reused across renders (no re-fetch, no flicker). A URL that fails to load is
   *  remembered and the pane re-renders once to show the reason instead of a broken image. */
  _nodeImg(item, url) {
    this._nodeImgs = this._nodeImgs || new Map();
    this._nodeBroken = this._nodeBroken || new Set();
    let img = this._nodeImgs.get(url);
    if (!img) {
      img = this.doc.createElement("img");
      img.decoding = "async";
      img.loading = "lazy";
      img.addEventListener("error", () => {
        if (this._nodeBroken.has(url)) return;
        this._nodeBroken.add(url);
        this._renderPane(true);
      });
      img.src = url;
      this._nodeImgs.set(url, img);
      while (this._nodeImgs.size > 200) this._nodeImgs.delete(this._nodeImgs.keys().next().value);
    }
    return img;
  }

  _liveCtx(run) {
    const s = this.session;
    const job = s.job;
    const view = s.live.view({ nowS: Date.now() / 1000, job, progress: run.progress || job?.progress,
      marks: run.marks, droppedLines: run.droppedLines, nodes: run.nodes });
    return {
      view, model: s.live, job, isQueue: s.kind === "queue", stopping: s.stopping,
      imgFor: (name, role) => this._imgs.get(name, role),
      isBroken: (name) => this._imgs.isBroken(name),
      videoFor: (name, role) => this._imgs.video(name, role),
      urlFor: (name) => this._imgs.url(name),
      message: this._live.message,
      selectedMask: this._live.selected, overlayMode: this._live.mode,
      onStop: () => this._liveAction(() => s.stop("now"), "Stop requested. Waiting for the cloud to confirm."),
      onReseed: () => this._liveAction(async () => {
        const res = await s.stopAndReseed();
        const n = res?.changes?.length || 0;
        return `${res?.stopped ? "Stopped the old render. " : ""}New render started with ${n} new seed${n === 1 ? "" : "s"}.`;
      }),
      onHold: (seq) => { s.live.hold(seq); this._renderPane(true); },
      onSelectMask: (seq) => { this._live.selected = seq; this._renderPane(true); },
      onOverlayMode: (mode) => { this._live.mode = mode; this._renderPane(true); },
    };
  }

  /** The minimized pill's Stop: the same real cancel as the console's Stop, after a confirmation. */
  async _pillStop() {
    const s = this.session;
    if (!s.id || this._pillStopping === s.id) return;
    const ok = this.win.confirm ? this.win.confirm("Stop this cloud render now? Work that is not finished is lost.") : true;
    if (!ok) return;
    this._pillStopping = s.id;
    this._refreshChrome();
    try {
      await s.stop("now");
    } catch (err) {
      this.win.alert?.(`Could not stop the render: ${err?.message || err}`);
    } finally {
      this._pillStopping = null;
      this._refreshChrome();
    }
  }

  async _liveAction(fn, okText) {
    this._live.message = null;
    try {
      const out = await fn();
      this._live.message = typeof out === "string" ? out : okText;
    } catch (err) {
      this._live.message = err?.message || String(err);
    }
    this._renderPane(true);
  }

  _announceStep(view) {
    const now = Date.now();
    if (now - this._announcedAt < ANNOUNCE_MIN_MS) return;
    const text = [view.stepText, view.nodeText].filter(Boolean).join(", ");
    if (text && text !== this._announce.textContent) {
      this._announce.textContent = text;
      this._announcedAt = now;
    }
  }

  _onWindow() {
    this.session.setWatching(this.window.isOpen);
    // the activity record is followed only while the console is open (open() loads its history first)
    if (this.window.isOpen) this.activity?.start(ACTIVITY_POLL_MS); else this.activity?.stop();
    if (this.window.isOpen) this._renderPane(true);
  }

  _onSession(what) {
    if (what === "attach") {
      this.logView.reset();
      this._imgs.clear();
      this._nodeImgs?.clear();
      this._nodeBroken?.clear();
      this._live = { selected: null, mode: this._live.mode, message: null, autoPending: true, userPicked: false };
      this._problemsUi.techOpen = false;
    }
    if (this._live.autoPending && this.session.job) {
      // the Live view comes first while a render runs, unless the user picked another tab for this job
      this._live.autoPending = false;
      if (!isTerminal(this.session.job) && !this._live.userPicked && this.session.kind === "job") this.selectTab("live", false, false);
    }
    this._refreshChrome();
    this._renderPane(what === "attach");
  }

  _refreshChrome() {
    const s = this.session;
    const job = s.job;
    if (!s.id) {
      this.statusBadge.className = "hf-badge hf-tone-neutral";
      this.statusBadge.textContent = "No job";
      this.window.setJobLabel("");
      this.window.setPill("HaloFuse console", "neutral");
      return;
    }
    const st = job ? statusInfo(job.status) : { label: "Loading…", tone: "neutral" };
    const ci = job ? cancelInfo(job.cancel) : null;
    this.statusBadge.className = `hf-badge hf-tone-${ci ? ci.tone : st.tone}`;
    this.statusBadge.textContent = ci ? ci.label : st.label;
    const label = job?.label ? `${job.label} · ${s.id}` : s.id;
    this.window.setJobLabel(label);
    const ended = !!job && isTerminal(job);
    // owner 2026-11-25: the pill kept saying "Running" after the job had ended - the last progress phase of the
    // render outlived it. A finished job shows its real status; only a live job shows progress.
    const prog = job && !ended ? progressText(this.session.run.snapshot().progress || job.progress, job.status) : "";
    this.window.setPill(`HaloFuse · ${prog || (ci && !ended ? ci.label : st.label)}`, ended ? st.tone : (ci ? ci.tone : st.tone));
    const stoppable = !!job && !ended && s.kind === "job" && cancelActions(job).now;
    this.window.setPillAction(stoppable ? () => this._pillStop() : null, this._pillStopping === s.id);
    const errors = this.session.store.counts().error;
    this.tabButtons.problems._count.textContent = errors ? String(errors) : "";
    this.tabButtons.problems._count.className = `hf-count${errors ? " hf-tone-danger" : ""}`;
  }

  /** Non-log panes re-render at most twice a second, and only when visible. */
  _renderPane(force = false) {
    if (!this.window.isOpen || this.tab === "logs" || this.tab === "activity") return;
    const now = Date.now();
    if (!force && now - this._lastPane < PANE_MIN_INTERVAL_MS) {
      if (!this._paneTimer) {
        this._paneTimer = this.win.setTimeout(() => { this._paneTimer = 0; this._renderPane(true); },
          PANE_MIN_INTERVAL_MS - (now - this._lastPane));
      }
      return;
    }
    this._lastPane = now;
    const H = this.H;
    const s = this.session;
    const run = s.run.snapshot();
    const ctx = { job: s.job, run, now: now / 1000 };
    if (this.tab === "problems") {
      const sig = problemsSignature(s.job, run, s.warnings, this._problemsUi);
      if (sig === this._problemsSig && this.panels.problems.firstChild) return; // nothing new: keep selection + focus
      this._problemsSig = sig;
    }
    let el;
    if (this.tab === "progress") el = renderProgress(H, { ...ctx, onFocusNode: (id) => this.o.onFocusNode(id) });
    else if (this.tab === "telemetry") el = renderTelemetry(H, ctx);
    else if (this.tab === "problems") {
      el = renderProblems(H, { ...ctx, warnings: s.warnings, onJump: (seq) => this.jumpTo(seq),
        onFocusNode: (id) => this.o.onFocusNode(id), ui: this._problemsUi,
        onCopy: async (text) => {
          const ok = await copyText(this.win, text);
          this._announce.textContent = ok ? "Error copied to the clipboard." : "Copy failed: select the technical detail and copy it.";
          return ok;
        } });
    }
    else if (this.tab === "live") {
      const live = this._liveCtx(run);
      // owner 2026-11-25: ONE scroll box for the Live tab, kept across renders - only its content is replaced, so the
      // browser keeps the scroll position while frames arrive (a new box per render lost it during a running render)
      if (!this._liveScroll) this._liveScroll = H("div", { className: "hf-scroll" });
      el = this._liveScroll;
      const keep = el.scrollTop;
      el.replaceChildren(renderLive(H, live), renderNodeSections(H, {
        board: s.nodes, jobId: s.id, previewUrl: (jobId, name) => this.o.previewUrl(jobId, name),
        viewUrl: this.o.viewUrl, onFocusNode: (id) => this.o.onFocusNode(id),
        imgFor: (item, url) => this._nodeImg(item, url), isBroken: (url) => !!this._nodeBroken?.has(url) }));
      el.scrollTop = keep;
      this._announceStep(live.view);
    }
    else {
      el = renderSummary(H, {
        ...ctx, outputs: s.outputs, viewUrl: this.o.viewUrl,
        previewUrl: (name) => this.o.previewUrl(s.id, name),
        onRetryDelivery: () => this.o.onRetryDelivery(s.id),
        onCopy: (text) => copyText(this.win, text),
      });
    }
    const panel = this.panels[this.tab];
    const prevScroll = panel.firstChild?.scrollTop || 0;
    const prevTech = panel.querySelector?.(".hf-tech")?.scrollTop || 0;
    // keep keyboard focus on the same control across a re-render (buttons carry data-hf-key)
    const active = this.doc.activeElement;
    const key = active && panel.contains(active) ? active.getAttribute?.("data-hf-key") : null;
    if (this.tab === "live" && el.parentNode === panel) {
      // the Live tab's scroll box stays in place (detaching it on every render reset its scroll position)
      if (this._announce.parentNode !== panel) panel.append(this._announce);
    } else {
      if (this.tab === "live" || this.tab === "problems") panel.replaceChildren(el, this._announce);
      else panel.replaceChildren(el);
      if (this.tab !== "live") el.scrollTop = prevScroll;
    }
    const tech = prevTech ? panel.querySelector?.(".hf-tech") : null;
    if (tech) tech.scrollTop = prevTech;
    if (key) panel.querySelector(`[data-hf-key="${key}"]`)?.focus({ preventScroll: true });
  }

  async _fullText() {
    const s = this.session;
    if (!s.id) return "";
    if (s.kind === "queue") throw new Error("no text route for queues");
    return s.api.getLogText(s.id);
  }
}

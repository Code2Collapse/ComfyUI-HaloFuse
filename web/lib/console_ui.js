// HaloFuse floating console: window chrome + tabs (Logs, Progress, Telemetry, Problems, Summary) bound to a
// JobSession. Lives on document.body, independent of the compact panel. Side-effect-free ES module.

import { bindH, copyText } from "./dom.js";
import { FloatingWindow } from "./console_window.js";
import { LogView } from "./console_logs.js";
import { renderProblems, renderProgress, renderSummary, renderTelemetry } from "./console_panes.js";
import { cancelInfo, progressText, statusInfo } from "./job_view.js";

const TABS = [
  ["logs", "Logs"], ["progress", "Progress"], ["telemetry", "Telemetry"], ["problems", "Problems"], ["summary", "Summary"],
];
const PANE_MIN_INTERVAL_MS = 500;

export class HaloFuseConsole {
  /**
   * @param {{doc: Document, win: Window, storage: Storage|null, session: import("./session.js").JobSession,
   *   viewUrl: (ref: object) => string, previewUrl: (jobId: string, name: string) => string,
   *   onFocusNode: (nodeId: string) => void, onRetryDelivery: (jobId: string) => void}} o
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
    });
    this.panels.logs.append(this.logView.el);
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

  open(tab) {
    this.window.open();
    if (tab) this.selectTab(tab);
    this._renderPane(true);
  }

  close() { this.window.close(); }
  toggle() { if (this.window.isOpen) this.window.close(); else this.open(); }

  selectTab(id, focus = true) {
    if (!this.panels[id]) id = "logs";
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

  _onWindow() {
    this.session.setWatching(this.window.isOpen);
    if (this.window.isOpen) this._renderPane(true);
  }

  _onSession(what) {
    if (what === "attach") this.logView.reset();
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
    const prog = job ? progressText(this.session.run.snapshot().progress || job.progress, job.status) : "";
    this.window.setPill(`HaloFuse · ${prog || st.label}`, st.tone);
    const errors = this.session.store.counts().error;
    this.tabButtons.problems._count.textContent = errors ? String(errors) : "";
    this.tabButtons.problems._count.className = `hf-count${errors ? " hf-tone-danger" : ""}`;
  }

  /** Non-log panes re-render at most twice a second, and only when visible. */
  _renderPane(force = false) {
    if (!this.window.isOpen || this.tab === "logs") return;
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
    let el;
    if (this.tab === "progress") el = renderProgress(H, { ...ctx, onFocusNode: (id) => this.o.onFocusNode(id) });
    else if (this.tab === "telemetry") el = renderTelemetry(H, ctx);
    else if (this.tab === "problems") el = renderProblems(H, { ...ctx, warnings: s.warnings, onJump: (seq) => this.jumpTo(seq) });
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
    panel.replaceChildren(el);
    el.scrollTop = prevScroll;
  }

  async _fullText() {
    const s = this.session;
    if (!s.id) return "";
    if (s.kind === "queue") throw new Error("no text route for queues");
    return s.api.getLogText(s.id);
  }
}

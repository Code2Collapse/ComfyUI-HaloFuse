// HaloFuse console: live log tab (search, severity / source filters, timestamps, follow, copy, save,
// clear view, details). Rendering is virtualised and filtering is time-sliced, so a fast stream never
// produces long main-thread tasks. Side-effect-free ES module.

import { bindH, copyText, downloadText } from "./dom.js";
import { FilteredView, formatEntry } from "./log_model.js";
import { VirtualList } from "./virtual_list.js";
import { fmtClock, fmtIsoUtc } from "./format.js";

const ROW_H = 20;
const SOURCE_LABEL = { runner: "runner", comfyui: "ComfyUI", bridge: "HaloFuse", provider: "cloud" };
const LEVEL_LABEL = { error: "ERROR", warning: "WARN", info: "", debug: "debug" };
const META_KINDS = new Set(["state", "submit", "prompt", "cost", "queue", "note", "delivery", "phase", "node", "progress", "telemetry", "item"]);
const nf = new Intl.NumberFormat();

export class LogView {
  /**
   * @param {{doc: Document, win: Window, store: import("./log_model.js").LogStore,
   *   prefs: () => object, setPrefs: (p: object) => void, getFullText: () => Promise<string>,
   *   fileName: () => string, onTimestamps: (on: boolean) => void}} o
   */
  constructor(o) {
    Object.assign(this, { doc: o.doc, win: o.win, store: o.store, prefs: o.prefs, setPrefs: o.setPrefs,
      getFullText: o.getFullText, fileName: o.fileName, onTimestamps: o.onTimestamps, hasJob: o.hasJob });
    const H = bindH(this.doc);
    this.view = new FilteredView(this.store);
    this._work = 0;
    this._lastCount = 0;
    this._newWhilePaused = 0;
    this._searchTimer = 0;
    const p = this.prefs();

    this.search = H("input", { className: "hf-input hf-search", type: "search", placeholder: "Search logs…",
      attrs: { "aria-label": "Search logs" }, oninput: () => this._debouncedFilter() });
    this.regexBtn = this._toggle(H, ".*", "Use a regular expression", false, () => this._applyFilter());
    this.levelSel = H("select", { className: "hf-select", attrs: { "aria-label": "Minimum severity" }, onchange: () => this._applyFilter() },
      H("option", { value: "all", text: "All levels" }),
      H("option", { value: "info", text: "Info and above" }),
      H("option", { value: "warning", text: "Warnings and errors" }));
    this.errBtn = this._toggle(H, "Errors only", "Show only errors", false, () => { this.warnBtn.setAttribute("aria-pressed", "false"); this._applyFilter(); });
    this.warnBtn = this._toggle(H, "Warnings only", "Show only warnings", false, () => { this.errBtn.setAttribute("aria-pressed", "false"); this._applyFilter(); });
    this.sourceBoxes = {};
    const chips = Object.entries(SOURCE_LABEL).map(([key, label]) => {
      const box = H("input", { type: "checkbox", checked: p.sources?.[key] !== false, onchange: () => {
        this.setPrefs({ sources: { ...this.prefs().sources, [key]: box.checked } });
        this._applyFilter();
      } });
      this.sourceBoxes[key] = box;
      return H("label", { className: `hf-chip hf-src-${key}`, title: `Show ${label} lines` }, box, label);
    });
    this.tsBtn = this._toggle(H, "Timestamps", "Show timestamps", p.timestamps !== false, (on) => {
      this.setPrefs({ timestamps: on });
      this.onTimestamps(on);
    });
    this.followBtn = H("button", { className: "hf-btn", type: "button", onclick: () => this.setFollowing(!this.list.follow) });
    this.copyBtn = H("button", { className: "hf-btn", type: "button", text: "Copy all", title: "Copy the complete saved log",
      onclick: () => this._copyAll() });
    this.saveBtn = H("button", { className: "hf-btn", type: "button", text: "Save", title: "Download the complete saved log",
      onclick: () => this._save() });
    this.clearBtn = H("button", { className: "hf-btn", type: "button", text: "Clear view",
      title: "Hide the current lines from this view. The saved job log is not changed.", onclick: () => this._clearOrRestore() });

    // one toolbar row: filters on the left, view + export actions on the right (wraps in narrow windows)
    this.toolbar = H("div", { className: "hf-toolbar", attrs: { role: "toolbar", "aria-label": "Log filters and actions" } },
      this.search, this.regexBtn, this.levelSel, this.errBtn, this.warnBtn,
      H("span", { className: "hf-chips", attrs: { role: "group", "aria-label": "Sources" } }, ...chips),
      H("span", { className: "hf-spacer" }),
      H("span", { className: "hf-btn-group", attrs: { role: "group", "aria-label": "Log actions" } },
        this.tsBtn, this.followBtn, this.copyBtn, this.saveBtn, this.clearBtn));
    this.filterError = H("div", { className: "hf-filter-error", hidden: true, attrs: { role: "alert" } });
    this.listEl = H("div", { className: "hf-loglist", attrs: { role: "grid", "aria-label": "Job log", "aria-readonly": "true" } });
    this.detailHead = H("div", { className: "hf-detail-head" });
    this.detailPre = H("pre", { tabIndex: 0 });
    this.detail = H("div", { className: "hf-detail", hidden: true, attrs: { role: "region", "aria-label": "Line details" } },
      this.detailHead, this.detailPre);
    this.statusText = H("span", { className: "hf-grow", attrs: { "aria-live": "off" } });
    this.notice = H("span", { attrs: { role: "status", "aria-live": "polite" } });
    this.statusBar = H("div", { className: "hf-logstatus" }, this.statusText, this.notice);
    // empty state over the list: what is happening, not a blank pane
    this.emptyEl = H("div", { className: "hf-log-empty", attrs: { role: "status" } });
    this.listWrap = H("div", { className: "hf-logwrap" }, this.listEl, this.emptyEl);
    this.el = H("div", { className: "hf-tabpanel" }, this.toolbar, this.filterError, this.listWrap, this.detail, this.statusBar);

    this.list = new VirtualList(this.listEl, {
      rowHeight: ROW_H, win: this.win,
      renderRow: (row, i) => this._renderRow(row, i),
      onActivate: (i) => this._showDetail(this.view.entryAt(i)),
      onScrollState: (pinned) => this._setFollowState(pinned),
    });
    this._unsub = this.store.subscribe(() => this._schedule());
    this._applyFilter();
    this._setFollowState(true);
  }

  /** Scroll to and select the entry with this seq (restores cleared lines and filters if needed). */
  jumpToSeq(seq) {
    if (seq < this.store.viewStart) this.store.restoreView();
    let i = this.view.indexOfSeq(seq);
    if (i < 0) {
      this.search.value = "";
      this.levelSel.value = "all";
      this.errBtn.setAttribute("aria-pressed", "false");
      this.warnBtn.setAttribute("aria-pressed", "false");
      for (const b of Object.values(this.sourceBoxes)) b.checked = true;
      this.setPrefs({ sources: { runner: true, comfyui: true, bridge: true, provider: true } });
      this._applyFilter();
      i = this.view.indexOfSeq(seq);
    }
    if (i < 0) return false;
    this.setFollowing(false);
    this.list.setActive(i);
    this.list.scrollToIndex(i);
    this._showDetail(this.view.entryAt(i));
    return true;
  }

  setFollowing(on) {
    if (on) { this.list.scrollToEnd(); this._newWhilePaused = 0; } else this.list.follow = false;
    this._setFollowState(on);
  }

  /** Called when the store was replaced by another job. */
  reset() {
    this.detail.hidden = true;
    this._newWhilePaused = 0;
    this._applyFilter();
    this.setFollowing(true);
  }

  destroy() {
    this._unsub?.();
    if (this._work) this.win.cancelAnimationFrame(this._work);
    this.list.destroy();
  }

  _toggle(H, label, title, on, cb) {
    const b = H("button", { className: "hf-btn", type: "button", text: label, title, attrs: { "aria-pressed": String(on) } });
    b.addEventListener("click", () => {
      const next = b.getAttribute("aria-pressed") !== "true";
      b.setAttribute("aria-pressed", String(next));
      cb(next);
    });
    return b;
  }

  _filterSpec() {
    const lvl = this.levelSel.value;
    const levels = lvl === "info" ? ["info", "warning", "error"] : lvl === "warning" ? ["warning", "error"] : null;
    const sources = Object.entries(this.sourceBoxes).filter(([, b]) => b.checked).map(([k]) => k);
    return {
      text: this.search.value,
      regex: this.regexBtn.getAttribute("aria-pressed") === "true",
      levels,
      sources,
      errorsOnly: this.errBtn.getAttribute("aria-pressed") === "true",
      warningsOnly: this.warnBtn.getAttribute("aria-pressed") === "true",
    };
  }

  _debouncedFilter() {
    if (this._searchTimer) this.win.clearTimeout(this._searchTimer);
    this._searchTimer = this.win.setTimeout(() => { this._searchTimer = 0; this._applyFilter(); }, 150);
  }

  _applyFilter() {
    const err = this.view.setFilter(this._filterSpec());
    this.filterError.hidden = !err;
    this.filterError.textContent = err || "";
    this._lastCount = -1;
    this.list.refresh();
    this._schedule();
  }

  /** Time-sliced catch-up: at most ~6 ms of filtering per frame. */
  _schedule() {
    if (this._work) return;
    this._work = this.win.requestAnimationFrame(() => {
      this._work = 0;
      const done = this.view.step(6);
      const n = this.view.length;
      if (n !== this._lastCount) {
        if (n < this._lastCount) this.list.refresh();
        if (!this.list.follow && this._lastCount >= 0 && n > this._lastCount) this._newWhilePaused += n - this._lastCount;
        this._lastCount = n;
        this.list.setCount(n);
      }
      this._updateStatus(done);
      if (!done) this._schedule();
    });
  }

  _renderRow(row, i) {
    const e = this.view.entryAt(i);
    if (!row.firstChild) {
      const d = this.doc;
      for (const c of ["hf-c-ts", "hf-c-src", "hf-c-lvl", "hf-c-text"]) {
        const s = d.createElement("span");
        s.className = c;
        row.append(s);
      }
      row.tabIndex = -1;
    }
    if (!e) {
      // not scanned yet (filter catching up): draw blank and drop the index so the row is redrawn when data arrives
      for (const c of row.children) c.textContent = "";
      row.className = "hf-vl-row";
      row.dataset.index = "";
      return;
    }
    const [ts, src, lvl, text] = row.children;
    ts.textContent = fmtClock(e.ts);
    src.textContent = SOURCE_LABEL[e.source] || e.source;
    src.className = `hf-c-src hf-src-${e.source}`;
    lvl.textContent = LEVEL_LABEL[e.level] ?? "";
    const shown = e.display ?? e.text;
    const firstLine = shown.indexOf("\n");
    text.textContent = firstLine >= 0 ? `${shown.slice(0, firstLine)}  …` : shown;
    row.className = `hf-vl-row hf-l-${e.level}${META_KINDS.has(e.kind) ? " hf-k-meta" : ""}`;
    row.title = "";
  }

  _showDetail(e) {
    if (!e) { this.detail.hidden = true; return; }
    const H = bindH(this.doc);
    const parts = [
      H("strong", { text: `${LEVEL_LABEL[e.level] || "INFO"} · ${SOURCE_LABEL[e.source] || e.source}` }),
      H("span", { text: fmtIsoUtc(e.ts) || "no timestamp" }),
      H("span", { text: `line ${nf.format(e.seq + 1)}` }),
      e.kind !== "line" ? H("span", { text: `event: ${e.kind}` }) : null,
      e.node_id ? H("span", { text: `node ${e.node_id}` }) : null,
      H("span", { className: "hf-grow", style: { flex: "1" } }),
      H("button", { className: "hf-btn", type: "button", text: "Copy line", onclick: () => this._flash(copyText(this.win, formatEntry(e)), "Line copied.") }),
      H("button", { className: "hf-btn", type: "button", text: "Hide details", attrs: { "aria-label": "Hide line details" },
        onclick: () => { this.detail.hidden = true; this.listEl.focus(); } })];
    // replaceChildren() would stringify null placeholders as "null"
    this.detailHead.replaceChildren(...parts.filter(Boolean));
    const data = e.data && Object.keys(e.data).length ? `\n\n${JSON.stringify(e.data, null, 2)}` : "";
    this.detailPre.textContent = (e.display && e.display !== e.text ? `${e.display}\n${e.text}` : e.text) + data;
    this.detail.hidden = false;
  }

  _setFollowState(pinned) {
    if (pinned) { this.list.follow = true; this._newWhilePaused = 0; }
    else this.list.follow = false;
    this.followBtn.setAttribute("aria-pressed", String(this.list.follow));
    this.followBtn.textContent = this.list.follow ? "Auto-scroll on" : "Auto-scroll paused";
    this.followBtn.title = this.list.follow ? "Pause auto-scroll" : "Resume auto-scroll and jump to the newest line";
    this._updateStatus(true);
  }

  _updateStatus(done) {
    const c = this.store.counts();
    const parts = [`${nf.format(c.total)} lines`, `${nf.format(c.warning)} warnings`, `${nf.format(c.error)} errors`];
    const hidden = c.total - this.view.length;
    if (hidden > 0) parts.push(`${nf.format(hidden)} hidden by filters`);
    if (this.store.clearedCount) parts.push(`${nf.format(this.store.clearedCount)} cleared from view`);
    if (!done) parts.push("filtering…");
    if (!this.list.follow && this._newWhilePaused) parts.push(`${nf.format(this._newWhilePaused)} new since paused`);
    this.statusText.textContent = parts.join(" · ");
    let empty = "";
    if (this.store.size === 0) empty = this.hasJob?.() ? "Waiting for the first log lines from the cloud job…" : "No job attached. Start a render or open one from Recent jobs.";
    else if (c.total === 0) empty = "The view is cleared. New lines appear here; use “Show cleared” to bring the earlier ones back.";
    else if (done && this.view.length === 0) empty = "No lines match the current filters.";
    this.emptyEl.textContent = empty;
    this.emptyEl.hidden = !empty;
    this.clearBtn.textContent = this.store.clearedCount ? `Show cleared (${nf.format(this.store.clearedCount)})` : "Clear view";
  }

  _clearOrRestore() {
    if (this.store.clearedCount) this.store.restoreView(); else this.store.clearView();
    this.detail.hidden = true;
    this.list.refresh();
    this._updateStatus(true);
  }

  async _fullText() {
    try {
      return { text: await this.getFullText(), fromServer: true };
    } catch {
      const lines = [];
      for (let s = 0; s < this.store.size; s++) lines.push(formatEntry(this.store.get(s)));
      return { text: lines.length ? `${lines.join("\n")}\n` : "", fromServer: false };
    }
  }

  async _copyAll() {
    this.copyBtn.disabled = true;
    try {
      const { text, fromServer } = await this._fullText();
      const ok = await copyText(this.win, text);
      const n = text ? text.split("\n").length - 1 : 0;
      this._note(ok ? `Copied ${nf.format(n)} lines${fromServer ? "" : " (saved log unavailable; copied what this view loaded)"}.`
        : "The browser blocked the clipboard. Use Save instead.");
    } finally { this.copyBtn.disabled = false; }
  }

  async _save() {
    this.saveBtn.disabled = true;
    try {
      const { text, fromServer } = await this._fullText();
      downloadText(this.win, this.fileName(), text);
      this._note(fromServer ? "Saved the complete job log." : "Saved what this view loaded (the saved log was unavailable).");
    } finally { this.saveBtn.disabled = false; }
  }

  async _flash(promise, msg) { if (await promise) this._note(msg); }

  _note(msg) {
    this.notice.textContent = msg;
    if (this._noteTimer) this.win.clearTimeout(this._noteTimer);
    this._noteTimer = this.win.setTimeout(() => { this.notice.textContent = ""; }, 6000);
  }
}

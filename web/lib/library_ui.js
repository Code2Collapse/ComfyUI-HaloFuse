// HaloFuse panel section "Cloud model library" (flaws C1 / C6): what is already in the cloud library, what exists only
// on this computer, what differs from your copy, duplicates. Loads on demand (GET /halofuse/library) when the section
// opens or Refresh is pressed - never on a timer. Read-only: uploads stay in the pre-render model check.
// Side-effect-free ES module: DOM is created in the constructor.

import { bindH } from "./dom.js";
import { fmtBytes } from "./format.js";
import { FILTERS, filterItems, statusOf, summarize } from "./library_view.js";

const MAX_ROWS = 200;

export class LibrarySection {
  /** @param {{doc: Document, api: {library: () => Promise<object>}}} o */
  constructor({ doc, api }) {
    this.doc = doc;
    this.api = api;
    this.report = null;
    this.loading = false;
    this.error = "";
    const H = bindH(doc);
    this.H = H;
    const uid = Math.random().toString(36).slice(2, 8);
    this.summary = H("div", { className: "hf-hint", style: { margin: "2px 0 8px" }, attrs: { "aria-live": "polite" } });
    this.filterSel = H("select", { className: "hf-select", id: `hf-lib-filter-${uid}`, style: { width: "auto" },
      attrs: { "aria-label": "Show" }, onchange: () => this.render() },
      ...FILTERS.map((f) => H("option", { value: f.value, text: f.label })));
    this.search = H("input", { className: "hf-input", type: "search", id: `hf-lib-search-${uid}`,
      placeholder: "Find a model", attrs: { "aria-label": "Find a model by folder or file name" },
      oninput: () => this.render() });
    this.refreshBtn = H("button", { className: "hf-btn", type: "button", text: "Refresh",
      title: "List the cloud model library again", onclick: () => this.load() });
    this.list = H("div", { className: "hf-list", attrs: { role: "list" } });
    this.more = H("div", { className: "hf-hint", style: { margin: "4px 0 0" } });
    this.dups = H("div", { className: "hf-hint", style: { margin: "6px 0 0" } });
    this.el = H("div", {},
      this.summary,
      H("div", { className: "hf-row", style: { gap: "6px", marginBottom: "6px" } },
        H("div", { className: "hf-grow" }, this.search), this.filterSel, this.refreshBtn),
      this.list, this.more, this.dups);
    this.render();
  }

  async load() {
    if (this.loading) return;
    this.loading = true;
    this.error = "";
    this.render();
    try {
      this.report = await this.api.library();
    } catch (e) {
      this.error = e?.message || "The cloud model library could not be listed.";
    } finally {
      this.loading = false;
      this.render();
    }
  }

  /** Load once when the section is first opened; Refresh reloads. */
  ensureLoaded() { if (!this.report && !this.loading) this.load(); }

  render() {
    const H = this.H;
    this.refreshBtn.disabled = this.loading;
    this.list.replaceChildren();
    this.more.textContent = "";
    this.dups.textContent = "";
    if (this.loading && !this.report) { this.summary.textContent = "Listing the cloud model library…"; return; }
    if (this.error) { this.summary.className = "hf-hint hf-tone-danger"; this.summary.textContent = this.error; return; }
    this.summary.className = "hf-hint";
    if (!this.report) { this.summary.textContent = "Open this section to list the models in your cloud library."; return; }
    const s = summarize(this.report);
    this.summary.textContent = `${s.inCloud} model${s.inCloud === 1 ? "" : "s"} in the cloud (${fmtBytes(s.cloudBytes)})`
      + (s.count.local_only ? ` · ${s.count.local_only} only on this computer (${fmtBytes(s.localOnlyBytes)})` : "")
      + (s.count.size_mismatch ? ` · ${s.count.size_mismatch} differ from your copy` : "")
      + (s.truncated ? " · the listing was cut short (very large library)" : "")
      + (this.loading ? " · refreshing…" : "");
    const rows = filterItems(this.report.items, { filter: this.filterSel.value, text: this.search.value });
    if (!rows.length) {
      this.list.append(H("div", { className: "hf-hint", style: { margin: 0 }, text: "No models match." }));
    }
    for (const it of rows.slice(0, MAX_ROWS)) {
      const st = statusOf(it);
      const size = it.cloud_size ?? it.local_size;
      const sizeText = it.status === "size_mismatch"
        ? `cloud ${fmtBytes(it.cloud_size)} · yours ${fmtBytes(it.local_size)}` : fmtBytes(size);
      // two lines: the file (full width, the part that identifies it), then size + status in small type
      this.list.append(H("div", { className: "hf-item", attrs: { role: "listitem" },
        style: { flexDirection: "column", alignItems: "stretch", gap: "2px" } },
        H("span", { title: `${it.folder}/${it.name}`, text: `${it.folder}/${it.name}`,
          style: { whiteSpace: "normal", overflowWrap: "anywhere" } }),
        H("span", { style: { display: "flex", gap: "8px", fontSize: "12px", whiteSpace: "nowrap" } },
          H("span", { className: "hf-num hf-tone-neutral", text: sizeText }),
          H("span", { className: `hf-tone-${st.tone}`, text: st.label }))));
    }
    if (rows.length > MAX_ROWS) this.more.textContent = `Showing ${MAX_ROWS} of ${rows.length}. Type in Find a model to narrow the list.`;
    if (s.duplicates) {
      this.dups.textContent = `${s.duplicates} model${s.duplicates === 1 ? " is" : "s are"} stored more than once in the cloud `
        + `(${fmtBytes(s.duplicateBytes)} could be freed).`;
    }
  }
}

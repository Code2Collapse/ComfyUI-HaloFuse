// HaloFuse panel section "My custom nodes" (owner 2026-11-23): the artist lists extra custom node packs for their
// own jobs (GitHub owner/repo[@ref] or Comfy Registry id[@version], one per line). "Check and save" asks the bridge
// to pin each line and read its licence (POST /halofuse/user_nodes/resolve), then saves the pinned list as the
// `user_nodes` setting; the runner installs it at the start of every job when the cloud runtime supports it.
// Dynamic text only via textContent (dom.js). Side-effect-free ES module: DOM is created in the constructor.

import { bindH, clear } from "./dom.js";
import { addLine, checkSummary, entryText, licenceText, linesOf } from "./user_nodes_view.js";

export class UserNodesSection {
  /** @param {{doc: Document, api: {resolveUserNodes: (lines: string) => Promise<object>},
   *   save: (prefs: object) => Promise<void>}} o */
  constructor({ doc, api, save }) {
    Object.assign(this, { doc, api, save });
    const H = bindH(doc);
    this.H = H;
    const uid = Math.random().toString(36).slice(2, 8);
    this.busy = false;
    this.rows = null;
    this._saved = null;
    this.note = H("div", { className: "hf-hint", style: { margin: "2px 0 8px" } });
    this.input = H("textarea", { className: "hf-input", id: `hf-user-nodes-${uid}`, rows: 4, spellcheck: false,
      placeholder: "owner/repo  or  owner/repo@v1.2  or  a Comfy Registry id",
      attrs: { "aria-label": "My custom nodes, one GitHub repository or Comfy Registry id per line" },
      style: { width: "100%", boxSizing: "border-box", fontFamily: "monospace", resize: "vertical" } });
    this.checkBtn = H("button", { className: "hf-btn", type: "button", text: "Check and save",
      title: "Pin each line to an exact version and show its licence, then save the list", onclick: () => this.check() });
    // owner 2026-11-24: the Workflow Check adds the pack a missing node needs by itself (public GitHub packs whose
    // licence the image's licence policy allows; the bridge decides, user_nodes.apply_to_report)
    this.autoBox = H("input", { type: "checkbox", id: `hf-user-nodes-auto-${uid}`,
      onchange: () => this.save({ auto_add_packs: this.autoBox.checked }) });
    this.status = H("div", { className: "hf-hint", attrs: { role: "status", "aria-live": "polite" } });
    this.list = H("div", { className: "hf-list", attrs: { role: "list" } });
    this.el = H("div", {}, this.note,
      H("label", { className: "hf-field", htmlFor: this.input.id }, H("span", { text: "One pack per line" }), this.input),
      H("div", { className: "hf-row", style: { margin: "6px 0" } }, this.checkBtn),
      H("label", { className: "hf-row", htmlFor: this.autoBox.id, style: { gap: "6px", margin: "2px 0 6px" },
        title: "Only public GitHub packs with a licence the HaloFuse image allows; code on this computer is never uploaded" },
        this.autoBox, H("span", { text: "Always add the packs a workflow needs" })),
      this.status, this.list);
    this.render();
  }

  /** The panel passes every settings update; the text box is refilled only when the saved list changed. */
  setSettings(s) {
    if (!s) return;
    this.supported = s.user_nodes_supported === true;
    this.noteText = s.user_nodes_note || "";
    if (this.doc.activeElement !== this.autoBox) this.autoBox.checked = s.auto_add_packs === true;
    const saved = JSON.stringify(s.user_nodes || []);
    if (saved !== this._saved) {
      this._saved = saved;
      this.entries = s.user_nodes || [];
      if (this.doc.activeElement !== this.input) this.input.value = linesOf(this.entries);
    }
    this.render();
  }

  async check() {
    if (this.busy) return;
    this.busy = true;
    this.statusText = { text: "Checking on GitHub and the Comfy Registry…", tone: "active" };
    this.render();
    try {
      const r = await this.api.resolveUserNodes(this.input.value);
      this.rows = r.rows || [];
      this.statusText = checkSummary(r);
      if (r.ok) {
        await this.save({ user_nodes: r.entries || [] });
        this.input.value = linesOf(r.entries);
        this.rows = null; // the saved list below now shows them
      }
    } catch (e) {
      this.statusText = { text: e?.message || "The custom node list could not be checked.", tone: "danger" };
    } finally {
      this.busy = false;
      this.render();
    }
  }

  /** "Add <pack> to my custom nodes" from the Workflow Check: append the line, check and save. */
  async addLine(line) {
    this.input.value = addLine(this.input.value, line);
    await this.check();
  }

  render() {
    const H = this.H;
    this.note.className = `hf-hint${this.supported === false ? " hf-tone-warning" : ""}`;
    this.note.textContent = this.noteText || "Extra custom node packs for your own jobs, installed when each job starts.";
    this.checkBtn.disabled = this.busy;
    this.checkBtn.textContent = this.busy ? "Checking…" : "Check and save";
    this.status.className = `hf-hint hf-tone-${this.statusText?.tone || "neutral"}`;
    this.status.textContent = this.statusText?.text || "";
    clear(this.list);
    const rows = this.rows || (this.entries || []).map((e) => ({ ok: true, entry: e, line: e.line, notes: [] }));
    for (const r of rows) {
      if (!r.ok) {
        this.list.append(H("div", { className: "hf-item hf-tone-danger", attrs: { role: "listitem" } },
          H("span", { className: "hf-grow", style: { whiteSpace: "normal" }, text: `${r.line ? `${r.line}: ` : ""}${r.message}` })));
        continue;
      }
      const lic = licenceText(r.entry);
      this.list.append(H("div", { className: "hf-item", attrs: { role: "listitem" }, style: { display: "block" } },
        H("div", { text: entryText(r.entry) }),
        H("div", { className: `hf-meta hf-tone-${lic.tone}`, style: { whiteSpace: "normal" }, text: `Licence: ${lic.text}` }),
        ...(r.notes || []).map((n) => H("div", { className: "hf-meta hf-tone-warning", text: n }))));
    }
    if (!rows.length && !this.busy) {
      this.list.append(H("div", { className: "hf-hint", style: { margin: 0 }, text: "No custom nodes yet." }));
    }
  }
}

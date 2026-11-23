// HaloFuse warm cloud machine section of the panel (owner 2026-11-23): "Keep the cloud machine warm", the running
// state with its cost, a prominent "Shut down the cloud machine" button with an inline confirmation that carries the
// cost line, and the cleaning choices. A state the bridge cannot confirm shows as a danger banner (role=alert).
// Side-effect-free ES module: nothing runs at import; polling starts in start() and stops in destroy().

import { bindH, clear } from "./dom.js";
import { POLL_MS, warmModel } from "./warm_view.js";

export class WarmSection {
  /**
   * @param {{doc: Document, win: Window, api: object, context: () => {instanceType: string, rate: number|null},
   *   save: (prefs: object) => Promise<void>}} o
   */
  constructor({ doc, win, api, context, save }) {
    Object.assign(this, { doc, win, api, context, save });
    const H = bindH(doc);
    this.H = H;
    const uid = Math.random().toString(36).slice(2, 8);
    this.view = null;
    this.error = null;
    this.busy = false;
    this.confirming = false;
    this._timer = null;
    this.keep = H("input", { type: "checkbox", id: `hf-warm-keep-${uid}`, onchange: () => this._save({ keep_warm: this.keep.checked }) });
    this.clean = H("input", { type: "checkbox", id: `hf-warm-clean-${uid}`, onchange: () => this._save({ clean_between_renders: this.clean.checked }) });
    this.unload = H("input", { type: "checkbox", id: `hf-warm-unload-${uid}`, onchange: () => this._save({ unload_models_between_renders: this.unload.checked }) });
    this.banner = H("div", { className: "hf-consent hf-tone-danger", hidden: true, attrs: { role: "alert" } });
    this.title = H("strong", { attrs: { role: "status", "aria-live": "polite" } });
    this.lines = H("div", { className: "hf-hint" });
    this.btns = H("div", { className: "hf-row" });
    this.confirmBox = H("div", { className: "hf-consent", hidden: true, attrs: { role: "group", "aria-label": "Confirm shutting down the cloud machine" } });
    this.msg = H("div", { className: "hf-hint", attrs: { "aria-live": "polite" } });
    this.cleanNote = H("div", { className: "hf-hint" });
    const check = (input, text) => H("label", { className: "hf-field hf-check", htmlFor: input.id }, input, H("span", { text }));
    this.el = H("div", { className: "hf-section hf-warm", attrs: { "aria-label": "Warm cloud machine" } },
      this.banner, this.title, check(this.keep, "Keep the cloud machine warm (renders start it and it stays on until you shut it down)"),
      this.btns, this.confirmBox, this.msg, this.lines,
      H("details", {}, H("summary", { text: "Cleaning between renders" }),
        check(this.clean, "Clean caches between renders"),
        check(this.unload, "Also unload models between renders (slower next render, frees GPU memory)"),
        this.cleanNote));
    this.render();
  }

  start() {
    if (this._timer == null) this._timer = this.win.setInterval(() => this.refresh(), POLL_MS);
    return this.refresh();
  }

  destroy() {
    if (this._timer != null) this.win.clearInterval(this._timer);
    this._timer = null;
    this.el.remove();
  }

  async refresh() {
    try {
      const d = await this.api.getWarm();
      this.view = d?.warm || null;
      this.error = null;
    } catch (err) {
      // the bridge itself did not answer: never pretend the machine is off
      this.error = `HaloFuse could not read the warm machine state (${err.message}). A cloud machine may still be running.`;
    }
    this.render();
  }

  async _save(prefs) {
    try { await this.save(prefs); } finally { await this.refresh(); }
  }

  async _act(fn, okText) {
    if (this.busy) return;
    this.busy = true;
    this.msg.textContent = "";
    this.render();
    try {
      const d = await fn();
      this.view = d?.warm || this.view;
      this.msg.className = "hf-hint";
      this.msg.textContent = okText || "";
    } catch (err) {
      this.msg.className = "hf-hint hf-tone-danger";
      this.msg.textContent = err.message;
    } finally {
      this.busy = false;
      this.confirming = false;
      this.render();
    }
  }

  render() {
    const H = this.H;
    const ctx = this.context ? this.context() : {};
    const m = warmModel(this.view, { nowS: Date.now() / 1000, rate: ctx.rate ?? null });
    const s = this.view?.settings || {};
    this.keep.checked = s.keep_warm === true;
    this.clean.checked = s.clean_between_renders !== false;
    this.unload.checked = s.unload_models_between_renders === true;
    this.cleanNote.textContent = m.cleaning?.note || "";
    const banner = this.error || m.banner;
    this.banner.hidden = !banner;
    this.banner.textContent = banner || "";
    this.title.textContent = m.title;
    this.title.className = `hf-tone-${m.tone}`;
    clear(this.lines);
    for (const line of m.lines) this.lines.append(H("div", { text: line }));
    clear(this.btns);
    if (m.actions.start) {
      this.btns.append(H("button", { className: "hf-btn", type: "button", disabled: this.busy || !ctx.instanceType,
        text: ctx.instanceType ? `Start a warm ${ctx.instanceType} machine` : "Start a warm machine",
        onclick: () => this._act(() => this.api.startWarm(ctx.instanceType), "Starting the warm machine. It is billed until you shut it down.") }));
    }
    if (m.actions.shutdown || this.error) {
      this.btns.append(H("button", { className: "hf-btn hf-danger hf-solid", type: "button", disabled: this.busy,
        text: "Shut down the cloud machine", onclick: () => { this.confirming = true; this.render(); } }));
    }
    if (m.actions.check || this.error) {
      this.btns.append(H("button", { className: "hf-btn", type: "button", disabled: this.busy, text: "Check",
        onclick: () => this._act(() => this.api.checkWarm()) }));
    }
    clear(this.confirmBox);
    this.confirmBox.hidden = !this.confirming;
    if (this.confirming) {
      this.confirmBox.append(H("div", { text: m.confirm || "Shut down the cloud machine? The next render will start a new machine." }),
        H("div", { className: "hf-row" },
          H("button", { className: "hf-btn hf-danger hf-solid", type: "button", disabled: this.busy, text: "Yes, shut it down",
            onclick: () => this._act(() => this.api.shutdownWarm()) }),
          H("button", { className: "hf-btn", type: "button", text: "Keep it running", onclick: () => { this.confirming = false; this.render(); } })));
    }
  }
}

// HaloFuse warm cloud machine section of the panel (owner 2026-11-23; modes, warm duration, cost, cache and
// housekeeping 2026-11-24, L9.340): the warm mode (Off / Always Warm / Smart Warm) and the warm duration, the running
// state with remaining warm time and costs, a prominent "Shut down the cloud machine" button with an inline
// confirmation that carries the cost line, "Keep it warm", and the housekeeping choices (cleaning, memory behaviour,
// transfers, model cache). A state the bridge cannot confirm shows as a danger banner (role=alert).
// Side-effect-free ES module: nothing runs at import; polling starts in start() and stops in destroy().

import { bindH, clear } from "./dom.js";
import { MEMORY, MODES, POLL_MS, warmModel } from "./warm_view.js";

const MIN_CUSTOM_MIN = 5;
const MAX_CUSTOM_MIN = 7 * 24 * 60;

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
    this.customOpen = false;
    this._timer = null;
    const sel = (id, opts, onchange) => H("select", { className: "hf-select", id: `${id}-${uid}`, onchange },
      ...opts.map(([k, label]) => H("option", { value: k, text: label })));
    this.modeSel = sel("hf-warm-mode", MODES, () => this._mode({ warm_mode: this.modeSel.value }));
    this.durSel = H("select", { className: "hf-select", id: `hf-warm-dur-${uid}`, onchange: () => this._duration() });
    this.customIn = H("input", { className: "hf-input", type: "number", id: `hf-warm-custom-${uid}`,
      attrs: { min: MIN_CUSTOM_MIN, max: MAX_CUSTOM_MIN, step: 5, inputmode: "numeric" } });
    this.customBox = H("div", { className: "hf-row", hidden: true },
      H("label", { className: "hf-field hf-grow", htmlFor: this.customIn.id }, H("span", { text: "Custom warm duration (minutes, 5 – 10080)" }), this.customIn),
      H("button", { className: "hf-btn", type: "button", text: "Set", onclick: () => this._custom() }));
    this.modeHint = H("div", { className: "hf-hint" });
    this.clean = H("input", { type: "checkbox", id: `hf-warm-clean-${uid}`, onchange: () => this._save({ clean_between_renders: this.clean.checked }) });
    this.memSel = sel("hf-warm-mem", MEMORY, () => this._save({ memory_behavior: this.memSel.value }));
    this.memHint = H("div", { className: "hf-hint" });
    this.highIn = H("input", { className: "hf-input", type: "number", id: `hf-warm-high-${uid}`, attrs: { min: 10, max: 99 } });
    this.lowIn = H("input", { className: "hf-input", type: "number", id: `hf-warm-low-${uid}`, attrs: { min: 10, max: 99 } });
    this.autoBox = H("div", { hidden: true },
      H("div", { className: "hf-row" },
        H("label", { className: "hf-field hf-grow", htmlFor: this.highIn.id }, H("span", { text: "Unload at GPU memory use (%)" }), this.highIn),
        H("label", { className: "hf-field hf-grow", htmlFor: this.lowIn.id }, H("span", { text: "Allow again below (%)" }), this.lowIn)),
      H("button", { className: "hf-btn", type: "button", text: "Save limits", onclick: () => this._limits() }));
    this.downSel = sel("hf-warm-down", [1, 2, 3, 4].map((n) => [String(n), String(n)]), () => this._save({ transfer_max_downloads: Number(this.downSel.value) }));
    this.upSel = sel("hf-warm-up", [1, 2, 3, 4].map((n) => [String(n), String(n)]), () => this._save({ transfer_max_uploads: Number(this.upSel.value) }));
    this.banner = H("div", { className: "hf-consent hf-tone-danger", hidden: true, attrs: { role: "alert" } });
    this.title = H("strong", { attrs: { role: "status", "aria-live": "polite" } });
    this.auto = H("div", { className: "hf-hint", attrs: { "aria-live": "polite" } });
    this.kv = H("dl", { className: "hf-kv" });
    this.lines = H("div", { className: "hf-hint" });
    this.btns = H("div", { className: "hf-row" });
    this.confirmBox = H("div", { className: "hf-consent", hidden: true, attrs: { role: "group", "aria-label": "Confirm shutting down the cloud machine" } });
    this.msg = H("div", { className: "hf-hint", attrs: { "aria-live": "polite" } });
    this.transfers = H("div", { className: "hf-list", attrs: { "aria-label": "Model transfers" } });
    this.cacheNote = H("div", { className: "hf-hint" });
    this.cacheBtns = H("div", { className: "hf-row" });
    this.cleanNote = H("div", { className: "hf-hint" });
    const field = (label, input, hint) => H("label", { className: "hf-field", htmlFor: input.id }, H("span", { text: label }), input, hint || null);
    const check = (input, text) => H("label", { className: "hf-field hf-check", htmlFor: input.id }, input, H("span", { text }));
    this.el = H("div", { className: "hf-section hf-warm", attrs: { "aria-label": "Warm cloud machine" } },
      this.banner, this.title,
      H("div", { className: "hf-row hf-warm-choice" },
        H("div", { className: "hf-grow" }, field("Warm mode", this.modeSel)),
        H("div", { className: "hf-grow" }, field("Warm duration (idle)", this.durSel))),
      this.customBox, this.modeHint,
      this.btns, this.confirmBox, this.msg, this.auto, this.kv, this.transfers,
      H("details", {}, H("summary", { text: "Costs and safety" }), this.lines),
      H("details", {}, H("summary", { text: "Housekeeping, transfers and model cache" }),
        check(this.clean, "Clean temporary files between workflows (never deletes model files)"),
        field("GPU memory between workflows", this.memSel, this.memHint), this.autoBox,
        H("div", { className: "hf-row" },
          H("div", { className: "hf-grow" }, field("Downloads at a time", this.downSel)),
          H("div", { className: "hf-grow" }, field("Uploads at a time", this.upSel))),
        this.cacheNote, this.cacheBtns, this.cleanNote));
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

  /** Mode and duration go through the warm route: Always Warm starts the machine as soon as it is chosen. */
  _mode(prefs) {
    const okText = prefs.warm_mode === "always" ? "Always Warm: starting the machine. It is billed while it is on, also when idle."
      : prefs.warm_mode === "smart" ? "Smart Warm: the machine starts when you render." : prefs.warm_mode === "off" ? "Warm mode off: nothing starts a machine by itself." : "Warm duration saved.";
    return this._act(() => this.api.setWarmMode(prefs), okText);
  }

  _duration() {
    if (this.durSel.value === "custom") {
      this.customOpen = true;
      const cur = this.view?.settings?.warm_idle_timeout_seconds;
      this.customIn.value = String(Math.round((typeof cur === "number" ? cur : 3600) / 60));
      this.render();
      try { this.customIn.focus(); } catch { /* not focusable in tests */ }
      return;
    }
    this.customOpen = false;
    this._mode({ warm_idle_timeout_seconds: Number(this.durSel.value) });
  }

  _custom() {
    const minutes = Math.round(Number(this.customIn.value));
    if (!Number.isFinite(minutes) || minutes < MIN_CUSTOM_MIN || minutes > MAX_CUSTOM_MIN) {
      this.msg.className = "hf-hint hf-tone-warning";
      this.msg.textContent = `Choose a warm duration between ${MIN_CUSTOM_MIN} minutes and 7 days (${MAX_CUSTOM_MIN} minutes).`;
      return;
    }
    this.customOpen = false;
    this._mode({ warm_idle_timeout_seconds: minutes * 60 });
  }

  _limits() {
    const high = Math.round(Number(this.highIn.value));
    const low = Math.round(Number(this.lowIn.value));
    if (!(high >= 10 && high <= 99 && low >= 10 && low < high)) {
      this.msg.className = "hf-hint hf-tone-warning";
      this.msg.textContent = "The upper limit must be 10–99 % and the lower limit below it.";
      return;
    }
    this._save({ vram_unload_high_pct: high, vram_unload_low_pct: low });
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
      this.msg.textContent = d?.warm?.message && /could not be started/.test(d.warm.message) ? d.warm.message : (okText || "");
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
    // choices (never overwrite a select the artist is using right now)
    const focused = this.doc.activeElement;
    if (focused !== this.modeSel) this.modeSel.value = m.mode;
    if (focused !== this.durSel) {
      clear(this.durSel);
      for (const [k, label] of m.durationOptions) this.durSel.append(H("option", { value: k, text: label }));
      this.durSel.value = this.customOpen ? "custom" : String(m.duration);
    }
    this.customBox.hidden = !this.customOpen;
    this.modeHint.textContent = m.modeHint;
    this.modeSel.disabled = this.durSel.disabled = this.busy;
    this.clean.checked = s.clean_between_renders !== false;
    if (focused !== this.memSel) this.memSel.value = m.memory;
    this.memHint.textContent = (MEMORY.find(([k]) => k === m.memory) || MEMORY[0])[2];
    this.autoBox.hidden = m.memory !== "auto";
    if (focused !== this.highIn) this.highIn.value = String(s.vram_unload_high_pct ?? 90);
    if (focused !== this.lowIn) this.lowIn.value = String(s.vram_unload_low_pct ?? 75);
    if (focused !== this.downSel) this.downSel.value = String(s.transfer_max_downloads ?? 2);
    if (focused !== this.upSel) this.upSel.value = String(s.transfer_max_uploads ?? 2);
    this.cleanNote.textContent = m.cleaning?.note || "";
    // state
    const banner = this.error || m.banner;
    this.banner.hidden = !banner;
    this.banner.textContent = banner || "";
    this.title.textContent = m.title;
    this.title.className = `hf-tone-${m.tone}`;
    this.auto.hidden = !m.autoText;
    this.auto.textContent = m.autoText || "";
    this.auto.className = `hf-hint hf-tone-${m.autoTone}`;
    clear(this.kv);
    for (const [k, v] of m.kv) this.kv.append(H("dt", { text: k }), H("dd", { text: v }));
    this.kv.hidden = !m.kv.length;
    clear(this.transfers);
    for (const t of m.transfers) {
      const bar = H("div", { className: `hf-bar${t.fraction == null ? " hf-indeterminate" : ""}`, attrs: { role: "progressbar",
        "aria-valuemin": 0, "aria-valuemax": 100, "aria-valuenow": t.fraction == null ? null : Math.round(t.fraction * 100), "aria-label": t.text } }, H("div", {}));
      if (t.fraction != null) bar.firstChild.style.width = `${Math.round(t.fraction * 100)}%`;
      this.transfers.append(H("div", { className: "hf-hint" }, H("div", { text: t.text }), bar));
    }
    this.transfers.hidden = !m.transfers.length;
    clear(this.lines);
    for (const line of m.lines) this.lines.append(H("div", { text: line }));
    this.cacheNote.textContent = m.cacheText;
    clear(this.cacheBtns);
    if (m.abandoned) {
      this.cacheBtns.append(H("button", { className: "hf-btn", type: "button", disabled: this.busy,
        text: `Remove ${m.abandoned} abandoned partial download(s)`,
        title: "Only unfinished downloads untouched for over 7 days. Complete model files and the cloud library are never deleted.",
        onclick: () => this._act(async () => { await this.api.cleanupWarmCache(); return this.api.getWarm(); }, "Abandoned partial downloads removed. Model files were not touched.") }));
    }
    // actions
    clear(this.btns);
    if (m.actions.start) {
      this.btns.append(H("button", { className: "hf-btn", type: "button", disabled: this.busy || !ctx.instanceType,
        text: ctx.instanceType ? `Start a warm ${ctx.instanceType} machine` : "Start a warm machine",
        onclick: () => this._act(() => this.api.startWarm(ctx.instanceType), "Starting the warm machine. It is billed until it shuts down.") }));
    }
    if (m.actions.keep) {
      this.btns.append(H("button", { className: "hf-btn", type: "button", disabled: this.busy, text: "Keep it warm",
        title: "Start the idle countdown again from now",
        onclick: () => this._act(() => this.api.keepWarm(), "The idle countdown starts again now.") }));
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

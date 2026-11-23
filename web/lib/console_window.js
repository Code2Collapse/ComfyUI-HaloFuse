// HaloFuse floating window chrome: move, resize (8 handles), near-full-screen, minimise to a pill, close.
// Appended to document.body (no ComfyUI internals). Geometry + view preferences persist through layout.js only.
// Side-effect-free ES module: DOM is created in the constructor.

import { bindH } from "./dom.js";
import { LAYOUT_KEY, clampGeometry, loadLayout, nearFullScreen, saveLayout } from "./layout.js";

const HANDLES = ["n", "s", "e", "w", "ne", "nw", "se", "sw"];
// The console and the floating panel window share one layer: the one opened or clicked last sits on top (the console
// used to open BEHIND the floating panel it was opened from). Two values only, so nothing climbs over ComfyUI's
// menus (1001) or dialogs.
const Z_TOP = "1000";
const Z_BELOW = "999";
const OPEN_WINDOWS = new Set();

export class FloatingWindow {
  /**
   * @param {{doc: Document, win: Window, storage: Storage|null, title: string, layoutKey?: string,
   *   initialGeometry?: (vp: {width: number, height: number}) => object, label?: string, minimizable?: boolean}} opts
   *   layoutKey / initialGeometry: a second window (the floating HaloFuse panel) keeps its own geometry.
   */
  constructor({ doc, win, storage, title, layoutKey = LAYOUT_KEY, initialGeometry = null, label = "console", minimizable = true }) {
    this.doc = doc;
    this.win = win;
    this.storage = storage;
    this.layoutKey = layoutKey;
    this.layout = loadLayout(storage, this._vp(), layoutKey, initialGeometry);
    this._listeners = new Set();
    this._raf = 0;
    this._saveTimer = 0;
    const H = bindH(doc);
    const titleId = `hf-console-title-${Math.random().toString(36).slice(2, 8)}`;

    this.btnMin = H("button", { className: "hf-btn hf-ghost hf-icon", type: "button", text: "–", title: "Minimise",
      attrs: { "aria-label": `Minimise ${label}` }, onclick: () => this.minimize() });
    this.btnMax = H("button", { className: "hf-btn hf-ghost hf-icon", type: "button", text: "□", title: "Near full screen",
      attrs: { "aria-label": "Near full screen", "aria-pressed": "false" }, onclick: () => this.toggleMax() });
    this.btnClose = H("button", { className: "hf-btn hf-ghost hf-icon", type: "button", text: "✕", title: "Close (the render keeps running)",
      attrs: { "aria-label": `Close ${label}` }, onclick: () => this.close() });
    this.titleEl = H("span", { className: "hf-win-title", id: titleId, tabIndex: -1 },
      H("span", { className: "hf-mark", attrs: { "aria-hidden": "true" } }), title);
    this.jobEl = H("span", { className: "hf-win-job" });
    this.statusSlot = H("span");
    if (!minimizable) this.btnMin.hidden = true;
    this.head = H("div", { className: "hf-win-head" }, this.titleEl, this.jobEl, this.statusSlot, this.btnMin, this.btnMax, this.btnClose);
    this.body = H("div", { className: "hf-tabpanel", style: { display: "flex" } });
    this.el = H("section", { className: "hf-console", hidden: true,
      attrs: { role: "dialog", "aria-modal": "false", "aria-labelledby": titleId } }, this.head, this.body);
    for (const dir of HANDLES) {
      this.el.append(H("div", { className: `hf-rs hf-rs-${dir}`, dataset: { dir }, attrs: { "aria-hidden": "true" } }));
    }
    this.pillText = H("span", { className: "hf-pill-text", text: title });
    this.pillDot = H("span", { className: "hf-dot hf-tone-neutral" });
    this.pill = H("button", { className: "hf-pill", type: "button", hidden: true, title: `Restore the HaloFuse ${label}`,
      attrs: { "aria-label": `Restore the HaloFuse ${label}` }, onclick: () => this.restore() }, this.pillDot, this.pillText);

    this._onPointerDown = (ev) => this._pointerDown(ev);
    this._onResize = () => this._scheduleFit();
    this.el.addEventListener("pointerdown", this._onPointerDown);
    this._onRaise = () => this.raise();
    this.el.addEventListener("pointerdown", this._onRaise, true);
    OPEN_WINDOWS.add(this);
    this.head.addEventListener("dblclick", (ev) => { if (!ev.target.closest("button")) this.toggleMax(); });
    win.addEventListener("resize", this._onResize);
    doc.body.append(this.el, this.pill);
  }

  get isOpen() { return !this.el.hidden; }
  get mode() { return this.layout.mode; }

  onChange(fn) { this._listeners.add(fn); return () => this._listeners.delete(fn); }

  /** Put this window above the other HaloFuse window. */
  raise() {
    for (const w of OPEN_WINDOWS) if (w !== this) w.el.style.zIndex = Z_BELOW;
    this.el.style.zIndex = Z_TOP;
  }

  /** Show the window in its remembered mode (a remembered "min" opens normally). */
  open() {
    if (this.layout.mode === "min") this.layout.mode = "normal";
    this.layout.open = true;
    this.el.hidden = false;
    this.pill.hidden = true;
    this.raise();
    this._apply();
    this._persist();
    this.titleEl.focus({ preventScroll: true });
    this._emit();
  }

  close() {
    this.el.hidden = true;
    this.pill.hidden = true;
    if (this.layout.open) { this.layout.open = false; this._persist(); }
    this._emit();
  }

  minimize() {
    this.layout.mode = "min";
    this.layout.open = false;
    this.el.hidden = true;
    this.pill.hidden = false;
    this._persist();
    this.pill.focus({ preventScroll: true });
    this._emit();
  }

  restore() {
    this.layout.mode = this._modeBeforeMin === "max" ? "max" : "normal";
    this.layout.open = true;
    this.el.hidden = false;
    this.pill.hidden = true;
    this.raise();
    this._apply();
    this._persist();
    this.titleEl.focus({ preventScroll: true });
    this._emit();
  }

  toggleMax() {
    this.layout.mode = this.layout.mode === "max" ? "normal" : "max";
    this._modeBeforeMin = this.layout.mode;
    this._apply();
    this._persist();
    this._emit();
  }

  setJobLabel(text) { this.jobEl.textContent = text || ""; this.jobEl.title = text || ""; }

  setPill(text, tone = "neutral") {
    this.pillText.textContent = text;
    this.pillDot.className = `hf-dot hf-tone-${tone}`;
  }

  /** Persisted view preferences (tab, timestamps, sources). */
  prefs() { return this.layout; }

  setPrefs(partial) {
    Object.assign(this.layout, partial);
    this._persist();
  }

  destroy() {
    this.win.removeEventListener("resize", this._onResize);
    this.el.removeEventListener("pointerdown", this._onPointerDown);
    this.el.removeEventListener("pointerdown", this._onRaise, true);
    OPEN_WINDOWS.delete(this);
    if (this._raf) this.win.cancelAnimationFrame(this._raf);
    this.el.remove();
    this.pill.remove();
  }

  _emit() { for (const fn of this._listeners) fn(this); }

  _vp() { return { width: this.win.innerWidth, height: this.win.innerHeight }; }

  _apply() {
    const vp = this._vp();
    const max = this.layout.mode === "max";
    if (max) this._modeBeforeMin = "max";
    else if (this.layout.mode === "normal") this._modeBeforeMin = "normal";
    const g = max ? nearFullScreen(vp) : (this.layout.geometry = clampGeometry(this.layout.geometry, vp));
    const s = this.el.style;
    s.left = `${g.x}px`; s.top = `${g.y}px`; s.width = `${g.w}px`; s.height = `${g.h}px`;
    this.el.classList.toggle("hf-max", max);
    this.btnMax.setAttribute("aria-pressed", String(max));
    this.btnMax.title = max ? "Restore size" : "Near full screen";
    this.btnMax.textContent = max ? "❐" : "□";
  }

  _scheduleFit() {
    if (this._raf) return;
    this._raf = this.win.requestAnimationFrame(() => {
      this._raf = 0;
      if (this.isOpen) this._apply();
    });
  }

  _persist() {
    if (this._saveTimer) this.win.clearTimeout(this._saveTimer);
    this._saveTimer = this.win.setTimeout(() => {
      this._saveTimer = 0;
      saveLayout(this.storage, this.layout, this._vp(), this.layoutKey);
    }, 250);
  }

  _pointerDown(ev) {
    if (ev.button !== 0) return;
    const handle = ev.target.closest(".hf-rs");
    const onHead = !handle && this.head.contains(ev.target) && !ev.target.closest("button, input, select, a");
    if (!handle && !onHead) return;
    if (this.layout.mode === "max") {
      if (handle) return;
      // dragging a maximised window restores it under the pointer
      this.layout.mode = "normal";
      const g = this.layout.geometry;
      g.x = Math.round(ev.clientX - g.w / 2);
      g.y = Math.round(ev.clientY - 16);
      this._apply();
    }
    ev.preventDefault();
    const start = { ...this.layout.geometry };
    const sx = ev.clientX;
    const sy = ev.clientY;
    const dir = handle ? handle.dataset.dir : null;
    const target = ev.target;
    target.setPointerCapture?.(ev.pointerId);
    this.el.classList.add(dir ? "hf-resizing" : "hf-dragging");
    let frame = 0;
    let next = start;
    // west / north edges keep the opposite edge fixed when the minimum size is reached
    const fit = (n) => {
      const vp = this._vp();
      const c = clampGeometry(n, vp);
      if (dir && dir.includes("w")) c.x = Math.min(c.x, start.x + start.w - c.w);
      if (dir && dir.includes("n")) c.y = Math.min(c.y, start.y + start.h - c.h);
      return clampGeometry(c, vp);
    };
    const move = (e) => {
      const dx = e.clientX - sx;
      const dy = e.clientY - sy;
      next = { ...start };
      if (!dir) { next.x = start.x + dx; next.y = start.y + dy; } else {
        if (dir.includes("e")) next.w = start.w + dx;
        if (dir.includes("s")) next.h = start.h + dy;
        if (dir.includes("w")) { next.w = start.w - dx; next.x = start.x + dx; }
        if (dir.includes("n")) { next.h = start.h - dy; next.y = start.y + dy; }
      }
      if (!frame) {
        frame = this.win.requestAnimationFrame(() => {
          frame = 0;
          this.layout.geometry = fit(next);
          const s = this.el.style;
          s.left = `${this.layout.geometry.x}px`; s.top = `${this.layout.geometry.y}px`;
          s.width = `${this.layout.geometry.w}px`; s.height = `${this.layout.geometry.h}px`;
        });
      }
    };
    const up = () => {
      target.removeEventListener("pointermove", move);
      target.removeEventListener("pointerup", up);
      target.removeEventListener("pointercancel", up);
      this.el.classList.remove("hf-resizing", "hf-dragging");
      if (frame) { this.win.cancelAnimationFrame(frame); frame = 0; }
      this.layout.geometry = fit(next);
      this._apply();
      this._persist();
      this._emit();
    };
    target.addEventListener("pointermove", move);
    target.addEventListener("pointerup", up);
    target.addEventListener("pointercancel", up);
  }
}

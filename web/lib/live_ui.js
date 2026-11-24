// HaloFuse Live ComfyUI (L9.345): the "Open Live ComfyUI" button and its status block (state, GPU, runtime, cost,
// Stop). Side-effect-free ES module: nothing runs at import; polling runs only while a session is active.
import { bindH } from "./dom.js";

export const LIVE_POLL_MS = 5000;

export function liveText(v) {
  if (!v || v.state === "off") return "";
  const parts = [{ starting: "Starting the live ComfyUI…", running: "Live ComfyUI is running", stopping: "Stopping…" }[v.state] || v.state];
  if (v.gpu_name || v.instance_type) parts.push(v.gpu_name || v.instance_type);
  if (v.runtime_s != null) parts.push(`${Math.floor(v.runtime_s / 60)} min`);
  if (v.cost_usd != null) parts.push(`about $${Number(v.cost_usd).toFixed(2)}`);
  if (v.state === "running" && v.idle_warning) parts.push(`idle: stops in ${Math.ceil((v.idle_remaining_s || 0) / 60)} min`);
  return parts.join(" · ");
}

/** Exactly what stops "Open Live ComfyUI", in words, or "" when it can be pressed. */
export function openBlocker(v, instanceType) {
  if (!v) return "HaloFuse could not reach the Live ComfyUI service. Reload the page; if it stays, restart ComfyUI.";
  if (!v.available) return v.reason || "Live ComfyUI is off. Turn on the Live ComfyUI switch under Advanced.";
  if (!instanceType) return "Choose a GPU first.";
  return "";
}

export const HOST_POLL_MS = 2000;

/** The host switch's status line: every state names the next action (owner rule: no dead ends). */
export function hostText(h) {
  if (!h) return "HaloFuse could not read the Live ComfyUI switch. Reload the page and try again.";
  if (h.state === "ready") return `Live ComfyUI is ready at ${h.address}. Press Open Live ComfyUI.`;
  if (h.state === "error") return `${h.message || "Live ComfyUI could not start."} Press Try again, or turn the switch off.`;
  return h.message || "";
}

/** "Live ComfyUI" switch (Advanced): starts the relay gateway + secure address on this computer. */
export class LiveHostSection {
  constructor({ doc, win, api, onChange }) {
    Object.assign(this, { doc, win, api, onChange });
    const H = bindH(doc);
    this.host = null;
    this._timer = null;
    this.toggle = H("input", { type: "checkbox", id: "hf-live-host", onchange: () => this.setOn(this.toggle.checked) });
    this.status = H("div", { className: "hf-hint", attrs: { role: "status", "aria-live": "polite" } });
    this.downloadBtn = H("button", { className: "hf-btn", type: "button", text: "Download cloudflared",
      title: "Downloads the official cloudflared program (checked against its published checksum) into HaloFuse's tools folder.",
      onclick: () => this.download() });
    this.retryBtn = H("button", { className: "hf-btn", type: "button", text: "Try again", onclick: () => this.setOn(true) });
    this.el = H("div", {},
      H("label", { className: "hf-field hf-check", htmlFor: "hf-live-host" }, this.toggle,
        H("span", { text: "Live ComfyUI (runs a secure relay on this computer so a cloud ComfyUI can open in your browser)" })),
      this.status, H("div", { className: "hf-row" }, this.downloadBtn, this.retryBtn));
    this._show(null, true);
  }

  _show(h, quiet = false) {
    this.host = h;
    const st = h?.state || "off";
    this.toggle.checked = !!h && st !== "off";
    this.toggle.disabled = st === "downloading";
    this.status.textContent = quiet ? "" : hostText(h);
    this.downloadBtn.hidden = st !== "needs_cloudflared";
    this.retryBtn.hidden = !(st === "error" || (!h && !quiet));
    clearTimeout(this._timer);
    this._timer = ["starting", "downloading"].includes(st) ? setTimeout(() => this.refresh(), HOST_POLL_MS) : null;
    if (!quiet) this.onChange?.(h);
  }

  async refresh() {
    try { this._show((await this.api.getLiveRelay()).host); } catch { this._show(null); }
  }

  async setOn(on) {
    this.status.textContent = on ? "Starting Live ComfyUI on this computer…" : "Turning Live ComfyUI off…";
    try { this._show((await this.api.setLiveRelay(on)).host); } catch (e) {
      this._show(null); this.status.textContent = `${e?.message || "The switch did not respond."} Press Try again.`;
    }
  }

  async download() {
    this._show({ ...(this.host || {}), state: "downloading", message: "Downloading cloudflared…" });
    try { this._show((await this.api.downloadCloudflared()).host); } catch (e) {
      this._show({ state: "error", message: e?.message || "cloudflared could not be downloaded." });
    }
  }

  destroy() { clearTimeout(this._timer); this._timer = null; }
}

export class LiveSection {
  constructor({ doc, win, api, context }) {
    Object.assign(this, { doc, win, api, context });
    const H = bindH(doc);
    this.view = null;
    this._timer = null;
    this.button = H("button", { className: "hf-btn", type: "button", text: "Open Live ComfyUI", onclick: () => this.open() });
    this.status = H("span", { className: "hf-grow", attrs: { role: "status", "aria-live": "polite" } });
    this.stopBtn = H("button", { className: "hf-btn", type: "button", text: "Stop", onclick: () => this.stop() });
    this.el = H("div", { className: "hf-row hf-hint" }, this.status, this.stopBtn);
    this.el.hidden = true;
    this.refresh();
  }

  _show(v) {
    this.view = v;
    const active = !!v && ["starting", "running", "stopping"].includes(v.state);
    this.relabel();
    this.el.hidden = !active;
    this.status.textContent = liveText(v);
    this.stopBtn.disabled = !active || v.state === "stopping";
    clearTimeout(this._timer);
    this._timer = active ? setTimeout(() => this.refresh(), LIVE_POLL_MS) : null;
  }

  /** Button state from the last view + the current GPU choice (the panel calls it when the GPU changes). */
  relabel() {
    const v = this.view;
    const active = !!v && ["starting", "running", "stopping"].includes(v.state);
    const missing = openBlocker(v, this.context?.().instanceType);
    this.button.disabled = !!missing && !active;
    this.button.title = missing && !active ? missing
      : "Opens the real ComfyUI on a cloud GPU in a new tab. It is billed while it runs.";
    this.button.textContent = v && v.state === "starting" ? "Starting…" : "Open Live ComfyUI";
  }

  async refresh() {
    try { this._show((await this.api.getLive()).live); } catch { this._show(null); }
  }

  async open() {
    // open the tab inside the click (popup blockers), then point it at the one-shot link once ready
    const tab = this.win?.open ? this.win.open("about:blank", "_blank") : null;
    try {
      let v = (await this.api.startLive(this.context().instanceType)).live;
      for (let i = 0; v.state === "starting" && i < 180; i++) {
        this._show(v);
        await new Promise((r) => setTimeout(r, LIVE_POLL_MS));
        v = (await this.api.getLive()).live;
      }
      this._show(v);
      if (v.state !== "running") throw new Error("Live ComfyUI did not become ready.");
      const { url } = await this.api.openLive();
      if (tab) { tab.opener = null; tab.location.href = url; } else this.win?.open?.(url, "_blank", "noopener");
    } catch (e) {
      if (tab) tab.close();
      this.status.textContent = e?.message || "Live ComfyUI could not be opened.";
      this.el.hidden = false;
    }
  }

  async stop() {
    try { this._show((await this.api.stopLive()).live); } catch (e) { this.status.textContent = e?.message || "Stop failed."; }
  }

  destroy() { clearTimeout(this._timer); this._timer = null; }
}

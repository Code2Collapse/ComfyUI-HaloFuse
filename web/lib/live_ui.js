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
    this.button.disabled = !v || !v.available;
    this.button.title = v && !v.available ? (v.reason || "Live ComfyUI is off.")
      : "Opens the real ComfyUI on a cloud GPU in a new tab. It is billed while it runs.";
    this.button.textContent = v && v.state === "starting" ? "Starting…" : "Open Live ComfyUI";
    this.el.hidden = !active;
    this.status.textContent = liveText(v);
    this.stopBtn.disabled = !active || v.state === "stopping";
    clearTimeout(this._timer);
    this._timer = active ? setTimeout(() => this.refresh(), LIVE_POLL_MS) : null;
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

import { app } from "../../scripts/app.js";
import { api as comfyApi } from "../../scripts/api.js";
import { createApi, previewPath, viewPath } from "./lib/api.js";
import { ensureStyles } from "./lib/dom.js";
import { HF_CSS } from "./lib/styles.js";
import { JobSession } from "./lib/session.js";
import { Controller } from "./lib/controller.js";
import { Panel } from "./lib/panel_ui.js";
import { HaloFuseConsole } from "./lib/console_ui.js";
import { FloatingWindow } from "./lib/console_window.js";
import { PANEL_LAYOUT_KEY, PANEL_SETTING, clampLauncher, loadLauncher, normalizeMode, panelGeometry, saveLauncher, wants }
  from "./lib/panel_place.js";

// HaloFuse extension entry. The compact panel lives in a ComfyUI sidebar tab
// (extensionManager.registerSidebarTab); the large console is a floating window on document.body that keeps
// running when the panel is closed. Only official extension APIs are used: app.registerExtension,
// app.extensionManager (sidebar tab, toast), api.fetchApi / api.apiURL / api.addEventListener,
// app.graphToPrompt, app.graph / app.canvas for seed advance and "show node on canvas".
// Extension id "HaloFuse.Bridge" (renamed in 0.4; no browser storage key ever used the old id).

function safeStorage() {
  try { return window.localStorage; } catch { return null; } // layout preferences only (lib/layout.js)
}

function toast(severity, summary, detail) {
  try { app.extensionManager?.toast?.add({ severity, summary, detail, life: 6000 }); } catch { /* optional */ }
}

/** Centre the canvas on a node from a runner node id; subgraph ids look like "wrapper:inner". */
function focusNode(nodeId) {
  const graph = app.canvas?.graph || app.graph;
  const ids = String(nodeId).split(":");
  let node = null;
  for (const g of [graph, app.graph]) {
    if (!g?.getNodeById) continue;
    node = g.getNodeById(Number(ids[0])) || g.getNodeById(ids[0]);
    if (node) break;
  }
  if (!node) { toast("info", "HaloFuse", `Node ${nodeId} is not in the open graph.`); return; }
  app.canvas?.centerOnNode?.(node);
  app.canvas?.selectNode?.(node, false);
  app.canvas?.setDirty?.(true, true);
}

let instance = null;

function start() {
  if (instance) return instance;
  ensureStyles(document, HF_CSS);
  const hfApi = createApi((path, init) => comfyApi.fetchApi(path, init));
  const session = new JobSession({ api: hfApi });
  const controller = new Controller({
    api: hfApi,
    session,
    comfy: {
      graphToPrompt: () => app.graphToPrompt(),
      graphNodes: () => app.graph?._nodes || [],
      toast,
    },
  });
  const viewUrl = (ref) => comfyApi.apiURL(viewPath(ref));
  const consoleWin = new HaloFuseConsole({
    doc: document, win: window, storage: safeStorage(), session,
    viewUrl,
    previewUrl: (jobId, name) => comfyApi.apiURL(previewPath(jobId, name)),
    onFocusNode: focusNode,
    onRetryDelivery: (id) => controller.retryDelivery(id),
  });
  const panel = new Panel({
    doc: document, win: window, api: hfApi, controller, session,
    openConsole: (tab) => consoleWin.open(tab),
    viewUrl,
  });
  // Push hint from the bridge (C-4 §0.8) and websocket reconnects after a ComfyUI restart: poll right away.
  comfyApi.addEventListener("halofuse.job", (ev) => { if (ev?.detail?.job_id === session.id) session.poke(); });
  comfyApi.addEventListener("reconnected", () => { session.poke(); if (!session.id) controller.reattach(); });
  // Toast on terminal transitions, so a finished render is noticed with the panel closed.
  let lastStatus = null;
  let lastId = null;
  session.subscribe((what, s) => {
    if (what !== "job" || !s.job) return;
    const st = s.job.status;
    const sameJob = lastId === s.id;
    lastId = s.id;
    // toast only on a transition observed live, never when opening an already-finished job
    if (sameJob && lastStatus && st !== lastStatus && s.job.terminal) {
      const ok = st === "succeeded" || st === "completed";
      toast(ok ? "success" : st === "cancelled" ? "warn" : "error", "HaloFuse",
        ok ? "Render completed. Outputs are saved in the HaloFuse output folder." : `Render ${st.replace("_", " ")}.`);
    }
    lastStatus = st;
  });
  controller.init();
  instance = { hfApi, session, controller, consoleWin, panel, viewUrl };
  return instance;
}

// ── Panel location (owner 2026-11-21): Sidebar (default) / Floating window / Both ───────────────────────────────
// Floating = a small draggable launcher button that opens the same panel in its own movable, resizable window (like
// the old floating panel). The window keeps its own geometry key; the launcher position is a per-browser convenience.
let panelMode = "sidebar";
let sidebarOn = false;
let launcher = null;
let floatWin = null;

function sidebarTabSpec(inst) {
  return {
    id: "halofuse",
    title: "HaloFuse",
    tooltip: "HaloFuse cloud rendering",
    icon: "pi pi-bolt",
    type: "custom",
    // The frontend's tab container has no height of its own (measured, fe 1.53): .hf-root then grew to its content
    // and, being an overscroll-contained scroller with nothing to scroll, swallowed the wheel - the panel could not
    // scroll at all. Filling the sidebar makes .hf-root the scroller.
    render: (container) => {
      container.style.height = "100%";
      container.style.minHeight = "0";
      inst.panel.mount(container);
    },
    destroy: () => inst.panel.unmount(),
  };
}

function panelWindow(inst) {
  if (floatWin) return floatWin;
  floatWin = new FloatingWindow({ doc: document, win: window, storage: safeStorage(), title: "HaloFuse",
    layoutKey: PANEL_LAYOUT_KEY, initialGeometry: panelGeometry, label: "panel", minimizable: false });
  floatWin.el.classList.add("hf-panel-window");
  // A second Panel on the same controller + session: both views stay in step (the sidebar one may be open too).
  const p = new Panel({ doc: document, win: window, api: inst.hfApi, controller: inst.controller, session: inst.session,
    openConsole: (tab) => inst.consoleWin.open(tab), viewUrl: inst.viewUrl });
  p.mount(floatWin.body);
  const showVersion = () => floatWin.setJobLabel(p.version.textContent); // the inner heading is hidden in the window
  inst.controller.subscribe(showVersion);
  showVersion();
  floatWin.onChange((w) => launcher?.el.setAttribute("aria-pressed", String(w.isOpen)));
  return floatWin;
}

function togglePanelWindow() {
  const w = panelWindow(start());
  if (w.isOpen) w.close(); else w.open();
}

function makeLauncher(inst) {
  const vp = () => ({ w: window.innerWidth, h: window.innerHeight });
  const b = document.createElement("button");
  b.type = "button";
  b.className = "hf-launcher";
  b.title = "HaloFuse (drag to move)";
  b.setAttribute("aria-label", "Show or hide the HaloFuse panel");
  b.setAttribute("aria-pressed", String(!!floatWin?.isOpen));
  const icon = document.createElement("span");
  icon.className = "pi pi-bolt";
  icon.setAttribute("aria-hidden", "true");
  const dot = document.createElement("span");
  dot.className = "hf-launcher-dot";
  dot.setAttribute("aria-hidden", "true");
  b.append(icon, dot);
  let pos = loadLauncher(safeStorage(), vp());
  const place = () => { pos = clampLauncher(pos, vp()); b.style.left = `${pos.x}px`; b.style.top = `${pos.y}px`; };
  let drag = null;
  let swallowClick = false;
  b.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    drag = { sx: e.clientX, sy: e.clientY, x: pos.x, y: pos.y, moved: false };
    b.setPointerCapture?.(e.pointerId);
  });
  b.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const dx = e.clientX - drag.sx;
    const dy = e.clientY - drag.sy;
    if (!drag.moved && Math.hypot(dx, dy) < 5) return; // a click, not a drag
    drag.moved = true;
    pos = { x: drag.x + dx, y: drag.y + dy };
    place();
  });
  const endDrag = () => {
    if (!drag) return;
    if (drag.moved) { swallowClick = true; saveLauncher(safeStorage(), pos, vp()); }
    drag = null;
  };
  b.addEventListener("pointerup", endDrag);
  b.addEventListener("pointercancel", endDrag);
  b.addEventListener("click", () => {
    if (swallowClick) { swallowClick = false; return; }
    togglePanelWindow();
  });
  const onResize = () => place();
  window.addEventListener("resize", onResize);
  // a quiet status dot while a render runs (the panel itself has the details)
  const unsub = inst.session.subscribe((what, s) => {
    if (what === "job" || what === "attach" || what === "detach") b.dataset.busy = s?.job && !s.job.terminal ? "1" : "";
  });
  document.body.append(b);
  place();
  return { el: b, remove: () => { window.removeEventListener("resize", onResize); unsub?.(); b.remove(); } };
}

function applyPanelMode(value) {
  panelMode = normalizeMode(value);
  const inst = instance;
  if (!inst) return; // setup() applies the stored mode once the extension is running
  const want = wants(panelMode);
  const em = app.extensionManager;
  if (want.sidebar && !sidebarOn) {
    em.registerSidebarTab(sidebarTabSpec(inst));
    sidebarOn = true;
  } else if (!want.sidebar && sidebarOn && typeof em.unregisterSidebarTab === "function") {
    em.unregisterSidebarTab("halofuse"); // a frontend without unregisterSidebarTab keeps the tab (= Both)
    sidebarOn = false;
  }
  if (want.floating && !launcher) launcher = makeLauncher(inst);
  else if (!want.floating && launcher) { launcher.remove(); launcher = null; floatWin?.close(); }
}

app.registerExtension({
  name: "HaloFuse.Bridge",
  commands: [
    { id: "HaloFuse.OpenConsole", label: "HaloFuse: Open console", icon: "pi pi-align-left", function: () => start().consoleWin.open() },
    { id: "HaloFuse.TogglePanelWindow", label: "HaloFuse: Show or hide the floating panel", icon: "pi pi-bolt",
      function: () => togglePanelWindow() },
  ],
  settings: [
    {
      id: PANEL_SETTING,
      category: ["HaloFuse", "Panel", "Location"],
      name: "Panel location",
      tooltip: "Sidebar: the HaloFuse tab in the left bar. Floating window: a small bolt button you can drag anywhere; "
        + "it opens the panel in a window you can move and resize. Both: the tab and the button.",
      type: "combo",
      options: [
        { text: "Sidebar", value: "sidebar" },
        { text: "Floating window", value: "floating" },
        { text: "Both", value: "both" },
      ],
      defaultValue: "sidebar",
      onChange: (v) => applyPanelMode(v),
    },
  ],
  async setup() {
    start();
    let stored = panelMode;
    try { stored = app.extensionManager.setting?.get?.(PANEL_SETTING) ?? panelMode; } catch { /* default */ }
    applyPanelMode(stored);
  },
});

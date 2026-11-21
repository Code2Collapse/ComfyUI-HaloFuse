import { app } from "../../scripts/app.js";
import { api as comfyApi } from "../../scripts/api.js";
import { createApi, previewPath, viewPath } from "./lib/api.js";
import { ensureStyles } from "./lib/dom.js";
import { HF_CSS } from "./lib/styles.js";
import { JobSession } from "./lib/session.js";
import { Controller } from "./lib/controller.js";
import { Panel } from "./lib/panel_ui.js";
import { HaloFuseConsole } from "./lib/console_ui.js";

// HaloFuse extension entry. The compact panel lives in a ComfyUI sidebar tab
// (extensionManager.registerSidebarTab); the large console is a floating window on document.body that keeps
// running when the panel is closed. Only official extension APIs are used: app.registerExtension,
// app.extensionManager (sidebar tab, toast), api.fetchApi / api.apiURL / api.addEventListener,
// app.graphToPrompt, app.graph / app.canvas for seed advance and "show node on canvas".
// The extension id stays "SparkFuse.Bridge" so existing installs and settings keep working.

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
  instance = { hfApi, session, controller, consoleWin, panel };
  return instance;
}

app.registerExtension({
  name: "SparkFuse.Bridge",
  commands: [
    { id: "HaloFuse.OpenConsole", label: "HaloFuse: Open console", icon: "pi pi-align-left", function: () => start().consoleWin.open() },
  ],
  async setup() {
    const inst = start();
    app.extensionManager.registerSidebarTab({
      id: "halofuse",
      title: "HaloFuse",
      tooltip: "HaloFuse cloud rendering",
      icon: "pi pi-bolt",
      type: "custom",
      render: (container) => inst.panel.mount(container),
      destroy: () => inst.panel.unmount(),
    });
  },
});

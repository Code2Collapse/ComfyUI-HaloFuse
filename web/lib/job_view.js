// HaloFuse job presentation logic (C-4 §2). Pure functions, side-effect-free ES module.

import { fmtBytes, fmtDuration, fmtPercent, fmtUsd } from "./format.js";
import { toNumber } from "./api.js";

export const STATUSES = Object.freeze({
  queued: { label: "Queued", tone: "neutral", terminal: false },
  provisioning: { label: "Provisioning a GPU", tone: "active", terminal: false },
  starting: { label: "Starting ComfyUI", tone: "active", terminal: false },
  running: { label: "Rendering", tone: "active", terminal: false },
  preview_available: { label: "Preview available", tone: "active", terminal: false },
  cancelling: { label: "Cancelling", tone: "warning", terminal: false },
  cancelled: { label: "Cancelled", tone: "warning", terminal: true },
  succeeded: { label: "Completed", tone: "success", terminal: true },
  failed: { label: "Failed", tone: "danger", terminal: true },
  timed_out: { label: "Timed out", tone: "danger", terminal: true },
  downloading: { label: "Downloading outputs", tone: "active", terminal: false },
  finalizing: { label: "Finalizing", tone: "active", terminal: false },
  paused: { label: "Paused", tone: "neutral", terminal: false },
});

const LEGACY = { completed: "succeeded", preparing: "provisioning", pending: "queued", submitted: "queued" };

export function statusInfo(status) {
  const key = LEGACY[status] || status;
  return STATUSES[key] ? { ...STATUSES[key] } : { label: String(status || "Unknown"), tone: "neutral", terminal: false };
}

export function isTerminal(statusOrJob) {
  if (statusOrJob && typeof statusOrJob === "object") {
    if (statusOrJob.terminal === true) return true;
    return statusInfo(statusOrJob.status).terminal;
  }
  return statusInfo(statusOrJob).terminal;
}

const CANCEL = {
  requested: { label: "Stop requested", tone: "active" },
  accepted: { label: "Stop accepted by the cloud", tone: "active" },
  confirmed: { label: "Stopped — cloud job ended", tone: "warning" },
  failed: { label: "Stop failed — the cloud job may still be running", tone: "danger" },
  already_completed: { label: "Already finished before the stop", tone: "success" },
};

/** C-2 §5.2 names carry a "cancel_" prefix; v0.1 names did not. Both are accepted. */
function cancelState(cancel) {
  const st = cancel?.state;
  if (typeof st !== "string") return null;
  return st.startsWith("cancel_") ? st.slice(7) : st;
}

export function cancelInfo(cancel) {
  const state = cancelState(cancel);
  if (!state || !CANCEL[state]) return null;
  const base = CANCEL[state];
  const suffix = cancel.mode === "after_current" && (state === "requested" || state === "accepted")
    ? " (after the current item)" : "";
  return { label: base.label + suffix, tone: base.tone, detail: cancel.note || "" };
}

/** Which stop buttons are meaningful for a job or queue object. */
export function cancelActions(job) {
  const none = { now: false, afterCurrent: false };
  if (!job || isTerminal(job)) return none;
  const st = cancelState(job.cancel) || "none";
  if (st === "confirmed" || st === "already_completed") return none;
  const pending = st === "requested" || st === "accepted";
  const multi = Array.isArray(job.items) || (Number(job.render_repeats) > 1);
  if (pending) return { now: job.cancel.mode === "after_current", afterCurrent: false };
  return { now: true, afterCurrent: multi };
}

// C-3 §3.1 runner phases + bridge/derived phases (node stages from run_state.js)
const PHASES = {
  provisioning: "Provisioning a GPU",
  image_pull: "Preparing the runtime image",
  starting: "Starting ComfyUI",
  boot: "Runner start",
  stage_inputs: "Staging inputs",
  user_nodes: "Installing your custom nodes",
  model_paths: "Resolving model paths",
  comfy_start: "Starting ComfyUI",
  prefetch_wait: "Waiting for model prefetch",
  execute: "Executing workflow",
  collect_outputs: "Collecting outputs",
  recovery: "Recovering",
  shutdown: "Shutting down",
  encoding: "Encoding",
  running: "Running",
  node_import: "Importing nodes",
  prefetch: "Prefetching models",
  model_load: "Loading models",
  sampling: "Sampling",
  vae_decode: "VAE decode",
  outputs: "Writing outputs",
  download: "Downloading outputs",
  finalizing: "Finalizing",
};

export function phaseLabel(name) {
  if (!name) return "";
  if (PHASES[name]) return PHASES[name];
  const s = String(name).replace(/_/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

export function progressText(progress, status) {
  if (!progress || !progress.phase) return status ? statusInfo(status).label : "";
  const parts = [phaseLabel(progress.phase)];
  if (Number.isFinite(progress.step) && Number.isFinite(progress.steps)) parts.push(`step ${progress.step} of ${progress.steps}`);
  const pct = fmtPercent(progress.fraction);
  if (pct) parts.push(pct);
  if (typeof progress.eta_s === "number" && Number.isFinite(progress.eta_s)) parts.push(`about ${fmtDuration(progress.eta_s)} left`);
  return parts.join(" · ");
}

export function phaseRows(phases, now) {
  return (phases || []).filter((p) => p && Number.isFinite(p.started_at)).map((p) => {
    const running = !Number.isFinite(p.ended_at);
    return {
      name: p.name, label: phaseLabel(p.name), startedAt: p.started_at, endedAt: running ? null : p.ended_at,
      duration: (running ? now : p.ended_at) - p.started_at, running,
    };
  });
}

export function nodeRows(nodes, now) {
  return (nodes || []).filter((n) => n && n.node_id != null).map((n) => {
    const running = Number.isFinite(n.started_at) && !Number.isFinite(n.ended_at);
    const duration = Number.isFinite(n.started_at) ? (running ? now : n.ended_at) - n.started_at : null;
    return { nodeId: String(n.node_id), classType: n.class_type || "", duration, cached: !!n.cached, running };
  });
}

function folderText(d) {
  return d.folder?.subfolder ? `output/${d.folder.subfolder}` : "the ComfyUI output folder";
}

export function deliveryText(d) {
  if (!d) return null;
  switch (d.state) {
    case "downloading":
      return `Downloading ${d.files_done ?? 0} of ${d.files_total ?? "?"} files · ${fmtBytes(d.bytes_done)} of ${fmtBytes(d.bytes_total)}`;
    case "complete":
      return `All ${d.files_total ?? 0} files saved to ${folderText(d)}`;
    case "partial":
      return `Partial: ${d.files_done ?? 0} of ${d.files_total ?? "?"} files saved to ${folderText(d)}`;
    case "failed":
      return `Delivery failed: ${d.errors?.[0]?.message || "see the console for details."}`;
    default:
      return "Waiting for the render to finish";
  }
}

export function costText(cost) {
  if (!cost) return null;
  const rateNum = toNumber(cost.rate_per_hour_usd);
  const rate = rateNum != null ? `${fmtUsd(rateNum)}/h` : null;
  const actual = toNumber(cost.actual_usd ?? (cost.basis === "actual" ? cost.amount_usd : null));
  if (actual != null) return `Actual: ${fmtUsd(actual)}`;
  const estimate = toNumber(cost.estimate_usd ?? (cost.basis !== "actual" ? cost.amount_usd : null));
  if (estimate != null) return `Estimate: ${fmtUsd(estimate)}${rate ? ` at ${rate}` : ""}`;
  return rate ? `Rate: ${rate}` : null;
}

const num = (v) => typeof v === "number" && Number.isFinite(v);

export function telemetryRows(telemetry) {
  const latest = telemetry?.latest || {};
  const peak = telemetry?.peak || {};
  const pct = (v) => (num(v) ? `${Math.round(v)}%` : "—");
  const gb = (v) => (num(v) ? `${v.toFixed(1)} GB` : "—");
  const pair = (u, t) => (num(u) ? (num(t) ? `${u.toFixed(1)} / ${t.toFixed(1)} GB` : `${u.toFixed(1)} GB`) : "—");
  const rows = [
    ["GPU", latest.gpu_util, peak.gpu_util, pct(latest.gpu_util), pct(peak.gpu_util)],
    ["VRAM", latest.vram_used_gb, peak.vram_used_gb, pair(latest.vram_used_gb, latest.vram_total_gb), gb(peak.vram_used_gb)],
    ["RAM", latest.ram_used_gb, peak.ram_used_gb, pair(latest.ram_used_gb, latest.ram_total_gb), gb(peak.ram_used_gb)],
    ["CPU", latest.cpu_util, peak.cpu_util, pct(latest.cpu_util), pct(peak.cpu_util)],
    ["Free disk", latest.disk_free_gb, null, gb(latest.disk_free_gb), "—"],
  ];
  return rows.filter(([, l, p]) => num(l) || num(p)).map(([label, , , l, p]) => ({ label, latest: l, peak: p }));
}

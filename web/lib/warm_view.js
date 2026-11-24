// HaloFuse warm cloud machine: the panel's view model (owner 2026-11-23; modes, warm duration, cost and cache
// 2026-11-24, L9.340). Pure functions, no DOM.
// The bridge's GET /halofuse/warm view is the source; the running cost and the remaining warm time are recomputed from
// the clock between polls (labelled estimates). A state the bridge cannot confirm is a danger banner, never a quiet
// line. Nothing here claims that models stay loaded in GPU memory between separate renders: they do not (each render
// starts ComfyUI again on the warm machine; only a queue batch keeps them loaded).

import { fmtBytes, fmtUsd } from "./format.js";

export const POLL_MS = 15000;

export const MODES = [
  ["off", "Off", "Nothing starts a machine by itself. Start one here when you want it."],
  ["always", "Always Warm", "Starts the machine now and keeps it ready between renders until it has been idle for the warm duration."],
  ["smart", "Smart Warm", "Starts the machine only when you are about to render (the Workflow Check passed after Run, or models are being prepared for a render), so it provisions while models transfer. Opening the panel never starts it."],
];

/** Warm duration presets in seconds (the bridge sends the same list). */
export const DURATIONS = [1800, 3600, 7200, 14400, 28800, 43200, 86400];

export const MEMORY = [
  ["keep", "Keep models loaded", "Fastest for the next workflow of the same queue batch."],
  ["auto", "Unload when GPU memory is high", "Unloads after a workflow when GPU memory use is at or above the upper limit, and only again after it fell below the lower limit."],
  ["always", "Always unload between workflows", "Every workflow starts with empty GPU memory; its models load again (slower)."],
];

const pad = (n) => String(n).padStart(2, "0");
const isNum = (n) => typeof n === "number" && Number.isFinite(n);

/** Local "14:05" from epoch seconds; "" when unknown. */
export function hhmm(ts) {
  if (!isNum(ts)) return "";
  const d = new Date(ts * 1000);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function minutes(s) {
  if (!isNum(s)) return "?";
  if (s >= 3600 && s % 3600 === 0) return `${s / 3600} h`;
  return `${Math.round(s / 60)} min`;
}

/** "45 min", "2 h", "1 h 30 min", "3 days" (a warm duration or a remaining time). */
export function fmtSpan(s) {
  if (!isNum(s)) return "?";
  s = Math.max(0, Math.round(s));
  if (s >= 86400 && s % 86400 === 0 && s > 86400) return `${s / 86400} days`;
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (!h) return `${Math.max(m, s > 0 ? 1 : 0)} min`;
  return m ? `${h} h ${m} min` : `${h} h`;
}

/** Live cost estimate: rate x seconds since the machine was requested. null without a rate. */
export function liveCost(view, nowS) {
  const rate = view?.rate_per_hour_usd;
  if (!isNum(rate) || !isNum(view?.requested_at)) return null;
  return (rate * Math.max(0, nowS - view.requested_at)) / 3600;
}

/** Remaining warm time now (the bridge's value advanced by the clock since `at`), null when the countdown is paused. */
export function remainingNow(view, nowS) {
  const at = view?.auto_shutdown?.at;
  if (!isNum(at) || view?.idle?.paused) return null;
  return Math.max(0, at - nowS);
}

function durationOptions(current) {
  const opts = DURATIONS.map((s) => [String(s), fmtSpan(s)]);
  if (isNum(current) && !DURATIONS.includes(current)) opts.push([String(current), `${fmtSpan(current)} (custom)`]);
  opts.push(["custom", "Custom…"]);
  return opts;
}

function transfersText(cache) {
  if (!cache) return null;
  const parts = [];
  for (const [key, verb, field] of [["downloads", "downloading", "received"], ["uploads", "uploading", "sent"]]) {
    const files = (cache[key] || []).filter((f) => f.status !== "queued");
    const waiting = (cache[key] || []).length - files.length;
    for (const f of files.slice(0, 4)) {
      const pct = isNum(f.size) && f.size > 0 && isNum(f[field]) ? ` ${Math.round((100 * f[field]) / f.size)}%` : "";
      parts.push({ text: `${verb} ${f.name}${pct} (${fmtBytes(f[field])} of ${fmtBytes(f.size)})`,
        fraction: isNum(f.size) && f.size > 0 && isNum(f[field]) ? f[field] / f.size : null });
    }
    if (waiting > 0) parts.push({ text: `${waiting} more ${key === "downloads" ? "download" : "upload"}(s) waiting for a free slot`, fraction: null });
  }
  return parts;
}

function cacheText(cache) {
  if (!cache) return "Model cache status is not available right now.";
  const t = cache.transfers || {};
  const lim = `${t.download?.limit ?? "?"} download(s) and ${t.upload?.limit ?? "?"} upload(s) at a time`;
  let text = `Cloud model library: ${cache.library_files ?? 0} model(s) uploaded from this computer (${fmtBytes(cache.library_bytes ?? 0)}), ` +
    `${cache.verified_files ?? 0} with a SHA-256 record. Transfers: ${lim}.`;
  const partials = cache.partials || [];
  if (partials.length) {
    text += ` ${partials.length} unfinished download(s) on this computer (${fmtBytes(cache.partial_bytes ?? 0)}) resume next time` +
      (cache.abandoned ? `; ${cache.abandoned} untouched for over 7 days can be removed.` : ".");
  }
  return text;
}

function gpuText(gpu, nowS) {
  if (!gpu) return null;
  const parts = [];
  if (isNum(gpu.vram_used_gb) && isNum(gpu.vram_total_gb)) parts.push(`GPU memory ${gpu.vram_used_gb.toFixed(1)} of ${gpu.vram_total_gb.toFixed(1)} GB`);
  if (isNum(gpu.gpu_util)) parts.push(`GPU ${Math.round(gpu.gpu_util)}% busy`);
  if (isNum(gpu.disk_free_gb)) parts.push(`${gpu.disk_free_gb.toFixed(0)} GB disk free`);
  if (!parts.length) return null;
  const age = isNum(gpu.at) ? ` (last render, ${fmtSpan(Math.max(0, nowS - gpu.at))} ago)` : " (last render)";
  return parts.join(" · ") + age;
}

/**
 * @param {object} view  GET /halofuse/warm "warm" object
 * @param {{nowS: number, rate?: number|null}} o  rate = the selected GPU's hourly price (for the "off" hint)
 */
export function warmModel(view, { nowS, rate = null } = {}) {
  const v = view || { state: "off" };
  const state = v.state || "off";
  const s = v.settings || {};
  const lines = [];
  const kv = [];
  const actions = { start: false, shutdown: false, check: false, keep: false };
  let tone = "neutral";
  let title;
  let banner = null;
  const cost = liveCost(v, nowS);
  const costText = cost == null ? "cost unknown (no price for this GPU)" : `about ${fmtUsd(cost)} so far`;
  const safety = v.safety || {};
  const mode = MODES.some(([k]) => k === s.warm_mode) ? s.warm_mode : (s.keep_warm === true ? "always" : "off");
  const duration = isNum(s.warm_idle_timeout_seconds) ? s.warm_idle_timeout_seconds : 3600;
  const hourly = isNum(v.rate_per_hour_usd) ? v.rate_per_hour_usd : (isNum(rate) ? rate : null);

  if (state === "off") {
    title = "Warm machine: off";
    actions.start = true;
    lines.push(`A warm machine stays on between renders: no machine start or image download before each render. ` +
      `${isNum(rate) ? `It costs ${fmtUsd(rate)} per hour while it is on, also when idle.` : "It is billed per hour while it is on, also when idle."}`);
    if (v.last?.message) {
      lines.push(`Last warm machine: ${v.last.message}${isNum(v.last.cost_usd) ? ` About ${fmtUsd(v.last.cost_usd)} (estimate).` : ""}`);
    }
  } else if (state === "starting") {
    title = "Warm machine: starting…";
    tone = "warning";
    actions.shutdown = true;
    lines.push(v.message || "Starting the warm machine (usually a few minutes). Renders wait for it.");
  } else if (state === "stopping") {
    title = "Warm machine: shutting down…";
    tone = "warning";
  } else if (state === "unknown") {
    title = "Warm machine: a cloud machine may still be running";
    tone = "danger";
    banner = v.warning || "A HaloFuse cloud machine may still be running and billing. Check it, or shut it down.";
    actions.check = true;
    actions.shutdown = true;
  } else {
    title = `Warm machine: running since ${hhmm(v.ready_at ?? v.requested_at)}, ${costText}`;
    tone = "success";
    actions.shutdown = true;
  }

  // ---- status grid (label, value) ----
  if (state !== "off") {
    const active = (v.active_renders || 0);
    kv.push(["Renders", `${v.renders || 0} so far${active ? `, ${active} running now` : ""}${v.queues_running ? ` (${v.queues_running} queue(s) running)` : ""}`]);
  }
  const rem = remainingNow(v, nowS);
  if (state === "running") {
    const auto = v.auto_shutdown || {};
    if (auto.status === "paused") kv.push(["Warm time left", `paused while renders run (${fmtSpan(duration)} after the last one)`]);
    else if (auto.status === "waiting") kv.push(["Warm time left", "shuts down after the running render(s)"]);
    else if (rem != null) kv.push(["Warm time left", `${fmtSpan(rem)} (shuts down at ${hhmm(v.auto_shutdown.at)})`]);
    actions.keep = rem != null;   // "Keep it warm": the idle countdown starts again (the artist's own activity)
  }
  if (isNum(hourly)) {
    kv.push(["Hourly rate", `${fmtUsd(hourly)} per hour (${v.instance_type || "selected GPU"})`]);
    kv.push([`Idle cost, ${fmtSpan(duration)}`, `about ${fmtUsd((hourly * duration) / 3600)} if no render comes`]);
  }
  if (state !== "off" && cost != null) {
    const left = rem != null ? rem : duration;
    kv.push(["Cost so far", `about ${fmtUsd(cost)}`]);
    if (isNum(hourly)) kv.push(["Estimated total", `about ${fmtUsd(cost + (hourly * left) / 3600)} if it shuts down on time`]);
  }
  const gpu = gpuText(v.gpu, nowS);
  if (gpu) kv.push(["GPU", gpu]);

  // ---- explanatory lines ----
  let autoTone = "neutral";
  let autoText = null;
  if (state === "running") {
    autoText = v.auto_shutdown?.text || null;
    if (v.auto_shutdown?.status === "soon") autoTone = "warning";
    if (v.auto_shutdown?.status === "waiting") autoTone = "warning";
    if (rem != null && v.auto_shutdown?.status === "armed") {
      autoText = `Automatic shutdown: after ${fmtSpan(duration)} without a render (in about ${fmtSpan(rem)}).`;
    }
  } else if (state === "starting") {
    autoText = `Automatic shutdown: armed once it is ready, after ${fmtSpan(duration)} without a render.`;
  }
  if (state !== "off") {
    if (isNum(v.rate_per_hour_usd)) {
      lines.push(`${v.instance_type || "GPU"} at ${fmtUsd(v.rate_per_hour_usd)} per hour: billed while it is on, also when idle (estimate: rate × time since it was requested, renders on it included).`);
    }
    if (v.renders) lines.push(`${v.renders} render(s) so far${v.active_renders ? `, ${v.active_renders} render(s) running on it now` : ""}.`);
    else if (v.active_renders) lines.push(`${v.active_renders} render(s) running on it now.`);
    lines.push(`Each render still starts ComfyUI in a fresh container on this machine (models load again from its disk cache); a queue batch keeps ComfyUI and its models loaded between its workflows.`);
    lines.push(`If ComfyUI closes, the cloud ends it about ${minutes(v.hold_s)} after its last job.`);
  }
  lines.push(isNum(safety.daily_cap_usd)
    ? `Daily spending limit ${fmtUsd(safety.daily_cap_usd)} (today ${fmtUsd(safety.today_usd)}, estimate); the machine is shut down when it is reached, after any render running on it.`
    : "No daily spending limit is set (Advanced); the machine runs until you shut it down or its warm duration ends.");
  if (isNum(safety.max_runtime_s)) lines.push(`Maximum runtime per render: ${minutes(safety.max_runtime_s)}.`);

  return {
    state, title, tone, lines, kv, banner, actions, confirm: v.shutdown_confirm || null, cleaning: v.cleaning || null,
    mode, modeHint: (MODES.find(([k]) => k === mode) || MODES[0])[2], duration, durationOptions: durationOptions(duration),
    autoText, autoTone, memory: MEMORY.some(([k]) => k === s.memory_behavior) ? s.memory_behavior : (s.unload_models_between_renders ? "always" : "keep"),
    cacheText: cacheText(v.cache), transfers: transfersText(v.cache) || [], abandoned: v.cache?.abandoned || 0,
  };
}

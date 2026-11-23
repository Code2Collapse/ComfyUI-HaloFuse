// HaloFuse warm cloud machine: the panel's view model (owner 2026-11-23). Pure functions, no DOM.
// The bridge's GET /halofuse/warm view is the source; the running cost is recomputed from the clock between polls
// (rate x time since HaloFuse asked for the machine, labelled an estimate). A state the bridge cannot confirm is a
// danger banner, never a quiet line.

import { fmtUsd } from "./format.js";

export const POLL_MS = 15000;

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

/** Live cost estimate: rate x seconds since the machine was requested. null without a rate. */
export function liveCost(view, nowS) {
  const rate = view?.rate_per_hour_usd;
  if (!isNum(rate) || !isNum(view?.requested_at)) return null;
  return (rate * Math.max(0, nowS - view.requested_at)) / 3600;
}

/**
 * @param {object} view  GET /halofuse/warm "warm" object
 * @param {{nowS: number, rate?: number|null}} o  rate = the selected GPU's hourly price (for the "off" hint)
 */
export function warmModel(view, { nowS, rate = null } = {}) {
  const v = view || { state: "off" };
  const state = v.state || "off";
  const lines = [];
  const actions = { start: false, shutdown: false, check: false };
  let tone = "neutral";
  let title;
  let banner = null;
  const cost = liveCost(v, nowS);
  const costText = cost == null ? "cost unknown (no price for this GPU)" : `about ${fmtUsd(cost)} so far`;
  const safety = v.safety || {};

  if (state === "off") {
    title = "Warm machine: off";
    actions.start = true;
    lines.push(`A warm machine stays on between renders until you shut it down: no machine start or image download ` +
      `before each render. ${isNum(rate) ? `It costs ${fmtUsd(rate)} per hour while it is on, also when idle.` : "It is billed per hour while it is on, also when idle."}`);
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

  if (state !== "off") {
    if (isNum(v.rate_per_hour_usd)) {
      lines.push(`${v.instance_type || "GPU"} at ${fmtUsd(v.rate_per_hour_usd)} per hour: billed while it is on, also when idle (estimate: rate × time since it was requested, renders on it included).`);
    }
    if (v.renders) lines.push(`${v.renders} render(s) so far${v.active_renders ? `, ${v.active_renders} render(s) running on it now` : ""}.`);
    else if (v.active_renders) lines.push(`${v.active_renders} render(s) running on it now.`);
    lines.push(`Each render still starts ComfyUI in a fresh container on this machine; a queue batch keeps ComfyUI and its models loaded between its workflows.`);
    lines.push(`If ComfyUI closes, the cloud ends it about ${minutes(v.hold_s)} after its last job.`);
  }
  lines.push(isNum(safety.daily_cap_usd)
    ? `Daily spending limit ${fmtUsd(safety.daily_cap_usd)} (today ${fmtUsd(safety.today_usd)}, estimate); the machine is shut down when it is reached.`
    : "No daily spending limit is set (Advanced); the machine runs until you shut it down.");
  if (isNum(safety.max_runtime_s)) lines.push(`Maximum runtime per render: ${minutes(safety.max_runtime_s)}.`);
  return { state, title, tone, lines, banner, actions, confirm: v.shutdown_confirm || null, cleaning: v.cleaning || null };
}

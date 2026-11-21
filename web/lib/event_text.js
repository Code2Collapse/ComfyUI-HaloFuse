// One-line, artist-readable descriptions of structured run events (C-3 types stored in the log by C-2).
// Lines that already carry real text are shown verbatim; only placeholder "[hf] <type>" texts (or empty ones)
// are replaced. Side-effect-free ES module.

import { fmtBytes, fmtDuration } from "./format.js";
import { phaseLabel } from "./job_view.js";

const num = (v) => typeof v === "number" && Number.isFinite(v);
const gb = (mb) => (mb / 1024).toFixed(1);

function placeholder(e) {
  return !e.text || /^\[hf\]\s*\w*\s*$/.test(e.text);
}

const DESCRIBE = {
  phase(d) {
    const name = phaseLabel(d.phase);
    if (d.state === "start") return `${name} — started`;
    if (d.state === "end") return `${name} — finished${num(d.dur) ? ` in ${fmtDuration(d.dur)}` : ""}`;
    return `${name} — ${String(d.detail || "mark").replace(/_/g, " ")}${num(d.dur) ? ` after ${fmtDuration(d.dur)}` : ""}`;
  },
  node(d) {
    if (d.state === "cached") {
      const ids = (d.nodes || []).map(String);
      return `${ids.length} node${ids.length === 1 ? "" : "s"} reused from cache${ids.length ? ` (${ids.slice(0, 12).join(", ")}${ids.length > 12 ? ", …" : ""})` : ""}`;
    }
    const who = `Node ${d.node ?? "?"}${d.class_type ? ` ${d.class_type}` : ""}`;
    if (d.state === "start") return `${who} started`;
    if (d.state === "done") return `${who} finished${num(d.dur) ? ` in ${fmtDuration(d.dur)}` : ""}`;
    return null;
  },
  progress(d) {
    if (!num(d.value) || !num(d.max)) return null;
    return `Node ${d.node ?? "?"} step ${d.value} of ${d.max}${num(d.eta_s) ? ` · about ${fmtDuration(d.eta_s)} left` : ""}`;
  },
  telemetry(d) {
    const parts = [];
    const g = Array.isArray(d.gpu) ? d.gpu[0] : null;
    if (g && num(g.util_pct)) parts.push(`GPU ${Math.round(g.util_pct)}%`);
    if (g && num(g.mem_used_mb)) parts.push(`VRAM ${gb(g.mem_used_mb)}${num(g.mem_total_mb) ? ` / ${gb(g.mem_total_mb)}` : ""} GB`);
    const r = d.ram;
    if (r && num(r.total_mb) && num(r.available_mb)) parts.push(`RAM ${gb(r.total_mb - r.available_mb)} / ${gb(r.total_mb)} GB`);
    return parts.length ? parts.join(" · ") : null;
  },
  preview(d) { return `Preview (${d.kind || "image"})${d.node != null ? ` from node ${d.node}` : ""}`; },
  output(d) { return d.path ? `Output ${d.path}${num(d.bytes) ? ` (${fmtBytes(d.bytes)})` : ""}` : null; },
  final(d) {
    return `Runner finished: ${d.status || "unknown"}${num(d.outputs) ? `, ${d.outputs} output file(s)` : ""}${d.error ? ` (${d.error})` : ""}`;
  },
  item(d) {
    return `Queue item ${d.wf ?? "?"}${d.of ? ` of ${d.of}` : ""} ${d.state || ""}${d.reason ? `: ${d.reason}` : ""}`.trim();
  },
  recovery(d) { return `Recovery ${String(d.action || "").replace(/_/g, " ")}: ${d.result || "started"}${d.reason ? ` (${d.reason})` : ""}`; },
};

/** Text to display for a normalised log entry. */
export function describeEntry(e) {
  if (!e) return "";
  if (!placeholder(e)) return e.text;
  const fn = DESCRIBE[e.kind];
  const out = fn && e.data ? fn(e.data) : null;
  return out || e.text;
}

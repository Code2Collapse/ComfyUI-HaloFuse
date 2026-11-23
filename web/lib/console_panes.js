// HaloFuse console: Progress, Telemetry, Problems and Summary tabs. Each render function returns a fresh
// element from the job record (C-4 §2) merged with the state derived from the log (run_state.js).
// Side-effect-free ES module.

import {
  cancelInfo, costText, deliveryText, isTerminal, nodeRows, phaseLabel, phaseRows, progressText, statusInfo,
  telemetryRows, timeSplitRows,
} from "./job_view.js";
import { fmtBytes, fmtDuration, fmtLocal } from "./format.js";

/** Merge record fields (when the bridge provides them) with log-derived run state. */
export function mergedRun(job, run) {
  const r = run || {};
  return {
    phases: job?.phases?.length ? job.phases : r.phases || [],
    nodes: job?.nodes?.length ? job.nodes : r.nodes || [],
    progress: job?.progress || r.progress || null,
    telemetry: job?.telemetry?.latest ? job.telemetry : r.telemetry || { latest: null, peak: null },
    errors: r.errors || [],
    previews: r.previews || [],
    outputs: r.outputs || [],
    droppedLines: r.droppedLines || 0,
    final: r.final || null,
    marks: r.marks || [],
  };
}

function empty(H, text) { return H("div", { className: "hf-empty", text }); }

export function renderProgress(H, { job, run, now, onFocusNode }) {
  const m = mergedRun(job, run);
  const wrap = H("div", { className: "hf-scroll" });
  if (!job) { wrap.append(empty(H, "No job attached. Start a render or open one from Recent jobs.")); return wrap; }
  const st = statusInfo(job.status);
  const frac = m.progress?.fraction;
  const bar = H("div", { className: `hf-bar${frac == null && !isTerminal(job) ? " hf-indeterminate" : ""}`,
    attrs: { role: "progressbar", "aria-label": "Render progress", "aria-valuemin": "0", "aria-valuemax": "100",
      "aria-valuenow": frac == null ? null : String(Math.round(frac * 100)) } }, H("div"));
  if (frac != null) bar.firstChild.style.width = `${Math.round(frac * 100)}%`;
  else if (isTerminal(job)) bar.firstChild.style.width = "100%";
  wrap.append(
    H("div", { className: "hf-row" }, H("span", { className: `hf-badge hf-tone-${st.tone}`, text: st.label }),
      H("span", { className: "hf-grow", text: isTerminal(job) ? "" : progressText(m.progress, job.status) })),
    H("div", { style: { margin: "8px 0 4px" } }, bar),
    H("div", { className: "hf-hint", text: m.progress?.eta_s == null && !isTerminal(job)
      ? "Time remaining is shown only while a step counter gives a defensible estimate." : "" }));

  const phases = phaseRows(m.phases, now);
  wrap.append(H("div", { className: "hf-h", text: "Phase timeline" }));
  if (!phases.length) {
    wrap.append(empty(H, "No phase events yet. They appear once the cloud runner reports them."));
  } else {
    const t0 = Math.min(...phases.map((p) => p.startedAt));
    const t1 = Math.max(...phases.map((p) => (p.running ? now : p.endedAt)));
    const span = Math.max(t1 - t0, 0.001);
    const tl = H("div", { className: "hf-timeline", attrs: { role: "list", "aria-label": "Phase timeline" } });
    for (const p of phases) {
      const bar2 = H("div", { className: "hf-tl-span" });
      bar2.style.left = `${((p.startedAt - t0) / span) * 100}%`;
      bar2.style.width = `${(p.duration / span) * 100}%`;
      tl.append(H("div", { className: `hf-tl-row${p.running ? " hf-running" : ""}`, attrs: { role: "listitem" } },
        H("span", { text: p.label + (p.running ? " (running)" : "") }),
        H("div", { className: "hf-tl-track", attrs: { "aria-hidden": "true" } }, bar2),
        H("span", { className: "hf-num", text: fmtDuration(p.duration) })));
    }
    wrap.append(tl);
  }

  const nodes = nodeRows(m.nodes, now);
  wrap.append(H("div", { className: "hf-h", text: "Per-node timing" }));
  if (!nodes.length) {
    wrap.append(empty(H, "No node events yet. Per-node timing needs the runner's ComfyUI event feed."));
  } else {
    const body = H("tbody");
    for (const n of nodes) {
      const tr = H("tr", { className: "hf-clickable", tabIndex: 0, title: "Show this node on the canvas" },
        H("td", { className: "hf-mono", text: n.nodeId }),
        H("td", { text: n.classType || "—" }),
        H("td", { text: n.cached ? "cached" : n.running ? "running" : "done" }),
        H("td", { className: "hf-num", text: n.cached ? "—" : fmtDuration(n.duration) }));
      const go = () => onFocusNode?.(n.nodeId);
      tr.addEventListener("click", go);
      tr.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); go(); } });
      body.append(tr);
    }
    wrap.append(H("table", { className: "hf-table" },
      H("thead", {}, H("tr", {}, H("th", { text: "Node" }), H("th", { text: "Type" }), H("th", { text: "State" }), H("th", { className: "hf-num", text: "Time" }))),
      body));
  }
  return wrap;
}

export function renderTelemetry(H, { job, run }) {
  const m = mergedRun(job, run);
  const wrap = H("div", { className: "hf-scroll" });
  const rows = telemetryRows(m.telemetry);
  if (!rows.length) {
    wrap.append(empty(H, job ? "No resource samples yet. The cloud runner reports GPU, memory and disk every few seconds while it runs."
      : "No job attached."));
    return wrap;
  }
  const body = H("tbody");
  for (const r of rows) body.append(H("tr", {}, H("td", { text: r.label }), H("td", { className: "hf-num", text: r.latest }), H("td", { className: "hf-num", text: r.peak })));
  wrap.append(H("table", { className: "hf-table" },
    H("thead", {}, H("tr", {}, H("th", { text: "Resource" }), H("th", { className: "hf-num", text: "Latest" }), H("th", { className: "hf-num", text: "Peak" }))), body));
  const ts = m.telemetry.latest?.ts;
  if (ts) wrap.append(H("div", { className: "hf-hint", style: { marginTop: "8px" }, text: `Last sample ${fmtLocal(ts)}. Values are measurements from the cloud machine.` }));
  return wrap;
}

export function renderProblems(H, { job, run, warnings, onJump }) {
  const m = mergedRun(job, run);
  const wrap = H("div", { className: "hf-scroll" });
  const d = job?.diagnosis;
  if (d) {
    wrap.append(H("div", { className: "hf-card hf-card-danger", attrs: { role: "group", "aria-label": "Diagnosis" } },
      H("h3", { text: d.title || d.code || "Render problem" }),
      d.cause ? H("div", {}, H("strong", { text: "Likely cause: " }), d.cause) : null,
      d.next_action ? H("div", {}, H("strong", { text: "What to do: " }), d.next_action) : null,
      d.raw_ref?.seq != null ? H("div", {}, H("button", { className: "hf-btn", type: "button", text: "Show the raw error in the log",
        onclick: () => onJump(d.raw_ref.seq) })) : null));
  } else if (job?.error?.message) {
    wrap.append(H("div", { className: "hf-card hf-card-danger" }, H("h3", { text: "Render problem" }), H("div", { text: job.error.message })));
  }
  if (m.droppedLines) {
    wrap.append(H("div", { className: "hf-card" }, H("h3", { className: "hf-tone-warning", text: "Lines missing from the live stream" }),
      H("div", { text: `The cloud log stream dropped ${m.droppedLines} line(s). The full runner log is saved with the outputs when the job ends.` })));
  }
  wrap.append(H("div", { className: "hf-h", text: `Errors (${m.errors.length})` }));
  if (!m.errors.length) wrap.append(empty(H, "No errors reported."));
  for (const e of m.errors.slice(-200)) {
    const where = [e.class_type, e.node_id ? `node ${e.node_id}` : null].filter(Boolean).join(", ");
    wrap.append(H("div", { className: "hf-item" },
      H("span", { className: "hf-grow", title: e.message, text: `${e.code ? `[${e.code}] ` : ""}${e.message}${where ? ` (${where})` : ""}` }),
      H("button", { className: "hf-btn hf-icon", type: "button", text: "Log", title: "Show in the log", onclick: () => onJump(e.seq) })));
  }
  wrap.append(H("div", { className: "hf-h", text: `Warnings (${warnings.length})` }));
  if (!warnings.length) wrap.append(empty(H, "No warnings."));
  for (const w of warnings.slice(-200)) {
    wrap.append(H("div", { className: "hf-item" }, H("span", { className: "hf-grow", title: w.text, text: w.text }),
      H("button", { className: "hf-btn hf-icon", type: "button", text: "Log", title: "Show in the log", onclick: () => onJump(w.seq) })));
  }
  return wrap;
}

function mediaFor(H, kind, url, name) {
  if (kind === "video") return H("video", { src: url, controls: true, preload: "metadata", attrs: { "aria-label": name } });
  if (kind === "audio") return H("audio", { src: url, controls: true, preload: "none", attrs: { "aria-label": name } });
  if (kind === "image" || kind === "mask" || kind === "depth") return H("img", { src: url, alt: name, loading: "lazy" });
  return null;
}

export function outputKind(name, kind) {
  if (kind && kind !== "other") return kind;
  const ext = String(name).split(".").pop().toLowerCase();
  if (["png", "jpg", "jpeg", "webp", "gif", "bmp", "avif"].includes(ext)) return "image";
  if (["mp4", "webm", "mov", "mkv"].includes(ext)) return "video";
  if (["wav", "mp3", "flac", "ogg", "m4a", "aac", "opus"].includes(ext)) return "audio";
  return "other";
}

export function renderSummary(H, { job, run, outputs, viewUrl, previewUrl, onRetryDelivery, onCopy }) {
  const m = mergedRun(job, run);
  const wrap = H("div", { className: "hf-scroll" });
  if (!job) { wrap.append(empty(H, "No job attached.")); return wrap; }
  const st = statusInfo(job.status);
  const ci = cancelInfo(job.cancel);
  const kv = H("dl", { className: "hf-kv" });
  const add = (k, v, copy) => {
    if (v === null || v === undefined || v === "") return;
    kv.append(H("dt", { text: k }), H("dd", {}, H("span", { className: copy ? "hf-mono" : "", text: String(v) }),
      copy ? H("button", { className: "hf-btn hf-ghost hf-icon", type: "button", text: "⧉", title: `Copy ${k.toLowerCase()}`,
        attrs: { "aria-label": `Copy ${k.toLowerCase()}` }, onclick: () => onCopy(String(v)) }) : null));
  };
  add("Status", st.label + (job.status_note ? ` — ${job.status_note}` : ""));
  if (ci) add("Stop", `${ci.label}${ci.detail ? ` — ${ci.detail}` : ""}`);
  add("Job", job.label);
  add("HaloFuse job id", job.job_id, true);
  add("Cloud job id", (job.cloud_job_ids || []).join(", "), true);
  add("ComfyUI prompt ids", (job.prompt_ids || []).join(", "), true);
  add("GPU", job.instance_type);
  add("Created", fmtLocal(job.created_at));
  add("Ended", fmtLocal(job.ended_at));
  add("Cost", costText(job.cost));
  add("Outputs", deliveryText(job.delivery));
  if (m.final) add("Runner result", `${m.final.status}${m.final.error ? ` (${m.final.error})` : ""}, ${m.final.outputs ?? 0} output file(s)`);
  wrap.append(kv);
  // L9.338: where the time went (queue, machine and image, start, model copies, first node, execution)
  const split = timeSplitRows({ phases: m.phases, timeline: run?.timeline, createdAt: job.created_at,
    now: isTerminal(job) && Number.isFinite(job.ended_at) ? job.ended_at : Date.now() / 1000 });
  if (split.length) {
    wrap.append(H("div", { className: "hf-h", text: "Time split" }));
    const tkv = H("dl", { className: "hf-kv", attrs: { "aria-label": "Time split" } });
    for (const r of split) {
      tkv.append(H("dt", { text: r.label }), H("dd", { text: `${fmtDuration(r.seconds)}${r.note ? ` (${r.note})` : ""}` }));
    }
    wrap.append(tkv);
  }
  if (job.delivery?.state === "failed" || job.delivery?.state === "partial") {
    wrap.append(H("div", { className: "hf-row", style: { marginTop: "8px" } },
      H("button", { className: "hf-btn", type: "button", text: "Retry output download", onclick: onRetryDelivery })));
  }

  const previews = m.previews.filter((p) => p.dataUrl || p.name);
  wrap.append(H("div", { className: "hf-h", text: "Previews" }));
  if (!previews.length) {
    wrap.append(empty(H, "No previews for this job. Choose Live preview before rendering to receive them (experimental)."));
  } else {
    const g = H("div", { className: "hf-gallery" });
    for (const p of previews.slice(-24).reverse()) {
      const src = p.dataUrl || previewUrl(p.name);
      g.append(H("figure", {}, H("img", { src, alt: `${p.kind} preview${p.node_id ? ` from node ${p.node_id}` : ""}` }),
        H("figcaption", { text: `${p.kind}${p.node_id ? ` · node ${p.node_id}` : ""}` })));
    }
    wrap.append(g);
  }

  wrap.append(H("div", { className: "hf-h", text: "Delivered outputs" }));
  if (!outputs?.length) {
    wrap.append(empty(H, isTerminal(job) ? "No delivered files yet." : "Outputs are downloaded automatically when the render ends."));
  } else {
    const g = H("div", { className: "hf-gallery" });
    for (const o of outputs.slice(0, 200)) {
      const name = o.filename || o.rel_path;
      const url = viewUrl(o);
      const media = mediaFor(H, outputKind(name, o.kind), url, name);
      g.append(H("figure", {}, media || H("div", { className: "hf-empty", text: "No preview" }),
        H("figcaption", { title: o.rel_path || name }, H("a", { href: url, target: "_blank", rel: "noopener", text: name }),
          o.size != null ? ` · ${fmtBytes(o.size)}` : "")));
    }
    wrap.append(g);
    if (outputs.length > 200) wrap.append(H("div", { className: "hf-hint", text: `Showing 200 of ${outputs.length} files. All are in the output folder.` }));
  }
  if (m.marks.length) {
    wrap.append(H("div", { className: "hf-h", text: "Runner markers" }));
    for (const mk of m.marks) wrap.append(H("div", { className: "hf-hint", style: { margin: "0 0 2px" }, text: `${phaseLabel(mk.name)}: ${mk.detail || ""} ${mk.dur != null ? fmtDuration(mk.dur) : ""}` }));
  }
  return wrap;
}

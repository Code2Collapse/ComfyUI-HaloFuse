// HaloFuse Live view: the latest preview of a running cloud render, its step, the masks and intermediate images,
// and the "looks wrong" actions (Stop now, Stop and try a new seed). Side-effect-free ES module.
// PREVIEW_DESIGN section 4. Frames are files the bridge wrote into the job folder while the job runs (C-4 6a);
// they load by URL, never as data: strings. Everything here is labelled experimental until a real cloud render
// with the new runtime image has been verified.

const MAX_LATENT = 120;
const MAX_RECORDS = 300;
const FILMSTRIP = 30;
const MASKS = 12;
const STALE_S = 6; // 3 x the 2 s preview interval
const THUMB_KINDS = new Set(["temp_output", "output_thumb"]);
export const TINT = [45, 212, 191]; // --hf-runner teal
export const OVERLAY_MODES = [["overlay", "Overlay"], ["mask", "Mask only"], ["image", "Image only"]];

export const LIVE_TEXT = Object.freeze({
  off: "Live preview is off for this render. Choose Live preview before rendering to watch it.",
  waiting: "Waiting for the first preview…",
  stale: "Waiting for the next preview (the cloud log can drop lines).",
  none: "This render sent no previews. The cloud runtime may be an older image without live preview.",
  fallback: "Detailed preview unavailable for this model; showing the fast preview.",
  experimental: "Experimental: needs the new HaloFuse runtime image.",
});

const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);
const terminal = (job) => !!job && (job.terminal === true
  || ["succeeded", "completed", "failed", "cancelled", "timed_out"].includes(job.status));

export function methodText(method) {
  if (method === "taesd") return "Detailed (small decoder)";
  if (method === "none") return "Off (masks and results only)";
  return "Fast (colour approximation)";
}

/** In place: a grey mask becomes the tint colour with alpha = grey x strength. */
export function tintMask(imageData, rgb = TINT, strength = 0.6) {
  const d = imageData.data;
  for (let i = 0; i < d.length; i += 4) {
    const grey = (d[i] + d[i + 1] + d[i + 2]) / 3;
    d[i] = rgb[0]; d[i + 1] = rgb[1]; d[i + 2] = rgb[2];
    d[i + 3] = Math.round(grey * strength);
  }
  return imageData;
}

export class LiveModel {
  constructor() { this.reset(); }

  reset() {
    this._bySeq = new Map();
    this._held = null;
    this.version = 0;
  }

  get size() { return this._bySeq.size; }

  /** Records from GET /halofuse/jobs/{id}/previews (any order, repeats harmless). */
  ingest(records) {
    let added = 0;
    for (const r of records || []) {
      if (!r || !Number.isInteger(r.seq) || typeof r.name !== "string" || this._bySeq.has(r.seq)) continue;
      this._bySeq.set(r.seq, { ...r });
      added += 1;
    }
    if (!added) return 0;
    this._prune();
    this.version += 1;
    return added;
  }

  hold(seq) {
    this._held = seq == null ? null : seq;
    this.version += 1;
  }

  _sorted() { return [...this._bySeq.values()].sort((a, b) => a.seq - b.seq); }

  _prune() {
    const all = this._sorted();
    const latent = all.filter((r) => r.kind === "latent");
    for (const r of latent.slice(0, Math.max(0, latent.length - MAX_LATENT))) this._bySeq.delete(r.seq);
    const rest = this._sorted();
    for (const r of rest.slice(0, Math.max(0, rest.length - MAX_RECORDS))) this._bySeq.delete(r.seq);
  }

  /** The newest non-mask image of the same prompt (the picture a mask belongs over), or null. */
  overlayBase(mask) {
    if (!mask) return null;
    const all = this._sorted();
    for (let i = all.length - 1; i >= 0; i--) {
      const r = all[i];
      if (!r.mask && r.prompt_id && r.prompt_id === mask.prompt_id) return r;
    }
    return null;
  }

  view({ nowS, job, progress = null, marks = [], droppedLines = 0 } = {}) {
    const all = this._sorted();
    const latent = all.filter((r) => r.kind === "latent");
    const thumbs = all.filter((r) => THUMB_KINDS.has(r.kind) || r.source === "delivered");
    const held = this._held != null ? this._bySeq.get(this._held) || null : null;
    const newestImage = [...thumbs].reverse().find((r) => !r.mask) || null;
    const frame = held || latent.at(-1) || newestImage;
    const running = !!job && !terminal(job);
    const newestFrameTs = num(latent.at(-1)?.ts ?? frame?.ts);
    const ageS = frame && num(frame.ts) != null && num(nowS) != null ? Math.max(0, Math.round(nowS - frame.ts)) : null;
    const stale = running && !held && newestFrameTs != null && nowS - newestFrameTs > STALE_S;
    let statusText = null;
    if (!all.length) {
      if (job && job.preview_mode && job.preview_mode !== "live" && !terminal(job)) statusText = LIVE_TEXT.off;
      else if (running) statusText = LIVE_TEXT.waiting;
      else if (job) statusText = job.preview_mode && job.preview_mode !== "live" ? LIVE_TEXT.off : LIVE_TEXT.none;
    } else if (stale) statusText = LIVE_TEXT.stale;
    const step = num(progress?.step) ?? num(frame?.step);
    const steps = num(progress?.steps) ?? num(frame?.steps);
    const nodeId = progress?.node_id ?? frame?.node_id ?? null;
    const method = job?.preview_method || frame?.method || null;
    const fellBack = (marks || []).some((m) => m && m.name === "preview_fallback");
    return {
      frame,
      holding: !!held,
      ageS,
      ageText: ageS == null ? null : `Frame from ${ageS} s ago`,
      stale,
      statusText,
      stepText: step != null && steps ? `Step ${step} of ${steps}` : null,
      fraction: step != null && steps ? Math.min(step / steps, 1) : null,
      nodeText: nodeId != null ? `Node ${nodeId}${progress?.class_type ? ` ${progress.class_type}` : ""}` : null,
      etaText: num(progress?.eta_s) != null ? `about ${Math.round(progress.eta_s)} s left` : null,
      methodText: method === "taesd" && fellBack ? LIVE_TEXT.fallback : methodText(method),
      droppedText: droppedLines > 0 ? `The cloud dropped ${droppedLines} log lines; some previews are missing.` : null,
      filmstrip: latent.slice(-FILMSTRIP),
      masks: thumbs.slice(-MASKS).reverse(),
      running,
    };
  }
}

/** Draw a mask over its image on a canvas: "overlay" tinted mask over the base, "mask" tint on a checkerboard,
 *  "image" the base only. Both images must be loaded (same-origin bridge files, so getImageData is allowed). */
export function drawMaskOverlay(canvas, baseImg, maskImg, mode = "overlay", doc = canvas.ownerDocument) {
  const src = baseImg && baseImg.naturalWidth ? baseImg : maskImg;
  const w = src?.naturalWidth || 0;
  const h = src?.naturalHeight || 0;
  if (!w || !h) return false;
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, w, h);
  if (mode === "mask" || !baseImg || !baseImg.naturalWidth) {
    const cell = Math.max(8, Math.round(Math.min(w, h) / 24));
    for (let y = 0; y < h; y += cell) {
      for (let x = 0; x < w; x += cell) {
        ctx.fillStyle = ((x / cell + y / cell) % 2) ? "#3a3a3a" : "#2a2a2a";
        ctx.fillRect(x, y, cell, cell);
      }
    }
  } else {
    ctx.drawImage(baseImg, 0, 0, w, h);
  }
  if (mode === "image" || !maskImg || !maskImg.naturalWidth) return true;
  const off = doc.createElement("canvas");
  off.width = w;
  off.height = h;
  const octx = off.getContext("2d");
  octx.drawImage(maskImg, 0, 0, w, h);
  const data = octx.getImageData(0, 0, w, h);
  tintMask(data, TINT, mode === "mask" ? 0.9 : 0.6);
  octx.putImageData(data, 0, 0);
  ctx.drawImage(off, 0, 0);
  return true;
}

/**
 * The Live pane. ctx: {view, model, job, imgFor(name) -> <img> (reused across renders: no re-fetch, no flicker),
 * stopping, message, selectedMask, overlayMode, onStop(), onReseed(), onHold(seq|null), onSelectMask(seq|null),
 * onOverlayMode(mode), isQueue}.
 */
export function renderLive(H, ctx) {
  const v = ctx.view;
  const job = ctx.job;
  const cancelState = job?.cancel?.state;
  const stopping = ctx.stopping || cancelState === "cancel_requested" || cancelState === "cancel_accepted";
  const canAct = !!job && v.running && !ctx.isQueue;
  const actions = H("div", { className: "hf-live-actions" });
  if (canAct) {
    actions.append(
      H("button", { className: "hf-btn hf-danger hf-solid", type: "button", disabled: stopping, dataset: { hfKey: "stop" },
        text: stopping ? "Stopping…" : "Stop now", title: "Cancel the cloud job now; partial outputs are kept",
        onclick: () => ctx.onStop() }),
      H("button", { className: "hf-btn hf-danger", type: "button", disabled: stopping, dataset: { hfKey: "reseed" },
        text: "Stop and try a new seed", title: "Stop this render and start it again with new seeds (same GPU and settings)",
        onclick: () => ctx.onReseed() }));
  }
  const head = H("div", { className: "hf-live-head" },
    H("span", { className: "hf-live-exp", text: LIVE_TEXT.experimental }), actions);

  // stage: the frame (or a mask drawn over its image) with the step bar
  const stage = H("figure", { className: `hf-live-stage${v.stale ? " hf-live-stale" : ""}` });
  const selected = ctx.selectedMask != null ? v.masks.find((m) => m.seq === ctx.selectedMask) || null : null;
  if (selected) {
    const canvas = H("canvas", { className: "hf-live-canvas", attrs: { role: "img",
      "aria-label": `${selected.mask ? "Mask" : "Image"} from node ${selected.node_id ?? "?"}` } });
    const base = selected.mask ? ctx.model.overlayBase(selected) : null;
    const maskImg = ctx.imgFor(selected.name, "src");
    const baseImg = base ? ctx.imgFor(base.name, "src") : null;
    const draw = () => {
      if ((maskImg.complete && maskImg.naturalWidth) && (!baseImg || (baseImg.complete && baseImg.naturalWidth))) {
        drawMaskOverlay(canvas, selected.mask ? baseImg : maskImg, selected.mask ? maskImg : null,
          selected.mask ? ctx.overlayMode : "image");
      }
    };
    for (const im of [maskImg, baseImg]) if (im && !(im.complete && im.naturalWidth)) im.addEventListener("load", draw, { once: true });
    draw();
    stage.append(canvas);
  } else if (v.frame) {
    const img = ctx.imgFor(v.frame.name, "stage");
    img.className = "hf-live-frame";
    img.alt = v.frame.kind === "latent" ? `Live preview${v.stepText ? `, ${v.stepText.toLowerCase()}` : ""}` : "Latest preview image";
    stage.append(img);
  } else {
    stage.append(H("div", { className: "hf-live-empty", text: v.statusText || LIVE_TEXT.waiting }));
  }
  if (v.fraction != null && !selected) {
    const bar = H("div", { className: "hf-live-step" }, H("div", { className: "hf-live-step-fill" }),
      H("span", { text: v.stepText.replace(/^Step (\d+) of (\d+)$/, "$1/$2") }));
    bar.firstChild.style.width = `${Math.round(v.fraction * 100)}%`;
    stage.append(bar);
  }

  // side: what is shown, where it comes from, and how fresh it is
  const info = H("dl", { className: "hf-live-info" });
  const row = (k, val) => { if (val) info.append(H("dt", { text: k }), H("dd", { text: val })); };
  row("Node", v.nodeText);
  row("Progress", [v.stepText, v.etaText].filter(Boolean).join(", "));
  row("Preview", v.methodText);
  row("Frame", v.holding ? "Held (live view paused)" : v.ageText);
  // the console owns one persistent polite live region (updated at most every 5 s); this line is visual only
  const status = H("p", { className: `hf-live-status${v.stale || v.droppedText ? " hf-tone-warning" : ""}`, text: [v.frame ? v.statusText : null, v.droppedText, ctx.message].filter(Boolean).join(" ") });
  const side = H("div", { className: "hf-live-side" }, info, status);
  if (v.masks.length) {
    const thumbs = H("div", { className: "hf-live-thumbs", attrs: { role: "group", "aria-label": "Masks and intermediate images" } });
    for (const m of v.masks) {
      const on = m.seq === ctx.selectedMask;
      const label = `${m.mask ? "Mask" : m.kind === "output_thumb" ? (m.source_kind === "video" ? "Video frames" : "Result") : "Image"} from node ${m.node_id ?? "?"}`;
      const im = ctx.imgFor(m.name, "thumb");
      im.alt = "";
      thumbs.append(H("button", { className: `hf-live-thumb${on ? " hf-on" : ""}${m.mask ? " hf-live-mask" : ""}`, type: "button",
        dataset: { hfKey: `mask-${m.seq}` },
        title: label, attrs: { "aria-label": label, "aria-pressed": String(on) },
        onclick: () => ctx.onSelectMask(on ? null : m.seq) }, im));
    }
    side.append(H("h4", { className: "hf-live-h", text: "Masks and intermediate images" }), thumbs);
  }
  if (selected?.mask) {
    const name = `hf-overlay-${selected.seq}`;
    const group = H("fieldset", { className: "hf-live-modes" }, H("legend", { text: "Show the mask as" }));
    for (const [id, label] of OVERLAY_MODES) {
      const input = H("input", { type: "radio", name, value: id, checked: ctx.overlayMode === id, dataset: { hfKey: `mode-${id}` },
        onchange: () => ctx.onOverlayMode(id) });
      group.append(H("label", {}, input, ` ${label}`));
    }
    side.append(group);
  }
  if (selected) side.append(H("button", { className: "hf-btn", type: "button", text: "Back to the live frame", dataset: { hfKey: "back-frame" }, onclick: () => ctx.onSelectMask(null) }));

  const body = H("div", { className: "hf-live-body" }, stage, side);
  const out = H("section", { className: "hf-live", attrs: { "aria-label": "Live preview" } }, head, body);
  if (v.filmstrip.length > 1) {
    const strip = H("div", { className: "hf-live-strip", attrs: { role: "group", "aria-label": "Recent frames" } });
    for (const f of v.filmstrip) {
      const on = v.holding && v.frame?.seq === f.seq;
      const im = ctx.imgFor(f.name, "strip");
      im.alt = "";
      strip.append(H("button", { className: `hf-live-thumb${on ? " hf-on" : ""}`, type: "button", dataset: { hfKey: `frame-${f.seq}` },
        attrs: { "aria-label": f.step != null ? `Show frame from step ${f.step}` : `Show frame ${f.seq}`, "aria-pressed": String(on) },
        onclick: () => ctx.onHold(on ? null : f.seq) }, im));
    }
    out.append(strip);
    if (v.holding) out.append(H("button", { className: "hf-btn", type: "button", text: "Back to live", dataset: { hfKey: "back-live" }, onclick: () => ctx.onHold(null) }));
  }
  return out;
}

/** <img> elements by preview name, reused across renders (a re-created <img> would re-fetch a no-store file). */
export function createImageCache(doc, urlFor, max = 160) {
  const map = new Map();
  return {
    /** One element per (name, role): an element can sit in one place only (stage, thumbnail, filmstrip). */
    get(name, role = "") {
      const key = `${role}:${name}`;
      let img = map.get(key);
      if (img) { map.delete(key); map.set(key, img); return img; }
      img = doc.createElement("img");
      img.decoding = "async";
      img.src = urlFor(name);
      map.set(key, img);
      while (map.size > max) map.delete(map.keys().next().value);
      return img;
    },
    clear() { map.clear(); },
    get size() { return map.size; },
  };
}

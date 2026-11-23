// HaloFuse per-node preview sections (NODE_PREVIEWS_DESIGN section 2): one card per node of the running workflow
// that shows something - its title, a state badge (waiting / running / done / cached / failed / not run) and its
// items, rendered by kind (frames, images, masks, videos, audio, text, file chips). The board comes from
// GET /halofuse/jobs/{id}/nodes; nothing here fetches. Side-effect-free ES module. Text goes in with textContent only.

export const STATE_TONE = Object.freeze({
  waiting: "neutral", running: "active", done: "success", cached: "info", failed: "danger", not_run: "neutral",
});
export const STATE_LABEL = Object.freeze({
  waiting: "Waiting", running: "Running", done: "Done", cached: "Cached", failed: "Failed", not_run: "Not run",
});
// settings.auto_preview (bridge auto_preview.MODES): one frame per preview, because image batches live in host RAM
export const AUTO_PREVIEW_MODES = Object.freeze([
  ["off", "Off"], ["first", "First frame"], ["last", "Last frame"], ["first_last", "First and last frame"],
]);
const MEDIA_KINDS = new Set(["frame", "image", "mask"]);
const MAX_CARD_ITEMS = 8;

/** The board as the panel/console show it: sections in order, each with display items and a one-line summary. */
export function sectionsView(board) {
  const sections = Array.isArray(board?.sections) ? board.sections : [];
  return {
    sections: sections.map((s) => ({
      ...s,
      tone: STATE_TONE[s.state] || "neutral",
      badge: STATE_LABEL[s.state] || String(s.state || ""),
      items: (Array.isArray(s.items) ? s.items : []).slice(-MAX_CARD_ITEMS),
      thumb: [...(s.items || [])].reverse().find((i) => MEDIA_KINDS.has(i.kind) && !i.pending
        && (i.preview || (i.type === "output" && i.filename && i.subfolder))) || null,
    })),
    notes: Array.isArray(board?.notes) ? board.notes : [],
    nodeKinds: board?.capabilities?.node_kinds === true,
    empty: !sections.length,
  };
}

/** URL of one item: a preview file of this (or an earlier) job, or a delivered file through ComfyUI's /view. */
export function itemUrl(item, { jobId, previewUrl, viewUrl }) {
  if (!item) return null;
  if (item.preview) return previewUrl(item.job_id || jobId, item.preview);
  if (item.filename && item.subfolder && item.type === "output" && !item.pending) {
    return viewUrl({ filename: item.filename, subfolder: item.subfolder, type: "output" });
  }
  return null;
}

/** A short caption for an item (never a URL). */
export function itemCaption(item) {
  const parts = [];
  if (item.label) parts.push(item.label);
  if (item.kind === "frame") {
    parts.push(item.step != null && item.steps ? `Step ${item.step} of ${item.steps}` : "Latest step preview");
  } else if (item.kind === "mask") parts.push("Mask");
  if (item.pending) parts.push(`${item.filename || "File"} - arrives when the job ends`);
  else if (item.filename && !MEDIA_KINDS.has(item.kind)) parts.push(item.filename);
  if (item.source === "earlier_job" && item.job_id) parts.push(`from job ${String(item.job_id).slice(0, 8)}`);
  if (item.match === "file_name") parts.push("matched by file name");
  return parts.join(" · ");
}

function renderItem(H, item, ctx) {
  const url = itemUrl(item, ctx);
  const caption = itemCaption(item);
  let media = null;
  if (item.kind === "text") {
    media = H("pre", { className: "hf-node-pre", text: String(item.text ?? "") });
  } else if (url && MEDIA_KINDS.has(item.kind)) {
    media = ctx.imgFor ? ctx.imgFor(item, url) : H("img", { src: url, loading: "lazy" });
    media.alt = caption || `${item.kind} preview`;
    media.className = `hf-node-img${item.kind === "mask" ? " hf-node-mask" : ""}`;
  } else if (url && item.kind === "video") {
    media = H("video", { src: url, controls: true, preload: "metadata", attrs: { "aria-label": caption || "Video" } });
  } else if (url && item.kind === "audio") {
    media = H("audio", { src: url, controls: true, preload: "none", attrs: { "aria-label": caption || "Audio" } });
  } else if (url) {
    // 3D models and any file kind HaloFuse has no viewer for: a plain file chip
    media = H("a", { className: "hf-node-chip", href: url, target: "_blank", rel: "noopener",
      text: `${item.kind === "3d" ? "3D model" : "File"}: ${item.filename || item.preview}` });
  } else {
    media = H("span", { className: "hf-node-chip hf-node-pending", text: item.kind === "3d" ? "3D model" : item.kind || "file" });
  }
  return H("figure", { className: `hf-node-item hf-node-${item.kind || "file"}` }, media,
    caption && item.kind !== "text" ? H("figcaption", { text: caption }) : null);
}

/**
 * The sections list. ctx: {board, jobId, previewUrl(jobId, name), viewUrl(ref), onFocusNode(id)?, imgFor(item,url)?,
 * compact} - compact = the panel's job card: one line per node with its newest picture, at most 6 nodes.
 */
export function renderNodeSections(H, ctx) {
  const v = sectionsView(ctx.board);
  const wrap = H("section", { className: `hf-nodes${ctx.compact ? " hf-nodes-compact" : ""}`,
    attrs: { "aria-label": "Previews by node" } });
  if (!ctx.compact) wrap.append(H("h4", { className: "hf-live-h hf-nodes-wide", text: "Previews by node" }));
  for (const note of v.notes) wrap.append(H("p", { className: "hf-hint hf-tone-warning hf-nodes-wide", text: note }));
  const org = ctx.board?.organized;
  if (org?.folder && !ctx.compact) {
    const how = org.method === "copy" ? " (copies)" : org.method === "hardlink" ? " (hard links)" : "";
    wrap.append(H("p", { className: "hf-hint hf-nodes-wide", text: org.files
      ? `Saved by name: ${org.files} file(s)${how} in output/${org.folder}, with job.log and metadata.json. The job folder is kept.`
      : `No output files in this render. Its job.log and metadata.json are saved in output/${org.folder}.` }));
  }
  if (v.empty) {
    if (!ctx.compact) wrap.append(H("div", { className: "hf-empty hf-nodes-wide", text: "No node of this workflow shows a preview or an output yet." }));
    return wrap;
  }
  const list = ctx.compact ? v.sections.slice(0, 6) : v.sections;
  for (const s of list) {
    const head = H("div", { className: "hf-node-head" },
      H("span", { className: "hf-node-title", text: s.title, title: `${s.class_type || ""} #${s.node_id}` }),
      H("span", { className: `hf-badge hf-tone-${s.tone}`, text: s.badge }));
    if (ctx.onFocusNode && !ctx.compact) {
      head.append(H("button", { className: "hf-btn hf-ghost hf-icon", type: "button", text: "⌖",
        title: "Show this node on the canvas", attrs: { "aria-label": `Show ${s.title} on the canvas` },
        onclick: () => ctx.onFocusNode(s.node_id) }));
    }
    const card = H("article", { className: `hf-node-card hf-node-state-${s.state}`, dataset: { hfNode: s.node_id },
      attrs: { "aria-label": `${s.title}: ${s.badge}` } }, head);
    if (ctx.compact) {
      if (s.thumb) card.append(renderItem(H, s.thumb, ctx));
      wrap.append(card);
      continue;
    }
    if (s.state_text && s.state_text !== s.badge) card.append(H("div", { className: "hf-hint", text: s.state_text }));
    if (s.items.length) {
      const items = H("div", { className: "hf-node-items" });
      for (const item of s.items) items.append(renderItem(H, item, ctx));
      card.append(items);
    }
    for (const note of s.notes || []) card.append(H("p", { className: "hf-hint", text: note }));
    wrap.append(card);
  }
  if (ctx.compact && v.sections.length > list.length) {
    wrap.append(H("div", { className: "hf-hint", text: `${v.sections.length - list.length} more in the console.` }));
  }
  return wrap;
}

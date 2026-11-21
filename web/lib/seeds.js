// Seed control_after_generate for HaloFuse submissions. Side-effect-free ES module.
// Behaviour moved unchanged from the 0.3.4 web/spark_fuse.js (compat contract: WORKSTREAMS §4).
//
// The bridge submits app.graphToPrompt() output directly, bypassing ComfyUI's own Queue path, so
// control_after_generate never fires by itself. Like ComfyUI, the prompt is captured FIRST with the current
// seed, then the widget is advanced so the NEXT submission differs.
// - Seed widgets are found through ComfyUI's own association (widget.linkedWidgets contains the node's
//   control_after_generate widget), never by name lists or array adjacency: a promoted subgraph widget's
//   control can sit anywhere later in the widget list (verified live; adjacency silently found nothing).
//   linkedWidgets entries may be widget objects or plain name strings; strings are resolved by name.
// - The seed's own options.{min,max} are the bounds; max (observed 18446744073709552000) is clamped to
//   Number.MAX_SAFE_INTEGER and every new value must pass Number.isSafeInteger().
// - Only top-level graph nodes are walked: a promoted seed lives on the wrapper node and is linked to the
//   inner widget, so recursing into subgraphs risks advancing one seed twice. Known gap: a seed never
//   promoted out of a subgraph is not advanced.

export function findLinkedControl(widget, widgets) {
  const linked = widget.linkedWidgets;
  if (!Array.isArray(linked)) return null;
  for (const entry of linked) {
    if (entry && typeof entry === "object" && entry.name === "control_after_generate") return entry;
    if (typeof entry === "string" && entry === "control_after_generate") {
      const found = widgets.find((w2) => w2.name === "control_after_generate");
      if (found) return found;
    }
  }
  return null;
}

/** Advance seeds on the given top-level nodes (pass app.graph._nodes). Returns counts per mode. */
export function advanceSeeds(nodes, random = Math.random) {
  const counts = { randomize: 0, increment: 0, decrement: 0 };
  for (const node of nodes || []) {
    const widgets = node.widgets;
    if (!Array.isArray(widgets)) continue;
    for (const w of widgets) {
      if (typeof w.value !== "number") continue;
      const control = findLinkedControl(w, widgets);
      if (!control) continue;
      const mode = control.value;
      if (mode === "fixed") continue;
      const lower = Number.isFinite(w.options?.min) ? w.options.min : 0;
      const upper = Math.min(
        Number.isFinite(w.options?.max) ? w.options.max : Number.MAX_SAFE_INTEGER,
        Number.MAX_SAFE_INTEGER);
      let next;
      if (mode === "randomize") next = Math.floor(random() * (upper - lower + 1)) + lower;
      else if (mode === "increment") next = Math.min(w.value + 1, upper);
      else if (mode === "decrement") next = Math.max(w.value - 1, lower);
      else continue; // unrecognised control mode; never guess
      if (!Number.isSafeInteger(next)) continue;
      w.value = next;
      counts[mode] = (counts[mode] || 0) + 1;
    }
  }
  return counts;
}

export function seedSummary(counts) {
  const parts = Object.entries(counts || {}).filter(([, n]) => n).map(([mode, n]) => `${n} ${mode}`);
  return parts.length ? `seeds advanced: ${parts.join(", ")}` : "";
}

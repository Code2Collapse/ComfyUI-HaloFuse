// Model download links an editor workflow carries (owner 2026-11-23, "Get missing models automatically"): official
// ComfyUI templates put [{name, url, directory}] in a loader node's properties.models (and some in the workflow's
// own "models" list). The panel sends them with the Workflow Check so the bridge can use the workflow's own link
// first. H2Vault vault nodes are skipped: a vault's inner models are secret and the vault provisions them itself.
// Side-effect-free ES module.

const MAX_LINKS = 1000;
const isVault = (type) => typeof type === "string" && type.startsWith("H2Vault");

function take(out, seen, list) {
  if (!Array.isArray(list)) return;
  for (const m of list) {
    if (out.length >= MAX_LINKS) return;
    if (!m || typeof m.name !== "string" || typeof m.url !== "string") continue;
    if (!m.url.startsWith("https://") || m.url.length > 2048 || !m.name || m.name.length > 512) continue;
    const key = `${m.name}\n${m.url}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ name: m.name, url: m.url, directory: typeof m.directory === "string" ? m.directory : null });
  }
}

/** @param {object} workflow the editor (UI-format) graph, e.g. app.graphToPrompt().workflow */
export function modelLinks(workflow) {
  const out = [];
  const seen = new Set();
  if (!workflow || typeof workflow !== "object") return out;
  take(out, seen, workflow.models);
  const graphs = [workflow, ...(Array.isArray(workflow.definitions?.subgraphs) ? workflow.definitions.subgraphs : [])];
  for (const g of graphs) {
    for (const node of Array.isArray(g?.nodes) ? g.nodes : []) {
      if (!node || isVault(node.type)) continue;
      take(out, seen, node.properties?.models);
    }
  }
  return out;
}

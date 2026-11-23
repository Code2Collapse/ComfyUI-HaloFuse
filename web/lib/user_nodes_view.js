// "My custom nodes" (owner 2026-11-23): pure helpers for the panel section. Entries come from the bridge
// (POST /halofuse/user_nodes/resolve): repository names, ids, pins and licences only, never a URL.
// Side-effect-free ES module.

const LICENCE_HINT = {
  permissive: "",
  copyleft: "copyleft: check its terms before commercial use; you decide for your own jobs",
  "network-copyleft": "network copyleft (AGPL): check its terms before commercial use; you decide for your own jobs",
  "non-commercial": "non-commercial: not for commercial work unless the author allows it",
  "no-licence": "no licence found: the author has not granted usage rights; you decide for your own jobs",
  unknown: "licence not recognised: read it on the pack's page; you decide for your own jobs",
};

/** {text, tone} for an entry's licence (informational only). */
export function licenceText(entry) {
  const cls = entry?.licence_class || "unknown";
  const name = entry?.licence || (cls === "no-licence" ? "no licence" : "licence unknown");
  const hint = LICENCE_HINT[cls] ?? LICENCE_HINT.unknown;
  return { text: hint ? `${name} (${hint})` : `${name} (${cls})`, tone: cls === "permissive" ? "neutral" : "warning" };
}

/** One line describing the pin: "owner/repo @ 1a2b3c4d5e6f (v1.2)" or "registry-id 1.5.0 (owner/repo)". */
export function entryText(entry) {
  if (!entry) return "";
  if (entry.source === "github") {
    return `${entry.repo} @ ${String(entry.commit || "").slice(0, 12)}${entry.ref ? ` (${entry.ref})` : ""}`;
  }
  return `${entry.id} ${entry.version}${entry.repo ? ` (${entry.repo})` : ""}`;
}

/** The textarea text for saved entries (their canonical lines). */
export function linesOf(entries) {
  return (entries || []).map((e) => e.line).filter(Boolean).join("\n");
}

/** `text` with `line` appended unless an equal line (case-insensitive) is already there. */
export function addLine(text, line) {
  const lines = String(text || "").split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
  const want = String(line || "").trim();
  if (!want || lines.some((l) => l.toLowerCase() === want.toLowerCase())) return lines.join("\n");
  return [...lines, want].join("\n");
}

/** Status sentence after a check. */
export function checkSummary(result) {
  const rows = result?.rows || [];
  const bad = rows.filter((r) => !r.ok).length;
  if (!rows.length) return { text: "The list is empty: no custom nodes are installed.", tone: "neutral" };
  if (bad) return { text: `${bad} line(s) need attention; nothing was saved.`, tone: "danger" };
  return { text: `${rows.length} pack(s) checked and pinned.`, tone: "success" };
}

// Cloud model library view model (flaws C1 / C6): turns the bridge's library report (GET /halofuse/library:
// items [{folder, name, cloud_size, local_size, status, verified}], duplicates, totals, truncated) into what the
// panel shows. Pure, side-effect-free ES module (tested under Node).

export const STATUS = {
  both: { label: "In the cloud", tone: "success" },
  cloud_only: { label: "Cloud only", tone: "neutral" },
  local_only: { label: "Only on this computer", tone: "warning" },
  size_mismatch: { label: "Differs from your copy", tone: "danger" },
};

export const FILTERS = [
  { value: "all", label: "All" },
  { value: "cloud", label: "In the cloud" },
  { value: "local_only", label: "Only on this computer" },
  { value: "size_mismatch", label: "Differs" },
];

export function statusOf(item) {
  const s = STATUS[item?.status] || { label: "Unknown", tone: "neutral" };
  if (item?.status === "both" && item.verified === "sha256") return { ...s, label: "In the cloud · checksum verified" };
  if (item?.status === "both" && item.verified === "size") return { ...s, label: "In the cloud · same size" };
  return s;
}

/** Items matching a status filter and free text (folder or file name, case-insensitive). */
export function filterItems(items, { filter = "all", text = "" } = {}) {
  const q = String(text || "").trim().toLowerCase();
  return (Array.isArray(items) ? items : []).filter((it) => {
    if (filter === "cloud" && !(it.status === "both" || it.status === "cloud_only")) return false;
    if (filter === "local_only" && it.status !== "local_only") return false;
    if (filter === "size_mismatch" && it.status !== "size_mismatch") return false;
    if (!q) return true;
    return `${it.folder}/${it.name}`.toLowerCase().includes(q);
  });
}

/** Counts per status + the report's totals, for the summary line. */
export function summarize(report) {
  const items = Array.isArray(report?.items) ? report.items : [];
  const count = { both: 0, cloud_only: 0, local_only: 0, size_mismatch: 0 };
  for (const it of items) if (it.status in count) count[it.status] += 1;
  const t = report?.totals || {};
  return {
    count,
    inCloud: count.both + count.cloud_only,
    cloudBytes: Number(t.cloud_bytes) || 0,
    localOnlyBytes: Number(t.local_only_bytes) || 0,
    duplicateBytes: Number(t.duplicate_bytes) || 0,
    duplicates: Array.isArray(report?.duplicates) ? report.duplicates.length : 0,
    truncated: !!report?.truncated,
  };
}

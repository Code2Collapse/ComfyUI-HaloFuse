// HaloFuse formatting helpers. Side-effect-free ES module (ComfyUI imports every .js under web/).

const isNum = (n) => typeof n === "number" && Number.isFinite(n);
const pad = (n, w = 2) => String(n).padStart(w, "0");

/** Human size with 1024 steps; "?" when unknown. */
export function fmtBytes(n) {
  if (!isNum(n) || n < 0) return "?";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  if (i === 0) return `${Math.round(v)} B`;
  return `${v.toFixed(i === 4 ? 2 : 1)} ${units[i]}`;
}

/** Duration in seconds → "4.2 s", "42 s", "3 m 05 s", "2 h 03 m"; "—" when unknown. */
export function fmtDuration(seconds) {
  if (!isNum(seconds) || seconds < 0) return "—";
  if (seconds < 10) return `${seconds.toFixed(1)} s`;
  if (seconds < 60) return `${Math.floor(seconds)} s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} m ${pad(Math.floor(seconds % 60))} s`;
  return `${Math.floor(seconds / 3600)} h ${pad(Math.floor((seconds % 3600) / 60))} m`;
}

/** Local wall-clock "HH:MM:SS.mmm" from epoch seconds. */
export function fmtClock(ts) {
  if (!isNum(ts)) return "--:--:--.---";
  const d = new Date(ts * 1000);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}`;
}

/** ISO-8601 UTC from epoch seconds; "" when unknown. */
export function fmtIsoUtc(ts) {
  if (!isNum(ts)) return "";
  return new Date(ts * 1000).toISOString();
}

/** "$0.03"; "—" when the amount is unknown (never invent a price). */
export function fmtUsd(amount) {
  if (!isNum(amount)) return "—";
  return `$${amount.toFixed(2)}`;
}

/** 0.4 → "40%"; "" when unknown. */
export function fmtPercent(fraction) {
  if (!isNum(fraction)) return "";
  return `${Math.round(Math.min(Math.max(fraction, 0), 1) * 100)}%`;
}

const LOCAL_DT = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });

/** Locale date + time for people ("20 Nov 2026, 09:31"); "" when unknown. */
export function fmtLocal(ts) {
  if (!isNum(ts)) return "";
  return LOCAL_DT.format(new Date(ts * 1000));
}

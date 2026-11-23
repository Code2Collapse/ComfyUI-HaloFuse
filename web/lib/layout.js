// HaloFuse console layout preferences. Side-effect-free ES module.
// Only window geometry and view preferences are ever persisted (localStorage key LAYOUT_KEY).
// Credentials, account data and log contents are never written to browser storage.

export const LAYOUT_KEY = "halofuse.console.layout.v1";
export const MIN_W = 420;
export const MIN_H = 260;

const MODES = ["normal", "max", "min"];
const TABS = ["logs", "activity", "progress", "telemetry", "problems", "summary"];
const SOURCE_KEYS = ["runner", "comfyui", "bridge", "provider"];

const fin = (n) => typeof n === "number" && Number.isFinite(n);
const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);

/** Large default window, anchored bottom-right with a 24 px margin. */
export function defaultGeometry(vp) {
  const margin = 8;
  if (vp.width < MIN_W + 2 * margin + 32 || vp.height < MIN_H + 2 * margin) {
    return { x: margin, y: margin, w: Math.max(vp.width - 2 * margin, 120), h: Math.max(vp.height - 2 * margin, 120) };
  }
  const w = Math.max(Math.min(1180, vp.width - 48), MIN_W);
  const h = Math.max(Math.min(760, vp.height - 120), MIN_H);
  return { x: Math.max(vp.width - w - 24, margin), y: Math.max(vp.height - h - 24, margin), w, h };
}

/** Keep a window inside the viewport and at least MIN size (or the viewport size when smaller). */
export function clampGeometry(g, vp, margin = 8) {
  if (!g || ![g.x, g.y, g.w, g.h].every(fin)) return defaultGeometry(vp);
  const maxW = Math.max(vp.width - 2 * margin, 120);
  const maxH = Math.max(vp.height - 2 * margin, 120);
  const w = Math.round(clamp(g.w, Math.min(MIN_W, maxW), maxW));
  const h = Math.round(clamp(g.h, Math.min(MIN_H, maxH), maxH));
  const x = Math.round(clamp(g.x, margin, Math.max(vp.width - w - margin, margin)));
  const y = Math.round(clamp(g.y, margin, Math.max(vp.height - h - margin, margin)));
  return { x, y, w, h };
}

/** Near-full-screen geometry. */
export function nearFullScreen(vp, margin = 16) {
  return { x: margin, y: margin, w: Math.max(vp.width - 2 * margin, 120), h: Math.max(vp.height - 2 * margin, 120) };
}

/** Whitelist + validate a layout object. Unknown keys are dropped. */
export function sanitizeLayout(obj, vp) {
  const o = obj && typeof obj === "object" && !Array.isArray(obj) ? obj : {};
  const src = o.sources && typeof o.sources === "object" ? o.sources : {};
  const sources = {};
  for (const k of SOURCE_KEYS) sources[k] = typeof src[k] === "boolean" ? src[k] : true;
  return {
    geometry: clampGeometry(o.geometry, vp),
    mode: MODES.includes(o.mode) ? o.mode : "normal",
    tab: TABS.includes(o.tab) ? o.tab : "logs",
    timestamps: typeof o.timestamps === "boolean" ? o.timestamps : true,
    wrap: typeof o.wrap === "boolean" ? o.wrap : false,
    open: o.open === true, // was the window open when the page went away (the floating panel reopens; owner 2026-11-23)
    sources,
  };
}

/** Read preferences; any failure gives defaults. `key` lets a second window keep its own geometry; `initial(vp)`
 *  is that window's starting geometry when nothing is stored yet. */
export function loadLayout(storage, vp, key = LAYOUT_KEY, initial = null) {
  const fresh = () => sanitizeLayout(initial ? { geometry: initial(vp) } : null, vp);
  try {
    const raw = storage ? storage.getItem(key) : null;
    return raw ? sanitizeLayout(JSON.parse(raw), vp) : fresh();
  } catch {
    return fresh();
  }
}

/** Write preferences (sanitised); returns false when storage is unavailable. */
export function saveLayout(storage, layout, vp, key = LAYOUT_KEY) {
  try {
    if (!storage) return false;
    storage.setItem(key, JSON.stringify(sanitizeLayout(layout, vp)));
    return true;
  } catch {
    return false;
  }
}

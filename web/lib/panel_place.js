// HaloFuse panel location (owner 2026-11-21): Sidebar (default) / Floating window / Both.
// Pure logic, side-effect-free ES module (tested under Node). The DOM side lives in halofuse.js.
// Browser storage holds only the launcher button's position (a per-viewer convenience).

export const PANEL_MODES = ["sidebar", "floating", "both"];
export const PANEL_SETTING = "HaloFuse.Panel.Location";
export const LAUNCHER_KEY = "halofuse.panel.launcher.v1";
export const PANEL_LAYOUT_KEY = "halofuse.panel.layout.v1";
const SIZE = 48;
const MARGIN = 8;

export function normalizeMode(v) {
  const m = String(v ?? "").toLowerCase();
  return PANEL_MODES.includes(m) ? m : "sidebar";
}

export function wants(mode) {
  const m = normalizeMode(mode);
  return { sidebar: m !== "floating", floating: m !== "sidebar" };
}

/** Starting geometry of the floating panel window: panel-width, under the top bar, left of the launcher column.
 *  (layout.clampGeometry still keeps it inside the viewport and at least its minimum size.) */
export function panelGeometry(vp) {
  const w = 420;
  const h = Math.max(Math.min(760, vp.height - 140), 260);
  return { x: Math.max(vp.width - w - SIZE - 3 * MARGIN, MARGIN), y: 96, w, h };
}

/** Default launcher spot: right edge, vertically centred (clear of the top bar and the bottom panel). */
export function clampLauncher(pos, vp) {
  const def = { x: vp.w - SIZE - MARGIN, y: Math.round(vp.h / 2 - SIZE / 2) };
  const x = Number.isFinite(Number(pos?.x)) && pos?.x !== null ? Number(pos.x) : def.x;
  const y = Number.isFinite(Number(pos?.y)) && pos?.y !== null ? Number(pos.y) : def.y;
  return {
    x: Math.round(Math.min(Math.max(x, MARGIN), vp.w - SIZE - MARGIN)),
    y: Math.round(Math.min(Math.max(y, MARGIN), vp.h - SIZE - MARGIN)),
  };
}

export function loadLauncher(storage, vp) {
  try {
    const raw = storage ? storage.getItem(LAUNCHER_KEY) : null;
    return clampLauncher(raw ? JSON.parse(raw) : null, vp);
  } catch {
    return clampLauncher(null, vp);
  }
}

export function saveLauncher(storage, pos, vp) {
  try {
    if (!storage) return false;
    storage.setItem(LAUNCHER_KEY, JSON.stringify(clampLauncher(pos, vp)));
    return true;
  } catch {
    return false;
  }
}

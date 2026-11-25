// HaloFuse stylesheet (injected once by the entry). Side-effect-free ES module.
// Colours follow the active ComfyUI palette through its CSS variables, with dark fallbacks.
// Every class is prefixed hf- so nothing here styles ComfyUI's own elements.

export const HF_CSS = `
.hf-root, .hf-console, .hf-pill {
  --hf-bg: var(--comfy-menu-bg, #1f1f1f);
  --hf-bg2: var(--comfy-menu-secondary-bg, #292929);
  --hf-input: var(--comfy-input-bg, #222);
  --hf-fg: var(--fg-color, #e6e6e6);
  --hf-input-fg: var(--input-text, #e6e6e6);
  --hf-muted: var(--descrip-text, #9a9a9a);
  --hf-border: var(--border-color, #3d3d3d);
  --hf-accent: var(--p-primary-color, #4f8cff);
  --hf-accent-fg: var(--p-primary-contrast-color, #fff);
  --hf-err: #f87171; --hf-warn: #f5b942; --hf-ok: #4ade80; --hf-active: #60a5fa;
  --hf-runner: #2dd4bf; --hf-comfy: #a3a3a3; --hf-bridge: #c4a1ff; --hf-provider: #7cb4ff;
  /* Chromium on Windows ignores ui-monospace; Consolas renders lighter than Cascadia Mono at 12px */
  --hf-mono: ui-monospace, "SF Mono", Menlo, Consolas, "Cascadia Mono", "DejaVu Sans Mono", monospace;
  color: var(--hf-fg);
  font: 13px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif;
  box-sizing: border-box;
}
.hf-root *, .hf-console *, .hf-pill * { box-sizing: border-box; }
/* author display rules (grid/flex) must never defeat the hidden attribute */
.hf-root [hidden], .hf-console [hidden], .hf-pill[hidden] { display: none !important; }
/* the HaloFuse mark: a single halo ring, the only decorative element */
.hf-mark { display: inline-block; flex: none; width: 14px; height: 14px; border-radius: 50%;
  background: conic-gradient(from 200deg, #5eead4, #818cf8, #c084fc, #5eead4);
  -webkit-mask: radial-gradient(circle, transparent 3.6px, #000 4.2px); mask: radial-gradient(circle, transparent 3.6px, #000 4.2px); }
.hf-root :focus-visible, .hf-console :focus-visible, .hf-pill:focus-visible { outline: 2px solid var(--hf-accent); outline-offset: 1px; }

/* ---------- shared controls ---------- */
.hf-btn { appearance: none; border: 1px solid var(--hf-border); background: var(--hf-bg2); color: var(--hf-fg);
  border-radius: 6px; padding: 5px 10px; font: inherit; cursor: pointer; white-space: nowrap; }
.hf-btn:hover:not(:disabled) { border-color: var(--hf-muted); }
.hf-btn:disabled { opacity: .45; cursor: not-allowed; }
.hf-btn.hf-primary { background: var(--hf-accent); border-color: var(--hf-accent); color: var(--hf-accent-fg); font-weight: 600; }
.hf-btn.hf-danger { border-color: color-mix(in srgb, var(--hf-err) 60%, transparent); color: var(--hf-err); }
.hf-btn.hf-danger.hf-solid { background: color-mix(in srgb, var(--hf-err) 22%, var(--hf-bg2)); }
.hf-btn.hf-ghost { background: transparent; border-color: transparent; }
.hf-btn.hf-ghost:hover:not(:disabled) { background: var(--hf-bg2); }
.hf-btn[aria-pressed="true"] { background: color-mix(in srgb, var(--hf-accent) 22%, var(--hf-bg2)); border-color: color-mix(in srgb, var(--hf-accent) 70%, transparent); }
.hf-btn.hf-icon { padding: 3px 7px; min-width: 28px; }
.hf-input, .hf-select { width: 100%; background: var(--hf-input); color: var(--hf-input-fg); border: 1px solid var(--hf-border);
  border-radius: 6px; padding: 5px 8px; font: inherit; min-width: 0; }
.hf-field { display: grid; gap: 3px; margin: 0 0 10px; }
.hf-field > span { color: var(--hf-muted); font-size: 12px; }
.hf-field.hf-check { grid-template-columns: auto 1fr; align-items: start; column-gap: 8px; }
.hf-field.hf-check > input { margin: 2px 0 0; }
.hf-hint { color: var(--hf-muted); font-size: 12px; margin: -4px 0 10px; }
.hf-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.hf-row > .hf-grow { flex: 1 1 auto; min-width: 0; }
.hf-status { font-size: 12px; min-height: 17px; }
.hf-tone-danger { color: var(--hf-err); } .hf-tone-warning { color: var(--hf-warn); }
.hf-tone-success { color: var(--hf-ok); } .hf-tone-active { color: var(--hf-active); } .hf-tone-neutral { color: var(--hf-muted); }
.hf-badge { display: inline-flex; align-items: center; gap: 4px; border: 1px solid currentColor; border-radius: 999px;
  padding: 0 7px; font-size: 11px; line-height: 17px; white-space: nowrap; }
.hf-bar { height: 6px; background: var(--hf-bg2); border-radius: 3px; overflow: hidden; }
.hf-bar > div { height: 100%; background: var(--hf-accent); width: 0; transition: width .3s ease; }
.hf-bar.hf-indeterminate > div { width: 30%; animation: hf-slide 1.4s ease-in-out infinite; }
@keyframes hf-slide { 0% { margin-left: -30%; } 100% { margin-left: 100%; } }
@media (prefers-reduced-motion: reduce) { .hf-bar > div { transition: none; } .hf-bar.hf-indeterminate > div { animation: none; width: 100%; opacity: .4; } }
.hf-empty { color: var(--hf-muted); padding: 16px; text-align: center; }
.hf-kv { display: grid; grid-template-columns: max-content 1fr; gap: 4px 12px; font-size: 12px; }
.hf-kv dt { color: var(--hf-muted); } .hf-kv dd { margin: 0; min-width: 0; overflow-wrap: anywhere; }
.hf-mono { font-family: var(--hf-mono); font-size: 12px; font-weight: 400; }
.hf-sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }

/* ---------- compact panel (sidebar tab) ---------- */
.hf-root { padding: 12px 12px 24px; height: 100%; overflow-y: auto; overflow-x: hidden; overscroll-behavior: contain; background: var(--hf-bg); box-sizing: border-box; }
/* nothing may widen the panel past the sidebar (a long select option scrolled the whole panel sideways) */
.hf-root .hf-input, .hf-root .hf-select { max-width: 100%; min-width: 0; box-sizing: border-box; }
.hf-root .hf-field, .hf-root .hf-section, .hf-root .hf-list { min-width: 0; max-width: 100%; }
.hf-root h2 { font-size: 15px; margin: 0; font-weight: 650; letter-spacing: .01em; display: flex; align-items: center; gap: 8px; }
.hf-head { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; margin-bottom: 12px; }
.hf-version { color: var(--hf-muted); font-size: 11px; }
.hf-section { border-top: 1px solid var(--hf-border); padding-top: 10px; margin-top: 10px; }
.hf-warm { display: grid; gap: 6px; } .hf-warm > .hf-hint { display: grid; gap: 2px; margin: 0; }
.hf-warm .hf-row > .hf-grow { flex: 1 1 140px; } .hf-warm .hf-row > .hf-grow > .hf-field { margin-bottom: 4px; } .hf-warm details .hf-hint { display: grid; gap: 2px; }
.hf-root details > summary { cursor: pointer; list-style: none; display: flex; align-items: center; gap: 8px;
  font-weight: 600; padding: 2px 0 6px; }
.hf-root details > summary::-webkit-details-marker { display: none; }
.hf-root details > summary::before { content: "\\25B8"; color: var(--hf-muted); font-size: 11px; transition: transform .15s; }
.hf-root details[open] > summary::before { transform: rotate(90deg); }
.hf-root details > summary .hf-badge { margin-left: auto; font-weight: 400; }
.hf-jobcard { border: 1px solid var(--hf-border); border-radius: 8px; padding: 10px; display: grid; gap: 6px; background: var(--hf-bg2); }
.hf-jobcard .hf-jobtitle { display: flex; justify-content: space-between; gap: 8px; align-items: center; font-weight: 600; }
.hf-jobcard .hf-meta { color: var(--hf-muted); font-size: 12px; overflow-wrap: anywhere; }
.hf-thumb { max-width: 100%; max-height: 180px; border-radius: 6px; border: 1px solid var(--hf-border); display: block; }
.hf-list { display: grid; gap: 4px; margin: 6px 0; }
.hf-item { display: flex; gap: 6px; align-items: center; border: 1px solid var(--hf-border); border-radius: 6px;
  padding: 4px 6px; font-size: 12px; background: var(--hf-bg2); }
.hf-item .hf-grow { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
/* owner 2026-11-23: Workflow Check lines are read in full - wrap, never cut */
.hf-list .hf-item { align-items: flex-start; flex-wrap: wrap; }
.hf-list .hf-item .hf-grow { white-space: normal; overflow-wrap: anywhere; text-overflow: clip; }
.hf-consent { border: 1px solid var(--hf-warn); border-radius: 8px; padding: 10px; display: grid; gap: 8px; }

/* ---------- floating console ---------- */
.hf-console { position: fixed; z-index: 1000; display: flex; flex-direction: column; background: var(--hf-bg);
  border: 1px solid var(--hf-border); border-radius: 10px; box-shadow: 0 18px 48px rgba(0,0,0,.45); overflow: hidden;
  min-width: 0; min-height: 0; }
.hf-console[hidden] { display: none; }
.hf-console.hf-dragging, .hf-console.hf-resizing { user-select: none; }
.hf-win-head { display: flex; align-items: center; gap: 8px; padding: 6px 6px 6px 12px; background: var(--hf-bg2);
  border-bottom: 1px solid var(--hf-border); cursor: grab; touch-action: none; }
.hf-console.hf-dragging .hf-win-head { cursor: grabbing; }
.hf-win-title { font-weight: 650; white-space: nowrap; display: inline-flex; align-items: center; gap: 8px; outline: none; }
.hf-win-job { color: var(--hf-muted); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; flex: 1 1 auto; }
.hf-win-head .hf-btn { cursor: pointer; }
.hf-tabs { flex: none; display: flex; gap: 2px; padding: 0 8px; border-bottom: 1px solid var(--hf-border); background: var(--hf-bg); overflow-x: auto; }
/* flex: none - a tall pane (the technical detail) squeezed the tab row and cut its labels (L9.338) */
.hf-tab { appearance: none; background: none; border: 0; border-bottom: 2px solid transparent; color: var(--hf-muted);
  padding: 7px 10px 6px; font: inherit; cursor: pointer; white-space: nowrap; }
.hf-tab[aria-selected="true"] { color: var(--hf-fg); border-bottom-color: var(--hf-accent); }
.hf-tab .hf-count { margin-left: 5px; font-size: 11px; }
.hf-tabpanel { flex: 1 1 auto; min-height: 0; display: flex; flex-direction: column; overflow-y: auto; }
/* owner 2026-11-24: the Live tab could not scroll - its content (stage + node cards) is taller than the window */
.hf-console .hf-tabpanel > .hf-live { flex: 0 0 auto; overflow: visible; }
.hf-tabpanel[hidden] { display: none; }
.hf-scroll { overflow: auto; overscroll-behavior: contain; padding: 12px; flex: 1 1 auto; min-height: 0; }
/* Live tab re-render: a 1 px marker at the old content height keeps the scroll position from being clamped */
.hf-scroll.hf-scroll-hold { position: relative; }
.hf-scroll-marker { position: absolute; left: 0; width: 1px; height: 1px; pointer-events: none; visibility: hidden; }
.hf-toolbar { display: flex; flex-wrap: wrap; gap: 6px 8px; align-items: center; padding: 8px 10px; border-bottom: 1px solid var(--hf-border); background: var(--hf-bg); }
.hf-toolbar .hf-btn, .hf-toolbar .hf-select, .hf-toolbar .hf-input { font-size: 12px; padding: 4px 9px; height: 28px; }
.hf-toolbar .hf-search { flex: 1 1 200px; max-width: 360px; }
.hf-chips { display: inline-flex; gap: 10px; align-items: center; padding: 0 4px; }
.hf-spacer { flex: 1 1 0; }
.hf-btn-group { display: inline-flex; gap: 4px; flex-wrap: wrap; }
.hf-toolbar .hf-sep { width: 1px; align-self: stretch; background: var(--hf-border); margin: 0 2px; }
.hf-toolbar .hf-select { width: auto; }
.hf-chip { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; color: var(--hf-muted); cursor: pointer; user-select: none; }
.hf-chip input { margin: 0; accent-color: var(--hf-accent); }
.hf-filter-error { color: var(--hf-err); font-size: 12px; padding: 0 8px 6px; }

.hf-logwrap { position: relative; flex: 1 1 auto; min-height: 60px; display: flex; flex-direction: column; }
.hf-log-empty { position: absolute; inset: 0; display: grid; place-items: center; padding: 24px; text-align: center;
  color: var(--hf-muted); font-size: 13px; pointer-events: none; }
.hf-loglist { position: relative; flex: 1 1 auto; min-height: 60px; overflow: auto; font-family: var(--hf-mono);
  font-size: 12px; font-weight: 400; background: var(--hf-input); contain: strict; overscroll-behavior: contain; }
.hf-loglist:focus-visible { outline: 2px solid var(--hf-accent); outline-offset: -2px; }
.hf-vl-sizer { position: absolute; top: 0; left: 0; width: 1px; }
.hf-vl-row { position: absolute; top: 0; left: 0; right: 0; display: flex; align-items: center; gap: 8px; padding: 0 8px;
  white-space: pre; overflow: hidden; border-left: 3px solid transparent; cursor: default; will-change: transform; }
.hf-vl-row[aria-selected="true"] { background: color-mix(in srgb, var(--hf-accent) 18%, transparent); }
.hf-vl-row:hover { background: color-mix(in srgb, var(--hf-fg) 6%, transparent); }
.hf-vl-row.hf-l-error { border-left-color: var(--hf-err); background: color-mix(in srgb, var(--hf-err) 9%, transparent); }
.hf-vl-row.hf-l-warning { border-left-color: var(--hf-warn); }
.hf-vl-row.hf-k-meta { color: var(--hf-muted); }
.hf-c-ts { color: var(--hf-muted); flex: none; font-variant-numeric: tabular-nums; }
.hf-c-src { flex: none; width: 64px; font-size: 11px; text-transform: none; }
.hf-src-runner { color: var(--hf-runner); } .hf-src-comfyui { color: var(--hf-comfy); }
.hf-src-bridge { color: var(--hf-bridge); } .hf-src-provider { color: var(--hf-provider); }
.hf-c-lvl { flex: none; width: 44px; font-size: 11px; font-weight: 600; }
.hf-l-error .hf-c-lvl { color: var(--hf-err); } .hf-l-warning .hf-c-lvl { color: var(--hf-warn); }
.hf-c-text { overflow: hidden; text-overflow: ellipsis; min-width: 0; color: color-mix(in srgb, var(--hf-fg) 88%, transparent); }
.hf-l-error .hf-c-text, .hf-l-warning .hf-c-text { color: var(--hf-fg); }
.hf-k-meta .hf-c-text { color: var(--hf-muted); }
.hf-console.hf-no-ts .hf-c-ts { display: none; }
.hf-logstatus { display: flex; gap: 12px; align-items: center; padding: 4px 8px; font-size: 12px; color: var(--hf-muted);
  border-top: 1px solid var(--hf-border); flex-wrap: wrap; }
.hf-logstatus .hf-grow { flex: 1 1 auto; }
.hf-detail { border-top: 1px solid var(--hf-border); max-height: 45%; display: flex; flex-direction: column; min-height: 0; }
.hf-detail[hidden] { display: none; }
.hf-detail-head { display: flex; gap: 8px; align-items: center; padding: 6px 8px; font-size: 12px; color: var(--hf-muted); flex-wrap: wrap; }
.hf-detail pre { margin: 0; padding: 8px 10px; overflow: auto; white-space: pre-wrap; word-break: break-word;
  font-family: var(--hf-mono); font-size: 12px; background: var(--hf-input); flex: 1 1 auto; min-height: 0; }

.hf-timeline { display: grid; gap: 4px; margin: 8px 0 16px; }
.hf-tl-row { display: grid; grid-template-columns: minmax(120px, 200px) 1fr 80px; gap: 10px; align-items: center; font-size: 12px; }
.hf-tl-track { position: relative; height: 10px; background: var(--hf-bg2); border-radius: 3px; }
.hf-tl-span { position: absolute; top: 0; bottom: 0; background: var(--hf-accent); border-radius: 3px; min-width: 2px; }
.hf-tl-row.hf-running .hf-tl-span { background: var(--hf-active); }
.hf-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.hf-table th { text-align: left; color: var(--hf-muted); font-weight: 500; padding: 4px 8px; border-bottom: 1px solid var(--hf-border); position: sticky; top: 0; background: var(--hf-bg); }
.hf-table td { padding: 4px 8px; border-bottom: 1px solid color-mix(in srgb, var(--hf-border) 50%, transparent); }
.hf-table tr.hf-clickable { cursor: pointer; } .hf-table tr.hf-clickable:hover td { background: var(--hf-bg2); }
.hf-num { text-align: right; font-variant-numeric: tabular-nums; }
.hf-card { border: 1px solid var(--hf-border); border-radius: 8px; padding: 10px 12px; margin-bottom: 12px; display: grid; gap: 6px; }
.hf-card.hf-card-danger { border-color: var(--hf-err); }
.hf-card h3 { margin: 0; font-size: 13px; }
/* L9.338: errors are read in full - wrap, never cut; the technical block scrolls and can be selected */
.hf-wrap { white-space: pre-wrap; overflow-wrap: anywhere; min-width: 0; }
.hf-er-p { margin: 0; }
.hf-er-steps { margin: 0; padding-left: 20px; display: grid; gap: 2px; }
.hf-er-actions { flex-wrap: wrap; gap: 6px; }
.hf-er-node { flex-wrap: wrap; gap: 6px; align-items: center; }
.hf-tech { margin: 2px 0 0; padding: 8px 10px; max-height: 50vh; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere;
  font-family: var(--hf-mono); font-size: 12px; background: var(--hf-input); color: var(--hf-input-fg);
  border: 1px solid var(--hf-border); border-radius: 6px; user-select: text; -webkit-user-select: text; }
.hf-tech:focus-visible { outline: 2px solid var(--hf-accent); outline-offset: 1px; }
.hf-problems .hf-item { align-items: flex-start; }
.hf-problems .hf-item .hf-grow { white-space: pre-wrap; overflow-wrap: anywhere; text-overflow: clip; overflow: visible; }
.hf-er-detail { color: var(--hf-muted); margin-top: 2px; font-family: var(--hf-mono); font-size: 11px; }
.hf-gallery { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 8px; align-items: start; }
.hf-gallery audio { margin: 8px 0 0; }
.hf-gallery figure { margin: 0; border: 1px solid var(--hf-border); border-radius: 6px; overflow: hidden; background: var(--hf-input); }
.hf-gallery img, .hf-gallery video { width: 100%; display: block; max-height: 200px; object-fit: contain; background: #000; }
.hf-gallery audio { width: 100%; display: block; }
.hf-gallery figcaption { font-size: 11px; color: var(--hf-muted); padding: 4px 6px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.hf-h { font-size: 12px; font-weight: 650; color: var(--hf-muted); margin: 14px 0 6px; }
.hf-h:first-child { margin-top: 0; }

.hf-rs { position: absolute; z-index: 2; touch-action: none; }
.hf-rs-n, .hf-rs-s { left: 8px; right: 8px; height: 6px; cursor: ns-resize; }
.hf-rs-e, .hf-rs-w { top: 8px; bottom: 8px; width: 6px; cursor: ew-resize; }
.hf-rs-n { top: -3px; } .hf-rs-s { bottom: -3px; } .hf-rs-e { right: -3px; } .hf-rs-w { left: -3px; }
.hf-rs-ne, .hf-rs-nw, .hf-rs-se, .hf-rs-sw { width: 12px; height: 12px; }
.hf-rs-ne { top: -3px; right: -3px; cursor: nesw-resize; } .hf-rs-sw { bottom: -3px; left: -3px; cursor: nesw-resize; }
.hf-rs-nw { top: -3px; left: -3px; cursor: nwse-resize; } .hf-rs-se { bottom: -3px; right: -3px; cursor: nwse-resize; }
.hf-console.hf-max .hf-rs { display: none; }

.hf-pill { position: fixed; z-index: 1000; right: 16px; bottom: 16px; display: inline-flex; gap: 8px; align-items: center;
  background: var(--hf-bg2); border: 1px solid var(--hf-border); border-radius: 999px; padding: 6px 12px 6px 10px;
  box-shadow: 0 6px 18px rgba(0,0,0,.35); cursor: pointer; max-width: min(420px, calc(100vw - 32px)); font: inherit; }
.hf-pill[hidden] { display: none; }
/* floating panel (Panel location = Floating window / Both): launcher button + panel-sized window */
.hf-launcher { position: fixed; z-index: 999; width: 48px; height: 48px; border-radius: 50%; display: grid; place-items: center;
  background: var(--comfy-menu-secondary-bg, #292929); color: var(--fg-color, #e6e6e6); border: 1px solid var(--border-color, #3d3d3d);
  box-shadow: 0 6px 18px rgba(0,0,0,.35); cursor: grab; touch-action: none; padding: 0; font-size: 18px; }
.hf-launcher:hover { border-color: var(--p-primary-color, #4f8cff); }
.hf-launcher:active { cursor: grabbing; }
.hf-launcher:focus-visible { outline: 2px solid var(--p-primary-color, #4f8cff); outline-offset: 2px; }
.hf-launcher[aria-pressed="true"] { border-color: var(--p-primary-color, #4f8cff); color: var(--p-primary-color, #4f8cff); }
.hf-launcher-dot { position: absolute; top: 6px; right: 6px; width: 8px; height: 8px; border-radius: 50%; background: #60a5fa; display: none; }
.hf-launcher[data-busy="1"] .hf-launcher-dot { display: block; }
@media (prefers-reduced-motion: no-preference) { .hf-launcher[data-busy="1"] .hf-launcher-dot { animation: hf-pulse 1.6s ease-in-out infinite; } }
@keyframes hf-pulse { 50% { opacity: .35; } }
.hf-panel-window .hf-tabpanel { overflow: hidden; }
.hf-panel-window .hf-root { height: 100%; }
.hf-panel-window .hf-root > .hf-head { display: none; } /* the window title already says HaloFuse */
.hf-pill .hf-pill-text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.hf-dot { width: 8px; height: 8px; border-radius: 50%; background: currentColor; flex: none; }

@media (max-width: 640px) {
  .hf-tl-row { grid-template-columns: 110px 1fr 64px; }
  .hf-toolbar .hf-search { flex-basis: 100%; max-width: none; }
}

/* Live view (PREVIEW_DESIGN 4): the frame is the one prominent element; everything around it stays quiet */
.hf-sr-only { position: absolute; width: 1px; height: 1px; margin: -1px; padding: 0; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0; }
.hf-live { display: flex; flex-direction: column; gap: 8px; padding: 10px 12px; min-height: 0; flex: 1 1 auto; overflow: auto; }
.hf-live-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.hf-live-exp { color: var(--hf-muted); font-size: 12px; flex: 1 1 200px; }
.hf-live-actions { display: flex; gap: 6px; flex-wrap: wrap; }
.hf-live-body { display: grid; grid-template-columns: minmax(0, 1fr) minmax(200px, 280px); gap: 12px; min-height: 0; flex: 1 1 auto; }
.hf-live-stage { position: relative; margin: 0; min-height: 200px; background: #111 repeating-conic-gradient(#1a1a1a 0 25%, #141414 0 50%) 0 0 / 16px 16px;
  border: 1px solid var(--hf-border); border-radius: 6px; display: flex; align-items: center; justify-content: center; overflow: hidden; }
.hf-live-frame, .hf-live-canvas { display: block; width: 100%; height: 100%; max-height: 62vh; object-fit: contain; image-rendering: auto; }
.hf-live-stale .hf-live-frame, .hf-live-stale .hf-thumb { opacity: .55; }
.hf-live-empty { color: var(--hf-muted); padding: 24px; text-align: center; max-width: 42ch; }
.hf-live-step { position: absolute; left: 8px; right: 8px; bottom: 8px; height: 18px; display: flex; align-items: center;
  background: color-mix(in srgb, #000 55%, transparent); border-radius: 4px; overflow: hidden; }
.hf-live-step-fill { position: absolute; left: 0; top: 0; bottom: 0; background: color-mix(in srgb, var(--hf-accent) 45%, transparent); }
.hf-live-step span { position: relative; margin-left: auto; padding: 0 6px; font: 11px/1 var(--hf-mono); color: #fff; }
.hf-live-side { display: flex; flex-direction: column; gap: 8px; min-width: 0; }
.hf-live-info { display: grid; grid-template-columns: auto 1fr; gap: 2px 10px; margin: 0; font-size: 12px; }
.hf-live-info dt { color: var(--hf-muted); }
.hf-live-info dd { margin: 0; overflow-wrap: anywhere; }
.hf-live-status { margin: 0; font-size: 12px; color: var(--hf-muted); min-height: 1.4em; }
.hf-live-h { margin: 4px 0 0; font-size: 12px; font-weight: 600; color: var(--hf-muted); }
.hf-live-thumbs, .hf-live-strip { display: flex; gap: 6px; flex-wrap: wrap; }
.hf-live-strip { flex-wrap: nowrap; overflow-x: auto; padding-bottom: 2px; }
.hf-live-thumb { appearance: none; padding: 0; width: 56px; height: 56px; flex: none; border: 1px solid var(--hf-border); border-radius: 4px;
  background: #111; overflow: hidden; cursor: pointer; }
.hf-live-thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
.hf-live-thumb video { width: 100%; height: 100%; object-fit: cover; display: block; pointer-events: none; }
.hf-live-thumb.hf-live-video { position: relative; }
.hf-live-thumb.hf-live-video::after { content: "▶"; position: absolute; right: 3px; bottom: 2px; font-size: 10px; color: #fff;
  text-shadow: 0 0 2px #000; }
.hf-live-missing { color: var(--hf-muted); font-size: 12px; text-align: center; padding: 24px; max-width: 42ch; min-height: 120px;
  display: flex; align-items: center; justify-content: center; }
.hf-live-missing.hf-live-missing-sm { min-height: 0; width: 100%; height: 100%; padding: 2px; font-size: 10px; max-width: none;
  border: 1px dashed var(--hf-border); box-sizing: border-box; border-radius: 3px; }
.hf-live-thumb.hf-on { border-color: var(--hf-accent); box-shadow: 0 0 0 1px var(--hf-accent); }
.hf-live-thumb.hf-live-mask { border-style: dashed; }
.hf-live-modes { border: 1px solid var(--hf-border); border-radius: 4px; margin: 0; padding: 4px 8px 6px; display: flex; gap: 10px; flex-wrap: wrap; font-size: 12px; }
.hf-live-modes legend { color: var(--hf-muted); padding: 0 4px; }
.hf-thumbwrap { position: relative; margin: 6px 0 0; }
@media (max-width: 720px) { .hf-live-body { grid-template-columns: 1fr; } }
.hf-tone-info { color: var(--hf-runner); }
.hf-nodes { margin-top: 10px; display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 8px; align-items: start; }
.hf-nodes > .hf-nodes-wide { grid-column: 1 / -1; margin: 0; }
.hf-nodes-compact { margin-top: 6px; display: grid; grid-template-columns: repeat(auto-fill, minmax(110px, 1fr)); gap: 6px; }
.hf-nodes-compact > .hf-hint { grid-column: 1 / -1; margin: 0; }
.hf-node-card { border: 1px solid var(--hf-border); border-radius: 6px; padding: 6px 8px; background: var(--hf-input); min-width: 0; }
.hf-node-card.hf-node-state-running { border-color: var(--hf-active); }
.hf-node-card.hf-node-state-failed { border-color: var(--hf-err); }
.hf-node-card > .hf-hint { margin: 2px 0 4px; }
.hf-node-head { display: flex; align-items: center; gap: 6px; min-width: 0; }
.hf-node-title { flex: 1 1 auto; min-width: 0; font-size: 12px; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.hf-node-items { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 6px; }
.hf-node-item { margin: 0; min-width: 0; }
.hf-node-item img, .hf-node-item video { width: 100%; max-height: 220px; object-fit: contain; display: block; background: #000; border-radius: 4px; }
.hf-nodes-compact .hf-node-item img { max-height: 90px; margin-top: 4px; }
.hf-node-item audio { width: 100%; display: block; }
.hf-node-item figcaption { font-size: 11px; color: var(--hf-muted); padding-top: 2px; overflow-wrap: anywhere; }
.hf-node-item img.hf-node-mask { background: repeating-conic-gradient(#3a3a3a 0% 25%, #2a2a2a 0% 50%) 50% / 16px 16px; }
.hf-node-item.hf-node-text { grid-column: 1 / -1; }
.hf-node-pre { margin: 0; padding: 6px; max-height: 160px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; font-size: 12px;
  background: var(--hf-bg2); border-radius: 4px; }
.hf-node-chip { display: inline-block; padding: 2px 8px; border: 1px solid var(--hf-border); border-radius: 999px; font-size: 11px;
  color: var(--hf-text, inherit); overflow-wrap: anywhere; }
.hf-node-pending { color: var(--hf-muted); border-style: dashed; }
`;

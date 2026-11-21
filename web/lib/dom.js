// HaloFuse DOM helpers. Side-effect-free ES module: nothing runs at import.
// Dynamic text is always set with textContent / attributes, never parsed as HTML.

/**
 * h("button", {className: "hf-btn", onclick, attrs: {"aria-label": "Close"}}, "text", childEl)
 * props: className, text, title, type, value, hidden, disabled, dataset{}, attrs{}, style{}, on<event>.
 */
export function h(doc, tag, props = {}, ...children) {
  const el = doc.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === undefined || v === null) continue;
    if (k === "className") el.className = v;
    else if (k === "text") el.textContent = String(v);
    else if (k === "attrs") for (const [a, av] of Object.entries(v)) { if (av !== null && av !== undefined && av !== false) el.setAttribute(a, av === true ? "" : String(av)); }
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k === "style") Object.assign(el.style, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else el[k] = v;
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Object && "nodeType" in c ? c : doc.createTextNode(String(c)));
  }
  return el;
}

/** Bind h to a document: const H = bindH(document); H("div", {...}). */
export function bindH(doc) {
  return (tag, props, ...children) => h(doc, tag, props, ...children);
}

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
}

/** Copy text; falls back to a hidden textarea when the async clipboard is unavailable (non-secure origin). */
export async function copyText(win, text) {
  try {
    if (win.navigator?.clipboard?.writeText) {
      await win.navigator.clipboard.writeText(text);
      return true;
    }
  } catch { /* fall through */ }
  const doc = win.document;
  const ta = doc.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.position = "fixed";
  ta.style.left = "-9999px";
  doc.body.append(ta);
  ta.select();
  let ok = false;
  try { ok = doc.execCommand("copy"); } catch { ok = false; }
  ta.remove();
  return ok;
}

/** Offer text as a file download (no server round trip, no storage). */
export function downloadText(win, filename, text, mime = "text/plain;charset=utf-8") {
  const doc = win.document;
  const url = win.URL.createObjectURL(new win.Blob([text], { type: mime }));
  const a = doc.createElement("a");
  a.href = url;
  a.download = filename;
  a.rel = "noopener";
  a.style.display = "none";
  doc.body.append(a);
  a.click();
  a.remove();
  win.setTimeout(() => win.URL.revokeObjectURL(url), 30_000);
}

const STYLE_ID = "halofuse-styles";

export function ensureStyles(doc, css) {
  let el = doc.getElementById(STYLE_ID);
  if (!el) {
    el = doc.createElement("style");
    el.id = STYLE_ID;
    doc.head.append(el);
  }
  if (el.textContent !== css) el.textContent = css;
  return el;
}

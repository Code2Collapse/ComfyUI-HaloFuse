// HaloFuse console log model: a de-duplicating store keyed by server sequence number plus an
// incrementally filtered view. Side-effect-free ES module. Built to stay fast at 10^6 entries:
// ingest is O(new entries), counts are O(1), filtering runs in time slices (no long tasks).

import { fmtClock } from "./format.js";
import { describeEntry } from "./event_text.js";

export const LEVELS = ["debug", "info", "warning", "error"];
export const SOURCES = ["runner", "comfyui", "bridge", "provider"];

const LEVEL_ALIASES = { warn: "warning", err: "error", critical: "error", fatal: "error", trace: "debug" };
const ERROR_RE = /\b(Traceback|Error:|ERROR\b|Exception\b)/;
const WARN_RE = /\b(WARNING|Warning:|DeprecationWarning)\b/;
const LEVEL_INDEX = { debug: 0, info: 1, warning: 2, error: 3 };

function inferSource(text) {
  if (text.startsWith("[runner]") || text.startsWith("[hf]")) return "runner";
  if (text.startsWith("[bridge]") || text.startsWith("[queue]") || text.startsWith("[halofuse]")) return "bridge";
  return "comfyui";
}

/** Validate and coerce one server log entry (C-4 §3). Returns null when it has no usable seq. */
export function normalizeEntry(raw) {
  if (!raw || typeof raw !== "object") return null;
  const seq = raw.seq;
  if (!Number.isInteger(seq) || seq < 0) return null;
  let text = String(raw.text ?? "").replace(/\r/g, "");
  const kind = typeof raw.kind === "string" && raw.kind ? raw.kind : "line";
  const explicit = typeof raw.level === "string" && raw.level !== "";
  let level = explicit ? raw.level.toLowerCase() : "info";
  level = LEVEL_ALIASES[level] || level;
  if (!(level in LEVEL_INDEX)) level = "info";
  if (!explicit) {
    if (ERROR_RE.test(text)) level = "error";
    else if (WARN_RE.test(text)) level = "warning";
  }
  const data = raw.data && typeof raw.data === "object" ? raw.data : null;
  if (kind === "truncated") {
    level = level === "error" ? "error" : "warning";
    if (!text) text = `Upstream log stream dropped ${data?.dropped ?? "some"} line(s); they are not in this log.`;
  }
  const source = SOURCES.includes(raw.source) ? raw.source : inferSource(text);
  return {
    seq,
    ts: typeof raw.ts === "number" && Number.isFinite(raw.ts) ? raw.ts : null,
    source,
    level,
    kind,
    text,
    node_id: raw.node_id != null ? String(raw.node_id) : null,
    phase: typeof raw.phase === "string" ? raw.phase : null,
    data,
  };
}

/** Holds every entry of one job log exactly once, ordered by seq. */
export class LogStore {
  constructor() {
    this._subs = new Set();
    this.reset(true);
  }

  /** Remove everything (switching jobs). */
  reset(silent = false) {
    this._entries = [];
    this._contiguous = 0;
    this._totals = [0, 0, 0, 0];
    this._viewBase = [0, 0, 0, 0];
    this._viewStart = 0;
    this.generation = (this.generation || 0) + 1;
    if (!silent) this._emit();
  }

  /** Add raw server entries in any order; returns how many were new. */
  ingest(rawEntries) {
    let added = 0;
    for (const raw of rawEntries || []) {
      const entry = normalizeEntry(raw);
      if (!entry || this._entries[entry.seq] !== undefined) continue;
      entry.display = describeEntry(entry); // readable text for structured events; equals text for plain lines
      this._entries[entry.seq] = entry;
      added += 1;
    }
    while (this._entries[this._contiguous] !== undefined) {
      this._totals[LEVEL_INDEX[this._entries[this._contiguous].level]] += 1;
      this._contiguous += 1;
    }
    if (added) this._emit();
    return added;
  }

  get size() { return this._contiguous; }
  get nextSeq() { return this._contiguous; }
  get viewStart() { return this._viewStart; }
  get clearedCount() { return this._viewStart; }

  get(seq) { return seq < this._contiguous ? this._entries[seq] : undefined; }

  /** Hide current rows from the view; the data (and the persisted server log) is kept. */
  clearView() {
    this._viewStart = this._contiguous;
    this._viewBase = this._totals.slice();
    this._emit();
  }

  restoreView() {
    this._viewStart = 0;
    this._viewBase = [0, 0, 0, 0];
    this._emit();
  }

  /** Level counts over the visible range [viewStart, size). */
  counts() {
    const c = this._totals.map((t, i) => t - this._viewBase[i]);
    return { total: c[0] + c[1] + c[2] + c[3], debug: c[0], info: c[1], warning: c[2], error: c[3] };
  }

  subscribe(fn) {
    this._subs.add(fn);
    return () => this._subs.delete(fn);
  }

  _emit() { for (const fn of this._subs) fn(); }
}

function escapeRe(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }

/** Build a predicate from a filter spec. Invalid regex → error message and match-all. */
export function compileFilter(spec = {}) {
  const { text = "", regex = false, caseSensitive = false, levels = null, sources = null,
    errorsOnly = false, warningsOnly = false } = spec || {};
  let allowedLevels = Array.isArray(levels) ? new Set(levels) : null;
  if (errorsOnly || warningsOnly) {
    allowedLevels = new Set([...(errorsOnly ? ["error"] : []), ...(warningsOnly ? ["warning"] : [])]);
  }
  const allowedSources = Array.isArray(sources) && sources.length < SOURCES.length ? new Set(sources) : null;
  let re = null;
  let error = null;
  if (text) {
    try {
      re = new RegExp(regex ? text : escapeRe(text), caseSensitive ? "" : "i");
    } catch (exc) {
      error = `Invalid pattern: ${exc.message}`;
      re = null;
    }
  }
  const identity = !re && !allowedLevels && !allowedSources;
  return {
    error,
    identity,
    test(entry) {
      if (allowedLevels && !allowedLevels.has(entry.level)) return false;
      if (allowedSources && !allowedSources.has(entry.source)) return false;
      if (re) {
        re.lastIndex = 0;
        if (!re.test(entry.text) && !(entry.display !== entry.text && entry.display && re.test(entry.display))
          && !re.test(`[${entry.source}]`)) return false;
      }
      return true;
    },
  };
}

/** Incrementally filtered list of seqs over a LogStore's visible range. */
export class FilteredView {
  constructor(store) {
    this.store = store;
    this._filter = compileFilter({});
    this._seqs = new Int32Array(1024);
    this._len = 0;
    this._cursor = 0;
    this._gen = -1;
    this._viewStart = -1;
  }

  setFilter(spec) {
    this._filter = compileFilter(spec);
    this._gen = -1; // force a rescan
    return this._filter.error;
  }

  get error() { return this._filter.error; }
  get isIdentity() { return this._filter.identity; }

  _syncBase() {
    const s = this.store;
    if (this._gen !== s.generation || this._viewStart !== s.viewStart) {
      this._gen = s.generation;
      this._viewStart = s.viewStart;
      this._cursor = s.viewStart;
      this._len = 0;
    }
  }

  /** Scan new entries for at most budgetMs; returns true when caught up. */
  step(budgetMs = 8, now = () => performance.now()) {
    this._syncBase();
    if (this.isIdentity) return true;
    const end = this.store.size;
    const t0 = now();
    const test = this._filter.test;
    while (this._cursor < end) {
      const stop = Math.min(this._cursor + 2048, end);
      for (let seq = this._cursor; seq < stop; seq++) {
        if (test(this.store.get(seq))) {
          if (this._len === this._seqs.length) {
            const grown = new Int32Array(this._seqs.length * 2);
            grown.set(this._seqs);
            this._seqs = grown;
          }
          this._seqs[this._len++] = seq;
        }
      }
      this._cursor = stop;
      if (now() - t0 >= budgetMs) break;
    }
    return this._cursor >= this.store.size;
  }

  get length() {
    this._syncBase();
    return this.isIdentity ? this.store.size - this.store.viewStart : this._len;
  }

  /** Seq of view row i, or -1 when i is outside the view (the Int32Array keeps stale slots past length). */
  seqAt(i) {
    if (!(i >= 0 && i < this.length)) return -1;
    return this.isIdentity ? this.store.viewStart + i : this._seqs[i];
  }

  entryAt(i) {
    const seq = this.seqAt(i);
    return seq < 0 ? undefined : this.store.get(seq);
  }

  /** Position of seq in the view, or -1. */
  indexOfSeq(seq) {
    if (this.isIdentity) {
      const i = seq - this.store.viewStart;
      return i >= 0 && i < this.length ? i : -1;
    }
    let lo = 0;
    let hi = this._len - 1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      const v = this._seqs[mid];
      if (v === seq) return mid;
      if (v < seq) lo = mid + 1; else hi = mid - 1;
    }
    return -1;
  }
}

/** One plain-text line for copying a selection. */
export function formatEntry(entry, { timestamps = true } = {}) {
  const head = `${entry.source.padEnd(8)} ${entry.level.toUpperCase().padEnd(7)} ${entry.display ?? entry.text}`;
  return timestamps ? `${fmtClock(entry.ts)} ${head}` : head;
}

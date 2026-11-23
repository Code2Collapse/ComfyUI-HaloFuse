// HaloFuse bridge activity feed for the console's Activity tab: failures that never reach a job log (Workflow Check
// blocking a render, a refused submit, a failed model upload, a sign-in error) read from the bridge's persisted
// record (GET /halofuse/activity). Owner 2026-11-23: the console showed no data at all, not even failures.
// Side-effect-free ES module; timers are injected so it is testable.

import { LogStore } from "./log_model.js";
import { createPoller } from "./follower.js";

const TAIL = 500;

export class ActivityFeed {
  /** @param {{api: object, timers?: object}} o */
  constructor({ api, timers }) {
    this.api = api;
    this.timers = timers;
    this.store = new LogStore();
    this.loaded = false;   // at least one successful read
    this.error = null;     // the last read error (shown in the empty state), null after a good read
    this.unsupported = false; // an older bridge without the route (404)
    this._next = null;     // the server's next seq; null = load the newest TAIL entries again
    this._poller = null;
    this._subs = new Set();
  }

  subscribe(fn) { this._subs.add(fn); return () => this._subs.delete(fn); }
  _emit() { for (const fn of this._subs) fn(this); }

  /** Newest error entry's time (server seconds), or null. */
  get lastErrorTs() {
    for (let s = this.store.size - 1; s >= 0; s--) {
      const e = this.store.get(s);
      if (e?.level === "error") return e.ts;
    }
    return null;
  }

  /** Load the history again (newest TAIL entries). Resolves when done; never throws. */
  async refresh() {
    // a poll in flight would append entries the fresh tail already holds: pause polling around the reload
    const polling = !!this._poller;
    this.stop();
    this._next = null;
    try { this._onPage(await this._load()); } catch (err) { this._onError(err); }
    if (polling && !this.unsupported) this.start(this._interval);
  }

  /** Follow new entries while someone is watching. */
  start(intervalMs = 2000) {
    if (this._poller) return;
    this._interval = intervalMs;
    this._poller = createPoller({ timers: this.timers, intervalMs, load: () => this._load(),
      onValue: (page) => this._onPage(page), onError: (err) => this._onError(err) });
    this._poller.start();
  }

  stop() { this._poller?.stop(); this._poller = null; }

  async _load() {
    const tail = this._next == null;
    const page = await (tail ? this.api.getActivity({ limit: TAIL }) : this.api.getActivity({ since: this._next, limit: TAIL }));
    return { page, tail };
  }

  _onPage({ page, tail }) {
    const entries = Array.isArray(page?.entries) ? page.entries : [];
    if (tail) this.store.reset();
    // server seqs are global (never reused); the store wants 0..n, so number the entries locally in arrival order
    const base = this.store.size;
    this.store.ingest(entries.map((e, i) => ({ ...e, seq: base + i })));
    if (Number.isInteger(page?.next)) this._next = page.next;
    else if (tail) this._next = 0;
    const changed = tail || entries.length || !this.loaded || this.error;
    this.loaded = true;
    this.error = null;
    this.unsupported = false;
    if (changed) this._emit();
  }

  _onError(err) {
    if (err?.status === 404) { this.unsupported = true; this.stop(); }
    this.error = err;
    this._emit();
  }

  /** The text the Activity tab shows when it has no lines (always says why). */
  emptyText() {
    if (this.unsupported) return "This HaloFuse bridge keeps no activity record yet. Update HaloFuse and restart ComfyUI.";
    if (this.error) return `Could not load the bridge activity: ${this.error.message || String(this.error)}`;
    if (!this.loaded) return "Loading the bridge activity…";
    return "Nothing recorded yet. Workflow Check results, refused renders, model upload failures and sign-in errors "
      + "appear here with their time.";
  }
}

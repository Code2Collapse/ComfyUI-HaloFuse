// HaloFuse job session: follows one job (or queue) - its record, its complete persisted log from seq 0,
// the run state derived from the log, and its delivered outputs. Shared by the panel and the console, so
// closing either never stops following. Side-effect-free ES module.

import { LogStore } from "./log_model.js";
import { RunState } from "./run_state.js";
import { createPager, createPoller } from "./follower.js";
import { isTerminal } from "./job_view.js";

const FINAL_DELIVERY = new Set(["complete", "partial", "failed", "not_started"]);
const MAX_WARNINGS = 2000;

export class JobSession {
  /** @param {{api: object, timers?: object}} o */
  constructor({ api, timers }) {
    this.api = api;
    this.timers = timers;
    this.store = new LogStore();
    this.run = new RunState();
    this.warnings = [];
    this.warningCount = 0;
    this.corruptSkipped = 0;
    this.job = null;
    this.id = null;
    this.kind = null; // "job" | "queue"
    this.outputs = [];
    this.error = null;
    this._subs = new Set();
    this._runCursor = 0;
    this._deliveryStamp = null;
    this._interval = 1000;
    this._poller = null;
    this._pager = null;
  }

  subscribe(fn) { this._subs.add(fn); return () => this._subs.delete(fn); }
  _emit(what) { for (const fn of this._subs) fn(what, this); }

  get logComplete() { return !!this._pager && !this._pager.running && isTerminal(this.job); }

  /** Follow a job ("job") or a queue ("queue") from the start of its log. */
  attach(id, kind = "job") {
    if (this.id === id && this.kind === kind) { this.poke(); return; }
    this.detach();
    this.id = id;
    this.kind = kind;
    this.job = null;
    this.outputs = [];
    this.error = null;
    this.store.reset();
    this.run.reset();
    this.warnings = [];
    this.warningCount = 0;
    this.corruptSkipped = 0;
    this._runCursor = 0;
    this._deliveryStamp = null;
    const api = this.api;
    const isQueue = kind === "queue";
    this._poller = createPoller({
      timers: this.timers,
      intervalMs: this._interval,
      load: () => (isQueue ? api.getQueue(id) : api.getJob(id)),
      onValue: (res) => this._onRecord(isQueue ? res?.queue : res?.job),
      onError: (err) => { this.error = err; this._emit("error"); },
    });
    this._pager = createPager({
      timers: this.timers,
      pageSize: 5000,
      fetchPage: async (since, limit) => {
        const page = await (isQueue ? api.getQueueLog(id, since, limit) : api.getLog(id, since, limit));
        // a page is terminal only when the record says so; the log route may lag the record by a poll
        return page;
      },
      onPage: (page) => this._onPage(page),
      onError: (err) => { this.error = err; this._emit("error"); },
    });
    this._poller.start();
    this._pager.start(0);
    this._emit("attach");
  }

  detach() {
    this._poller?.stop();
    this._pager?.stop();
    this._poller = null;
    this._pager = null;
    const had = this.id;
    this.id = null;
    this.kind = null;
    if (had) this._emit("detach");
  }

  /** Faster polling while someone is watching. */
  setWatching(on) {
    this._interval = on ? 1000 : 5000;
    this._poller?.setInterval(this._interval);
  }

  poke() { this._poller?.poke(); this._pager?.poke(); }

  _onPage(page) {
    if (page?.skipped_corrupt) this.corruptSkipped += page.skipped_corrupt;
    const added = this.store.ingest(page?.entries || []);
    if (!added) return;
    const end = this.store.size;
    for (let s = this._runCursor; s < end; s++) {
      const e = this.store.get(s);
      this.run.ingest(e);
      if (e.level === "warning") {
        this.warningCount += 1;
        this.warnings.push({ seq: e.seq, text: e.text });
        if (this.warnings.length > MAX_WARNINGS) this.warnings.splice(0, this.warnings.length - MAX_WARNINGS);
      }
    }
    this._runCursor = end;
    this._emit("log");
  }

  async _onRecord(rec) {
    if (!rec) return;
    this.error = null;
    this.job = rec;
    this._emit("job");
    if (this.kind !== "job") {
      if (isTerminal(rec)) this._poller?.setInterval(5000);
      return;
    }
    const d = rec.delivery;
    const stamp = d ? `${d.state}:${d.files_done}:${d.updated_at}` : null;
    if (d && stamp !== this._deliveryStamp && (d.files_done > 0 || FINAL_DELIVERY.has(d.state))) {
      this._deliveryStamp = stamp;
      try {
        const res = await this.api.getDelivery(rec.job_id || this.id);
        this.outputs = Array.isArray(res?.outputs) ? res.outputs : [];
        this._emit("outputs");
      } catch { /* delivery route optional until WS3 lands; the record still shows the status */ }
    }
    if (isTerminal(rec) && (!d || FINAL_DELIVERY.has(d.state))) {
      // finished: keep a slow poll so a manual delivery retry is still noticed
      this._poller?.setInterval(10000);
    }
  }
}

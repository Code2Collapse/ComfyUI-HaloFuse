// HaloFuse run state derived from the job log's C-3 events (phase, node, progress, telemetry, preview,
// output, error, final) and C-2 bridge kinds. Side-effect-free ES module.
// C-2 job.json carries no timeline/telemetry fields, so the console rebuilds them from the persisted log;
// a reload replays the log from seq 0 and reaches the same state.

const STAGE_PHASE = { load: "model_load", encode: "encoding", sample: "sampling", decode: "vae_decode", save: "outputs" };
const SAFE_MIME = new Set(["image/jpeg", "image/png", "image/webp"]);
const B64 = /^[A-Za-z0-9+/]+={0,2}$/;
const MAX_INLINE = 900_000; // characters of base64 kept inline per preview
const MAX_PREVIEWS = 200; // newest kept for display; older ones stay in the persisted log

const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);
const gb = (mb) => (num(mb) == null ? null : Math.round((mb / 1024) * 100) / 100);
const str = (v) => (v == null ? null : String(v));

function emptyTelemetry() {
  return { ts: null, gpu_util: null, vram_used_gb: null, vram_total_gb: null, ram_used_gb: null, ram_total_gb: null,
    cpu_util: null, disk_free_gb: null };
}

export class RunState {
  constructor() { this.reset(); }

  reset() {
    this._phases = [];
    this._openPhase = new Map();
    this._marks = [];
    this._nodes = [];
    this._nodeIndex = new Map();
    this._progress = null;
    this._latest = null;
    this._peak = emptyTelemetry();
    this._samples = 0;
    this._previews = [];
    this._outputs = [];
    this._errors = [];
    this._dropped = 0;
    this._final = null;
    this.version = 0;
  }

  /** Ingest one normalised LogEntry (log_model.normalizeEntry), in seq order. */
  ingest(e) {
    if (!e) return;
    const d = e.data || {};
    switch (e.kind) {
      case "phase": this._phase(e, d); break;
      case "node": this._node(e, d); break;
      case "progress": this._progressEv(e, d); break;
      case "telemetry": this._telemetry(e, d); break;
      case "preview": this._preview(e, d); break;
      case "output":
        this._outputs.push({ seq: e.seq, path: str(d.path), bytes: num(d.bytes), kind: d.kind || "other", node_id: str(d.node) });
        break;
      case "error":
        this._errors.push({ seq: e.seq, ts: e.ts, code: str(d.code), message: d.message ? String(d.message) : e.text,
          node_id: str(d.node ?? e.node_id), class_type: str(d.class_type), detail: d.detail ? String(d.detail) : null });
        break;
      case "truncated": this._dropped += num(d.dropped) ?? 0; break;
      case "final": this._final = { ...d }; break;
      default:
        if (e.level === "error") this._errors.push({ seq: e.seq, ts: e.ts, code: null, message: e.text, node_id: e.node_id, class_type: null, detail: null });
    }
    this.version += 1;
  }

  _phase(e, d) {
    const name = str(d.phase) || e.phase;
    if (!name) return;
    if (d.state === "start") {
      const row = { name, started_at: e.ts, ended_at: null, detail: str(d.detail) };
      this._phases.push(row);
      this._openPhase.set(name, row);
    } else if (d.state === "end") {
      const row = this._openPhase.get(name);
      if (row) {
        row.ended_at = e.ts ?? (num(row.started_at) != null && num(d.dur) != null ? row.started_at + d.dur : null);
        this._openPhase.delete(name);
      } else if (num(e.ts) != null) {
        const dur = num(d.dur) ?? 0;
        this._phases.push({ name, started_at: e.ts - dur, ended_at: e.ts, detail: str(d.detail) });
      }
    } else {
      this._marks.push({ name, detail: str(d.detail), ts: e.ts, dur: num(d.dur) });
    }
  }

  _node(e, d) {
    if (d.state === "cached") {
      for (const id of d.nodes || []) {
        const key = String(id);
        if (this._nodeIndex.has(key)) continue;
        const row = { node_id: key, class_type: null, stage: null, started_at: null, ended_at: null, cached: true, dur: null };
        this._nodeIndex.set(key, row);
        this._nodes.push(row);
      }
      return;
    }
    const id = str(d.node ?? e.node_id);
    if (id == null) return;
    let row = this._nodeIndex.get(id);
    if (d.state === "start") {
      if (!row || row.ended_at != null || row.cached) {
        row = { node_id: id, class_type: str(d.class_type), stage: d.stage || null, started_at: e.ts, ended_at: null, cached: false, dur: null };
        this._nodeIndex.set(id, row);
        this._nodes.push(row);
      }
      this._progress = { ...(this._progress || {}), phase: STAGE_PHASE[d.stage] || "running", node_id: id,
        class_type: row.class_type, step: null, steps: null, fraction: null, eta_s: null };
    } else if (d.state === "done" && row) {
      row.ended_at = e.ts;
      row.dur = num(d.dur) ?? (num(row.started_at) != null && num(e.ts) != null ? e.ts - row.started_at : null);
    }
  }

  _progressEv(e, d) {
    const value = num(d.value);
    const max = num(d.max);
    const id = str(d.node ?? e.node_id);
    const row = id != null ? this._nodeIndex.get(id) : null;
    this._progress = {
      phase: this._progress?.node_id === id ? this._progress.phase : (STAGE_PHASE[row?.stage] || "running"),
      node_id: id,
      class_type: row?.class_type ?? null,
      step: value,
      steps: max,
      fraction: value != null && max ? Math.min(value / max, 1) : null,
      eta_s: num(d.eta_s),
    };
  }

  _telemetry(e, d) {
    const g = Array.isArray(d.gpu) && d.gpu.length ? d.gpu[0] : null;
    const ramTotal = num(d.ram?.total_mb);
    const ramAvail = num(d.ram?.available_mb);
    const t = {
      ts: e.ts,
      gpu_util: num(g?.util_pct),
      vram_used_gb: gb(g?.mem_used_mb),
      vram_total_gb: gb(g?.mem_total_mb),
      ram_used_gb: ramTotal != null && ramAvail != null ? gb(ramTotal - ramAvail) : gb(d.cgroup?.current_mb),
      ram_total_gb: gb(ramTotal ?? d.cgroup?.max_mb),
      cpu_util: num(d.cpu_pct),
      disk_free_gb: gb(d.disk?.output?.free_mb),
    };
    this._latest = t;
    this._samples += 1;
    for (const k of Object.keys(t)) {
      if (k === "ts" || t[k] == null) continue;
      if (this._peak[k] == null || t[k] > this._peak[k]) this._peak[k] = t[k];
    }
  }

  _preview(e, d) {
    const mime = typeof d.mime === "string" ? d.mime.toLowerCase() : "";
    const data = typeof d.data === "string" ? d.data : "";
    const ok = SAFE_MIME.has(mime) && data.length > 0 && data.length <= MAX_INLINE && B64.test(data);
    // C-4 6a: the bridge stores the frame as a file and the log entry carries its record (data.preview.name);
    // inline data survives only in logs written by an older bridge, and is kept for those alone
    const rec = d.preview && typeof d.preview === "object" ? d.preview : null;
    this._previews.push({ seq: e.seq, ts: e.ts, kind: d.kind || "latent", node_id: str(d.node ?? e.node_id), mime: rec?.mime || mime || null,
      name: rec && typeof rec.name === "string" ? rec.name : null, mask: d.mask === true,
      dataUrl: !rec && ok ? `data:${mime};base64,${data}` : null, path: str(d.path), bytes: num(d.bytes), dropped: num(d.dropped) ?? 0 });
    if (this._previews.length > MAX_PREVIEWS) this._previews.splice(0, this._previews.length - MAX_PREVIEWS);
  }

  snapshot() {
    return {
      phases: this._phases.map((p) => ({ ...p })),
      marks: this._marks.slice(),
      nodes: this._nodes.map((n) => ({ ...n })),
      progress: this._progress ? { ...this._progress } : null,
      telemetry: { latest: this._latest ? { ...this._latest } : null, peak: this._samples ? { ...this._peak } : null },
      telemetrySamples: this._samples,
      previews: this._previews.slice(),
      outputs: this._outputs.slice(),
      errors: this._errors.slice(),
      droppedLines: this._dropped,
      final: this._final,
    };
  }
}

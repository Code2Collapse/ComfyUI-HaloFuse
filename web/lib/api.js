// HaloFuse C-4 HTTP client. Side-effect-free ES module; the fetch implementation is injected
// (ComfyUI's api.fetchApi in production, a mock in tests). Every request carries
// X-HaloFuse-Request: 1. Errors use the C-4 envelope {"error": {"code","message","id"}}.

export class ApiError extends Error {
  constructor(code, message, id = null, status = 0, jobId = null) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.id = id;
    this.status = status;
    this.jobId = jobId; // a submit that failed after the bridge created the job record names it (its log has details)
  }
}

const enc = encodeURIComponent;
const CANCEL_MODES = ["now", "after_current"];

function query(params) {
  const q = Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => `${enc(k)}=${enc(String(v))}`).join("&");
  return q ? `?${q}` : "";
}

export function previewPath(jobId, name) {
  return `/halofuse/jobs/${enc(jobId)}/previews/${enc(name)}`;
}

export function viewPath(ref) {
  return `/view?filename=${enc(ref.filename)}&subfolder=${enc(ref.subfolder || "")}&type=${enc(ref.type || "output")}`;
}

/** Number from a number or numeric string (the provider rate may arrive verbatim as a string); else null. */
export function toNumber(v) {
  if (typeof v === "number") return Number.isFinite(v) ? v : null;
  if (typeof v === "string" && v.trim() !== "" && Number.isFinite(Number(v))) return Number(v);
  return null;
}

/** C-1 §7 camelCase SKU → snake_case; null without an instance type. */
export function normalizeSku(s) {
  if (!s || typeof s !== "object") return null;
  const instance = s.instance_type ?? s.instanceType;
  if (!instance) return null;
  return {
    instance_type: String(instance),
    gpu_type: s.gpu_type ?? s.gpuType ?? null,
    gpu_memory_gb: toNumber(s.gpu_memory_gb ?? s.gpuMemoryGb),
    core_count: toNumber(s.core_count ?? s.coreCount),
    memory_gb: toNumber(s.memory_gb ?? s.memoryGb),
  };
}

export function createApi(fetchImpl) {
  async function call(method, path, body, { text = false } = {}) {
    const headers = { "X-HaloFuse-Request": "1" };
    const init = { method, headers };
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetchImpl(path, init);
    } catch {
      throw new ApiError("network", "HaloFuse could not reach the ComfyUI server.", null, 0);
    }
    if (!res.ok) {
      let env = null;
      try { env = await res.json(); } catch { env = null; }
      const e = env && typeof env.error === "object" && env.error ? env.error : null;
      if (e && typeof e.code === "string") {
        throw new ApiError(e.code, String(e.message || "HaloFuse request failed."), e.id ?? null, res.status,
          typeof e.job_id === "string" && e.job_id ? e.job_id : null);
      }
      throw new ApiError(`http_${res.status}`, `HaloFuse request failed (HTTP ${res.status}).`, null, res.status);
    }
    if (text) return res.text();
    try {
      return await res.json();
    } catch {
      throw new ApiError("bad_response", "HaloFuse received an unreadable response.", null, res.status);
    }
  }

  const get = (p, o) => call("GET", p, undefined, o);
  const post = (p, b = {}) => call("POST", p, b);
  const checkMode = (mode) => {
    if (!CANCEL_MODES.includes(mode)) throw new ApiError("bad_request", `Unknown stop mode: ${mode}`);
    return { mode };
  };
  const J = (id) => `/halofuse/jobs/${enc(id)}`;
  const Q = (id) => `/halofuse/queues/${enc(id)}`;

  return Object.freeze({
    getSettings: () => get("/halofuse/settings"),
    saveSettings: (body) => post("/halofuse/settings", body),
    getAccount: () => get("/halofuse/account"),
    saveCredentials({ email, password, storeUnencrypted } = {}) {
      const body = {};
      if (typeof email === "string" && email.trim()) body.email = email.trim();
      if (typeof password === "string" && password.trim()) body.password = password;
      if (storeUnencrypted === true) body.store_unencrypted = true;
      return post("/halofuse/account/credentials", body);
    },
    clearCredentials: () => call("DELETE", "/halofuse/account/credentials"),
    testCredentials: (body) => post("/halofuse/account/test", body || {}),
    listSkus: async () => {
      const data = await get("/halofuse/skus");
      return { skus: (data?.skus || []).map(normalizeSku).filter(Boolean) };
    },
    estimate: async (instanceType) => {
      const d = await get(`/halofuse/estimate${query({ instance_type: instanceType })}`);
      return { instance_type: d?.instance_type ?? d?.instanceType ?? instanceType,
        rate_per_hour_usd: toNumber(d?.rate_per_hour_usd ?? d?.ratePerHourUsd) };
    },
    costSummary: () => get("/halofuse/cost/summary"),
    submitJob: (body) => post("/halofuse/jobs", body),
    listJobs: ({ active, limit } = {}) => get(`/halofuse/jobs${query({ active: active ? 1 : undefined, limit })}`),
    getJob: (id) => get(J(id)),
    getLog: (id, since = 0, limit = 5000) => get(`${J(id)}/log${query({ since, limit })}`),
    getLogText: (id) => get(`${J(id)}/log.txt`, { text: true }),
    cancelJob: async (id, mode) => post(`${J(id)}/cancel`, checkMode(mode)),
    resubmit: (id, body = { reseed: true, stop_current: true }) => post(`${J(id)}/resubmit`, body),
    previewMethods: () => get("/halofuse/preview_methods"),
    getDelivery: (id) => get(`${J(id)}/delivery`),
    retryDelivery: (id) => post(`${J(id)}/delivery/retry`),
    /** Bridge activity (failures outside a job log): the newest `limit` entries, or those from `since` on. */
    getActivity: ({ since, limit } = {}) => get(`/halofuse/activity${query({ since, limit })}`),
    recentJobs: (limit = 50) => get(`/halofuse/recent_jobs${query({ limit })}`),
    recoverJob: (id) => post(`/halofuse/recovery/${enc(id)}`),
    preflight: (body) => post("/halofuse/preflight", body),
    draft: (body) => post("/halofuse/draft", body),
    library: () => get("/halofuse/library"),
    startModelUpload: (files) => post("/halofuse/models/upload", { files }),
    getModelUpload: (id) => get(`/halofuse/models/upload/${enc(id)}`),
    listModelUploads: () => get("/halofuse/models/uploads"),
    cancelModelUpload: (id) => post(`/halofuse/models/upload/${enc(id)}/cancel`),
    listPreviews: (id, since = 0, limit = 200) => get(`${J(id)}/previews${query({ since, limit })}`),
    createQueue: (body) => post("/halofuse/queues", body),
    getQueue: (id) => get(Q(id)),
    listQueues: ({ active } = {}) => get(`/halofuse/queues${query({ active: active ? 1 : undefined })}`),
    getQueueLog: (id, since = 0, limit = 5000) => get(`${Q(id)}/log${query({ since, limit })}`),
    cancelQueue: async (id, mode) => post(`${Q(id)}/cancel`, checkMode(mode)),
    pauseQueue: (id) => post(`${Q(id)}/pause`),
    resumeQueue: (id) => post(`${Q(id)}/resume`),
    reorderQueue: (id, itemIds) => post(`${Q(id)}/reorder`, { item_ids: itemIds }),
    patchQueueItem: (qid, itemId, patch) => call("PATCH", `${Q(qid)}/items/${enc(itemId)}`, patch),
    deleteQueueItem: (qid, itemId) => call("DELETE", `${Q(qid)}/items/${enc(itemId)}`),
  });
}

// HaloFuse paging / polling loops. Side-effect-free ES module.
// Guarantees: never two requests in flight, no busy loop, exponential backoff on errors, a stopped
// loop ignores a late result. Timers are injected so the loops are testable.

const defaultTimers = () => ({ setTimeout: globalThis.setTimeout.bind(globalThis), clearTimeout: globalThis.clearTimeout.bind(globalThis) });

function backoffMs(failures, maxMs) {
  return Math.min(1000 * 2 ** Math.max(failures - 1, 0), maxMs);
}

/**
 * Follows an offset-paged resource (C-4 §3 log, §6 previews) until it is terminal and complete.
 * fetchPage(since, limit) → {entries|previews, next, total, terminal}.
 */
export function createPager({ fetchPage, onPage, onError = () => {}, timers = defaultTimers(), idleMs = 500,
  maxBackoffMs = 10000, pageSize = 5000 }) {
  let next = 0;
  let running = false;
  let inFlight = false;
  let timer = null;
  let token = 0;
  let failures = 0;
  let pokeWhileBusy = false;
  let resolveDone;
  let done = new Promise((r) => { resolveDone = r; });

  const clear = () => { if (timer != null) { timers.clearTimeout(timer); timer = null; } };
  const schedule = (ms) => { clear(); timer = timers.setTimeout(() => { timer = null; tick(); }, ms); };

  async function tick() {
    if (!running || inFlight) return;
    inFlight = true;
    const my = token;
    let page;
    try {
      page = await fetchPage(next, pageSize);
    } catch (err) {
      inFlight = false;
      if (my !== token || !running) return;
      failures += 1;
      onError(err);
      schedule(backoffMs(failures, maxBackoffMs));
      return;
    }
    inFlight = false;
    if (my !== token || !running) return;
    failures = 0;
    const items = page?.entries || page?.previews || [];
    if (Number.isInteger(page?.next) && page.next >= next) next = page.next;
    else next += items.length;
    onPage(page);
    const total = Number.isInteger(page?.total) ? page.total : next;
    if (next < total || pokeWhileBusy) {
      pokeWhileBusy = false;
      schedule(0);
    } else if (page?.terminal) {
      running = false;
      clear();
      resolveDone(next);
    } else {
      schedule(idleMs);
    }
  }

  return {
    start(since = 0) {
      token += 1;
      clear();
      next = since;
      failures = 0;
      inFlight = false;
      running = true;
      done = new Promise((r) => { resolveDone = r; });
      tick();
    },
    stop() {
      token += 1;
      running = false;
      inFlight = false;
      clear();
    },
    poke() {
      if (!running) return;
      if (inFlight) { pokeWhileBusy = true; return; }
      clear();
      tick();
    },
    get running() { return running; },
    get next() { return next; },
    get done() { return done; },
  };
}

/** Polls a single resource (job detail, queue) every intervalMs with the same no-overlap/backoff rules. */
export function createPoller({ load, onValue, onError = () => {}, intervalMs = 1000, timers = defaultTimers(),
  maxBackoffMs = 10000 }) {
  let running = false;
  let inFlight = false;
  let timer = null;
  let token = 0;
  let failures = 0;
  let interval = intervalMs;

  const clear = () => { if (timer != null) { timers.clearTimeout(timer); timer = null; } };
  const schedule = (ms) => { clear(); timer = timers.setTimeout(() => { timer = null; tick(); }, ms); };

  async function tick() {
    if (!running || inFlight) return;
    inFlight = true;
    const my = token;
    try {
      const value = await load();
      inFlight = false;
      if (my !== token || !running) return;
      failures = 0;
      onValue(value);
      if (running && my === token) schedule(interval);
    } catch (err) {
      inFlight = false;
      if (my !== token || !running) return;
      failures += 1;
      onError(err);
      schedule(backoffMs(failures, maxBackoffMs));
    }
  }

  return {
    start() { token += 1; running = true; failures = 0; inFlight = false; clear(); tick(); },
    stop() { token += 1; running = false; inFlight = false; clear(); },
    poke() { if (running && !inFlight) { clear(); tick(); } },
    setInterval(ms) {
      interval = ms;
      if (running && !inFlight && timer != null) schedule(ms);
    },
    get running() { return running; },
  };
}

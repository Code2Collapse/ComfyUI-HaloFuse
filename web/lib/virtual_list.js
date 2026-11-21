// HaloFuse virtualised list: renders only the rows in view from a pooled set of elements.
// Side-effect-free ES module; DOM is touched only inside VirtualList methods.
// Fixed row height. When count * rowHeight exceeds maxPx (browsers cap element height, Firefox near
// 17.8M px) the scroll range is compressed and rows are anchored to the viewport instead.

const DEFAULT_MAX_PX = 8_000_000;

export function virtualMetrics({ count, rowHeight, maxPx = DEFAULT_MAX_PX }) {
  const full = Math.max(count, 0) * rowHeight;
  if (full <= maxPx) return { totalPx: full, scale: 1 };
  return { totalPx: maxPx, scale: full / maxPx };
}

/** Fractional index of the first row at the top of the viewport. */
export function firstRow({ scrollTop, viewportHeight, rowHeight, count, maxPx = DEFAULT_MAX_PX }) {
  const { totalPx } = virtualMetrics({ count, rowHeight, maxPx });
  const maxScroll = Math.max(totalPx - viewportHeight, 0);
  const maxFirst = Math.max(count - viewportHeight / rowHeight, 0);
  if (maxScroll <= 0) return 0;
  const top = Math.min(Math.max(scrollTop, 0), maxScroll);
  return (top / maxScroll) * maxFirst;
}

export function visibleRange({ scrollTop, viewportHeight, rowHeight, count, overscan = 8, maxPx = DEFAULT_MAX_PX }) {
  if (count <= 0) return { start: 0, end: 0 };
  const first = Math.floor(firstRow({ scrollTop, viewportHeight, rowHeight, count, maxPx }));
  const visible = Math.ceil(viewportHeight / rowHeight);
  return { start: Math.max(first - overscan, 0), end: Math.min(first + visible + overscan, count) };
}

/** Content-space top (px) at which row `index` is drawn. */
export function rowOffset(index, { scrollTop, viewportHeight, rowHeight, count, maxPx = DEFAULT_MAX_PX }) {
  const first = firstRow({ scrollTop, viewportHeight, rowHeight, count, maxPx });
  const { totalPx } = virtualMetrics({ count, rowHeight, maxPx });
  const top = Math.min(Math.max(scrollTop, 0), Math.max(totalPx - viewportHeight, 0));
  return top + (index - first) * rowHeight;
}

export function isPinnedToEnd({ scrollTop, viewportHeight, scrollHeight, slack = 4 }) {
  return scrollTop + viewportHeight >= scrollHeight - slack;
}

/**
 * DOM adapter. renderRow(rowEl, index) fills a pooled row element; onActivate(index, event) fires on
 * click / Enter. onScrollState(pinned) reports whether the user is at the end after a user scroll.
 */
export class VirtualList {
  constructor(scrollEl, { rowHeight, renderRow, onActivate = () => {}, onScrollState = () => {}, win = window,
    maxPx = DEFAULT_MAX_PX }) {
    this.el = scrollEl;
    this.rowHeight = rowHeight;
    this.renderRow = renderRow;
    this.onActivate = onActivate;
    this.onScrollState = onScrollState;
    this.win = win;
    this.maxPx = maxPx;
    this.follow = true;
    this.activeIndex = -1;
    this._count = 0;
    this._raf = 0;
    this._force = false;
    this._pool = [];
    const doc = scrollEl.ownerDocument;
    this.sizer = doc.createElement("div");
    this.sizer.className = "hf-vl-sizer";
    this.sizer.setAttribute("aria-hidden", "true");
    scrollEl.append(this.sizer);
    if (!scrollEl.hasAttribute("tabindex")) scrollEl.tabIndex = 0;
    this._onScroll = () => {
      if (!this._programmatic) this.onScrollState(this.isAtEnd());
      this._programmatic = false;
      this._schedule();
    };
    this._onClick = (ev) => {
      const row = ev.target.closest?.(".hf-vl-row");
      if (!row || !this.el.contains(row)) return;
      const index = Number(row.dataset.index);
      if (!Number.isInteger(index)) return;
      this.setActive(index);
      this.onActivate(index, ev);
    };
    this._onKey = (ev) => this._key(ev);
    scrollEl.addEventListener("scroll", this._onScroll, { passive: true });
    scrollEl.addEventListener("click", this._onClick);
    scrollEl.addEventListener("keydown", this._onKey);
    this._ro = typeof win.ResizeObserver === "function" ? new win.ResizeObserver(() => this._schedule()) : null;
    this._ro?.observe(scrollEl);
  }

  get count() { return this._count; }

  setCount(n) {
    if (n === this._count && !this.follow) return;
    this._count = n;
    if (this.activeIndex >= n) this.activeIndex = n - 1;
    this._schedule();
  }

  refresh() { this._force = true; this._schedule(); }

  scrollToEnd() { this.follow = true; this._schedule(); }

  scrollToIndex(i) {
    this.follow = false;
    this._pendingIndex = Math.max(0, Math.min(i, this._count - 1));
    this._schedule();
  }

  setActive(i) {
    this.activeIndex = i;
    this._force = true;
    this._schedule();
  }

  isAtEnd() {
    const el = this.el;
    return isPinnedToEnd({ scrollTop: el.scrollTop, viewportHeight: el.clientHeight, scrollHeight: el.scrollHeight });
  }

  destroy() {
    if (this._raf) this.win.cancelAnimationFrame(this._raf);
    this._raf = 0;
    this.el.removeEventListener("scroll", this._onScroll);
    this.el.removeEventListener("click", this._onClick);
    this.el.removeEventListener("keydown", this._onKey);
    this._ro?.disconnect();
    this.sizer.remove();
    for (const r of this._pool) r.remove();
    this._pool = [];
  }

  _schedule() {
    if (this._raf) return;
    this._raf = this.win.requestAnimationFrame(() => { this._raf = 0; this._render(); });
  }

  _key(ev) {
    const page = Math.max(Math.floor(this.el.clientHeight / this.rowHeight) - 1, 1);
    const moves = { ArrowDown: 1, ArrowUp: -1, PageDown: page, PageUp: -page };
    let next = this.activeIndex;
    if (ev.key in moves) next = (next < 0 ? 0 : next) + moves[ev.key];
    else if (ev.key === "Home") next = 0;
    else if (ev.key === "End") next = this._count - 1;
    else if (ev.key === "Enter" && this.activeIndex >= 0) { this.onActivate(this.activeIndex, ev); ev.preventDefault(); return; }
    else return;
    ev.preventDefault();
    if (this._count === 0) return;
    next = Math.max(0, Math.min(next, this._count - 1));
    this.activeIndex = next;
    this.follow = false;
    this.onScrollState(false);
    this._pendingIndex = next;
    this._force = true;
    this._schedule();
  }

  _render() {
    const el = this.el;
    const { rowHeight, maxPx } = this;
    const count = this._count;
    const { totalPx } = virtualMetrics({ count, rowHeight, maxPx });
    this.sizer.style.height = `${totalPx}px`;
    const viewportHeight = el.clientHeight;
    const maxScroll = Math.max(totalPx - viewportHeight, 0);
    if (this.follow) {
      if (el.scrollTop !== maxScroll) { this._programmatic = true; el.scrollTop = maxScroll; }
    } else if (this._pendingIndex != null) {
      const i = this._pendingIndex;
      const maxFirst = Math.max(count - viewportHeight / rowHeight, 0);
      const cur = firstRow({ scrollTop: el.scrollTop, viewportHeight, rowHeight, count, maxPx });
      const rowsInView = viewportHeight / rowHeight;
      let target = cur;
      if (i < cur) target = i;
      else if (i + 1 > cur + rowsInView) target = i + 1 - rowsInView;
      if (target !== cur && maxFirst > 0) { this._programmatic = true; el.scrollTop = (target / maxFirst) * maxScroll; }
    }
    this._pendingIndex = null;
    const scrollTop = Math.min(el.scrollTop, maxScroll);
    const p = { scrollTop, viewportHeight, rowHeight, count, maxPx };
    const { start, end } = visibleRange({ ...p, overscan: 6 });
    const need = end - start;
    const doc = el.ownerDocument;
    while (this._pool.length < need) {
      const row = doc.createElement("div");
      row.className = "hf-vl-row";
      row.setAttribute("role", "row");
      row.style.height = `${rowHeight}px`;
      el.append(row);
      this._pool.push(row);
    }
    for (let k = 0; k < this._pool.length; k++) {
      const row = this._pool[k];
      if (k >= need) { if (!row.hidden) row.hidden = true; continue; }
      const index = start + k;
      row.hidden = false;
      row.style.transform = `translateY(${rowOffset(index, p)}px)`;
      const active = index === this.activeIndex;
      if (this._force || row.dataset.index !== String(index)) {
        row.dataset.index = String(index);
        this.renderRow(row, index);
      }
      row.setAttribute("aria-selected", active ? "true" : "false");
    }
    this._force = false;
  }
}

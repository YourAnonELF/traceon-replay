// Trace waterfall: span tree on the left, time bars on the right, and a
// playhead that re-walks the recording. Rows are windowed (only the visible
// slice is in the DOM) and all per-frame work touches only those rows, using
// transform/opacity, so 5k-event fixtures stay smooth.

import { h } from "./dom.js";
import { fmtExactMs, fmtMs, fmtTick } from "./format.js";
import { progress } from "./logic.js";

const ROW_H = 22;
const OVERSCAN = 12;
const INTRO_MS = 450;
const INTRO_STAGGER_MS = 300;

function axisSteps(raw) {
  if (!(raw > 0)) return { major: 1, minor: 0.2 };
  const pow = Math.pow(10, Math.floor(Math.log10(raw)));
  for (const m of [1, 2, 2.5, 5, 10]) {
    if (raw <= m * pow) return { major: m * pow, minor: (m * pow) / (m === 2 ? 4 : 5) };
  }
  return { major: 10 * pow, minor: 2 * pow };
}

export function reducedMotion() {
  return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

export class Waterfall {
  constructor(host, { onSelect, onScrub }) {
    this.host = host;
    this.onSelect = onSelect;
    this.onScrub = onScrub;
    this.model = null;
    this.collapsed = new Set();
    this.rows = [];
    this.rowPos = new Map();
    this.rendered = [];
    this.selected = -1;
    this.active = -1;
    this.t = null;
    this.flashing = new Map();
    this.frame = 0;
    this.introUntil = 0;
    this.trackWidth = 0;
    this.resizeObserver = new ResizeObserver(() => this.layout());
  }

  destroy() {
    this.resizeObserver.disconnect();
    if (this.frame) cancelAnimationFrame(this.frame);
  }

  setModel(model, selectedId) {
    this.model = model;
    this.collapsed = new Set();
    this.build();
    this.introUntil = reducedMotion() ? 0 : performance.now() + INTRO_MS;
    this.refreshRows();
    const i = selectedId ? model.index.get(selectedId) : undefined;
    this.selected = -1;
    if (i !== undefined) this.select(i, { scroll: "center", notify: false });
    else this.paint();
  }

  build() {
    const legend = h("span", { class: "wf-legend", "aria-hidden": "true" },
      ["tool", "model", "value", "span"].map((k) => h("span", null, h("span", { class: `wf-swatch wf-swatch-${k}` }), k)));
    this.axis = h("div", { class: "wf-axis", title: "Click or drag to move the playhead" });
    this.head = h("div", { class: "wf-head" },
      h("div", { class: "wf-head-name" }, h("span", null, "Event"), legend),
      this.axis);
    this.grid = h("div", { class: "wf-grid", "aria-hidden": "true" });
    this.rowsEl = h("div", { class: "wf-rows", role: "rowgroup" });
    this.spacer = h("div", { class: "wf-spacer" }, this.grid, h("div", { class: "wf-namecol-rule" }), this.rowsEl);
    this.body = h("div", {
      class: "wf-body", role: "treegrid", tabindex: "0", "aria-label": "Trace events",
      "aria-readonly": "true", "aria-colcount": "2",
    }, this.spacer);
    this.playhead = h("div", { class: "wf-playhead", "aria-hidden": "true", hidden: true },
      h("div", { class: "wf-playhead-handle" }), h("div", { class: "wf-playhead-line" }));
    this.body.addEventListener("scroll", () => this.schedule(), { passive: true });
    this.body.addEventListener("click", (ev) => this.onClick(ev));
    this.body.addEventListener("dblclick", (ev) => {
      const row = ev.target.closest(".wf-row");
      if (row) this.toggle(Number(row.dataset.i));
    });
    this.axis.addEventListener("pointerdown", (ev) => this.scrubStart(ev));
    this.root = h("div", { class: "wf" }, this.head, this.body, this.playhead);
    this.host.append(this.root);
    this.resizeObserver.observe(this.body);
  }

  // Scrubbing --------------------------------------------------------------
  timeAtClientX(x) {
    const rect = this.axis.getBoundingClientRect();
    const frac = rect.width > 0 ? (x - rect.left) / rect.width : 0;
    return Math.max(0, Math.min(1, frac)) * this.model.total;
  }

  scrubStart(ev) {
    if (ev.button !== 0 || !this.model) return;
    ev.preventDefault();
    this.axis.setPointerCapture(ev.pointerId);
    this.root.classList.add("is-scrubbing");
    if (this.onScrub) this.onScrub(this.timeAtClientX(ev.clientX), "start");
    const move = (e) => { if (this.onScrub) this.onScrub(this.timeAtClientX(e.clientX), "move"); };
    const up = (e) => {
      this.root.classList.remove("is-scrubbing");
      this.axis.removeEventListener("pointermove", move);
      this.axis.removeEventListener("pointerup", up);
      this.axis.removeEventListener("pointercancel", up);
      if (this.onScrub) this.onScrub(this.timeAtClientX(e.clientX), "end");
    };
    this.axis.addEventListener("pointermove", move);
    this.axis.addEventListener("pointerup", up);
    this.axis.addEventListener("pointercancel", up);
  }

  // Layout -----------------------------------------------------------------
  refreshRows() {
    this.rows = this.model.visible(this.collapsed);
    this.rowPos = new Map(this.rows.map((idx, pos) => [idx, pos]));
    this.spacer.style.height = `${this.rows.length * ROW_H}px`;
    this.body.setAttribute("aria-rowcount", String(this.rows.length));
    this.layout();
  }

  layout() {
    if (!this.model) return;
    // Keep the axis exactly as wide as the body's track (minus its scrollbar).
    const scrollbar = this.body.offsetWidth - this.body.clientWidth;
    this.head.style.paddingRight = `${Math.max(0, scrollbar)}px`;
    const width = this.axis.clientWidth || 600;
    this.trackWidth = width;
    const total = this.model.total;
    const { major, minor } = axisSteps(total / Math.max(2, Math.floor(width / 96)));
    const ticks = [];
    const lines = [];
    const n = Math.floor(total / minor + 1e-6);
    for (let k = 0; k <= n; k++) {
      const t = k * minor;
      const isMajor = Math.abs(t / major - Math.round(t / major)) < 1e-6;
      const pct = (t / total) * 100;
      const tick = h("div", { class: isMajor ? "wf-tick wf-tick-major" : "wf-tick" });
      tick.style.left = `${pct}%`;
      if (isMajor && t / total < 0.95) tick.append(h("span", { class: "wf-tick-label" }, fmtTick(t, major)));
      ticks.push(tick);
      if (k > 0) {
        const line = h("div", { class: isMajor ? "wf-gridline wf-gridline-major" : "wf-gridline" });
        line.style.left = `${pct}%`;
        lines.push(line);
      }
    }
    this.axis.replaceChildren(...ticks);
    this.grid.replaceChildren(...lines);
    this.positionPlayhead();
    this.paint();
  }

  schedule() {
    if (this.frame) return;
    this.frame = requestAnimationFrame(() => { this.frame = 0; this.paint(); });
  }

  paint() {
    if (!this.model) return;
    const top = this.body.scrollTop;
    const height = this.body.clientHeight || 600;
    const first = Math.max(0, Math.floor(top / ROW_H) - OVERSCAN);
    const last = Math.min(this.rows.length, Math.ceil((top + height) / ROW_H) + OVERSCAN);
    const now = performance.now();
    const intro = now < this.introUntil;
    const frag = document.createDocumentFragment();
    this.rendered = [];
    for (let pos = first; pos < last; pos++) {
      const r = this.renderRow(this.rows[pos], pos);
      if (intro) {
        const delay = ((pos - first) / Math.max(1, last - first)) * INTRO_STAGGER_MS;
        r.bar.classList.add("is-intro");
        r.bar.style.animationDelay = `${Math.round(delay)}ms`;
      }
      this.rendered.push(r);
      frag.append(r.row);
    }
    this.rowsEl.style.transform = `translateY(${first * ROW_H}px)`;
    this.rowsEl.replaceChildren(frag);
    this.applyPlayback();
    if (this.selected >= 0 && this.rowPos.has(this.selected)) {
      this.body.setAttribute("aria-activedescendant", `wf-row-${this.selected}`);
    } else {
      this.body.removeAttribute("aria-activedescendant");
    }
  }

  renderRow(i, pos) {
    const m = this.model;
    const ev = m.events[i];
    const depth = m.depth[i];
    const kids = m.children.get(i);
    const hasKids = Boolean(kids && kids.length);
    const isCollapsed = this.collapsed.has(i);
    const isError = ev.status === "error";
    const start = m.start[i];
    const dur = m.duration[i];

    const guides = [];
    for (let d = 0; d < depth; d++) guides.push(h("span", { class: "wf-guide" }));
    const twisty = hasKids
      ? h("button", {
        type: "button", class: "wf-twisty", tabindex: "-1", "data-toggle": "1",
        "aria-label": isCollapsed ? "Expand" : "Collapse",
      }, isCollapsed ? "▸" : "▾")
      : h("span", { class: "wf-twisty wf-twisty-leaf", "aria-hidden": "true" });

    const name = h("div", { class: "wf-name", role: "gridcell" },
      guides, twisty,
      h("span", { class: `tag tag-${ev.kind}` }, ev.kind),
      h("span", { class: "wf-evname" }, ev.name),
      isError ? h("span", { class: "wf-flag wf-flag-error" }, "error") : null,
      ev.status === "cancelled" ? h("span", { class: "wf-flag wf-flag-fx" }, "cancelled") : null,
      ev.side_effect ? h("span", { class: "wf-flag wf-flag-fx", title: "Side effect: the real call mutates the outside world" }, "side effect") : null,
      h("span", { class: "wf-dur" }, dur === null ? "–" : fmtMs(dur)));

    const total = m.total;
    const leftPct = (start / total) * 100;
    const widthPct = ((dur || 0) / total) * 100;
    const fill = h("div", { class: "wf-bar-fill" });
    const bar = h("div", { class: `wf-bar wf-bar-${ev.kind}${isError ? " is-error" : ""}` }, fill);
    bar.style.left = `${leftPct}%`;
    if (ev.kind !== "value") bar.style.width = `${widthPct}%`;
    const label = h("span", { class: "wf-bar-label" }, dur === null ? "" : fmtMs(dur));
    const endPct = leftPct + (ev.kind === "value" ? 0 : widthPct);
    // Label beside the bar where there is room; otherwise the name column's
    // duration is enough.
    let showLabel = dur !== null;
    if (endPct <= 82) label.style.left = `calc(${endPct}% + 4px)`;
    else if (leftPct >= 12) label.style.right = `calc(${100 - leftPct}% + 4px)`;
    else showLabel = false;
    let noexec = null;
    if (ev.side_effect && ev.kind !== "span") {
      noexec = h("span", { class: "wf-noexec", title: "Side effect not executed in replay: the recorded response is served instead." },
        "not executed in replay");
      if (endPct <= 60) noexec.style.left = `calc(${endPct}% + 52px)`;
      else noexec.style.right = `calc(${100 - leftPct}% + 52px)`;
    }
    const track = h("div", { class: "wf-track", role: "gridcell" }, bar, showLabel ? label : null, noexec);

    const row = h("div", {
      class: `wf-row${isError ? " is-error" : ""}${i === this.selected ? " is-selected" : ""}${i === this.active ? " is-active" : ""}`,
      role: "row",
      id: `wf-row-${i}`,
      "aria-level": String(depth + 1),
      "aria-rowindex": String(pos + 1),
      "aria-selected": String(i === this.selected),
      "aria-expanded": hasKids ? String(!isCollapsed) : null,
      title: `${ev.kind}:${ev.name}\nstart +${fmtExactMs(start)}\nduration ${fmtExactMs(dur)}\nstatus ${ev.status}`,
      dataset: { i: String(i), kind: ev.kind },
    }, name, track);
    const until = this.flashing.get(i);
    if (until && until > performance.now()) row.classList.add("is-flash");
    return { i, row, bar, fill, noexec };
  }

  // Playback ---------------------------------------------------------------
  positionPlayhead() {
    if (!this.playhead) return;
    if (this.t === null || !this.model) { this.playhead.hidden = true; return; }
    this.playhead.hidden = false;
    const x = (this.t / this.model.total) * this.trackWidth;
    this.playhead.style.transform = `translateX(${x.toFixed(1)}px)`;
  }

  applyPlayback() {
    const m = this.model;
    const playing = this.t !== null;
    for (const r of this.rendered) {
      if (!playing) {
        r.bar.classList.remove("is-playing");
        r.fill.style.transform = "";
        if (r.noexec) r.noexec.classList.remove("is-shown");
        continue;
      }
      const p = progress(m.start[r.i], m.duration[r.i], this.t);
      r.bar.classList.add("is-playing");
      r.fill.style.transform = `scaleX(${p.toFixed(4)})`;
      if (r.noexec) r.noexec.classList.toggle("is-shown", this.t >= m.start[r.i]);
    }
  }

  /** Move the playhead to t (ms), or hide it with null (all bars full). */
  setPlayhead(t) {
    this.t = t;
    this.root.classList.toggle("is-replaying", t !== null);
    this.positionPlayhead();
    this.applyPlayback();
  }

  /** Mark the row the playhead is in; keep it visible. */
  setActive(i, { follow = true } = {}) {
    if (i === this.active) return;
    const prev = this.active;
    this.active = i;
    if (i >= 0) {
      this.reveal(i);
      if (follow) this.scrollTo(i, "follow");
    }
    let repaint = false;
    for (const r of this.rendered) {
      if (r.i === prev) r.row.classList.remove("is-active");
      if (r.i === i) r.row.classList.add("is-active");
    }
    if (i >= 0 && !this.rendered.some((r) => r.i === i)) repaint = true;
    if (repaint) this.paint();
  }

  /** Flash a row once (error reached during playback). */
  flash(i) {
    if (reducedMotion()) return;
    this.flashing.set(i, performance.now() + 520);
    const r = this.rendered.find((x) => x.i === i);
    if (r) {
      r.row.classList.remove("is-flash");
      void r.row.offsetWidth;
      r.row.classList.add("is-flash");
    }
  }

  reveal(i) {
    let p = this.model.events[i].parent_id;
    let changed = false;
    while (p) {
      const pi = this.model.index.get(p);
      if (pi === undefined) break;
      if (this.collapsed.delete(pi)) changed = true;
      p = this.model.events[pi].parent_id;
    }
    if (changed) this.refreshRows();
  }

  // Selection --------------------------------------------------------------
  onClick(ev) {
    const row = ev.target.closest(".wf-row");
    if (!row) return;
    const i = Number(row.dataset.i);
    if (ev.target.closest("[data-toggle]")) {
      this.toggle(i);
      return;
    }
    this.select(i, { scroll: false });
    this.body.focus({ preventScroll: true });
  }

  select(i, { scroll = "nearest", notify = true } = {}) {
    if (!this.model || i < 0 || i >= this.model.events.length) return;
    this.reveal(i);
    this.selected = i;
    if (scroll) this.scrollTo(i, scroll);
    this.paint();
    if (notify && this.onSelect) this.onSelect(this.model.events[i]);
  }

  /** Move the selection highlight without repainting (playback follow). */
  markSelected(i) {
    if (i === this.selected) return;
    const prev = this.selected;
    this.selected = i;
    for (const r of this.rendered) {
      if (r.i === prev) { r.row.classList.remove("is-selected"); r.row.setAttribute("aria-selected", "false"); }
      if (r.i === i) { r.row.classList.add("is-selected"); r.row.setAttribute("aria-selected", "true"); }
    }
    if (i >= 0 && this.rowPos.has(i)) this.body.setAttribute("aria-activedescendant", `wf-row-${i}`);
  }

  clearSelection() {
    this.selected = -1;
    this.paint();
    if (this.onSelect) this.onSelect(null);
  }

  scrollTo(i, mode) {
    const pos = this.rowPos.get(i);
    if (pos === undefined) return;
    const top = pos * ROW_H;
    const view = this.body.clientHeight || 600;
    const cur = this.body.scrollTop;
    if (mode === "center") {
      this.body.scrollTop = Math.max(0, top - view / 2 + ROW_H);
    } else if (mode === "follow") {
      // Keep the playhead row in the middle band, scrolling only when it leaves it.
      if (top < cur + ROW_H * 2 || top > cur + view - ROW_H * 4) {
        this.body.scrollTop = Math.max(0, top - view / 3);
      }
    } else if (top < cur) {
      this.body.scrollTop = top;
    } else if (top + ROW_H > cur + view) {
      this.body.scrollTop = top + ROW_H - view;
    }
  }

  move(delta) {
    if (!this.rows.length) return;
    const cur = this.rowPos.has(this.selected) ? this.rowPos.get(this.selected) : -1;
    let next = cur < 0 ? (delta > 0 ? 0 : this.rows.length - 1) : cur + delta;
    next = Math.max(0, Math.min(this.rows.length - 1, next));
    this.select(this.rows[next]);
  }

  page(dir) {
    this.move(dir * Math.max(1, Math.floor((this.body.clientHeight || 600) / ROW_H) - 1));
  }

  // Expand / collapse with a FLIP slide: rows that stay move from their old
  // position to the new one; rows that appear unfold from the top edge.
  toggle(i) {
    if (!this.model.children.get(i)) return;
    const before = new Map(this.rendered.map((r) => [r.i, this.rowPos.get(r.i)]));
    if (this.collapsed.has(i)) this.collapsed.delete(i);
    else this.collapsed.add(i);
    this.refreshRows();
    if (this.selected >= 0 && !this.rowPos.has(this.selected)) this.select(i, { scroll: false });
    if (reducedMotion()) return;
    const moved = [];
    for (const r of this.rendered) {
      const old = before.get(r.i);
      const now = this.rowPos.get(r.i);
      if (old === undefined) {
        r.row.classList.add("is-entering");
      } else if (old !== now) {
        r.row.style.transform = `translateY(${(old - now) * ROW_H}px)`;
        moved.push(r.row);
      }
    }
    if (moved.length) {
      requestAnimationFrame(() => {
        for (const el of moved) {
          el.classList.add("is-sliding");
          el.style.transform = "";
        }
      });
    }
  }

  expand() {
    const i = this.selected;
    if (i < 0) return;
    if (this.collapsed.has(i)) { this.toggle(i); return; }
    const kids = this.model.children.get(i);
    if (kids && kids.length) this.select(kids[0]);
  }

  collapse() {
    const i = this.selected;
    if (i < 0) return;
    const kids = this.model.children.get(i);
    if (kids && kids.length && !this.collapsed.has(i)) { this.toggle(i); return; }
    const parent = this.model.events[i].parent_id;
    const pi = parent ? this.model.index.get(parent) : undefined;
    if (pi !== undefined) this.select(pi);
  }

  focus() {
    this.body.focus({ preventScroll: true });
  }
}

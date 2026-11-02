// Divergence fork: baseline and candidate drawn as two call tracks that
// coincide while calls match and split at the first divergence -- the
// brand mark's motif, built from the actual alignment.

import { h, s } from "./dom.js";

const H = 76;
const LABEL_W = 78;
const Y_BASE = 26;
const Y_FORK = 54;
const BEND = 16;
const PAD_R = 14;

export class ForkView {
  /**
   * @param report DiffReport JSON
   * @param firstPair alignment index of the first divergence, or -1
   * @param divPairs Set of alignment indexes that carry any divergence
   */
  constructor(report, firstPair, divPairs, { onHover, onPick }) {
    this.report = report;
    this.firstPair = firstPair;
    this.divPairs = divPairs;
    this.onHover = onHover;
    this.onPick = onPick;
    this.width = 0;
    this.animated = false;
    this.el = h("div", { class: "fork", role: "img", "aria-label": this.describe() });
    this.observer = new ResizeObserver(() => {
      const w = this.el.clientWidth;
      if (w > 0 && Math.abs(w - this.width) > 1) this.draw(w);
    });
    this.observer.observe(this.el);
  }

  destroy() {
    this.observer.disconnect();
  }

  describe() {
    const r = this.report;
    if (r.equivalent) return "Baseline and candidate tracks coincide: no divergence.";
    const fd = r.first_divergence;
    return `Tracks coincide until ${fd && fd.label ? fd.label : "the end of the run"}, where the candidate diverges: ${fd ? fd.message : ""}`;
  }

  highlight(i) {
    if (!this.cols) return;
    for (const [k, col] of this.cols) col.classList.toggle("is-hover", k === i);
  }

  draw(width) {
    this.width = width;
    const align = this.report.alignment;
    const n = align.length;
    const x0 = LABEL_W;
    const x1 = Math.max(x0 + 40, width - PAD_R);
    const step = n > 0 ? (x1 - x0) / n : x1 - x0;
    const xAt = (i) => x0 + (i + 0.5) * step;
    const fork = this.report.equivalent ? -1 : (this.firstPair >= 0 ? this.firstPair : n);
    const forkX = fork < 0 ? null : (fork >= n ? x1 : x0 + fork * step);
    const yCand = (i) => (fork >= 0 && i >= fork ? Y_FORK : Y_BASE);

    const svg = s("svg", { class: "fork-svg", width: String(width), height: String(H), viewBox: `0 0 ${width} ${H}` });
    // Lane labels.
    svg.append(
      s("text", { x: "0", y: String(Y_BASE + 4), class: "fork-lane" }, "baseline"),
      s("text", { x: "0", y: String((fork >= 0 && fork < n ? Y_FORK : Y_BASE + 16) + 4), class: "fork-lane fork-lane-cand" },
        fork >= 0 && fork < n ? "candidate" : "candidate ="));

    const draw = s("g", { class: `fork-draw${this.animated ? " is-static" : ""}` });
    // Baseline track.
    draw.append(s("path", { d: `M${x0} ${Y_BASE}H${x1}`, class: "fork-track fork-track-base" }));
    // Candidate track: rides on the baseline line while calls match, then
    // leaves it at the fork (the brand mark's motif).
    if (forkX !== null && fork < n) {
      const d = `M${forkX} ${Y_BASE}L${Math.min(x1, forkX + BEND)} ${Y_FORK}H${x1}`;
      draw.append(s("path", { d, class: "fork-track fork-track-cand" }));
    }

    // Per-call ticks and hit columns.
    this.cols = new Map();
    const ticks = s("g", { class: "fork-ticks" });
    const hits = s("g", { class: "fork-hits" });
    align.forEach((p, i) => {
      const x = xAt(i);
      const col = s("g", { class: "fork-col", dataset: { i: String(i) } });
      col.append(s("line", { x1: String(x), x2: String(x), y1: "8", y2: String(H - 8), class: "fork-col-line" }));
      const marks = [];
      if (p.baseline_event_id) {
        const cls = p.status === "removed" ? "is-removed" : (this.divPairs.has(i) ? "is-div" : `k-${p.kind}`);
        marks.push(s("line", { x1: String(x), x2: String(x), y1: String(Y_BASE - 6), y2: String(Y_BASE + 6), class: `fork-tick ${cls}` }));
      }
      if (p.candidate_event_id) {
        const y = yCand(i);
        const cls = p.status === "added" ? "is-added" : (this.divPairs.has(i) ? "is-div" : `k-${p.kind}`);
        if (y !== Y_BASE || !p.baseline_event_id) {
          marks.push(s("line", { x1: String(x), x2: String(x), y1: String(y - 6), y2: String(y + 6), class: `fork-tick ${cls}` }));
        }
      }
      ticks.append(...marks);
      col.append(s("rect", {
        x: String(x - step / 2), y: "0", width: String(Math.max(2, step)), height: String(H), class: "fork-hit",
      }, s("title", null, `#${i} ${p.kind}:${p.name} · ${p.status}`)));
      col.addEventListener("mouseenter", () => { this.highlight(i); if (this.onHover) this.onHover(i); });
      col.addEventListener("mouseleave", () => { this.highlight(-1); if (this.onHover) this.onHover(-1); });
      col.addEventListener("click", () => { if (this.onPick) this.onPick(i); });
      this.cols.set(i, col);
      hits.append(col);
    });
    draw.append(ticks);
    svg.append(hits, draw);

    if (forkX !== null) {
      const size = 7;
      const g = s("g", { class: `fork-point${this.animated ? " is-static" : ""}`, transform: `translate(${forkX} ${Y_BASE})` });
      g.append(
        s("rect", { x: String(-size), y: String(-size), width: String(size * 2), height: String(size * 2), class: "fork-pulse" }),
        s("rect", { x: String(-size / 2), y: String(-size / 2), width: String(size), height: String(size), class: "fork-dot" }));
      svg.append(g);
      const fd = this.report.first_divergence;
      const label = fd ? `${fd.label || "run"}${fd.path ? `  ${fd.path}` : ""}` : "";
      const anchorRight = forkX > width - 260;
      svg.append(s("text", {
        x: String(anchorRight ? forkX - 12 : forkX + 12), y: "12",
        class: `fork-note${this.animated ? " is-static" : ""}`, "text-anchor": anchorRight ? "end" : "start",
      }, `first divergence  ${label}`));
    } else {
      svg.append(s("text", { x: String(x1), y: "12", class: "fork-note fork-note-ok", "text-anchor": "end" }, "tracks coincide: equivalent"));
    }
    this.el.replaceChildren(svg);
    this.animated = true;
  }
}

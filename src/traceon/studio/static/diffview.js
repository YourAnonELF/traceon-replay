// Diff mode: baseline vs candidate, first divergence pinned, metrics deltas,
// and the call-by-call alignment computed by traceon.diff.compare.

import { h, mount } from "./dom.js";
import { fmtCost, fmtDelta, fmtInt, fmtMs, oneLine } from "./format.js";
import { changeList, payloadChanges, renderEvent, statusLabel } from "./detail.js";
import { ForkView } from "./fork.js";
import { FixtureModel } from "./model.js";

const MARKS = { same: "=", changed: "~", added: "+", removed: "−" };
const STATUS_TEXT = {
  same: "same call, same request",
  changed: "same call identity, something differs",
  added: "only in candidate",
  removed: "only in baseline",
};

const METRICS = [
  ["duration", "duration_ms", "ms"],
  ["events", "events", "count"],
  ["tool calls", "tool_calls", "count"],
  ["model calls", "model_calls", "count"],
  ["errors", "errors", "count"],
  ["input tokens", "input_tokens", "tokens"],
  ["output tokens", "output_tokens", "tokens"],
  ["cost, measured", "cost_measured", "cost"],
  ["cost, estimated", "cost_estimated", "cost"],
];

function fmtMetric(v, kind, currency) {
  if (v === null || v === undefined) return "–";
  if (kind === "ms") return fmtMs(v);
  if (kind === "cost") return fmtCost(v, currency);
  return fmtInt(v);
}

export class DiffView {
  constructor(host, detail, api, { onChange }) {
    this.host = host;
    this.detail = detail;
    this.api = api;
    this.onChange = onChange;
    this.report = null;
    this.base = null;
    this.cand = null;
    this.selected = -1;
    this.divByPair = new Map();
    this.token = 0;
    this.fork = null;
    this.hovered = -1;
  }

  async open(summaries, baselineId, candidateId, pos) {
    const token = ++this.token;
    this.summaries = summaries;
    this.baselineId = baselineId;
    this.candidateId = candidateId;
    const scroll = h("div", { class: "diff-scroll" });
    this.verdict = h("span", { class: "diff-verdict" });
    mount(this.host, h("div", { class: "diff" }, this.renderBar(), scroll));
    this.scroll = scroll;
    if (summaries.length < 2) {
      scroll.append(h("p", { class: "empty" }, "Diff needs two fixtures. Load both with ",
        h("code", null, "traceon studio baseline.json candidate.json"), "."));
      mount(this.detail);
      return;
    }
    if (!baselineId || !candidateId) {
      scroll.append(h("p", { class: "empty" }, "Choose a baseline and a candidate fixture."));
      mount(this.detail);
      return;
    }
    scroll.append(h("p", { class: "empty" }, "Comparing."));
    let report, base, cand;
    try {
      [report, base, cand] = await Promise.all([
        this.api.diff(baselineId, candidateId),
        this.api.fixture(baselineId),
        this.api.fixture(candidateId),
      ]);
    } catch (err) {
      if (token !== this.token) return;
      mount(scroll, h("p", { class: "empty error-text" }, err.message));
      mount(this.detail);
      return;
    }
    if (token !== this.token) return;
    this.report = report;
    this.base = new FixtureModel(base);
    this.cand = new FixtureModel(cand);
    this.indexDivergences();
    this.renderReport();
    const start = Number.isInteger(pos) && pos >= 0 && pos < report.alignment.length ? pos : this.firstPos();
    this.select(start, { notify: false, scroll: true });
  }

  renderBar() {
    const makeSelect = (id, current) => {
      const sel = h("select", { id, "aria-label": id === "diff-baseline" ? "Baseline fixture" : "Candidate fixture" },
        h("option", { value: "" }, "Choose a fixture"),
        this.summaries.map((s) => h("option", { value: s.id, selected: s.id === current }, `${s.name} · ${s.id}`)));
      sel.addEventListener("change", () => this.emitIds());
      return sel;
    };
    this.baseSel = makeSelect("diff-baseline", this.baselineId);
    this.candSel = makeSelect("diff-candidate", this.candidateId);
    const swap = h("button", {
      type: "button", class: "btn", title: "Swap baseline and candidate",
      onclick: () => this.onChange(this.candSel.value, this.baseSel.value, null),
    }, "Swap");
    return h("div", { class: "diff-bar" },
      h("label", null, "Baseline", this.baseSel),
      h("span", { class: "diff-arrow", "aria-hidden": "true" }, "→"),
      h("label", null, "Candidate", this.candSel),
      swap,
      this.verdict);
  }

  emitIds() {
    this.onChange(this.baseSel.value, this.candSel.value, null);
  }

  indexDivergences() {
    const align = this.report.alignment;
    const byBase = new Map(), byCand = new Map();
    align.forEach((p, i) => {
      if (p.baseline_event_id) byBase.set(p.baseline_event_id, i);
      if (p.candidate_event_id) byCand.set(p.candidate_event_id, i);
    });
    this.divByPair = new Map();
    this.pairOfDiv = new Map();
    this.report.divergences.forEach((d, di) => {
      let i;
      if (d.baseline_event_id && byBase.has(d.baseline_event_id)) i = byBase.get(d.baseline_event_id);
      else if (d.candidate_event_id && byCand.has(d.candidate_event_id)) i = byCand.get(d.candidate_event_id);
      if (i === undefined) return;
      if (!this.divByPair.has(i)) this.divByPair.set(i, []);
      this.divByPair.get(i).push(d);
      this.pairOfDiv.set(di, i);
    });
    const fd = this.report.first_divergence;
    this.firstPair = -1;
    if (fd) {
      const di = this.report.divergences.findIndex((d) => d.kind === fd.kind && d.message === fd.message);
      if (di >= 0 && this.pairOfDiv.has(di)) this.firstPair = this.pairOfDiv.get(di);
      else if (fd.baseline_event_id && byBase.has(fd.baseline_event_id)) this.firstPair = byBase.get(fd.baseline_event_id);
      else if (fd.candidate_event_id && byCand.has(fd.candidate_event_id)) this.firstPair = byCand.get(fd.candidate_event_id);
    }
  }

  firstPos() {
    if (this.firstPair >= 0) return this.firstPair;
    return this.report.alignment.length ? 0 : -1;
  }

  renderReport() {
    const r = this.report;
    const n = r.divergences.length;
    this.verdict.textContent = r.equivalent ? "equivalent" : `diverged · ${n} ${n === 1 ? "divergence" : "divergences"}`;
    this.verdict.className = `diff-verdict ${r.equivalent ? "is-same" : "is-diverged"}`;
    if (this.fork) this.fork.destroy();
    this.fork = new ForkView(r, this.firstPair, new Set(this.divByPair.keys()), {
      onHover: (i) => this.hoverRow(i),
      onPick: (i) => { this.select(i, { scroll: true }); this.focus(); },
    });
    const alignment = this.renderAlignment();
    mount(this.scroll,
      h("section", { class: "fork-wrap", "aria-label": "Divergence fork" },
        h("div", { class: "sec-title" }, "Timeline", h("span", { class: "count" }, "one tick per aligned call · hover to find, click to open")),
        this.fork.el),
      h("div", { class: "diff-top" }, this.renderFirst(), this.renderMetrics()),
      alignment,
      n > 1 ? this.renderDivergences() : null);
  }

  renderFirst() {
    const fd = this.report.first_divergence;
    const title = h("div", { class: "sec-title" }, "First divergence");
    if (!fd) {
      return h("section", { "aria-label": "First divergence" }, title,
        h("p", { class: "fd-none" }, "None. Every call, request and the run output match."));
    }
    const values = [];
    if (fd.path) values.push(h("dt", null, "path"), h("dd", null, fd.path));
    if (fd.expected !== null && fd.expected !== undefined) values.push(h("dt", null, "expected"), h("dd", { class: "exp" }, oneLine(fd.expected)));
    if (fd.actual !== null && fd.actual !== undefined) values.push(h("dt", null, "actual"), h("dd", { class: "act" }, oneLine(fd.actual)));
    const jump = this.firstPair >= 0
      ? h("button", { type: "button", class: "btn", onclick: () => this.select(this.firstPair, { scroll: true }) }, "Show call")
      : null;
    return h("section", { "aria-label": "First divergence" }, title,
      h("div", { class: "fd" },
        h("div", { class: "fd-label" },
          h("span", { class: "fd-call" }, fd.label || "run"),
          h("span", { class: "fd-kind" }, fd.kind.replace(/_/g, " ")),
          fd.position !== null && fd.position !== undefined ? h("span", { class: "fd-pos" }, `call #${fd.position}`) : null,
          jump),
        h("p", { class: "fd-msg" }, fd.message),
        values.length ? h("dl", { class: "fd-values" }, values) : null));
  }

  renderMetrics() {
    const b = this.report.baseline_metrics, c = this.report.candidate_metrics;
    const currency = b.currency || c.currency;
    const rows = METRICS.map(([label, key, kind]) => {
      const d = fmtDelta(b[key], c[key], kind === "tokens" ? "tokens" : kind);
      if ((b[key] === null || b[key] === undefined) && (c[key] === null || c[key] === undefined)) return null;
      return h("tr", null,
        h("td", null, label),
        h("td", { class: "num" }, fmtMetric(b[key], kind, currency)),
        h("td", { class: "num" }, fmtMetric(c[key], kind, currency)),
        h("td", { class: `num ${d.cls}` }, d.text));
    });
    return h("section", { "aria-label": "Metrics" },
      h("div", { class: "sec-title" }, "Metrics"),
      h("table", { class: "grid" },
        h("thead", null, h("tr", null, h("th", null, "metric"), h("th", { class: "num" }, "baseline"),
          h("th", { class: "num" }, "candidate"), h("th", { class: "num" }, "Δ"))),
        h("tbody", null,
          h("tr", null, h("td", null, "status"), h("td", { class: "num" }, statusLabel(b.status)),
            h("td", { class: "num" }, statusLabel(c.status)), h("td", { class: "num delta-zero" }, b.status === c.status ? "" : "changed")),
          rows)));
  }

  callCell(model, eventId) {
    if (!eventId) return h("span", { class: "call-empty" }, "—");
    const ev = model.byId(eventId);
    if (!ev) return h("span", { class: "call-empty" }, eventId);
    const i = model.index.get(eventId);
    return h("span", { class: "call" },
      h("span", { class: `tag tag-${ev.kind}` }, ev.kind),
      h("span", { class: "call-name" }, ev.name),
      ev.status === "error" ? h("span", { class: "wf-flag wf-flag-error" }, "error") : null,
      h("span", { class: "call-dur" }, fmtMs(model.duration[i])));
  }

  renderAlignment() {
    const align = this.report.alignment;
    this.tbody = h("tbody");
    this.rowEls = align.map((p, i) => {
      const divs = this.divByPair.get(i);
      const tr = h("tr", {
        class: `st-${p.status}${divs ? " has-div" : ""}${i === this.firstPair ? " is-first" : ""}`,
        role: "row", id: `al-row-${i}`, "aria-selected": "false", dataset: { i: String(i) },
        title: STATUS_TEXT[p.status] || p.status,
      },
      h("td", { class: "idx" }, String(i)),
      h("td", { class: "base" }, this.callCell(this.base, p.baseline_event_id)),
      h("td", { class: "mark", "aria-label": p.status }, MARKS[p.status] || "?"),
      h("td", { class: "cand" }, this.callCell(this.cand, p.candidate_event_id),
        i === this.firstPair ? h("span", { class: "first-flag" }, "first divergence") : null));
      return tr;
    });
    this.tbody.append(...this.rowEls);
    this.table = h("table", {
      class: "grid align", role: "grid", tabindex: "0", "aria-label": "Aligned calls", "aria-rowcount": String(align.length),
    },
    h("colgroup", null, h("col", { class: "c-idx" }), h("col"), h("col", { class: "c-mark" }), h("col")),
    h("thead", null, h("tr", null, h("th", { class: "num" }, "#"), h("th", null, `baseline · ${this.report.baseline_name}`),
      h("th", null, ""), h("th", null, `candidate · ${this.report.candidate_name}`))),
    this.tbody);
    this.table.addEventListener("mouseover", (ev) => {
      const tr = ev.target.closest("tr[data-i]");
      if (this.fork) this.fork.highlight(tr ? Number(tr.dataset.i) : -1);
    });
    this.table.addEventListener("mouseleave", () => { if (this.fork) this.fork.highlight(-1); });
    this.table.addEventListener("click", (ev) => {
      const tr = ev.target.closest("tr[data-i]");
      if (!tr) return;
      this.select(Number(tr.dataset.i), { scroll: false });
      this.table.focus({ preventScroll: true });
    });
    const counts = { same: 0, changed: 0, added: 0, removed: 0 };
    for (const p of align) counts[p.status] = (counts[p.status] || 0) + 1;
    return h("section", { class: "align-wrap", "aria-label": "Calls" },
      h("div", { class: "sec-title" }, "Calls",
        h("span", { class: "count" }, `${align.length} aligned · ${counts.same} same · ${counts.changed} changed · ${counts.added} added · ${counts.removed} removed`)),
      align.length ? this.table : h("p", { class: "empty" }, "Neither run made any calls."));
  }

  hoverRow(i) {
    if (this.hovered >= 0 && this.rowEls[this.hovered]) this.rowEls[this.hovered].classList.remove("is-hover");
    this.hovered = i;
    if (i >= 0 && this.rowEls[i]) this.rowEls[i].classList.add("is-hover");
  }

  destroy() {
    this.token++;
    if (this.fork) this.fork.destroy();
  }

  renderDivergences() {
    const rows = this.report.divergences.map((d, di) => {
      const pair = this.pairOfDiv.get(di);
      return h("tr", {
        class: pair !== undefined ? "is-link" : null,
        onclick: pair !== undefined ? () => this.select(pair, { scroll: true }) : null,
      },
      h("td", { class: "num" }, d.position === null || d.position === undefined ? "run" : String(d.position)),
      h("td", { class: "mono" }, d.kind.replace(/_/g, " ")),
      h("td", { class: "mono" }, d.label || ""),
      h("td", { class: "mono" }, d.path || ""),
      h("td", null, d.message));
    });
    return h("section", { class: "align-wrap", "aria-label": "All divergences" },
      h("div", { class: "sec-title" }, "All divergences", h("span", { class: "count" }, String(this.report.divergences.length))),
      h("table", { class: "grid" },
        h("thead", null, h("tr", null, h("th", { class: "num" }, "call"), h("th", null, "kind"), h("th", null, "label"),
          h("th", null, "path"), h("th", null, "message"))),
        h("tbody", null, rows)));
  }

  select(i, { notify = true, scroll = true } = {}) {
    if (!this.report || i < 0 || i >= this.report.alignment.length) {
      if (this.report) this.renderPairDetail(-1);
      return;
    }
    if (this.selected >= 0 && this.rowEls[this.selected]) {
      this.rowEls[this.selected].classList.remove("is-selected");
      this.rowEls[this.selected].setAttribute("aria-selected", "false");
    }
    this.selected = i;
    const tr = this.rowEls[i];
    tr.classList.add("is-selected");
    tr.setAttribute("aria-selected", "true");
    this.table.setAttribute("aria-activedescendant", tr.id);
    if (scroll) tr.scrollIntoView({ block: "nearest" });
    this.renderPairDetail(i);
    if (notify) this.onChange(this.baselineId, this.candidateId, i, { selectionOnly: true });
  }

  move(delta) {
    if (!this.report || !this.report.alignment.length) return;
    const n = this.report.alignment.length;
    const next = this.selected < 0 ? (delta > 0 ? 0 : n - 1) : Math.max(0, Math.min(n - 1, this.selected + delta));
    this.select(next);
  }

  clearSelection() {
    if (this.selected >= 0 && this.rowEls[this.selected]) {
      this.rowEls[this.selected].classList.remove("is-selected");
      this.rowEls[this.selected].setAttribute("aria-selected", "false");
    }
    this.selected = -1;
    this.renderPairDetail(-1);
  }

  focus() {
    if (this.table) this.table.focus({ preventScroll: true });
  }

  renderPairDetail(i) {
    if (i < 0) {
      mount(this.detail, h("p", { class: "empty" }, "Select a call to compare baseline and candidate."));
      return;
    }
    const p = this.report.alignment[i];
    const bev = p.baseline_event_id ? this.base.byId(p.baseline_event_id) : null;
    const cev = p.candidate_event_id ? this.cand.byId(p.candidate_event_id) : null;
    const out = [h("div", { class: "d-head" },
      h("span", { class: `tag tag-${p.kind}` }, p.kind),
      h("span", { class: "d-title" }, p.name),
      h("span", { class: "status" }, `${MARKS[p.status]} ${p.status}`))];
    const divs = this.divByPair.get(i) || [];
    if (divs.length) {
      out.push(h("section", { class: "d-section" },
        h("div", { class: "d-section-head" }, h("h3", null, "Divergences")),
        divs.map((d) => h("div", { class: "fd" },
          h("div", { class: "fd-label" }, h("span", { class: "fd-kind" }, d.kind.replace(/_/g, " ")),
            d.path ? h("span", { class: "mono" }, d.path) : null),
          h("p", { class: "fd-msg" }, d.message),
          d.path ? changeList([{ path: d.path, kind: "changed", expected: d.expected, actual: d.actual }]) : null))));
    }
    if (bev && cev) {
      out.push(payloadChanges("Request", bev.request, cev.request));
      out.push(payloadChanges("Response", bev.response, cev.response));
      if (bev.status !== cev.status || JSON.stringify(bev.error) !== JSON.stringify(cev.error)) {
        out.push(h("section", { class: "d-section" },
          h("div", { class: "d-section-head" }, h("h3", null, "Status")),
          h("div", { class: "side-head" }, "baseline ", statusLabel(bev.status), " candidate ", statusLabel(cev.status)),
          bev.error ? h("div", { class: "chg-exp mono" }, `− ${bev.error.type}: ${bev.error.message}`) : null,
          cev.error ? h("div", { class: "chg-act mono" }, `+ ${cev.error.type}: ${cev.error.message}`) : null));
      }
    }
    const side = (label, model, ev) => {
      if (!ev) return null;
      return renderEvent(model, ev, { compact: true, label });
    };
    out.push(side("Baseline", this.base, bev), side("Candidate", this.cand, cev));
    mount(this.detail, out);
    this.detail.scrollTop = 0;
  }
}

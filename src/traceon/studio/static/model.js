// Derived, render-ready view of a fixture: timing offsets, the span tree and
// run-level totals. Built once per fixture load.

import { keyPath, parseTs } from "./format.js";

const CALL_KINDS = new Set(["tool", "model", "value"]);

export class FixtureModel {
  constructor(fx) {
    this.fx = fx;
    this.events = Array.isArray(fx.events) ? fx.events : [];
    this.index = new Map();
    this.children = new Map();
    this.roots = [];
    this.start = [];
    this.duration = [];
    this.depth = [];

    let t0 = Infinity;
    const starts = this.events.map((ev) => parseTs(ev.started_at));
    for (const t of starts) if (Number.isFinite(t) && t < t0) t0 = t;
    if (!Number.isFinite(t0)) t0 = 0;
    this.t0 = t0;

    let end = 0;
    this.events.forEach((ev, i) => {
      this.index.set(ev.event_id, i);
      const st = Number.isFinite(starts[i]) ? starts[i] - t0 : 0;
      let dur = typeof ev.duration_ms === "number" ? ev.duration_ms : null;
      if (dur === null && ev.ended_at) {
        const e = parseTs(ev.ended_at);
        if (Number.isFinite(e)) dur = Math.max(0, e - starts[i]);
      }
      this.start.push(st);
      this.duration.push(dur);
      end = Math.max(end, st + (dur || 0));
    });
    this.total = end > 0 ? end : 1;

    this.events.forEach((ev, i) => {
      const parent = ev.parent_id;
      if (parent !== null && parent !== undefined && this.index.has(parent)) {
        const pi = this.index.get(parent);
        if (!this.children.has(pi)) this.children.set(pi, []);
        this.children.get(pi).push(i);
      } else {
        this.roots.push(i);
      }
    });
    // Depth via iterative walk from roots (schema guarantees no cycles).
    this.depth = new Array(this.events.length).fill(0);
    const stack = this.roots.map((i) => [i, 0]);
    while (stack.length) {
      const [i, d] = stack.pop();
      this.depth[i] = d;
      for (const c of this.children.get(i) || []) stack.push([c, d + 1]);
    }

    this.callIndex = new Map();
    let n = 0;
    this.events.forEach((ev, i) => { if (CALL_KINDS.has(ev.kind)) this.callIndex.set(i, n++); });
  }

  byId(eventId) {
    const i = this.index.get(eventId);
    return i === undefined ? null : this.events[i];
  }

  /** Ordered visible row indexes given a set of collapsed event indexes. */
  visible(collapsed) {
    const out = [];
    const stack = [...this.roots].reverse();
    while (stack.length) {
      const i = stack.pop();
      out.push(i);
      if (collapsed.has(i)) continue;
      const kids = this.children.get(i);
      if (kids) for (let k = kids.length - 1; k >= 0; k--) stack.push(kids[k]);
    }
    return out;
  }
}

export function totals(fx) {
  let toolCalls = 0, modelCalls = 0, valueCalls = 0, errors = 0;
  let inTok = null, outTok = null, measured = null, estimated = null;
  const currencies = new Set();
  for (const ev of fx.events || []) {
    if (ev.kind === "tool") toolCalls++;
    else if (ev.kind === "model") modelCalls++;
    else if (ev.kind === "value") valueCalls++;
    if (ev.status === "error") errors++;
    const u = ev.usage;
    if (!u) continue;
    if (typeof u.input_tokens === "number") inTok = (inTok || 0) + u.input_tokens;
    if (typeof u.output_tokens === "number") outTok = (outTok || 0) + u.output_tokens;
    if (u.cost && typeof u.cost.amount === "number") {
      currencies.add(u.cost.currency || "USD");
      if (u.cost.kind === "measured") measured = (measured || 0) + u.cost.amount;
      else estimated = (estimated || 0) + u.cost.amount;
    }
  }
  return {
    toolCalls, modelCalls, valueCalls, errors, inTok, outTok, measured, estimated,
    currency: currencies.size === 1 ? [...currencies][0] : null,
  };
}

function typeTag(v) {
  if (v === null || v === undefined) return "null";
  if (Array.isArray(v)) return "array";
  if (typeof v === "boolean") return "bool";
  if (typeof v === "number") return "number";
  if (typeof v === "string") return "string";
  return "object";
}

/** Structural differences between two JSON values; mirrors traceon.core.compare. */
export function diffValues(expected, actual, path = "$", limit = 100) {
  const out = [];
  const walk = (e, a, p) => {
    if (out.length >= limit) return;
    const te = typeTag(e), ta = typeTag(a);
    if (te !== ta) { out.push({ path: p, kind: "type_changed", expected: e, actual: a }); return; }
    if (te === "object") {
      const keys = [...new Set([...Object.keys(e), ...Object.keys(a)])].sort();
      for (const k of keys) {
        if (out.length >= limit) return;
        const sub = keyPath(p, k);
        if (!(k in a)) out.push({ path: sub, kind: "removed", expected: e[k], actual: undefined });
        else if (!(k in e)) out.push({ path: sub, kind: "added", expected: undefined, actual: a[k] });
        else walk(e[k], a[k], sub);
      }
      return;
    }
    if (te === "array") {
      for (let i = 0; i < Math.max(e.length, a.length); i++) {
        if (out.length >= limit) return;
        const sub = `${p}[${i}]`;
        if (i >= a.length) out.push({ path: sub, kind: "removed", expected: e[i], actual: undefined });
        else if (i >= e.length) out.push({ path: sub, kind: "added", expected: undefined, actual: a[i] });
        else walk(e[i], a[i], sub);
      }
      return;
    }
    if (e !== a) out.push({ path: p, kind: "changed", expected: e, actual: a });
  };
  walk(expected, actual, path);
  return out;
}

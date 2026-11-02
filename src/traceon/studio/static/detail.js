// Right-hand detail pane: event fields, payloads, errors, usage; run summary.

import { copyButton, h } from "./dom.js";
import {
  fmtBytes, fmtCost, fmtExactMs, fmtInt, oneLine, shortHash,
} from "./format.js";
import { jsonTree } from "./jsonview.js";
import { diffValues } from "./model.js";

const PAYLOAD_STATES = {
  captured: {
    label: "captured",
    title: "Captured: the full value was recorded and replay can serve it.",
  },
  truncated: {
    label: "truncated",
    title: "Truncated: recorded, then cut to fit the payload size limit. Size and sha256 describe the full value. Replay cannot serve a truncated response.",
  },
  redacted: {
    label: "redacted",
    title: "Redacted: captured, then removed by the redaction policy. Nothing of the value is stored; replay cannot serve it.",
  },
  hash_only: {
    label: "hash only",
    title: "Hash only: just the sha256 of the value was stored. Enough to detect changes, not to read or serve the value.",
  },
  not_captured: {
    label: "not captured",
    title: "Not captured: capture was disabled for this payload, so nothing was recorded.",
  },
};

const STATE_NOTES = {
  redacted: "Removed by the redaction policy.",
  hash_only: "Only a hash was stored. Changes are detectable; the content is not.",
  not_captured: "Capture was disabled for this payload.",
};

export function payloadStateKey(p) {
  if (!p) return "not_captured";
  if (p.state === "captured" && p.truncated) return "truncated";
  return PAYLOAD_STATES[p.state] ? p.state : "not_captured";
}

export function payloadBadge(p) {
  const key = payloadStateKey(p);
  const info = PAYLOAD_STATES[key];
  return h("span", { class: `pstate pstate-${key}`, title: info.title }, info.label);
}

export function statusLabel(status) {
  const st = ["ok", "error", "cancelled"].includes(status) ? status : "ok";
  return h("span", { class: `status status-${st}` },
    h("span", { class: `st st-${st}`, "aria-hidden": "true" }), st);
}

function section(title, extra, ...body) {
  return h("section", { class: "d-section" },
    h("div", { class: "d-section-head" }, h("h3", null, title), h("span", { class: "fill" }), extra),
    body);
}

function kv(rows) {
  const dl = h("dl", { class: "kv" });
  for (const row of rows) {
    if (!row) continue;
    const [k, v, plain] = row;
    if (v === null || v === undefined || v === "") continue;
    dl.append(h("dt", null, k), h("dd", { class: plain ? "plain" : null }, v));
  }
  return dl;
}

export function payloadSection(title, payload) {
  const p = payload || { state: "not_captured" };
  const key = payloadStateKey(p);
  const meta = [];
  if (typeof p.size_bytes === "number") meta.push(fmtBytes(p.size_bytes));
  if (p.sha256) meta.push(shortHash(p.sha256));
  const extras = [payloadBadge(p)];
  if (meta.length) extras.push(h("span", { class: "p-meta", title: p.sha256 || "" }, meta.join(" · ")));
  const captured = p.state === "captured";
  if (captured) extras.push(copyButton(() => JSON.stringify(p.value, null, 2)));
  const body = [];
  if (captured) {
    body.push(jsonTree(p.value, { label: `${title} payload` }));
    if (p.truncated) body.push(h("p", { class: "p-note" }, "Value shown is truncated."));
  } else {
    body.push(h("p", { class: "p-note" }, STATE_NOTES[key] || ""));
  }
  if (Array.isArray(p.redacted_paths) && p.redacted_paths.length) {
    body.push(h("div", { class: "p-redacted-paths" }, "Redacted at ",
      p.redacted_paths.map((path, i) => [i ? ", " : "", h("code", null, path)])));
  }
  return section(title, extras, body);
}

function errorBlock(err) {
  if (!err) return null;
  return h("div", { class: "err-block", role: "group", "aria-label": "Error" },
    h("div", { class: "err-type" }, err.type || "error"),
    h("div", { class: "err-msg" }, err.message || ""),
    Array.isArray(err.stack) && err.stack.length ? h("pre", { class: "err-stack" }, err.stack.join("")) : null);
}

function usageRows(usage) {
  if (!usage) return null;
  const c = usage.cost;
  return kv([
    ["input tokens", typeof usage.input_tokens === "number" ? fmtInt(usage.input_tokens) : null],
    ["output tokens", typeof usage.output_tokens === "number" ? fmtInt(usage.output_tokens) : null],
    ["total tokens", typeof usage.total_tokens === "number" ? fmtInt(usage.total_tokens) : null],
    c ? ["cost", `${fmtCost(c.amount, c.currency)} ${c.kind}`] : null,
    c && c.price_source ? ["price source", c.price_source, true] : null,
  ]);
}

/** Header + full detail for one event. `onSelectEvent(eventId)` follows parent links. */
export function renderEvent(model, ev, { onSelectEvent, compact = false, label = "" } = {}) {
  const t = (name) => (label ? `${label} ${name.toLowerCase()}` : name);
  const i = model.index.get(ev.event_id);
  const parent = ev.parent_id ? model.byId(ev.parent_id) : null;
  const parentLink = parent
    ? h("button", { type: "button", class: "linkish", onclick: () => onSelectEvent && onSelectEvent(parent.event_id) },
      `${parent.kind}:${parent.name}`)
    : null;
  const callNo = model.callIndex.has(i) ? `#${model.callIndex.get(i)}` : null;
  const out = [];
  if (!compact) {
    out.push(h("div", { class: "d-head" },
      h("span", { class: `tag tag-${ev.kind}` }, ev.kind),
      h("span", { class: "d-title", title: ev.name }, ev.name),
      statusLabel(ev.status)));
  }
  out.push(section(t("Event"), copyButton(() => ev.event_id, "Copy id"), kv([
    ["event id", ev.event_id],
    ["call", callNo],
    ["sequence", String(ev.sequence)],
    ["parent", parentLink],
    ["start", `+${fmtExactMs(model.start[i])}`],
    ["started at", ev.started_at],
    ["duration", fmtExactMs(model.duration[i])],
    ["status", ev.status],
    ["side effect", ev.side_effect ? "yes: mutates the outside world" : null, true],
    ["call key", ev.call_key],
  ])));
  if (ev.error) out.push(section(t("Error"), null, errorBlock(ev.error)));
  if (ev.usage) out.push(section(t("Usage"), null, usageRows(ev.usage)));
  if (ev.kind !== "span" || (ev.request && ev.request.state !== "not_captured")) {
    out.push(payloadSection(t("Request"), ev.request));
  }
  if (ev.kind !== "span" || (ev.response && ev.response.state !== "not_captured")) {
    out.push(payloadSection(t("Response"), ev.response));
  }
  if (ev.attributes && Object.keys(ev.attributes).length) {
    out.push(section(t("Attributes"), null, jsonTree(ev.attributes, { expandDepth: 1, label: "Attributes" })));
  }
  return out;
}

/** Run-level detail shown when no event is selected. */
export function renderRun(model, summary) {
  const fx = model.fx;
  const env = fx.environment || {};
  const red = fx.redaction || {};
  const adapters = env.adapters && Object.keys(env.adapters).length
    ? Object.entries(env.adapters).map(([k, v]) => `${k} ${v}`).join(", ")
    : null;
  const out = [
    h("div", { class: "d-head" },
      h("span", { class: "tag tag-span" }, "run"),
      h("span", { class: "d-title", title: fx.name }, fx.name),
      statusLabel(fx.status)),
    section("Run", null, kv([
      ["fixture id", fx.fixture_id],
      ["run id", fx.run_id],
      ["file", summary ? summary.file : null],
      ["created at", fx.created_at],
      ["mode", fx.mode],
      ["source", fx.source],
      ["schema", fx.schema_version],
    ])),
  ];
  if (fx.error) out.push(section("Run error", null, errorBlock(fx.error)));
  out.push(payloadSection("Input", fx.input));
  out.push(payloadSection("Output", fx.output));
  out.push(section("Environment", null, kv([
    ["python", env.python],
    ["platform", env.platform],
    ["traceon", env.traceon_version],
    ["adapters", adapters],
  ])));
  out.push(section("Redaction", null, kv([
    ["policy", red.policy_id],
    ["rules", Array.isArray(red.rules) && red.rules.length ? red.rules.join(", ") : "none"],
    ["fields omitted", Array.isArray(red.fields_omitted) && red.fields_omitted.length ? red.fields_omitted.join(", ") : "none"],
  ])));
  if (fx.metadata && Object.keys(fx.metadata).length) {
    out.push(section("Metadata", null, jsonTree(fx.metadata, { expandDepth: 1, label: "Metadata" })));
  }
  if (Array.isArray(fx.warnings) && fx.warnings.length) {
    out.push(section(`Warnings (${fx.warnings.length})`, null,
      h("ul", { class: "p-note" }, fx.warnings.map((w) => h("li", null, w)))));
  }
  return out;
}

/** List of value changes as path / expected / actual rows. */
export function changeList(changes) {
  if (!changes.length) return h("p", { class: "p-note" }, "No differences.");
  return h("div", { class: "changes" }, changes.map((c) => h("div", { class: "chg" },
    h("div", null, h("span", { class: "chg-path" }, c.path), h("span", { class: "chg-kind" }, c.kind.replace("_", " "))),
    c.kind !== "added" ? h("div", { class: "chg-exp" }, `− ${oneLine(c.expected)}`) : null,
    c.kind !== "removed" ? h("div", { class: "chg-act" }, `+ ${oneLine(c.actual)}`) : null)));
}

/** Compare two payloads for the diff detail pane. */
export function payloadChanges(title, a, b) {
  if (a && b && a.state === "captured" && b.state === "captured") {
    const changes = diffValues(a.value, b.value);
    const note = a.truncated || b.truncated ? h("p", { class: "p-note" }, "At least one side is truncated; differences past the cut are not shown.") : null;
    return section(`${title} changes`, h("span", { class: "d-sub" }, changes.length ? `${changes.length}${changes.length >= 100 ? "+" : ""}` : "none"),
      changeList(changes), note);
  }
  const ha = a && a.sha256, hb = b && b.sha256;
  let verdict = "Cannot compare: content not available on both sides.";
  if (ha && hb) verdict = ha === hb ? "Hashes match: content identical." : "Hashes differ: content changed.";
  return section(`${title} changes`, null,
    h("div", { class: "side-head" }, "baseline ", payloadBadge(a), " candidate ", payloadBadge(b)),
    h("p", { class: "p-note" }, verdict));
}

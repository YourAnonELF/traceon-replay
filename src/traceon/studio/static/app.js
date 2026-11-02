// Traceon Studio entry point: routing, fixture list, run header, keyboard.

import { h, mount, storage } from "./dom.js";
import { renderEvent, renderRun, statusLabel } from "./detail.js";
import { DiffView } from "./diffview.js";
import { fmtCost, fmtInt, fmtMs, relTime, shortId } from "./format.js";
import { defaultPair, pickPartner } from "./logic.js";
import { FixtureModel, totals } from "./model.js";
import { Player } from "./playback.js";
import { Waterfall } from "./waterfall.js";

const $ = (id) => document.getElementById(id);
const els = {
  app: $("app"),
  list: $("fixture-list"),
  filter: $("filter"),
  foot: $("sidebar-foot"),
  main: $("main"),
  detail: $("detail"),
  splitter: $("splitter"),
  tabTrace: $("tab-trace"),
  tabDiff: $("tab-diff"),
  theme: $("theme-toggle"),
};

// ---------------------------------------------------------------------------
// API

async function getJSON(url) {
  let resp;
  try {
    resp = await fetch(url, { headers: { Accept: "application/json" }, cache: "no-store" });
  } catch (err) {
    throw new Error("Cannot reach the Studio server. Is `traceon studio` still running?");
  }
  let body = null;
  try { body = await resp.json(); } catch (err) { body = null; }
  if (!resp.ok) {
    const msg = body && body.error && body.error.message ? body.error.message : `HTTP ${resp.status}`;
    throw new Error(msg);
  }
  return body;
}

const fixtureCache = new Map();
const api = {
  list: () => getJSON("/api/fixtures"),
  fixture(id) {
    if (!fixtureCache.has(id)) {
      const p = getJSON(`/api/fixtures/${encodeURIComponent(id)}`);
      p.catch(() => fixtureCache.delete(id));
      fixtureCache.set(id, p);
    }
    return fixtureCache.get(id);
  },
  diff: (b, c) => getJSON(`/api/diff?baseline=${encodeURIComponent(b)}&candidate=${encodeURIComponent(c)}`),
};

// ---------------------------------------------------------------------------
// State

const state = {
  summaries: [],
  mode: "trace",
  runId: null,
  model: null,
  waterfall: null,
  player: null,
  diff: null,
  diffIds: [null, null],
  listActive: -1,
};

// ---------------------------------------------------------------------------
// Routing: #/run/<id>?event=<event_id>&t=<ms>  and  #/diff/<baseline>/<candidate>?pos=<n>

function parseHash() {
  const raw = location.hash.replace(/^#/, "");
  const [path, query = ""] = raw.split("?");
  const parts = path.split("/").filter(Boolean).map((p) => {
    try { return decodeURIComponent(p); } catch (err) { return p; }
  });
  const params = new URLSearchParams(query);
  if (parts[0] === "diff") {
    const pos = params.has("pos") ? Number.parseInt(params.get("pos"), 10) : null;
    return { mode: "diff", baseline: parts[1] || null, candidate: parts[2] || null, pos: Number.isInteger(pos) ? pos : null };
  }
  if (parts[0] === "run" && parts[1]) {
    const t = params.has("t") ? Number.parseFloat(params.get("t")) : null;
    return { mode: "trace", runId: parts[1], eventId: params.get("event"), t: Number.isFinite(t) && t >= 0 ? t : null };
  }
  return { mode: "trace", runId: null, eventId: null, t: null };
}

function runHash(id, eventId, t) {
  const q = new URLSearchParams();
  if (eventId) q.set("event", eventId);
  if (t !== null && t !== undefined && Number.isFinite(t)) q.set("t", String(Math.round(t * 1000) / 1000));
  const qs = q.toString();
  return `#/run/${encodeURIComponent(id)}${qs ? `?${qs}` : ""}`;
}

function playheadT() {
  return state.player && state.player.t !== null ? state.player.t : null;
}

function diffHash(b, c, pos) {
  if (!b && !c) return "#/diff";
  return `#/diff/${encodeURIComponent(b || "")}/${encodeURIComponent(c || "")}${Number.isInteger(pos) ? `?pos=${pos}` : ""}`;
}

// Selection changes rewrite the hash without adding history entries. Debounced:
// browsers throttle rapid history API calls while j/k is held down.
let pendingHash = null;
let hashTimer = 0;
function replaceHash(hash) {
  pendingHash = hash;
  if (hashTimer) return;
  hashTimer = window.setTimeout(() => {
    hashTimer = 0;
    if (pendingHash !== null && location.hash !== pendingHash) history.replaceState(null, "", pendingHash);
    pendingHash = null;
  }, 120);
}

function navigate(hash) {
  if (hashTimer) { window.clearTimeout(hashTimer); hashTimer = 0; pendingHash = null; }
  if (location.hash === hash) route();
  else location.hash = hash;
}

async function route() {
  const r = parseHash();
  setModeTabs(r.mode);
  if (!state.summaries.length) {
    renderNoFixtures();
    return;
  }
  if (r.mode === "diff") {
    await openDiff(r);
  } else {
    const id = r.runId && state.summaries.some((s) => s.id === r.runId) ? r.runId : null;
    if (r.runId && !id) {
      teardownViews();
      state.mode = "trace";
      state.runId = null;
      renderList();
      mount(els.main, h("p", { class: "empty" }, `No loaded fixture has id "${r.runId}".`));
      mount(els.detail);
      return;
    }
    await openRun(id || state.summaries[0].id, r.eventId, r.t);
  }
}

// ---------------------------------------------------------------------------
// Fixture list

function summaryById(id) {
  return state.summaries.find((s) => s.id === id) || null;
}

function filtered() {
  const terms = els.filter.value.toLowerCase().split(/\s+/).filter(Boolean);
  if (!terms.length) return state.summaries;
  return state.summaries.filter((s) => {
    const hay = [s.name, s.id, s.run_id, s.source, s.file, s.status, s.mode].join(" ").toLowerCase();
    return terms.every((t) => hay.includes(t));
  });
}

function renderList() {
  const items = filtered();
  const currentId = state.mode === "trace" ? state.runId : null;
  const [b, c] = state.mode === "diff" ? state.diffIds : [null, null];
  els.list.replaceChildren(...items.map((s, n) => {
    const role = s.id === b ? "base" : s.id === c ? "cand" : null;
    const opt = h("div", {
      class: `fx-item${role === "base" ? " is-diff-baseline" : ""}${role === "cand" ? " is-diff-candidate" : ""}`,
      role: "option",
      id: `fx-opt-${n}`,
      "aria-selected": String(s.id === currentId),
      dataset: { id: s.id },
      title: `${s.name}\n${s.id} · ${s.file}`,
    },
    h("span", { class: `fx-status st st-${s.status}`, title: s.status, "aria-label": s.status }),
    h("span", { class: "fx-name" }, s.name),
    role ? h("span", { class: "fx-role" }, role) : h("span"),
    h("span", { class: "fx-meta" },
      h("span", { title: s.created_at }, relTime(s.created_at)),
      h("span", null, `${fmtInt(s.calls)} calls`),
      s.errors ? h("span", { class: "error-text" }, `${s.errors} err`) : null,
      s.mode === "replay" ? h("span", { class: "badge" }, "replay") : null,
      s.source !== "traceon" ? h("span", { class: "badge badge-accent" }, s.source) : null));
    return opt;
  }));
  if (!items.length && state.summaries.length) {
    els.list.append(h("p", { class: "empty" }, "No fixtures match the filter."));
  }
  const activeIdx = items.findIndex((s) => s.id === currentId);
  state.listActive = activeIdx;
  if (activeIdx >= 0) els.list.setAttribute("aria-activedescendant", `fx-opt-${activeIdx}`);
  else els.list.removeAttribute("aria-activedescendant");
}

function listMove(delta) {
  const items = filtered();
  if (!items.length) return;
  let i = state.listActive < 0 ? 0 : state.listActive + delta;
  i = Math.max(0, Math.min(items.length - 1, i));
  navigate(runHash(items[i].id));
  const el = $(`fx-opt-${i}`);
  if (el) el.scrollIntoView({ block: "nearest" });
}

els.list.addEventListener("click", (ev) => {
  const item = ev.target.closest(".fx-item");
  if (!item) return;
  navigate(runHash(item.dataset.id));
});

els.filter.addEventListener("input", renderList);

function renderWarnings(warnings) {
  if (!warnings || !warnings.length) { mount(els.foot); return; }
  mount(els.foot,
    h("p", null, `${warnings.length} ${warnings.length === 1 ? "file was" : "files were"} skipped:`),
    warnings.map((w) => h("p", { class: "warn" }, w)));
}

// ---------------------------------------------------------------------------
// Trace view

function teardownViews() {
  if (state.player) { state.player.destroy(); state.player = null; }
  if (state.waterfall) { state.waterfall.destroy(); state.waterfall = null; }
  if (state.diff) { state.diff.destroy(); state.diff = null; }
}

/** Restart a short opacity fade on el (fixture switch, detail follow). */
function fadeIn(el, cls = "is-fading") {
  el.classList.remove(cls);
  void el.offsetWidth;
  el.classList.add(cls);
}

function rhItem(key, ...value) {
  return h("div", { class: "rh-item" }, h("span", { class: "rh-k" }, key), h("span", { class: "rh-v" }, value));
}

function runHeader(model, summary) {
  const fx = model.fx;
  const t = totals(fx);
  const env = fx.environment || {};
  const red = fx.redaction || {};
  const sep = () => h("span", { class: "sep" }, " / ");
  const cost = [];
  if (t.measured !== null) cost.push(`${fmtCost(t.measured, t.currency)} measured`);
  if (t.estimated !== null) cost.push(`${fmtCost(t.estimated, t.currency)} est.`);
  const ruleCount = Array.isArray(red.rules) ? red.rules.length : 0;
  return h("section", { class: "run-header", "aria-label": "Run summary" },
    h("h1", { class: "run-title", title: fx.name }, fx.name),
    h("div", { class: "rh-item", title: fx.run_id }, h("span", { class: "rh-k" }, "run"), h("span", { class: "rh-v" }, shortId(fx.run_id))),
    h("div", { class: "rh-item" }, statusLabel(fx.status)),
    rhItem("duration", fmtMs(summary ? summary.duration_ms : model.total)),
    rhItem("calls", `${t.toolCalls} tool`, sep(), `${t.modelCalls} model`, sep(), `${t.valueCalls} value`),
    t.errors ? h("div", { class: "rh-item" }, h("span", { class: "rh-k" }, "errors"), h("span", { class: "rh-v error-text" }, String(t.errors))) : null,
    t.inTok !== null || t.outTok !== null ? rhItem("tokens", `${fmtInt(t.inTok)} in`, sep(), `${fmtInt(t.outTok)} out`) : null,
    cost.length ? rhItem("cost", cost.join(" + ")) : null,
    env.python ? rhItem("python", env.python) : null,
    env.traceon_version ? rhItem("traceon", env.traceon_version) : null,
    rhItem("redaction", `${red.policy_id || "default"} · ${ruleCount} ${ruleCount === 1 ? "rule" : "rules"}`),
    fx.mode === "replay" ? h("div", { class: "rh-item" }, h("span", { class: "badge" }, "replay")) : null,
    fx.source !== "traceon" ? h("div", { class: "rh-item" }, h("span", { class: "badge badge-accent" }, fx.source)) : null);
}

function showEventDetail(ev, { fade = false } = {}) {
  const model = state.model;
  if (!model) return;
  if (fade) fadeIn(els.detail, "is-following");
  if (!ev) {
    mount(els.detail, renderRun(model, summaryById(state.runId)));
    replaceHash(runHash(state.runId, null, playheadT()));
    return;
  }
  mount(els.detail, renderEvent(model, ev, {
    onSelectEvent: (id) => {
      const i = model.index.get(id);
      if (i !== undefined && state.waterfall) state.waterfall.select(i, { scroll: "center" });
    },
  }));
  els.detail.scrollTop = 0;
  replaceHash(runHash(state.runId, ev.event_id, playheadT()));
}

// During fast playback the current call can change every frame; render the
// detail pane at most every 90 ms, always ending on the latest call.
let followTimer = 0;
let followLast = 0;
let followEvent = null;
function followDetail(ev) {
  followEvent = ev;
  if (followTimer) return;
  const wait = Math.max(0, 90 - (performance.now() - followLast));
  followTimer = window.setTimeout(() => {
    followTimer = 0;
    followLast = performance.now();
    if (followEvent && state.model && state.model.byId(followEvent.event_id) === followEvent) {
      showEventDetail(followEvent, { fade: true });
    }
  }, wait);
}

async function openRun(id, eventId, t = null) {
  if (state.mode === "trace" && state.runId === id && state.model && state.waterfall) {
    const i = eventId ? state.model.index.get(eventId) : undefined;
    if (i !== undefined) state.waterfall.select(i, { scroll: "center" });
    if (t !== null && state.player && t !== state.player.t) state.player.seek(t);
    return;
  }
  teardownViews();
  state.mode = "trace";
  state.runId = id;
  state.model = null;
  renderList();
  mount(els.main, h("p", { class: "empty" }, "Loading fixture."));
  mount(els.detail);
  let fx;
  try {
    fx = await api.fixture(id);
  } catch (err) {
    if (state.runId !== id) return;
    mount(els.main, h("p", { class: "empty error-text" }, `Cannot load fixture ${id}: ${err.message}`));
    return;
  }
  if (state.runId !== id || state.mode !== "trace") return;
  const model = new FixtureModel(fx);
  state.model = model;
  mount(els.main, runHeader(model, summaryById(id)));
  fadeIn(els.main);
  fadeIn(els.detail);
  if (!model.events.length) {
    els.main.append(h("p", { class: "empty" }, "This run recorded no events."));
    showEventDetail(null);
    return;
  }
  const wf = new Waterfall(els.main, {
    onSelect: (ev) => showEventDetail(ev),
    onScrub: (time, phase) => { if (state.player) state.player.scrub(time, phase); },
  });
  const player = new Player(model, wf, {
    onCurrent: (ev) => {
      if (!ev) return;
      wf.markSelected(model.index.get(ev.event_id));
      followDetail(ev);
    },
    onSettle: () => {
      const sel = wf.selected >= 0 ? model.events[wf.selected].event_id : null;
      replaceHash(runHash(id, sel, playheadT()));
    },
  });
  // Transport sits between the run header and the waterfall.
  els.main.append(player.el);
  state.waterfall = wf;
  state.player = player;
  wf.setModel(model, eventId);
  if (t !== null) {
    player.seek(t);
    if (eventId && model.index.has(eventId)) wf.select(model.index.get(eventId), { scroll: "center", notify: false });
  }
  const shown = eventId && model.index.has(eventId) ? model.byId(eventId) : null;
  showEventDetail(shown || (t !== null && wf.selected >= 0 ? model.events[wf.selected] : null));
}

// ---------------------------------------------------------------------------
// Diff view

function defaultDiffIds() {
  // Same run name first (ignoring a trailing "(variant)"); the older run is the baseline.
  return defaultPair(state.summaries, state.runId);
}

async function openDiff(r) {
  let b = r.baseline, c = r.candidate;
  if (!b && !c) {
    [b, c] = state.diffIds[0] ? state.diffIds : defaultDiffIds();
    if (b && c) { replaceHash(diffHash(b, c, null)); }
  }
  const known = (id) => id && state.summaries.some((s) => s.id === id);
  b = known(b) ? b : null;
  c = known(c) ? c : null;
  if (b && !c) c = pickPartner(state.summaries, b);
  else if (c && !b) b = pickPartner(state.summaries, c);
  if (b && c && (b !== r.baseline || c !== r.candidate)) replaceHash(diffHash(b, c, r.pos));
  if (state.mode === "diff" && state.diff && state.diffIds[0] === b && state.diffIds[1] === c) {
    if (Number.isInteger(r.pos) && r.pos !== state.diff.selected) state.diff.select(r.pos, { notify: false });
    return;
  }
  teardownViews();
  state.mode = "diff";
  state.diffIds = [b, c];
  renderList();
  state.diff = new DiffView(els.main, els.detail, api, {
    onChange(nb, nc, pos, opts) {
      if (opts && opts.selectionOnly) replaceHash(diffHash(nb, nc, pos));
      else navigate(diffHash(nb || null, nc || null, pos));
    },
  });
  await state.diff.open(state.summaries, b, c, r.pos);
  fadeIn(els.main);
}

// ---------------------------------------------------------------------------
// Chrome: tabs, theme, splitter, keyboard

function setModeTabs(mode) {
  els.tabTrace.setAttribute("aria-selected", String(mode === "trace"));
  els.tabDiff.setAttribute("aria-selected", String(mode === "diff"));
}

function toggleMode() {
  if (state.mode === "diff") {
    navigate(runHash(state.diffIds[1] || state.runId || (state.summaries[0] && state.summaries[0].id) || ""));
  } else {
    const [b, c] = state.diffIds[0] && state.diffIds[1] ? state.diffIds : defaultDiffIds();
    navigate(diffHash(b, c, null));
  }
}

els.tabTrace.addEventListener("click", () => { if (state.mode !== "trace") toggleMode(); });
els.tabDiff.addEventListener("click", () => { if (state.mode !== "diff") toggleMode(); });

const THEMES = ["auto", "light", "dark"];
function applyTheme(theme) {
  if (theme === "light" || theme === "dark") {
    document.documentElement.setAttribute("data-theme", theme);
    storage.set("traceon.theme", theme);
  } else {
    document.documentElement.removeAttribute("data-theme");
    storage.remove("traceon.theme");
  }
  const label = theme === "light" ? "Light" : theme === "dark" ? "Dark" : "Auto";
  els.theme.textContent = label;
  els.theme.title = theme === "auto"
    ? "Theme follows the system setting. Click to switch."
    : `Theme: ${label.toLowerCase()}. Click to switch.`;
}
els.theme.addEventListener("click", () => {
  const cur = document.documentElement.getAttribute("data-theme") || "auto";
  applyTheme(THEMES[(THEMES.indexOf(cur) + 1) % THEMES.length]);
});
applyTheme(document.documentElement.getAttribute("data-theme") || "auto");

function setDetailWidth(px) {
  const max = Math.max(320, Math.min(900, window.innerWidth - 520));
  const w = Math.round(Math.max(280, Math.min(max, px)));
  els.app.style.setProperty("--detail-w", `${w}px`);
  els.splitter.setAttribute("aria-valuenow", String(w));
  return w;
}
{
  const saved = Number.parseInt(storage.get("traceon.detailWidth") || "", 10);
  if (Number.isFinite(saved)) setDetailWidth(saved);
}
els.splitter.addEventListener("pointerdown", (ev) => {
  ev.preventDefault();
  els.splitter.setPointerCapture(ev.pointerId);
  els.splitter.classList.add("is-dragging");
  const move = (e) => setDetailWidth(window.innerWidth - e.clientX - 2);
  const up = () => {
    els.splitter.classList.remove("is-dragging");
    els.splitter.removeEventListener("pointermove", move);
    els.splitter.removeEventListener("pointerup", up);
    storage.set("traceon.detailWidth", els.splitter.getAttribute("aria-valuenow"));
  };
  els.splitter.addEventListener("pointermove", move);
  els.splitter.addEventListener("pointerup", up);
});
els.splitter.addEventListener("keydown", (ev) => {
  const cur = Number(els.splitter.getAttribute("aria-valuenow")) || 420;
  if (ev.key === "ArrowLeft") setDetailWidth(cur + 20);
  else if (ev.key === "ArrowRight") setDetailWidth(cur - 20);
  else return;
  ev.preventDefault();
  ev.stopPropagation();
  storage.set("traceon.detailWidth", els.splitter.getAttribute("aria-valuenow"));
});

function isTyping(target) {
  return target instanceof HTMLElement && (target.isContentEditable || ["INPUT", "SELECT", "TEXTAREA"].includes(target.tagName));
}

document.addEventListener("keydown", (ev) => {
  if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
  if (isTyping(ev.target)) {
    if (ev.key === "Escape" && ev.target === els.filter) {
      if (els.filter.value) { els.filter.value = ""; renderList(); } else els.filter.blur();
      ev.preventDefault();
    } else if (ev.key === "Enter" && ev.target === els.filter) {
      const first = filtered()[0];
      if (first) navigate(runHash(first.id));
      ev.preventDefault();
    }
    return;
  }
  // Let focused buttons handle Enter/Space natively -- except the speed radios: once a speed
  // is chosen, Space should start playback rather than re-select the same speed.
  if (ev.target instanceof HTMLElement && ev.target.closest("button:not(.tp-speed-btn)") && (ev.key === "Enter" || ev.key === " ")) return;
  const inList = els.list.contains(document.activeElement);
  const view = state.mode === "diff" ? state.diff : state.waterfall;
  const key = ev.key;
  let handled = true;
  if (key === "/") els.filter.focus();
  else if (key === "d") toggleMode();
  else if (key === "Escape") {
    if (els.filter.value) { els.filter.value = ""; renderList(); }
    if (view) view.clearSelection();
  } else if (key === "j" || key === "ArrowDown") {
    if (inList) listMove(1); else if (view) { view.move(1); view.focus(); }
  } else if (key === "k" || key === "ArrowUp") {
    if (inList) listMove(-1); else if (view) { view.move(-1); view.focus(); }
  } else if (state.mode === "trace" && state.player && (key === " " || key === "," || key === ".")) {
    if (key === " ") state.player.toggle();
    else state.player.step(key === "." ? 1 : -1);
  } else if (state.mode === "trace" && state.waterfall && !inList) {
    const wf = state.waterfall;
    if (key === "Enter" || key === "ArrowRight") wf.expand();
    else if (key === "ArrowLeft") wf.collapse();
    else if (key === "Home") { wf.select(wf.rows[0]); }
    else if (key === "End") { wf.select(wf.rows[wf.rows.length - 1]); }
    else if (key === "PageDown") wf.page(1);
    else if (key === "PageUp") wf.page(-1);
    else handled = false;
  } else handled = false;
  if (handled) ev.preventDefault();
});

// ---------------------------------------------------------------------------
// Boot

function renderNoFixtures() {
  teardownViews();
  renderList();
  mount(els.main, h("div", { class: "empty" },
    h("p", null, "No fixtures loaded. Run ", h("code", null, "traceon studio path/to/fixtures"), "."),
    h("p", null, "Studio serves only the files and directories named on the command line.")));
  mount(els.detail);
}

async function boot() {
  let data;
  try {
    data = await api.list();
  } catch (err) {
    mount(els.main, h("p", { class: "empty error-text" }, `Cannot list fixtures: ${err.message}`));
    return;
  }
  state.summaries = Array.isArray(data.fixtures) ? data.fixtures : [];
  renderWarnings(data.warnings);
  window.addEventListener("hashchange", route);
  await route();
}

boot();

// Replay playback: a transport bar that re-walks a recorded run on its own
// time axis. The playhead is driven by requestAnimationFrame; with
// prefers-reduced-motion it advances in discrete call-to-call steps instead.

import { h, s } from "./dom.js";
import {
  buildTimeline, callAt, nextCallTime, prevCallTime, uniformCursor, uniformTime,
} from "./logic.js";
import { reducedMotion } from "./waterfall.js";

const SPEEDS = [
  { id: "1", label: "1×", rate: 1, title: "Real time" },
  { id: "4", label: "4×", rate: 4, title: "Four times real time" },
  { id: "16", label: "16×", rate: 16, title: "Sixteen times real time" },
  { id: "uniform", label: "uniform", rate: 0, title: "Every call gets equal time, however long it took" },
];
const UNIFORM_CALLS_PER_SEC = 2.5;
const STEP_INTERVAL_MS = 650;

function icon(kind) {
  const attrs = { viewBox: "0 0 12 12", width: "12", height: "12", "aria-hidden": "true", focusable: "false" };
  if (kind === "play") return s("svg", attrs, s("path", { d: "M3 1.5L10 6L3 10.5Z", class: "ic-fill" }));
  if (kind === "pause") {
    return s("svg", attrs, s("rect", { x: "2.5", y: "1.5", width: "2.5", height: "9", class: "ic-fill" }),
      s("rect", { x: "7", y: "1.5", width: "2.5", height: "9", class: "ic-fill" }));
  }
  if (kind === "back") {
    return s("svg", attrs, s("rect", { x: "1.5", y: "2", width: "1.6", height: "8", class: "ic-fill" }),
      s("path", { d: "M10.5 2L4.5 6L10.5 10Z", class: "ic-fill" }));
  }
  return s("svg", attrs, s("path", { d: "M1.5 2L7.5 6L1.5 10Z", class: "ic-fill" }),
    s("rect", { x: "8.9", y: "2", width: "1.6", height: "8", class: "ic-fill" }));
}

export class Player {
  constructor(model, waterfall, { onCurrent, onSettle }) {
    this.model = model;
    this.wf = waterfall;
    this.onCurrent = onCurrent;
    this.onSettle = onSettle;
    const tl = buildTimeline(model.events.map((e) => e.kind), model.start, model.duration, model.total);
    this.order = tl.order;
    this.times = tl.times;
    this.total = model.total;
    this.t = null;
    this.cursor = -1;
    this.playing = false;
    this.speed = SPEEDS[0];
    this.raf = 0;
    this.timer = 0;
    this.last = 0;
    this.u = 0;
    this.wasPlaying = false;
    this.el = this.build();
    this.render();
  }

  destroy() {
    this.stop();
  }

  build() {
    this.playBtn = h("button", { type: "button", class: "tp-btn tp-play", "aria-label": "Play (Space)", title: "Play (Space)" }, icon("play"));
    this.playBtn.addEventListener("click", () => this.toggle());
    const back = h("button", { type: "button", class: "tp-btn", "aria-label": "Previous call (,)", title: "Previous call (,)" }, icon("back"));
    back.addEventListener("click", () => this.step(-1));
    const fwd = h("button", { type: "button", class: "tp-btn", "aria-label": "Next call (.)", title: "Next call (.)" }, icon("fwd"));
    fwd.addEventListener("click", () => this.step(1));
    this.speedBtns = SPEEDS.map((sp) => {
      const b = h("button", { type: "button", role: "radio", class: "tp-speed-btn", title: sp.title, dataset: { id: sp.id } }, sp.label);
      b.addEventListener("click", () => this.setSpeed(sp.id));
      return b;
    });
    this.tOut = h("span", { class: "tp-t" });
    this.callOut = h("span", { class: "tp-call" });
    const totalOut = h("span", { class: "tp-total" }, `/ ${(this.total / 1000).toFixed(3)} s`);
    return h("div", { class: "transport", role: "toolbar", "aria-label": "Replay playback" },
      h("span", { class: "tp-label" }, "replay"),
      h("div", { class: "tp-group" }, back, this.playBtn, fwd),
      h("div", { class: "tp-speed", role: "radiogroup", "aria-label": "Playback speed" }, this.speedBtns),
      h("div", { class: "tp-readout", "aria-live": "off" },
        h("span", { class: "tp-k" }, "t ="), this.tOut, totalOut,
        h("span", { class: "tp-sep", "aria-hidden": "true" }), this.callOut),
      h("span", { class: "tp-hint" }, h("kbd", null, "Space"), " play ", h("kbd", null, ","), h("kbd", null, "."), " step"));
  }

  render() {
    const t = this.t === null ? 0 : this.t;
    this.tOut.textContent = `${(t / 1000).toFixed(3)} s`;
    const n = this.order.length;
    this.callOut.textContent = this.t === null || this.cursor < 0 ? `call – · 0 of ${n}` : `call #${this.model.callIndex.get(this.order[this.cursor])} · ${this.cursor + 1} of ${n}`;
    this.playBtn.replaceChildren(icon(this.playing ? "pause" : "play"));
    const label = this.playing ? "Pause (Space)" : "Play (Space)";
    this.playBtn.setAttribute("aria-label", label);
    this.playBtn.title = label;
    this.playBtn.classList.toggle("is-on", this.playing);
    for (const b of this.speedBtns) b.setAttribute("aria-checked", String(b.dataset.id === this.speed.id));
  }

  /** Jump to time t (ms). `quiet` skips error flashes (used when scrubbing). */
  seek(t, { quiet = true } = {}) {
    const prev = this.t;
    this.t = Math.max(0, Math.min(this.total, t));
    if (this.speed.rate === 0) this.u = uniformCursor(this.times, this.total, this.t);
    this.wf.setPlayhead(this.t);
    const k = callAt(this.times, this.t);
    if (!quiet && prev !== null && this.t > prev) {
      for (let j = Math.max(0, callAt(this.times, prev) + 1); j <= k; j++) {
        const i = this.order[j];
        if (this.model.events[i].status === "error") this.wf.flash(i);
      }
    }
    if (k !== this.cursor) {
      this.cursor = k;
      const i = k >= 0 ? this.order[k] : -1;
      this.wf.setActive(i);
      if (this.onCurrent) this.onCurrent(i >= 0 ? this.model.events[i] : null);
    }
    this.render();
  }

  toggle() {
    if (this.playing) this.pause();
    else this.play();
  }

  play() {
    if (this.playing) return;
    if (this.t === null || this.t >= this.total - 1e-6) this.seek(0);
    this.playing = true;
    this.render();
    if (reducedMotion()) {
      // No continuous movement: hop from call to call.
      const interval = this.speed.rate > 1 ? STEP_INTERVAL_MS / Math.sqrt(this.speed.rate) : STEP_INTERVAL_MS;
      this.timer = window.setInterval(() => {
        const next = nextCallTime(this.times, this.t);
        if (next === null) { this.seek(this.total); this.pause(); return; }
        this.seek(next, { quiet: false });
      }, interval);
      return;
    }
    this.last = performance.now();
    if (this.speed.rate === 0) this.u = uniformCursor(this.times, this.total, this.t);
    const tick = (now) => {
      if (!this.playing) return;
      const dt = Math.min(100, now - this.last);
      this.last = now;
      let t;
      if (this.speed.rate === 0) {
        this.u += (dt / 1000) * UNIFORM_CALLS_PER_SEC;
        t = uniformTime(this.times, this.total, this.u);
      } else {
        t = this.t + dt * this.speed.rate;
      }
      if (t >= this.total) {
        this.seek(this.total, { quiet: false });
        this.pause();
        return;
      }
      this.seek(t, { quiet: false });
      this.raf = requestAnimationFrame(tick);
    };
    this.raf = requestAnimationFrame(tick);
  }

  stop() {
    this.playing = false;
    if (this.raf) cancelAnimationFrame(this.raf);
    if (this.timer) window.clearInterval(this.timer);
    this.raf = 0;
    this.timer = 0;
  }

  pause() {
    this.stop();
    this.render();
    if (this.onSettle) this.onSettle(this.t);
  }

  step(dir) {
    this.stop();
    const from = this.t === null ? -1 : this.t;
    const next = dir > 0 ? nextCallTime(this.times, from) : prevCallTime(this.times, Math.max(0, from));
    this.seek(next === null ? this.total : next, { quiet: dir < 0 });
    this.render();
    if (this.onSettle) this.onSettle(this.t);
  }

  setSpeed(id) {
    const sp = SPEEDS.find((x) => x.id === id);
    if (!sp) return;
    const resume = this.playing;
    this.stop();
    this.speed = sp;
    if (resume) { this.playing = false; this.play(); } else this.render();
  }

  /** Pointer scrubbing on the time axis. */
  scrub(t, phase) {
    if (phase === "start") {
      this.wasPlaying = this.playing;
      this.stop();
    }
    this.seek(t);
    if (phase === "end") {
      if (this.wasPlaying) { this.playing = false; this.play(); } else this.pause();
    } else {
      this.render();
    }
  }
}

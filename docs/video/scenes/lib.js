/* Shared helpers for the launch film. Every function is pure in t: the same
   time always produces the same frame. */
(function () {
  "use strict";
  const TV = (window.TV = window.TV || {});
  TV.scenes = [];

  TV.C = {
    bg: "#121315", surface: "#18191C", surface2: "#1E1F23", border: "#2B2D31",
    grid: "#222428", fg: "#E3E3E0", muted: "#A3A6AC", subtle: "#8D9097", dim: "#5C5F66",
    accent: "#E89242", tool: "#55B2B8", model: "#86A6DC", value: "#B3BC62",
    span: "#8F939A", ok: "#62B07D", error: "#EC746A",
  };

  // ---------- math ----------
  const clamp = (v, a = 0, b = 1) => Math.min(b, Math.max(a, v));
  const lerp = (a, b, k) => a + (b - a) * k;
  TV.clamp = clamp;
  TV.lerp = lerp;
  /** Linear progress of t through [a, a + d], clamped to 0..1. */
  TV.p = (t, a, d) => (d <= 0 ? (t >= a ? 1 : 0) : clamp((t - a) / d));

  function cubicBezier(p1x, p1y, p2x, p2y) {
    const cx = 3 * p1x, bx = 3 * (p2x - p1x) - cx, ax = 1 - cx - bx;
    const cy = 3 * p1y, by = 3 * (p2y - p1y) - cy, ay = 1 - cy - by;
    const sx = (u) => ((ax * u + bx) * u + cx) * u;
    const sy = (u) => ((ay * u + by) * u + cy) * u;
    const dx = (u) => (3 * ax * u + 2 * bx) * u + cx;
    return (x) => {
      if (x <= 0) return 0;
      if (x >= 1) return 1;
      let u = x;
      for (let i = 0; i < 8; i++) {
        const e = sx(u) - x;
        if (Math.abs(e) < 1e-7) return sy(u);
        const d = dx(u);
        if (Math.abs(d) < 1e-6) break;
        u -= e / d;
      }
      let lo = 0, hi = 1;
      u = x;
      for (let i = 0; i < 40; i++) {
        if (sx(u) < x) lo = u; else hi = u;
        u = (lo + hi) / 2;
      }
      return sy(u);
    };
  }
  TV.cubicBezier = cubicBezier;
  /** Decelerating entrance, cubic-bezier(0.2, 0, 0, 1). */
  TV.out = cubicBezier(0.2, 0, 0, 1);
  /** Symmetric move for things that travel between two points. */
  TV.move = cubicBezier(0.6, 0, 0.3, 1);
  /** Plain ease-in-out for fades. */
  TV.inOut = cubicBezier(0.42, 0, 0.58, 1);
  /** Accelerating exit. */
  TV.inn = cubicBezier(0.4, 0, 1, 1);

  /** Opacity envelope: fades in over [a, a+fi], out over [b-fo, b]. */
  TV.env = (t, a, b, fi = 0.35, fo = 0.35) => {
    if (t < a || t > b) return 0;
    const i = fi > 0 ? TV.inOut(clamp((t - a) / fi)) : 1;
    const o = fo > 0 ? TV.inOut(clamp((b - t) / fo)) : 1;
    return Math.min(i, o);
  };

  /** Speed of the TV.move curve at progress k (0..1), normalized so peak ~ 1. */
  TV.moveSpeed = (k) => {
    const h = 0.01;
    const a = TV.move(clamp(k - h)), b = TV.move(clamp(k + h));
    return clamp(((b - a) / (2 * h)) / 2.2);
  };

  // ---------- DOM ----------
  const SVGNS = "http://www.w3.org/2000/svg";
  TV.h = (tag, cls, parent, text) => {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text != null) el.textContent = text;
    if (parent) parent.appendChild(el);
    return el;
  };
  TV.s = (tag, attrs, parent) => {
    const el = document.createElementNS(SVGNS, tag);
    for (const k in attrs || {}) el.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(el);
    return el;
  };
  TV.css = (el, styles) => {
    for (const k in styles) el.style[k] = styles[k];
    return el;
  };

  /** Set opacity and transform in one call. Hidden elements get visibility:hidden. */
  TV.set = (el, o, x = 0, y = 0, sc = 1, extra = "") => {
    el.style.opacity = String(Math.round(o * 1000) / 1000);
    el.style.visibility = o <= 0.001 ? "hidden" : "visible";
    let tr = `translate(${x.toFixed(2)}px, ${y.toFixed(2)}px)`;
    if (sc !== 1) tr += ` scale(${sc.toFixed(4)})`;
    el.style.transform = tr + (extra ? " " + extra : "");
  };

  /** Reveal fraction f (0..1) of an SVG stroke from its start. */
  TV.draw = (path, f) => {
    if (path._len == null) path._len = path.getTotalLength();
    const L = path._len;
    path.setAttribute("stroke-dasharray", `${L + 1} ${L + 1}`);
    path.setAttribute("stroke-dashoffset", String((1 - clamp(f)) * (L + 1)));
    path.style.visibility = f <= 0 ? "hidden" : "visible";
  };

  /** Point at fraction f of an SVG path's length. */
  TV.at = (path, f) => {
    if (path._len == null) path._len = path.getTotalLength();
    const pt = path.getPointAtLength(clamp(f) * path._len);
    return { x: pt.x, y: pt.y };
  };

  /**
   * A line of mono text made of colored segments that can be revealed
   * character by character. segs: [[text, className], ...]
   */
  TV.rich = (parent, segs, cls = "line") => {
    const el = TV.h("div", cls, parent);
    const spans = segs.map(([text, c]) => {
      const sp = TV.h("span", c || "", el);
      sp._full = text;
      return sp;
    });
    const total = segs.reduce((n, s) => n + s[0].length, 0);
    el._total = total;
    el.reveal = (n) => {
      let left = Math.round(n);
      for (const sp of spans) {
        const k = clamp(left, 0, sp._full.length);
        const txt = sp._full.slice(0, k);
        if (sp.textContent !== txt) sp.textContent = txt;
        left -= sp._full.length;
      }
    };
    el.revealFrac = (f) => el.reveal(clamp(f) * total);
    el.reveal(total);
    return el;
  };

  /** Status marker: ok = filled square, error = diamond, pending = outline. */
  TV.marker = (el, state, color) => {
    if (el._state === state + (color || "")) return;
    el._state = state + (color || "");
    const C = TV.C;
    el.style.transform = "";
    el.style.border = "none";
    el.style.background = "transparent";
    if (state === "ok") {
      el.style.background = C.ok;
    } else if (state === "error") {
      el.style.background = C.error;
      el.style.transform = "rotate(45deg) scale(0.86)";
    } else if (state === "active") {
      el.style.background = color || C.fg;
    } else if (state === "accent") {
      el.style.background = C.accent;
    } else {
      el.style.border = `2px solid ${C.dim}`;
    }
  };

  /** A travelling payload chip: [kind square] text. Optional second text for morphs. */
  TV.chip = (parent, text, color, text2) => {
    const el = TV.h("div", "chip", parent);
    const k = TV.h("span", "k", el);
    k.style.background = color;
    el.style.borderColor = color;
    const sw = TV.h("span", "swap", el);
    const a = TV.h("span", "b1", sw, text);
    let b = null;
    if (text2 != null) {
      // both texts share one grid cell, so the chip is as wide as the longer one
      b = TV.h("span", "", sw, text2);
      sw.style.display = "inline-grid";
      a.style.gridArea = "1 / 1";
      b.style.gridArea = "1 / 1";
    }
    el._a = a;
    el._b = b;
    el.morph = (f) => {
      if (!b) return;
      a.style.opacity = String(1 - f);
      b.style.opacity = String(f);
    };
    el.morph(0);
    // trailing ticks
    el._ticks = [0, 1, 2].map(() => {
      const tk = TV.h("div", "tick", parent);
      tk.style.background = color;
      return tk;
    });
    return el;
  };

  /**
   * Place a chip at fraction k along a path (with TV.move easing already applied
   * by the caller) and its trailing ticks behind it. `raw` is linear progress used
   * for speed. o: base opacity.
   */
  TV.placeChip = (chip, path, k, raw, o, dir = 1) => {
    const pt = TV.at(path, k);
    TV.set(chip, o, pt.x, pt.y, 1, "translate(-50%, -50%)");
    const sp = o > 0 ? TV.moveSpeed(raw) : 0;
    chip._ticks.forEach((tk, i) => {
      const back = (i + 1) * 0.035 * dir;
      const q = TV.at(path, clamp(k - back));
      const to = o * sp * (0.55 - i * 0.16);
      TV.set(tk, Math.max(0, to), q.x - 2, q.y - 2);
    });
  };

  TV.hideChip = (chip) => {
    TV.set(chip, 0);
    chip._ticks.forEach((tk) => TV.set(tk, 0));
  };

  /** Shorthand for a typed reveal driven by time. */
  TV.typeAt = (line, t, a, cps = 70) => {
    line.reveal(Math.max(0, (t - a) * cps));
  };
})();

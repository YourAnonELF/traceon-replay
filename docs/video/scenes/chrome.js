/* Persistent chrome: the brand logo (built in the open, parked lower-left,
   brought back for the end card), scene captions and the chapter index. */
(function () {
  "use strict";
  const TV = window.TV;
  const C = TV.C;

  // Paths copied from brand/logo-dark.svg (mark scaled 1.3333 inside a 141.8 x 32 box).
  const MARK_BASE = "M3 9H21";
  const MARK_BRANCH = "M9.5 9L14.5 16H21";
  const WORDMARK =
    "M3.7 5V18.8A4 4 0 0 0 7.7 22.8H8.7 M0 11.2H8.7 M12.5 24V10 M12.5 16.2A5 5 0 0 1 17.5 11.2H19.1 " +
    "M28.5 11.2A5.8 5.8 0 1 0 28.5 22.8A5.8 5.8 0 1 0 28.5 11.2Z M34.3 10V24 M49.6 12.9A5.8 5.8 0 1 0 49.6 21.1 " +
    "M54.6 17H66.2A5.8 5.8 0 1 0 64.5 21.1 M77.4 11.2A5.8 5.8 0 1 0 77.4 22.8A5.8 5.8 0 1 0 77.4 11.2Z " +
    "M88.6 24V10 M88.6 16.2A5 5 0 0 1 98.6 16.2V24";

  /** Build a logo-dark SVG; returns {svg, base, branch, sq, word}. */
  TV.buildLogo = (parent) => {
    const svg = TV.s("svg", { viewBox: "0 0 141.8 32", width: "141.8", height: "32" }, parent);
    const g = TV.s("g", { transform: "scale(1.3333)" }, svg);
    const base = TV.s("path", { d: MARK_BASE, fill: "none", stroke: C.fg, "stroke-width": "2.5", "stroke-linecap": "square" }, g);
    const branch = TV.s("path", { d: MARK_BRANCH, fill: "none", stroke: C.accent, "stroke-width": "2.5", "stroke-linecap": "square", "stroke-linejoin": "miter" }, g);
    const sq = TV.s("rect", { x: "7.25", y: "6.75", width: "4.5", height: "4.5", fill: C.accent }, g);
    const word = TV.s("path", { d: WORDMARK, transform: "translate(42 -1)", fill: "none", stroke: C.fg, "stroke-width": "2.4", "stroke-linecap": "butt", "stroke-linejoin": "miter" }, svg);
    return { svg, base, branch, sq, word };
  };

  let logo, capSlots, chapEls;

  // Logo placements: x, y = top-left in px, s = scale of the 141.8 x 32 box.
  const OPEN = { s: 4.5, cx: 960, cy: 452 };
  const FOOT = { s: 0.875, x: 240, y: 962 };
  const END = { s: 4, cx: 960, cy: 352 };
  const place = (p) => (p.x != null ? p : { s: p.s, x: p.cx - (141.8 * p.s) / 2, y: p.cy - (32 * p.s) / 2 });

  function init(stage) {
    const layer = TV.h("div", "layer", stage);
    layer.style.zIndex = "50";
    logo = TV.buildLogo(layer);
    logo.svg.id = "brandlogo";

    capSlots = [0, 1].map(() => {
      const el = TV.h("div", "cap", layer);
      const eb = TV.h("div", "eyebrow", el);
      const n = TV.h("span", "n", eb);
      const lab = TV.h("span", "", eb);
      const hl = TV.h("div", "headline", el);
      return { el, n, lab, hl, cue: null };
    });

    const ch = TV.h("div", "chapters", layer);
    chapEls = TV.chapters.map((c) => {
      const sp = TV.h("span", "", ch, c.label);
      const bar = TV.h("i", "bar", sp);
      return { sp, bar, c };
    });
    ch.id = "chapters";
  }

  function logoUpdate(t) {
    const o = place(OPEN), f = place(FOOT), e = place(END);
    let pos;
    const fly = TV.T.open[1] - 0.7;
    if (t < fly) pos = o;
    else if (t < fly + 0.8) {
      const k = TV.move(TV.p(t, fly, 0.8));
      pos = { x: TV.lerp(o.x, f.x, k), y: TV.lerp(o.y, f.y, k), s: TV.lerp(o.s, f.s, k) };
    } else if (t < TV.T.end[0] + 0.1) pos = f;
    // (end card: the parked logo returns to the center)
    else {
      const k = TV.move(TV.p(t, TV.T.end[0] + 0.1, 0.9));
      pos = { x: TV.lerp(f.x, e.x, k), y: TV.lerp(f.y, e.y, k), s: TV.lerp(f.s, e.s, k) };
    }
    TV.set(logo.svg, 1, pos.x, pos.y, pos.s);

    // Build sequence: base track draws, the square lands on the split point,
    // the branch leaves it, the wordmark resolves.
    TV.draw(logo.base, TV.out(TV.p(t, 0.25, 0.8)));
    const sk = TV.out(TV.p(t, 0.85, 0.45));
    logo.sq.setAttribute("transform", `translate(0 ${((1 - sk) * -5).toFixed(3)})`);
    logo.sq.style.opacity = String(TV.p(t, 0.85, 0.15));
    TV.draw(logo.branch, TV.inOut(TV.p(t, 1.25, 0.55)));
    TV.draw(logo.word, TV.inOut(TV.p(t, 1.55, 1.0)));
  }

  function capUpdate(t) {
    const active = TV.captions.filter((c) => t >= c.a && t <= c.b);
    capSlots.forEach((slot, i) => {
      const cue = active[i];
      if (!cue) { TV.set(slot.el, 0); return; }
      if (slot.cue !== cue) {
        slot.cue = cue;
        slot.n.textContent = cue.n;
        slot.lab.textContent = cue.eyebrow;
        slot.hl.textContent = cue.text;
      }
      const fi = TV.out(TV.p(t, cue.a, 0.6));
      const fo = TV.inOut(TV.p(cue.b - t, 0, 0.3));
      TV.set(slot.el, Math.min(TV.p(t, cue.a, 0.4), fo), 0, (1 - fi) * 10);
    });
  }

  function chapUpdate(t) {
    const [a] = TV.T.sys, b = TV.T.ci[1];
    const o = Math.min(TV.p(t, a + 0.3, 0.5), TV.inOut(TV.p(b - t, 0, 0.4)));
    TV.set(document.getElementById("chapters"), o);
    chapEls.forEach(({ sp, bar, c }) => {
      const on = t >= c.a && t < c.b;
      sp.className = on ? "on" : "";
      const k = on ? TV.out(TV.p(t, c.a, 0.6)) : 0;
      bar.style.transform = `scaleX(${k.toFixed(3)})`;
      bar.style.opacity = on ? "1" : "0";
    });
  }

  TV.scenes.push({ name: "chrome", init, update(t) { logoUpdate(t); capUpdate(t); chapUpdate(t); } });
})();

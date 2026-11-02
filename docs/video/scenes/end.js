/* 7. End card. The parked logo returns to the center (chrome.js). */
(function () {
  "use strict";
  const TV = window.TV;
  let root, tag, inst, url, facts;

  function init(stage) {
    root = TV.h("div", "scene", stage);
    const center = { left: "0", width: "1920px", textAlign: "center" };
    tag = TV.css(TV.h("div", "abs", root, "Deterministic replay for AI agents"), Object.assign({ fontSize: "40px", lineHeight: "52px", color: "var(--muted)" }, center));

    inst = TV.h("div", "abs", root);
    TV.css(inst, Object.assign({ display: "flex", justifyContent: "center" }, center));
    const box = TV.h("div", "mono", inst);
    TV.css(box, {
      height: "68px", lineHeight: "66px", padding: "0 28px", fontSize: "26px", whiteSpace: "pre",
      background: "var(--surface)", border: "1px solid var(--border)", borderRadius: "6px",
    });
    TV.h("span", "subtle", box, "$ ");
    TV.h("span", "c-fg", box, "pip install ");
    TV.h("span", "c-value", box, '"git+https://github.com/YourAnonELF/traceon-replay"');

    url = TV.css(TV.h("div", "abs mono c-fg", root, "github.com/YourAnonELF/traceon-replay"), Object.assign({ fontSize: "26px", lineHeight: "34px" }, center));
    facts = TV.css(
      TV.h("div", "abs", root, "Apache-2.0  ·  Python 3.10+  ·  works with any agent framework  ·  imports Langfuse traces"),
      Object.assign({ fontSize: "26px", lineHeight: "34px", color: "var(--muted)" }, center)
    );
  }

  function update(t) {
    const [a] = TV.T.end;
    const d = t - a;
    const on = d > -0.5;
    root.style.display = on ? "block" : "none";
    if (!on) return;
    const rise = (s, el, y) => {
      const k = TV.out(TV.p(d, s, 0.7));
      TV.set(el, TV.p(d, s, 0.4), 0, y + (1 - k) * 12);
    };
    rise(0.75, tag, 452);
    rise(1.05, inst, 556);
    rise(1.3, url, 670);
    rise(1.5, facts, 726);
  }

  TV.scenes.push({ name: "end", init, update });
})();

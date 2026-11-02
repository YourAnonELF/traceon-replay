/* 1. Open: the mark builds itself (chrome.js), the tagline settles under it. */
(function () {
  "use strict";
  const TV = window.TV;
  let tag;

  function init(stage) {
    const root = TV.h("div", "scene", stage);
    tag = TV.h("div", "abs", root, "Deterministic replay for AI agents");
    TV.css(tag, {
      left: "0", top: "0", width: "1920px", textAlign: "center",
      fontSize: "40px", lineHeight: "52px", color: "var(--muted)", letterSpacing: "-0.005em",
    });
  }

  function update(t) {
    const end = TV.T.open[1];
    const k = TV.out(TV.p(t, 2.25, 0.8));
    const o = Math.min(TV.p(t, 2.25, 0.5), TV.inOut(TV.p(end - 0.45 - t, 0, 0.3)));
    TV.set(tag, o, 0, 568 + (1 - k) * 12);
  }

  TV.scenes.push({ name: "open", init, update });
})();

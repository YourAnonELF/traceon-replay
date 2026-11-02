/* 6. pytest: the fixture becomes a regression test. Terminal lines are the
   real output of `pytest -v` with the traceon plugin (compacted: the long
   traceback lines are omitted). */
(function () {
  "use strict";
  const TV = window.TV;
  let root, code, term, cmd, caret, lines, exitTag;

  function init(stage) {
    root = TV.h("div", "scene", stage);

    code = TV.h("div", "card", root);
    TV.css(code, { left: "240px", top: "232px", width: "1440px", height: "198px" });
    const hd = TV.h("div", "hd", code);
    TV.h("span", "c-fg", hd, "test_order_status.py");
    const body = TV.h("div", "", code);
    TV.css(body, { padding: "14px 24px", fontSize: "24px", lineHeight: "38px" });
    TV.rich(body, [["@pytest.mark.traceon(", "muted"], ['"fixtures/eta_parse_failure.json"', "c-value"], [")", "muted"]]);
    TV.rich(body, [["def ", "muted"], ["test_order_id_refactor", "c-fg"], ["():", "muted"]]);
    TV.rich(body, [["    run_agent_refactored(", "c-fg"], ['"A-1042"', "c-value"], [")", "c-fg"]]);

    term = TV.h("div", "card", root);
    TV.css(term, { left: "240px", top: "462px", width: "1440px", height: "452px", background: "#141518" });
    const th = TV.h("div", "hd", term);
    TV.h("span", "muted", th, "terminal");
    exitTag = TV.h("span", "c-error", th, "exit 1");
    const tb = TV.h("div", "", term);
    TV.css(tb, { padding: "18px 24px", fontSize: "22px", lineHeight: "34px" });
    const cl = TV.h("div", "line", tb);
    TV.h("span", "subtle", cl, "$ ");
    cmd = TV.rich(cl, [["pytest -v", "c-fg"]], "");
    cmd.style.display = "inline";
    caret = TV.h("span", "caret", cl);
    const L = (segs) => TV.rich(tb, segs);
    lines = [
      L([["test_order_status.py::test_reproduces_eta_bug ", "c-fg"], ["PASSED", "c-ok b"], ["             [ 50%]", "subtle"]]),
      L([["test_order_status.py::test_order_id_refactor ", "c-fg"], ["FAILED", "c-error b"], ["              [100%]", "subtle"]]),
      L([["----------------- ", "subtle"], ["traceon: first divergence", "muted"], [" -----------------", "subtle"]]),
      L([["x first divergence", "c-fg b"], ["  argument_mismatch - call #0 - tool:lookup_order", "muted"]]),
      L([["    path      ", "subtle"], ["$.order_id", "c-fg"]]),
      L([["    expected  ", "subtle"], ['"A-1042"', "c-fg"]]),
      L([["    actual    ", "subtle"], ['"a-1042"', "c-accent"]]),
      L([["traceon: 2 replayed, 1 diverged", "muted"]]),
      L([["=================== ", "subtle"], ["1 failed", "c-error"], [", ", "muted"], ["1 passed", "c-ok"], [" in 0.04s", "muted"], [" ===================", "subtle"]]),
    ];
  }

  const SHOW = [1.95, 2.35, 2.85, 2.97, 3.09, 3.21, 3.33, 3.65, 3.85];

  function update(t) {
    const [a, b] = TV.T.ci;
    const d = t - a, D = b - a;
    const on = d > -0.5 && d < D + 0.5;
    root.style.display = on ? "block" : "none";
    if (!on) return;
    root.style.opacity = String(Math.min(TV.p(d, 0, 0.4), TV.inOut(TV.p(D - d, 0, 0.4))));

    const ck = TV.out(TV.p(d, 0.2, 0.6));
    TV.set(code, TV.p(d, 0.2, 0.35), 0, (1 - ck) * 12);
    const tk = TV.out(TV.p(d, 0.55, 0.6));
    TV.set(term, TV.p(d, 0.55, 0.35), 0, (1 - tk) * 12);

    TV.typeAt(cmd, d, 1.15, 22);
    // caret: solid while typing, then blinks at a fixed rate until output starts
    const typing = d >= 1.15 && d < 1.15 + 9 / 22;
    const blinkOn = Math.floor((d - 1.15) / 0.5) % 2 === 0;
    caret.style.visibility = d < 1.9 && (d < 1.15 || typing || blinkOn) ? "visible" : "hidden";
    lines.forEach((l, i) => { l.style.opacity = d >= SHOW[i] ? "1" : "0"; });
    exitTag.style.opacity = d >= SHOW[8] ? "1" : "0";
  }

  TV.scenes.push({ name: "ci", init, update });
})();

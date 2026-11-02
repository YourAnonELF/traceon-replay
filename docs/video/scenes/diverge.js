/* 5. Divergence. Beat 1: the fixed parser is replayed against the same
   recording; the tracks stay together and only the run status changes
   (error -> ok). Beat 2: a refactor lowercases the order id; the replayed track
   leaves the recorded one at call #0 and the real CLI report appears. */
(function () {
  "use strict";
  const TV = window.TV;
  const C = TV.C;

  const X0 = 420, X1 = 1600, TICKS = [640, 1000, 1360];
  const Y_REC = 474, Y_REP = 530, FORK = { dx: 50, dy: 70, run: 130 };
  const FIX_DUR = 5.8; // beat 1 length
  let root, diff, diffFix, diffRef, dHeadR, svg, rec, repA, repB, repFull, ticksRec, ticksRep, tickLabels, labRec, labRep;
  let endRec, endRep, fixStatus, forkSq, pulse, failNote, cli, cliLines;

  function codeLine(parent, sign, segs, kind) {
    const row = TV.h("div", "line", parent);
    TV.css(row, { height: "44px", lineHeight: "44px", padding: "0 24px", fontSize: "24px" });
    row.style.background = kind === "-" ? "rgba(236,116,106,0.08)" : "rgba(98,176,125,0.08)";
    TV.h("span", kind === "-" ? "c-error" : "c-ok", row, sign + "  ");
    segs.forEach(([t, c]) => TV.h("span", c || "c-fg", row, t));
    return row;
  }

  function init(stage) {
    root = TV.h("div", "scene", stage);
    // diff card
    diff = TV.h("div", "card", root);
    TV.css(diff, { left: "240px", top: "232px", width: "1440px", height: "160px", overflow: "hidden" });
    const hd = TV.h("div", "hd", diff);
    const hl = TV.h("span", "", hd);
    TV.h("span", "c-fg", hl, "agent.py");
    const fnName = TV.h("span", "muted", hl, "");
    dHeadR = TV.h("span", "", hd, "");
    diff._fn = fnName;
    const body = TV.h("div", "", diff);
    TV.css(body, { position: "relative", paddingTop: "8px" });
    diffFix = TV.h("div", "", body);
    diffFix._l = [
      codeLine(diffFix, "-", [["    return dt.datetime.strptime(raw, "], ['"%Y-%m-%dT%H:%M:%S"', "c-value"], [")"]], "-"),
      codeLine(diffFix, "+", [["    return dt.datetime.fromisoformat(raw).astimezone(dt.timezone.utc)"]], "+"),
    ];
    diffRef = TV.h("div", "abs", body);
    TV.css(diffRef, { left: "0", top: "8px", width: "100%" });
    diffRef._l = [
      codeLine(diffRef, "-", [["    return _run(order_id, parse_eta)"]], "-"),
      codeLine(diffRef, "+", [["    return _run("], ["order_id.lower()", "c-accent"], [", parse_eta)"]], "+"),
    ];

    // tracks
    const lab = (txt, y) => {
      const el = TV.h("div", "abs mono", root, txt);
      TV.css(el, { fontSize: "22px", lineHeight: "28px", color: C.muted });
      el._y = y;
      return el;
    };
    labRec = lab("recorded", Y_REC);
    labRep = lab("replayed", Y_REP);
    svg = TV.s("svg", { class: "full" }, root);
    rec = TV.s("path", { d: `M${X0} ${Y_REC}H${X1}`, stroke: C.fg, "stroke-width": "2.5", fill: "none" }, svg);
    repFull = TV.s("path", { d: `M${X0} ${Y_REP}H${X1}`, stroke: C.fg, "stroke-width": "2.5", fill: "none" }, svg);
    repA = TV.s("path", { d: `M${X0} ${Y_REP}H${TICKS[0]}`, stroke: C.fg, "stroke-width": "2.5", fill: "none" }, svg);
    repB = TV.s("path", {
      d: `M${TICKS[0]} ${Y_REP}L${TICKS[0] + FORK.dx} ${Y_REP + FORK.dy}H${TICKS[0] + FORK.dx + FORK.run}`,
      stroke: C.accent, "stroke-width": "2.5", fill: "none", "stroke-linejoin": "miter", "stroke-linecap": "square",
    }, svg);
    const kc = [C.tool, C.tool, C.model];
    const sq = (x, y, c) => TV.s("rect", { x: x - 7, y: y - 7, width: 14, height: 14, fill: c }, svg);
    ticksRec = TICKS.map((x, i) => sq(x, Y_REC, kc[i]));
    ticksRep = TICKS.map((x, i) => sq(x, Y_REP, kc[i]));
    const TL = ["#0  lookup_order", "#1  track_shipment", "#2  draft_reply"];
    tickLabels = TICKS.map((x, i) => {
      const el = TV.h("div", "abs mono", root, TL[i]);
      TV.css(el, { width: "320px", textAlign: "center", fontSize: "22px", lineHeight: "28px", color: C.muted });
      el._x = x;
      return el;
    });
    forkSq = TV.s("rect", { x: -8, y: -8, width: 16, height: 16, fill: C.accent }, svg);
    pulse = TV.s("rect", { x: -8, y: -8, width: 16, height: 16, fill: "none", stroke: C.accent, "stroke-width": "1.5" }, svg);

    const endMark = (y, state, txt, cls) => {
      const el = TV.h("div", "abs mono", root);
      TV.css(el, { fontSize: "22px", lineHeight: "28px", display: "flex", alignItems: "center", gap: "12px" });
      const m = TV.h("span", "marker", el);
      TV.marker(m, state);
      TV.h("span", cls, el, txt);
      el._y = y;
      return el;
    };
    endRec = endMark(Y_REC, "error", "error", "c-error");
    endRep = endMark(Y_REP, "ok", "ok", "c-ok");

    fixStatus = TV.h("div", "abs mono", root);
    TV.css(fixStatus, { fontSize: "22px", lineHeight: "28px", whiteSpace: "pre" });
    TV.h("span", "c-fg", fixStatus, "calls 3/3 served from recording");
    TV.h("span", "subtle", fixStatus, "   ·   ");
    TV.h("span", "muted", fixStatus, "status ");
    TV.h("span", "c-error", fixStatus, "error");
    TV.h("span", "muted", fixStatus, " → ");
    TV.h("span", "c-ok", fixStatus, "ok");
    TV.h("span", "subtle", fixStatus, "   ·   the fix changes no call");

    failNote = TV.h("div", "abs mono", root);
    TV.css(failNote, { fontSize: "22px", lineHeight: "28px", whiteSpace: "pre" });
    TV.h("span", "c-accent", failNote, "UnmatchedCallError");
    TV.h("span", "muted", failNote, "  the real lookup_order is never called");

    // CLI card with the real `traceon replay` output
    cli = TV.h("div", "card", root);
    TV.css(cli, { left: "240px", top: "640px", width: "1440px", height: "256px" });
    const chd = TV.h("div", "hd", cli);
    const cl = TV.h("span", "", chd);
    TV.h("span", "subtle", cl, "$ ");
    TV.h("span", "c-fg", cl, "traceon replay fixtures/eta_parse_failure.json --entry agent:run_agent_refactored");
    TV.h("span", "subtle", chd, "exit 1");
    const cb = TV.h("div", "", cli);
    TV.css(cb, { padding: "20px 24px", fontSize: "24px", lineHeight: "40px" });
    cliLines = [
      TV.rich(cb, [["✗ ", "c-error"], ["first divergence", "c-fg b"], ["  argument_mismatch · call #0 · tool:lookup_order", "muted"]]),
      TV.rich(cb, [["    path      ", "subtle"], ["$.order_id", "c-fg"]]),
      TV.rich(cb, [["    expected  ", "subtle"], ['"A-1042"', "c-fg"]]),
      TV.rich(cb, [["    actual    ", "subtle"], ['"a-1042"', "c-accent"]]),
    ];
  }

  function update(t) {
    const [a, b] = TV.T.div;
    const d = t - a, D = b - a;
    const on = d > -0.5 && d < D + 0.5;
    root.style.display = on ? "block" : "none";
    if (!on) return;
    root.style.opacity = String(Math.min(TV.p(d, 0, 0.4), TV.inOut(TV.p(D - d, 0, 0.4))));

    const beat2 = d >= FIX_DUR;
    const d2 = d - FIX_DUR - 0.4;

    // diff card
    const dk = TV.out(TV.p(d, 0.2, 0.6));
    TV.set(diff, TV.p(d, 0.2, 0.35), 0, (1 - dk) * 12);
    const swap = TV.inOut(TV.p(d, FIX_DUR - 0.1, 0.5));
    diffFix.style.opacity = String(1 - swap);
    diffRef.style.opacity = String(swap);
    const fn = swap < 0.5 ? "  ·  parse_eta" : "  ·  run_agent_refactored";
    if (diff._fn.textContent !== fn) diff._fn.textContent = fn;
    const tagTxt = swap < 0.5 ? "fix" : "refactor";
    if (dHeadR.textContent !== tagTxt) dHeadR.textContent = tagTxt;
    dHeadR.className = swap < 0.5 ? "c-ok" : "c-accent";
    diffFix._l.forEach((l, i) => TV.set(l, TV.p(d, 0.55 + i * 0.3, 0.3), (1 - TV.out(TV.p(d, 0.55 + i * 0.3, 0.5))) * -10, 0));
    diffRef._l.forEach((l, i) => TV.set(l, TV.p(d, FIX_DUR + 0.2 + i * 0.3, 0.3), (1 - TV.out(TV.p(d, FIX_DUR + 0.2 + i * 0.3, 0.5))) * -10, 0));

    // labels
    const labO = TV.p(d, 1.1, 0.4);
    TV.set(labRec, labO, 240, Y_REC - 14);
    TV.set(labRep, labO, 240, Y_REP - 14);

    // track drawing
    let xr, tF = null;
    const beat1Out = 1 - TV.inOut(TV.p(d, FIX_DUR - 0.3, 0.4));
    if (!beat2) {
      xr = TV.lerp(X0, X1, TV.inOut(TV.p(d, 1.4, 2.2)));
      TV.draw(rec, (xr - X0) / (X1 - X0));
      TV.draw(repFull, (xr - X0) / (X1 - X0));
      TV.draw(repA, 0);
      TV.draw(repB, 0);
      svg.style.opacity = String(beat1Out);
    } else {
      const k = TV.inOut(TV.p(d2, 0.5, 2.4));
      xr = TV.lerp(X0, X1, k);
      TV.draw(rec, (xr - X0) / (X1 - X0));
      TV.draw(repFull, 0);
      TV.draw(repA, Math.min(1, (xr - X0) / (TICKS[0] - X0)));
      // time when the recorded track reaches the fork (solve numerically, deterministic)
      for (let s = 0; s <= 2.4; s += 0.005) {
        if (TV.lerp(X0, X1, TV.inOut(TV.p(s, 0, 2.4))) >= TICKS[0]) { tF = 0.5 + s; break; }
      }
      TV.draw(repB, TV.out(TV.p(d2, tF, 0.6)));
      svg.style.opacity = String(TV.p(d2, 0, 0.3));
    }
    const tickVis = (x) => (xr >= x ? 1 : 0);
    ticksRec.forEach((r, i) => r.setAttribute("opacity", String(tickVis(TICKS[i]))));
    ticksRep.forEach((r, i) => r.setAttribute("opacity", String(!beat2 ? tickVis(TICKS[i]) : 0)));
    const tlO = beat2 ? TV.p(d2, 0.2, 0.3) : TV.p(d, 1.2, 0.4) * beat1Out;
    tickLabels.forEach((el) => TV.set(el, tlO, el._x - 160, Y_REC - 50));

    // beat 1 end state
    const endO = !beat2 ? TV.p(d, 3.6, 0.35) * beat1Out : 0;
    TV.set(endRec, endO, X1 + 20, Y_REC - 14);
    TV.set(endRep, endO, X1 + 20, Y_REP - 14);
    TV.set(fixStatus, !beat2 ? TV.p(d, 3.9, 0.4) * beat1Out : 0, X0, Y_REP + 42);

    // fork marker + pulse
    const fx = TICKS[0], fy = Y_REP;
    if (beat2 && tF != null && d2 >= tF) {
      const k = TV.out(TV.p(d2, tF, 0.3));
      forkSq.setAttribute("transform", `translate(${fx} ${fy}) scale(${(0.4 + 0.6 * k).toFixed(3)})`);
      forkSq.setAttribute("opacity", String(TV.p(d2, tF, 0.12)));
      const pk = TV.p(d2, tF + 0.1, 0.9);
      const ps = 1 + 2.6 * TV.out(pk);
      pulse.setAttribute("transform", `translate(${fx} ${fy}) scale(${ps.toFixed(3)})`);
      pulse.setAttribute("stroke-width", String((1.5 / ps).toFixed(3)));
      pulse.setAttribute("opacity", String(pk > 0 && pk < 1 ? (0.9 * (1 - pk)).toFixed(3) : 0));
    } else {
      forkSq.setAttribute("opacity", "0");
      pulse.setAttribute("opacity", "0");
    }
    const noteO = beat2 && tF != null ? TV.p(d2, tF + 0.55, 0.35) : 0;
    TV.set(failNote, noteO, fx + FORK.dx + FORK.run + 24, fy + FORK.dy - 14);

    // CLI card
    const cT = tF != null ? tF + 0.8 : 99;
    const ck = beat2 ? TV.out(TV.p(d2, cT, 0.6)) : 0;
    TV.set(cli, beat2 ? TV.p(d2, cT, 0.35) : 0, 0, (1 - ck) * 14);
    cliLines.forEach((l, i) => {
      const s = cT + 0.35 + i * 0.16;
      l.style.opacity = beat2 && d2 >= s ? "1" : "0";
    });
  }

  TV.scenes.push({ name: "diverge", init, update });
})();

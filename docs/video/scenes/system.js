/* 2-4. One continuous diagram: the agent and the services it calls.
   Phase P (incident): the run happens live and fails while parsing the ETA.
   Phase R (record):   the same run passes through the traceon recorder; every
                       call is copied into the fixture.
   Phase Y (replay):   services are cut off; responses come from the fixture
                       tape and the same ValueError is reproduced offline.
   All strings come from examples/failed_tool_call and its real fixture. */
(function () {
  "use strict";
  const TV = window.TV;
  const C = TV.C;

  // ---------- geometry ----------
  const AG = { x: 240, y: 262, w: 460, h: 288 };
  const PORT = { x: AG.x + AG.w, y: 406 };
  const SV_X = 1340, SV_W = 340, SV_H = 80;
  const BAND_X = 1020;
  const SERVICES = [
    { name: "orders-db", sub: "tool · lookup_order", color: C.tool, cy: 292 },
    { name: "carrier-api", sub: "tool · track_shipment", color: C.tool, cy: 406 },
    { name: "LLM", sub: "model · draft_reply", color: C.model, cy: 520 },
  ];
  const ROWS = [
    { name: "lookup_order", kind: "tool", color: C.tool },
    { name: "track_shipment", kind: "tool", color: C.tool },
    { name: "draft_reply", kind: "model", color: C.model },
    { name: "parse_eta", kind: "code", color: C.fg },
  ];
  const PANEL = { x: 240, y: 648, w: 1440 };
  const TAPE = { x0: 300, x1: 1620, y: 800, ticks: [560, 960, 1360] };

  const REQ = [
    'lookup_order("A-1042")',
    'track_shipment(…, "PN-88341207")',
    'draft_reply  api_key="sk-proj-4f9a2c…"',
  ];
  const REQ_REDACTED = 'draft_reply  api_key="[REDACTED:sensitive_key]"';
  const RESP = [
    '{"carrier": "parcelnet", …}',
    '"IN_TRANSIT;ETA=…T14:00:00+01:00;…"',
    '"Order A-1042 is on its way …"',
  ];

  const TAPE_RESP = ['{"carrier": "parcelnet", …}', '"IN_TRANSIT;ETA=…+01:00…"', '"Order A-1042 is on its way…"'];

  // ---------- schedule (seconds, local to the scene start) ----------
  let P0, R0, Y0, END;
  const SCH = {
    P: { s: [1.5, 3.15, 4.8], leg: 0.65, dwell: 0.15, parse: 6.6 },
    R: { s: [1.3, 3.6, 5.9], leg: 0.8, dwell: 0.15, parse: 8.6 },
    Y: { h: [3.4, 4.9, 6.4], parse: 7.6 },
  };
  const PLAYHEAD = [
    [2.6, 3.4, TAPE.x0, TAPE.ticks[0]],
    [3.65, 4.9, TAPE.ticks[0], TAPE.ticks[1]],
    [5.15, 6.4, TAPE.ticks[1], TAPE.ticks[2]],
    [6.65, 7.35, TAPE.ticks[2], TAPE.x1],
  ];

  let root, svg, conns, connA, connB, agent, rows, agStatus, services, band, bandTicks, bandLab, bandLab2;
  let panel, pHead, rHead, yHead, pBody, rBody, yBody, rLines, rCounters, yCounters, tapeSvg, tapeLine, tapeTicks, tapeLabels, tapePayloads, playhead;
  let chipsLayer, chipsP, chipsR, copies, chipsY, bounce, tags, nbe, flights, yResult;

  // ---------- helpers ----------
  function bez(cy) {
    return `M${PORT.x} ${PORT.y} C${BAND_X} ${PORT.y} ${BAND_X} ${cy} ${SV_X} ${cy}`;
  }
  function bezHalves(cy) {
    // de Casteljau split at u = 0.5 (x = BAND_X by symmetry)
    const p0 = [PORT.x, PORT.y], p1 = [BAND_X, PORT.y], p2 = [BAND_X, cy], p3 = [SV_X, cy];
    const m = (a, b) => [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
    const a1 = m(p0, p1), b1 = m(p1, p2), c1 = m(p2, p3), a2 = m(a1, b1), b2 = m(b1, c1), mid = m(a2, b2);
    const f = (q) => `${q[0]} ${q[1]}`;
    return [`M${f(p0)} C${f(a1)} ${f(a2)} ${f(mid)}`, `M${f(mid)} C${f(b2)} ${f(c1)} ${f(p3)}`, mid];
  }

  function init(stage) {
    root = TV.h("div", "scene", stage);
    END = TV.T.sys[1] - TV.T.sys[0];
    P0 = 0;
    R0 = TV.T.record - TV.T.problem;
    Y0 = TV.T.replay - TV.T.problem;

    // connections
    svg = TV.s("svg", { class: "full" }, root);
    svg.style.zIndex = "1";
    conns = []; connA = []; connB = [];
    SERVICES.forEach((s) => {
      const [a, b, mid] = bezHalves(s.cy);
      connA.push(TV.s("path", { d: a, fill: "none", stroke: "#3A3D43", "stroke-width": "1.5" }, svg));
      const pb = TV.s("path", { d: b, fill: "none", stroke: "#3A3D43", "stroke-width": "1.5" }, svg);
      connB.push(pb);
      const full = TV.s("path", { d: bez(s.cy), fill: "none", stroke: "none" }, svg);
      full._mid = mid;
      conns.push(full);
    });

    // recorder band
    const crossYs = conns.map((c) => c._mid[1]);
    band = TV.h("div", "abs", root);
    TV.css(band, {
      left: BAND_X - 13 + "px", top: crossYs[0] - 50 + "px", width: "26px",
      height: crossYs[2] - crossYs[0] + 100 + "px", background: C.surface,
      border: `1px solid ${C.border}`, borderRadius: "3px", zIndex: "2", transformOrigin: "50% 50%",
    });
    bandTicks = crossYs.map((y) => {
      const tk = TV.h("div", "abs", root);
      TV.css(tk, { width: "26px", height: "2px", background: C.muted, zIndex: "2" });
      tk._y = y;
      return tk;
    });
    const labStyle = { width: "320px", textAlign: "center", fontFamily: "var(--mono)", fontSize: "22px", lineHeight: "28px", color: C.muted, zIndex: "2" };
    bandLab = TV.css(TV.h("div", "abs", root, "traceon recorder"), labStyle);
    bandLab2 = TV.css(TV.h("div", "abs", root, "traceon replay"), labStyle);

    // bottom panel (traceback -> fixture log -> tape)
    panel = TV.h("div", "card", root);
    TV.css(panel, { left: PANEL.x + "px", top: PANEL.y + "px", width: PANEL.w + "px", zIndex: "3", overflow: "hidden" });
    const hdWrap = TV.h("div", "", panel);
    TV.css(hdWrap, { position: "relative", height: "56px" });
    const mkHead = (left, right) => {
      const hd = TV.h("div", "hd", hdWrap);
      TV.css(hd, { position: "absolute", left: "0", top: "0", right: "0" });
      const l = TV.h("span", "", hd);
      left.forEach(([txt, c]) => TV.h("span", c || "", l, txt));
      const r = TV.h("span", "", hd, right);
      return { hd, r };
    };
    pHead = mkHead([["traceback", "c-fg"], ["   production · order-status-agent", "muted"]], "");
    rHead = mkHead([["fixtures/eta_parse_failure.json", "c-fg"], ["   traceon.fixture/v1", "muted"]], "");
    yHead = mkHead([["fixtures/eta_parse_failure.json", "c-fg"], ["   replay", "muted"]], "");
    rCounters = rHead.r;
    yCounters = yHead.r;

    const body = TV.h("div", "", panel);
    TV.css(body, { position: "relative" });
    pBody = TV.h("div", "abs", body);
    TV.css(pBody, { left: "24px", top: "22px", fontSize: "24px", lineHeight: "40px" });
    const l1 = TV.h("div", "line", pBody);
    TV.h("span", "muted", l1, "parse_eta(");
    TV.h("span", "c-fg", l1, '"IN_TRANSIT;ETA=2026-11-05T14:00:00');
    const hl = TV.h("span", "c-error", l1, "+01:00");
    TV.css(hl, { textDecoration: "underline", textDecorationThickness: "2px", textUnderlineOffset: "7px" });
    TV.h("span", "c-fg", l1, ';LOC=Leipzig"');
    TV.h("span", "muted", l1, ")");
    pBody._l1 = l1;
    pBody._l2 = TV.rich(pBody, [["ValueError: ", "c-error b"], ["unconverted data remains: +01:00", "c-error"]]);

    rBody = TV.h("div", "abs", body);
    TV.css(rBody, { left: "0", top: "14px", width: PANEL.w + "px", fontSize: "22px", lineHeight: "40px" });
    const k = (s) => [s, "subtle"];
    const LOG = [
      { seq: "2", segs: [k('{"kind": '), ['"tool"', "c-tool"], k(', "name": '), ['"lookup_order"', "c-fg"], k(', "call_key": '), ['"sha256:408ea4a3…"', "muted"], k("}")], st: "ok" },
      { seq: "3", segs: [k('{"kind": '), ['"tool"', "c-tool"], k(', "name": '), ['"track_shipment"', "c-fg"], k(', "call_key": '), ['"sha256:2badf736…"', "muted"], k("}")], st: "ok" },
      { seq: "5", segs: [k('{"kind": '), ['"model"', "c-model"], k(', "name": '), ['"draft_reply"', "c-fg"], k(', "request": {"state": '), ['"hash_only"', "c-value"], k("}}")], st: "ok" },
      { seq: "0", segs: [k('{"kind": '), ['"span"', "muted"], k(', "name": '), ['"order-status-agent"', "c-fg"], k(', "status": '), ['"error"', "c-error"], k("}")], st: "error" },
    ];
    rLines = LOG.map((L, i) => {
      const row = TV.h("div", "abs", rBody);
      TV.css(row, { left: "0", top: i * 40 + "px", width: PANEL.w + "px", height: "40px" });
      const g = TV.h("div", "abs mono", row, L.seq);
      TV.css(g, { left: "24px", width: "32px", textAlign: "right", color: C.subtle, top: "0" });
      const wrap = TV.h("div", "abs", row);
      TV.css(wrap, { left: "80px", top: "0" });
      const line = TV.rich(wrap, L.segs);
      const mk = TV.h("span", "marker abs", row);
      TV.css(mk, { left: PANEL.w - 24 - 12 - 2 + "px", top: "14px" });
      TV.marker(mk, L.st);
      return { row, g, line, mk };
    });

    yBody = TV.h("div", "abs", body);
    TV.css(yBody, { left: "0", top: "0", width: PANEL.w + "px", height: "170px" });
    // tape is drawn in stage coordinates on its own svg above the panel
    tapeSvg = TV.s("svg", { class: "full" }, root);
    tapeSvg.style.zIndex = "4";
    tapeLine = TV.s("path", { d: `M${TAPE.x0} ${TAPE.y}H${TAPE.x1}`, stroke: C.fg, "stroke-width": "2", fill: "none" }, tapeSvg);
    tapeTicks = TAPE.ticks.map((x, i) => {
      const r = TV.s("rect", { x: x - 7, y: TAPE.y - 7, width: 14, height: 14, fill: i === 2 ? C.model : C.tool }, tapeSvg);
      return r;
    });
    const TL = ["#0  tool:lookup_order", "#1  tool:track_shipment", "#2  model:draft_reply"];
    tapeLabels = TAPE.ticks.map((x, i) => {
      const el = TV.h("div", "abs mono", root, TL[i]);
      TV.css(el, { width: "380px", textAlign: "center", fontSize: "22px", lineHeight: "28px", color: C.fg, zIndex: "4" });
      el._x = x;
      return el;
    });
    tapePayloads = TAPE.ticks.map((x, i) => {
      const el = TV.h("div", "abs mono", root, TAPE_RESP[i]);
      TV.css(el, { width: "390px", textAlign: "center", fontSize: "22px", lineHeight: "28px", color: C.subtle, zIndex: "4", whiteSpace: "nowrap" });
      el._x = x;
      return el;
    });
    playhead = TV.h("div", "abs", root);
    TV.css(playhead, { width: "2px", height: "136px", background: C.accent, zIndex: "6" });
    const ph = TV.h("div", "abs", playhead);
    TV.css(ph, { left: "-5px", top: "-6px", width: "12px", height: "12px", background: C.accent });

    yResult = TV.h("div", "abs mono", root);
    TV.css(yResult, { left: PANEL.x + "px", top: "0", fontSize: "22px", lineHeight: "32px", zIndex: "4", whiteSpace: "pre" });
    const okm = TV.h("span", "marker", yResult);
    TV.marker(okm, "ok");
    okm.style.verticalAlign = "-1px";
    TV.h("span", "c-fg", yResult, "  replay matches the recording");
    TV.h("span", "subtle", yResult, "      raised  ");
    TV.h("span", "c-error", yResult, "ValueError: unconverted data remains: +01:00");

    // chips (under the cards so they slide out from beneath them)
    chipsLayer = TV.h("div", "layer", root);
    chipsLayer.style.zIndex = "5";
    const color = (i) => (i === 2 ? C.model : C.tool);
    chipsP = [0, 1, 2].map((i) => ({ req: TV.chip(chipsLayer, REQ[i], color(i)), resp: TV.chip(chipsLayer, RESP[i], color(i)) }));
    chipsR = [0, 1, 2].map((i) => ({ req: TV.chip(chipsLayer, REQ[i], color(i)), resp: TV.chip(chipsLayer, RESP[i], color(i)) }));
    copies = [0, 1, 2].map((i) => TV.chip(chipsLayer, REQ[i], color(i), i === 2 ? REQ_REDACTED : undefined));
    chipsY = [0, 1, 2].map((i) => ({ req: TV.chip(chipsLayer, REQ[i], color(i)), resp: TV.chip(chipsLayer, RESP[i], color(i)) }));
    bounce = TV.chip(chipsLayer, "connect carrier-api:443", C.muted);
    flights = TAPE.ticks.map((x) =>
      TV.s("path", { d: `M${x} ${TAPE.y} C${x} ${TAPE.y - 150} ${AG.x + AG.w / 2} ${AG.y + AG.h + 150} ${AG.x + AG.w / 2} ${AG.y + AG.h - 20}`, fill: "none", stroke: "none" }, svg)
    );

    // agent card
    agent = TV.h("div", "card", root);
    TV.css(agent, { left: AG.x + "px", top: AG.y + "px", width: AG.w + "px", height: AG.h + "px", zIndex: "7" });
    const ahd = TV.h("div", "hd", agent);
    const at = TV.h("span", "", ahd);
    TV.h("span", "c-fg", at, "run_agent(");
    TV.h("span", "c-value", at, '"A-1042"');
    TV.h("span", "c-fg", at, ")");
    agStatus = TV.h("span", "", ahd, "");
    const rw = TV.h("div", "", agent);
    TV.css(rw, { padding: "8px 0" });
    rows = ROWS.map((r) => {
      const row = TV.h("div", "", rw);
      TV.css(row, { height: "52px", display: "flex", alignItems: "center", gap: "18px", padding: "0 24px", fontFamily: "var(--mono)", fontSize: "24px" });
      const mk = TV.h("span", "marker", row);
      const nm = TV.h("span", "", row, r.name);
      nm.style.color = r.color;
      const sp = TV.h("span", "", row);
      sp.style.flex = "1";
      TV.h("span", "subtle", row, r.kind).style.fontSize = "22px";
      return { row, mk, nm, r };
    });

    // service cards
    services = SERVICES.map((s) => {
      const el = TV.h("div", "card", root);
      TV.css(el, { left: SV_X + "px", top: s.cy - SV_H / 2 + "px", width: SV_W + "px", height: SV_H + "px", zIndex: "7", padding: "10px 22px", overflow: "hidden" });
      const bar = TV.h("div", "abs", el);
      TV.css(bar, { left: "0", top: "0", width: "3px", height: "100%", background: s.color });
      TV.css(TV.h("div", "mono c-fg", el, s.name), { fontSize: "24px", lineHeight: "30px" });
      TV.css(TV.h("div", "mono subtle", el, s.sub), { fontSize: "22px", lineHeight: "28px" });
      return { el, s };
    });

    // blocked tags + error label (replay)
    tags = conns.map((c) => {
      const tg = TV.h("div", "tag", root);
      tg.style.zIndex = "8";
      const m = TV.h("span", "marker", tg);
      TV.marker(m, "error");
      m.style.width = m.style.height = "10px";
      TV.h("span", "", tg, "blocked");
      tg._pt = TV.at(c, 0.86);
      return tg;
    });
    nbe = TV.h("div", "abs mono c-error", root, "NetworkBlockedError");
    TV.css(nbe, { width: "300px", textAlign: "center", fontSize: "22px", lineHeight: "28px", zIndex: "8" });
  }

  // ---------- per-frame ----------
  function phaseOf(lt) {
    if (lt < R0) return "P";
    if (lt < Y0) return "R";
    return "Y";
  }

  /** Request/response timing for call i in the current phase. Times local to phase. */
  function callTimes(ph, i) {
    const S = SCH[ph];
    if (ph === "Y") {
      const h = S.h[i];
      return { req0: h - 0.6, req1: h, resp0: h + 0.05, resp1: h + 0.85 };
    }
    const s = S.s[i];
    return { req0: s, req1: s + S.leg, resp0: s + S.leg + S.dwell, resp1: s + 2 * S.leg + S.dwell };
  }

  function rowsUpdate(ph, pt) {
    const parseT = SCH[ph].parse;
    rows.forEach((r, i) => {
      let st = "pending", flash = 0;
      if (i < 3) {
        const c = callTimes(ph, i);
        if (pt >= c.resp1) st = "ok";
        else if (pt >= c.req0) st = "active";
      } else {
        if (pt >= parseT) st = "error";
        else if (pt >= parseT - 0.3) st = "active";
        if (pt >= parseT) flash = 1 - TV.p(pt, parseT, 0.9);
      }
      TV.marker(r.mk, st, r.r.color);
      r.row.style.background = flash > 0 ? `rgba(236,116,106,${(0.16 * flash).toFixed(3)})` : "transparent";
      r.nm.style.opacity = st === "pending" ? "0.55" : "1";
    });
    let status = "", cls = "muted";
    if (ph === "P") status = pt >= 1.2 ? "running" : "";
    if (ph === "R") status = "recording";
    if (ph === "Y") status = "replaying";
    if (pt >= parseT) { status = "✗ error"; cls = "c-error"; }
    if (agStatus.textContent !== status) agStatus.textContent = status;
    agStatus.className = cls;
  }

  function serviceUpdate(ph, pt, lt) {
    const dimK = ph === "Y" ? TV.inOut(TV.p(lt - Y0, 0.3, 0.6)) : 0;
    services.forEach((sv, i) => {
      let act = 0;
      if (ph !== "Y") {
        const c = callTimes(ph, i);
        act = pt >= c.req1 - 0.05 && pt <= c.resp0 + 0.35 ? 1 : 0;
      }
      sv.el.style.borderColor = act ? sv.s.color : C.border;
      const k = TV.out(TV.p(lt, 0.35 + i * 0.12, 0.7));
      TV.set(sv.el, TV.p(lt, 0.35 + i * 0.12, 0.4) * (1 - 0.62 * dimK), (1 - k) * 16, 0);
    });
  }

  function chipLeg(chip, path, t, a, b, forward, kFrom = 0, kTo = 1) {
    if (t < a || t > b) { TV.hideChip(chip); return; }
    const raw = TV.p(t, a, b - a);
    const e = TV.move(raw);
    const k = forward ? TV.lerp(kFrom, kTo, e) : TV.lerp(kTo, kFrom, e);
    TV.placeChip(chip, path, k, raw, 1, forward ? 1 : -1);
  }

  function update(t) {
    const lt = t - TV.T.problem;
    const on = lt > -0.5 && lt < END + 0.5;
    root.style.display = on ? "block" : "none";
    if (!on) return;
    const sceneO = Math.min(TV.p(lt, 0, 0.4), TV.inOut(TV.p(END - lt, 0, 0.4)));
    root.style.opacity = String(sceneO);

    const ph = phaseOf(lt);
    const pt = lt - (ph === "P" ? P0 : ph === "R" ? R0 : Y0);
    const rt = lt - R0, yt = lt - Y0;

    // entrance of the diagram
    const agK = TV.out(TV.p(lt, 0.1, 0.7));
    TV.set(agent, TV.p(lt, 0.1, 0.4), 0, (1 - agK) * 14);
    conns.forEach((c, i) => {
      const f = TV.inOut(TV.p(lt, 0.6 + i * 0.1, 0.9));
      TV.draw(connA[i], Math.min(1, f * 2));
      TV.draw(connB[i], Math.max(0, f * 2 - 1));
      const cut = TV.inOut(TV.p(yt, 0.3, 0.5));
      connB[i].setAttribute("stroke", cut > 0 ? "#2A2C30" : "#3A3D43");
      connB[i].setAttribute("stroke-dasharray", cut > 0.5 ? "4 6" : connB[i].getAttribute("stroke-dasharray"));
      if (cut > 0.5) connB[i].setAttribute("stroke-dashoffset", "0");
    });

    rowsUpdate(ph, pt);
    serviceUpdate(ph, pt, lt);

    // ---- band ----
    const bandIn = TV.out(TV.p(rt, 0.2, 0.7));
    TV.set(band, bandIn > 0 ? 1 : 0, 0, 0, 1, `scaleY(${bandIn.toFixed(4)})`);
    bandTicks.forEach((tk, i) => TV.set(tk, TV.p(rt, 0.6 + i * 0.08, 0.3), BAND_X - 13, tk._y - 1));
    const labY = conns[0]._mid[1] - 50 - 44;
    const swapK = TV.inOut(TV.p(yt, 0.0, 0.5));
    TV.set(bandLab, TV.p(rt, 0.6, 0.4) * (1 - swapK), BAND_X - 160, labY + swapK * -6);
    TV.set(bandLab2, swapK, BAND_X - 160, labY + (1 - swapK) * 6);

    // ---- panel ----
    const pIn = TV.out(TV.p(lt, SCH.P.parse + 0.05, 0.5));
    const hP = 184, hR = 248, hY = 220;
    const toR = TV.move(TV.p(rt, 0.2, 0.6)), toY = TV.move(TV.p(yt, 0.2, 0.6));
    const ph_h = ph === "P" ? hP : ph === "R" ? TV.lerp(hP, hR, toR) : TV.lerp(hR, hY, toY);
    panel.style.height = ph_h.toFixed(1) + "px";
    const panelO = ph === "P" ? TV.p(lt, SCH.P.parse + 0.05, 0.35) : 1;
    TV.set(panel, panelO, 0, ph === "P" ? (1 - pIn) * 12 : 0);
    const pO = ph === "P" ? 1 : 1 - TV.p(rt, 0.0, 0.3);
    const rO = ph === "R" ? TV.p(rt, 0.45, 0.35) : ph === "Y" ? 1 - TV.p(yt, 0, 0.3) : 0;
    const yO = ph === "Y" ? TV.p(yt, 0.45, 0.35) : 0;
    TV.set(pHead.hd, pO); TV.set(pBody, pO);
    TV.set(rHead.hd, rO);
    TV.set(yHead.hd, yO);
    // traceback lines
    pBody._l1.style.opacity = String(TV.p(lt, SCH.P.parse + 0.2, 0.3));
    TV.typeAt(pBody._l2, lt, SCH.P.parse + 0.55, 60);
    // fixture log
    TV.set(rBody, rO);
    rLines.forEach((L, i) => {
      let typeA, stT;
      if (i < 3) {
        const c = callTimes("R", i);
        typeA = c.req0 + (i === 2 ? 1.9 : 1.0); stT = Math.max(c.resp1, typeA + 0.8);
      } else {
        typeA = SCH.R.parse + 0.2; stT = SCH.R.parse + 0.9;
      }
      const tpt = ph === "R" ? rt : ph === "Y" ? 99 : -99;
      TV.typeAt(L.line, tpt, typeA, 95);
      L.row.style.opacity = tpt >= typeA - 0.02 ? "1" : "0";
      L.mk.style.opacity = tpt >= stT ? "1" : "0";
      L.g.style.color = tpt >= typeA && tpt < typeA + 0.5 ? C.fg : C.subtle;
    });
    if (ph === "R") {
      let ev = 0, calls = 0;
      SCH.R.s.forEach((s, i) => { if (rt >= s) { calls = i + 1; ev = [3, 4, 6][i]; } });
      const txt = `events ${ev} · calls ${calls}` + (rt >= SCH.R.parse + 0.9 ? " · status error" : "");
      if (rCounters.textContent !== txt) rCounters.textContent = txt;
    }
    // tape
    const tapeDraw = ph === "Y" ? TV.inOut(TV.p(yt, 0.5, 0.8)) : 0;
    TV.draw(tapeLine, tapeDraw);
    tapeSvg.style.opacity = ph === "Y" ? "1" : "0";
    tapeTicks.forEach((r, i) => {
      const k = ph === "Y" ? TV.out(TV.p(yt, 0.8 + i * 0.15, 0.4)) : 0;
      r.setAttribute("opacity", String(k));
    });
    tapeLabels.forEach((el, i) => TV.set(el, ph === "Y" ? TV.p(yt, 0.9 + i * 0.15, 0.4) : 0, el._x - 190, TAPE.y - 54));
    tapePayloads.forEach((el, i) => {
      const lit = ph === "Y" && yt >= SCH.Y.h[i] ? 1 : 0;
      el.style.color = lit ? C.muted : C.subtle;
      TV.set(el, ph === "Y" ? TV.p(yt, 1.0 + i * 0.15, 0.4) : 0, el._x - 195, TAPE.y + 24);
    });
    // playhead
    let px = TAPE.x0;
    PLAYHEAD.forEach(([a, b, x0, x1]) => { if (yt >= a) px = TV.lerp(x0, x1, TV.move(TV.p(yt, a, b - a))); });
    TV.set(playhead, ph === "Y" ? TV.p(yt, 2.2, 0.4) * (1 - TV.p(yt, 7.6, 0.4)) : 0, px - 1, TAPE.y - 68);
    if (ph === "Y") {
      let served = 0;
      SCH.Y.h.forEach((h, i) => { if (yt >= h + 0.85) served = i + 1; });
      const txt = `calls served ${served}/3 · network blocked · real executions 0`;
      if (yCounters.textContent !== txt) yCounters.textContent = txt;
      yCounters.className = "c-fg";
    }
    TV.set(yResult, ph === "Y" ? TV.p(yt, SCH.Y.parse + 0.3, 0.35) : 0, PANEL.x, PANEL.y + hY + 18);

    // ---- chips ----
    ["P", "R"].forEach((P) => {
      const set = P === "P" ? chipsP : chipsR;
      const lp = P === "P" ? lt : rt;
      const active = ph === P;
      set.forEach((c, i) => {
        if (!active) { TV.hideChip(c.req); TV.hideChip(c.resp); return; }
        const ct = callTimes(P, i);
        chipLeg(c.req, conns[i], lp, ct.req0, ct.req1, true);
        chipLeg(c.resp, conns[i], lp, ct.resp0, ct.resp1, false);
      });
    });
    // copies peel off at the band and settle into the fixture log
    copies.forEach((cp, i) => {
      cp._ticks.forEach((tk) => TV.set(tk, 0));
      if (ph !== "R") { TV.set(cp, 0); return; }
      const ct = callTimes("R", i);
      const a = (ct.req0 + ct.req1) / 2;
      const d = 0.62;
      const hold = i === 2 ? 0.9 : 0;
      if (rt < a || rt > a + d + hold + 0.25) { TV.set(cp, 0); return; }
      const from = conns[i]._mid;
      const w = cp.offsetWidth;
      const to = [PANEL.x + 80 + w / 2, PANEL.y + 56 + 14 + i * 40 + 20];
      const raw = TV.p(rt, a, d);
      const x = TV.lerp(from[0], to[0], TV.move(raw));
      const y = TV.lerp(from[1], to[1], TV.out(raw));
      const o = rt <= a + d + hold ? 1 : 1 - TV.p(rt, a + d + hold, 0.25);
      cp.morph(TV.inOut(TV.p(raw, 0.15, 0.35)));
      if (i === 2) cp.classList.toggle("dashed", raw > 0.3);
      TV.set(cp, o, x, y, 1, "translate(-50%, -50%)");
    });
    // replay: requests stop at the band, responses come from the tape
    chipsY.forEach((c, i) => {
      if (ph !== "Y") { TV.hideChip(c.req); TV.hideChip(c.resp); return; }
      const ct = callTimes("Y", i);
      if (yt >= ct.req0 && yt <= ct.req1 + 0.2) {
        const raw = TV.p(yt, ct.req0, ct.req1 - ct.req0);
        const k = 0.5 * TV.move(raw);
        const o = 1 - TV.p(yt, ct.req1, 0.2);
        TV.placeChip(c.req, conns[i], k, raw, o, 1);
      } else TV.hideChip(c.req);
      chipLeg(c.resp, flights[i], yt, ct.resp0, ct.resp1, true);
    });
    // network attempt bounces off the cut
    const b0 = 1.0, bHit = 1.55, b1 = 2.2;
    if (ph === "Y" && yt >= b0 && yt <= b1) {
      const w = bounce.offsetWidth;
      const stopX = tags[1]._pt.x - 57 - 8 - w / 2;
      const kStop = (stopX - PORT.x) / (SV_X - PORT.x);
      let k, raw;
      if (yt < bHit) { raw = TV.p(yt, b0, bHit - b0); k = kStop * TV.inn(raw); }
      else { raw = TV.p(yt, bHit, b1 - bHit); k = kStop * (1 - TV.out(raw)); }
      const o = Math.min(TV.p(yt, b0, 0.1), 1 - TV.p(yt, b1 - 0.25, 0.25));
      TV.placeChip(bounce, conns[1], k, 0.5, o, yt < bHit ? 1 : -1);
    } else TV.hideChip(bounce);

    // blocked tags
    tags.forEach((tg, i) => {
      const k = ph === "Y" ? TV.out(TV.p(yt, 0.55 + i * 0.08, 0.45)) : 0;
      const w = tg.offsetWidth;
      TV.set(tg, ph === "Y" ? TV.p(yt, 0.55 + i * 0.08, 0.3) : 0, tg._pt.x - w / 2, tg._pt.y - 18, 0.94 + 0.06 * k);
      const hit = i === 1 && ph === "Y" && yt >= bHit && yt < bHit + 0.7;
      tg.style.borderColor = hit ? C.error : C.border;
      tg.style.color = hit ? C.error : C.muted;
    });
    const nbO = ph === "Y" ? Math.min(TV.p(yt, bHit, 0.2), 1 - TV.p(yt, 3.0, 0.4)) : 0;
    TV.set(nbe, nbO, tags[1]._pt.x - 150, tags[1]._pt.y + 26);
  }

  TV.scenes.push({ name: "system", init, update });
})();

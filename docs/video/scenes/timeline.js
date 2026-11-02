/* Scene timing (seconds). Edit here to retime the film; scenes read these. */
(function () {
  "use strict";
  const TV = window.TV;
  const OPEN = 4.6, PROBLEM = 9.5, RECORD = 10, REPLAY = 10, DIV = 13, CI = 7, END = 6;
  const t1 = OPEN, t2 = t1 + PROBLEM, t3 = t2 + RECORD, t4 = t3 + REPLAY, t5 = t4 + DIV, t6 = t5 + CI;
  TV.T = {
    open: [0, t1],
    sys: [t1, t4], // one continuous diagram: problem, record, replay
    problem: t1,
    record: t2,
    replay: t3,
    div: [t4, t5],
    ci: [t5, t6],
    end: [t6, t6 + END],
    total: t6 + END,
  };

  TV.captions = [
    { a: t1 + 0.3, b: t2 - 0.1, n: "01", eyebrow: "incident", text: "An agent failed in production. You can’t reproduce it." },
    { a: t2 + 0.1, b: t3 - 0.1, n: "02", eyebrow: "record", text: "Record every tool call, model call and nondeterministic value." },
    { a: t3 + 0.1, b: t4 - 0.1, n: "03", eyebrow: "replay offline", text: "Replay it offline. Same inputs, every time. Nothing leaves the machine." },
    { a: t4 + 0.2, b: t5 - 0.1, n: "04", eyebrow: "first divergence", text: "Find the first point where behavior changed." },
    { a: t5 + 0.2, b: t6 - 0.2, n: "05", eyebrow: "pytest", text: "Turn incidents into regression tests." },
  ];

  TV.chapters = [
    { label: "incident", a: t1, b: t2 },
    { label: "record", a: t2, b: t3 },
    { label: "replay", a: t3, b: t4 },
    { label: "divergence", a: t4, b: t5 },
    { label: "pytest", a: t5, b: t6 },
  ];
})();

// Boot watchdog (classic script, runs even if the ES modules fail to load).
// app.js sets data-booted on <html> once every module imported. If that has not
// happened after a few seconds -- a module 404, a syntax error, a stale server --
// replace the "Loading." placeholder with an explanation instead of spinning forever.
(function () {
  "use strict";
  var LIMIT_MS = 6000;
  function explain() {
    if (document.documentElement.getAttribute("data-booted") === "1") return;
    var main = document.getElementById("main");
    if (!main) return;
    while (main.firstChild) main.removeChild(main.firstChild);
    var box = document.createElement("div");
    box.className = "page";
    var title = document.createElement("p");
    title.className = "empty";
    title.textContent = "Studio could not start.";
    var hint = document.createElement("p");
    hint.className = "muted";
    hint.textContent =
      "A script failed to load. Reload the page; if it persists, restart " +
      "'traceon studio' (the running server may be older than the installed package) " +
      "and check the browser console for the failing file.";
    box.appendChild(title);
    box.appendChild(hint);
    main.appendChild(box);
  }
  window.addEventListener("error", function (ev) {
    var t = ev && ev.target;
    if (t && t.tagName === "SCRIPT") window.setTimeout(explain, 0);
  }, true);
  window.setTimeout(explain, LIMIT_MS);
})();

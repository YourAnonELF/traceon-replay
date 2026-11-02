// Applies the persisted theme before first paint. Loaded as a classic script in
// <head> so the page never flashes the wrong theme. Storage may be unavailable
// (private windows, blocked site data); the system preference is the fallback.
(function () {
  "use strict";
  var theme = null;
  try {
    theme = window.localStorage.getItem("traceon.theme");
  } catch (err) {
    theme = null;
  }
  if (theme === "light" || theme === "dark") {
    document.documentElement.setAttribute("data-theme", theme);
  }
})();

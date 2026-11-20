// Platewright inside Bambuddy's frame: follow its theme, and ask it to open its own pages.
// Messages are exchanged with Bambuddy's origin only.
(function () {
  "use strict";

  var root = document.documentElement;
  var BAMBUDDY = root.dataset.bambuddyOrigin;
  var framed = window.parent !== window;
  var canNavigate = false;
  var THEME_KEY = "platewright.theme";

  var MODES = ["light", "dark"];
  var STYLES = ["classic", "glow", "vibrant"];
  var BACKGROUNDS = ["neutral", "warm", "cool", "oled", "slate", "forest"];
  var ACCENTS = ["green", "teal", "blue", "orange", "purple", "red"];

  function swapClass(prefix, allowed, value, fallback) {
    allowed.forEach(function (v) { root.classList.remove(prefix + v); });
    root.classList.add(prefix + (allowed.indexOf(value) >= 0 ? value : fallback));
  }

  function applyTheme(t) {
    var mode = MODES.indexOf(t.mode) >= 0 ? t.mode : "light";
    root.classList.toggle("dark", mode === "dark");
    root.style.colorScheme = mode;
    swapClass("style-", STYLES, t.style, "classic");
    swapClass("bg-", BACKGROUNDS, t.background, "neutral");
    swapClass("accent-", ACCENTS, t.accent, "green");
  }

  function remembered() {
    try { return JSON.parse(sessionStorage.getItem(THEME_KEY) || "null"); } catch (err) { return null; }
  }

  // Each page load inside the frame gets a fresh theme message; until it arrives, use the
  // last one (no flash between pages), or the device's setting outside Bambuddy.
  var media = window.matchMedia("(prefers-color-scheme: dark)");
  var gotTheme = false;
  var last = framed ? remembered() : null;
  if (last) {
    applyTheme(last);
    canNavigate = last.canNavigate === true;
  } else {
    applyTheme({ mode: media.matches ? "dark" : "light" });
  }
  if (media.addEventListener) {
    media.addEventListener("change", function () {
      if (!gotTheme && !last) applyTheme({ mode: media.matches ? "dark" : "light" });
    });
  }

  window.addEventListener("message", function (e) {
    if (e.origin !== BAMBUDDY || e.source !== window.parent) return;
    var d = e.data;
    if (!d || d.type !== "bambuddy:theme") return;
    gotTheme = true;
    canNavigate = d.canNavigate === true;
    applyTheme(d);
    try {
      sessionStorage.setItem(THEME_KEY, JSON.stringify({
        mode: d.mode, style: d.style, background: d.background, accent: d.accent, canNavigate: canNavigate
      }));
    } catch (err) { /* storage blocked: the next page just starts from the device theme */ }
  });

  if (framed) {
    // Bambuddy sends the theme on load too; asking covers a missed first one.
    try { window.parent.postMessage({ type: "bambuddy:theme-request" }, BAMBUDDY); } catch (err) { /* not Bambuddy */ }
  }

  // "Open in Bambuddy": switch Bambuddy's own page when it allows it, else a new tab.
  document.addEventListener("click", function (e) {
    var link = e.target.closest && e.target.closest("a[data-bambuddy-path]");
    if (!link || !framed || !canNavigate) return;
    e.preventDefault();
    window.parent.postMessage({ type: "bambuddy:navigate", path: link.dataset.bambuddyPath }, BAMBUDDY);
  });

  // "Back" on an error page: return to the form, keeping what was typed.
  document.addEventListener("click", function (e) {
    var back = e.target.closest && e.target.closest("[data-back]");
    if (!back || window.history.length < 2) return;
    e.preventDefault();
    window.history.back();
  });

  // Color swatches: set from data-hex (inline styles are refused by the CSP).
  function paint(scope) {
    (scope || document).querySelectorAll("[data-hex]").forEach(function (el) {
      var hex = el.dataset.hex || "";
      if (/^#[0-9a-fA-F]{6}$/.test(hex)) el.style.backgroundColor = hex;
    });
  }
  document.addEventListener("DOMContentLoaded", function () { paint(); });
  document.addEventListener("htmx:afterSwap", function (e) { paint(e.target); });
})();

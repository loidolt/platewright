// Platewright inside Bambuddy's frame: follow its theme, and ask it to open its
// own pages. Messages are exchanged with Bambuddy's origin only.
(function () {
  "use strict";

  var root = document.documentElement;
  var BAMBUDDY = root.dataset.bambuddyOrigin;
  var framed = window.parent !== window;
  var canNavigate = false;

  var MODES = ["light", "dark"];
  var STYLES = ["classic", "glow", "vibrant"];
  var BACKGROUNDS = ["neutral", "warm", "cool", "oled", "slate", "forest"];
  var ACCENTS = ["green", "teal", "blue", "orange", "purple", "red"];

  function show(id, text, cls) {
    var el = document.getElementById(id);
    if (!el) return;
    el.textContent = text;
    el.className = cls || "";
  }

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

  // Outside Bambuddy (or before its first message) follow the device.
  var media = window.matchMedia("(prefers-color-scheme: dark)");
  var gotTheme = false;
  function deviceTheme() { if (!gotTheme) applyTheme({ mode: media.matches ? "dark" : "light" }); }
  deviceTheme();
  if (media.addEventListener) media.addEventListener("change", deviceTheme);

  window.addEventListener("message", function (e) {
    if (e.origin !== BAMBUDDY || e.source !== window.parent) return;
    var d = e.data;
    if (!d || d.type !== "bambuddy:theme") return;
    gotTheme = true;
    canNavigate = d.canNavigate === true;
    applyTheme(d);
    var now = new Date().toLocaleTimeString();
    show("diag-theme", [d.mode, d.style, d.background, d.accent].join(" / ") + " at " + now, "good");
    show("diag-nav", canNavigate ? "in place (canNavigate)" : "new tab (Bambuddy didn't offer navigate)",
         canNavigate ? "good" : "muted");
  });

  if (framed) {
    // Bambuddy sends the theme on load too; asking covers a missed first one.
    try { window.parent.postMessage({ type: "bambuddy:theme-request" }, BAMBUDDY); } catch (err) { /* not Bambuddy */ }
  }

  document.addEventListener("click", function (e) {
    var link = e.target.closest && e.target.closest("a[data-bambuddy-path]");
    if (!link || !framed || !canNavigate) return; // plain link: opens a new tab
    e.preventDefault();
    window.parent.postMessage({ type: "bambuddy:navigate", path: link.dataset.bambuddyPath }, BAMBUDDY);
  });

  document.addEventListener("DOMContentLoaded", function () {
    var ref = "";
    try { ref = document.referrer ? new URL(document.referrer).origin : ""; } catch (err) { ref = ""; }
    show("diag-framed", framed ? "yes" + (ref ? " (referrer " + ref + ")" : "") : "no, opened directly",
         framed ? "good" : "muted");
    if (!gotTheme) show("diag-nav", framed ? "waiting for theme message" : "new tab (not framed)", "muted");
    show("diag-ua", navigator.userAgent, "small");
    if (document.hasStorageAccess) {
      document.hasStorageAccess().then(function (ok) {
        show("diag-storage", ok ? "has storage access" : "no storage access (cookies may be partitioned)", ok ? "good" : "bad");
      }, function () { show("diag-storage", "hasStorageAccess() failed", "bad"); });
    } else {
      show("diag-storage", "Storage Access API not available", "muted");
    }
  });
})();

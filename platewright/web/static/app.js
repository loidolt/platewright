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

  // Toasts: the one-line notice after a save, gone after a few seconds (as Bambuddy's), and
  // dropped from the address so a reload doesn't show it again.
  function dismiss(toast) {
    if (!toast || toast.classList.contains("hide")) return;
    toast.classList.add("hide");
    setTimeout(function () { toast.remove(); }, 300);
  }
  function toasts() {
    document.querySelectorAll("[data-toast]").forEach(function (t) {
      // errors and warnings stay longer, as Bambuddy's do
      var slow = t.classList.contains("bad") || t.classList.contains("warn");
      setTimeout(function () { dismiss(t); }, slow ? 8000 : 4000);
    });
    try {
      var url = new URL(window.location.href);
      if (url.searchParams.has("notice")) {
        url.searchParams.delete("notice");
        url.searchParams.delete("tone");
        window.history.replaceState(window.history.state, "", url.pathname + url.search + url.hash);
      }
    } catch (err) { /* old browser: the notice just stays in the address */ }
  }
  document.addEventListener("click", function (e) {
    var x = e.target.closest && e.target.closest("[data-dismiss]");
    if (x) dismiss(x.closest("[data-toast]"));
  });

  // Busy forms: a second tap must not post twice (two taps on "Queue" would queue twice).
  // The submit button shows a spinner until the next page arrives. (No confirm(): Bambuddy's
  // frame sandbox has no allow-modals, so dialogs are blocked; confirmations are inline folds.)
  document.addEventListener("submit", function (e) {
    var form = e.target;
    var submitter = e.submitter;
    if (form.hasAttribute("hx-post") || form.hasAttribute("hx-get")) return; // htmx marks its own
    if (form.dataset.busy) { e.preventDefault(); return; }
    form.dataset.busy = "1";
    form.classList.add("is-submitting");
    var btn = submitter || form.querySelector("button[type=submit], button:not([type])");
    if (btn) setTimeout(function () { btn.classList.add("is-busy"); }, 0);
  }, true);
  window.addEventListener("pageshow", function (e) {
    if (!e.persisted) return; // back from the next page: forms work again
    document.querySelectorAll("form[data-busy]").forEach(function (f) {
      delete f.dataset.busy;
      f.classList.remove("is-submitting");
    });
    document.querySelectorAll(".is-busy").forEach(function (b) { b.classList.remove("is-busy"); });
  });

  // Controls that submit on change: a chosen file, an override box. data-submit names the
  // button to submit with (its formaction / formmethod), or "" for the form's own.
  document.addEventListener("change", function (e) {
    var el = e.target;
    if (!el.matches || !el.matches("[data-submit]") || !el.form) return;
    if (el.type === "file" && !el.files.length) return;
    var btn = el.dataset.submit ? document.querySelector(el.dataset.submit) : null;
    if (el.form.requestSubmit) el.form.requestSubmit(btn || undefined); else el.form.submit();
  });

  // Drop zone: highlight while a file is dragged over it (the file input underneath takes the drop).
  ["dragenter", "dragover"].forEach(function (type) {
    document.addEventListener(type, function (e) {
      var z = e.target.closest && e.target.closest(".dropzone");
      if (z) z.classList.add("over");
    });
  });
  ["dragleave", "drop"].forEach(function (type) {
    document.addEventListener(type, function (e) {
      var z = e.target.closest && e.target.closest(".dropzone");
      if (z) z.classList.remove("over");
    });
  });

  // Settings › Palette: the swatch follows the hex as it's typed.
  document.addEventListener("input", function (e) {
    if (!e.target.matches || !e.target.matches("input[name=color_hex]")) return;
    var swatch = e.target.closest("tr") && e.target.closest("tr").querySelector(".swatch");
    var hex = e.target.value.trim();
    if (swatch && /^#?[0-9a-fA-F]{6}$/.test(hex)) swatch.style.backgroundColor = hex.charAt(0) === "#" ? hex : "#" + hex;
  });

  document.addEventListener("DOMContentLoaded", function () { paint(); toasts(); });
  document.addEventListener("htmx:afterSwap", function (e) { paint(e.target); });
})();

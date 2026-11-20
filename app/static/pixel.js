/* Meta Pixel for the public order form, loaded only after the customer's consent.
   No inline script (strict CSP): the pixel id and the event come from data attributes on
   #cc-pixel. Choice remembered in localStorage ("cc-ads": "1" accepted, "0" declined).
   On submit, the consent and the _fbp/_fbc browser ids go with the order, so the server
   can send a Purchase (Conversions API) once the order is confirmed on WhatsApp. */
(function () {
  "use strict";
  var KEY = "cc-ads";
  var cfg = document.getElementById("cc-pixel");
  if (!cfg || !/^\d{5,20}$/.test(cfg.getAttribute("data-pixel") || "")) return;
  var pixelId = cfg.getAttribute("data-pixel");
  var pageEvent = cfg.getAttribute("data-event") || "PageView";
  var banner = document.getElementById("ads-consent");

  function getChoice() {
    try { return localStorage.getItem(KEY); } catch (e) { return null; }
  }
  function setChoice(v) {
    try { localStorage.setItem(KEY, v); } catch (e) { /* private mode */ }
  }
  function cookie(name) {
    var m = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
    return m ? decodeURIComponent(m[1]) : "";
  }

  function loadPixel() {
    if (window.fbq) return;
    var f = function () {
      f.callMethod ? f.callMethod.apply(f, arguments) : f.queue.push(arguments);
    };
    window.fbq = f;
    if (!window._fbq) window._fbq = f;
    f.push = f; f.loaded = true; f.version = "2.0"; f.queue = [];
    var s = document.createElement("script");
    s.async = true;
    s.src = "https://connect.facebook.net/en_US/fbevents.js";
    document.head.appendChild(s);
    window.fbq("init", pixelId);
    window.fbq("track", "PageView");
    if (pageEvent !== "PageView") window.fbq("track", pageEvent);
  }

  function hideBanner() { if (banner) banner.hidden = true; }

  var choice = getChoice();
  if (choice === "1") loadPixel();
  else if (choice === null && banner) banner.hidden = false;

  if (banner) {
    banner.addEventListener("click", function (e) {
      var btn = e.target.closest("[data-ads]");
      if (!btn) return;
      var yes = btn.getAttribute("data-ads") === "yes";
      setChoice(yes ? "1" : "0");
      hideBanner();
      if (yes) loadPixel();
    });
  }

  // Attach consent + browser ids to the order at submit time (cookies exist by then).
  var form = document.querySelector("form[data-order-form]");
  if (form) {
    form.addEventListener("submit", function () {
      if (getChoice() !== "1") return;
      var fbc = cookie("_fbc");
      var clid = new URLSearchParams(location.search).get("fbclid");
      if (!fbc && clid) fbc = "fb.1." + Date.now() + "." + clid;
      var set = function (name, value) {
        var input = form.querySelector('input[name="' + name + '"]');
        if (input) input.value = value;
      };
      set("ad_consent", "1");
      set("fbp", cookie("_fbp"));
      set("fbc", fbc);
    });
  }
})();

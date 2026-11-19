// UI behaviours without inline JavaScript (the Content-Security-Policy forbids it).
(function () {
  "use strict";

  // <form data-confirm="Are you sure?"> asks for confirmation before submitting.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    var button = event.submitter;
    var message = (button && button.getAttribute && button.getAttribute("data-confirm")) ||
      (form && form.getAttribute && form.getAttribute("data-confirm"));
    if (message && !window.confirm(message)) {
      event.preventDefault();
    }
  }, true);

  // Table rows with data-href open the order (links inside keep working).
  document.addEventListener("click", function (event) {
    var row = event.target.closest && event.target.closest("tr[data-href]");
    if (row && !event.target.closest("a, button, form")) {
      window.location.href = row.getAttribute("data-href");
    }
  });

  // <button data-print> prints the page (receipts).
  document.addEventListener("click", function (event) {
    if (event.target.closest("[data-print]")) window.print();
  });

  // Mobile navigation drawer.
  document.addEventListener("click", function (event) {
    if (event.target.closest("[data-nav-toggle]")) {
      document.body.classList.toggle("nav-open");
    } else if (event.target.closest(".scrim")) {
      document.body.classList.remove("nav-open");
    }
  });

  // Light / dark toggle (remembered per browser).
  document.addEventListener("click", function (event) {
    if (!event.target.closest("[data-theme-toggle]")) return;
    var root = document.documentElement;
    var current = root.getAttribute("data-theme") ||
      (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    var next = current === "dark" ? "light" : "dark";
    root.setAttribute("data-theme", next);
    try { window.localStorage.setItem("cc-theme", next); } catch (e) { /* ignore */ }
  });

  // Toasts disappear after a few seconds.
  function hideToasts() {
    document.querySelectorAll(".toast").forEach(function (toast) {
      window.setTimeout(function () {
        toast.classList.add("hide");
        window.setTimeout(function () { toast.remove(); }, 400);
      }, 4500);
    });
  }

  // Chart tooltip: columns carry data-tip="label;Name|value;Name|value...".
  function bindCharts(root) {
    (root || document).querySelectorAll(".chart").forEach(function (chart) {
      if (chart.dataset.bound) return;
      chart.dataset.bound = "1";
      var tip = document.createElement("div");
      tip.className = "chart-tip";
      tip.hidden = true;
      chart.appendChild(tip);
      var keys = ["", "", "confirmed", "cancelled", "no_answer", "pending"];

      function show(col, clientX) {
        chart.querySelectorAll(".col.on").forEach(function (c) { c.classList.remove("on"); });
        col.classList.add("on");
        var parts = col.getAttribute("data-tip").split(";");
        tip.textContent = "";
        var title = document.createElement("b");
        title.textContent = parts[0];
        tip.appendChild(title);
        parts.slice(1).forEach(function (p, i) {
          var kv = p.split("|");
          var r = document.createElement("div");
          r.className = "r";
          var sw = document.createElement("span");
          sw.className = keys[i + 1] ? "sw sw-" + keys[i + 1] : "sw";
          var name = document.createElement("span");
          name.textContent = kv[0];
          var val = document.createElement("span");
          val.textContent = kv[1];
          r.appendChild(sw); r.appendChild(name); r.appendChild(val);
          tip.appendChild(r);
        });
        tip.hidden = false;
        var box = chart.getBoundingClientRect();
        var x = clientX - box.left + 14;
        if (x + tip.offsetWidth > box.width) x = clientX - box.left - tip.offsetWidth - 14;
        tip.style.left = Math.max(0, x) + "px";
        tip.style.top = "8px";
      }
      chart.querySelectorAll(".col").forEach(function (col) {
        col.addEventListener("mousemove", function (e) { show(col, e.clientX); });
        col.addEventListener("focus", function () {
          var b = col.getBoundingClientRect();
          show(col, b.left + b.width / 2);
        });
        col.addEventListener("blur", function () { tip.hidden = true; col.classList.remove("on"); });
      });
      chart.addEventListener("mouseleave", function () {
        tip.hidden = true;
        chart.querySelectorAll(".col.on").forEach(function (c) { c.classList.remove("on"); });
      });
    });
  }

  function init() { hideToasts(); bindCharts(); }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
  document.addEventListener("htmx:afterSwap", function (e) { bindCharts(e.target); });
})();

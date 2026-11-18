// Loaded in <head> (blocking, tiny) so the saved theme applies before first paint.
(function () {
  try {
    var saved = window.localStorage.getItem("cc-theme");
    if (saved === "dark" || saved === "light") {
      document.documentElement.setAttribute("data-theme", saved);
    }
  } catch (e) { /* storage blocked: follow the OS setting */ }
})();

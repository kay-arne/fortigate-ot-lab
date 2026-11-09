// Licht/donker: volgt het systeem tot de gebruiker kiest; de keuze wordt onthouden.
(function () {
  var KEY = "otlab-theme", root = document.documentElement;
  var mq = window.matchMedia("(prefers-color-scheme: dark)");
  function stored() { try { return localStorage.getItem(KEY); } catch (e) { return null; } }
  function effective() { return root.dataset.theme || (mq.matches ? "dark" : "light"); }
  function sync() {
    document.querySelectorAll("[data-theme-toggle]").forEach(function (b) { b.dataset.mode = effective(); });
  }
  var t = stored();
  if (t === "light" || t === "dark") root.dataset.theme = t;
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-theme-toggle]").forEach(function (b) {
      b.addEventListener("click", function () {
        var next = effective() === "dark" ? "light" : "dark";
        root.dataset.theme = next;
        try { localStorage.setItem(KEY, next); } catch (e) { /* zonder opslag blijft de keuze per pagina */ }
        sync();
      });
    });
    sync();
  });
  mq.addEventListener("change", sync);
})();

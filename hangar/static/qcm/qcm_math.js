// Formules des pages QCM hors du jeu (signalements…) : spans .math rendus par le KaTeX local.
(function () {
  function rendre() {
    if (!window.katex) return;
    document.querySelectorAll(".math").forEach((el) => {
      if (el.dataset.done) return;
      const tex = el.textContent.trim().replace(/^\\[\[(]|\\[\])]$/g, "");
      try { window.katex.render(tex, el, { displayMode: el.classList.contains("math-block"), throwOnError: false }); } catch (e) { /* texte brut */ }
      el.dataset.done = "1";
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", rendre); else rendre();
})();

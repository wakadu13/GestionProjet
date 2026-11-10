// Application Android (WebView de Revision) : le bouton « retour » d'Android appelle d'abord
// window.appliRetour() ; s'il renvoie vrai, la page a refermé quelque chose (fenêtre, menu,
// tiroir de navigation) et l'appli ne revient pas à la page précédente (MainActivity.onBackPressed).
(function () {
  window.appliRetour = function () {
    const fenetre = document.querySelector("dialog[open]");
    if (fenetre) { fenetre.close(); return true; }
    const menus = document.querySelectorAll(".dd.open");
    if (menus.length) { menus.forEach((m) => m.classList.remove("open")); return true; }
    if (document.body.classList.contains("sb-open")) { document.body.classList.remove("sb-open"); return true; }
    if (document.documentElement.classList.contains("ai-open") && window.HANGAR && window.HANGAR.toggleAI) {
      window.HANGAR.toggleAI();
      return true;
    }
    return false;
  };
})();

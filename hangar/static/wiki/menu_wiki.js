// Menu de gauche du wiki : chaque page n'écrit que les dossiers de premier niveau et la branche de la
// page affichée. Le contenu d'un autre dossier est demandé au serveur à son ouverture (route
// /menu-wiki/?dossier=…), puis gardé dans la page. Hors connexion, un dossier déjà ouvert une fois, ou
// gardé avec « ⤓ Hors ligne » (route /hors-ligne/dossier.json), reste disponible (le service worker garde
// la réponse).
(function () {
  "use strict";

  async function remplir(details) {
    const enfants = details.querySelector(":scope > .wiki-tree-children");
    const menu = details.closest("[data-menu-url]");
    if (!enfants || !menu || details.dataset.chargement) return;
    details.dataset.chargement = "1";
    details.setAttribute("aria-busy", "true");
    try {
      const adresse = menu.dataset.menuUrl + "?dossier=" + encodeURIComponent(details.dataset.menuDossier);
      const r = await fetch(adresse, { credentials: "same-origin", headers: { Accept: "text/html" } });
      if (!r.ok || r.redirected) throw new Error(String(r.status));
      enfants.innerHTML = await r.text();
      details.removeAttribute("data-menu-dossier");
      enfants.querySelectorAll("a.wiki-tree-note").forEach((a) => {
        if (a.pathname === location.pathname) a.classList.add("active");
      });
    } catch (e) {
      const message = document.createElement("div");
      message.className = "wiki-sb-empty";
      message.textContent = navigator.onLine === false
        ? "Pas de connexion : ce dossier n’a pas été gardé sur cet appareil. Referme-le puis rouvre-le une fois reconnecté."
        : "Impossible d’afficher ce dossier pour l’instant. Referme-le puis rouvre-le pour réessayer.";
      enfants.replaceChildren(message);
    } finally {
      delete details.dataset.chargement;
      details.removeAttribute("aria-busy");
    }
  }

  // « toggle » ne remonte pas : écouté à la capture, il sert aussi au menu remplacé par le plein écran.
  document.addEventListener("toggle", (e) => {
    const d = e.target;
    if (d.open && d.matches && d.matches("details[data-menu-dossier]")) remplir(d);
  }, true);
})();

// Plein écran du wiki (adapté de plein_ecran.js de Revision) : il ne reste que le texte.
// La barre du haut, la barre d'icônes, le menu de gauche et le panneau IA de Hangar sont
// cachés ; le menu revient quand la souris touche le bord gauche de l'écran (ou d'un appui
// sur le bord). Bouton ⛶ ou Ctrl+Maj+F ; Échap pour quitter.
(function () {
  "use strict";
  const html = document.documentElement;
  const body = document.body;
  const BORD = 10;
  const MARGE = 48;
  let actif = false;

  function boutons() {
    return document.querySelectorAll("[data-plein-ecran]");
  }

  function majBoutons() {
    boutons().forEach((b) => {
      b.setAttribute("aria-pressed", String(actif));
      b.title = actif ? "Quitter le plein écran (Échap)" : "Plein écran (Ctrl+Maj+F)";
    });
  }

  function annoncer() {
    window.dispatchEvent(new CustomEvent("wiki:plein-ecran", { detail: { actif } }));
  }

  function astuce(texte) {
    document.querySelector(".pe-astuce")?.remove();
    const div = document.createElement("div");
    div.className = "pe-astuce";
    div.textContent = texte;
    body.appendChild(div);
    setTimeout(() => div.classList.add("pe-astuce-fin"), 3200);
    setTimeout(() => div.remove(), 4200);
  }

  function entrer() {
    if (actif) return;
    actif = true;
    html.classList.add("wiki-focus");
    body.classList.add("plein-ecran");
    if (document.fullscreenEnabled && !document.fullscreenElement) {
      // Refusé (iPhone, réglage du navigateur) : la page reste quand même épurée.
      html.requestFullscreen({ navigationUI: "hide" }).catch(() => {});
    }
    const sortie = document.createElement("button");
    sortie.type = "button";
    sortie.className = "pe-sortie btn btn-sm";
    sortie.textContent = "Quitter le plein écran";
    sortie.addEventListener("click", sortir);
    body.appendChild(sortie);
    majBoutons();
    astuce("Plein écran : le menu revient au bord gauche de l'écran. Échap pour quitter.");
    annoncer();
  }

  function sortir() {
    if (!actif) return;
    actif = false;
    html.classList.remove("wiki-focus", "pe-gauche");
    body.classList.remove("plein-ecran");
    document.querySelector(".pe-sortie")?.remove();
    document.querySelector(".pe-astuce")?.remove();
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    majBoutons();
    annoncer();
  }

  function basculer() {
    if (actif) sortir();
    else entrer();
  }

  document.addEventListener("click", (e) => {
    const b = e.target.closest && e.target.closest("[data-plein-ecran]");
    if (!b) return;
    e.preventDefault();
    document.querySelectorAll(".dd.open").forEach((d) => d.classList.remove("open"));
    basculer();
  });
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.shiftKey && !e.altKey && e.key.toLowerCase() === "f") {
      e.preventDefault();
      basculer();
    } else if (e.key === "Escape" && actif && !document.querySelector("dialog[open]")) {
      sortir();
    }
  });
  // Échap (géré par le navigateur) ou F11 : on suit.
  document.addEventListener("fullscreenchange", () => {
    if (!document.fullscreenElement && actif) sortir();
  });

  // Menu de gauche : revient au bord gauche (souris ou appui), repart quand on s'en éloigne.
  const sidebar = document.getElementById("sidebar");
  document.addEventListener("mousemove", (e) => {
    if (!actif || !sidebar) return;
    if (e.clientX <= BORD) html.classList.add("pe-gauche");
    else if (html.classList.contains("pe-gauche") && e.clientX > sidebar.offsetWidth + MARGE) html.classList.remove("pe-gauche");
  });
  document.addEventListener("touchstart", (e) => {
    if (!actif || !sidebar) return;
    const x = e.touches[0].clientX;
    if (x <= 24) html.classList.add("pe-gauche");
    else if (html.classList.contains("pe-gauche") && !sidebar.contains(e.target)) html.classList.remove("pe-gauche");
  }, { passive: true });
})();

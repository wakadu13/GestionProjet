// Lecture et plein écran du wiki (port complet de plein_ecran.js de Revision, adapté à la coquille de Hangar).
//
// Pages de lecture (une page ou un PDF ; classe menu-cache sur <body>) : le menu de gauche de la section est
// caché pour laisser toute la largeur au texte. Il revient d'un appui sur la languette › (bord gauche de la
// page), d'un appui ou d'un glissement du doigt depuis ce bord, ou quand la souris touche ce bord ; un appui
// sur le texte (ou Échap) le referme. Le bouton ▯ en tête du menu le garde affiché (cookie wiki_menu=epingle,
// choix retenu sur l'appareil). Sur téléphone, le menu reste derrière le bouton ☰ de la barre du haut.
//
// Plein écran (bouton ⛶, Ctrl+Maj+F ; Échap pour quitter) : il ne reste que le texte. Le menu revient au bord
// gauche de l'écran, le panneau de droite (plan de la page, sommaire du PDF, plan de l'éditeur) au bord droit,
// la barre d'outils de l'éditeur au bord haut ; ils se cachent quand la souris s'en éloigne. On passe d'une
// page du wiki à l'autre (menu, liens, recherche, retour) sans quitter le plein écran.
(function () {
  "use strict";
  const body = document.body;
  const BORD = 12;     // px depuis le bord
  const MARGE = 48;    // la souris s'éloigne d'autant du panneau avant qu'il se cache
  const ATTENTE = 160; // ms au bord avant d'ouvrir le menu (page de lecture : la barre d'icônes est juste à côté)
  const ASIDE = ".wiki-doc-aside, .wiki-edit-aside, .wiki-pdf-aside";
  const COOKIE = "wiki_menu";
  let actif = false;
  let astuceTimer = null;
  // Sur téléphone, le menu est déjà un tiroir (☰) : menu-cache ne joue qu'en écran large.
  const large = window.matchMedia("(min-width: 821px)");
  const menuCache = () => body.classList.contains("menu-cache") && large.matches;
  const escamotable = () => actif || menuCache();

  const $ = (sel, root = document) => root.querySelector(sel);
  const sidebar = () => document.getElementById("sidebar");
  const aside = () => $(ASIDE);
  const toolbar = () => $(".editing .toolbar");

  // Languette › du bord gauche (pages de lecture, et plein écran sur écran tactile).
  if (!$(".menu-poignee")) {
    const p = document.createElement("button");
    p.type = "button";
    p.className = "menu-poignee";
    p.dataset.ouvrirMenu = "";
    p.setAttribute("aria-label", "Afficher le menu");
    p.title = "Afficher le menu";
    p.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9.5 6 6 6-6 6"/></svg>';
    body.appendChild(p);
  }

  // Bord gauche du texte : à droite de la barre d'icônes (cachée en plein écran).
  function bordGauche() {
    const rail = $(".rail");
    if (actif || !rail || !large.matches) return 0;
    const r = rail.getBoundingClientRect();
    return r.width ? r.right : 0;
  }

  function majBoutons() {
    document.querySelectorAll("[data-plein-ecran]").forEach((b) => {
      b.setAttribute("aria-pressed", String(actif));
      b.title = actif ? "Quitter le plein écran (Échap)" : "Plein écran (Ctrl+Maj+F)";
    });
  }

  function cacher(...cotes) {
    cotes.forEach((c) => body.classList.remove("pe-" + c));
  }

  function annoncer() {
    window.dispatchEvent(new CustomEvent("wiki:plein-ecran", { detail: { actif } }));
  }

  function astuce(texte, duree = 3500) {
    $(".pe-astuce")?.remove();
    const div = document.createElement("div");
    div.className = "pe-astuce";
    div.setAttribute("role", "status");
    if (!texte) {
      const parties = ["Bord gauche : menu"];
      if (aContenu(aside())) parties.push("bord droit : plan");
      if (toolbar()) parties.push("bord haut : barre d'outils");
      texte = parties.join(" · ") + " · Échap pour quitter";
    }
    div.textContent = texte;
    body.appendChild(div);
    clearTimeout(astuceTimer);
    astuceTimer = setTimeout(() => div.classList.add("pe-astuce-fin"), duree);
    div.addEventListener("transitionend", () => div.remove());
  }

  function entrer() {
    if (actif) return;
    actif = true;
    body.classList.add("plein-ecran");
    document.querySelectorAll(".dd.open").forEach((d) => d.classList.remove("open"));
    majBoutons();
    if (document.fullscreenEnabled && !document.fullscreenElement) {
      // Refusé (iPhone, réglage du navigateur) : la page reste quand même épurée.
      document.documentElement.requestFullscreen({ navigationUI: "hide" }).catch(() => {});
    }
    if (!$(".pe-sortie")) {
      const sortie = document.createElement("button");
      sortie.type = "button";
      sortie.className = "pe-sortie btn btn-sm";
      sortie.textContent = "Quitter le plein écran";
      sortie.addEventListener("click", sortir);
      body.appendChild(sortie);
    }
    astuce();
    annoncer();
  }

  function sortir() {
    if (!actif) return;
    actif = false;
    body.classList.remove("plein-ecran");
    cacher("gauche", "droite", "haut");
    majBoutons();
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    $(".pe-astuce")?.remove();
    $(".pe-sortie")?.remove();
    annoncer();
  }

  function basculer() {
    if (actif) sortir();
    else entrer();
  }

  // Échap (géré par le navigateur) ou F11 : on suit.
  document.addEventListener("fullscreenchange", () => {
    if (!document.fullscreenElement) sortir();
  });

  function ouvrirMenu() {
    if (body.classList.contains("pe-gauche")) return;
    body.classList.add("pe-gauche");
    // Page affichée hors de vue dans l'arborescence : on la montre.
    const active = sidebar() && sidebar().querySelector(".wiki-tree-note.active");
    if (active) {
      const r = active.getBoundingClientRect();
      if (r.top < 0 || r.bottom > window.innerHeight) active.scrollIntoView({ block: "center" });
    }
  }

  const estEpingle = () => document.cookie.split(/;\s*/).includes(`${COOKIE}=epingle`);

  // Garder le menu affiché pendant la lecture, ou le cacher de nouveau.
  function epingler(garder) {
    const secure = location.protocol === "https:" ? "; Secure" : "";
    document.cookie = garder ? `${COOKIE}=epingle; path=/; max-age=31536000; SameSite=Lax${secure}`
      : `${COOKIE}=; path=/; max-age=0; SameSite=Lax${secure}`;
    if (body.classList.contains("wiki-lecture")) body.classList.toggle("menu-cache", !garder);
    cacher("gauche");
    majEpingle();
    if (window.HANGAR && window.HANGAR.toast && !body.classList.contains("wiki-lecture")) {
      window.HANGAR.toast(garder ? "Le menu restera affiché pendant la lecture." : "Le menu se cachera pendant la lecture d’une page.", "info");
    }
  }

  function majEpingle() {
    const garder = estEpingle();
    document.querySelectorAll("[data-epingler-menu]").forEach((b) => {
      b.setAttribute("aria-pressed", String(garder));
      b.title = garder ? "Cacher le menu pendant la lecture" : "Garder le menu affiché pendant la lecture";
      b.setAttribute("aria-label", b.title);
    });
  }

  // -- Panneaux qui réapparaissent au bord de l'écran ---------------------------

  const aContenu = (el) => el && el.textContent.trim() !== "";
  // Panneau à laisser ouvert : on y tape (recherche) ou un menu déroulant y est ouvert.
  const garde = (el) => el && ((el.contains(document.activeElement) && document.activeElement.matches("input, textarea, select"))
    || el.querySelector(".toolbar-menu[open], .dd.open"));

  let auBord = 0;
  function suivre(x, y) {
    const largeur = document.documentElement.clientWidth;
    const sb = sidebar();
    const a = aside();
    const bar = toolbar();
    const g = bordGauche();

    if (x >= g - 2 && x <= g + BORD) {
      // Plein écran : tout de suite ; page de lecture : après un court instant (la barre d'icônes est à côté).
      if (actif) ouvrirMenu();
      else if (!auBord) auBord = setTimeout(() => { auBord = 0; ouvrirMenu(); }, ATTENTE);
    } else {
      clearTimeout(auBord);
      auBord = 0;
      if (body.classList.contains("pe-gauche") && sb && x > sb.getBoundingClientRect().right + MARGE && !garde(sb)) cacher("gauche");
    }
    if (!actif) return; // pages de lecture : seulement le menu

    if (x >= largeur - BORD && aContenu(a)) body.classList.add("pe-droite");
    else if (body.classList.contains("pe-droite") && a && x < a.getBoundingClientRect().left - MARGE && !garde(a)) cacher("droite");

    if (bar) {
      if (y <= BORD) body.classList.add("pe-haut");
      else if (body.classList.contains("pe-haut") && y > bar.getBoundingClientRect().bottom + MARGE && !garde(bar)) cacher("haut");
    }
  }

  let attente = null;
  document.addEventListener("mousemove", (e) => {
    if (!escamotable() || attente) return;
    attente = requestAnimationFrame(() => {
      attente = null;
      suivre(e.clientX, e.clientY);
    });
  }, { passive: true });

  // Écran tactile : un appui sur le bord ouvre le panneau, un appui ailleurs (sur le texte) le referme.
  const PANNEAUX = `#sidebar, ${ASIDE}, .toolbar, .pe-astuce, .pe-sortie, [data-ouvrir-menu], .rail, .topbar, dialog, .ai-panel`;
  document.addEventListener("pointerdown", (e) => {
    if (!escamotable()) return;
    const dansPanneau = e.target.closest(PANNEAUX);
    if (e.pointerType !== "mouse" && !dansPanneau) {
      const largeur = document.documentElement.clientWidth;
      const g = bordGauche();
      if (e.clientX >= g && e.clientX <= g + 24) return void ouvrirMenu();
      if (actif && e.clientX >= largeur - 24 && aContenu(aside())) return void body.classList.add("pe-droite");
      if (actif && e.clientY <= 24 && toolbar()) return void body.classList.add("pe-haut");
    }
    if (!dansPanneau) cacher("gauche", "droite", "haut");
  });

  // Glisser le doigt depuis le bord gauche ouvre le menu, vers la gauche le referme.
  let glisse = null;
  document.addEventListener("touchstart", (e) => {
    const t = e.touches[0];
    const ouvert = body.classList.contains("pe-gauche");
    const g = bordGauche();
    glisse = escamotable() && e.touches.length === 1 && (ouvert || (t.clientX >= g && t.clientX <= g + 32))
      ? { x: t.clientX, y: t.clientY, ouvert } : null;
  }, { passive: true });
  document.addEventListener("touchend", (e) => {
    if (!glisse) return;
    const t = e.changedTouches[0];
    const dx = t.clientX - glisse.x;
    if (Math.abs(dx) > 50 && Math.abs(dx) > 1.5 * Math.abs(t.clientY - glisse.y)) {
      if (dx > 0 && !glisse.ouvert) ouvrirMenu();
      else if (dx < 0 && glisse.ouvert) cacher("gauche");
    }
    glisse = null;
  }, { passive: true });

  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.shiftKey && !e.altKey && e.key.toLowerCase() === "f") {
      e.preventDefault();
      basculer();
      return;
    }
    // Échap referme d'abord un panneau ouvert.
    if (e.key === "Escape" && /\bpe-(gauche|droite|haut)\b/.test(body.className) && !e.target.closest(".cm-editor, dialog")) {
      cacher("gauche", "droite", "haut");
      return;
    }
    // Sans plein écran du navigateur (refusé) : Échap quitte aussi, sauf dans l'éditeur, une fenêtre ou
    // un champ, qui s'en servent déjà.
    if (e.key === "Escape" && actif && !document.fullscreenElement && !e.defaultPrevented
        && !e.target.closest(".cm-editor, input, textarea, select, dialog") && !document.querySelector("dialog[open]")) {
      sortir();
    }
  });

  document.addEventListener("click", (e) => {
    if (!e.target.closest) return;
    if (e.target.closest("[data-plein-ecran]")) {
      e.preventDefault();
      document.querySelectorAll(".dd.open").forEach((d) => d.classList.remove("open"));
      basculer();
    } else if (e.target.closest("[data-epingler-menu]")) {
      e.preventDefault();
      epingler(!estEpingle());
    } else if (e.target.closest("[data-ouvrir-menu]") && escamotable()) {
      e.preventDefault();
      if (body.classList.contains("pe-gauche")) cacher("gauche");
      else ouvrirMenu();
    }
  });

  // Menu revenu au bord : son bouton « Masquer le menu » le renvoie simplement (sans replier le menu des
  // autres pages, ce que fait ce bouton d'ordinaire).
  document.addEventListener("click", (e) => {
    if (!escamotable() || !e.target.closest || !e.target.closest("#sidebar [data-sidebar-collapse]")) return;
    e.preventDefault();
    e.stopPropagation();
    cacher("gauche");
  }, true);

  majEpingle();
  // Première lecture avec le menu caché : dire où il est passé.
  if (menuCache()) {
    let vu = true;
    try { vu = localStorage.getItem("wiki-menu-cache-vu") === "1"; localStorage.setItem("wiki-menu-cache-vu", "1"); } catch (e) { /* stockage bloqué */ }
    if (!vu) astuce("Menu caché pendant la lecture : touche le bord gauche (languette ›) pour l’afficher, ou « Garder le menu affiché » en tête du menu", 6000);
  }

  // -- Changer de page sans quitter le plein écran ------------------------------

  // Pages du wiki qui se lisent sans script en plus : chargées sans recharger le document (recharger
  // ferait quitter le plein écran au navigateur). L'éditeur, le lecteur PDF, le graphe… rechargent la page.
  const LECTURE = /^\/(wiki|sommaire|tags|recherche|documentation|historique)\//;
  const scriptsCharges = () => new Set([...document.scripts].filter((s) => s.src).map((s) => s.src));
  let enCours = null;

  function eligible(url) {
    return url.origin === location.origin && LECTURE.test(url.pathname);
  }

  async function naviguer(url, { retour = false } = {}) {
    const controle = new AbortController();
    enCours?.abort();
    enCours = controle;
    let doc;
    let finale;
    try {
      const reponse = await fetch(url, { headers: { Accept: "text/html" }, signal: controle.signal, credentials: "same-origin" });
      finale = new URL(reponse.url);
      if (!reponse.ok || !(reponse.headers.get("Content-Type") || "").includes("text/html")) throw new Error();
      doc = new DOMParser().parseFromString(await reponse.text(), "text/html");
    } catch (e) {
      if (e.name === "AbortError") return;
      location.href = url;
      return;
    }
    const charges = scriptsCharges();
    const requis = [...doc.querySelectorAll("script[src]")].map((s) => new URL(s.getAttribute("src"), finale).href);
    const main = doc.querySelector("main.main");
    const menu = doc.getElementById("sidebar");
    if (!main || !menu || requis.some((src) => !charges.has(src)) || !actif || !eligible(finale)) {
      location.href = finale.href;
      return;
    }

    // Première page du plein écran : marquée pour que « Retour » y revienne sans recharger.
    if (!history.state || !history.state.pleinEcran) history.replaceState({ pleinEcran: true }, "");
    if (!retour) history.pushState({ pleinEcran: true }, "", finale.href);

    $("main.main").replaceWith(document.adoptNode(main));
    sidebar().replaceWith(document.adoptNode(menu));
    document.title = doc.title;
    const garder = ["plein-ecran"].filter((c) => body.classList.contains(c));
    body.className = doc.body.className;
    body.classList.add(...garder);
    const shell = $(".shell");
    const autre = doc.querySelector(".shell");
    if (shell && autre) shell.dataset.section = autre.dataset.section;

    cacher("gauche", "droite", "haut");
    majBoutons();
    majEpingle();
    const contenu = $("main.main .content");
    if (window.wikiInitPage) window.wikiInitPage($("main.main"));
    const ancre = finale.hash && document.getElementById(decodeURIComponent(finale.hash.slice(1)));
    if (ancre) ancre.scrollIntoView();
    else if (contenu) contenu.scrollTop = 0;
    if (contenu) {
      contenu.setAttribute("tabindex", "-1");
      contenu.focus({ preventScroll: true });
    }
  }

  document.addEventListener("click", (e) => {
    if (!actif || e.defaultPrevented || e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
    const lien = e.target.closest("a[href]");
    if (!lien || lien.target || lien.hasAttribute("download")) return;
    const url = new URL(lien.href, location.href);
    if (!eligible(url)) return;
    // Ancre de la même page : le navigateur s'en charge.
    if (url.pathname === location.pathname && url.search === location.search && url.hash) return;
    e.preventDefault();
    naviguer(url.href);
  });

  // Formulaires de recherche (GET) : même chose.
  document.addEventListener("submit", (e) => {
    const form = e.target;
    if (!actif || e.defaultPrevented || (form.method || "get").toLowerCase() !== "get") return;
    const url = new URL(form.action || location.href, location.href);
    if (!eligible(url)) return;
    url.search = new URLSearchParams(new FormData(form)).toString();
    e.preventDefault();
    naviguer(url.href);
  });

  window.addEventListener("popstate", (e) => {
    if (!e.state || !e.state.pleinEcran) return;
    // Plein écran quitté entre-temps : la page affichée n'est pas celle de l'adresse.
    if (actif) naviguer(location.href, { retour: true });
    else location.reload();
  });

  window.wikiPleinEcran = { basculer, entrer, sortir, actif: () => actif };
})();

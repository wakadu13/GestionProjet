// Palette de commandes du wiki (port de palette.js de Revision), branchée sur la palette Ctrl+K de Hangar
// (une seule palette) : ouvrir une page ou un PDF du wiki, un set de QCM, lancer une session de langue,
// ou une commande (nouvelle page, importer, plein écran, Aujourd'hui, Demander au wiki…). Recherche
// tolérante aux accents. Données : /palette.json, chargé à la première ouverture (puis gardé une minute).
// Dans l'éditeur, Ctrl+K reste le lien vers une page : Ctrl+P ouvre alors la palette.
(function () {
  "use strict";
  const H = window.HANGAR;
  if (!H) return;
  const URL_DONNEES = "/palette.json";
  const GARDE_MS = 60000;
  let donnees = null;
  let icones = {};
  let chargement = null;
  let chargeA = 0;

  // Même pliage que hangar/texte.py (plier) : casse, accents, forme de l'apostrophe et ligatures ignorés.
  const normaliser = (s) => String(s || '').replace(/[\u2018\u2019\u201a\u201b\u02bc\u2032\u00b4]/g, "'").replace(/[\u00ab\u00bb\u201c\u201d\u201e\u201f]/g, '"').replace(/[\u2010\u2011]/g, '-')
    .normalize('NFKD').toLowerCase().replace(/\u0153/g, 'oe').replace(/\u00e6/g, 'ae').replace(/[\u0300-\u036f]/g, '');

  function charger() {
    if (chargement && Date.now() - chargeA < GARDE_MS) return chargement;
    chargeA = Date.now();
    chargement = fetch(URL_DONNEES, { headers: { Accept: "application/json" }, credentials: "same-origin" })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then((d) => {
        icones = d.icones || {};
        const genre = (items, g) => (items || []).map((x) => ({ ...x, g }));
        donnees = [
          ...genre(d.commandes, "Commande"),
          ...genre(d.langues, "Langue"),
          ...genre(d.pages, "Page"),
          ...genre(d.qcm, "QCM"),
        ].map((x) => ({ ...x, n: normaliser(x.t + " " + (x.s || "") + " " + (x.k || "")) }));
        return donnees;
      })
      .catch(() => { chargement = null; donnees = donnees || []; return donnees; });
    return chargement;
  }

  // Score : début du titre > début d'un mot > contenu > lettres dans l'ordre (comme Revision).
  function score(item, mots) {
    let total = 0;
    for (const mot of mots) {
      const i = item.n.indexOf(mot);
      if (i === 0) total += 100;
      else if (i > 0 && item.n[i - 1] === " ") total += 60;
      else if (i > 0) total += 30;
      else {
        let k = 0;
        for (const c of item.n) { if (c === mot[k]) k++; if (k === mot.length) break; }
        if (k < mot.length) return -1;
        total += 5;
      }
    }
    return total - item.n.length / 200;
  }

  const section = () => (document.querySelector(".shell") || {}).dataset?.section || "";
  const ICONE_GENRE = { Commande: "cmd", Langue: "langue", Page: "page", QCM: "qcm" };

  function element(x) {
    if (x.a === "edit" && !document.querySelector("[data-edit-shortcut]")) return null;
    const it = { title: x.t, sub: x.s || "", icon: icones[x.ic || ICONE_GENRE[x.g]] || "" };
    if (x.a) it.run = () => action(x.a);
    else it.url = x.u;
    return it;
  }

  function action(a) {
    if (a === "fullscreen") {
      if (window.wikiPleinEcran) window.wikiPleinEcran.basculer();
      else if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
      else document.documentElement.requestFullscreen().catch(() => {});
    } else if (a === "edit") {
      const lien = document.querySelector("[data-edit-shortcut]");
      if (lien) location.href = lien.href;
    } else if (a === "theme") {
      document.querySelector("[data-theme-toggle]")?.click();
    }
  }

  // Source de la palette de Hangar : [[titre du groupe, éléments], …]
  async function source(q) {
    const liste = await charger();
    if (!liste.length) return [];
    const groupes = { Commande: [], Langue: [], Page: [], QCM: [] };
    const meilleur = { Commande: 0, Langue: -1, Page: -2, QCM: -3 };
    if (!q) {
      // Sans texte : quelques commandes et les sessions du jour, dans les sections Wiki, QCM et IA.
      if (!["wiki", "qcm", "ia"].includes(section())) return [];
      liste.filter((x) => x.g === "Commande").slice(0, 8).forEach((x) => groupes.Commande.push(x));
      liste.filter((x) => x.g === "Langue" && x.t.startsWith("Session du jour")).slice(0, 4).forEach((x) => groupes.Langue.push(x));
    } else {
      // Les mieux notés d'abord (comme la liste unique de Revision), rangés par genre ; le genre du
      // meilleur résultat vient en tête, pour qu'Entrée ouvre ce qui correspond le mieux.
      const mots = normaliser(q).split(/\s+/).filter(Boolean);
      let n = 0;
      liste.map((x) => ({ x, s: score(x, mots) }))
        .filter((r) => r.s >= 0)
        .sort((a, b) => b.s - a.s)
        .forEach(({ x, s }) => {
          if (n >= 18 || groupes[x.g].length >= (x.g === "Page" ? 8 : 6)) return;
          if (!groupes[x.g].length) meilleur[x.g] = s;
          groupes[x.g].push(x);
          n++;
        });
    }
    const conv = (arr) => arr.map(element).filter(Boolean);
    const TITRES = { Commande: "Wiki et révision", Page: "Pages et PDF du wiki", QCM: "QCM", Langue: "Langues" };
    return Object.keys(TITRES).sort((a, b) => meilleur[b] - meilleur[a]).map((g) => [TITRES[g], conv(groupes[g])]);
  }

  (H.paletteSources = H.paletteSources || []).push(source);
  // Données prêtes avant la première frappe.
  document.addEventListener("click", (e) => { if (e.target.closest && e.target.closest("[data-palette]")) charger(); }, true);

  // Dans l'éditeur, Ctrl+K insère un lien (editor.js) : Ctrl+P ouvre la palette à la place de l'impression.
  document.addEventListener("keydown", (e) => {
    if (!(e.ctrlKey || e.metaKey) || e.altKey || e.shiftKey || e.key.toLowerCase() !== "p") return;
    if (!document.getElementById("editor-form") || document.querySelector("dialog[open]:not(.palette)")) return;
    e.preventDefault();
    charger();
    if (H.openPalette) H.openPalette();
  });
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") charger();
  }, true);
})();

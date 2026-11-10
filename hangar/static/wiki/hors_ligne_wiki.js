// Lecture hors ligne du wiki (port de hors_ligne_wiki.js de Revision) :
// - sommaire : bouton « ⤓ Hors ligne » sur chaque dossier, qui met en cache d'un coup ses pages, ses images
//   et ses PDF (dans le cache du service worker /sw.js), ou les retire d'un nouveau clic ;
// - page « hors connexion » : liste des pages lisibles sur l'appareil.
(function () {
  "use strict";
  const CLE = "wiki-dossiers-hors-ligne";
  const porteur = document.querySelector("[data-cache]");
  const CACHE = (porteur && porteur.dataset.cache) || "hangar-wiki-v1";

  const lireGardes = () => { try { return JSON.parse(localStorage.getItem(CLE) || "{}"); } catch (e) { return {}; } };
  const ecrireGardes = (g) => { try { localStorage.setItem(CLE, JSON.stringify(g)); } catch (e) { /* stockage bloqué */ } };

  // -- Sommaire : un bouton par dossier --
  const sommaire = document.querySelector("[data-dossier-url]");
  if (sommaire && window.caches) {
    sommaire.querySelectorAll("summary[data-dossier]").forEach((summary) => {
      const dossier = summary.dataset.dossier;
      const b = document.createElement("button");
      b.type = "button";
      b.className = "btn btn-xs sommaire-hors-ligne";
      const etat = () => {
        const g = lireGardes()[dossier];
        b.textContent = g ? `✓ Hors ligne (${g.n})` : "⤓ Hors ligne";
        b.title = g ? `Gardé hors ligne le ${new Date(g.date).toLocaleDateString("fr-FR")} : clic pour le retirer de cet appareil`
          : "Garder les pages, images et PDF de ce dossier sur cet appareil, pour les lire sans connexion";
        b.classList.toggle("is-garde", !!g);
      };
      etat();
      b.addEventListener("click", async (e) => {
        e.preventDefault();
        e.stopPropagation();
        if (b.disabled) return;
        const g = lireGardes();
        if (g[dossier]) {
          if (!confirm(`Retirer « ${dossier} » du cache hors ligne de cet appareil ?`)) return;
          const cache = await caches.open(CACHE);
          await Promise.all((g[dossier].urls || []).filter((u) => !u.startsWith("/static/")).map((u) => cache.delete(u)));
          delete g[dossier];
          ecrireGardes(g);
          etat();
          return;
        }
        b.disabled = true;
        try {
          const r = await fetch(sommaire.dataset.dossierUrl + "?dossier=" + encodeURIComponent(dossier), {
            credentials: "same-origin", headers: { Accept: "application/json" },
          });
          if (!r.ok) throw new Error("liste indisponible (" + r.status + ")");
          const { urls } = await r.json();
          const cache = await caches.open(CACHE);
          let faits = 0;
          let rates = 0;
          for (const u of urls) {
            b.textContent = `${faits} / ${urls.length}…`;
            try {
              const rep = await fetch(u, { credentials: "same-origin" });
              if (rep.ok && !rep.redirected && rep.status === 200) await cache.put(u, rep); else rates++;
            } catch (err) { rates++; }
            faits++;
          }
          if (navigator.storage && navigator.storage.persist) navigator.storage.persist().catch(() => {});
          g[dossier] = { n: urls.length - rates, urls, date: Date.now() };
          ecrireGardes(g);
          etat();
          if (rates) b.title += ` · ${rates} fichier(s) non gardé(s)`;
          if (window.HANGAR && window.HANGAR.toast) window.HANGAR.toast(`« ${dossier} » est gardé sur cet appareil (${urls.length - rates} fichiers).`, "success");
        } catch (err) {
          b.textContent = "⤓ Hors ligne (réessayer)";
          b.title = err.message;
        } finally {
          b.disabled = false;
        }
      });
      summary.appendChild(b);
    });
  }

  // -- Page « hors connexion » : ce qui est lisible sur l'appareil --
  const liste = document.querySelector("[data-pages-hors-ligne]");
  if (liste) {
    const reseau = document.querySelector("[data-hl-reseau]");
    const majReseau = () => {
      if (!reseau) return;
      reseau.textContent = navigator.onLine ? "● En ligne" : "○ Hors ligne";
      reseau.classList.toggle("hors-ligne", !navigator.onLine);
    };
    majReseau();
    window.addEventListener("online", majReseau);
    window.addEventListener("offline", majReseau);
    document.querySelector("[data-hl-reessayer]")?.addEventListener("click", () => location.reload());
    if (!window.caches) {
      liste.textContent = "Cache indisponible sur ce navigateur.";
      return;
    }
    const NOMS = { "/sommaire/": "Sommaire", "/tags/": "Tags", "/graphe/": "Graphe des liens", "/documentation/": "Documentation" };
    caches.open(CACHE).then(async (cache) => {
      const pages = [];
      const vus = new Set();
      for (const req of await cache.keys()) {
        const url = new URL(req.url);
        if (url.origin !== location.origin || vus.has(url.pathname)) continue;
        const p = url.pathname;
        let nom = null;
        if (p === "/wiki/") nom = "Accueil du wiki";
        else if (p.startsWith("/wiki/")) nom = decodeURIComponent(p.slice(6));
        else if (p.startsWith("/pdf/")) nom = decodeURIComponent(p.slice(5)) + " (PDF)";
        else if (NOMS[p]) nom = NOMS[p];
        else if (p.startsWith("/documentation/")) nom = "Documentation · " + decodeURIComponent(p.slice(15).replace(/\/$/, ""));
        if (!nom) continue;
        vus.add(p);
        pages.push({ nom, href: p + url.search });
      }
      pages.sort((a, b) => a.nom.localeCompare(b.nom, "fr"));
      liste.replaceChildren();
      if (!pages.length) {
        liste.append(Object.assign(document.createElement("p"), { className: "muted", textContent: "Aucune page en cache pour l'instant." }));
        return;
      }
      const ul = document.createElement("ul");
      ul.className = "hl-pages";
      for (const p of pages) {
        const li = document.createElement("li");
        const a = document.createElement("a");
        a.href = p.href;
        a.textContent = p.nom;
        li.append(a);
        ul.append(li);
      }
      liste.append(ul);
    }).catch(() => { liste.textContent = "Cache indisponible sur ce navigateur."; });
  }
})();

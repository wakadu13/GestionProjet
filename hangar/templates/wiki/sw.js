// Service worker du wiki de Hangar (portée : tout le site, version {{ version }}) : lecture hors ligne.
// Port de sw_wiki.js de Revision.
// - Les pages du wiki visitées, les images et les fichiers statiques sont gardés en cache (réseau
//   d'abord, cache ensuite) : sans connexion, une page déjà lue s'affiche encore.
// - « ⤓ Hors ligne » sur un dossier du sommaire met en cache d'un coup ses pages, ses images et ses
//   PDF (hors_ligne_wiki.js, même cache).
// - Sans réseau ni cache, la page « hors connexion » liste ce qui est disponible.
// - Une page servie depuis le cache porte la date de sa copie (attribut data-copie-hors-ligne de
//   <body>) : wiki.js affiche alors « Copie gardée sur cet appareil… ».
// - Les fichiers statiques versionnés (?v=…, empreinte de leur contenu, voir web.py) et la feuille
//   d'icônes sont servis depuis le cache d'abord ; une seule version de chaque fichier est gardée.
// Seules les adresses de lecture du wiki sont prises en charge : les autres sections de Hangar, les
// requêtes POST, l'API et les QCM (qui ont leur propre application hors ligne) passent sans lui.
"use strict";
const CACHE = {{ cache|tojson }};
const HORS_CONNEXION = {{ hors_connexion|tojson }};
const A_PRECHARGER = {{ precharger|tojson }};
// Pages de lecture : réseau d'abord, puis cache, puis page « hors connexion ».
const PAGES = [/^\/wiki\//, /^\/pdf\//, /^\/sommaire\//, /^\/tags\//, /^\/graphe\/$/, /^\/documentation\//,
  /^\/hors-connexion\/$/];
// Autres ressources gardées au passage (réseau d'abord) : fichiers statiques, fichiers du coffre, graphe,
// dossiers du menu ouverts une fois (menu_wiki.js).
const RESSOURCES = [/^\/static\//, /^\/fichiers\//, /^\/graphe\.json$/, /^\/menu-wiki\/$/, /^\/icones\.svg$/];
// Fichier dont l'adresse porte l'empreinte de son contenu : la copie en cache est toujours la bonne.
const versionne = (url) => url.searchParams.has("v") && (url.pathname.startsWith("/static/") || url.pathname === "/icones.svg");
const DELAI_RESEAU_MS = 5000;

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    await Promise.all(A_PRECHARGER.map(async (url) => {
      try {
        const r = await fetch(url, { credentials: "same-origin", cache: "no-cache" });
        if (r.ok && !r.redirected) await cache.put(url, r);
        await oublierAnciennes(cache, url);
      } catch (e) { /* hors ligne pendant l'installation */ }
    }));
    await self.skipWaiting();
  })());
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    for (const nom of await caches.keys()) {
      if (nom.startsWith("hangar-wiki") && nom !== CACHE) await caches.delete(nom);
    }
    await self.clients.claim();
  })());
});

const correspond = (liste, chemin) => liste.some((re) => re.test(chemin));

// Autres versions (?v=…) d'un fichier qu'on vient de garder : inutiles désormais.
async function oublierAnciennes(cache, adresse) {
  const url = new URL(adresse, self.location.origin);
  if (!versionne(url)) return;
  for (const ancienne of await cache.keys(url.href, { ignoreSearch: true })) {
    if (ancienne.url !== url.href) await cache.delete(ancienne);
  }
}

// Fichier versionné : copie en cache d'abord ; sinon le réseau (et on le garde) ; sans réseau, une autre
// version du même fichier (page gardée avant une mise à jour de Hangar).
async function cacheDabord(request) {
  const cache = await caches.open(CACHE);
  const enCache = await cache.match(request);
  if (enCache) return enCache;
  try {
    const reponse = await fetch(request);
    if (reponse.ok && !reponse.redirected && reponse.status === 200) {
      await cache.put(request, reponse.clone());
      await oublierAnciennes(cache, request.url);
    }
    return reponse;
  } catch (e) {
    const autre = await cache.match(request, { ignoreSearch: true });
    if (autre) return autre;
    throw e;
  }
}

// Copie d'une page servie depuis le cache : la page le saura (bandeau affiché par wiki.js).
async function copieMarquee(reponse) {
  if (!(reponse.headers.get("Content-Type") || "").includes("text/html")) return reponse;
  const date = (reponse.headers.get("Date") || "").replace(/[^\w ,:+-]/g, "");
  const texte = (await reponse.text()).replace(/<body\b/i, `<body data-copie-hors-ligne="${date}"`);
  const entetes = new Headers(reponse.headers);
  entetes.delete("Content-Length");
  entetes.delete("Content-Encoding"); // corps déjà décodé par le cache
  return new Response(texte, { status: reponse.status, statusText: reponse.statusText, headers: entetes });
}

async function garder(cache, request, reponse) {
  // Une page de connexion (session expirée) ou une réponse partielle ne remplace pas ce qui est en cache.
  if (!reponse.ok || reponse.redirected || reponse.status !== 200 || reponse.type === "opaqueredirect") return;
  try { await cache.put(request, reponse.clone()); } catch (e) { /* quota, réponse non gardable */ }
}

// Réseau d'abord ; au-delà du délai, ou sans réseau, la copie en cache s'il y en a une
// (sinon on attend le réseau : une page lente n'échoue pas pour autant).
async function reseauPuisCache(request, { marquer = false } = {}) {
  const cache = await caches.open(CACHE);
  const reseau = fetch(request).then(async (reponse) => { await garder(cache, request, reponse); return reponse; });
  reseau.catch(() => {}); // échec traité plus bas (ou copie en cache déjà servie)
  // Copie en cache : la page exacte ; pour un fichier statique demandé sans ?v=, une version gardée.
  const statique = new URL(request.url).pathname.startsWith("/static/");
  const copie = async () => {
    const enCache = (await cache.match(request)) || (statique ? await cache.match(request, { ignoreSearch: true }) : null);
    return enCache && marquer ? copieMarquee(enCache) : enCache;
  };
  let minuteur;
  const delai = new Promise((resolve) => { minuteur = setTimeout(resolve, DELAI_RESEAU_MS, null); });
  try {
    const premier = await Promise.race([reseau, delai]);
    if (premier) return premier;
    return (await copie()) || await reseau;
  } catch (e) {
    const enCache = await copie();
    if (enCache) return enCache;
    throw e;
  } finally {
    clearTimeout(minuteur);
  }
}

async function page(request) {
  try {
    return await reseauPuisCache(request, { marquer: true });
  } catch (e) {
    return (await caches.match(HORS_CONNEXION)) || Response.error();
  }
}

// PDF : servis depuis le cache s'ils ont été gardés (dossier hors ligne), jamais mis en cache au passage
// (trop gros). Le lecteur demande des morceaux (en-tête Range) : on les découpe dans la copie gardée.
async function pdf(request) {
  const enCache = await caches.match(request);
  if (!enCache) return fetch(request);
  const m = /^bytes=(\d+)-(\d*)$/.exec(request.headers.get("Range") || "");
  if (!m) return enCache;
  const blob = await enCache.blob();
  const debut = Number(m[1]);
  const fin = m[2] ? Math.min(Number(m[2]), blob.size - 1) : blob.size - 1;
  if (debut >= blob.size) return new Response(null, { status: 416, headers: { "Content-Range": `bytes */${blob.size}` } });
  return new Response(blob.slice(debut, fin + 1), {
    status: 206,
    headers: {
      "Content-Type": "application/pdf", "Accept-Ranges": "bytes", "Content-Length": String(fin - debut + 1),
      "Content-Range": `bytes ${debut}-${fin}/${blob.size}`,
    },
  });
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  const chemin = url.pathname;
  if (request.mode === "navigate") {
    if (correspond(PAGES, chemin)) event.respondWith(page(request));
    return; // autres sections de Hangar : sans le service worker
  }
  if (chemin.startsWith("/fichiers/") && /\.pdf$/i.test(chemin)) {
    event.respondWith(pdf(request));
    return;
  }
  if (versionne(url)) event.respondWith(cacheDabord(request));
  else if (correspond(RESSOURCES, chemin)) event.respondWith(reseauPuisCache(request));
});

// Écouteurs ajoutés par d'autres modules (par exemple les notifications web) : gabarit facultatif
// templates/partials/sw_extensions.js, inclus ici s'il existe (un seul service worker pour la portée « / »).
{% include "partials/sw_extensions.js" ignore missing %}

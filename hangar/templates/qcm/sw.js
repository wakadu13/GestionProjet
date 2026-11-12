// Service worker des QCM hors ligne (portée : /qcm/). Port du sw.js de Revision.
// - Garde en cache la page hors ligne, ses scripts, styles et KaTeX : la page s'ouvre sans
//   connexion (sinon, toute page de /qcm/ y renvoie).
// - Garde les images des questions (/qcm/fichiers/, /fichiers/, images de « Mon espace »).
// - Envoie les réponses en attente dès que la connexion revient (Background Sync).
// - Affiche le rappel quotidien de révision (notifications web, qcm/rappels.py).
// Les fichiers statiques portent l'empreinte de leur contenu (?v=…, voir web.py) : une adresse
// versionnée est servie depuis le cache d'abord, sans aller-retour avec le serveur.
importScripts("{{ url_for('static', filename='qcm/qcm_attente.js') }}");

const VERSION = "{{ version }}";
const CACHE_DONNEES = QcmAttente.CACHE;                  // images des questions, gardées
const COQUILLE = "{{ url_for('qcm.hors_ligne') }}";
const MANIFESTE = "{{ url_for('qcm.manifeste') }}";
const ICONE = "{{ url_for('static', filename='qcm/icone-192.png') }}";
const A_PRECHARGER = [
  COQUILLE,
  MANIFESTE,
  ICONE,
  "{{ adresse_icones() }}",
  "{{ url_for('static', filename='css/app.css') }}",
  "{{ url_for('static', filename='qcm/qcm.css') }}",
  "{{ url_for('static', filename='qcm/hors_ligne.css') }}",
  "{{ url_for('static', filename='qcm/qcm_attente.js') }}",
  "{{ url_for('static', filename='qcm/fsrs.js') }}",
  "{{ url_for('static', filename='qcm/quiz.js') }}",
  "{{ url_for('static', filename='qcm/quiz_langue.js') }}",
  "{{ url_for('static', filename='qcm/langues_hors_ligne.js') }}",
  "{{ url_for('static', filename='qcm/qcm_hors_ligne.js') }}",
  "{{ url_for('static', filename='vendor/katex/katex.min.css') }}",
  "{{ url_for('static', filename='vendor/katex/katex.min.js') }}",
{%- for f in polices_katex %}
  "{{ url_for('static', filename='vendor/katex/fonts/' ~ f, v=None) }}",
{%- endfor %}
];
// Coquille remplacée dès qu'un de ses fichiers change (même sans nouvelle version de Hangar).
const EMPREINTE = A_PRECHARGER.join("|").split("").reduce((h, c) => (h * 31 + c.charCodeAt(0)) >>> 0, 7).toString(36);
const CACHE_COQUILLE = "hangar-qcm-coquille-" + VERSION + "-" + EMPREINTE;
// Réponses de l'API hors ligne : jamais mises en cache (les données sont dans IndexedDB).
const API = COQUILLE;
const IMAGES = ["/qcm/fichiers/", "/fichiers/", "/qcm/mon-espace/images/"];
const DELAI_RESEAU_MS = 4000;

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE_COQUILLE);
    // Un à un : un fichier indisponible n'empêche pas d'installer le reste.
    await Promise.all(A_PRECHARGER.map(async (url) => {
      try {
        const r = await fetch(url, { credentials: "same-origin" });
        if (r.ok && !r.redirected) await cache.put(url, r);
      } catch (e) { /* hors ligne pendant l'installation */ }
    }));
    await self.skipWaiting();
  })());
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    for (const nom of await caches.keys()) {
      if (nom.startsWith("hangar-qcm-coquille-") && nom !== CACHE_COQUILLE) await caches.delete(nom);
    }
    await self.clients.claim();
  })());
});

// Réseau d'abord, puis le cache. Une connexion très faible ne bloque pas la page : passé le délai,
// la version en cache est servie si elle existe (sinon on attend le réseau). Les bonnes réponses
// sont gardées en cache pour la prochaine fois.
async function reseauPuisCache(request, nomCache, cleCache) {
  const cache = await caches.open(nomCache);
  const cle = cleCache || request;
  const reseau = fetch(request).then(async (reponse) => {
    // Une page de connexion (session expirée) ne remplace pas la page en cache.
    if (reponse.ok && !reponse.redirected && reponse.type !== "opaqueredirect") {
      await cache.put(cle, reponse.clone());
    }
    return reponse;
  });
  let delai;
  const attente = new Promise((res) => { delai = setTimeout(() => res(null), DELAI_RESEAU_MS); });
  try {
    const premier = await Promise.race([reseau, attente]);
    if (premier) return premier;
    const enCache = await caches.match(cle, { ignoreSearch: !!cleCache });
    return enCache || await reseau;
  } catch (e) {
    const enCache = await caches.match(cle, { ignoreSearch: !!cleCache });
    if (enCache) return enCache;
    throw e;
  } finally {
    clearTimeout(delai);
  }
}

// Fichier versionné (?v=…) : son contenu ne change jamais, la copie en cache suffit. Sans réseau ni copie
// exacte, une autre version du même fichier (page gardée avant une mise à jour).
async function cacheDabord(request, nomCache) {
  const enCache = await caches.match(request);
  if (enCache) return enCache;
  try {
    const reponse = await fetch(request);
    if (reponse.ok && !reponse.redirected && reponse.type !== "opaqueredirect") {
      const cache = await caches.open(nomCache);
      await cache.put(request, reponse.clone());
    }
    return reponse;
  } catch (e) {
    const autre = await caches.match(request, { ignoreSearch: true });
    if (autre) return autre;
    throw e;
  }
}

// Page hors ligne et autres pages des QCM : si le réseau ne répond pas, la page hors ligne en
// cache prend le relais.
async function navigation(request) {
  const url = new URL(request.url);
  if (url.pathname === COQUILLE) return reseauPuisCache(request, CACHE_COQUILLE, COQUILLE);
  try {
    return await fetch(request);
  } catch (e) {
    const enCache = await caches.match(COQUILLE, { ignoreSearch: true });
    if (enCache) return enCache;
    throw e;
  }
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (request.mode === "navigate") {
    event.respondWith(navigation(request));
    return;
  }
  if (url.pathname.startsWith(API) && url.pathname !== COQUILLE) return;
  if ((url.pathname.startsWith("/static/") || url.pathname === "/icones.svg") && url.searchParams.has("v")) {
    event.respondWith(cacheDabord(request, CACHE_COQUILLE));
  } else if (url.pathname.startsWith("/static/") || url.pathname === MANIFESTE) {
    event.respondWith(reseauPuisCache(request, CACHE_COQUILLE));
  } else if (IMAGES.some((p) => url.pathname.startsWith(p))) {
    event.respondWith(reseauPuisCache(request, CACHE_DONNEES));
  }
});

self.addEventListener("sync", (event) => {
  if (event.tag === "qcm-envoi") event.waitUntil(QcmAttente.envoyer());
});

// Notifications web (rappel quotidien des QCM, rappels de l'agenda) : un clic ouvre (ou ramène au
// premier plan) la page indiquée. Chaque rappel a son étiquette (tag) : un rappel de l'agenda ne
// remplace pas le rappel des QCM, ni un autre rappel de l'agenda.
self.addEventListener("push", (event) => {
  let d = {};
  try { d = event.data ? event.data.json() : {}; } catch (e) { d = { texte: event.data ? event.data.text() : "" }; }
  event.waitUntil(self.registration.showNotification(d.titre || "Hangar · révisions", {
    body: d.texte || "C'est l'heure de réviser un peu.",
    icon: ICONE,
    badge: ICONE,
    tag: d.tag || "rappel-quotidien",
    data: { url: d.url || "{{ url_for('qcm.aujourdhui') }}" },
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = new URL((event.notification.data && event.notification.data.url) || "/", self.location.origin).href;
  event.waitUntil((async () => {
    for (const c of await self.clients.matchAll({ type: "window", includeUncontrolled: true })) {
      if (c.url === url && "focus" in c) return c.focus();
    }
    return self.clients.openWindow(url);
  })());
});

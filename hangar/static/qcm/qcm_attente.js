// Stockage hors ligne des QCM (IndexedDB), partagé par la page hors ligne et le service worker
// (importScripts) — port de qcm_attente.js de Revision :
// - « sets » : sets téléchargés (questions pré-rendues, progression, quotas) ;
// - « attente » : réponses pas encore reçues par le serveur, avec leur date ;
// - « meta » : compte connecté, dernière synchronisation.
// envoyer() transmet la file d'attente au serveur, qui rejoue les réponses dans l'ordre
// chronologique ; les réponses traitées sont retirées de la file.
// Dans l'application Android, les requêtes passent par le pont natif (global.QcmReseau, même
// interface que fetch) qui ajoute le jeton d'appareil.
(function (global) {
  const BASE = "hangar-qcm-hors-ligne";
  const VERSION = 1;
  const URL_CATALOGUE = "/qcm/hors-ligne/catalogue/";
  const URL_SYNCHRO = "/qcm/hors-ligne/synchro/";
  const PAQUET = 500; // réponses par requête
  let base = null;

  const requete = (url, options) => (global.QcmReseau ? global.QcmReseau.fetch(url, options) : fetch(url, options));

  function ouvrir() {
    if (!base) {
      base = new Promise((res, rej) => {
        const req = indexedDB.open(BASE, VERSION);
        req.onupgradeneeded = () => {
          const db = req.result;
          if (!db.objectStoreNames.contains("sets")) db.createObjectStore("sets", { keyPath: "id" });
          if (!db.objectStoreNames.contains("attente")) db.createObjectStore("attente", { keyPath: "uid" });
          if (!db.objectStoreNames.contains("meta")) db.createObjectStore("meta");
        };
        req.onsuccess = () => res(req.result);
        req.onerror = () => { base = null; rej(req.error); };
      });
    }
    return base;
  }

  // Exécute fn(store) dans une transaction et attend qu'elle soit terminée.
  async function transaction(nom, mode, fn) {
    const db = await ouvrir();
    return new Promise((res, rej) => {
      const tx = db.transaction(nom, mode);
      let resultat;
      const r = fn(tx.objectStore(nom));
      if (r && "onsuccess" in r) r.onsuccess = () => { resultat = r.result; };
      tx.oncomplete = () => res(resultat);
      tx.onerror = tx.onabort = () => rej(tx.error);
    });
  }

  const tous = (nom) => transaction(nom, "readonly", (s) => s.getAll());
  const lire = (nom, cle) => transaction(nom, "readonly", (s) => s.get(cle));
  const ecrire = (nom, valeur, cle) => transaction(nom, "readwrite", (s) => (cle === undefined ? s.put(valeur) : s.put(valeur, cle)));
  const effacer = (nom, cle) => transaction(nom, "readwrite", (s) => s.delete(cle));

  function uid() {
    if (global.crypto && crypto.randomUUID) return crypto.randomUUID();
    return Date.now().toString(36) + Math.random().toString(36).slice(2);
  }

  // Met une réponse en file d'attente ; {set_id, question_id, correct, note, duree_ms, date,
  // joueur}. Demande au service worker de l'envoyer dès que la connexion revient, même si la
  // page est fermée (Background Sync, Android).
  async function ajouter(reponse) {
    const r = { uid: uid(), date: new Date().toISOString(), ...reponse };
    await ecrire("attente", r);
    if (global.navigator && navigator.serviceWorker) {
      navigator.serviceWorker.ready
        .then((reg) => reg.sync && reg.sync.register("qcm-envoi"))
        .catch(() => {});
    }
    return r;
  }

  // Erreur levée quand la session a expiré : il faut se reconnecter.
  class Deconnecte extends Error {}

  // Réponse JSON de l'API ; Deconnecte si la session a expiré (le serveur renvoie vers la page de
  // connexion) ou si le jeton d'appareil est révoqué (401).
  async function lireJson(r) {
    const type = r.headers.get("Content-Type") || "";
    if (r.redirected || r.status === 401 || !type.includes("json")) throw new Deconnecte("Session expirée");
    if (!r.ok) throw new Error("Serveur indisponible (" + r.status + ")");
    return r.json();
  }

  // Compte connecté et jeton CSRF.
  async function catalogue() {
    return lireJson(await requete(URL_CATALOGUE, { credentials: "same-origin", cache: "no-store" }));
  }

  let envoiEnCours = null;

  // Envoie les réponses en attente du compte connecté. Renvoie {envoyees, enregistrees, restantes,
  // autres} ; `autres` : réponses d'un autre compte, gardées jusqu'à ce qu'il se reconnecte.
  function envoyer(infos) {
    if (!envoiEnCours) {
      envoiEnCours = (async () => {
        const attente = await tous("attente");
        if (!attente.length) return { envoyees: 0, enregistrees: 0, restantes: 0, autres: 0 };
        const cat = infos || await catalogue();
        const miennes = attente.filter((r) => !r.joueur || r.joueur === cat.joueur);
        let envoyees = 0, enregistrees = 0;
        for (let i = 0; i < miennes.length; i += PAQUET) {
          const paquet = miennes.slice(i, i + PAQUET);
          const r = await requete(URL_SYNCHRO, {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json", "X-CSRF-Token": cat.csrf, "X-Requested-With": "fetch" },
            body: JSON.stringify({ reponses: paquet }),
          });
          if (r.redirected || r.status === 401) throw new Deconnecte("Session expirée");
          if (!r.ok) throw new Error("Synchronisation refusée (" + r.status + ")");
          const res = await r.json();
          await transaction("attente", "readwrite", (s) => { res.traitees.forEach((u) => s.delete(u)); });
          envoyees += res.traitees.length;
          enregistrees += res.enregistrees;
        }
        return { envoyees, enregistrees, restantes: miennes.length - envoyees, autres: attente.length - miennes.length };
      })().finally(() => { envoiEnCours = null; });
    }
    return envoiEnCours;
  }

  global.QcmAttente = {
    CACHE: "hangar-qcm-donnees-v1", // images des questions (la coquille a son propre cache, par version)
    Deconnecte, ajouter, envoyer, catalogue, requete, lireJson,
    tous, lire, ecrire, effacer,
  };
})(self);

// Ce que Hangar garde sur cet appareil (navigateur) : pages du wiki lues ou gardées hors ligne (cache du
// service worker), QCM téléchargés et réponses en attente (IndexedDB), brouillons de l'éditeur.
// - « Se déconnecter » avec « Effacer aussi les pages gardées sur cet appareil » coché : tout est effacé,
//   après avoir essayé d'envoyer les réponses de QCM en attente (et prévenu s'il en reste). Les fichiers
//   de l'application (scripts, styles, icônes) restent : ils ne contiennent rien de personnel.
// - Un autre compte se connecte sur ce navigateur : les pages du wiki gardées pour le compte précédent
//   (son menu, ses favoris, ses échanges avec le tuteur) sont effacées.
// Pas d'en-tête Clear-Site-Data : il effacerait aussi les réglages et, sans prévenir, les réponses en attente.
(function () {
  "use strict";
  const H = window.HANGAR || {};
  const BASE_QCM = "hangar-qcm-hors-ligne";
  const CLE_COMPTE = "hangar.compte";
  // Entrées du cache qui ne contiennent rien de personnel (fichiers de l'application).
  const APPLICATION = (p) => p.startsWith("/static/") || p === "/icones.svg" || p === "/hors-connexion/"
    || p === "/manifest.webmanifest" || p === "/qcm/manifest.webmanifest";

  const cles = () => { try { return Object.keys(localStorage); } catch (e) { return []; } };
  const lire = (cle) => { try { return localStorage.getItem(cle); } catch (e) { return null; } };
  const ecrire = (cle, valeur) => { try { localStorage.setItem(cle, valeur); } catch (e) { /* stockage bloqué */ } };
  const retirer = (cle) => { try { localStorage.removeItem(cle); } catch (e) { /* stockage bloqué */ } };

  async function viderCaches(prefixe) {
    if (!window.caches) return;
    for (const nom of await caches.keys()) {
      if (!nom.startsWith(prefixe)) continue;
      const cache = await caches.open(nom);
      for (const req of await cache.keys()) {
        if (!APPLICATION(new URL(req.url).pathname)) await cache.delete(req);
      }
    }
  }

  // Pages du wiki gardées (et la liste des dossiers « ✓ Hors ligne » du sommaire, devenue fausse).
  async function effacerWiki() {
    await viderCaches("hangar-wiki");
    retirer("wiki-dossiers-hors-ligne");
  }

  // Réponses de QCM pas encore reçues par le serveur : file de l'appli hors ligne (IndexedDB) et réponses
  // des sessions en ligne gardées faute de connexion (localStorage).
  function compterAttente() {
    let n = 0;
    cles().filter((c) => c.startsWith("hangar.qcm.attente.")).forEach((c) => {
      try { n += (JSON.parse(lire(c) || "[]") || []).length; } catch (e) { /* illisible */ }
    });
    return new Promise((resolve) => {
      if (!window.indexedDB) { resolve(n); return; }
      let req;
      try { req = indexedDB.open(BASE_QCM); } catch (e) { resolve(n); return; }
      // Base absente : on ne la crée pas (l'appli hors ligne la créera avec ses tables).
      req.onupgradeneeded = () => req.transaction.abort();
      req.onerror = () => resolve(n);
      req.onsuccess = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains("attente")) { db.close(); resolve(n); return; }
        const compte = db.transaction("attente").objectStore("attente").count();
        compte.onsuccess = () => { db.close(); resolve(n + compte.result); };
        compte.onerror = () => { db.close(); resolve(n); };
      };
    });
  }

  function chargerScript(src) {
    return new Promise((resolve, reject) => {
      if (window.QcmAttente) { resolve(); return; }
      const s = document.createElement("script");
      s.src = src;
      s.onload = resolve;
      s.onerror = reject;
      document.head.appendChild(s);
    });
  }

  function supprimerBase() {
    return new Promise((resolve) => {
      if (!window.indexedDB) { resolve(); return; }
      let req;
      try { req = indexedDB.deleteDatabase(BASE_QCM); } catch (e) { resolve(); return; }
      const fin = () => resolve();
      req.onsuccess = fin;
      req.onerror = fin;
      req.onblocked = fin; // ouverte dans un autre onglet : effacée dès sa fermeture
      setTimeout(fin, 3000);
    });
  }

  async function effacerTout() {
    await effacerWiki();
    await viderCaches("hangar-qcm");
    await supprimerBase();
    cles().filter((c) => c.startsWith("wiki-brouillon:") || c.startsWith("hangar.qcm.attente.")).forEach(retirer);
    retirer(CLE_COMPTE);
  }

  // -- Se déconnecter -------------------------------------------------------------------------------------
  document.addEventListener("submit", async (e) => {
    const form = e.target;
    if (!form.matches || !form.matches("form[data-deconnexion]") || form.dataset.envoi) return;
    const effacer = form.querySelector("[data-effacer-appareil]");
    if (!effacer || !effacer.checked) return;
    e.preventDefault();
    const bouton = form.querySelector("button");
    if (bouton) bouton.disabled = true;
    try {
      let attente = await compterAttente();
      if (attente && navigator.onLine && form.dataset.qcmAttente) {
        // Encore connecté : on essaie d'abord de les envoyer.
        try {
          await chargerScript(form.dataset.qcmAttente);
          await window.QcmAttente.envoyer();
        } catch (err) { /* serveur injoignable : on prévient plus bas */ }
        attente = await compterAttente();
      }
      if (attente && !confirm(
        `${attente} réponse${attente > 1 ? "s" : ""} de QCM ${attente > 1 ? "n'ont" : "n'a"} pas encore été `
        + `envoyée${attente > 1 ? "s" : ""} au serveur. Si tu effaces les données de cet appareil, `
        + `${attente > 1 ? "elles seront perdues" : "elle sera perdue"}.\n\n`
        + "OK : te déconnecter et tout effacer quand même.\n"
        + "Annuler : rester connecté (ouvre les QCM quand la connexion revient pour les envoyer).")) {
        if (bouton) bouton.disabled = false;
        return;
      }
      await effacerTout();
    } catch (err) { /* effacement partiel : la déconnexion passe quand même */ }
    form.dataset.envoi = "1";
    form.submit();
  });

  // -- Changement de compte sur ce navigateur ------------------------------------------------------------
  if (H.me && H.me.id) {
    const moi = String(H.me.id);
    const avant = lire(CLE_COMPTE);
    if (avant !== moi) {
      const fin = () => ecrire(CLE_COMPTE, moi);
      if (avant) effacerWiki().catch(() => {}).finally(fin);
      else fin();
    }
  }
})();

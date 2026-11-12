// Rappel quotidien par notification web (page « Aujourd'hui » des QCM) : abonnement de cet appareil
// au service de notifications du navigateur, avec l'heure choisie. Port de rappel_web.js de
// Revision ; le service worker des QCM (/qcm/sw.js) affiche les notifications.
(function () {
  "use strict";
  const bloc = document.getElementById("rappel-web");
  if (!bloc || bloc.hidden) return;
  const etat = bloc.querySelector("[data-rappel-etat]");
  const controles = bloc.querySelector("[data-rappel-controles]");
  const heure = bloc.querySelector("[data-rappel-heure]");
  const activer = bloc.querySelector("[data-rappel-activer]");
  const tester = bloc.querySelector("[data-rappel-tester]");
  const desactiver = bloc.querySelector("[data-rappel-desactiver]");
  const iphone = /iPhone|iPad/.test(navigator.userAgent);
  const installe = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone;
  const csrf = (window.HANGAR && window.HANGAR.csrf) || "";

  if (!window.isSecureContext) {
    etat.textContent = "Les notifications demandent une adresse sécurisée (HTTPS, ou Hangar ouvert sur cet ordinateur).";
    return;
  }
  if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
    etat.textContent = iphone && !installe
      ? "Sur iPhone, ajoute d'abord Hangar à l'écran d'accueil (Partager, puis « Sur l'écran d'accueil ») et ouvre-le depuis l'icône : le rappel sera alors possible."
      : "Ce navigateur ne permet pas les notifications web.";
    return;
  }

  const post = (url, corps) => fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf, "X-Requested-With": "fetch" },
    body: JSON.stringify(corps),
  }).then(async (r) => {
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || "erreur " + r.status);
    return d;
  });

  function base64EnOctets(b64) {
    const s = atob((b64 + "=".repeat((4 - (b64.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/"));
    return Uint8Array.from(s, (c) => c.charCodeAt(0));
  }

  let enregistrement = null, abonnement = null, cle = null, connus = {};

  // S'abonner demande un service worker actif : à la première visite, on attend la fin de son installation.
  function pret(reg) {
    const w = reg.installing || reg.waiting;
    if (reg.active || !w) return Promise.resolve(reg);
    return new Promise((ok) => w.addEventListener("statechange", () => { if (w.state === "activated") ok(reg); }));
  }

  function afficher(message) {
    controles.hidden = false;
    // Un appareil abonné aux seuls rappels de l'agenda n'a pas le rappel des QCM
    const actif = !!(abonnement && connus[abonnement.endpoint] && connus[abonnement.endpoint].quotidien !== false);
    activer.textContent = actif ? "Changer l'heure" : "Activer";
    tester.hidden = !actif;
    desactiver.hidden = !actif;
    bloc.classList.toggle("actif", actif);
    if (actif) {
      const h = connus[abonnement.endpoint];
      heure.value = String(h.heure).padStart(2, "0") + ":" + String(h.minute).padStart(2, "0");
    }
    etat.textContent = message || (actif
      ? `Rappel activé sur cet appareil, chaque jour à ${heure.value} (rien si tout est déjà révisé).`
      : Notification.permission === "denied"
        ? "Les notifications sont bloquées pour Hangar : autorise-les dans les réglages du navigateur."
        : "Notification sur cet appareil à l'heure choisie, avec ce qu'il reste à réviser et ta série de jours.");
  }

  async function demarrer() {
    try {
      // L'enregistrement renvoyé par register() : c'est le service worker des QCM qui affiche les
      // notifications (navigator.serviceWorker.ready pourrait donner un autre service worker).
      enregistrement = await navigator.serviceWorker.register(bloc.dataset.swUrl, { scope: bloc.dataset.scope });
      await pret(enregistrement);
      abonnement = await enregistrement.pushManager.getSubscription();
      const r = await fetch(bloc.dataset.cleUrl, { credentials: "same-origin" });
      const d = await r.json();
      cle = d.cle;
      connus = d.abonnements || {};
      afficher();
    } catch (e) {
      etat.textContent = "Notifications indisponibles : " + e.message;
    }
  }

  activer.addEventListener("click", async () => {
    activer.disabled = true;
    try {
      if (Notification.permission !== "granted" && (await Notification.requestPermission()) !== "granted") {
        afficher("Permission refusée : le rappel ne peut pas s'afficher.");
        return;
      }
      if (!abonnement) {
        abonnement = await enregistrement.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: base64EnOctets(cle) });
      }
      const [h, m] = (heure.value || "19:00").split(":").map(Number);
      await post(bloc.dataset.abonnerUrl, { subscription: abonnement.toJSON(), heure: h, minute: m });
      connus[abonnement.endpoint] = Object.assign(connus[abonnement.endpoint] || { agenda: false }, { heure: h, minute: m, quotidien: true });
      afficher();
    } catch (e) {
      afficher("Activation impossible : " + e.message);
    } finally {
      activer.disabled = false;
    }
  });

  tester.addEventListener("click", async () => {
    tester.disabled = true;
    try {
      await post(bloc.dataset.testerUrl, { endpoint: abonnement.endpoint });
      afficher("Notification de test envoyée : elle arrive dans quelques secondes.");
    } catch (e) {
      afficher("Test impossible : " + e.message);
    } finally {
      tester.disabled = false;
    }
  });

  desactiver.addEventListener("click", async () => {
    try {
      const r = await post(bloc.dataset.desabonnerUrl, { endpoint: abonnement.endpoint, quoi: "qcm" });
      if (r.garde) {
        // L'appareil reçoit encore les rappels de l'agenda : on garde l'abonnement du navigateur
        connus[abonnement.endpoint].quotidien = false;
      } else {
        delete connus[abonnement.endpoint];
        await abonnement.unsubscribe();
        abonnement = null;
      }
      afficher("Rappel désactivé sur cet appareil.");
    } catch (e) {
      afficher("Désactivation impossible : " + e.message);
    }
  });

  demarrer();
})();

// Rappels de l'agenda en notifications du système (« Rappels et e-mails ») : abonne cet appareil aux
// notifications web, comme le rappel quotidien des QCM (rappel_web.js), mais pour les rappels de
// l'agenda seulement. Le service worker des QCM (/qcm/sw.js) affiche les notifications : on garde
// l'enregistrement renvoyé par register() (sur /agenda/…, navigator.serviceWorker.ready donnerait le
// service worker du wiki, qui ne sait pas afficher une notification, ou attendrait sans fin).
(function () {
  "use strict";
  const bloc = document.getElementById("rappel-agenda");
  if (!bloc || bloc.hidden) return;
  const etat = bloc.querySelector("[data-push-etat]");
  const controles = bloc.querySelector("[data-push-controles]");
  const activer = bloc.querySelector("[data-push-activer]");
  const tester = bloc.querySelector("[data-push-tester]");
  const desactiver = bloc.querySelector("[data-push-desactiver]");
  const csrf = (window.HANGAR && window.HANGAR.csrf) || "";
  const iphone = /iPhone|iPad/.test(navigator.userAgent);

  if (!window.isSecureContext) {
    etat.textContent = "Les notifications demandent une adresse sécurisée (HTTPS, ou Hangar ouvert sur cet ordinateur). Ici, les rappels arrivent par e-mail ou dans l'application.";
    return;
  }
  if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
    etat.textContent = iphone
      ? "Sur iPhone, ajoute d'abord Hangar à l'écran d'accueil (Partager, puis « Sur l'écran d'accueil ») et ouvre-le depuis l'icône."
      : "Ce navigateur ne permet pas les notifications.";
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

  function pret(reg) {
    const w = reg.installing || reg.waiting;
    if (reg.active || !w) return Promise.resolve(reg);
    return new Promise((ok) => w.addEventListener("statechange", () => { if (w.state === "activated") ok(reg); }));
  }

  let enregistrement = null, abonnement = null, cle = null, connus = {};

  function afficher(message) {
    controles.hidden = false;
    const info = abonnement && connus[abonnement.endpoint];
    const actif = !!(info && info.agenda);
    activer.hidden = actif;
    tester.hidden = !actif;
    desactiver.hidden = !actif;
    etat.textContent = message || (actif
      ? "Cet appareil reçoit les rappels de l'agenda en notification."
      : Notification.permission === "denied"
        ? "Les notifications sont bloquées pour Hangar : autorise-les dans les réglages du navigateur."
        : "Cet appareil ne reçoit pas encore les rappels en notification.");
  }

  async function demarrer() {
    try {
      enregistrement = await pret(await navigator.serviceWorker.register(bloc.dataset.swUrl, { scope: bloc.dataset.scope }));
      abonnement = await enregistrement.pushManager.getSubscription();
      const d = await (await fetch(bloc.dataset.cleUrl, { credentials: "same-origin" })).json();
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
        afficher("Permission refusée : les notifications ne peuvent pas s'afficher.");
        return;
      }
      if (!abonnement) {
        abonnement = await enregistrement.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: base64EnOctets(cle) });
      }
      const r = await post(bloc.dataset.abonnerUrl, { subscription: abonnement.toJSON(), quoi: "agenda" });
      connus[abonnement.endpoint] = Object.assign(connus[abonnement.endpoint] || {}, { agenda: true, quotidien: r.quotidien });
      afficher("C'est fait : choisis « notification sur mes appareils » comme moyen de rappel.");
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
      const r = await post(bloc.dataset.desabonnerUrl, { endpoint: abonnement.endpoint, quoi: "agenda" });
      if (r.garde) {  // le rappel quotidien des QCM reste actif sur cet appareil
        connus[abonnement.endpoint].agenda = false;
      } else {
        delete connus[abonnement.endpoint];
        await abonnement.unsubscribe();
        abonnement = null;
      }
      afficher("Cet appareil ne reçoit plus les rappels de l'agenda.");
    } catch (e) {
      afficher("Désactivation impossible : " + e.message);
    }
  });

  demarrer();
})();

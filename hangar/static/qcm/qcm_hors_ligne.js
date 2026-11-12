// QCM hors ligne : sets téléchargés sur l'appareil, révision sans connexion et synchronisation
// avec le serveur quand la connexion revient. Les paquets du module Langues (FSRS) passent par
// langues_hors_ligne.js, même file d'attente. Port de qcm_hors_ligne.js de Revision.
//
// La progression (FSRS, et boîte de Leitner comme indicateur) est recalculée sur l'appareil avec
// les mêmes règles que models.py et quiz.py (fsrs.js), pour savoir quelles questions sont à
// réviser sans le serveur. Le serveur reste la référence : à chaque synchronisation, les réponses
// en attente lui sont envoyées puis la progression est re-téléchargée.
//
// En plus de Revision : « #telecharger=id,id » dans l'adresse télécharge ces sets tout de suite
// (boutons « ⤓ Hors ligne » de la page des QCM), et les réponses des sessions en ligne gardées sur
// l'appareil faute de connexion (quiz.js, localStorage) rejoignent la même file d'attente.
(function () {
  const A = window.QcmAttente;
  const app = document.getElementById("hl");
  if (!app || !A) return;
  const $ = (id) => document.getElementById(id);
  const L = window.LanguesHorsLigne || null;
  const F = window.FSRS;
  if (!F) return;
  const estLangue = (s) => s.type === "langue";
  // Page gardée par le navigateur pour s'ouvrir sans réseau ? Il faut une adresse sécurisée (HTTPS, ou
  // cet ordinateur) pour le service worker et le cache. Sur http://192.168…, les sets se téléchargent
  // bien (IndexedDB), mais la page elle-même ne s'ouvrira pas sans réseau. L'application Android a sa
  // propre page embarquée (pas de service worker dans sa WebView : on ne teste pas serviceWorker).
  const pageGardee = !!window.QcmReseau || window.isSecureContext !== false;
  const SANS_RESEAU = pageGardee ? "disponibles sans réseau" : "gardés sur l'appareil";

  // --- Révision espacée : mêmes règles que models.py / quiz.py ---
  const BOITE_MAX = 5;
  const NOTES = ["a_revoir", "difficile", "bien", "facile"];

  function nouvelleBoite(boite, note) {
    if (note === "a_revoir") return 0;
    if (note === "difficile") return Math.max(1, boite);
    return Math.min(BOITE_MAX, boite + (note === "facile" ? 2 : 1));
  }

  const estDue = (st, now) => F.estDuQcm(st, now);

  function jour(d) {
    const x = new Date(d);
    return x.getFullYear() + "-" + (x.getMonth() + 1) + "-" + x.getDate();
  }

  // Applique une réponse à la progression locale d'un set (comme QuizProgress.record) :
  // statistiques, boîte de Leitner et état FSRS, qui fixe la prochaine échéance.
  function appliquer(set, rep) {
    const st = set.progression[rep.question_id] || { box: 0, seen: 0, correct: 0, last_review: null, premiere_revue: null,
      stabilite: null, difficulte: null, echeance: null, decouverte: null, oublis: 0, last_correct: null };
    if (st.last_review && Date.parse(rep.date) <= Date.parse(st.last_review)) return;
    const note = NOTES.includes(rep.note) ? rep.note : (rep.correct ? "bien" : "a_revoir");
    const noteFsrs = NOTES.indexOf(note) + 1;
    const now = Date.parse(rep.date);
    const dejaAppris = st.stabilite != null || st.seen > 0;
    const etat = F.etatSuivantQcm(st, noteFsrs, now);
    st.seen++;
    if (note !== "a_revoir") st.correct++;
    st.box = nouvelleBoite(st.box, note);
    st.last_correct = note !== "a_revoir";
    st.last_review = rep.date;
    if (!st.premiere_revue) st.premiere_revue = rep.date;
    st.stabilite = Math.round(etat.stabilite * 1000) / 1000;
    st.difficulte = Math.round(etat.difficulte * 1000) / 1000;
    // Rétention visée comme sur le serveur : plus haute dans les 30 jours avant l'examen du set.
    st.echeance = new Date(now + F.delaiMs(etat, noteFsrs, F.retentionCible(set.date_objectif, now))).toISOString();
    if (!st.decouverte && !dejaAppris) st.decouverte = rep.date;
    if (dejaAppris && note === "a_revoir") st.oublis = (st.oublis || 0) + 1;
    set.progression[rep.question_id] = st;
  }

  // Questions à réviser aujourd'hui (comme services.file_du_jour) : réapprentissage, révisions
  // (les plus menacées d'oubli, puis plus anciennes), nouvelles ; avec les quotas.
  function fileDuJour(set, now) {
    const aujourdhui = jour(now);
    const reapprendre = [], vues = [], nouvelles = [];
    for (const q of set.questions) {
      const st = set.progression[q.id];
      if (!st) nouvelles.push(q);
      else if (estDue(st, now)) {
        if (st.last_review && jour(st.last_review) === aujourdhui) reapprendre.push(q);
        else vues.push(q);
      }
    }
    const cle = (q) => set.progression[q.id];
    vues.sort((a, b) => (F.urgence(cle(a), now) - F.urgence(cle(b), now)) || (Date.parse(cle(a).last_review || 0) - Date.parse(cle(b).last_review || 0)));
    let dejaN = 0, dejaR = 0;
    for (const st of Object.values(set.progression)) {
      if (st.premiere_revue && jour(st.premiere_revue) === aujourdhui) dejaN++;
      else if (st.last_review && jour(st.last_review) === aujourdhui) dejaR++;
    }
    const { nouvelles_jour: qn, revisions_jour: qr } = set.quotas || {};
    const r = qr == null ? vues : vues.slice(0, Math.max(0, qr - dejaR));
    const n = qn == null ? nouvelles : nouvelles.slice(0, Math.max(0, qn - dejaN));
    return reapprendre.concat(r, n);
  }

  // --- État ---
  const etat = { sets: new Map(), meta: {}, catalogue: null, attente: 0 };

  async function charger() {
    etat.sets = new Map((await A.tous("sets")).map((s) => [s.id, s]));
    etat.meta = (await A.lire("meta", "meta")) || {};
    etat.attente = (await A.tous("attente")).length;
  }

  const sauverMeta = () => A.ecrire("meta", etat.meta, "meta");

  // --- Affichage ---
  function el(tag, cls, texte) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (texte !== undefined) e.textContent = texte;
    return e;
  }

  // Lien-bouton ; `cls` : "primary" pour le bouton principal (comme dans Revision).
  function lien(texte, href, cls) {
    const a = el("a", "btn btn-sm" + (cls === "primary" ? " btn-primary" : cls ? " " + cls : ""), texte);
    a.href = href;
    return a;
  }

  const pluriel = (n, mot) => n + " " + mot + (n > 1 ? "s" : "");

  function pastille(n, titre) {
    const p = el("span", "qcm-due" + (n ? "" : " zero"), n ? String(n) : "✓");
    if (titre) p.title = titre;
    return p;
  }

  function afficherReseau() {
    const enLigne = navigator.onLine;
    $("hl-reseau").textContent = enLigne ? "En ligne" : "Hors ligne";
    $("hl-reseau").classList.toggle("hors-ligne", !enLigne);
    document.querySelectorAll("[data-en-ligne]").forEach((e) => { e.hidden = !enLigne; });
    $("hl-synchroniser").disabled = !enLigne;
  }

  function afficherEtat(message) {
    const parts = [pluriel(etat.sets.size, "set") + " sur l'appareil"];
    parts.push(etat.attente ? pluriel(etat.attente, "réponse") + " à synchroniser" : "tout est synchronisé");
    if (etat.meta.derniereSynchro) {
      parts.push("dernière synchro " + new Date(etat.meta.derniereSynchro).toLocaleString("fr-FR", { dateStyle: "short", timeStyle: "short" }));
    }
    $("hl-etat").textContent = message || parts.join(" · ");
  }

  function alerte(texte, lienConnexion) {
    const zone = $("hl-alerte");
    zone.hidden = !texte;
    const titre = $("hl-alerte-texte");
    titre.textContent = texte || "";
    if (lienConnexion) {
      titre.append(" ");
      const a = el("a", "", "Se reconnecter");
      a.href = app.dataset.loginUrl;
      titre.append(a);
    }
  }

  function parCategorie(sets) {
    const cats = new Map();
    for (const s of sets) {
      const nom = estLangue(s) ? "Langues · " + s.langue + (s.dossier ? " · " + s.dossier : "")
        : s.langue ? s.langue + " · " + s.categorie : s.categorie;
      if (!cats.has(nom)) cats.set(nom, []);
      cats.get(nom).push(s);
    }
    return [...cats.entries()].sort((a, b) => a[0].localeCompare(b[0], "fr", { numeric: true }));
  }

  function categorieRepliable(nom, meta) {
    const details = el("details", "qcm-cat");
    details.open = true;
    const summary = el("summary");
    summary.append(el("span", "hl-chev", "›"), el("span", "qcm-cat-name", nom));
    const m = el("span", "qcm-cat-meta");
    m.append(...meta);
    summary.append(m);
    const corps = el("div", "qcm-cat-body");
    details.append(summary, corps);
    return { details, corps };
  }

  function afficherTelecharges() {
    const zone = $("hl-telecharges");
    zone.replaceChildren();
    const now = Date.now();
    if (!etat.sets.size) {
      const vide = el("div", "empty hl-vide");
      vide.append(el("h3", "", "Aucun set sur cet appareil"),
        el("p", "", "Ouvre « Télécharger des sets » ci-dessous (connexion nécessaire) : les sets choisis restent " + SANS_RESEAU + "."));
      zone.append(vide);
      return;
    }
    const qcm = [...etat.sets.values()].filter((s) => !estLangue(s));
    if (!qcm.length) {
      if (L) L.afficher(zone, el, lien, pluriel);
      return;
    }
    let total = 0;
    const sections = [];
    for (const [nom, sets] of parCategorie(qcm)) {
      let dues = 0;
      const ul = el("ul", "qcm-sets");
      for (const s of sets.sort((a, b) => a.titre.localeCompare(b.titre, "fr", { numeric: true }))) {
        const n = fileDuJour(s, now).length;
        dues += n;
        const li = el("li", "qcm-set");
        const head = el("div", "qcm-set-head");
        head.append(el("div", "qcm-set-title", s.titre), pastille(n, pluriel(n, "question") + " à réviser"));
        li.append(head);
        if (s.description) li.append(el("p", "qcm-set-desc", s.description));
        const vues = s.questions.filter((q) => s.progression[q.id]).length;
        li.append(el("p", "qcm-set-status", pluriel(n, "question") + " à réviser · " + vues + " / " + s.questions.length + " rencontrées"));
        const boutons = el("div", "qcm-set-buttons");
        const id = encodeURIComponent(s.id);
        if (n) boutons.append(lien("Réviser (" + n + ")", "#jouer?mode=review&set=" + id, "primary"));
        boutons.append(lien("Tout", "#jouer?mode=all&set=" + id), lien("Intensif", "#jouer?mode=intensif&set=" + id));
        const retirer = el("button", "btn btn-sm btn-ghost hl-retirer", "Retirer");
        retirer.type = "button";
        retirer.title = "Retirer ce set de l'appareil (les réponses non synchronisées sont gardées)";
        retirer.addEventListener("click", () => retirerSet(s));
        boutons.append(retirer);
        li.append(boutons);
        ul.append(li);
      }
      total += dues;
      const { details, corps } = categorieRepliable(nom, [el("span", "", pluriel(sets.length, "set")), pastille(dues)]);
      corps.append(ul);
      sections.push(details);
    }
    const actions = el("div", "hl-actions");
    actions.append(lien("Révision intelligente (" + total + ")", "#jouer?revision=1", "primary"));
    const h = el("div", "hl-section-h");
    h.append(el("h2", "", "QCM"), el("span", "count", pluriel(qcm.length, "set")));
    zone.append(h, actions, ...sections);
    if (L) L.afficher(zone, el, lien, pluriel);
  }

  function afficherCatalogue() {
    const liste = $("hl-liste");
    liste.replaceChildren();
    if (!etat.catalogue) {
      $("hl-catalogue-meta").textContent = navigator.onLine ? "" : "connexion nécessaire";
      liste.append(el("p", "muted", navigator.onLine ? "Chargement de la liste…" : "La liste des sets s'affiche quand l'appareil est connecté."));
      return;
    }
    $("hl-catalogue-meta").textContent = pluriel(etat.catalogue.length, "set");
    for (const [nom, sets] of parCategorie(etat.catalogue)) {
      if (sets.some(estLangue) && !L) continue; // page sans module Langues
      const bloc = el("fieldset", "hl-categorie");
      const legende = el("legend");
      const toutes = el("input");
      toutes.type = "checkbox";
      const etiquette = el("label", "");
      etiquette.append(toutes, " " + nom);
      legende.append(etiquette);
      bloc.append(legende);
      const cases = [];
      for (const s of sets) {
        const label = el("label", "hl-choix");
        const c = el("input");
        c.type = "checkbox";
        c.value = s.id;
        c.checked = etat.sets.has(s.id);
        cases.push(c);
        label.append(c, el("span", "t", s.titre), el("span", "muted", "(" + s.total + ")"));
        if (etat.sets.has(s.id)) label.append(el("span", "badge hl-sur-appareil", "sur l'appareil"));
        bloc.append(label);
      }
      toutes.checked = cases.every((c) => c.checked);
      toutes.addEventListener("change", () => cases.forEach((c) => { c.checked = toutes.checked; }));
      liste.append(bloc);
    }
  }

  const enSession = () => !$("quiz").hidden || ($("quiz-langue") && !$("quiz-langue").hidden);

  function afficherAccueil(message) {
    if (window.QuizMoteur) window.QuizMoteur.arreter();
    if (window.LangueMoteur) window.LangueMoteur.arreter();
    $("quiz").hidden = true;
    if ($("quiz-langue")) $("quiz-langue").hidden = true;
    $("hl-accueil").hidden = false;
    afficherReseau();
    afficherEtat(message);
    afficherTelecharges();
    afficherCatalogue();
  }

  // --- Sessions de révision ---
  function melanger(liste) {
    for (let i = liste.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [liste[i], liste[j]] = [liste[j], liste[i]];
    }
    return liste;
  }

  function construireSession(params) {
    const now = Date.now();
    if (params.get("revision")) {
      // Comme la révision intelligente du serveur : dues d'abord, les plus menacées, plus anciennes.
      const paires = [];
      for (const s of etat.sets.values()) {
        if (estLangue(s)) continue;
        for (const q of fileDuJour(s, now)) {
          const st = s.progression[q.id];
          paires.push({ q, prio: st ? [estDue(st, now) ? 0 : 1, F.urgence(st, now), Date.parse(st.last_review || 0)] : [0, 2, 0] });
        }
      }
      paires.sort((a, b) => a.prio[0] - b.prio[0] || a.prio[1] - b.prio[1] || a.prio[2] - b.prio[2]);
      return { titre: "Révision intelligente", questions: paires.map((p) => p.q), intensif: false };
    }
    const set = etat.sets.get(params.get("set"));
    if (!set || estLangue(set)) return null;
    const mode = params.get("mode");
    const questions = mode === "review" ? fileDuJour(set, now) : set.questions.slice();
    return { titre: set.titre, questions: melanger(questions), intensif: mode === "intensif" };
  }

  async function enregistrer(q, correct, note, dureeMs) {
    const rep = await A.ajouter({ set_id: q.set_id, question_id: q.id, correct, note: note || null, duree_ms: dureeMs, joueur: etat.meta.joueur || "" });
    const set = etat.sets.get(q.set_id);
    if (set) {
      appliquer(set, rep);
      await A.ecrire("sets", set);
    }
    etat.attente++;
    if (navigator.onLine) envoyerDiscretement();
  }

  function lancer(params) {
    const session = construireSession(params);
    if (!session) { location.hash = ""; return; }
    $("hl-accueil").hidden = true;
    $("quiz").hidden = false;
    $("hl-intensif-info").hidden = !(session.intensif && session.questions.length);
    $("hl-titre-session").textContent = session.titre;
    document.title = session.titre + " · QCM hors ligne";
    const stage = $("quiz-stage");
    if (!session.questions.length) {
      $("quiz-progress").textContent = "";
      const vide = el("div", "empty");
      vide.append(el("h3", "", "Rien à réviser ici 🎉"), el("p", "", "Toutes les questions sont à jour."));
      const retour = el("div", "mt");
      retour.append(lien("Retour aux sets", "#", "primary"));
      vide.append(retour);
      stage.replaceChildren(vide);
      return;
    }
    window.QuizMoteur.demarrer({
      stage,
      questions: session.questions,
      intensif: session.intensif,
      backUrl: "#",
      messageFin: "Réponses gardées sur l'appareil, envoyées au serveur à la prochaine connexion.",
      enregistrer,
    });
  }

  // Session de langue (langues_hors_ligne.js), dans le moteur des langues (quiz_langue.js).
  function lancerLangue(params) {
    const root = $("quiz-langue");
    if (!root || !L || !window.LangueMoteur) { location.hash = ""; return; }
    if (window.QuizMoteur) window.QuizMoteur.arreter();
    $("hl-accueil").hidden = true;
    $("quiz").hidden = true;
    root.hidden = false;
    if (!L.lancer(params, root)) location.hash = "";
  }

  function router() {
    const hash = location.hash;
    if (hash.startsWith("#jouer?")) lancer(new URLSearchParams(hash.slice(7)));
    else if (hash.startsWith("#langue?")) lancerLangue(new URLSearchParams(hash.slice(8)));
    else {
      document.title = "QCM hors ligne · Hangar";
      afficherAccueil();
    }
  }

  // --- Synchronisation ---
  let envoiPrevu = null;
  function envoyerDiscretement() {
    clearTimeout(envoiPrevu);
    envoiPrevu = setTimeout(async () => {
      try {
        await A.envoyer();
        etat.attente = (await A.tous("attente")).length;
      } catch (e) { /* nouvel essai à la prochaine synchronisation */ }
    }, 1500);
  }

  // Réponses des sessions en ligne restées sur l'appareil (quiz.js, faute de connexion) : elles
  // rejoignent la file d'attente, avec leur date, pour être synchronisées ici.
  async function reprendreAttenteEnLigne(cat) {
    const cle = "hangar.qcm.attente." + cat.joueur_id;
    let liste = [];
    try { liste = JSON.parse(localStorage.getItem(cle) || "[]"); } catch (e) { return; }
    if (!Array.isArray(liste) || !liste.length) return;
    for (const r of liste) {
      if (r && r.set_id && r.question_id) await A.ajouter({ ...r, date: r.date || new Date().toISOString(), joueur: cat.joueur });
    }
    try { localStorage.removeItem(cle); } catch (e) { /* stockage bloqué */ }
  }

  // Télécharge des sets (questions, progression du serveur, quotas) ; les réponses encore en
  // attente sont ré-appliquées par-dessus la progression reçue. Un set renommé ou déplacé sur le
  // serveur revient sous son nouvel identifiant (data.renommes) ; un set momentanément introuvable
  // (data.indisponibles : fichier en cours de modification…) reste sur l'appareil.
  // Renvoie {retires, indisponibles} : titres des sets retirés, et de ceux gardés faute de mieux.
  async function telecharger(ids) {
    const attente = await A.tous("attente");
    const retires = [], indisponibles = [];
    for (let i = 0; i < ids.length; i += 40) {
      const params = new URLSearchParams();
      ids.slice(i, i + 40).forEach((id) => params.append("id", id));
      const data = await A.lireJson(await A.requete(app.dataset.setsUrl + "?" + params, { credentials: "same-origin", cache: "no-store" }));
      const renommes = data.renommes || {};
      for (const set of data.sets) {
        const siens = [set.id, ...Object.keys(renommes).filter((ancien) => renommes[ancien] === set.id)];
        const miennes = attente.filter((r) => siens.includes(r.set_id) && (!r.joueur || r.joueur === data.joueur))
          .sort((a, b) => Date.parse(a.date) - Date.parse(b.date));
        if (estLangue(set)) {
          if (!L) continue;
          miennes.forEach((r) => { if (r.exo_id) L.appliquer(set, r.exo_id, r.note, r.date); });
        } else {
          miennes.forEach((r) => appliquer(set, r));
          if (window.QcmReseau) await integrerImages(set);
        }
        set.telecharge = new Date().toISOString();
        await A.ecrire("sets", set);
        etat.sets.set(set.id, set);
        if (!estLangue(set) && !window.QcmReseau) mettreImagesEnCache(set);
      }
      for (const id of data.manquants) {
        const set = etat.sets.get(id);
        if (set && !renommes[id]) retires.push(set.titre);
        await A.effacer("sets", id);
        etat.sets.delete(id);
      }
      for (const id of data.indisponibles || []) {
        const set = etat.sets.get(id);
        if (set) indisponibles.push(set.titre);
      }
    }
    return { retires, indisponibles };
  }

  // Images des questions (servies par Hangar) gardées dans le cache du service worker.
  async function mettreImagesEnCache(set) {
    if (!window.caches) return;
    const html = set.questions.map((q) => [q.enonce, q.verso, q.explication, ...q.choix].join("")).join("");
    const doc = new DOMParser().parseFromString(html, "text/html");
    const urls = new Set();
    doc.querySelectorAll("img[src]").forEach((img) => {
      const url = new URL(img.getAttribute("src"), location.href);
      if (url.origin === location.origin) urls.add(url.href);
    });
    if (!urls.size) return;
    const cache = await caches.open(A.CACHE);
    for (const url of urls) {
      try { if (!(await cache.match(url))) await cache.add(url); } catch (e) { /* image indisponible */ }
    }
  }

  // Application Android : pas de service worker, les images du serveur sont copiées dans les
  // questions (data: URI) pour s'afficher sans connexion.
  async function integrerImages(set) {
    const images = new Map();
    const enDataUrl = (blob) => new Promise((res) => {
      const lecteur = new FileReader();
      lecteur.onload = () => res(lecteur.result);
      lecteur.onerror = () => res(null);
      lecteur.readAsDataURL(blob);
    });
    async function remplacer(html) {
      if (!html || !html.includes("<img")) return html;
      const doc = new DOMParser().parseFromString(html, "text/html");
      let change = false;
      for (const img of doc.querySelectorAll("img[src^='/']")) {
        const src = img.getAttribute("src");
        if (!images.has(src)) {
          images.set(src, A.requete(src).then((r) => (r.ok ? r.blob().then(enDataUrl) : null)).catch(() => null));
        }
        const data = await images.get(src);
        if (data) { img.setAttribute("src", data); change = true; }
      }
      return change ? doc.body.innerHTML : html;
    }
    for (const q of set.questions) {
      q.enonce = await remplacer(q.enonce);
      q.verso = await remplacer(q.verso);
      q.explication = await remplacer(q.explication);
      q.choix = await Promise.all(q.choix.map(remplacer));
    }
  }

  let synchroEnCours = null;
  function synchroniser() {
    if (!synchroEnCours) synchroEnCours = synchroniserMaintenant().finally(() => { synchroEnCours = null; });
    return synchroEnCours;
  }

  async function synchroniserMaintenant() {
    if (!navigator.onLine) return;
    afficherEtat("Synchronisation…");
    let message;
    try {
      // Si un autre compte s'est connecté, le re-téléchargement des sets plus bas remplace la
      // progression de l'appareil par la sienne.
      const cat = await A.catalogue();
      etat.meta.joueur = cat.joueur;
      etat.catalogue = cat.sets.concat(cat.langues || []);
      await reprendreAttenteEnLigne(cat);
      const envoi = await A.envoyer(cat);
      const { retires, indisponibles } = etat.sets.size ? await telecharger([...etat.sets.keys()]) : { retires: [], indisponibles: [] };
      etat.meta.derniereSynchro = new Date().toISOString();
      await sauverMeta();
      etat.attente = (await A.tous("attente")).length;
      const avis = [];
      if (retires.length) avis.push("Retirés de l'appareil (supprimés du serveur) : " + retires.join(", ") + ".");
      if (indisponibles.length) avis.push("Introuvables sur le serveur pour l'instant, gardés sur l'appareil : " + indisponibles.join(", ") + ".");
      // Réponses non acquittées par le serveur : leur set est momentanément introuvable (fichier renommé,
      // déplacé ou en cours de modification) ; elles restent dans la file et repartiront plus tard.
      if (envoi.restantes) {
        const plus = envoi.restantes > 1;
        avis.push(pluriel(envoi.restantes, "réponse") + " gardée" + (plus ? "s" : "") + " sur l'appareil : "
          + (plus ? "leur" : "son") + " set est introuvable sur le serveur pour l'instant (fichier renommé, déplacé ou en cours de modification). "
          + (plus ? "Elles partiront" : "Elle partira") + " dès qu'il sera retrouvé ou rattaché (page des statistiques).");
      }
      if (envoi.autres) avis.push(pluriel(envoi.autres, "réponse") + " d'un autre compte en attente : elles seront envoyées quand ce compte se reconnectera.");
      alerte(avis.join(" "));
      if (envoi.envoyees) message = pluriel(envoi.envoyees, "réponse") + " synchronisée" + (envoi.envoyees > 1 ? "s" : "") + ".";
    } catch (e) {
      if (e instanceof A.Deconnecte) alerte("Session expirée : tes réponses restent sur l'appareil.", true);
      else alerte("Synchronisation impossible : " + e.message + ". Tes réponses restent sur l'appareil.");
    }
    if (!enSession()) afficherAccueil(message);
    if (message) setTimeout(() => { if (!enSession()) afficherEtat(); }, 4000);
  }

  async function telechargerIds(ids) {
    const bouton = $("hl-telecharger");
    bouton.disabled = true;
    afficherEtat("Téléchargement de " + pluriel(ids.length, "set") + "…");
    let ok = false;
    try {
      // Demande au navigateur de ne pas effacer les données en cas de manque de place.
      if (navigator.storage && navigator.storage.persist) navigator.storage.persist().catch(() => {});
      const cat = await A.catalogue();
      etat.meta.joueur = cat.joueur;
      etat.catalogue = cat.sets.concat(cat.langues || []);
      await A.envoyer(cat);
      await telecharger(ids);
      etat.meta.derniereSynchro = new Date().toISOString();
      await sauverMeta();
      alerte("");
      $("hl-catalogue").open = false;
      ok = true;
    } catch (e) {
      if (e instanceof A.Deconnecte) alerte("Session expirée.", true);
      else alerte("Téléchargement impossible : " + e.message);
    } finally {
      bouton.disabled = false;
    }
    afficherAccueil(ok ? pluriel(ids.length, "set") + " sur l'appareil"
      + (pageGardee ? ", disponible" + (ids.length > 1 ? "s" : "") + " sans réseau." : " (garde cette page ouverte pour réviser sans réseau).")
      : undefined);
    return ok;
  }

  function telechargerSelection() {
    const ids = [...$("hl-liste").querySelectorAll("input[value]:checked")].map((c) => c.value);
    if (!ids.length) { alerte("Coche au moins un set."); return; }
    telechargerIds(ids);
  }

  async function retirerSet(set) {
    if (!confirm("Retirer « " + set.titre + " » de cet appareil ? Les réponses non synchronisées sont gardées.")) return;
    await A.effacer("sets", set.id);
    etat.sets.delete(set.id);
    afficherAccueil();
  }

  // « ⤓ Hors ligne » de la page des QCM : /qcm/hors-ligne/#telecharger=id,id
  function demandeDeTelechargement() {
    const m = location.hash.match(/^#telecharger=(.+)$/);
    if (!m) return null;
    const ids = m[1].split(",").map((x) => { try { return decodeURIComponent(x); } catch (e) { return ""; } }).filter(Boolean);
    history.replaceState(null, "", location.pathname + location.search);
    return ids.length ? ids : null;
  }

  // --- Démarrage ---
  if (L) {
    L.init({
      sets: () => etat.sets, // rechargé par charger()
      joueur: () => etat.meta.joueur || "",
      sauver: (set) => A.ecrire("sets", set),
      repondu: () => { etat.attente++; if (navigator.onLine) envoyerDiscretement(); },
      retirer: retirerSet,
    });
  }
  $("hl-synchroniser").addEventListener("click", () => synchroniser());
  $("hl-telecharger").addEventListener("click", telechargerSelection);
  $("hl-tout-cocher").addEventListener("click", () => {
    $("hl-liste").querySelectorAll("input[type=checkbox]").forEach((c) => { c.checked = true; });
  });
  window.addEventListener("hashchange", router);
  window.addEventListener("online", () => { afficherReseau(); synchroniser(); });
  window.addEventListener("offline", afficherReseau);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && navigator.onLine && etat.attente) synchroniser();
  });

  if (!pageGardee) {
    // Avertissement permanent (la zone d'alerte, elle, est effacée à chaque synchronisation).
    $("hl-non-securise").hidden = false;
    $("hl-sous-titre").textContent = "Les sets téléchargés restent sur cet appareil et tes réponses rejoignent ta progression "
      + "dès que la connexion revient. À cette adresse, la page elle-même ne s'ouvre pas sans réseau.";
  }
  if ("serviceWorker" in navigator && app.dataset.swUrl) {
    navigator.serviceWorker.register(app.dataset.swUrl, { scope: app.dataset.scope }).catch(() => {
      alerte("Mode hors ligne indisponible : ce navigateur refuse le service worker (HTTPS requis, sauf sur cet ordinateur).");
    });
  }

  const aTelecharger = demandeDeTelechargement();
  charger().then(async () => {
    router();
    await synchroniser();
    if (aTelecharger && navigator.onLine) await telechargerIds(aTelecharger);
  }).catch((e) => alerte("Stockage indisponible sur ce navigateur : " + e.message));
})();

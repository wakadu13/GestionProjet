// Module Langues hors ligne : fichiers de langue téléchargés sur l'appareil, sessions sans
// connexion, synchronisation avec le serveur. Port de langues_hors_ligne.js de Revision (règles
// identiques ; seul l'affichage suit le style de Hangar).
//
// Tout ce que le serveur fait pendant une session est refait ici avec les mêmes règles :
// planification FSRS (qcm/fsrs.py, qcm/langues.py), correction tolérante des réponses tapées
// (noter_saisie, diff), construction de la session (révisions dues, nouveautés débloquées et quota
// du jour, une facette par mot, entrelacement). Les réponses partent dans la même file d'attente
// que les QCM (qcm_attente.js) ; le serveur les rejoue à leur date.
(function () {
  "use strict";
  const A = window.QcmAttente;
  if (!A) return;

  // -- FSRS et suivi (fsrs.js, partagé avec les QCM hors ligne) --
  const F = window.FSRS;
  if (!F) return;
  const { A_REVOIR, DIFFICILE, BIEN, JOUR_MS, estNouveau, estDu, retention, etatSuivant, delaiMs, texteDelai } = F;
  const clamp = (x, bas, haut) => Math.min(haut, Math.max(bas, x));

  const previsions = (st, now) => {
    const p = {};
    for (const n of [1, 2, 3, 4]) p[n] = texteDelai(delaiMs(etatSuivant(st, n, now), n));
    return p;
  };

  // Applique une révision à la progression locale (comme QuizProgress.record_langue).
  function appliquer(set, exoId, note, dateIso) {
    const now = Date.parse(dateIso);
    const st = set.progression[exoId] || { stabilite: null, difficulte: null, echeance: null, decouverte: null, oublis: 0, last_review: null, last_correct: null };
    if (st.last_review && now <= Date.parse(st.last_review)) return;
    const dejaAppris = !estNouveau(st);
    const etat = etatSuivant(st, note, now);
    st.stabilite = Math.round(etat.stabilite * 1000) / 1000;
    st.difficulte = Math.round(etat.difficulte * 1000) / 1000;
    st.echeance = new Date(now + delaiMs(etat, note)).toISOString();
    st.last_review = dateIso;
    st.last_correct = note !== A_REVOIR;
    if (!st.decouverte && !dejaAppris) st.decouverte = dateIso;
    if (dejaAppris && note === A_REVOIR) st.oublis = (st.oublis || 0) + 1;
    set.progression[exoId] = st;
  }

  // -- Correction (langues.py) --
  const plier = (s) => String(s || '').replace(/[\u2018\u2019\u201a\u201b\u02bc\u2032\u00b4]/g, "'").replace(/[\u00ab\u00bb\u201c\u201d\u201e\u201f]/g, '"').replace(/[\u2010\u2011]/g, '-')
    .normalize('NFKD').toLowerCase().replace(/\u0153/g, 'oe').replace(/\u00e6/g, 'ae').replace(/[\u0300-\u036f]/g, '');
  const normaliserTexte = (s) => plier(s).trim().split(/\s+/).join(" ");
  function normaliserStrict(s) {
    s = String(s).trim().toLowerCase();
    s = s.replace(/[’‘`ʼ]/g, "'").replace(/[“”«»]/g, '"');
    s = s.replace(/[.!?…;:\s]+$/, "");
    let res = "";
    for (const mot of s.split(/\s+/).filter(Boolean)) {
      if (res && !(mot.length === 1 && "?!:;,".includes(mot))) res += " ";
      res += mot;
    }
    return res;
  }
  function sansParticule(s, particules) {
    const i = s.indexOf(" ");
    if (i < 0) return s;
    const tete = s.slice(0, i), reste = s.slice(i + 1);
    return reste && particules.includes(tete) ? reste : s;
  }
  function distance(a, b) {
    const d = Array.from({ length: a.length + 1 }, (_, i) => { const l = new Array(b.length + 1).fill(0); l[0] = i; return l; });
    for (let j = 0; j <= b.length; j++) d[0][j] = j;
    for (let i = 1; i <= a.length; i++) {
      for (let j = 1; j <= b.length; j++) {
        const cout = a[i - 1] !== b[j - 1] ? 1 : 0;
        d[i][j] = Math.min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cout);
        if (i > 1 && j > 1 && a[i - 1] === b[j - 2] && a[i - 2] === b[j - 1]) d[i][j] = Math.min(d[i][j], d[i - 2][j - 2] + 1);
      }
    }
    return d[a.length][b.length];
  }
  const RANG = { juste: 0, accents: 1, faute: 2, faux: 3 };
  const MESSAGES = { juste: "✓ Correct !", accents: "≈ Presque : attention aux accents", faute: "≈ Presque : faute de frappe", faux: "✗ Incorrect" };
  const noteDuVerdict = (v) => ({ juste: BIEN, accents: DIFFICILE, faute: DIFFICILE })[v] || A_REVOIR;

  function noterSaisie(saisie, reponses, tolerant, particules) {
    const s = sansParticule(normaliserStrict(saisie), particules);
    const sPlat = normaliserTexte(s);
    let meilleur = ["faux", Infinity, reponses[0] || ""];
    for (const r of reponses) {
      const a = sansParticule(normaliserStrict(r), particules);
      if (a === s) return ["juste", r];
      const aPlat = normaliserTexte(a);
      const dist = distance(sPlat, aPlat);
      let verdict;
      if (!tolerant) verdict = "faux";
      else if (dist === 0) verdict = "accents";
      else {
        const n = aPlat.length, seuil = n >= 10 ? 2 : n >= 5 ? 1 : 0;
        verdict = dist <= seuil ? "faute" : "faux";
      }
      if (RANG[verdict] < RANG[meilleur[0]] || (RANG[verdict] === RANG[meilleur[0]] && dist < meilleur[1])) meilleur = [verdict, dist, r];
    }
    return [meilleur[0], meilleur[2]];
  }

  function diff(saisie, attendu) {
    const a = attendu, s = saisie;
    const eq = (x, y) => x.toLowerCase() === y.toLowerCase();
    const lcs = Array.from({ length: a.length + 1 }, () => new Array(s.length + 1).fill(0));
    for (let i = a.length - 1; i >= 0; i--) {
      for (let j = s.length - 1; j >= 0; j--) lcs[i][j] = eq(a[i], s[j]) ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
    }
    const res = [];
    let i = 0, j = 0;
    while (i < a.length) {
      if (j < s.length && eq(a[i], s[j])) { res.push([a[i], true]); i++; j++; }
      else if (j < s.length && lcs[i][j + 1] >= lcs[i + 1][j]) j++;
      else { res.push([a[i], /\s/.test(a[i])]); i++; }
    }
    return res;
  }

  function corriger(set, e, reponse) {
    let verdict, attendu = "", saisie = "";
    if (e.forme === "saisie") {
      saisie = String(reponse || "").trim();
      if (!saisie) return null;
      [verdict, attendu] = noterSaisie(saisie, e.reponses, e.tolerant, set.particules || []);
    } else if (e.forme === "choix") {
      if (!Array.isArray(reponse) || !reponse.length) return null;
      const choisis = [...new Set(reponse)].sort((x, y) => x - y);
      verdict = JSON.stringify(choisis) === JSON.stringify(e.reponses) ? "juste" : "faux";
    } else if (e.forme === "ordre") {
      if (!Array.isArray(reponse) || reponse.length !== e.mots.length) return null;
      saisie = reponse.map((k) => e.mots[k]).join(" ");
      [verdict, attendu] = noterSaisie(saisie, e.reponses, false, []);
    } else return null;
    const reponses = e.reponses || [];
    return {
      verdict, message: MESSAGES[verdict], note: noteDuVerdict(verdict), saisie, attendu,
      diff: e.forme === "saisie" && verdict !== "juste" ? diff(saisie, attendu) : [],
      autres: e.forme === "saisie" ? reponses.filter((r) => r !== attendu) : [],
    };
  }

  // -- Sessions (langues.py) --
  const GENRES = ["vocabulaire", "conjugaison", "grammaire"];
  const NOMS_GENRES = { vocabulaire: "Vocabulaire", conjugaison: "Conjugaison", grammaire: "Grammaire" };
  const MODES = { quotidien: "Session du jour", vocabulaire: "Vocabulaire", conjugaison: "Conjugaison", grammaire: "Grammaire", faibles: "Points faibles", intensif: "Révision intensive" };
  const NOUVEAUX_PAR_JOUR = 20, TAILLE_SESSION = 20, LIMITE_INTENSIF = 200;
  const jourLocal = (ms) => { const d = new Date(ms); return d.getFullYear() + "-" + (d.getMonth() + 1) + "-" + d.getDate(); };
  const debloque = (set, e) => {
    if (!e.prerequis) return true;
    const st = set.progression[e.prerequis];
    return !!(st && st.stabilite != null && st.last_correct === true);
  };
  function quotaNouveaux(sets, now) {
    const quotas = sets.map((s) => s.nouveaux_par_jour).filter((q) => q != null);
    const quota = quotas.length ? Math.max(...quotas) : NOUVEAUX_PAR_JOUR;
    const auj = jourLocal(now);
    let decouverts = 0;
    for (const s of sets) for (const st of Object.values(s.progression)) if (st.decouverte && jourLocal(Date.parse(st.decouverte)) === auj) decouverts++;
    return Math.max(0, quota - decouverts);
  }
  function melanger(liste) {
    for (let i = liste.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [liste[i], liste[j]] = [liste[j], liste[i]]; }
    return liste;
  }
  function melangerMots(n) {
    const m = [...Array(n).keys()];
    for (let essai = 0; essai < 10; essai++) { melanger(m); if (m.some((x, i) => x !== i)) break; }
    return m;
  }
  const filesParGenre = (items) => { const f = {}; for (const g of GENRES) f[g] = []; for (const it of items) f[it.e.genre].push(it); return f; };
  function repartir(items) {
    const files = filesParGenre(items), res = [];
    while (GENRES.some((g) => files[g].length)) for (const g of GENRES) if (files[g].length) res.push(files[g].shift());
    return res;
  }
  function entrelacer(items) {
    const files = filesParGenre(items);
    const tailles = {};
    for (const g of GENRES) tailles[g] = Math.max(1, files[g].length);
    const res = [];
    let dernier = null;
    for (;;) {
      const ordre = GENRES.filter((g) => files[g].length).sort((a, b) => (files[b].length / tailles[b]) - (files[a].length / tailles[a]));
      if (!ordre.length) return res;
      const g = ordre.find((x) => x !== dernier) || ordre[0];
      const file = files[g];
      // Évite deux formes du même verbe à la suite.
      if (res.length && file.length > 1 && file[0].e.entree === res[res.length - 1].e.entree) [file[0], file[1]] = [file[1], file[0]];
      res.push(file.shift());
      dernier = g;
    }
  }

  function sessionIntensive(sets, now) {
    const parEntree = new Map();
    for (const s of sets) for (const e of s.exercices) {
      const cle = s.id + "\u0000" + e.entree;
      if (!parEntree.has(cle)) parEntree.set(cle, []);
      parEntree.get(cle).push({ set: s, e, nouveau: estNouveau(s.progression[e.exo_id]) });
    }
    const fragilite = (items) => {
      const vus = items.map((i) => i.set.progression[i.e.exo_id]).filter((st) => st && st.stabilite != null);
      if (!vus.length) return [1, 0, 1];
      const oublis = vus.reduce((n, st) => n + (st.oublis || 0) + (st.last_correct === false ? 1 : 0), 0);
      return [0, -oublis, Math.min(...vus.map((st) => retention(st, now)))];
    };
    const entrees = melanger([...parEntree.values()]).map((items) => ({ items, f: fragilite(items) }))
      .sort((a, b) => a.f[0] - b.f[0] || a.f[1] - b.f[1] || a.f[2] - b.f[2]).map((x) => x.items);
    const retenues = [];
    let total = 0;
    for (const items of entrees) {
      if (total && total + items.length > LIMITE_INTENSIF) continue;
      retenues.push(items.slice().sort((a, b) => (a.e.prerequis ? 1 : 0) - (b.e.prerequis ? 1 : 0)));
      total += items.length;
    }
    const session = [];
    const max = Math.max(0, ...retenues.map((r) => r.length));
    for (let rang = 0; rang < max; rang++) session.push(...melanger(retenues.filter((r) => rang < r.length).map((r) => r[rang])));
    return session;
  }

  function construireSession(sets, mode, n, now) {
    if (mode === "intensif") return sessionIntensive(sets, now);
    const revues = [], nouveaux = [], faibles = [];
    for (const s of sets) for (const e of s.exercices) {
      if (GENRES.includes(mode) && e.genre !== mode) continue;
      const st = s.progression[e.exo_id];
      const item = { set: s, e, nouveau: estNouveau(st) };
      if (item.nouveau) { if (debloque(s, e)) nouveaux.push(item); continue; }
      const r = retention(st, now);
      if (mode === "faibles") { if ((st.oublis || 0) > 0 || st.last_correct === false) faibles.push([-(st.oublis || 0), r, item]); }
      else if (estDu(st, now)) revues.push([r, item]);
    }
    let candidats, quota;
    if (mode === "faibles") {
      faibles.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
      candidats = faibles.map((x) => x[2]);
      quota = 0;
    } else {
      revues.sort((a, b) => a[0] - b[0]);
      quota = quotaNouveaux(sets, now);
      const reserve = Math.min(nouveaux.length, Math.max(Math.floor(n / 3), 3), quota);
      const nbRevues = Math.min(revues.length, Math.max(0, n - reserve));
      candidats = revues.slice(0, nbRevues).map((x) => x[1]).concat(repartir(nouveaux));
    }
    const parEntree = new Map();
    const choisis = [];
    let nbNouveaux = 0;
    for (const it of candidats) {
      if (choisis.length >= n) break;
      if (it.nouveau && nbNouveaux >= quota) continue;
      const cle = it.set.id + "\u0000" + it.e.entree;
      const deja = parEntree.get(cle) || 0;
      if (deja < (it.e.max_par_session || 1)) { parEntree.set(cle, deja + 1); nbNouveaux += it.nouveau ? 1 : 0; choisis.push(it); }
    }
    return entrelacer(choisis);
  }

  // Résumé d'une sélection de fichiers (page d'accueil) : à revoir, nouveaux disponibles.
  function resume(sets, now) {
    let aRevoir = 0, nouveauxDispo = 0;
    for (const s of sets) for (const e of s.exercices) {
      const st = s.progression[e.exo_id];
      if (estNouveau(st)) { if (debloque(s, e)) nouveauxDispo++; } else if (estDu(st, now)) aRevoir++;
    }
    return { a_revoir: aRevoir, nouveaux_disponibles: nouveauxDispo };
  }

  // -- Session dans le moteur (quiz_langue.js) --
  function payload(it, now) {
    const e = it.e, st = it.set.progression[e.exo_id];
    const p = { ...e, nouveau: it.nouveau, caracteres: it.set.caracteres || [] };
    if (e.forme === "carte") p.previsions = previsions(st, now);
    if (e.forme === "ordre") p.melange = melangerMots(e.mots.length);
    if (p.audio && !p.audio.lang) p.audio = { ...p.audio, lang: it.set.code_langue || "" };
    return p;
  }

  let contexte = null; // { sets(): Map, joueur(), sauver(set), repondu(), retirer(set) } fourni par qcm_hors_ligne.js

  function lancer(params, root) {
    const langue = params.get("langue") || "";
    const fichier = params.get("fichier") || "";
    const dossier = params.get("dossier") || "";
    const mode = MODES[params.get("mode")] ? params.get("mode") : "quotidien";
    let sets = [...contexte.sets().values()].filter((s) => s.type === "langue" && s.langue === langue);
    if (fichier) sets = sets.filter((s) => s.id === fichier);
    else if (dossier) sets = sets.filter((s) => s.dossier === dossier);
    if (!sets.length) return false;
    const n = Number(params.get("n")) > 0 ? Math.min(Number(params.get("n")), LIMITE_INTENSIF) : TAILLE_SESSION;
    const now = Date.now();
    const session = construireSession(sets, mode, n, now);
    const parId = new Map(sets.map((s) => [s.id, s]));
    document.title = MODES[mode] + " · " + langue + (fichier ? " · " + sets[0].titre : dossier ? " · " + dossier : "") + " · hors ligne";
    const titre = root.querySelector("[data-titre-session]");
    if (titre) titre.textContent = MODES[mode] + " · " + langue + (fichier ? " · " + sets[0].titre : dossier ? " · " + dossier : "");
    root.querySelector(".quiz-intensif-info").hidden = !(mode === "intensif" && session.length);
    window.LangueMoteur.demarrer({
      root,
      exercices: session.map((it) => payload(it, now)),
      intensif: mode === "intensif",
      backUrl: "#",
      quitter: () => { location.hash = ""; },
      corriger: async (e, reponse) => {
        const v = corriger(parId.get(e.set_id), e, reponse);
        if (!v) throw new Error("réponse incomplète");
        if (e.forme === "choix") v.reponses = e.reponses;
        return v;
      },
      enregistrer: async (e, note, duree) => {
        const rep = await A.ajouter({ set_id: e.set_id, exo_id: e.exo_id, note, duree_ms: duree, joueur: contexte.joueur() });
        const set = parId.get(e.set_id);
        if (set) { appliquer(set, e.exo_id, note, rep.date); await contexte.sauver(set); }
        contexte.repondu();
      },
    });
    return true;
  }

  // Section « Langues » de la page d'accueil hors ligne.
  function afficher(zone, el, lien, pluriel) {
    const sets = [...contexte.sets().values()].filter((s) => s.type === "langue");
    if (!sets.length) return;
    const now = Date.now();
    const parLangue = new Map();
    for (const s of sets) { if (!parLangue.has(s.langue)) parLangue.set(s.langue, []); parLangue.get(s.langue).push(s); }
    const section = el("section", "hl-langues");
    const h = el("div", "hl-section-h");
    h.append(el("h2", "", "Langues"), el("span", "count", pluriel(sets.length, "paquet")));
    section.append(h);
    for (const [langue, fichiers] of [...parLangue.entries()].sort((a, b) => a[0].localeCompare(b[0], "fr", { numeric: true }))) {
      const r = resume(fichiers, now);
      const details = el("details", "qcm-cat");
      details.open = true;
      const summary = el("summary");
      const meta = el("span", "qcm-cat-meta");
      const due = el("span", "qcm-due" + (r.a_revoir ? "" : " zero"), r.a_revoir ? String(r.a_revoir) : "✓");
      due.title = r.a_revoir + " à revoir";
      meta.append(el("span", "", pluriel(fichiers.length, "paquet") + " · " + r.nouveaux_disponibles + " nouveau" + (r.nouveaux_disponibles > 1 ? "x" : "")), due);
      summary.append(el("span", "hl-chev", "›"), el("span", "qcm-cat-name", langue), meta);
      const corps = el("div", "qcm-cat-body");
      const actions = el("div", "qcm-cat-actions");
      const q = "langue=" + encodeURIComponent(langue);
      actions.append(lien("Session du jour", "#langue?mode=quotidien&" + q, "primary"), lien("Points faibles", "#langue?mode=faibles&" + q), lien("Révision intensive", "#langue?mode=intensif&" + q));
      const ul = el("ul", "qcm-sets");
      for (const s of fichiers.sort((a, b) => a.titre.localeCompare(b.titre, "fr", { numeric: true }))) {
        const rs = resume([s], now);
        const li = el("li", "qcm-set");
        const head = el("div", "qcm-set-head");
        head.append(el("div", "qcm-set-title", s.titre), el("span", "badge", pluriel(s.exercices.length, "exercice")));
        li.append(head);
        li.append(el("p", "qcm-set-status", (s.dossier ? s.dossier + " · " : "") + rs.a_revoir + " à revoir · " + rs.nouveaux_disponibles + " nouveau" + (rs.nouveaux_disponibles > 1 ? "x" : "")));
        const boutons = el("div", "qcm-set-buttons");
        const f = "&fichier=" + encodeURIComponent(s.id);
        boutons.append(lien("Réviser ce paquet", "#langue?mode=quotidien&" + q + f, "primary"), lien("Intensif", "#langue?mode=intensif&" + q + f));
        const retirer = el("button", "btn btn-sm btn-ghost hl-retirer", "Retirer");
        retirer.type = "button";
        retirer.addEventListener("click", () => contexte.retirer(s));
        boutons.append(retirer);
        li.append(boutons);
        ul.append(li);
      }
      corps.append(actions, ul);
      details.append(summary, corps);
      section.append(details);
    }
    zone.append(section);
  }

  window.LanguesHorsLigne = {
    init: (ctx) => { contexte = ctx; },
    lancer, afficher, appliquer, corriger, construireSession, resume, noterSaisie, diff, MODES,
  };
})();

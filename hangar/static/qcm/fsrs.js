// FSRS (qcm/fsrs.py) et suivi d'un élément (qcm/langues.py, qcm/models.py), partagés par les
// QCM et les paquets de langue hors ligne : mêmes règles que le serveur, pour planifier sur
// l'appareil sans connexion. Repris tel quel de Revision (wiki/static/wiki/fsrs.js).
(function (global) {
  "use strict";
  const W = [0.4072, 1.1829, 3.1262, 15.4722, 7.2102, 0.5316, 1.0651, 0.0234, 1.616, 0.1544, 1.0824, 1.9813,
    0.0953, 0.2975, 2.2042, 0.2407, 2.9466];
  const DECAY = -0.5, FACTOR = 19 / 81, RETENTION = 0.9, S_MIN = 0.01;
  const A_REVOIR = 1, DIFFICILE = 2, BIEN = 3, FACILE = 4;
  const JOUR_MS = 86400000;
  // Progression antérieure à FSRS (boîtes de Leitner, quiz.py) : intervalle de chaque
  // boîte, et stabilité estimée quand une telle question est revue pour la première fois.
  const INTERVALLES_LEITNER = { 0: 0, 1: 1, 2: 3, 3: 7, 4: 14, 5: 30 };
  const STABILITE_LEITNER = { 0: 0.5, 1: 1, 2: 3, 3: 7, 4: 14, 5: 30 };
  const clamp = (x, bas, haut) => Math.min(haut, Math.max(bas, x));

  const retrievabilite = (jours, s) => Math.pow(1 + FACTOR * Math.max(jours, 0) / Math.max(s, S_MIN), DECAY);
  const difficulteInitiale = (note) => clamp(W[4] - (note - 3) * W[5], 1, 10);
  const premiere = (note) => ({ stabilite: W[note - 1], difficulte: difficulteInitiale(note) });

  function suivante(etat, jours, note) {
    const s = Math.max(etat.stabilite, S_MIN), d = etat.difficulte;
    const r = retrievabilite(jours, s);
    const dNote = d - W[6] * (note - 3);
    const d2 = clamp(W[7] * difficulteInitiale(FACILE) + (1 - W[7]) * dNote, 1, 10);
    let s2;
    if (note === A_REVOIR) {
      const oubli = W[11] * Math.pow(d, -W[12]) * (Math.pow(s + 1, W[13]) - 1) * Math.exp(W[14] * (1 - r));
      s2 = Math.min(oubli, s);
    } else {
      const penalite = note === DIFFICILE ? W[15] : 1, bonus = note === FACILE ? W[16] : 1;
      s2 = s * (1 + Math.exp(W[8]) * (11 - d) * Math.pow(s, -W[9]) * (Math.exp(W[10] * (1 - r)) - 1) * penalite * bonus);
    }
    return { stabilite: Math.max(s2, S_MIN), difficulte: d2 };
  }
  const intervalleJours = (s, retention) => s / FACTOR * (Math.pow(retention || RETENTION, 1 / DECAY) - 1);

  // -- Suivi d'un élément : {stabilite, difficulte, echeance, last_review, box, seen…} --
  const joursDepuis = (st, now) => st.last_review ? Math.max(0, now - Date.parse(st.last_review)) / JOUR_MS : 0;
  const estNouveau = (st) => !st || st.stabilite == null;
  const estDu = (st, now) => !st.echeance || Date.parse(st.echeance) <= now;
  const retention = (st, now) => st.stabilite == null ? 0 : retrievabilite(joursDepuis(st, now), st.stabilite);
  const etatSuivant = (st, note, now) => (st && st.stabilite != null && st.difficulte != null)
    ? suivante({ stabilite: st.stabilite, difficulte: st.difficulte }, joursDepuis(st, now), note)
    : premiere(note);
  function delaiMs(etat, note) {
    if (note === A_REVOIR) return 10 * 60000;
    const jours = Math.floor(intervalleJours(etat.stabilite) + 0.5);
    return Math.min(36500, Math.max(1, jours)) * JOUR_MS;
  }
  function texteDelai(ms) {
    const jours = Math.floor(ms / JOUR_MS);
    if (jours < 1) return Math.floor(ms / 60000) + " min";
    if (jours < 31) return jours + " j";
    if (jours < 365) return Math.floor(jours / 30.4 + 0.5) + " mois";
    return (jours / 365).toFixed(1).replace(".", ",") + " an";
  }

  // -- Questions de QCM : progression FSRS, ou boîtes de Leitner pour une progression ancienne --
  const boite = (st) => Math.min(st && st.box || 0, 5);
  function etatSuivantQcm(st, note, now) {
    if (st && st.stabilite != null && st.difficulte != null) return etatSuivant(st, note, now);
    if (st && st.seen > 0) return suivante({ stabilite: STABILITE_LEITNER[boite(st)], difficulte: 5 }, joursDepuis(st, now), note);
    return premiere(note);
  }
  function estDuQcm(st, now) {
    if (!st) return true;
    if (st.echeance) return Date.parse(st.echeance) <= now;
    if (!st.last_review) return true;
    return now >= Date.parse(st.last_review) + INTERVALLES_LEITNER[boite(st)] * JOUR_MS;
  }
  // 0 (oublié) à 1 (bien su) : rétention FSRS, ou boîte de Leitner.
  const urgence = (st, now) => (st && st.stabilite != null ? retention(st, now) : boite(st) / 5);
  // Échéance (ms) d'une question : FSRS, ou intervalle de Leitner.
  function echeanceQcm(st) {
    if (!st) return null;
    if (st.echeance) return Date.parse(st.echeance);
    if (!st.last_review) return null;
    return Date.parse(st.last_review) + INTERVALLES_LEITNER[boite(st)] * JOUR_MS;
  }

  global.FSRS = {
    W, RETENTION, JOUR_MS, A_REVOIR, DIFFICILE, BIEN, FACILE, STABILITE_LEITNER, INTERVALLES_LEITNER,
    retrievabilite, premiere, suivante, intervalleJours, joursDepuis, estNouveau, estDu, retention, etatSuivant,
    delaiMs, texteDelai, etatSuivantQcm, estDuQcm, urgence, echeanceQcm,
  };
})(window);

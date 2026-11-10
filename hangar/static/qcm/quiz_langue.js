// Module Langues côté navigateur : cartes de vocabulaire (4 notes FSRS),
// réponses à taper (correction par le serveur, différences surlignées),
// QCM/QCR de grammaire et remise en ordre de mots.
// Un exercice raté revient 4 positions plus loin (2 nouvelles tentatives au
// plus) ; seule la 1re tentative est enregistrée, en quittant l'exercice (la
// touche F peut entre-temps la passer en « Facile »).
// Révision intensive : un exercice raté revient (3 positions plus loin) jusqu'à
// être réussi 2 fois de suite ; un exercice réussi du premier coup est su.
// Son : les mots en langue apprise sont lus par la synthèse vocale du navigateur
// (gratuite, hors ligne) ; la dictée fait écrire un mot entendu.
// Le moteur sert aussi hors ligne (LangueMoteur.demarrer, langues_hors_ligne.js) :
// la correction et l'enregistrement lui sont alors fournis par l'appareil.
(function () {
  let sessionPrecedente = null; // arrête les raccourcis clavier de la session précédente

  // options : root (l'article .quiz), exercices, intensif, backUrl, quitter(),
  // corriger(exercice, réponse) → Promise<verdict>, enregistrer(exercice, note) → Promise.
  function demarrer(options) {
    if (sessionPrecedente) sessionPrecedente.abort();
    const session = new AbortController();
    sessionPrecedente = session;
    const root = options.root;
    const stage = root.querySelector(".quiz-stage");
    const exercices = options.exercices;
    const backUrl = options.backUrl || "#";
    const intensif = !!options.intensif;
    const REUSSITES_REQUISES = 2;
    const progressText = root.querySelector(".quiz-progress-text");
    const barFill = root.querySelector(".quiz-bar-fill");
    const aide = root.querySelector(".quiz-aide");

  const LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ";

  // -- Synthèse vocale --
  const synthese = window.speechSynthesis && window.SpeechSynthesisUtterance ? window.speechSynthesis : null;
  let lectureAuto = true;
  try { lectureAuto = localStorage.getItem("wiki-langue-audio") !== "0"; } catch (e) { /* stockage bloqué */ }
  if (synthese) synthese.getVoices(); // déclenche le chargement des voix

  function voixPour(lang) {
    const voix = synthese.getVoices();
    const base = lang.split("-")[0].toLowerCase();
    return voix.find((v) => v.lang.toLowerCase() === lang.toLowerCase())
      || voix.find((v) => v.lang.toLowerCase().startsWith(base))
      || null;
  }

  function parler(audio) {
    if (!synthese || !audio || !audio.texte) return false;
    synthese.cancel();
    const u = new SpeechSynthesisUtterance(audio.texte);
    u.lang = audio.lang || "en-GB";
    u.rate = 0.9;
    const v = voixPour(u.lang);
    if (v) u.voice = v;
    synthese.speak(u);
    return true;
  }

  function boutonSon(audio, libelle) {
    const b = text("button", "btn btn-sm quiz-son", libelle || "🔊 Écouter");
    b.type = "button";
    b.title = "Lire à voix haute (synthèse vocale du navigateur)";
    b.addEventListener("click", (ev) => { ev.stopPropagation(); parler(audio); });
    return b;
  }

  // Bouton « son automatique » dans la barre du haut.
  const topbar = root.querySelector(".quiz-topbar");
  if (topbar) topbar.querySelectorAll(".quiz-son-auto").forEach((b) => b.remove());
  if (topbar && synthese && exercices.some((e) => e.audio)) {
    const auto = el("button", "btn btn-sm btn-ghost quiz-son-auto");
    auto.type = "button";
    const majAuto = () => {
      auto.textContent = lectureAuto ? "🔊 Son auto" : "🔇 Son coupé";
      auto.title = lectureAuto ? "Les mots sont lus à voix haute ; clic pour couper" : "Clic pour lire les mots à voix haute";
      auto.setAttribute("aria-pressed", String(lectureAuto));
    };
    auto.addEventListener("click", () => {
      lectureAuto = !lectureAuto;
      try { localStorage.setItem("wiki-langue-audio", lectureAuto ? "1" : "0"); } catch (e) { /* stockage bloqué */ }
      if (!lectureAuto) synthese.cancel();
      majAuto();
    });
    majAuto();
    topbar.insertBefore(auto, topbar.lastElementChild);
  }

  function lireSi(e, quand) {
    if (e.audio && e.audio.quand === quand && lectureAuto) parler(e.audio);
  }

  // -- Prononciation : reconnaissance vocale du navigateur, ou d'Android dans l'appli --
  const Reconnaissance = window.SpeechRecognition || window.webkitSpeechRecognition;
  const ecouteNative = !!(window.AppliNatif && typeof window.AppliNatif.ecouter === "function");
  const peutEcouter = !!(Reconnaissance || ecouteNative);

  function ecouter(lang) {
    // → Promise<string[]> : les transcriptions possibles, la plus probable d'abord.
    if (ecouteNative) {
      return new Promise((res, rej) => {
        window.resultatEcoute = (json) => {
          window.resultatEcoute = null;
          let r = {};
          try { r = typeof json === "string" ? JSON.parse(json) : json; } catch (err) { /* réponse illisible */ }
          if (r.erreur) rej(new Error(r.erreur)); else res(r.textes || []);
        };
        window.AppliNatif.ecouter(lang);
      });
    }
    return new Promise((res, rej) => {
      const r = new Reconnaissance();
      r.lang = lang;
      r.maxAlternatives = 5;
      r.interimResults = false;
      r.continuous = false;
      let fini = false;
      r.onresult = (ev) => { fini = true; res([...ev.results[0]].map((a) => a.transcript)); };
      r.onerror = (ev) => { fini = true; rej(new Error(ev.error)); };
      r.onend = () => { if (!fini) res([]); };
      r.start();
    });
  }

  function replier(t) {
    return t.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase()
      .replace(/[’']/g, "'").replace(/[^\p{L}\p{N}' ]+/gu, " ").replace(/\s+/g, " ").trim();
  }

  function distance(a, b) {
    const d = Array.from({ length: b.length + 1 }, (_, j) => j);
    for (let i = 1; i <= a.length; i++) {
      let prec = d[0];
      d[0] = i;
      for (let j = 1; j <= b.length; j++) {
        const t = d[j];
        d[j] = Math.min(d[j] + 1, d[j - 1] + 1, prec + (a[i - 1] === b[j - 1] ? 0 : 1));
        prec = t;
      }
    }
    return d[b.length];
  }

  // Bien prononcé : une transcription égale au mot (accents, ponctuation et petits mots
  // facultatifs comme « to » ou « the » ignorés), le contenant, ou à une lettre près.
  function prononceBien(entendus, oral) {
    const particules = (oral.particules || []).map(replier);
    const sans = (t) => {
      for (const p of particules) if (t.startsWith(p + " ")) return t.slice(p.length + 1);
      return t;
    };
    const cibles = oral.reponses.map((r) => sans(replier(r))).filter(Boolean);
    return entendus.some((brut) => {
      const t = sans(replier(brut));
      return cibles.some((c) => t === c || (" " + t + " ").includes(" " + c + " ")
        || (c.length >= 5 && distance(t, c) <= (c.length >= 10 ? 2 : 1)));
    });
  }

  const ERREURS_MICRO = {
    "not-allowed": "Micro refusé : autorise-le pour ce site, ou retourne la carte.",
    "service-not-allowed": "Reconnaissance vocale refusée par le navigateur.",
    "audio-capture": "Aucun micro détecté.",
    "network": "La reconnaissance vocale a besoin d'Internet ici.",
    "no-speech": "Rien entendu : réessaie, plus près du micro.",
    "indisponible": "Reconnaissance vocale indisponible sur cet appareil.",
    "annule": "Écoute annulée.",
  };
  const A_REVOIR = 1, DIFFICILE = 2, BIEN = 3, FACILE = 4;
  const NOTES = [[A_REVOIR, "À revoir"], [DIFFICILE, "Difficile"], [BIEN, "Bien"], [FACILE, "Facile"]];

  // File de la session : une entrée par passage (les réessais sont des copies).
  const file = exercices.map((e, i) => ({ i, tentative: 0, nouveau: e.nouveau, melange: (e.melange || []).slice() }));
  // Révision intensive : suivi de chaque exercice (ratés, réussites d'affilée, su).
  const suivi = exercices.map(() => ({ rates: 0, serie: 0, su: false }));
  let maitrises = 0;
  let index = 0, score = 0, premiers = 0;
  let s = null; // état de l'exercice courant

  // -- LaTeX : rendu des $...$ (déjà transformés en spans .math par le serveur),
  // avec le KaTeX local de Hangar (static/vendor/katex, chargé par la page) --
  function renderMath(container) {
    const spans = container.querySelectorAll(".math");
    if (!spans.length || !window.katex) return;
    spans.forEach((el) => {
      if (el.dataset.done) return;
      const block = el.classList.contains("math-block");
      const tex = el.textContent.trim().replace(/^\\[\[(]|\\[\])]$/g, "");
      try { window.katex.render(tex, el, { displayMode: block, throwOnError: false }); } catch (e) { /* texte brut */ }
      el.dataset.done = "1";
    });
  }

  function el(tag, cls, html) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html !== undefined) e.innerHTML = html;
    return e;
  }

  function text(tag, cls, t) {
    const e = el(tag, cls);
    e.textContent = t;
    return e;
  }

  function shuffle(n) {
    const m = [...Array(n).keys()];
    for (let essai = 0; essai < 10; essai++) {
      for (let i = n - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [m[i], m[j]] = [m[j], m[i]];
      }
      if (m.some((x, i) => x !== i)) break;
    }
    return m;
  }

  function current() { return file[index]; }
  function exo() { return exercices[current().i]; }

  function updateHeader() {
    if (intensif) {
      const reste = file.length - index;
      progressText.textContent = `Sus ${maitrises} / ${exercices.length} · ${reste} passage${reste > 1 ? "s" : ""} restant${reste > 1 ? "s" : ""}`;
      barFill.style.width = (maitrises / exercices.length * 100) + "%";
      return;
    }
    progressText.textContent = `Exercice ${index + 1} / ${file.length} · score ${score} / ${premiers}`;
    barFill.style.width = (index / file.length * 100) + "%";
  }

  function setAide() {
    const e = exo();
    let t;
    if (s.verdict) t = s.verdict.verdict === "juste" ? "Entrée Suivant · F C'était trop facile · Q Quitter" : "Entrée / Espace Suivant · Q Quitter";
    else if (e.forme === "carte") t = s.revele ? NOTES.map(([n, l]) => `${n} ${l}${n === (s.suggestion || BIEN) ? " (Espace)" : ""}`).join(" · ")
      : s.micro ? "Espace Prononcer · R Écouter · Q Quitter" : "Espace Retourner la carte · Q Quitter";
    else if (e.forme === "saisie") t = (e.audio && e.audio.quand === "debut" && synthese ? "🔊 Réécouter avec le bouton · " : "") + "Tape ta réponse · Entrée Valider · Échap Quitter";
    else if (e.forme === "choix") t = (e.audio && e.audio.quand === "debut" && synthese ? "R Réécouter · " : "")
      + (e.multiple ? "Lettres : cocher plusieurs réponses" : "Lettre : choisir") + " · Entrée Valider · Q Quitter";
    else t = "Clique les mots (ou tape leur numéro) · ⌫ Retirer le dernier · Entrée Valider · Q Quitter";
    if (aide) aide.textContent = t;
  }

  // -- Construction d'un exercice --
  function build() {
    const e = exo(), c = current();
    s = { revele: false, verdict: null, note: null, enregistre: false, selection: [], places: [], envoi: false, debut: performance.now() };
    stage.replaceChildren();
    updateHeader();

    const label = el("p", "quiz-set-label muted");
    label.appendChild(document.createTextNode(e.etiquette));
    if (c.nouveau) label.appendChild(text("span", "quiz-badge", "nouveau"));
    if (c.tentative) label.appendChild(text("span", "quiz-badge retry", "nouvelle tentative"));
    stage.appendChild(label);

    const zone = el("div");
    stage.appendChild(zone);
    s.feedback = el("div", "quiz-feedback");
    s.actions = el("div", "quiz-actions-row");
    s.nextBtn = el("button", "btn btn-primary", "Suivant →");
    s.nextBtn.type = "button";
    s.nextBtn.style.display = "none";
    s.nextBtn.addEventListener("click", suivante);

    if (e.forme === "carte") buildCarte(e, zone);
    else if (e.forme === "saisie") buildSaisie(e, zone);
    else if (e.forme === "choix") buildChoix(e, zone);
    else buildOrdre(e, c, zone);

    s.actions.appendChild(s.nextBtn);
    stage.appendChild(s.actions);
    stage.appendChild(s.feedback);
    renderMath(stage);
    setAide();
  }

  function buildCarte(e, zone) {
    const recto = el("div", "quiz-recto markdown", e.recto);
    zone.appendChild(recto);
    if (e.oral && peutEcouter) return buildOral(e, zone);
    if (e.oral) zone.appendChild(text("p", "muted", "Dis le mot à voix haute, puis retourne la carte pour l'écouter "
      + "(reconnaissance vocale indisponible sur ce navigateur)."));
    if (synthese && e.audio && e.audio.quand === "debut") {
      zone.appendChild(boutonSon(e.audio));
      lireSi(e, "debut");
    }
    const flip = el("button", "btn btn-primary", "Retourner la carte (Espace)");
    flip.type = "button";
    flip.addEventListener("click", reveler);
    s.actions.appendChild(flip);
    s.flip = flip;
  }

  // Carte de prononciation : « Prononcer » lance l'écoute ; bien prononcé, la carte se
  // retourne avec « Bien » proposé ; trois essais manqués : « À revoir ».
  function buildOral(e, zone) {
    s.essais = 0;
    const micro = el("button", "btn btn-primary quiz-micro", "🎤 Prononcer (Espace)");
    micro.type = "button";
    const resultat = el("p", "quiz-oral-resultat");
    resultat.setAttribute("role", "status");
    micro.addEventListener("click", async () => {
      if (micro.disabled || s.revele) return;
      micro.disabled = true;
      micro.textContent = "🎤 J'écoute…";
      resultat.className = "quiz-oral-resultat";
      resultat.textContent = "";
      try {
        const entendus = await ecouter(e.oral.lang || (e.audio && e.audio.lang) || "en-GB");
        if (s.revele || exo() !== e) return;
        if (!entendus.length) throw new Error("no-speech");
        s.essais++;
        if (prononceBien(entendus, e.oral)) {
          resultat.className = "quiz-oral-resultat juste";
          resultat.textContent = `✓ Bien prononcé : « ${entendus[0]} »`;
          s.suggestion = s.essais === 1 ? BIEN : DIFFICILE;
          reveler();
        } else {
          resultat.className = "quiz-oral-resultat faux";
          resultat.textContent = `✗ Entendu : « ${entendus[0]} »` + (s.essais < 3 ? " — réessaie !" : "");
          if (s.essais >= 3) { s.suggestion = A_REVOIR; reveler(); }
        }
      } catch (err) {
        resultat.className = "quiz-oral-resultat faux";
        resultat.textContent = ERREURS_MICRO[err.message] || "Écoute impossible (" + err.message + ").";
      } finally {
        micro.disabled = false;
        micro.textContent = "🎤 Réessayer (Espace)";
      }
    });
    zone.append(micro, resultat);
    s.micro = micro;
    const passer = el("button", "btn", "Je ne sais pas : voir et écouter");
    passer.type = "button";
    passer.addEventListener("click", () => { s.suggestion = A_REVOIR; reveler(); });
    s.actions.appendChild(passer);
    s.flip = passer;
  }

  function reveler() {
    const e = exo();
    if (s.revele) return;
    s.revele = true;
    s.flip.remove();
    if (s.micro) s.micro.remove();
    const verso = el("div", "quiz-verso markdown", e.verso);
    stage.insertBefore(verso, s.actions);
    if (synthese && e.audio && e.audio.quand === "verso") {
      stage.insertBefore(boutonSon(e.audio), s.actions);
      lireSi(e, "verso");
    }
    stage.insertBefore(details(e), s.actions);
    const notes = el("div", "quiz-notes");
    for (const [n, libelle] of NOTES) {
      const b = el("button", "btn note-" + n);
      b.type = "button";
      b.appendChild(text("span", "", n + " · " + libelle));
      b.appendChild(text("small", "", (e.previsions || {})[n] || ""));
      b.addEventListener("click", () => noter(n));
      if (s.suggestion === n) b.classList.add("suggere");
      notes.appendChild(b);
    }
    stage.insertBefore(text("p", "muted", "Tu t'en souvenais ?"), s.actions);
    stage.insertBefore(notes, s.actions);
    renderMath(stage);
    setAide();
  }

  function noter(n) {
    if (!s.revele || s.note) return;
    s.note = n;
    compter(n);
    suivante();
  }

  function buildSaisie(e, zone) {
    zone.appendChild(el("div", "quiz-enonce markdown", e.consigne));
    if (e.audio && e.audio.quand === "debut") {
      if (synthese) {
        const ecouter = boutonSon(e.audio, "🔊 Écouter le mot");
        ecouter.classList.remove("small");
        ecouter.classList.add("quiz-dictee");
        zone.appendChild(ecouter);
        lireSi(e, "debut");
        // Toujours lire une dictée au moins une fois, même son coupé.
        if (!lectureAuto) setTimeout(() => parler(e.audio), 0);
      } else if (e.indice) {
        zone.appendChild(text("p", "muted", "Synthèse vocale indisponible sur cet appareil — indice : « " + e.indice + " »"));
      }
    }
    const row = el("div", "quiz-saisie-row");
    if (e.invite) row.appendChild(text("span", "quiz-invite", e.invite + " →"));
    const input = el("input", "quiz-input");
    input.type = "text";
    input.autocomplete = "off";
    input.autocapitalize = "off";
    input.spellcheck = false;
    input.placeholder = e.prerempli ? "Phrase corrigée…" : "Ta réponse…";
    input.value = e.prerempli || "";
    input.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") { ev.preventDefault(); ev.stopPropagation(); valider(); }
      else if (ev.key === "Escape") { ev.preventDefault(); ev.stopPropagation(); quitter(); }
    });
    row.appendChild(input);
    zone.appendChild(row);
    s.input = input;
    if ((e.caracteres || []).length) {
      const spec = el("div", "quiz-speciaux");
      for (const ch of e.caracteres) {
        const b = text("button", "btn btn-sm", ch);
        b.type = "button";
        b.addEventListener("click", () => {
          if (input.disabled) return;
          const a = input.selectionStart ?? input.value.length, z = input.selectionEnd ?? a;
          input.setRangeText(ch, a, z, "end");
          input.focus();
        });
        spec.appendChild(b);
      }
      zone.appendChild(spec);
      s.speciaux = spec;
    }
    const v = el("button", "btn btn-primary", "Valider");
    v.type = "button";
    v.addEventListener("click", valider);
    s.actions.appendChild(v);
    s.validateBtn = v;
    setTimeout(() => { input.focus(); input.setSelectionRange(input.value.length, input.value.length); }, 0);
  }

  function buildChoix(e, zone) {
    zone.appendChild(el("div", "quiz-enonce markdown", e.enonce));
    if (e.audio && e.audio.quand === "debut") {
      if (synthese) {
        const ecouter = boutonSon(e.audio, e.ecoute ? "🔊 Écouter le mot" : "🔊 Écouter");
        if (e.ecoute) { ecouter.classList.remove("small"); ecouter.classList.add("quiz-dictee"); }
        zone.appendChild(ecouter);
        lireSi(e, "debut");
        // QCM à l'écoute : toujours lu au moins une fois, même son coupé.
        if (e.ecoute && !lectureAuto) setTimeout(() => parler(e.audio), 0);
      } else if (e.ecoute && e.indice) {
        zone.appendChild(text("p", "muted", "Synthèse vocale indisponible sur cet appareil — le mot : « " + e.indice + " »"));
      }
    }
    const list = el("div", "quiz-choices");
    s.choiceEls = e.choix.map((html, i) => {
      const b = el("button", "quiz-choice");
      b.type = "button";
      b.appendChild(el("span", "quiz-letter", LETTERS[i]));
      b.appendChild(el("span", "quiz-choice-text markdown", html));
      b.addEventListener("click", () => basculer(i));
      list.appendChild(b);
      return b;
    });
    zone.appendChild(list);
    const v = el("button", "btn btn-primary", "Valider");
    v.type = "button";
    v.addEventListener("click", valider);
    s.actions.appendChild(v);
    s.validateBtn = v;
  }

  function basculer(i) {
    const e = exo();
    if (s.verdict || s.envoi) return;
    if (!e.multiple) s.selection = [i];
    else {
      const at = s.selection.indexOf(i);
      if (at >= 0) s.selection.splice(at, 1); else s.selection.push(i);
    }
    s.choiceEls.forEach((b, j) => b.classList.toggle("selected", s.selection.includes(j)));
  }

  function buildOrdre(e, c, zone) {
    zone.appendChild(el("div", "quiz-enonce markdown", e.consigne));
    s.zoneOrdre = el("div", "quiz-ordre-zone");
    s.zoneMots = el("div", "quiz-ordre-mots");
    zone.appendChild(s.zoneOrdre);
    zone.appendChild(s.zoneMots);
    s.tuiles = c.melange.map((m, pos) => {
      const b = el("button", "quiz-mot");
      b.type = "button";
      b.appendChild(text("small", "", String(pos + 1)));
      b.appendChild(document.createTextNode(e.mots[m]));
      b.addEventListener("click", () => placer(pos));
      s.zoneMots.appendChild(b);
      return b;
    });
    const v = el("button", "btn btn-primary", "Valider");
    v.type = "button";
    v.addEventListener("click", valider);
    s.actions.appendChild(v);
    s.validateBtn = v;
  }

  function placer(pos) {
    const m = current().melange[pos];
    if (s.verdict || s.envoi || s.places.includes(m)) return;
    s.places.push(m);
    majOrdre();
  }

  function retirer(k) {
    if (s.verdict || s.envoi || k < 0 || k >= s.places.length) return;
    s.places.splice(k, 1);
    majOrdre();
  }

  function majOrdre() {
    const e = exo();
    s.zoneOrdre.replaceChildren();
    s.places.forEach((m, k) => {
      const b = text("button", "quiz-mot", e.mots[m]);
      b.type = "button";
      b.title = "Retirer";
      b.addEventListener("click", () => retirer(k));
      s.zoneOrdre.appendChild(b);
    });
    current().melange.forEach((m, pos) => { s.tuiles[pos].disabled = s.places.includes(m); });
  }

  function details(e) {
    const d = el("div", "quiz-details");
    for (const html of e.details || []) d.appendChild(el("p", "", "ⓘ " + html));
    return d;
  }

  function compter(note) {
    if (current().tentative) return;
    premiers++;
    if (note !== A_REVOIR) score++;
    updateHeader();
  }

  // -- Correction (par le serveur) --
  async function valider() {
    const e = exo();
    if (s.verdict || s.envoi) return;
    let reponse;
    if (e.forme === "saisie") {
      reponse = s.input.value.trim();
      if (!reponse) return;
    } else if (e.forme === "choix") {
      if (!s.selection.length) return;
      reponse = s.selection.slice();
    } else if (e.forme === "ordre") {
      if (s.places.length !== e.mots.length) return;
      reponse = s.places.slice();
    } else return;
    s.envoi = true;
    if (s.validateBtn) s.validateBtn.disabled = true;
    let v;
    try {
      v = await options.corriger(e, reponse);
    } catch (err) {
      s.envoi = false;
      if (s.validateBtn) s.validateBtn.disabled = false;
      s.feedback.replaceChildren(text("p", "quiz-verdict ko", "Correction impossible (connexion ?) — réessaie."));
      return;
    }
    s.envoi = false;
    s.verdict = v;
    s.note = v.note;
    compter(v.note);
    afficherCorrection(e, v);
  }

  function afficherCorrection(e, v) {
    const classe = v.verdict === "juste" ? "ok" : v.verdict === "faux" ? "ko" : "close";
    if (s.validateBtn) s.validateBtn.remove();
    const fb = s.feedback;
    fb.replaceChildren(text("div", "quiz-verdict " + classe, v.message));

    if (e.forme === "saisie") {
      s.input.disabled = true;
      s.input.classList.add(classe === "ok" ? "is-correct" : classe === "ko" ? "is-wrong" : "is-close");
      if (s.speciaux) s.speciaux.remove();
      if (v.verdict !== "juste") {
        const p = el("p", "quiz-attendu");
        p.appendChild(document.createTextNode("Réponse attendue : "));
        let span = null, etat = null;
        for (const [ch, ok] of v.diff) {
          if (ok !== etat) { span = el("span", ok ? "ok" : "ko"); p.appendChild(span); etat = ok; }
          span.textContent += ch;
        }
        if (!v.diff.length) p.appendChild(text("b", "", v.attendu));
        fb.appendChild(p);
        if (v.autres.length) fb.appendChild(el("p", "muted", "Aussi accepté : " + v.autres.join(" / ")));
      }
      if (e.tableau) fb.appendChild(tableau(e.tableau));
    } else if (e.forme === "choix") {
      const bonnes = new Set(v.reponses);
      s.choiceEls.forEach((b, i) => {
        b.disabled = true;
        if (bonnes.has(i)) b.classList.add("is-correct");
        else if (s.selection.includes(i)) b.classList.add("is-wrong");
      });
    } else if (e.forme === "ordre") {
      s.tuiles.forEach((b) => { b.disabled = true; });
      s.zoneOrdre.querySelectorAll("button").forEach((b) => { b.disabled = true; });
      if (v.verdict !== "juste") {
        fb.appendChild(text("p", "", "Ta phrase : " + v.saisie));
        const p = el("p", "quiz-attendu");
        p.appendChild(document.createTextNode("Réponse : "));
        p.appendChild(text("span", "ok", v.attendu));
        fb.appendChild(p);
      }
    }
    if (synthese && e.audio && e.audio.quand === "fin") {
      fb.appendChild(boutonSon(e.audio));
      lireSi(e, "fin");
    }
    fb.appendChild(details(e));
    renderMath(fb);
    s.nextBtn.style.display = "";
    s.nextBtn.focus();
    setAide();
  }

  function tableau(t) {
    const table = el("table", "quiz-tableau");
    table.appendChild(text("caption", "", t.titre));
    const tbody = el("tbody");
    t.lignes.forEach(([personne, forme], k) => {
      const tr = el("tr", k === t.surligne ? "surligne" : "");
      tr.appendChild(text("td", "", personne));
      tr.appendChild(text("td", "", forme));
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    return table;
  }

  function facile() {
    if (!s.verdict || s.verdict.verdict !== "juste" || s.note === FACILE) return;
    s.note = FACILE;
    s.feedback.appendChild(text("p", "quiz-facile", "Noté « trop facile » : il reviendra plus tard."));
  }

  // -- Enregistrement en quittant l'exercice --
  function enregistrer() {
    if (!s.note || s.enregistre) return;
    s.enregistre = true;
    const c = current(), e = exo();
    if (!c.tentative) {
      Promise.resolve(options.enregistrer(e, s.note, Math.round(performance.now() - s.debut)))
        .catch(() => { /* enregistrement au mieux ; l'échec n'interrompt pas la session */ });
    }
  }

  // Après un échec, l'exercice revient 4 positions plus loin (2 fois au plus).
  function reprogrammer() {
    const c = current();
    if (c.tentative >= 2) return;
    const copie = { i: c.i, tentative: c.tentative + 1, nouveau: false, melange: c.melange.length ? shuffle(c.melange.length) : [] };
    file.splice(Math.min(index + 4, file.length), 0, copie);
  }

  // Révision intensive : raté → revient vite ; réussi après une erreur → revient
  // plus tard, jusqu'à 2 réussites d'affilée.
  function reprogrammerIntensif() {
    const c = current(), t = suivi[c.i];
    if (t.su) return;
    if (s.note === A_REVOIR) { t.rates++; t.serie = 0; } else t.serie++;
    if (s.note !== A_REVOIR && (t.rates === 0 || t.serie >= REUSSITES_REQUISES)) {
      t.su = true;
      maitrises++;
      return;
    }
    const ecart = s.note === A_REVOIR ? 3 : 6;
    const copie = { i: c.i, tentative: c.tentative + 1, nouveau: false, melange: c.melange.length ? shuffle(c.melange.length) : [] };
    file.splice(Math.min(index + 1 + ecart, file.length), 0, copie);
  }

  function suivante() {
    if (!s.note) return;
    const echec = s.note === A_REVOIR && !s.enregistre;
    enregistrer();
    if (intensif) reprogrammerIntensif();
    else if (echec) reprogrammer();
    index++;
    if (index < file.length) build(); else resultats();
  }

  function quitter() {
    if (!confirm("Quitter la session ? Les réponses déjà données sont enregistrées.")) return;
    enregistrer();
    if (options.quitter) options.quitter(); else window.location.href = backUrl;
  }

  function resultats() {
    stage.replaceChildren();
    s = null;
    if (aide) aide.textContent = "";
    barFill.style.width = "100%";
    progressText.textContent = "Terminé";
    const pct = premiers ? Math.round(score / premiers * 100) : 0;
    const box = el("div", "quiz-results");
    box.appendChild(text("div", "quiz-score", intensif ? "Tout est su ✓" : score + " / " + premiers));
    box.appendChild(el("div", "mastery", '<div class="mastery-fill" style="width:' + (intensif ? 100 : pct) + '%"></div>'));
    const msg = pct >= 80 ? "Excellent 🎉" : pct >= 50 ? "Bien, continue !" : "À retravailler 💪";
    if (intensif) {
      box.appendChild(text("p", "", `Du premier coup : ${score} / ${premiers} · ${file.length} réponses au total pour tout savoir.`));
      const difficiles = exercices
        .map((e, i) => ({ e, rates: suivi[i].rates }))
        .filter((d) => d.rates)
        .sort((a, b) => b.rates - a.rates)
        .slice(0, 10);
      if (difficiles.length) {
        const liste = el("div", "quiz-hardest");
        liste.appendChild(text("h3", "", "Les plus difficiles"));
        const ul = el("ul");
        difficiles.forEach((d) => {
          const li = el("li");
          li.appendChild(text("span", "quiz-oublis", "×" + d.rates));
          li.appendChild(document.createTextNode(" " + (d.e.resume || d.e.etiquette)));
          ul.appendChild(li);
        });
        liste.appendChild(ul);
        box.appendChild(liste);
      }
      box.appendChild(text("p", "muted", "Seuls les premiers essais comptent pour la révision espacée."));
    } else {
      box.appendChild(text("p", "muted", msg + " (premiers essais) — la progression a été enregistrée."));
    }
    const actions = el("div", "quiz-actions");
    const back = text("a", "btn btn-primary", "Retour au menu");
    back.href = backUrl;
    actions.appendChild(back);
    box.appendChild(actions);
    stage.appendChild(box);
  }

  // -- Clavier -- (phase de capture : les touches du jeu passent avant les raccourcis de Hangar)
  const pris = (ev) => { ev.preventDefault(); ev.stopPropagation(); };
  window.addEventListener("keydown", (ev) => {
    if (!s || ev.ctrlKey || ev.metaKey || ev.altKey) return;
    if (root.hidden || !document.body.contains(root)) return;
    const e = exo();
    const key = ev.key;
    const k = key.toLowerCase();
    if (ev.target.tagName === "INPUT" && !ev.target.disabled) return; // saisie en cours
    if (ev.target.tagName === "BUTTON" && (key === "Enter" || key === " ") && !s.verdict && e.forme !== "carte") return;

    if (s.verdict) {
      if (key === "Enter" || key === " ") { pris(ev); suivante(); }
      else if (k === "f") { pris(ev); facile(); }
      else if (k === "q" || key === "Escape") { pris(ev); quitter(); }
      return;
    }
    if (k === "q" || key === "Escape") { pris(ev); quitter(); return; }
    if (k === "r" && e.audio && synthese) { pris(ev); parler(e.audio); return; }
    if (e.forme === "carte") {
      if (!s.revele) {
        if (key === " " || key === "Enter") { pris(ev); if (s.micro) s.micro.click(); else reveler(); }
        return;
      }
      if ("1234".includes(key) && key.length === 1) { pris(ev); noter(Number(key)); }
      else if (key === " " || key === "Enter") { pris(ev); noter(s.suggestion || BIEN); }
    } else if (e.forme === "choix") {
      const i = LETTERS.indexOf(key.toUpperCase());
      if (key.length === 1 && i >= 0 && i < e.choix.length) { pris(ev); basculer(i); }
      else if (key === "Enter" || key === " ") { pris(ev); valider(); }
    } else if (e.forme === "ordre") {
      if (key === "Enter") { pris(ev); valider(); }
      else if (key === "Backspace") { pris(ev); retirer(s.places.length - 1); }
      else if (/^[1-9]$/.test(key) && Number(key) <= e.mots.length) { pris(ev); placer(Number(key) - 1); }
    }
  }, { signal: session.signal, capture: true });

  if (exercices.length) build();
  else {
    progressText.textContent = "";
    stage.replaceChildren(text("p", "", "Rien à réviser ni à découvrir pour l'instant ✓"));
  }
  }

  function arreter() {
    if (sessionPrecedente) sessionPrecedente.abort();
    sessionPrecedente = null;
  }

  window.LangueMoteur = { demarrer, arreter };

  // Page « jouer » : session fournie par le serveur, qui corrige et enregistre.
  const root = document.getElementById("quiz-langue");
  const dataTag = document.getElementById("quiz-data");
  if (!root || !dataTag) return;
  const csrf = (window.HANGAR && window.HANGAR.csrf) || "";

  async function post(url, body) {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf, "X-Requested-With": "fetch" },
      body: JSON.stringify(body),
      credentials: "same-origin",
    });
    if (!r.ok) throw new Error(r.status);
    return r.json();
  }

  const exercicesPage = JSON.parse(dataTag.textContent);
  if (!exercicesPage.length || !root.querySelector(".quiz-stage")) return;
  demarrer({
    root,
    exercices: exercicesPage,
    intensif: root.dataset.mode === "intensif",
    backUrl: root.dataset.backUrl,
    corriger: (e, reponse) => post(root.dataset.corrigerUrl, { set_id: e.set_id, exo_id: e.exo_id, reponse }),
    enregistrer: (e, note, duree) => post(root.dataset.recordUrl, { set_id: e.set_id, exo_id: e.exo_id, note, duree_ms: duree }),
  });
})();

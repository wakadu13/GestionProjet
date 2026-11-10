// Moteur de quiz côté navigateur : QCM, QCR, texte à trous, réponse courte
// (QRC, ex. traduction ou synonyme) et flashcards.
// Feedback immédiat, navigation clavier, rendu LaTeX, enregistrement de la
// progression (Leitner) via une requête POST après chaque réponse.
// Mode « intensif » : les questions ratées reviennent plus loin dans la session
// jusqu'à être réussies 2 fois de suite ; seule la 1re réponse est enregistrée.
// Mode « examen » : chronomètre, aucune correction avant la fin, puis note sur
// 20 et correction détaillée ; les réponses sont enregistrées normalement.
// Port de Revision dans Hangar : KaTeX local (static/vendor/katex, chargé par la page),
// jeton CSRF de Hangar, raccourcis clavier prioritaires sur ceux de l'application, et
// réponses gardées sur l'appareil quand l'envoi échoue (renvoyées au prochain chargement).
(function () {
  let sessionPrecedente = null; // arrête les raccourcis clavier de la session précédente

  // -- LaTeX : rendu des $...$ (déjà transformés en spans .math par le serveur) --
  function renderMath(container) {
    const spans = container.querySelectorAll(".math");
    if (!spans.length || !window.katex) return;
    spans.forEach((el) => {
      if (el.dataset.done) return;
      const block = el.classList.contains("math-block");
      const tex = el.textContent.trim().replace(/^\\[\[(]|\\[\])]$/g, "");
      try { window.katex.render(tex, el, { displayMode: block, throwOnError: false }); } catch (e) { /* formule invalide : texte brut */ }
      el.dataset.done = "1";
    });
  }
  window.QcmRenderMath = renderMath;

  // options : stage, questions, intensif, backUrl, messageFin et
  // enregistrer(q, correct, note, dureeMs), appelé à la 1re réponse de chaque question.
  function demarrer(options) {
    if (sessionPrecedente) sessionPrecedente.abort();
    const session = new AbortController();
    sessionPrecedente = session;
    const { stage, questions, backUrl } = options;
    const intensif = !!options.intensif;
    const examen = options.examen || null; // { duree: secondes }
    let finExamen = null;
    let chrono = null;
    const progressText = document.getElementById("quiz-progress");
    const barFill = document.getElementById("quiz-bar-fill");

    const LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ";
    // Notes des flashcards : [note, libellé, classe du bouton].
    const NOTES = [
      ["a_revoir", "À revoir (1)", "note-a_revoir"],
      ["difficile", "Difficile (2)", ""],
      ["bien", "Bien (3)", "btn-primary"],
      ["facile", "Facile (4)", ""],
    ];
    // C (je savais) et I (à revoir) restent acceptés.
    const TOUCHES_NOTES = { 1: "a_revoir", 2: "difficile", 3: "bien", 4: "facile", c: "bien", i: "a_revoir" };
    // File des questions à poser (indices dans `questions`) ; en mode intensif,
    // une question ratée y est réinsérée un peu plus loin.
    const queue = questions.map((_, i) => i);
    const track = questions.map(() => ({ attempts: 0, misses: 0, streak: 0 }));
    const REUSSITES_REQUISES = 2; // réussites consécutives exigées après une erreur
    let pos = 0;
    let score = 0;
    let mastered = 0;
    const state = { selection: [], answered: false, known: null, shownAt: 0 };
    let termine = false;

    function texteTemps(ms) {
      const s = Math.max(0, Math.ceil(ms / 1000));
      return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
    }

    // Examen : le temps est écoulé, les questions restantes comptent faux.
    function tempsEcoule() {
      if (termine) return;
      pos = queue.length;
      results();
    }

    if (examen && examen.duree > 0) {
      finExamen = performance.now() + examen.duree * 1000;
      chrono = setInterval(() => {
        if (session.signal.aborted) { clearInterval(chrono); return; }
        if (finExamen - performance.now() <= 0) { clearInterval(chrono); tempsEcoule(); }
        else if (pos < queue.length) updateHeader();
      }, 500);
    }

    // -- Comparaison souple pour les TROU/QRC (casse, accents, espaces, apostrophes
    // typographiques et ponctuation finale ignorés) — même règle que normaliser_reponse --
    function normalize(s) {
      return s.normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase()
        .replace(/[\u2018\u2019]/g, "'").trim().replace(/[.!?;:\s]+$/, "").replace(/\s+/g, " ");
    }

    function current() {
      return questions[queue[pos]];
    }

    function updateHeader() {
      if (examen) {
        const reste = finExamen === null ? "" : " · ⏱ " + texteTemps(finExamen - performance.now());
        progressText.textContent = `Question ${pos + 1} / ${questions.length}${reste}`;
        progressText.classList.toggle("quiz-temps-court", finExamen !== null && finExamen - performance.now() < 60000);
        barFill.style.width = (pos / questions.length * 100) + "%";
        return;
      }
      if (intensif) {
        const reste = queue.length - pos - (state.answered ? 1 : 0);
        progressText.textContent = `Maîtrisées ${mastered} / ${questions.length} · ${reste} passage${reste > 1 ? "s" : ""} restant${reste > 1 ? "s" : ""}`;
        barFill.style.width = (mastered / questions.length * 100) + "%";
        return;
      }
      progressText.textContent = `Question ${pos + 1} / ${questions.length} · score ${score}`;
      barFill.style.width = (pos / questions.length * 100) + "%";
    }

    function shuffle(list) {
      for (let i = list.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [list[i], list[j]] = [list[j], list[i]];
      }
      return list;
    }

    // Une réponse vient d'être donnée : score, enregistrement, et en mode
    // intensif, remise en file de la question tant qu'elle n'est pas sue.
    // `note` : auto-évaluation d'une flashcard (à revoir, difficile, bien, facile).
    function answered(q, correct, note) {
      const t = track[queue[pos]];
      t.attempts++;
      // Seule la 1re réponse compte pour le score et la révision espacée : les
      // répétitions de la session ne doivent pas gonfler les boîtes de Leitner.
      if (t.attempts === 1) {
        t.ok = !!correct;
        if (correct) score++;
        options.enregistrer(q, correct, note, Math.round(performance.now() - state.shownAt));
      }
      if (intensif) {
        if (correct) t.streak++;
        else { t.streak = 0; t.misses++; }
        const done = correct && (t.misses === 0 || t.streak >= REUSSITES_REQUISES);
        if (done) mastered++;
        else {
          // Ratée : revient vite ; réussie une fois après erreur : revient plus tard.
          const gap = correct ? 6 : 3;
          const at = pos + 1 + Math.min(gap, queue.length - pos - 1);
          queue.splice(at, 0, queue[pos]);
        }
      }
      if (pos + 1 >= queue.length) state._els.nextBtn.textContent = "Voir le score";
      updateHeader();
    }

    function el(tag, cls, html) {
      const e = document.createElement(tag);
      if (cls) e.className = cls;
      if (html !== undefined) e.innerHTML = html;
      return e;
    }

    // -- Rendu d'une question --
    function build() {
      stage.replaceChildren();
      const q = current();
      const t = track[queue[pos]];
      state.selection = [];
      state.answered = false;
      state.known = null;
      state.shownAt = performance.now();
      updateHeader();

      const label = q.set_titre + " · " + q.type + (t.attempts ? " · à revoir (passage " + (t.attempts + 1) + ")" : "");
      stage.appendChild(el("p", "quiz-set-label muted", label));
      if (q.consigne) {
        const consigne = el("p", "quiz-consigne");
        consigne.textContent = q.consigne;
        stage.appendChild(consigne);
      }
      const enonce = el("div", "quiz-enonce markdown", q.enonce);
      stage.appendChild(enonce);

      const feedback = el("div", "quiz-feedback");
      const actions = el("div", "quiz-actions-row");
      const nextBtn = el("button", "btn btn-primary", "Suivant →");
      nextBtn.style.display = "none";
      nextBtn.addEventListener("click", next);

      let choiceEls = [];
      let input = null;

      if (q.type === "QCM" || q.type === "QCR") {
        const list = el("div", "quiz-choices");
        // Au 2e passage d'une question, on mélange les choix pour ne pas
        // retenir une position plutôt qu'une réponse.
        const order = q.choix.map((_, i) => i);
        if (intensif && t.attempts) shuffle(order);
        state._order = order;
        choiceEls = new Array(q.choix.length);
        order.forEach((i, k) => {
          const c = el("button", "quiz-choice");
          c.type = "button";
          c.appendChild(el("span", "quiz-letter", LETTERS[k]));
          c.appendChild(el("span", "quiz-choice-text markdown", q.choix[i]));
          c.addEventListener("click", () => toggle(i));
          list.appendChild(c);
          choiceEls[i] = c;
        });
        stage.appendChild(list);
        const validate = el("button", "btn btn-primary", "Valider");
        validate.addEventListener("click", submitChoices);
        actions.appendChild(validate);
        state._validateBtn = validate;
      } else if (q.type === "TROU" || q.type === "QRC") {
        input = el("input", "quiz-input");
        input.type = "text";
        input.placeholder = "Votre réponse…";
        input.autofocus = true;
        stage.appendChild(input);
        const validate = el("button", "btn btn-primary", "Valider");
        validate.addEventListener("click", submitTrou);
        actions.appendChild(validate);
        state._validateBtn = validate;
        input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); submitTrou(); } });
        setTimeout(() => input.focus(), 0);
      } else if (q.type === "FLASHCARD") {
        const verso = el("div", "quiz-verso markdown", q.verso);
        verso.style.display = "none";
        stage.appendChild(verso);
        const flip = el("button", "btn btn-primary", "Retourner la carte (Espace)");
        flip.addEventListener("click", () => revealCard(verso, flip));
        actions.appendChild(flip);
        state._flip = () => revealCard(verso, flip);
      }

      stage.appendChild(actions);
      stage.appendChild(feedback);
      actions.appendChild(nextBtn);
      state._els = { q, feedback, nextBtn, choiceEls, input, actions };
      renderMath(stage);

      function toggle(i) {
        if (state.answered) return;
        if (q.type === "QCM") state.selection = [i];
        else {
          const at = state.selection.indexOf(i);
          if (at >= 0) state.selection.splice(at, 1); else state.selection.push(i);
        }
        choiceEls.forEach((c, j) => c.classList.toggle("selected", state.selection.includes(j)));
      }
      state._toggle = toggle;
    }

    function showFeedback(correct, explicationHtml, verdict) {
      const { feedback, nextBtn } = state._els;
      if (examen) {
        // Pas de correction avant la fin : juste un accusé.
        feedback.innerHTML = '<div class="quiz-verdict muted">Réponse enregistrée</div>';
        nextBtn.textContent = pos + 1 >= queue.length ? "Terminer l'examen" : "Suivant →";
        nextBtn.style.display = "";
        nextBtn.focus();
        return;
      }
      feedback.innerHTML =
        '<div class="quiz-verdict ' + (verdict ? verdict.classe : correct ? "ok" : "ko") + '">' +
        (verdict ? verdict.texte : correct ? "✓ Correct" : "✗ Incorrect") + "</div>" +
        (explicationHtml ? '<div class="quiz-explication markdown">' + explicationHtml + "</div>" : "");
      renderMath(feedback);
      if (options.expliquer) ajouterExpliqueMoi(feedback, state._els.q);
      if (options.signaler && !state._els.q.perso) ajouterSignaler(feedback, state._els.q);
      nextBtn.style.display = "";
      nextBtn.focus();
    }

    // « Signaler » : question fausse ou ambiguë, transmise aux admins (corrigée ensuite, avec Claude au besoin).
    function ajouterSignaler(feedback, q) {
      const b = el("button", "btn btn-sm quiz-signaler", "⚑ Signaler un problème");
      b.type = "button";
      b.title = "Erreur, ambiguïté, coquille : prévenir les admins du wiki";
      b.addEventListener("click", async () => {
        const commentaire = prompt("Qu'est-ce qui ne va pas avec cette question ?", "");
        if (commentaire === null) return;
        b.disabled = true;
        try {
          await options.signaler(q, commentaire.trim());
          b.textContent = "⚑ Signalée, merci ✓";
        } catch (e) {
          b.disabled = false;
          b.textContent = "⚑ Signaler (réessayer)";
        }
      });
      feedback.appendChild(b);
    }

    // « Explique-moi » : Claude explique la réponse (d'après le cours quand le set en vient).
    function ajouterExpliqueMoi(feedback, q) {
      const b = el("button", "btn btn-sm quiz-expliquer", "✦ Explique-moi");
      b.type = "button";
      b.addEventListener("click", async () => {
        b.disabled = true;
        b.textContent = "Claude réfléchit…";
        try {
          const r = await options.expliquer(q);
          const box = el("div", "quiz-explication quiz-explication-claude markdown", r.html);
          box.prepend(el("p", "muted quiz-explication-source", "✦ Explication de Claude" + (r.gardee ? "" : " (nouvelle)")));
          b.replaceWith(box);
          renderMath(box);
        } catch (e) {
          b.disabled = false;
          b.textContent = "✦ Explique-moi";
          const err = el("p", "quiz-verdict ko", e.message || "Explication impossible pour l'instant.");
          b.after(err);
          setTimeout(() => err.remove(), 4000);
        }
      });
      feedback.appendChild(b);
    }

    function submitChoices() {
      const { q, choiceEls } = state._els;
      if (state.answered || !state.selection.length) return;
      state.answered = true;
      if (state._validateBtn) state._validateBtn.disabled = true;
      const correctSet = new Set(q.reponses);
      const chosen = new Set(state.selection);
      const correct = chosen.size === correctSet.size && [...chosen].every((i) => correctSet.has(i));
      track[queue[pos]].reponse = state.selection.slice();
      choiceEls.forEach((c, i) => {
        c.disabled = true;
        if (examen) return;
        if (correctSet.has(i)) c.classList.add("is-correct");
        else if (chosen.has(i)) c.classList.add("is-wrong");
      });
      showFeedback(correct, q.explication);
      answered(q, correct);
    }

    function submitTrou() {
      const { q, input } = state._els;
      if (state.answered || !input.value.trim()) return;
      state.answered = true;
      if (state._validateBtn) state._validateBtn.disabled = true;
      input.disabled = true;
      const correct = q.reponses_texte.some((r) => normalize(r) === normalize(input.value));
      track[queue[pos]].reponse = input.value.trim();
      if (!correct && q.type === "QRC" && options.corrigerQrc && !examen) {
        corrigerAvecClaude(q, input);
        return;
      }
      if (!examen) input.classList.add(correct ? "is-correct" : "is-wrong");
      let exp = q.explication;
      if (!correct) {
        exp = '<p>Réponse attendue : <b>' + q.reponses_texte.map(escapeHtml).join(" / ") + "</b></p>" + exp;
      } else if (q.type === "QRC") {
        // Synonymes / traductions : montrer les autres réponses acceptées.
        const autres = q.reponses_texte.filter((r) => normalize(r) !== normalize(input.value));
        if (autres.length) exp = '<p>Autres réponses acceptées : ' + autres.map(escapeHtml).join(" / ") + "</p>" + exp;
      }
      showFeedback(correct, exp);
      answered(q, correct);
    }

    // Réponse rédigée (QRC) différente du corrigé : Claude (Haiku) juge le fond ;
    // sans Claude (plafond, panne), correction stricte.
    async function corrigerAvecClaude(q, input) {
      const { feedback } = state._els;
      feedback.innerHTML = '<div class="quiz-verdict muted">✦ Claude lit ta réponse…</div>';
      const attendu = '<p>Réponse attendue : <b>' + q.reponses_texte.map(escapeHtml).join(" / ") + "</b></p>";
      let r = null;
      try { r = await options.corrigerQrc(q, input.value.trim()); } catch (e) { r = null; }
      if (!r || !r.verdict) {
        input.classList.add("is-wrong");
        showFeedback(false, attendu + q.explication);
        answered(q, false);
        return;
      }
      const ok = r.verdict === "juste";
      input.classList.add(ok ? "is-correct" : r.verdict === "partiel" ? "is-close" : "is-wrong");
      const retour = '<p class="quiz-retour-claude">✦ ' + escapeHtml(r.retour || "") + "</p>";
      showFeedback(ok, retour + (ok ? "" : attendu) + q.explication,
        r.verdict === "partiel" ? { texte: "≈ Partiel", classe: "close" } : null);
      answered(q, ok);
    }

    function revealCard(verso, flip) {
      const { q } = state._els;
      if (state.answered) return;
      state.answered = true;
      verso.style.display = "";
      flip.style.display = "none";
      // Image masquée : la zone à deviner se découvre.
      verso.parentNode.querySelectorAll("[data-masque]").forEach((m) => m.classList.add("revele"));
      renderMath(verso);
      // Auto-évaluation : à revoir (repart de zéro), difficile (même intervalle),
      // bien (intervalle suivant) ou facile (saute un intervalle).
      const self = el("div", "quiz-selfeval");
      for (const [note, label, cls] of NOTES) {
        const b = el("button", "btn " + cls, label);
        b.addEventListener("click", () => gradeCard(note));
        self.appendChild(b);
      }
      state._els.actions.insertBefore(self, state._els.nextBtn);
      state._selfeval = self;
      if (q.explication) {
        const exp = el("div", "quiz-explication markdown", q.explication);
        verso.after(exp);
        renderMath(exp);
      }
    }

    function gradeCard(note) {
      const { q, nextBtn } = state._els;
      if (state._selfeval) state._selfeval.remove();
      state._selfeval = null;
      nextBtn.style.display = "";
      nextBtn.focus();
      answered(q, note !== "a_revoir", note);
    }

    function next() {
      pos++;
      if (pos < queue.length) build();
      else results();
    }

    function results() {
      if (termine) return;
      termine = true;
      if (chrono) clearInterval(chrono);
      stage.replaceChildren();
      barFill.style.width = "100%";
      progressText.textContent = "Terminé";
      progressText.classList.remove("quiz-temps-court");
      const pct = questions.length ? Math.round(score / questions.length * 100) : 0;
      const box = el("div", "quiz-results");
      if (examen) {
        resultatsExamen(box);
        stage.appendChild(box);
        return;
      }
      box.appendChild(el("div", "quiz-score", score + " / " + questions.length));
      box.appendChild(el("div", "mastery", '<div class="mastery-fill" style="width:' + pct + '%"></div>'));
      const msg = pct >= 80 ? "Excellent 🎉" : pct >= 50 ? "Bien, continuez !" : "À retravailler 💪";
      box.appendChild(el("p", "muted", msg + " " + (options.messageFin || "La progression a été enregistrée.")));
      if (intensif) {
        box.querySelector(".quiz-score").textContent = "Tout est su ✓";
        box.appendChild(el("p", "",
          "Du premier coup : <b>" + score + " / " + questions.length + "</b> · " +
          "<b>" + queue.length + "</b> réponses au total pour tout maîtriser."));
        const hardest = questions
          .map((q, i) => ({ q, misses: track[i].misses }))
          .filter((h) => h.misses)
          .sort((a, b) => b.misses - a.misses)
          .slice(0, 10);
        if (hardest.length) {
          const wrap = el("div", "quiz-hardest");
          wrap.appendChild(el("p", "", "<b>Les plus difficiles</b> — elles reviendront dans la Révision intelligente :"));
          const ul = el("ul");
          hardest.forEach((h) => {
            const text = el("div", "", h.q.enonce).textContent.trim();
            const li = el("li");
            li.textContent = (text.length > 90 ? text.slice(0, 90) + "…" : text) + " — " + h.misses + " erreur" + (h.misses > 1 ? "s" : "");
            ul.appendChild(li);
          });
          wrap.appendChild(ul);
          box.appendChild(wrap);
        }
        box.appendChild(el("p", "muted", "Refaites une Révision intelligente demain pour ancrer ce que vous avez appris."));
      }
      const actions = el("div", "quiz-actions");
      const again = el("a", "btn btn-primary", "Retour aux sets");
      again.href = backUrl;
      actions.appendChild(again);
      box.appendChild(actions);
      stage.appendChild(box);
    }

    // Examen : note sur 20, puis la correction de chaque question.
    function resultatsExamen(box) {
      const total = questions.length;
      const repondues = track.filter((t) => t.attempts).length;
      const note = total ? Math.round(score / total * 20 * 10) / 10 : 0;
      box.appendChild(el("div", "quiz-score", String(note).replace(".", ",") + " / 20"));
      box.appendChild(el("div", "mastery", '<div class="mastery-fill" style="width:' + Math.round(note / 20 * 100) + '%"></div>'));
      let bilan = "<b>" + score + "</b> bonne" + (score > 1 ? "s" : "") + " réponse" + (score > 1 ? "s" : "") + " sur " + total;
      if (repondues < total) bilan += " · <b>" + (total - repondues) + "</b> sans réponse (temps écoulé)";
      box.appendChild(el("p", "", bilan));
      const msg = note >= 16 ? "Excellent 🎉" : note >= 12 ? "Bien, continuez !" : note >= 10 ? "Juste la moyenne : à consolider." : "À retravailler 💪";
      box.appendChild(el("p", "muted", msg + " Les réponses comptent pour la révision espacée."));
      const liste = el("ol", "quiz-correction");
      questions.forEach((q, i) => {
        const t = track[i];
        const juste = t.attempts && t.ok;
        const li = el("li", "quiz-correction-item " + (juste ? "ok" : "ko"));
        li.appendChild(el("div", "quiz-correction-enonce markdown", q.enonce));
        const donnee = el("p", "quiz-correction-reponse");
        let texte;
        if (!t.attempts) texte = "— (sans réponse)";
        else if (q.type === "QCM" || q.type === "QCR") texte = (t.reponse || []).map((k) => el("div", "", q.choix[k]).textContent.trim()).join(" · ") || "—";
        else texte = t.reponse || "—";
        donnee.innerHTML = "<b>Votre réponse :</b> " + escapeHtml(texte);
        li.appendChild(donnee);
        if (!juste) {
          const bonne = el("p", "quiz-correction-attendu");
          const attendu = (q.type === "QCM" || q.type === "QCR") ? q.reponses.map((k) => el("div", "", q.choix[k]).textContent.trim()).join(" · ") : q.reponses_texte.join(" / ");
          bonne.innerHTML = "<b>Bonne réponse :</b> " + escapeHtml(attendu);
          li.appendChild(bonne);
        }
        if (q.explication) li.appendChild(el("div", "quiz-explication markdown", q.explication));
        liste.appendChild(li);
      });
      box.appendChild(liste);
      renderMath(box);
      const actions = el("div", "quiz-actions");
      const again = el("a", "btn btn-primary", "Retour aux sets");
      again.href = backUrl;
      actions.appendChild(again);
      box.appendChild(actions);
    }

    function escapeHtml(s) {
      const d = document.createElement("div");
      d.textContent = s;
      return d.innerHTML;
    }

    // -- Clavier -- (en phase de capture : les touches du jeu passent avant les raccourcis
    // de Hangar, ex. « c » ou « t » qui ouvriraient une fenêtre de création)
    window.addEventListener("keydown", (e) => {
      if (!questions.length || pos >= queue.length || termine) return;
      if (e.ctrlKey || e.metaKey || e.altKey || document.querySelector("dialog[open]")) return;
      const q = current();
      if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA" || e.target.isContentEditable) return; // laisser taper dans les TROU/QRC
      const key = e.key.toLowerCase();
      const pris = () => { e.preventDefault(); e.stopPropagation(); };

      if (state.answered) {
        if (q.type === "FLASHCARD" && state._selfeval) {
          const note = TOUCHES_NOTES[key];
          if (note) { pris(); gradeCard(note); return; }
        }
        if (e.key === "Enter" || e.key === " ") { pris(); if (state._els.nextBtn.style.display !== "none") next(); }
        return;
      }

      if (q.type === "FLASHCARD") {
        if (e.key === " " || e.key === "Enter") { pris(); if (state._flip) state._flip(); }
        return;
      }
      if (q.type === "QCM" || q.type === "QCR") {
        const k = e.key.length === 1 ? LETTERS.indexOf(e.key.toUpperCase()) : -1;
        if (k >= 0 && k < q.choix.length) { pris(); state._toggle(state._order[k]); return; }
        if (/^[1-9]$/.test(e.key) && Number(e.key) <= q.choix.length) { pris(); state._toggle(state._order[Number(e.key) - 1]); return; }
        if (e.key === "Enter" || e.key === " ") { pris(); submitChoices(); }
      }
    }, { signal: session.signal, capture: true });

    if (questions.length) build();
  }

  function arreter() {
    if (sessionPrecedente) sessionPrecedente.abort();
    sessionPrecedente = null;
  }

  window.QuizMoteur = { demarrer, arreter };

  // Page « jouer » : questions fournies par le serveur, réponses envoyées tout de
  // suite ; si l'envoi échoue (connexion perdue, session expirée), la réponse est
  // gardée sur l'appareil (localStorage) et renvoyée, avec sa date, au prochain chargement.
  const root = document.getElementById("quiz");
  const stage = document.getElementById("quiz-stage");
  const dataTag = document.getElementById("quiz-data");
  if (!root || !dataTag) return;

  const CSRF = (window.HANGAR && window.HANGAR.csrf) || "";
  const ENTETES = { "Content-Type": "application/json", "X-CSRF-Token": CSRF, "X-Requested-With": "fetch" };
  const CLE_ATTENTE = "hangar.qcm.attente." + (root.dataset.joueur || "");

  function attente() {
    try { return JSON.parse(localStorage.getItem(CLE_ATTENTE) || "[]"); } catch (e) { return []; }
  }
  function garder(liste) {
    try { if (liste.length) localStorage.setItem(CLE_ATTENTE, JSON.stringify(liste.slice(-500))); else localStorage.removeItem(CLE_ATTENTE); } catch (e) { /* stockage bloqué */ }
  }

  async function envoyer(reponse) {
    const r = await fetch(root.dataset.recordUrl, { method: "POST", headers: ENTETES, body: JSON.stringify(reponse), credentials: "same-origin" });
    // Réponse enregistrée, ou refusée par le serveur (inutile de la renvoyer) ; sinon à garder.
    return (r.ok && !r.redirected) || (r.status >= 400 && r.status < 500 && r.status !== 400);
  }

  async function renvoyerAttente() {
    const liste = attente();
    if (!liste.length) return;
    const restantes = [];
    for (const rep of liste) {
      try { if (!(await envoyer(rep))) restantes.push(rep); } catch (e) { restantes.push(rep); }
    }
    garder(restantes);
    if (liste.length > restantes.length && window.HANGAR && window.HANGAR.toast) {
      const n = liste.length - restantes.length;
      window.HANGAR.toast(n + " réponse" + (n > 1 ? "s" : "") + " hors connexion enregistrée" + (n > 1 ? "s" : ""), "success", 2500);
    }
  }

  async function enregistrer(q, correct, note, dureeMs) {
    const reponse = { set_id: q.set_id, question_id: q.id, correct: correct, note: note, duree_ms: dureeMs };
    try {
      if (await envoyer(reponse)) return;
    } catch (e) { /* hors ligne */ }
    garder(attente().concat([{ ...reponse, date: new Date().toISOString() }]));
  }

  async function poster(url, corps) {
    const r = await fetch(url, { method: "POST", headers: ENTETES, body: JSON.stringify(corps), credentials: "same-origin" });
    const json = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(json.error || ("Erreur " + r.status));
    return json;
  }

  function signaler(q, commentaire) {
    return poster(root.dataset.signalerUrl, { set_id: q.set_id, question_id: q.id, commentaire });
  }

  function corrigerQrc(q, reponse) {
    return poster(root.dataset.corrigerQrcUrl, { set_id: q.set_id, question_id: q.id, reponse });
  }

  async function expliquer(q) {
    try {
      return await poster(root.dataset.expliquerUrl, { set_id: q.set_id, question_id: q.id });
    } catch (e) {
      throw new Error(e.message || "Explication impossible pour l'instant.");
    }
  }

  renvoyerAttente();
  if (!stage) return;

  demarrer({
    stage: stage,
    questions: JSON.parse(dataTag.textContent),
    expliquer: root.dataset.expliquerUrl && root.dataset.mode !== "examen" ? expliquer : null,
    signaler: root.dataset.signalerUrl ? signaler : null,
    corrigerQrc: root.dataset.corrigerQrcUrl && root.dataset.mode !== "examen" ? corrigerQrc : null,
    intensif: root.dataset.mode === "intensif",
    examen: root.dataset.mode === "examen" ? { duree: Number(root.dataset.duree) || 0 } : null,
    backUrl: root.dataset.backUrl,
    enregistrer: enregistrer,
  });
})();

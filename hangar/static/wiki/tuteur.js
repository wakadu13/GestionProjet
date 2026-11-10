// Tuteur d'une page (port de tuteur.js de Revision) : question envoyée à l'IA (/claude/tuteur/),
// réponse ajoutée au panneau « Demander au tuteur » sous la page ; Ctrl+Entrée envoie.
// Écouteurs posés sur le document : le panneau marche aussi après un changement de page en plein écran.
(function () {
  "use strict";
  if (window.wikiTuteurPret) return;
  window.wikiTuteurPret = true;

  function el(tag, cls, texte) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (texte !== undefined) e.textContent = texte;
    return e;
  }

  async function demander(form) {
    const panneau = form.closest("#tuteur");
    const zone = panneau.querySelector("[data-tuteur-echanges]");
    const champ = form.querySelector("textarea");
    const bouton = form.querySelector("button[type=submit]");
    const question = champ.value.trim();
    if (!question || bouton.disabled) return;
    bouton.disabled = true;
    bouton.textContent = "L’IA réfléchit…";
    const bloc = el("div", "tuteur-echange");
    bloc.append(el("p", "tuteur-question", question));
    const reponse = el("div", "tuteur-reponse markdown muted", "…");
    bloc.append(reponse);
    zone.append(bloc);
    bloc.scrollIntoView({ block: "nearest" });
    try {
      const r = await fetch(panneau.dataset.tuteurUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json", Accept: "application/json", "X-Requested-With": "fetch",
          "X-CSRF-Token": (window.HANGAR && window.HANGAR.csrf) || "",
        },
        body: JSON.stringify({ page: panneau.dataset.page, question }),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok || !data.html) throw new Error(data.error || "Réponse impossible pour l’instant (" + r.status + ").");
      reponse.classList.remove("muted");
      reponse.innerHTML = data.html;
      if (window.wikiEnhance) window.wikiEnhance(reponse); // formules, coloration du code
      champ.value = "";
    } catch (e) {
      reponse.classList.remove("muted");
      reponse.classList.add("tuteur-erreur");
      reponse.textContent = e.message;
    } finally {
      bouton.disabled = false;
      bouton.textContent = "Demander";
    }
  }

  document.addEventListener("submit", (e) => {
    const form = e.target.closest && e.target.closest("[data-tuteur-form]");
    if (!form) return;
    e.preventDefault();
    demander(form);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || !(e.ctrlKey || e.metaKey)) return;
    const form = e.target.closest && e.target.closest("[data-tuteur-form]");
    if (!form) return;
    e.preventDefault();
    e.stopPropagation(); // pas l'envoi automatique des formulaires de Hangar (Ctrl+Entrée)
    demander(form);
  }, true);
})();

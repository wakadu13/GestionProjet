// Pages des QCM : catégories repliables (état mémorisé), recherche en direct dans les sets
// (titre, description, catégorie, énoncés), fenêtre « Déplacer », import par glisser-déposer
// et types de cartes retenus. Port de quiz_index.js et quiz_import.js de Revision.
(function () {
  const input = document.querySelector("[data-quiz-search]");
  const empty = document.querySelector("[data-quiz-empty]");
  const categories = Array.from(document.querySelectorAll("details.qcm-cat"));
  const STORAGE_KEY = "hangar.qcm.categories-ouvertes";

  // Même normalisation que quiz.normaliser_texte côté serveur.
  function normalize(text) {
    return text.normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase().split(/\s+/).filter(Boolean);
  }
  function loadOpen() {
    try { return new Set(JSON.parse(localStorage.getItem(STORAGE_KEY)) || []); } catch (e) { return new Set(); }
  }
  function saveOpen() {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(categories.filter((d) => d.open).map((d) => d.dataset.cle))); } catch (e) { /* stockage bloqué */ }
  }

  let searching = false;
  function filter() {
    const words = normalize(input ? input.value : "");
    searching = words.length > 0;
    const remembered = searching ? null : loadOpen();
    let total = 0;
    for (const details of categories) {
      let shown = 0;
      for (const set of details.querySelectorAll(".qcm-set")) {
        const haystack = set.dataset.search || "";
        const match = words.every((w) => haystack.includes(w));
        set.hidden = !match;
        if (match) shown++;
      }
      total += shown;
      details.hidden = shown === 0;
      // Une ancre (#cat-…, #set-…) ouvre sa catégorie.
      const cible = location.hash && (details.id === location.hash.slice(1) || details.querySelector(location.hash.replace(/([^\w#-])/g, "\\$1")));
      details.open = searching ? shown > 0 : (remembered.has(details.dataset.cle) || !!cible);
      const count = details.querySelector("[data-quiz-count]");
      if (count) {
        const unite = count.textContent.includes("paquet") ? "paquet" : "set";
        count.textContent = shown + " " + unite + (shown > 1 ? "s" : "");
      }
    }
    if (empty) empty.hidden = !searching || total > 0;
  }

  if (categories.length) {
    for (const details of categories) details.addEventListener("toggle", () => { if (!searching) saveOpen(); });
    document.querySelectorAll("[data-fold]").forEach((button) => {
      button.addEventListener("click", () => {
        const open = button.dataset.fold === "open";
        categories.forEach((d) => { if (!d.hidden) d.open = open; });
      });
    });
    if (input) {
      input.addEventListener("input", () => {
        filter();
        const url = new URL(location.href);
        if (input.value.trim()) url.searchParams.set("q", input.value.trim()); else url.searchParams.delete("q");
        history.replaceState(null, "", url);
      });
      input.addEventListener("keydown", (e) => {
        if (e.key === "Escape") { input.value = ""; input.dispatchEvent(new Event("input")); }
      });
      input.form.addEventListener("submit", (e) => e.preventDefault());
    }
    try { filter(); } catch (e) { /* ancre illisible : on garde l'affichage du serveur */ }
    if (location.hash) {
      const cible = document.getElementById(decodeURIComponent(location.hash.slice(1)));
      if (cible) setTimeout(() => cible.scrollIntoView({ block: "center" }), 50);
    }
    window.addEventListener("hashchange", () => { try { filter(); } catch (e) { /* ignore */ } });
  }

  // « Déplacer dans un dossier… » : une seule fenêtre, remplie selon le set choisi.
  const dlg = document.getElementById("qcm-deplacer");
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-qcm-deplacer]");
    if (!b || !dlg) return;
    document.querySelectorAll(".dd.open").forEach((d) => d.classList.remove("open"));
    dlg.querySelector("[name=set]").value = b.dataset.set;
    dlg.querySelector("[data-titre]").textContent = b.dataset.titre;
    const champ = dlg.querySelector("[name=dossier]");
    champ.value = "";
    champ.setAttribute("list", b.dataset.liste || "qcm-categories");
    dlg.showModal();
    champ.focus();
  });

  // Import par glisser-déposer : le formulaire part dès que des fichiers sont choisis.
  document.querySelectorAll("[data-quiz-import]").forEach((form) => {
    const zone = form.querySelector("[data-dropzone]");
    const fichiers = form.querySelector("[data-import-files]");
    if (!zone || !fichiers) return;
    const accepte = (f) => /\.(json|apkg)$/i.test(f.name);
    function send(files) {
      const ok = Array.from(files).filter(accepte);
      if (!ok.length) { (window.HANGAR && window.HANGAR.toast ? window.HANGAR.toast("Seuls les fichiers .json et .apkg sont acceptés.", "error") : alert("Seuls les fichiers .json et .apkg sont acceptés.")); return; }
      const transfer = new DataTransfer();
      ok.forEach((f) => transfer.items.add(f));
      fichiers.files = transfer.files;
      zone.classList.add("is-sending");
      const titre = zone.querySelector("b");
      if (titre) titre.textContent = "Import de " + ok.length + " fichier" + (ok.length > 1 ? "s" : "") + "…";
      form.submit();
    }
    fichiers.addEventListener("change", () => { if (fichiers.files.length) send(fichiers.files); });
    ["dragenter", "dragover"].forEach((type) => zone.addEventListener(type, (e) => { e.preventDefault(); zone.classList.add("is-over"); }));
    ["dragleave", "drop"].forEach((type) => zone.addEventListener(type, () => zone.classList.remove("is-over")));
    zone.addEventListener("drop", (e) => {
      e.preventDefault();
      if (e.dataTransfer && e.dataTransfer.files.length) send(e.dataTransfer.files);
    });
    // Un fichier lâché à côté de la zone ne doit pas faire quitter la page.
    window.addEventListener("dragover", (e) => e.preventDefault());
    window.addEventListener("drop", (e) => e.preventDefault());
  });

  // Types de cartes de vocabulaire cochés : retenus sur l'appareil.
  document.querySelectorAll("[data-qcm-cartes]").forEach((form) => {
    const CLE = "hangar.qcm.cartes";
    const cases = [...form.querySelectorAll("input[name=cartes]")];
    try {
      const memo = JSON.parse(localStorage.getItem(CLE) || "null");
      if (Array.isArray(memo) && memo.length) cases.forEach((c) => { c.checked = memo.includes(c.value); });
    } catch (e) { /* stockage bloqué */ }
    form.addEventListener("change", () => {
      try { localStorage.setItem(CLE, JSON.stringify(cases.filter((c) => c.checked).map((c) => c.value))); } catch (e) { /* stockage bloqué */ }
    });
    form.addEventListener("submit", (e) => {
      if (!cases.some((c) => c.checked)) { e.preventDefault(); alert("Coche au moins un type de carte."); }
    });
  });
})();

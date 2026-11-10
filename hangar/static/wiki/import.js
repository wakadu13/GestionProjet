// Import de notes .md (et de leurs images) : glisser-déposer de fichiers ou de
// dossiers entiers, en gardant les sous-dossiers. Les fichiers sont envoyés par
// lots ; le serveur vérifie chacun et renvoie le résultat. Sans JavaScript, le
// formulaire s'envoie normalement.
(function () {
  "use strict";
  const form = document.querySelector("[data-md-import]");
  if (!form) return;
  const zone = form.querySelector("[data-wiki-dropzone]");
  const fileInput = form.querySelector("[data-import-files]");
  const folderInput = form.querySelector("[data-import-folder]");
  const folderButton = form.querySelector("[data-pick-folder]");
  const summary = form.querySelector("[data-summary]");
  const summaryText = form.querySelector("[data-summary-text]");
  const pendingList = form.querySelector("[data-pending]");
  const startButton = form.querySelector("[data-start]");
  const results = document.querySelector("[data-results]");
  const resultsText = document.querySelector("[data-results-text]");
  const resultsList = document.querySelector("[data-results-list]");
  const csrf = form.querySelector("[name=_csrf]").value;
  const claudeBox = form.querySelector("[data-claude]");
  const COURS = /\.(pdf|tex|latex)$/i;

  const NOTE = /\.(md|markdown|tex|latex)$/i;
  const ATTACHMENT = /\.(png|jpe?g|gif|svg|webp|bmp|avif|pdf|mp3|wav|ogg|m4a|flac|webm|3gp|mp4|ogv|mov|mkv)$/i;
  const BATCH_FILES = 20;
  const BATCH_BYTES = 40 * 1024 * 1024;
  const TOO_BIG = "trop volumineux (100 Mo au plus par fichier).";
  let pending = [];
  let skipped = 0;

  if ("webkitdirectory" in folderInput) folderButton.hidden = false;

  function plural(n, word) {
    return `${n} ${word}${n > 1 ? "s" : ""}`;
  }

  function add(entries) {
    for (const { file, path } of entries) {
      const clean = path.replace(/^\/+/, "");
      if (clean.split("/").some((part) => part.startsWith("."))) continue;
      if (!NOTE.test(clean) && !ATTACHMENT.test(clean)) {
        skipped += 1;
        continue;
      }
      if (!pending.some((p) => p.path === clean)) pending.push({ file, path: clean });
    }
    show();
  }

  function destination() {
    return form.elements.dossier.value.trim().replace(/^\/+|\/+$/g, "");
  }

  function show() {
    results.hidden = true;
    summary.hidden = !pending.length && !skipped;
    const notes = pending.filter((p) => NOTE.test(p.path)).length;
    const others = pending.length - notes;
    const parts = [];
    if (notes) parts.push(plural(notes, "page"));
    if (others) parts.push(plural(others, "pièce jointe").replace("pièce jointes", "pièces jointes"));
    const dest = destination();
    const cours = claudeBox && claudeBox.checked ? pending.filter((p) => COURS.test(p.path)).length : 0;
    summaryText.textContent = (parts.length ? `${parts.join(" et ")} à importer ${dest ? `dans « ${dest} »` : "à la racine du wiki"}.` : "Aucun fichier à importer.")
      + (cours ? ` L’IA en fera ${plural(cours, "page")} de cours.` : "")
      + (skipped ? ` ${plural(skipped, "fichier")} d'un autre type ${skipped > 1 ? "seront ignorés" : "sera ignoré"}.` : "");
    pendingList.replaceChildren(...pending.slice(0, 200).map((p) => {
      const li = document.createElement("li");
      li.textContent = (dest ? dest + "/" : "") + p.path.replace(/\.(markdown|tex|latex)$/i, ".md");
      return li;
    }));
    if (pending.length > 200) {
      const li = document.createElement("li");
      li.className = "muted";
      li.textContent = `… et ${pending.length - 200} autres`;
      pendingList.appendChild(li);
    }
    startButton.disabled = !pending.length;
  }

  function clear() {
    pending = [];
    skipped = 0;
    fileInput.value = "";
    folderInput.value = "";
    show();
  }

  // Parcourt un dossier déposé (API « entries » des navigateurs).
  function readEntry(entry) {
    if (entry.isFile) {
      return new Promise((resolve) => entry.file((file) => resolve([{ file, path: entry.fullPath }]), () => resolve([])));
    }
    if (!entry.isDirectory || entry.name.startsWith(".")) return Promise.resolve([]);
    const reader = entry.createReader();
    return new Promise((resolve) => {
      const all = [];
      const next = () => reader.readEntries(async (batch) => {
        if (!batch.length) {
          const nested = await Promise.all(all.map(readEntry));
          return resolve(nested.flat());
        }
        all.push(...batch);
        next();
      }, () => resolve([]));
      next();
    });
  }

  async function dropped(dataTransfer) {
    const items = [...(dataTransfer.items || [])];
    const entries = items.map((item) => item.webkitGetAsEntry && item.webkitGetAsEntry()).filter(Boolean);
    if (entries.length) {
      add((await Promise.all(entries.map(readEntry))).flat());
    } else {
      add([...dataTransfer.files].map((file) => ({ file, path: file.name })));
    }
  }

  function batches() {
    const out = [];
    let current = [];
    let size = 0;
    for (const p of pending) {
      if (current.length && (current.length >= BATCH_FILES || size + p.file.size > BATCH_BYTES)) {
        out.push(current);
        current = [];
        size = 0;
      }
      current.push(p);
      size += p.file.size;
    }
    if (current.length) out.push(current);
    return out;
  }

  function showResult(r) {
    const li = document.createElement("li");
    li.className = r.ok ? "ok" : r.ignored ? "muted" : "ko";
    if (r.ok && r.url) {
      const a = document.createElement("a");
      a.href = r.url;
      a.textContent = r.path;
      li.appendChild(a);
    } else {
      li.appendChild(document.createTextNode(r.path || r.name));
    }
    li.appendChild(document.createTextNode(` — ${r.message}`));
    if (r.claude) {
      const suivi = document.createElement("a");
      suivi.href = r.claude;
      suivi.textContent = "suivre";
      li.append(" (", suivi, ")");
    }
    resultsList.appendChild(li);
  }

  async function send() {
    const groups = batches();
    const total = pending.length;
    let done = 0;
    let ok = 0;
    let failed = 0;
    startButton.disabled = true;
    zone.classList.add("is-sending");
    resultsList.replaceChildren();
    results.hidden = false;
    for (const group of groups) {
      resultsText.textContent = `Import en cours… ${done}/${total}`;
      const data = new FormData();
      data.append("_csrf", csrf);
      data.append("dossier", destination());
      if (form.elements.remplacer.checked) data.append("remplacer", "1");
      if (claudeBox && claudeBox.checked) data.append("claude", "1");
      const lotBox = form.querySelector("[data-lot]");
      if (claudeBox && claudeBox.checked && lotBox && lotBox.checked) data.append("lot", "1");
      for (const p of group) {
        data.append("fichiers", p.file, p.file.name);
        data.append("chemins", p.path);
      }
      let json;
      try {
        const response = await fetch(form.action, { method: "POST", body: data, headers: { Accept: "application/json" } });
        // Refusé avant le wiki (Cloudflare : 100 Mo au plus par envoi).
        if (response.status === 413) throw new Error(TOO_BIG);
        json = await response.json();
        if (!response.ok) throw new Error(json.error || "Import refusé.");
      } catch (e) {
        const message = e instanceof SyntaxError ? "Réponse inattendue du serveur (session expirée ?)." : e.message || "Serveur injoignable.";
        json = { results: group.map((p) => ({ name: p.path, ok: false, message })) };
      }
      for (const r of json.results) {
        showResult(r);
        if (r.ok) ok += 1;
        else if (!r.ignored) failed += 1;
      }
      done += group.length;
    }
    zone.classList.remove("is-sending");
    resultsText.textContent = `Terminé : ${plural(ok, "fichier")} importé${ok > 1 ? "s" : ""}`
      + (failed ? `, ${plural(failed, "fichier")} refusé${failed > 1 ? "s" : ""} (détails ci-dessous).` : ".");
    pending = [];
    skipped = 0;
    fileInput.value = "";
    folderInput.value = "";
    summary.hidden = true;
  }

  fileInput.addEventListener("change", () => add([...fileInput.files].map((file) => ({ file, path: file.name }))));
  folderInput.addEventListener("change", () => add([...folderInput.files].map((file) => ({ file, path: file.webkitRelativePath || file.name }))));
  folderButton.addEventListener("click", () => folderInput.click());
  form.elements.dossier.addEventListener("input", () => pending.length && show());
  // Case « Transformer avec Claude » : le choix est retenu sur l'appareil.
  if (claudeBox) {
    try { if (localStorage.getItem("wiki-import-claude") === "0") claudeBox.checked = false; } catch (e) { /* stockage bloqué */ }
    claudeBox.addEventListener("change", () => {
      try { localStorage.setItem("wiki-import-claude", claudeBox.checked ? "1" : "0"); } catch (e) { /* stockage bloqué */ }
      if (pending.length) show();
    });
  }
  form.querySelector("[data-clear]").addEventListener("click", clear);
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    if (pending.length) send();
  });

  ["dragenter", "dragover"].forEach((type) => zone.addEventListener(type, (e) => {
    e.preventDefault();
    zone.classList.add("is-over");
  }));
  ["dragleave", "drop"].forEach((type) => zone.addEventListener(type, () => zone.classList.remove("is-over")));
  zone.addEventListener("drop", (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (e.dataTransfer) dropped(e.dataTransfer);
  });

  // Un fichier lâché à côté de la zone ne doit pas faire quitter la page.
  window.addEventListener("dragover", (e) => e.preventDefault());
  window.addEventListener("drop", (e) => {
    e.preventDefault();
    if (e.dataTransfer && e.dataTransfer.files.length) dropped(e.dataTransfer);
  });
})();

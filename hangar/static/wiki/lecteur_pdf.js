// Lecteur de PDF du wiki, construit sur PDF.js (Mozilla), regroupé dans
// vendor/pdfjs/ (voir bibliotheques/) : défilement continu, zoom, recherche dans
// le texte (sans tenir compte des accents), sommaire du document, liens
// internes, plein écran, reprise à la dernière page lue. Contrairement au
// lecteur du navigateur, il s'affiche aussi dans les notes sur téléphone.
// Repris de Revision ; dans Hangar, le jeton de sécurité part dans l'en-tête X-CSRF-Token.
import {
  EventBus, FindState, LinkTarget, PDFFindController, PDFLinkService, PDFViewer, pdfjsLib,
} from "./vendor/pdfjs/pdf.min.js";

const PRESETS = ["auto", "page-width", "page-fit", "page-actual"];

function stored(key, fallback) {
  try {
    return localStorage.getItem(key) ?? fallback;
  } catch (e) {
    return fallback;
  }
}

function store(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch (e) {
    // Stockage indisponible (navigation privée) : rien n'est mémorisé.
  }
}

async function openReader(reader) {
  const el = (name) => reader.querySelector(`[data-pdf="${name}"]`);
  const container = el("container");
  const statusLine = el("status");
  const pageInput = el("page");
  const zoom = el("zoom");
  const find = el("find");
  const findCount = el("find-count");
  const embedded = reader.classList.contains("is-embedded");
  const { src, key, worker, wasm } = reader.dataset;
  const pageKey = `wiki-pdf-page:${key}`;

  pdfjsLib.GlobalWorkerOptions.workerSrc = worker;
  const eventBus = new EventBus();
  const linkService = new PDFLinkService({ eventBus, externalLinkTarget: LinkTarget.BLANK, externalLinkRel: "noopener noreferrer" });
  const findController = new PDFFindController({ eventBus, linkService });
  const viewer = new PDFViewer({
    container,
    eventBus,
    linkService,
    findController,
    textLayerMode: 1,
    annotationEditorMode: pdfjsLib.AnnotationEditorType.DISABLE,
  });
  linkService.setViewer(viewer);

  let pdf;
  try {
    pdf = await pdfjsLib.getDocument({ url: src, wasmUrl: wasm, isEvalSupported: false, enableXfa: false }).promise;
  } catch (e) {
    statusLine.textContent = e && e.name === "PasswordException"
      ? "Ce PDF est protégé par un mot de passe : téléchargez-le pour l'ouvrir."
      : "Impossible d'afficher ce PDF (fichier abîmé ou illisible). Vous pouvez le télécharger.";
    statusLine.classList.add("is-error");
    return;
  }
  viewer.setDocument(pdf);
  linkService.setDocument(pdf, null);
  el("count").textContent = `/ ${pdf.numPages}`;
  pageInput.max = pdf.numPages;

  // -- Pages et zoom ------------------------------------------------------------

  const hashPage = () => {
    const m = /[#&]page=(\d+)/.exec(window.location.hash);
    return m ? Number(m[1]) : 0;
  };
  const clampPage = (n) => Math.min(Math.max(Math.round(n) || 1, 1), pdf.numPages);

  function showPage(pageNumber) {
    pageInput.value = pageNumber;
    el("prev").disabled = pageNumber <= 1;
    el("next").disabled = pageNumber >= pdf.numPages;
  }

  eventBus.on("pagesinit", () => {
    // Dans une note ou sur un petit écran : la page tient dans la largeur.
    const narrow = container.clientWidth < 640;
    viewer.currentScaleValue = embedded || narrow ? "page-width" : stored("wiki-pdf-zoom", "auto");
    const start = hashPage() || (embedded ? 0 : Number(stored(pageKey, 0)));
    if (start > 1) viewer.currentPageNumber = clampPage(start);
    showPage(viewer.currentPageNumber);
    statusLine.hidden = true;
  });

  eventBus.on("pagechanging", ({ pageNumber }) => {
    showPage(pageNumber);
    if (!embedded) store(pageKey, String(pageNumber));
  });

  eventBus.on("scalechanging", ({ scale, presetValue }) => {
    const custom = zoom.querySelector('[value="custom"]');
    const match = presetValue || [...zoom.options].find((o) => o.value !== "custom" && !PRESETS.includes(o.value)
      && Math.abs(Number(o.value) - scale) < 0.001)?.value;
    if (match) {
      zoom.value = match;
      custom.hidden = true;
    } else {
      custom.textContent = `${Math.round(scale * 100)} %`;
      custom.hidden = false;
      zoom.value = "custom";
    }
    if (!embedded) store("wiki-pdf-zoom", presetValue || String(scale));
  });

  el("prev").addEventListener("click", () => viewer.previousPage());
  el("next").addEventListener("click", () => viewer.nextPage());
  pageInput.addEventListener("change", () => {
    viewer.currentPageNumber = clampPage(Number(pageInput.value));
    pageInput.value = viewer.currentPageNumber;
  });
  el("zoom-in").addEventListener("click", () => viewer.increaseScale());
  el("zoom-out").addEventListener("click", () => viewer.decreaseScale());
  zoom.addEventListener("change", () => {
    if (zoom.value !== "custom") viewer.currentScaleValue = zoom.value;
  });

  // Ctrl+molette : zoom du PDF plutôt que de toute la page.
  container.addEventListener("wheel", (e) => {
    if (!e.ctrlKey && !e.metaKey) return;
    e.preventDefault();
    if (e.deltaY < 0) viewer.increaseScale();
    else viewer.decreaseScale();
  }, { passive: false });

  // Zoom « largeur de page » ou « auto » recalculé quand la place change.
  let resizeTimer = null;
  function refit() {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      const value = viewer.currentScaleValue;
      if (PRESETS.includes(value)) viewer.currentScaleValue = value;
    }, 100);
  }
  window.addEventListener("resize", refit);
  if (window.ResizeObserver) new ResizeObserver(refit).observe(container);

  // -- Recherche ------------------------------------------------------------------

  let findTimer = null;
  function search(again = false, previous = false) {
    const query = find.value.trim();
    if (!query) {
      findCount.textContent = "";
      eventBus.dispatch("findbarclose", { source: null });
      return;
    }
    eventBus.dispatch("find", {
      source: null, type: again ? "again" : "", query, caseSensitive: false, entireWord: false,
      highlightAll: true, findPrevious: previous, matchDiacritics: false,
    });
  }
  function showCount({ current, total }) {
    findCount.textContent = total ? `${current} / ${total}` : "";
  }
  find.addEventListener("input", () => {
    clearTimeout(findTimer);
    findTimer = setTimeout(() => search(), 250);
  });
  find.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      clearTimeout(findTimer);
      search(true, e.shiftKey);
    } else if (e.key === "Escape") {
      find.value = "";
      search();
      container.focus();
    }
  });
  eventBus.on("updatefindmatchescount", ({ matchesCount }) => showCount(matchesCount));
  eventBus.on("updatefindcontrolstate", ({ state, matchesCount }) => {
    if (state === FindState.NOT_FOUND) findCount.textContent = "Aucun résultat";
    else if (state === FindState.PENDING) findCount.textContent = "…";
    else showCount(matchesCount);
  });

  // -- Clavier et plein écran --------------------------------------------------------

  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === "f") {
      e.preventDefault();
      find.focus();
      find.select();
      return;
    }
    if (e.ctrlKey || e.metaKey || e.altKey || e.target.closest("input, textarea, select, [contenteditable]")) return;
    // Flèches gauche / droite : page précédente / suivante (sauf si le PDF déborde en largeur).
    if (container.scrollWidth > container.clientWidth + 1) return;
    if (e.key === "ArrowLeft") {
      e.preventDefault();
      viewer.previousPage();
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      viewer.nextPage();
    }
  });

  const fullscreen = el("fullscreen");
  if (!document.fullscreenEnabled) fullscreen.hidden = true;
  fullscreen.addEventListener("click", () => {
    if (document.fullscreenElement) document.exitFullscreen();
    else reader.requestFullscreen().catch(() => {});
  });
  document.addEventListener("fullscreenchange", refit);

  // -- Sommaire du document (panneau de droite) ------------------------------------------

  const panel = document.querySelector("[data-pdf-outline-panel]");
  if (!embedded && panel) {
    const outline = await pdf.getOutline();
    if (outline && outline.length) {
      panel.querySelector("[data-pdf-outline]").replaceChildren(outlineList(outline, linkService));
      panel.hidden = false;
    }
  }
  if (!embedded && reader.dataset.canEdit) annotations(reader, container, key);
}

// -- Passage sélectionné → carte de révision ou citation dans une page (admins) ----------------

function annotations(reader, container, key) {
  const frame = reader.querySelector(".pdf-frame");
  const dialog = document.getElementById("pdf-annotation-dialog");
  if (!frame || !dialog) return;
  const outils = document.createElement("div");
  outils.className = "pdf-selection-outils";
  outils.hidden = true;
  const bouton = document.createElement("button");
  bouton.type = "button";
  bouton.className = "btn btn-sm btn-primary";
  bouton.textContent = "🃏 Garder ce passage";
  bouton.title = "Carte de révision ou citation dans une page du wiki";
  outils.append(bouton);
  frame.append(outils);
  let selection = { texte: "", page: 0 };
  let timer = null;

  document.addEventListener("selectionchange", () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      const sel = window.getSelection();
      const texte = sel && sel.rangeCount ? sel.toString().replace(/\s+/g, " ").trim() : "";
      const noeud = sel && sel.rangeCount ? sel.getRangeAt(0).commonAncestorContainer : null;
      const el = noeud && (noeud.nodeType === 1 ? noeud : noeud.parentElement);
      if (!texte || !el || !container.contains(el)) {
        outils.hidden = true;
        return;
      }
      const pageEl = el.closest(".page");
      selection = { texte, page: pageEl ? Number(pageEl.dataset.pageNumber) || 0 : 0 };
      const rect = sel.getRangeAt(0).getBoundingClientRect();
      const cadre = frame.getBoundingClientRect();
      outils.style.left = `${Math.max(8, Math.min(rect.left - cadre.left, cadre.width - 220))}px`;
      outils.style.top = `${Math.max(8, rect.top - cadre.top - 40)}px`;
      outils.hidden = false;
    }, 150);
  });

  const champ = (s) => dialog.querySelector(s);
  const cible = () => dialog.querySelector("input[name=annotation-cible]:checked").value;
  function basculer() {
    const carte = cible() === "carte";
    champ("[data-annotation-recto-label]").hidden = !carte;
    champ("[data-annotation-recto]").hidden = !carte;
    champ("[data-annotation-page-label]").hidden = carte;
    champ("[data-annotation-page]").hidden = carte;
  }
  dialog.querySelectorAll("input[name=annotation-cible]").forEach((r) => r.addEventListener("change", basculer));
  champ("[data-annotation-fermer]").addEventListener("click", () => dialog.close());
  bouton.addEventListener("mousedown", (e) => e.preventDefault()); // garde la sélection
  bouton.addEventListener("click", () => {
    champ("[data-annotation-extrait]").textContent = selection.texte;
    champ("[data-annotation-page-pdf]").textContent = selection.page || "?";
    champ("[data-annotation-recto]").value = "";
    if (!champ("[data-annotation-page]").value) champ("[data-annotation-page]").value = reader.dataset.pageCours || "";
    basculer();
    outils.hidden = true;
    dialog.showModal();
  });

  function retour(message, url, erreur) {
    let toast = frame.querySelector(".pdf-annotation-retour");
    if (!toast) {
      toast = document.createElement("div");
      toast.className = "pdf-annotation-retour";
      frame.append(toast);
    }
    toast.replaceChildren(document.createTextNode(message + " "));
    if (url) {
      const a = document.createElement("a");
      a.href = url;
      a.textContent = "Voir";
      toast.append(a);
    }
    toast.classList.toggle("is-error", !!erreur);
    toast.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { toast.hidden = true; }, 7000);
  }

  champ("[data-annotation-ok]").addEventListener("click", async () => {
    const ok = champ("[data-annotation-ok]");
    ok.disabled = true;
    try {
      const r = await fetch(reader.dataset.annotationUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": reader.dataset.csrf, "X-Requested-With": "fetch" },
        body: JSON.stringify({
          pdf: key, page: selection.page, texte: selection.texte, cible: cible(),
          recto: champ("[data-annotation-recto]").value.trim(), page_wiki: champ("[data-annotation-page]").value.trim(),
        }),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.error || `Enregistrement impossible (${r.status}).`);
      dialog.close();
      retour(data.message, data.url);
    } catch (e) {
      retour(e.message, null, true);
    } finally {
      ok.disabled = false;
    }
  });
}

function outlineList(items, linkService) {
  const ul = document.createElement("ul");
  for (const item of items) {
    const li = document.createElement("li");
    let entry;
    if (item.url && /^(https?|mailto):/i.test(item.url)) {
      entry = document.createElement("a");
      entry.href = item.url;
      entry.target = "_blank";
      entry.rel = "noopener noreferrer";
    } else {
      entry = document.createElement("button");
      entry.type = "button";
      entry.className = "link-button";
      entry.addEventListener("click", () => item.dest && linkService.goToDestination(item.dest));
    }
    entry.textContent = item.title;
    entry.title = item.title;
    li.appendChild(entry);
    if (item.items && item.items.length) li.appendChild(outlineList(item.items, linkService));
    ul.appendChild(li);
  }
  return ul;
}

const reader = document.querySelector("[data-pdf-reader]");
if (reader) openReader(reader);

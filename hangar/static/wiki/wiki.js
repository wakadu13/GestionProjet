// Wiki de Hangar — améliorations côté navigateur (repris de Revision, wiki/static/wiki/wiki.js) :
// coloration du code, formules (KaTeX) et diagrammes (Mermaid), chargés seulement si la page en
// contient, depuis les copies locales de static/vendor/ (aucun CDN) ; impression ; Ctrl+E.
(function () {
  "use strict";
  const VENDOR = (window.WIKI && window.WIKI.vendor) || "/static/vendor/";
  const loaded = {};

  function loadScript(src) {
    if (!loaded[src]) {
      loaded[src] = new Promise((resolve, reject) => {
        const s = document.createElement("script");
        s.src = src;
        s.onload = resolve;
        s.onerror = reject;
        document.head.appendChild(s);
      });
    }
    return loaded[src];
  }

  function loadStyle(href) {
    if (document.querySelector(`link[href="${href}"]`)) return;
    const l = document.createElement("link");
    l.rel = "stylesheet";
    l.href = href;
    document.head.appendChild(l);
  }

  // Thème : celui de Hangar (attribut data-theme de <html> : light, dark ou system),
  // changé par le bouton de la barre du haut. On suit ses changements pour la coloration
  // du code, les diagrammes et l'éditeur (événement « wiki:theme »).
  const systemeSombre = window.matchMedia("(prefers-color-scheme: dark)");
  const theme = {
    resolved() {
      const t = document.documentElement.dataset.theme;
      if (t === "dark" || t === "light") return t;
      return systemeSombre.matches ? "dark" : "light";
    },
    apply() {
      const actuel = theme.resolved();
      document.querySelectorAll("link[data-theme-css]").forEach((l) => { l.media = l.dataset.themeCss === actuel ? "all" : "not all"; });
      window.dispatchEvent(new CustomEvent("wiki:theme", { detail: { theme: actuel } }));
    },
  };
  window.wikiTheme = theme;
  theme.apply();
  systemeSombre.addEventListener("change", () => theme.apply());
  new MutationObserver(() => theme.apply()).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

  // Sommaire : boutons « Tout déplier » / « Tout replier ».
  document.addEventListener("click", (e) => {
    const b = e.target.closest && e.target.closest("[data-fold]");
    if (!b) return;
    const open = b.dataset.fold === "open";
    document.querySelectorAll(".wiki-sommaire details").forEach((d) => { d.open = open; });
  });

  // Impression : bouton « Imprimer » ; encadrés repliés ouverts le temps de l'impression.
  document.addEventListener("click", (e) => {
    if (e.target.closest && e.target.closest("[data-imprimer]")) window.print();
  });
  let replies = [];
  window.addEventListener("beforeprint", () => {
    replies = [...document.querySelectorAll(".markdown details:not([open])")];
    replies.forEach((d) => { d.open = true; });
  });
  window.addEventListener("afterprint", () => {
    replies.forEach((d) => { d.open = false; });
    replies = [];
  });

  // Ctrl+E sur une page : ouvre l'éditeur (comme Obsidian passe de la lecture à l'édition).
  document.addEventListener("keydown", (e) => {
    if (!(e.ctrlKey || e.metaKey) || e.altKey || e.shiftKey || e.key.toLowerCase() !== "e") return;
    const editLink = document.querySelector("[data-edit-shortcut]");
    if (!editLink || e.target.closest("input, textarea, select, [contenteditable]")) return;
    e.preventDefault();
    window.location.href = editLink.href;
  });

  // Plan de la page : défilement doux dans la zone de contenu, titre courant mis en valeur.
  let planObservateur = null;
  function plan() {
    planObservateur?.disconnect();
    const toc = document.querySelector(".wiki-toc");
    if (!toc) return;
    const liens = [...toc.querySelectorAll("a[href^='#']")];
    const cibles = liens.map((a) => document.getElementById(decodeURIComponent(a.getAttribute("href").slice(1)))).filter(Boolean);
    const scroller = document.querySelector(".main > .content");
    if (cibles.length && scroller && "IntersectionObserver" in window) {
      planObservateur = new IntersectionObserver((entries) => {
        entries.forEach((en) => {
          if (!en.isIntersecting) return;
          liens.forEach((a) => a.classList.toggle("active", decodeURIComponent(a.getAttribute("href").slice(1)) === en.target.id));
        });
      }, { root: scroller, rootMargin: "0px 0px -70% 0px" });
      cibles.forEach((c) => planObservateur.observe(c));
    }
  }
  plan();

  // Diagrammes, coloration du code et formules d'un morceau de page : appelé au
  // chargement, et par l'éditeur pour son mode « Lecture ».
  function enhance(root) {
    // Diagrammes Mermaid (```mermaid).
    const diagrams = root.querySelectorAll("pre > code.language-mermaid");
    if (diagrams.length) {
      const nodes = [];
      diagrams.forEach((code) => {
        const div = document.createElement("div");
        div.className = "mermaid";
        div.textContent = code.textContent;
        code.parentElement.replaceWith(div);
        nodes.push(div);
      });
      loadScript(VENDOR + "mermaid/mermaid.min.js").then(() => {
        window.mermaid.initialize({ startOnLoad: false, theme: theme.resolved() === "dark" ? "dark" : "default", securityLevel: "strict" });
        window.mermaid.run({ nodes });
      }).catch(() => {});
    }

    // Coloration syntaxique.
    const blocks = root.querySelectorAll(".markdown pre code, pre.markdown code");
    if (blocks.length) {
      loadScript(VENDOR + "highlight/highlight.min.js").then(() => {
        blocks.forEach((el) => { if (!el.dataset.highlighted) window.hljs.highlightElement(el); });
      }).catch(() => {});
    }

    // Formules LaTeX ($...$ et $$...$$).
    if (root.querySelector(".math")) {
      loadKatex().then((katex) => {
        root.querySelectorAll(".math").forEach((el) => {
          const block = el.classList.contains("math-block");
          const tex = el.textContent.trim().replace(/^\\[\[(]|\\[\])]$/g, "");
          katex.render(tex, el, { displayMode: block, throwOnError: false });
        });
      }).catch(() => {});
    }
  }

  // KaTeX, chargé une seule fois : pour les pages et pour l'aperçu en direct de l'éditeur.
  let katexReady = null;
  function loadKatex() {
    if (!katexReady) {
      loadStyle(VENDOR + "katex/katex.min.css");
      katexReady = loadScript(VENDOR + "katex/katex.min.js").then(() => window.katex);
    }
    return katexReady;
  }
  window.wikiKatex = loadKatex;
  window.wikiEnhance = enhance;
  // Préparation d'une page : au chargement, et après un changement de page sans rechargement (plein écran).
  window.wikiInitPage = (root) => { enhance(root); plan(); };
  enhance(document);

  // Lecture hors ligne : service worker du wiki (/sw.js, portée « / » ; il ne s'occupe que des adresses de
  // lecture du wiki et des fichiers statiques). Il faut HTTPS, ou l'adresse localhost.
  const W = window.WIKI || {};
  if ("serviceWorker" in navigator && W.sw && window.isSecureContext) {
    navigator.serviceWorker.register(W.sw, { scope: "/" })
      .then(() => navigator.serviceWorker.ready)
      .then(() => {
        // La page qui a enregistré le service worker n'est pas passée par lui : on la garde aussi.
        if (!navigator.serviceWorker.controller && window.caches && W.cache
            && /^\/(wiki|pdf|sommaire|tags|documentation)\//.test(location.pathname)) {
          caches.open(W.cache).then((c) => c.add(location.href)).catch(() => {});
        }
      })
      .catch(() => { /* navigateur sans service worker, ou refusé : pas de lecture hors ligne */ });
  }
})();

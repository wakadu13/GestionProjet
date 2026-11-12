// Éditeur de pages façon Obsidian, construit sur CodeMirror 6 (le moteur
// d'Obsidian, regroupé dans vendor/codemirror.min.js, voir editeur/) :
// - « Aperçu en direct » : la syntaxe Markdown s'efface hors de la ligne en
//   cours (titres en grand, gras, liens, cases à cocher cliquables, images,
//   encadrés colorés) ; « Source » : Markdown brut ; « Lecture » : rendu final ;
// - [[ propose les pages, ![[ les images ; Ctrl+clic ouvre un lien ;
// - Ctrl+S enregistre sans quitter (enregistrement auto en option) ; les titres de
//   pages existantes cités sans lien sont alors proposés (un clic : [[lien]]) ;
// - images collées ou glissées : ajoutées au coffre ; .md glissé ou ouvert :
//   son texte est inséré ; bouton « Télécharger » : la page en .md.
// Si CodeMirror ne se charge pas, la zone de texte simple reste utilisable.
//
// Repris de Revision (wiki/static/wiki/editor.js) pour Hangar : jeton de sécurité
// « _csrf », barre d'outils collée en haut de la zone de contenu (qui défile seule
// dans Hangar), Ctrl+K gardé pour le lien (Ctrl+P ouvre la palette), « Améliorer »
// envoyé au module d'IA avec la clé de la page.
(function () {
  "use strict";
  const textarea = document.getElementById("editor");
  const CM = window.CM;
  if (!textarea || !CM) return;

  const {
    EditorState, EditorSelection, Compartment, Prec, StateField, EditorView, ViewPlugin, Decoration, WidgetType, keymap,
    drawSelection, dropCursor, placeholder, history, historyKeymap, defaultKeymap, indentWithTab,
    syntaxTree, syntaxHighlighting, HighlightStyle, indentOnInput, markdown, markdownLanguage,
    autocompletion, completionKeymap, closeBrackets, closeBracketsKeymap,
    search, searchKeymap, highlightSelectionMatches, tags, Tag, codeLanguage,
  } = CM;

  const form = document.getElementById("editor-form");
  const host = form.querySelector(".editor-cm");
  const preview = form.querySelector(".preview");
  const toolbar = form.querySelector(".toolbar");
  const footer = form.querySelector(".editor-footer");
  const versionInput = form.querySelector("[name=version]");
  // « 1 » une fois qu'un changement a été enregistré depuis cet éditeur : le serveur regroupe
  // alors dans l'historique les enregistrements rapprochés de la même séance.
  const seanceInput = form.querySelector("[name=seance]");
  const csrf = form.querySelector("[name=_csrf]").value;
  const stateLabel = form.querySelector("[data-state]");
  const countLabel = form.querySelector("[data-count]");
  const autosaveBox = form.querySelector("[data-autosave]");
  const closeLink = form.querySelector(".editor-actions a");
  const { previewUrl, importUrl, latexUrl, ameliorerUrl, noteKey, noteName } = form.dataset;
  const choices = JSON.parse(document.getElementById("link-choices").textContent);
  const attachments = JSON.parse(document.getElementById("attachment-choices").textContent);
  const tagChoices = JSON.parse((document.getElementById("tag-choices") || { textContent: "[]" }).textContent);

  // -- Outils -----------------------------------------------------------------

  function normalize(s) {
    return s.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
  }

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
      // Stockage indisponible (navigation privée) : réglage non mémorisé.
    }
  }

  // Identifiant d'un titre, comme le sommaire des pages (slugify_unicode de Python-Markdown).
  function slug(text) {
    return text.replace(/[^\p{L}\p{M}\p{N}_\s-]/gu, "").trim().toLowerCase().replace(/[-\s]+/g, "-");
  }

  function wikiUrl(page) {
    return "/wiki/" + page.split("/").map(encodeURIComponent).join("/");
  }

  const IMAGE_RE = /\.(png|jpe?g|gif|svg|webp|bmp|avif)$/i;
  const CALLOUT_ALIASES = {
    summary: "abstract", tldr: "abstract", hint: "tip", important: "tip", check: "success", done: "success",
    help: "question", faq: "question", caution: "warning", attention: "warning", fail: "failure",
    missing: "failure", error: "danger", cite: "quote",
  };

  // Pages et fichiers existants : un lien vers une page absente s'affiche en pointillés.
  const pageNames = new Set();
  const pageKeys = [];
  for (const c of choices) {
    const name = c.link.split("/").pop();
    const key = (c.folder ? c.folder + "/" : "") + name;
    pageKeys.push(key.toLowerCase());
    [name, key, ...c.aliases].forEach((n) => pageNames.add(n.toLowerCase()));
  }
  const files = new Map();
  function rememberFile(a) {
    const path = ((a.folder ? a.folder + "/" : "") + a.link.split("/").pop()).toLowerCase();
    [a.link.toLowerCase(), path, path.split("/").pop()].forEach((k) => files.has(k) || files.set(k, a));
  }
  attachments.forEach(rememberFile);

  function findFile(target) {
    return files.get(target.trim().toLowerCase()) || null;
  }

  function pageExists(target) {
    const t = target.trim().toLowerCase().replace(/\.md$/, "");
    if (!t || pageNames.has(t) || findFile(t)) return true;
    return t.includes("/") && pageKeys.some((k) => k.endsWith("/" + t));
  }

  // -- Syntaxe Obsidian en plus du Markdown (GFM) ---------------------------------

  const highlightTag = Tag.define();
  const commentTag = Tag.define();
  const hashtagTag = Tag.define();
  const mathTag = Tag.define();
  const wikilinkTag = Tag.define();
  const PUNCTUATION = /[!-/:-@[-`{-~¡-¿‐-‧]/;

  // ==surlignage== : délimiteur comme ~~barré~~.
  const HighlightDelim = { resolve: "Highlight", mark: "HighlightMark" };
  const Highlight = {
    defineNodes: [{ name: "Highlight", style: highlightTag }, { name: "HighlightMark", style: tags.processingInstruction }],
    parseInline: [{
      name: "Highlight",
      parse(cx, next, pos) {
        if (next !== 61 || cx.char(pos + 1) !== 61 || cx.char(pos + 2) === 61) return -1;
        const before = cx.slice(pos - 1, pos), after = cx.slice(pos + 2, pos + 3);
        const sBefore = /\s|^$/.test(before), sAfter = /\s|^$/.test(after);
        const pBefore = PUNCTUATION.test(before), pAfter = PUNCTUATION.test(after);
        return cx.addDelimiter(HighlightDelim, pos, pos + 2,
          !sAfter && (!pAfter || sBefore || pBefore),
          !sBefore && (!pBefore || sAfter || pAfter));
      },
      after: "Emphasis",
    }],
  };

  // [[Page|texte]] et ![[intégration]].
  const WikiLink = {
    defineNodes: [
      { name: "WikiLink", style: wikilinkTag },
      { name: "Embed", style: wikilinkTag },
      { name: "WikiLinkMark", style: tags.processingInstruction },
    ],
    parseInline: [{
      name: "WikiLink",
      parse(cx, next, pos) {
        const embed = next === 33;
        const open = embed ? pos + 1 : pos;
        if ((!embed && next !== 91) || cx.char(open) !== 91 || cx.char(open + 1) !== 91) return -1;
        for (let end = open + 2; end < cx.end - 1; end++) {
          const c = cx.char(end);
          if (c === 10 || c === 91) return -1;
          if (c === 93) {
            if (cx.char(end + 1) !== 93 || end === open + 2) return -1;
            return cx.addElement(cx.elt(embed ? "Embed" : "WikiLink", pos, end + 2, [
              cx.elt("WikiLinkMark", pos, open + 2), cx.elt("WikiLinkMark", end, end + 2),
            ]));
          }
        }
        return -1;
      },
      before: "Link",
    }],
  };

  // %% commentaire %% (invisible sur le wiki).
  const Comment = {
    defineNodes: [{ name: "ObsidianComment", style: commentTag }],
    parseInline: [{
      name: "ObsidianComment",
      parse(cx, next, pos) {
        if (next !== 37 || cx.char(pos + 1) !== 37) return -1;
        const close = cx.slice(pos + 2, cx.end).indexOf("%%");
        return cx.addElement(cx.elt("ObsidianComment", pos, close < 0 ? cx.end : pos + close + 4));
      },
      before: "Emphasis",
    }],
  };

  // #tag et #parent/enfant (pas uniquement des chiffres).
  const TAG_RE = /^#[\p{L}\p{N}_/-]*[\p{L}_/-][\p{L}\p{N}_/-]*/u;
  const Hashtag = {
    defineNodes: [{ name: "Hashtag", style: hashtagTag }],
    parseInline: [{
      name: "Hashtag",
      parse(cx, next, pos) {
        if (next !== 35 || /\S/.test(cx.slice(pos - 1, pos))) return -1;
        const m = TAG_RE.exec(cx.slice(pos, Math.min(cx.end, pos + 200)));
        return m ? cx.addElement(cx.elt("Hashtag", pos, pos + m[0].length)) : -1;
      },
    }],
  };

  // Formules LaTeX, reconnues comme sur le wiki : $...$ dans le texte (pas
  // d'espace contre les $, pas de chiffre après le $ fermant : « 5 $ et 10 $ »
  // n'est pas une formule) et blocs $$...$$, même collés au texte qui précède.
  const closesMath = (text) => /\$\$\s*$/.test(text);
  const Math_ = {
    defineNodes: [
      { name: "InlineMath", style: mathTag },
      { name: "BlockMath", block: true, style: mathTag },
      { name: "MathMark", style: tags.processingInstruction },
    ],
    parseInline: [{
      name: "InlineMath",
      parse(cx, next, pos) {
        if (next !== 36 || /[\\$]/.test(cx.slice(pos - 1, pos))) return -1;
        const rest = cx.slice(pos + 1, cx.end);
        if (rest[0] === "$") {
          const close = rest.indexOf("$$", 1);
          return close > 1 ? cx.addElement(cx.elt("InlineMath", pos, pos + close + 3)) : -1;
        }
        const m = /^(?=[^\s$])[^$\n]+?(?<=[^\s\\])\$(?!\d)/.exec(rest);
        return m ? cx.addElement(cx.elt("InlineMath", pos, pos + 1 + m[0].length)) : -1;
      },
      before: "Emphasis",
    }],
    parseBlock: [{
      name: "BlockMath",
      parse(cx, line) {
        const text = line.text.slice(line.pos);
        if (!text.startsWith("$$")) return false;
        const from = cx.lineStart + line.pos;
        const marks = [cx.elt("MathMark", from, from + 2)];
        let end = cx.lineStart + line.text.length;
        if (closesMath(text.slice(2)) && text.slice(2).replace(/\$\$\s*$/, "").trim()) {
          const at = from + text.lastIndexOf("$$");
          marks.push(cx.elt("MathMark", at, at + 2));
          cx.nextLine();
        } else {
          // Jusqu'au $$ fermant ; un bloc pas encore fermé s'arrête à la ligne vide.
          while (cx.nextLine() && line.depth >= cx.depth) {
            const rest = line.text.slice(line.pos);
            if (!rest.trim()) break;
            for (const m of line.markers) marks.push(m);
            end = cx.lineStart + line.text.length;
            if (closesMath(rest)) {
              const at = cx.lineStart + line.pos + rest.lastIndexOf("$$");
              marks.push(cx.elt("MathMark", at, at + 2));
              cx.nextLine();
              break;
            }
          }
        }
        cx.addElement(cx.elt("BlockMath", from, end, marks));
        return true;
      },
      endLeaf: (cx, line) => line.text.slice(line.pos).startsWith("$$"),
      before: "HorizontalRule",
    }],
  };

  // Texte LaTeX d'un bloc $$ (sans les $$ ni les « > » d'un encadré).
  function mathSource(text) {
    return text.split("\n").map((l, i) => (i ? l.replace(/^\s*(>\s?)+/, "") : l)).join("\n")
      .replace(/^\s*\$\$/, "").replace(/\$\$\s*$/, "").trim();
  }

  // Propriétés YAML entre deux lignes --- en tout début de note.
  const Frontmatter = {
    defineNodes: [{ name: "Frontmatter", block: true, style: tags.meta }, { name: "FrontmatterMark", style: tags.processingInstruction }],
    parseBlock: [{
      name: "Frontmatter",
      parse(cx, line) {
        if (cx.lineStart !== 0 || !/^---[ \t]*$/.test(line.text)) return false;
        const marks = [cx.elt("FrontmatterMark", 0, line.text.length)];
        let end = line.text.length;
        while (cx.nextLine()) {
          end = cx.lineStart + line.text.length;
          if (/^---[ \t]*$/.test(line.text)) {
            marks.push(cx.elt("FrontmatterMark", cx.lineStart, end));
            cx.nextLine();
            break;
          }
        }
        cx.addElement(cx.elt("Frontmatter", 0, end, marks));
        return true;
      },
      before: "HorizontalRule",
    }],
  };

  const obsidianHighlight = HighlightStyle.define([
    { tag: tags.heading1, class: "cmt-heading" },
    { tag: [tags.heading2, tags.heading3, tags.heading4, tags.heading5, tags.heading6], class: "cmt-heading" },
    { tag: tags.strong, class: "cmt-strong" },
    { tag: tags.emphasis, class: "cmt-em" },
    { tag: tags.strikethrough, class: "cmt-strike" },
    { tag: highlightTag, class: "cmt-highlight" },
    { tag: [tags.link, tags.url, wikilinkTag], class: "cmt-link" },
    { tag: tags.monospace, class: "cmt-code" },
    { tag: tags.processingInstruction, class: "cmt-mark" },
    { tag: tags.quote, class: "cmt-quote" },
    { tag: commentTag, class: "cmt-comment" },
    { tag: hashtagTag, class: "cmt-tag" },
    { tag: mathTag, class: "cmt-math" },
    { tag: tags.meta, class: "cmt-meta" },
    { tag: tags.labelName, class: "cmt-label" },
    // Blocs de code
    { tag: [tags.keyword, tags.operatorKeyword, tags.modifier, tags.controlKeyword, tags.definitionKeyword], class: "cmt-keyword" },
    { tag: [tags.string, tags.special(tags.string), tags.regexp, tags.character], class: "cmt-string" },
    { tag: [tags.comment, tags.lineComment, tags.blockComment, tags.docComment], class: "cmt-code-comment" },
    { tag: [tags.number, tags.bool, tags.null, tags.atom], class: "cmt-number" },
    { tag: [tags.function(tags.variableName), tags.function(tags.propertyName), tags.macroName], class: "cmt-function" },
    { tag: [tags.typeName, tags.className, tags.namespace, tags.tagName], class: "cmt-type" },
    { tag: [tags.propertyName, tags.attributeName, tags.special(tags.variableName)], class: "cmt-property" },
  ]);

  // -- Aperçu en direct --------------------------------------------------------------

  class CheckboxWidget extends WidgetType {
    constructor(checked) {
      super();
      this.checked = checked;
    }
    eq(other) {
      return other.checked === this.checked;
    }
    toDOM(view) {
      const box = document.createElement("input");
      box.type = "checkbox";
      box.className = "cm-task";
      box.checked = this.checked;
      box.setAttribute("aria-label", this.checked ? "Tâche faite" : "Tâche à faire");
      box.addEventListener("mousedown", (e) => e.preventDefault());
      box.addEventListener("click", (e) => {
        e.preventDefault();
        const pos = view.posAtDOM(box);
        if (!/^\[.\]$/.test(view.state.sliceDoc(pos, pos + 3))) return;
        view.dispatch({ changes: { from: pos + 1, to: pos + 2, insert: this.checked ? " " : "x" }, userEvent: "input" });
      });
      return box;
    }
    ignoreEvent() {
      return true;
    }
  }

  class TextWidget extends WidgetType {
    constructor(text, className) {
      super();
      this.text = text;
      this.className = className;
    }
    eq(other) {
      return other.text === this.text && other.className === this.className;
    }
    toDOM() {
      const span = document.createElement("span");
      span.className = this.className;
      span.textContent = this.text;
      return span;
    }
  }

  class ImageWidget extends WidgetType {
    constructor(url, width, alt) {
      super();
      this.url = url;
      this.width = width;
      this.alt = alt;
    }
    eq(other) {
      return other.url === this.url && other.width === this.width;
    }
    toDOM() {
      const img = document.createElement("img");
      img.className = "cm-embed-image";
      img.src = this.url;
      img.alt = this.alt;
      if (this.width) img.width = this.width;
      return img;
    }
  }

  // Formule rendue par KaTeX (chargé par wiki.js, comme pour les pages) ; un
  // clic place le curseur dessus et réaffiche le LaTeX.
  class MathWidget extends WidgetType {
    constructor(tex, display) {
      super();
      this.tex = tex;
      this.display = display;
    }
    eq(other) {
      return other.tex === this.tex && other.display === this.display;
    }
    toDOM(view) {
      const el = document.createElement(this.display ? "div" : "span");
      el.className = this.display ? "cm-math-block" : "cm-math-inline";
      el.textContent = this.display ? `$$${this.tex}$$` : `$${this.tex}$`;
      if (window.wikiKatex) {
        window.wikiKatex().then((katex) => {
          try {
            katex.render(this.tex, el, { displayMode: this.display, throwOnError: false });
          } catch (e) {
            // Formule invalide : le LaTeX reste affiché tel quel.
          }
          view.requestMeasure();
        }).catch(() => {});
      }
      return el;
    }
    ignoreEvent() {
      return false;
    }
  }

  const hidden = Decoration.replace({});
  const bullet = Decoration.replace({ widget: new TextWidget("•", "cm-bullet") });
  const rule = Decoration.replace({ widget: new TextWidget("", "cm-hr") });
  const checked = Decoration.replace({ widget: new CheckboxWidget(true) });
  const unchecked = Decoration.replace({ widget: new CheckboxWidget(false) });
  const lineDecos = {};
  function line(cls, attrs) {
    const id = cls + JSON.stringify(attrs || {});
    return (lineDecos[id] ||= Decoration.line({ attributes: Object.assign({ class: cls }, attrs || {}) }));
  }
  function linkMark(cls, href) {
    return Decoration.mark({ class: cls, attributes: href ? { "data-href": href, title: "Ctrl+clic pour ouvrir" } : {} });
  }

  const CALLOUT_RE = /^((?:\s*>)+\s?)\[!([\w-]+)\]([+-]?)[ \t]*(.*)$/;

  // « [!type] » d'un encadré : remplacé par le nom du type, ou masqué s'il y a un titre.
  function calloutMarker(out, lineFrom, m, type) {
    const start = lineFrom + m[1].length;
    const end = start + m[2].length + 3 + m[3].length;
    const after = out.doc.sliceString(end, end + 1) === " " ? end + 1 : end;
    const label = m[4] ? "" : type.charAt(0).toUpperCase() + type.slice(1);
    out.push((label ? Decoration.replace({ widget: new TextWidget(label, "cm-callout-label") }) : hidden).range(start, after));
  }

  function decorate(view, live) {
    const { state } = view;
    const doc = state.doc;
    const ranges = view.hasFocus ? state.selection.ranges : [];
    const touches = (from, to) => ranges.some((r) => r.from <= to && r.to >= from);
    const onLines = (from, to) => touches(doc.lineAt(from).from, doc.lineAt(to).to);
    const out = [];
    out.doc = doc;

    // Une décoration par ligne d'un bloc, en se limitant à la partie visible.
    function eachLine(from, to, viewFrom, viewTo, fn) {
      const first = doc.lineAt(from).number, last = doc.lineAt(to).number;
      const start = Math.max(first, doc.lineAt(viewFrom).number), end = Math.min(last, doc.lineAt(viewTo).number);
      for (let n = start; n <= end; n++) fn(doc.line(n), n === first, n === last);
    }

    for (const { from: viewFrom, to: viewTo } of view.visibleRanges) {
      syntaxTree(state).iterate({
        from: viewFrom,
        to: viewTo,
        enter(ref) {
          const { name, from, to } = ref;
          const node = ref.node;
          const parent = node.parent;
          const heading = /^ATXHeading(\d)$/.exec(name) || /^SetextHeading(\d)$/.exec(name);
          if (heading) {
            out.push(line(`cm-heading cm-h${heading[1]}`).range(doc.lineAt(from).from));
            return;
          }
          switch (name) {
            case "HeaderMark": {
              if (!live || !parent || !parent.name.startsWith("ATX") || onLines(parent.from, parent.to)) return;
              const atStart = from === parent.from;
              const end = atStart && doc.sliceString(to, to + 1) === " " ? to + 1 : to;
              const start = !atStart && doc.sliceString(from - 1, from) === " " ? from - 1 : from;
              if (end > start) out.push(hidden.range(start, end));
              return;
            }
            case "EmphasisMark":
            case "StrikethroughMark":
            case "HighlightMark":
              if (live && parent && !touches(parent.from, parent.to)) out.push(hidden.range(from, to));
              return;
            case "CodeMark":
              if (live && parent && parent.name === "InlineCode" && !touches(parent.from, parent.to)) out.push(hidden.range(from, to));
              return;
            case "QuoteMark": {
              if (!live || onLines(from, to)) return;
              const end = doc.sliceString(to, to + 1) === " " ? to + 1 : to;
              out.push(hidden.range(from, end));
              return;
            }
            case "ListMark": {
              if (!live || !parent || !parent.parent || parent.parent.name !== "BulletList") return;
              const task = parent.getChild("Task");
              const marker = task && task.getChild("TaskMarker");
              if (marker) {
                if (!touches(from, marker.to)) out.push(hidden.range(from, Math.min(to + 1, marker.from)));
              } else if (!touches(from, to + 1)) {
                out.push(bullet.range(from, to));
              }
              return;
            }
            case "TaskMarker":
              if (live && !touches(from, to)) {
                out.push((/x/i.test(doc.sliceString(from + 1, from + 2)) ? checked : unchecked).range(from, to));
              }
              return;
            case "HorizontalRule":
              if (live && !onLines(from, to)) {
                out.push(line("cm-hr-line").range(doc.lineAt(from).from));
                out.push(rule.range(from, to));
              }
              return;
            case "FencedCode":
              eachLine(from, to, viewFrom, viewTo, (l, first, last) => {
                const cls = ["cm-codeblock"];
                if (first) cls.push("cm-codeblock-first", "cm-fence");
                if (last) cls.push("cm-codeblock-last");
                if (last && !first && /^\s*(`{3,}|~{3,})\s*$/.test(l.text)) cls.push("cm-fence");
                out.push(line(cls.join(" ")).range(l.from));
              });
              return;
            case "InlineMath": {
              const text = doc.sliceString(from, to);
              if (!live || touches(from, to) || text.includes("\n")) return false;
              const double = text.startsWith("$$");
              const tex = text.slice(double ? 2 : 1, double ? -2 : -1);
              out.push(Decoration.replace({ widget: new MathWidget(tex, double) }).range(from, to));
              return false;
            }
            case "BlockMath":
              // Rendu par le champ blockMath (un bloc couvre plusieurs lignes) ; ici, le LaTeX brut.
              eachLine(from, to, viewFrom, viewTo, (l) => out.push(line("cm-math-line").range(l.from)));
              return false;
            case "Frontmatter":
              eachLine(from, to, viewFrom, viewTo, (l, first, last) => {
                out.push(line(`cm-frontmatter${first ? " cm-frontmatter-first" : ""}${last ? " cm-frontmatter-last" : ""}`).range(l.from));
              });
              return false;
            case "Table":
              eachLine(from, to, viewFrom, viewTo, (l) => out.push(line("cm-table").range(l.from)));
              return;
            case "Blockquote": {
              const firstLine = doc.lineAt(from);
              const m = CALLOUT_RE.exec(firstLine.text);
              if (parent && parent.name === "Blockquote") {
                // Encadré dans un encadré : seulement le titre.
                if (!m) return;
                const type = CALLOUT_ALIASES[m[2].toLowerCase()] || m[2].toLowerCase();
                const titleFrom = firstLine.to - m[4].length;
                if (m[4]) out.push(Decoration.mark({ class: "cm-callout-nested-title", attributes: { "data-callout": type } }).range(titleFrom, firstLine.to));
                if (live && !touches(firstLine.from, firstLine.to)) calloutMarker(out, firstLine.from, m, type);
                return;
              }
              if (!m) {
                eachLine(from, to, viewFrom, viewTo, (l) => out.push(line("cm-quote").range(l.from)));
                return;
              }
              const type = CALLOUT_ALIASES[m[2].toLowerCase()] || m[2].toLowerCase();
              eachLine(from, to, viewFrom, viewTo, (l, first, last) => {
                const cls = ["cm-callout"];
                if (first) cls.push("cm-callout-title");
                if (last) cls.push("cm-callout-last");
                out.push(line(cls.join(" "), { "data-callout": type }).range(l.from));
              });
              if (live && !touches(firstLine.from, firstLine.to)) calloutMarker(out, firstLine.from, m, type);
              return;
            }
            case "WikiLink":
            case "Embed": {
              const open = name === "Embed" ? 3 : 2;
              const inner = doc.sliceString(from + open, to - 2);
              const bar = inner.indexOf("|");
              const target = bar < 0 ? inner : inner.slice(0, bar);
              const alias = bar < 0 ? "" : inner.slice(bar + 1);
              const page = target.split("#")[0].trim();
              const file = name === "Embed" ? findFile(page) : null;
              const near = touches(from, to);
              if (file && IMAGE_RE.test(page) && live && !near) {
                const width = /^\d+/.exec(alias);
                out.push(Decoration.replace({ widget: new ImageWidget(file.url, width ? +width[0] : 0, page) }).range(from, to));
                return false;
              }
              const heading = target.includes("#") ? target.slice(target.indexOf("#") + 1).replace(/^\^/, "") : "";
              const href = file ? file.url : page ? wikiUrl(page) + (heading ? "#" + encodeURIComponent(slug(heading)) : "") : "";
              const cls = "cm-wikilink" + (pageExists(page) ? "" : " is-unresolved");
              if (!live || near) {
                if (inner) out.push(linkMark(cls, href).range(from + open, to - 2));
                return false;
              }
              const shownFrom = bar < 0 ? from + open : from + open + bar + 1;
              out.push(hidden.range(from, shownFrom));
              if (to - 2 > shownFrom) out.push(linkMark(cls, href).range(shownFrom, to - 2));
              const hash = bar < 0 ? target.indexOf("#") : -1;
              if (hash >= 0) {
                const at = from + open + hash;
                const end = at + (target[hash + 1] === "^" ? 2 : 1);
                out.push((hash ? Decoration.replace({ widget: new TextWidget(" > ", "cm-wikilink-sep") }) : hidden).range(at, end));
              }
              out.push(hidden.range(to - 2, to));
              return false;
            }
            case "Link": {
              if (/^\[![\w-]+\]$/.test(doc.sliceString(from, to))) return false;
              const url = node.getChild("URL");
              let href = url ? doc.sliceString(url.from, url.to).replace(/^<|>$/g, "") : "";
              if (/^[^:/?#]+\.md(#.*)?$/i.test(href)) href = wikiUrl(decodeURIComponent(href.replace(/\.md(#.*)?$/i, "")));
              else if (!/^(https?:|mailto:|\/)/i.test(href)) href = "";
              const marks = node.getChildren("LinkMark");
              if (!live || touches(from, to) || marks.length < 2) {
                if (url && href) out.push(linkMark("cm-link", href).range(url.from, url.to));
                return;
              }
              const textFrom = marks[0].to, textTo = marks[1].from;
              out.push(hidden.range(from, textFrom));
              if (textTo > textFrom) out.push(linkMark("cm-link", href).range(textFrom, textTo));
              out.push(hidden.range(textTo, to));
              return;
            }
            case "URL":
              if (parent && (parent.name === "Link" || parent.name === "Image")) return;
              out.push(linkMark("cm-link", doc.sliceString(from, to).replace(/^www\./i, "https://www.")).range(from, to));
              return;
            case "Hashtag": {
              const tag = doc.sliceString(from + 1, to).toLowerCase();
              out.push(linkMark("cm-hashtag", "/tags/" + tag.split("/").map(encodeURIComponent).join("/")).range(from, to));
              return;
            }
          }
        },
      });
    }
    return Decoration.set(out, true);
  }

  function previewPlugin(live) {
    return ViewPlugin.fromClass(class {
      constructor(view) {
        this.decorations = decorate(view, live);
      }
      update(u) {
        if (u.docChanged || u.viewportChanged || u.selectionSet || u.focusChanged || syntaxTree(u.startState) !== syntaxTree(u.state)) {
          this.decorations = decorate(u.view, live);
        }
      }
    }, { decorations: (v) => v.decorations });
  }
  // Blocs $$ rendus par KaTeX quand le curseur n'y est pas. Un bloc couvre
  // plusieurs lignes : ce remplacement doit venir d'un champ d'état, pas du plugin.
  const CONTAINERS = new Set(["Document", "Blockquote", "BulletList", "OrderedList", "ListItem"]);

  function blockMathDecorations(state) {
    const ranges = state.selection.ranges;
    const out = [];
    syntaxTree(state).iterate({
      enter(node) {
        if (node.name !== "BlockMath") return CONTAINERS.has(node.name);
        if (!ranges.some((r) => r.from <= node.to && r.to >= node.from)) {
          const tex = mathSource(state.sliceDoc(node.from, node.to));
          if (tex && !looksLikeLatex(tex)) out.push(Decoration.replace({ widget: new MathWidget(tex, true) }).range(node.from, node.to));
        }
        return false;
      },
    });
    return Decoration.set(out);
  }

  const blockMath = StateField.define({
    create: blockMathDecorations,
    update(decorations, tr) {
      if (tr.docChanged || tr.selection || syntaxTree(tr.startState) !== syntaxTree(tr.state)) {
        return blockMathDecorations(tr.state);
      }
      return decorations;
    },
    provide: (field) => EditorView.decorations.from(field),
  });

  const livePreview = [previewPlugin(true), blockMath];
  const sourceView = previewPlugin(false);

  // -- Commandes de mise en forme ---------------------------------------------------------

  function wrap(before, after = before, placeholderText = "texte") {
    return (view) => {
      const { state } = view;
      view.dispatch(state.update(state.changeByRange((range) => {
        const text = state.sliceDoc(range.from, range.to);
        const outside = state.sliceDoc(range.from - before.length, range.to + after.length);
        if (range.from >= before.length && outside.startsWith(before) && outside.endsWith(after)
          && outside.length >= before.length + after.length + text.length) {
          return {
            changes: [{ from: range.from - before.length, to: range.from }, { from: range.to, to: range.to + after.length }],
            range: EditorSelection.range(range.from - before.length, range.to - before.length),
          };
        }
        if (text.length >= before.length + after.length && text.startsWith(before) && text.endsWith(after)) {
          const inner = text.slice(before.length, text.length - after.length);
          return { changes: { from: range.from, to: range.to, insert: inner }, range: EditorSelection.range(range.from, range.from + inner.length) };
        }
        const inner = text || placeholderText;
        return {
          changes: { from: range.from, to: range.to, insert: before + inner + after },
          range: EditorSelection.range(range.from + before.length, range.from + before.length + inner.length),
        };
      }), { scrollIntoView: true, userEvent: "input" }));
      return true;
    };
  }

  function selectedLines(state) {
    const lines = new Map();
    for (const r of state.selection.ranges) {
      for (let pos = r.from; pos <= r.to;) {
        const l = state.doc.lineAt(pos);
        lines.set(l.number, l);
        pos = l.to + 1;
      }
    }
    return [...lines.values()];
  }

  // Change le début des lignes sélectionnées : fn(texte de la ligne) → nouveau préfixe.
  function editLines(view, fn) {
    const changes = [];
    for (const l of selectedLines(view.state)) {
      const change = fn(l.text);
      if (change) changes.push({ from: l.from + change.from, to: l.from + change.to, insert: change.insert });
    }
    view.dispatch({ changes, scrollIntoView: true, userEvent: "input" });
    view.focus();
    return true;
  }

  const MARKER_RE = /^(\s*)([-*+] \[[ xX]\] |[-*+] |\d+[.)] )?/;

  function listCommand(prefix) {
    return (view) => {
      const all = selectedLines(view.state).every((l) => MARKER_RE.exec(l.text)[2] === prefix);
      return editLines(view, (text) => {
        const m = MARKER_RE.exec(text);
        const from = m[1].length, to = from + (m[2] || "").length;
        return { from, to, insert: all ? "" : prefix };
      });
    };
  }

  function headingCommand(view) {
    return editLines(view, (text) => {
      const m = /^(#{1,6}) /.exec(text);
      const level = m ? m[1].length : 1;
      const next = !m ? "## " : level >= 4 ? "" : "#".repeat(level + 1) + " ";
      return { from: 0, to: m ? m[0].length : 0, insert: next };
    });
  }

  // Ctrl+Entrée : texte → tâche → tâche cochée → tâche (comme Obsidian).
  function toggleTask(view) {
    return editLines(view, (text) => {
      const m = /^(\s*)([-*+] )(\[([ xX])\] )?/.exec(text);
      if (!m) {
        const indent = /^\s*/.exec(text)[0].length;
        return { from: indent, to: indent, insert: "- [ ] " };
      }
      const at = m[1].length + m[2].length;
      if (!m[3]) return { from: at, to: at, insert: "[ ] " };
      return { from: at + 1, to: at + 2, insert: m[4] === " " ? "x" : " " };
    });
  }

  function insertBlock(view, text, select) {
    const { state } = view;
    const pos = state.selection.main.from;
    const before = state.sliceDoc(0, pos);
    const lead = before === "" || before.endsWith("\n\n") ? "" : before.endsWith("\n") ? "\n" : "\n\n";
    const start = pos + lead.length;
    view.dispatch({
      changes: { from: pos, to: state.selection.main.to, insert: lead + text + "\n" },
      selection: select ? EditorSelection.range(start + select[0], start + select[1]) : { anchor: start + text.length + 1 },
      scrollIntoView: true,
      userEvent: "input",
    });
    view.focus();
    return true;
  }

  function selectedText(view) {
    const { from, to } = view.state.selection.main;
    return view.state.sliceDoc(from, to);
  }

  function replaceSelection(view, text) {
    view.dispatch(view.state.replaceSelection(text), { scrollIntoView: true, userEvent: "input" });
    view.focus();
  }

  // Formule : sur une ligne vide, un bloc $$ ; sinon $...$ autour de la sélection.
  function mathCommand(view) {
    const { state } = view;
    const range = state.selection.main;
    const current = state.doc.lineAt(range.from);
    if (range.empty && !current.text.trim()) {
      view.dispatch({
        changes: { from: current.from, to: current.to, insert: "$$\n\n$$" },
        selection: { anchor: current.from + 3 },
        scrollIntoView: true,
        userEvent: "input",
      });
      view.focus();
      return true;
    }
    return wrap("$", "$", "x")(view);
  }

  // Avec du texte sélectionné, taper * _ = ~ ` ou $ l'entoure (« Auto pair Markdown syntax »).
  const pairOnSelection = EditorView.inputHandler.of((view, from, to, text) => {
    if (from === to || text.length !== 1 || !"*_=~`$".includes(text)) return false;
    const mark = text === "=" || text === "~" ? text + text : text;
    return wrap(mark)(view);
  });

  // -- Autocomplétion des liens ---------------------------------------------------------------

  function findPages(query) {
    const q = normalize(query.trim());
    const scored = [];
    for (const c of choices) {
      const names = [c.link, c.title, ...c.aliases].map(normalize);
      if (q && !names.some((n) => n.includes(q))) continue;
      const alias = q ? c.aliases.find((a) => normalize(a).includes(q) && !normalize(c.title).includes(q)) : null;
      scored.push([q && names.some((n) => n.startsWith(q)) ? 0 : 1, c, alias]);
    }
    scored.sort((a, b) => a[0] - b[0] || a[1].title.localeCompare(b[1].title));
    const found = scored.slice(0, 40).map(([, c, alias]) => ({ ...c, alias }));
    const name = query.trim();
    if (name && !/[\\:*?"<>|#^[\]]/.test(name) && !choices.some((c) => normalize(c.link) === normalize(name))) {
      found.push({ link: name, title: name, folder: "", aliases: [], isNew: true });
    }
    return found;
  }

  function findFiles(query) {
    const q = normalize(query.trim());
    return attachments.filter((a) => !q || normalize(a.link).includes(q)).slice(0, 40);
  }

  function applyLink(text) {
    return (view, completion, from, to) => {
      const after = view.state.sliceDoc(to, to + 2);
      const end = after === "]]" ? to + 2 : after[0] === "]" ? to + 1 : to;
      view.dispatch({
        changes: { from, to: end, insert: text + "]]" },
        selection: { anchor: from + text.length + 2 },
        userEvent: "input.complete",
      });
    };
  }

  function linkCompletion(context) {
    const match = context.matchBefore(/!?\[\[[^[\]|#\n]*/);
    if (!match) return null;
    const embed = match.text.startsWith("!");
    const from = match.from + (embed ? 3 : 2);
    const query = match.text.slice(embed ? 3 : 2);
    const options = [];
    if (embed) {
      for (const a of findFiles(query)) {
        options.push({ label: a.link, detail: a.folder, type: IMAGE_RE.test(a.link) ? "image" : "file", apply: applyLink(a.link) });
      }
    }
    for (const c of findPages(query)) {
      if (c.isNew) {
        options.push({ label: c.link, detail: "nouvelle page", type: "new", apply: applyLink(c.link), boost: -99 });
      } else if (c.alias) {
        options.push({ label: c.alias, detail: `→ ${c.title}`, type: "page", apply: applyLink(`${c.link}|${c.alias}`) });
      } else {
        options.push({ label: c.title, detail: c.folder, type: "page", apply: applyLink(c.link) });
      }
    }
    return { from, options, filter: false };
  }

  // -- Autocomplétion des tags (#) et des types d'encadré ([!) ------------------------------------

  // Types d'encadrés proposés (menu « Encadré », complétion après [!).
  const CALLOUTS = [
    ["note", "Note"], ["abstract", "Résumé"], ["info", "Info"], ["tip", "Astuce"], ["definition", "Définition"],
    ["theoreme", "Théorème"], ["propriete", "Propriété"], ["methode", "Méthode"], ["exemple", "Exemple"],
    ["demonstration", "Démonstration (repliée)"], ["warning", "Attention, piège"], ["question", "Question"],
    ["retenir", "À retenir"], ["quote", "Citation"],
  ];

  function tagCompletion(context) {
    const match = context.matchBefore(/(?:^|\s)#[\p{L}\p{N}_\/-]*/u);
    if (!match || !tagChoices.length) return null;
    const hash = match.text.indexOf("#");
    const from = match.from + hash + 1;
    const query = normalize(match.text.slice(hash + 1));
    if (/^\d*$/.test(match.text.slice(hash + 1)) && match.text.slice(hash + 1)) return null; // #1, numéro
    const options = tagChoices.filter((t) => normalize(t).includes(query)).slice(0, 12)
      .map((t) => ({ label: t, type: "keyword", apply: t + " " }));
    return options.length ? { from, options, filter: false } : null;
  }

  function calloutCompletion(context) {
    const match = context.matchBefore(/>\s?\[!\w*/);
    if (!match) return null;
    const from = match.from + match.text.indexOf("[!") + 2;
    const query = match.text.slice(match.text.indexOf("[!") + 2).toLowerCase();
    const options = CALLOUTS.filter(([type]) => type.startsWith(query))
      .map(([type, label]) => ({ label: type, detail: label, type: "type", apply: (view, c, f, to) => {
        const after = view.state.sliceDoc(to, to + 1) === "]" ? to + 1 : to;
        view.dispatch({ changes: { from: f, to: after, insert: type + "] " }, selection: { anchor: f + type.length + 2 }, userEvent: "input.complete" });
      } }));
    return options.length ? { from, options, filter: false } : null;
  }

  // -- Menus « Encadré » et « Insérer » -------------------------------------------------------------

  const SYMBOLES = "→ ← ⇒ ⇔ ≤ ≥ ≠ ≈ ≡ ± × ÷ · ° ∞ √ ∑ ∫ ∂ ∇ ∈ ∉ ⊂ ∪ ∩ ∅ ∀ ∃ α β γ δ ε θ λ μ π ρ σ τ φ ω Δ Σ Ω".split(" ");
  const FORMULES = [
    ["Fraction", "\\frac{a}{b}"], ["Racine", "\\sqrt{x}"], ["Puissance, indice", "x^{2}_{i}"], ["Somme", "\\sum_{i=1}^{n} a_i"],
    ["Intégrale", "\\int_{a}^{b} f(x)\\,dx"], ["Limite", "\\lim_{x \\to 0} f(x)"], ["Dérivée", "\\frac{df}{dx}"],
    ["Vecteur", "\\vec{u}"], ["Matrice", "\\begin{pmatrix} a & b \\\\ c & d \\end{pmatrix}"],
    ["Système", "\\begin{cases} x + y = 1 \\\\ x - y = 0 \\end{cases}"],
  ];

  function insererFormule(view, tex) {
    const { state } = view;
    const range = state.selection.main;
    const current = state.doc.lineAt(range.from);
    if (range.empty && !current.text.trim()) {
      view.dispatch({
        changes: { from: current.from, to: current.to, insert: "$$\n" + tex + "\n$$" },
        selection: EditorSelection.range(current.from + 3, current.from + 3 + tex.length),
        scrollIntoView: true, userEvent: "input",
      });
    } else {
      view.dispatch({
        changes: { from: range.from, to: range.to, insert: "$" + tex + "$" },
        selection: EditorSelection.range(range.from + 1, range.from + 1 + tex.length),
        scrollIntoView: true, userEvent: "input",
      });
    }
    view.focus();
  }

  function insererEncadre(view, type) {
    const title = selectedText(view).trim() || "Titre";
    const fold = type === "demonstration" ? "-" : "";
    const head = `> [!${type}]${fold} ${title}\n> `;
    return insertBlock(view, head + "Texte de l'encadré", [head.length, head.length + "Texte de l'encadré".length]);
  }

  // Aligne les colonnes du tableau où se trouve le curseur.
  function formaterTableau(view) {
    const { state } = view;
    const doc = state.doc;
    const estLigne = (n) => /^\s*\|/.test(doc.line(n).text);
    let first = doc.lineAt(state.selection.main.head).number;
    if (!estLigne(first)) {
      status("Placez le curseur dans un tableau pour l'aligner.", "dirty");
      return false;
    }
    let last = first;
    while (first > 1 && estLigne(first - 1)) first--;
    while (last < doc.lines && estLigne(last + 1)) last++;
    const rows = [];
    for (let n = first; n <= last; n++) {
      rows.push(doc.line(n).text.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim()));
    }
    const cols = Math.max(...rows.map((r) => r.length));
    rows.forEach((r) => { while (r.length < cols) r.push(""); });
    const sep = rows.findIndex((r, i) => i > 0 && r.every((c) => /^:?-+:?$/.test(c)));
    const largeur = (c) => [...c].length;
    const widths = rows[0].map((_, i) => Math.max(3, ...rows.map((r, k) => (k === sep ? 0 : largeur(r[i])))));
    const lignes = rows.map((r, k) => {
      if (k === sep) {
        return "| " + r.map((c, i) => {
          const w = widths[i];
          if (c.startsWith(":") && c.endsWith(":")) return ":" + "-".repeat(w - 2) + ":";
          if (c.endsWith(":")) return "-".repeat(w - 1) + ":";
          if (c.startsWith(":")) return ":" + "-".repeat(w - 1);
          return "-".repeat(w);
        }).join(" | ") + " |";
      }
      return "| " + r.map((c, i) => c + " ".repeat(widths[i] - largeur(c))).join(" | ") + " |";
    });
    view.dispatch({ changes: { from: doc.line(first).from, to: doc.line(last).to, insert: lignes.join("\n") }, userEvent: "input" });
    view.focus();
    return true;
  }

  // -- Fichiers : images collées ou glissées, .md ouverts ou glissés, téléchargement --------------

  function status(text, kind) {
    stateLabel.textContent = text;
    stateLabel.dataset.kind = kind || "";
  }

  function pastedName(file) {
    const d = new Date();
    const pad = (n) => String(n).padStart(2, "0");
    const stamp = `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
    const ext = (file.type.split("/")[1] || "png").replace("jpeg", "jpg").replace("svg+xml", "svg");
    return `Pasted image ${stamp}.${ext}`;
  }

  async function uploadFiles(view, list, pos) {
    if (!list.length) return;
    const data = new FormData();
    data.append("_csrf", csrf);
    data.append("pour_note", noteKey);
    for (const file of list) {
      data.append("fichiers", file, file.name);
      data.append("chemins", file.name);
    }
    status(`Envoi de ${list.length} fichier${list.length > 1 ? "s" : ""}…`);
    let json = {};
    let response;
    try {
      response = await fetch(importUrl, { method: "POST", body: data, headers: { Accept: "application/json" } });
      if (response.status === 413) {
        // Refusé avant le wiki (Cloudflare : 100 Mo au plus par envoi).
        status("Envoi impossible : fichier trop volumineux pour passer par le réseau (100 Mo au plus par envoi).", "error");
        return;
      }
      json = await response.json();
    } catch (e) {
      status("Envoi impossible : serveur injoignable ou session expirée.", "error");
      return;
    }
    if (!response.ok) return status(json.error || "Envoi impossible.", "error");
    const links = [];
    const errors = [];
    for (const r of json.results || []) {
      if (r.ok) {
        const a = { link: r.link, folder: r.path.includes("/") ? r.path.slice(0, r.path.lastIndexOf("/")) : "", url: r.url };
        attachments.push(a);
        rememberFile(a);
        links.push(`![[${r.link}]]`);
      } else {
        errors.push(`${r.name} : ${r.message}`);
      }
    }
    if (links.length) {
      const at = pos == null ? view.state.selection.main.head : Math.min(pos, view.state.doc.length);
      const text = links.join("\n");
      view.dispatch({ changes: { from: at, insert: text }, selection: { anchor: at + text.length }, userEvent: "input" });
    }
    status(errors.length ? errors.join(" · ") : `${links.length} fichier${links.length > 1 ? "s" : ""} ajouté${links.length > 1 ? "s" : ""} au coffre`,
      errors.length ? "error" : "ok");
  }

  // -- LaTeX : un cours .tex est converti en page (chapitres, formules, encadrés, tableaux) --------

  function looksLikeLatex(text) {
    return /\\documentclass|\\begin\s*\{document\}/.test(text)
      || (text.match(/\\(chapter|section|subsection)\*?\{|\\begin\{(itemize|enumerate|tabular|figure|theorem|definition)\}/g) || []).length >= 3;
  }

  async function convertLatex(source) {
    const data = new FormData();
    data.append("_csrf", csrf);
    data.append("source", source);
    status("Conversion du LaTeX…");
    const response = await fetch(latexUrl, { method: "POST", body: data, headers: { Accept: "application/json" } });
    const json = await response.json().catch(() => ({}));
    if (!response.ok || typeof json.markdown !== "string") {
      throw new Error(json.error || "Conversion impossible (session expirée ?).");
    }
    return json.markdown;
  }

  async function convertDocument() {
    const { from, to } = view.state.selection.main;
    const whole = from === to;
    const source = whole ? view.state.doc.toString() : view.state.sliceDoc(from, to);
    if (!source.trim()) return status("Rien à convertir : collez d'abord le LaTeX.", "error");
    try {
      const markdown = await convertLatex(source);
      view.dispatch({
        changes: whole ? { from: 0, to: view.state.doc.length, insert: markdown } : { from, to, insert: markdown },
        selection: { anchor: whole ? 0 : from },
        scrollIntoView: true,
        userEvent: "input",
      });
      status("LaTeX converti : vérifiez puis enregistrez (Ctrl+Z pour annuler).", "dirty");
      view.focus();
    } catch (e) {
      status(e.message, "error");
    }
  }

  // -- « Améliorer la rédaction » : le passage sélectionné (ou toute la page) relu par Claude ---------

  let amelioration = false;
  async function ameliorerTexte() {
    if (!ameliorerUrl || amelioration) return;
    const { from, to } = view.state.selection.main;
    const whole = from === to;
    const source = whole ? view.state.doc.toString() : view.state.sliceDoc(from, to);
    if (!source.trim()) return status("Rien à améliorer : sélectionnez un passage.", "error");
    if (source.length > 12000) return status("Passage trop long : sélectionnez une partie de la page (12 000 caractères au plus).", "error");
    amelioration = true;
    status(whole ? "L’IA relit toute la page…" : "L’IA relit le passage…");
    const data = new FormData();
    data.append("_csrf", csrf);
    data.append("texte", source);
    data.append("page", noteKey);
    try {
      const response = await fetch(ameliorerUrl, { method: "POST", body: data, headers: { Accept: "application/json" } });
      const json = await response.json().catch(() => ({}));
      if (!response.ok || typeof json.texte !== "string") throw new Error(json.error || "Relecture impossible (session expirée ?).");
      if (json.texte.trim() === source.trim()) return status("Rien à changer : l’IA trouve le texte correct.", "ok");
      view.dispatch({
        changes: whole ? { from: 0, to: view.state.doc.length, insert: json.texte } : { from, to, insert: json.texte },
        selection: whole ? { anchor: 0 } : { anchor: from, head: from + json.texte.length },
        scrollIntoView: true,
        userEvent: "input",
      });
      status("Texte amélioré par l’IA : relis-le, Ctrl+Z pour annuler.", "dirty");
    } catch (e) {
      status(e.message, "error");
    } finally {
      amelioration = false;
    }
  }

  const isLatexFile = (file) => /\.(tex|latex)$/i.test(file.name);
  const isMarkdown = (file) => /\.(md|markdown|txt|tex|latex)$/i.test(file.name);
  const readText = (file) => file.text().then((t) => t.replace(/^﻿/, "").replace(/\r\n?/g, "\n"));

  async function insertMarkdownFiles(view, list, pos) {
    let texts;
    try {
      texts = await Promise.all(list.map(async (file) => (isLatexFile(file) ? convertLatex(await readText(file)) : readText(file))));
    } catch (e) {
      return status(e.message, "error");
    }
    const at = Math.min(pos, view.state.doc.length);
    const text = texts.join("\n\n");
    view.dispatch({ changes: { from: at, insert: text }, selection: { anchor: at + text.length }, userEvent: "input" });
    view.focus();
  }

  function handleFiles(view, list, pos) {
    const md = list.filter(isMarkdown);
    const others = list.filter((f) => !isMarkdown(f));
    if (md.length) insertMarkdownFiles(view, md, pos);
    if (others.length) uploadFiles(view, others, pos);
  }

  const fileEvents = EditorView.domEventHandlers({
    paste(e, view) {
      const list = [...(e.clipboardData ? e.clipboardData.files : [])];
      if (!list.length) {
        const text = e.clipboardData ? e.clipboardData.getData("text/plain") : "";
        if (!text || !looksLikeLatex(text)) return false;
        e.preventDefault();
        pastedLatex = text;
        latexDialog.showModal();
        return true;
      }
      e.preventDefault();
      uploadFiles(view, list.map((f) => (f.type.startsWith("image/") ? new File([f], pastedName(f), { type: f.type }) : f)));
      return true;
    },
    drop(e, view) {
      const list = [...(e.dataTransfer ? e.dataTransfer.files : [])];
      if (!list.length) return false;
      e.preventDefault();
      const pos = view.posAtCoords({ x: e.clientX, y: e.clientY });
      handleFiles(view, list, pos == null ? view.state.selection.main.head : pos);
      return true;
    },
    mousedown(e) {
      // Ctrl+clic (Cmd+clic sur Mac) : ouvre le lien dans un nouvel onglet.
      if (!(e.ctrlKey || e.metaKey) || e.button !== 0) return false;
      const el = e.target.closest("[data-href]");
      if (!el) return false;
      e.preventDefault();
      window.open(el.dataset.href, "_blank", "noopener");
      return true;
    },
  });

  function download() {
    // Application Android : « Enregistrer sous » natif (la WebView ne télécharge pas les blob:).
    if (window.AppliNatif && window.AppliNatif.enregistrer) {
      const octets = new TextEncoder().encode(view.state.doc.toString());
      let binaire = "";
      for (let i = 0; i < octets.length; i += 0x8000) binaire += String.fromCharCode(...octets.subarray(i, i + 0x8000));
      window.AppliNatif.enregistrer(`${noteName}.md`, "text/markdown", btoa(binaire));
      return;
    }
    const blob = new Blob([view.state.doc.toString()], { type: "text/markdown;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${noteName}.md`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  }

  // -- Enregistrement --------------------------------------------------------------------------

  let savedDoc = null;
  let saving = null;
  let saveAgain = false;
  let conflict = false;
  let submitting = false;
  let autosaveTimer = null;
  const isDirty = () => !view.state.doc.eq(savedDoc);

  function refreshState() {
    if (saving || stateLabel.dataset.kind === "error") return;
    if (isDirty()) status("Modifications non enregistrées", "dirty");
  }

  function save(manuel) {
    if (saving) {
      saveAgain = true;
      return saving;
    }
    const doc = view.state.doc;
    const data = new FormData();
    data.append("_csrf", csrf);
    data.append("version", versionInput.value);
    if (seanceInput) data.append("seance", seanceInput.value);
    data.append("content", doc.toString());
    status("Enregistrement…");
    saving = fetch(form.action, { method: "POST", body: data, headers: { Accept: "application/json" } })
      .then(async (response) => {
        const json = await response.json().catch(() => ({}));
        if (response.ok && json.version) {
          versionInput.value = json.version;
          if (seanceInput) seanceInput.value = json.seance || "";
          savedDoc = doc;
          oublierBrouillon();
          conflict = false;
          if (json.url) closeLink.href = json.url;
          const now = new Date().toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
          status(`Enregistré à ${now}`, "ok");
          if (manuel) afficherLiens(false);
        } else {
          conflict = response.status === 409;
          status(json.error || "Échec de l'enregistrement (session expirée ?).", "error");
        }
      })
      .catch(() => status("Serveur injoignable : modifications non enregistrées.", "error"))
      .finally(() => {
        saving = null;
        if (saveAgain) {
          saveAgain = false;
          if (isDirty() && !conflict) save();
        }
        refreshState();
      });
    return saving;
  }

  function scheduleAutosave() {
    clearTimeout(autosaveTimer);
    if (autosaveBox.checked && !conflict) autosaveTimer = setTimeout(() => isDirty() && save(), 2000);
  }

  // -- Compteur de mots et plan ----------------------------------------------------------------

  const outline = document.querySelector("[data-outline]");
  let infoTimer = null;

  function headings(text) {
    const found = [];
    let fence = null;
    let pos = 0;
    for (const l of text.split("\n")) {
      const f = /^\s*(`{3,}|~{3,})/.exec(l);
      if (f && (!fence || f[1][0] === fence[0] && f[1].length >= fence.length)) fence = fence ? null : f[1];
      else if (!fence) {
        const h = /^(#{1,6})\s+(.+?)\s*#*\s*$/.exec(l);
        if (h) found.push({ level: h[1].length, text: h[2].replace(/\[\[([^\]|]*\|)?([^\]]*)\]\]/g, "$2").replace(/[*_=~`]/g, ""), pos });
      }
      pos += l.length + 1;
    }
    return found;
  }

  function refreshInfo() {
    const text = view.state.doc.toString();
    const body = text.replace(/^---\n[\s\S]*?\n---(\n|$)/, "");
    const words = (body.match(/[\p{L}\p{N}][\p{L}\p{N}'’-]*/gu) || []).length;
    const minutes = Math.max(1, Math.round(words / 200));
    countLabel.textContent = `${words} mot${words > 1 ? "s" : ""} · ${text.length} caractère${text.length > 1 ? "s" : ""}`
      + (words ? ` · ~${minutes} min de lecture` : "");
    if (!outline) return;
    const list = headings(text);
    if (!list.length) {
      outline.innerHTML = '<p class="muted">Les titres de la page apparaîtront ici.</p>';
      return;
    }
    const ul = document.createElement("ul");
    const min = Math.min(...list.map((h) => h.level));
    for (const h of list) {
      const li = document.createElement("li");
      li.style.paddingLeft = `${(h.level - min) * 0.8}rem`;
      const button = document.createElement("button");
      button.type = "button";
      button.className = "link-button";
      button.textContent = h.text;
      button.addEventListener("click", () => {
        if (mode === "reading") setMode(editMode);
        view.dispatch({ selection: { anchor: h.pos }, effects: EditorView.scrollIntoView(h.pos, { y: "start", yMargin: 120 }) });
        view.focus();
      });
      li.appendChild(button);
      ul.appendChild(li);
    }
    outline.replaceChildren(ul);
  }

  // -- Création de l'éditeur ---------------------------------------------------------------------

  const preview_ = new Compartment();
  const theme_ = new Compartment();
  const sombre = () => (window.wikiTheme ? window.wikiTheme.resolved() : (document.documentElement.dataset.theme || "light")) === "dark";
  // Hauteur cachée par la barre d'outils collée en haut (aucune en plein écran : elle est rentrée).
  // En plein écran, la barre d'outils rentre en haut de l'écran (elle revient quand la souris touche le bord).
  const stickyTop = () => (document.body.classList.contains("plein-ecran") ? 0
    : toolbar.getBoundingClientRect().height + (parseFloat(getComputedStyle(form).getPropertyValue("--sticky-top")) || 0));

  const appKeymap = [
    { key: "Mod-s", run: () => (save(true), true), preventDefault: true },
    { key: "Mod-b", run: wrap("**", "**", "texte en gras") },
    { key: "Mod-i", run: wrap("*", "*", "texte en italique") },
    { key: "Mod-Shift-h", run: wrap("==", "==", "texte surligné") },
    { key: "Mod-Shift-c", run: wrap("`", "`", "code") },
    { key: "Mod-/", run: wrap("%%", "%%", "commentaire") },
    { key: "Mod-k", run: () => (actions["page-link"](), true) },
    { key: "Mod-e", run: () => (setMode("reading"), true), preventDefault: true },
    { key: "Mod-Enter", run: toggleTask },
    { key: "Mod-Shift-m", run: mathCommand },
  ];

  const view = new EditorView({
    parent: host,
    state: EditorState.create({
      doc: textarea.value,
      extensions: [
        history(),
        drawSelection(),
        dropCursor(),
        indentOnInput(),
        EditorView.lineWrapping,
        markdown({
          base: markdownLanguage,
          codeLanguages: (info) => codeLanguage(info),
          extensions: [Frontmatter, WikiLink, Highlight, Comment, Hashtag, Math_],
        }),
        markdownLanguage.data.of({ closeBrackets: { brackets: ["(", "[", "{"] } }),
        syntaxHighlighting(obsidianHighlight),
        closeBrackets(),
        autocompletion({ override: [linkCompletion, calloutCompletion, tagCompletion], icons: false, activateOnTyping: true }),
        highlightSelectionMatches(),
        search({ top: true }),
        pairOnSelection,
        fileEvents,
        preview_.of(stored("wiki-editor-mode", "live") === "source" ? sourceView : livePreview),
        placeholder("Écrivez ici… Tapez [[ pour lier une page."),
        EditorView.contentAttributes.of({ spellcheck: "true", lang: "fr", autocapitalize: "sentences", "aria-label": "Contenu de la page" }),
        EditorView.scrollMargins.of(() => ({ top: stickyTop(), bottom: footer.getBoundingClientRect().height })),
        theme_.of(EditorView.darkTheme.of(sombre())),
        EditorView.theme({
          "&": { color: "var(--text)", backgroundColor: "transparent", fontSize: "1rem" },
          "&.cm-focused": { outline: "none" },
          ".cm-scroller": { fontFamily: "var(--editor-font)", lineHeight: "1.65" },
          ".cm-content": { caretColor: "var(--accent)", padding: ".75rem 0 4rem" },
          ".cm-line": { padding: "0 .25rem" },
          ".cm-cursor, .cm-dropCursor": { borderLeftColor: "var(--accent)", borderLeftWidth: "2px" },
          ".cm-placeholder": { color: "var(--muted)" },
          ".cm-panels": { backgroundColor: "var(--bg-alt)", color: "var(--text)" },
          ".cm-panels-top": { top: "calc(var(--sticky-top, 0px) + var(--toolbar-h, 0px))", borderBottom: "1px solid var(--border)" },
          ".cm-panel input, .cm-panel button": { fontSize: ".85rem" },
          ".cm-searchMatch": { backgroundColor: "var(--mark)" },
          ".cm-tooltip": { backgroundColor: "var(--bg)", color: "var(--text)", border: "1px solid var(--border)", borderRadius: "8px" },
          ".cm-tooltip.cm-tooltip-autocomplete > ul": { fontFamily: "var(--font)", maxHeight: "16rem" },
          ".cm-tooltip.cm-tooltip-autocomplete > ul > li": { padding: ".25rem .6rem" },
          ".cm-tooltip.cm-tooltip-autocomplete > ul > li[aria-selected]": { backgroundColor: "var(--accent-soft)", color: "var(--text)" },
          ".cm-completionDetail": { color: "var(--muted)", fontStyle: "normal", marginLeft: ".75em", fontSize: ".85em" },
          ".cm-completionMatchedText": { textDecoration: "none", fontWeight: "700" },
        }),
        EditorState.phrases.of({
          Find: "Rechercher", Replace: "Remplacer", next: "suivant", previous: "précédent", all: "tout",
          "match case": "respecter la casse", "by word": "mot entier", regexp: "expression régulière",
          replace: "remplacer", "replace all": "tout remplacer", close: "fermer", "current match": "occurrence actuelle",
          "replaced match on line $": "occurrence remplacée ligne $", "replaced $ matches": "$ occurrences remplacées",
          "on line": "ligne", "Go to line": "Aller à la ligne", go: "OK", "Control character": "Caractère de contrôle",
        }),
        Prec.high(keymap.of(appKeymap)),
        keymap.of([...closeBracketsKeymap, ...completionKeymap, ...searchKeymap, ...historyKeymap, indentWithTab, ...defaultKeymap]),
        EditorView.updateListener.of((u) => {
          if (!u.docChanged) return;
          if (stateLabel.dataset.kind !== "error" || !conflict) status("", "");
          refreshState();
          scheduleAutosave();
          garderBrouillon();
          if (mode === "split") planifierRendu();
          clearTimeout(infoTimer);
          infoTimer = setTimeout(() => {
            refreshInfo();
            refreshLatexNotice();
          }, 300);
        }),
      ],
    }),
  });
  savedDoc = view.state.doc;

  textarea.hidden = true;
  host.hidden = false;
  toolbar.hidden = false;
  form.querySelector("[data-action=save]").hidden = false;
  form.querySelector(".editor-status").hidden = false;
  autosaveBox.checked = stored("wiki-editor-autosave", "0") === "1";
  refreshInfo();

  // -- Brouillon de secours sur l'appareil --------------------------------------------------------
  // Le texte non enregistré est gardé dans le navigateur (onglet fermé, batterie à plat,
  // session expirée) et proposé à la prochaine ouverture de la page.

  const brouillonCle = "wiki-brouillon:" + noteKey;
  const brouillonNotice = form.querySelector("[data-brouillon-notice]");
  let brouillonTimer = null;

  function garderBrouillon() {
    clearTimeout(brouillonTimer);
    brouillonTimer = setTimeout(() => {
      if (!isDirty()) return oublierBrouillon();
      store(brouillonCle, JSON.stringify({ texte: view.state.doc.toString(), version: versionInput.value, date: Date.now() }));
    }, 800);
  }

  function oublierBrouillon() {
    clearTimeout(brouillonTimer);
    try { localStorage.removeItem(brouillonCle); } catch (e) { /* stockage indisponible */ }
  }

  (function proposerBrouillon() {
    let b = null;
    try { b = JSON.parse(stored(brouillonCle, "null")); } catch (e) { b = null; }
    if (!b || typeof b.texte !== "string" || b.texte === textarea.value) {
      if (b) oublierBrouillon();
      return;
    }
    brouillonNotice.querySelector("[data-brouillon-date]").textContent =
      new Date(b.date).toLocaleString("fr-FR", { dateStyle: "short", timeStyle: "short" });
    brouillonNotice.querySelector("[data-brouillon-autre]").hidden = b.version === versionInput.value;
    brouillonNotice.hidden = false;
    brouillonNotice.querySelector("[data-brouillon=reprendre]").addEventListener("click", () => {
      view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: b.texte }, userEvent: "input" });
      brouillonNotice.hidden = true;
      status("Brouillon repris : pensez à enregistrer.", "dirty");
      view.focus();
    });
    brouillonNotice.querySelector("[data-brouillon=ignorer]").addEventListener("click", () => {
      oublierBrouillon();
      brouillonNotice.hidden = true;
      view.focus();
    });
  })();

  // Barre d'outils et recherche collées en haut de la zone de contenu : dans Hangar,
  // c'est elle qui défile (sous la barre du haut), d'où un décalage nul.
  function layout() {
    const pleinEcran = document.body.classList.contains("plein-ecran");
    form.style.setProperty("--sticky-top", "0px");
    form.style.setProperty("--toolbar-h", `${pleinEcran ? 0 : toolbar.getBoundingClientRect().height}px`);
  }
  // Ctrl+K dans le texte : lien vers une page (la palette de Hangar ne s'ouvre pas).
  host.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === "k") e.stopPropagation();
  });
  layout();
  window.addEventListener("resize", layout);
  if (window.ResizeObserver) new ResizeObserver(layout).observe(toolbar);

  // -- Modes : aperçu en direct, source, lecture ---------------------------------------------------

  let mode = stored("wiki-editor-mode", "live") === "source" ? "source" : "live";
  let editMode = mode;
  let avantLecture = null; // mode affiché avant « Lecture » (pour y revenir)
  host.classList.toggle("is-source", mode === "source");

  // Rendu de la page par le serveur (mode Lecture, et panneau de droite en Côte à côte).
  let rendusEnCours = 0;
  async function rendre(attente) {
    const data = new FormData();
    data.append("content", view.state.doc.toString());
    data.append("_csrf", csrf);
    const numero = ++rendusEnCours;
    if (attente) preview.innerHTML = '<p class="muted">Chargement…</p>';
    let html;
    try {
      const response = await fetch(previewUrl, { method: "POST", body: data });
      html = response.ok ? await response.text() : "<p>Aperçu indisponible.</p>";
    } catch (e) {
      html = "<p>Aperçu indisponible : serveur injoignable.</p>";
    }
    if (numero !== rendusEnCours) return; // un rendu plus récent est parti
    preview.innerHTML = html;
    if (window.wikiEnhance) window.wikiEnhance(preview);
  }

  async function showReading() {
    host.hidden = true;
    preview.hidden = false;
    return rendre(true);
  }

  // Côte à côte : rendu rafraîchi pendant la saisie, défilement du rendu calé sur le texte.
  let renduTimer = null;
  function planifierRendu() {
    clearTimeout(renduTimer);
    renduTimer = setTimeout(() => rendre(false), 700);
  }
  view.scrollDOM.addEventListener("scroll", () => {
    if (mode !== "split") return;
    const s = view.scrollDOM;
    const ratio = s.scrollTop / Math.max(1, s.scrollHeight - s.clientHeight);
    preview.scrollTop = ratio * (preview.scrollHeight - preview.clientHeight);
  });

  function setMode(next, focus = true) {
    if (next === "reading" && mode === "reading") next = avantLecture || editMode;
    if (next === "split" && mode === "split") next = editMode;
    if (next === "reading") avantLecture = mode === "reading" ? avantLecture : mode;
    mode = next;
    form.querySelectorAll("[data-mode]").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.mode === next)));
    form.classList.toggle("is-split", next === "split");
    store("wiki-editor-split", next === "split" ? "1" : "0");
    if (next === "reading") return showReading();
    if (next === "split") {
      preview.hidden = false;
      host.hidden = false;
      host.classList.toggle("is-source", editMode === "source");
      view.dispatch({ effects: preview_.reconfigure(editMode === "source" ? sourceView : livePreview) });
      rendre(true);
      if (focus) view.focus();
      return;
    }
    editMode = next;
    store("wiki-editor-mode", next);
    preview.hidden = true;
    host.hidden = false;
    host.classList.toggle("is-source", next === "source");
    view.dispatch({ effects: preview_.reconfigure(next === "source" ? sourceView : livePreview) });
    if (focus) view.focus();
  }
  setMode(mode, !textarea.value.trim());
  if (stored("wiki-editor-split", "0") === "1" && window.matchMedia("(min-width: 900px)").matches) setMode("split", false);

  form.querySelectorAll("[data-mode]").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));
  document.addEventListener("keydown", (e) => {
    if (e.defaultPrevented || !(e.ctrlKey || e.metaKey) || e.altKey || e.shiftKey || document.querySelector("dialog[open]")) return;
    const key = e.key.toLowerCase();
    if (key === "e") {
      e.preventDefault();
      setMode(mode === "reading" ? (avantLecture || editMode) : "reading");
    } else if (key === "s") {
      e.preventDefault();
      save(true);
    }
  });

  window.addEventListener("wiki:theme", () => view.dispatch({ effects: theme_.reconfigure(EditorView.darkTheme.of(sombre())) }));

  // Plein écran (plein_ecran.js) : la barre d'outils rentre en haut de l'écran.
  window.addEventListener("wiki:plein-ecran", (e) => {
    layout();
    if (e.detail.actif && !host.hidden) view.focus();
  });

  // -- Fenêtre « Lien vers une page » ---------------------------------------------------------------

  function renderChoices(list, items, active, onPick) {
    list.replaceChildren();
    items.forEach((item, i) => {
      const li = document.createElement("li");
      li.setAttribute("role", "option");
      li.className = i === active ? "active" : "";
      const title = document.createElement("span");
      title.textContent = item.isNew ? `Nouvelle page « ${item.link} »` : item.title;
      li.appendChild(title);
      const detail = item.isNew ? "sera créée en cliquant sur le lien" : item.folder;
      if (detail) {
        const small = document.createElement("small");
        small.textContent = detail;
        li.appendChild(small);
      }
      li.addEventListener("mousedown", (e) => {
        e.preventDefault();
        onPick(i);
      });
      list.appendChild(li);
    });
  }

  function wikilink(link, label) {
    return label && label !== link ? `[[${link}|${label}]]` : `[[${link}]]`;
  }

  const pageDialog = document.getElementById("page-link-dialog");
  const pageSearch = document.getElementById("page-search");
  const pageList = document.getElementById("page-choices");
  const pageLabel = document.getElementById("page-label");
  let pageItems = [];
  let pageActive = 0;

  function refreshPageList() {
    pageItems = findPages(pageSearch.value);
    pageActive = Math.min(pageActive, Math.max(pageItems.length - 1, 0));
    renderChoices(pageList, pageItems, pageActive, (i) => {
      pageActive = i;
      insertPageLink();
    });
  }

  function insertPageLink() {
    const item = pageItems[pageActive];
    if (!item) return pageSearch.focus();
    pageDialog.close();
    replaceSelection(view, wikilink(item.link, pageLabel.value.trim()));
  }

  pageSearch.addEventListener("input", () => {
    pageActive = 0;
    refreshPageList();
  });
  pageSearch.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const n = pageItems.length || 1;
      pageActive = (pageActive + (e.key === "ArrowDown" ? 1 : n - 1)) % n;
      refreshPageList();
    } else if (e.key === "Enter") {
      e.preventDefault();
      insertPageLink();
    }
  });
  pageLabel.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      insertPageLink();
    }
  });
  document.getElementById("page-ok").addEventListener("click", insertPageLink);

  // -- Fenêtre « Lien web » ------------------------------------------------------------------------

  const webDialog = document.getElementById("web-link-dialog");
  const webUrl = document.getElementById("web-url");
  const webLabel = document.getElementById("web-label");

  function insertWebLink() {
    let url = webUrl.value.trim();
    if (!url) return webUrl.focus();
    if (!/^[a-z][\w+.-]*:/i.test(url)) url = "https://" + url;
    webDialog.close();
    replaceSelection(view, `[${webLabel.value.trim() || url}](${url.replace(/ /g, "%20").replace(/\)/g, "%29")})`);
  }

  document.getElementById("web-ok").addEventListener("click", insertWebLink);
  [webUrl, webLabel].forEach((input) => input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      insertWebLink();
    }
  }));

  // -- Collage de LaTeX : convertir ou coller tel quel -----------------------------------------------

  const latexDialog = document.getElementById("latex-paste-dialog");
  let pastedLatex = "";
  latexDialog.querySelectorAll("[data-paste-choice]").forEach((button) => button.addEventListener("click", async () => {
    latexDialog.close();
    let text = pastedLatex;
    if (button.dataset.pasteChoice === "convert") {
      try {
        text = await convertLatex(pastedLatex);
        status("LaTeX converti : vérifiez puis enregistrez (Ctrl+Z pour annuler).", "dirty");
      } catch (e) {
        return status(e.message, "error");
      }
    }
    replaceSelection(view, text);
  }));

  // Page qui contient du LaTeX brut (collé avant cette fonction) : proposer la conversion.
  const latexNotice = form.querySelector("[data-latex-notice]");
  function refreshLatexNotice() {
    latexNotice.hidden = !looksLikeLatex(view.state.doc.toString());
  }
  latexNotice.querySelector("[data-action=latex]").addEventListener("click", () => convertDocument());
  refreshLatexNotice();

  // -- Fenêtre « Ouvrir un .md » ---------------------------------------------------------------------

  const mdInput = form.querySelector("[data-md-input]");
  const mdDialog = document.getElementById("open-md-dialog");
  let mdText = "";

  mdInput.addEventListener("change", async () => {
    const file = mdInput.files[0];
    mdInput.value = "";
    if (!file) return;
    try {
      mdText = isLatexFile(file) ? await convertLatex(await readText(file)) : await readText(file);
    } catch (e) {
      return status(e.message, "error");
    }
    if (!view.state.doc.toString().trim()) return useMarkdown("replace");
    mdDialog.querySelector("[data-md-name]").textContent = `« ${file.name} »`;
    mdDialog.showModal();
  });

  function useMarkdown(how) {
    if (mdDialog.open) mdDialog.close();
    if (how === "replace") {
      view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: mdText }, selection: { anchor: 0 }, userEvent: "input" });
      view.focus();
    } else {
      replaceSelection(view, mdText);
    }
    status("Contenu du fichier chargé : pensez à enregistrer.", "dirty");
  }
  mdDialog.querySelectorAll("[data-md-choice]").forEach((b) => b.addEventListener("click", () => useMarkdown(b.dataset.mdChoice)));

  const attachInput = form.querySelector("[data-attach-input]");
  attachInput.addEventListener("change", () => {
    const list = [...attachInput.files];
    attachInput.value = "";
    handleFiles(view, list, view.state.selection.main.head);
  });

  document.querySelectorAll("[data-close]").forEach((button) => {
    button.addEventListener("click", () => button.closest("dialog").close());
  });
  document.querySelectorAll("dialog").forEach((dialog) => {
    dialog.addEventListener("close", () => view.focus());
  });

  // -- Boutons ------------------------------------------------------------------------------------------

  const actions = {
    "page-link"() {
      pageSearch.value = "";
      pageLabel.value = selectedText(view).trim();
      pageActive = 0;
      refreshPageList();
      pageDialog.showModal();
      pageSearch.focus();
    },
    "web-link"() {
      const text = selectedText(view).trim();
      const isUrl = /^(https?:\/\/|www\.)/i.test(text);
      webUrl.value = isUrl ? text : "";
      webLabel.value = isUrl ? "" : text;
      webDialog.showModal();
      webUrl.focus();
    },
    attach: () => attachInput.click(),
    bold: () => wrap("**", "**", "texte en gras")(view),
    italic: () => wrap("*", "*", "texte en italique")(view),
    strike: () => wrap("~~", "~~", "texte barré")(view),
    highlight: () => wrap("==", "==", "texte surligné")(view),
    code: () => wrap("`", "`", "code")(view),
    heading: () => headingCommand(view),
    list: () => listCommand("- ")(view),
    task: () => listCommand("- [ ] ")(view),
    callout: () => insererEncadre(view, "note"),
    table: () => insertBlock(view, "| Colonne 1 | Colonne 2 |\n| --- | --- |\n| Texte | Texte |", [2, 11]),
    "format-table": () => formaterTableau(view),
    undo: () => (CM.undo ? CM.undo(view) : historyKeymap.find((k) => k.key === "Mod-z").run(view)),
    redo: () => (CM.redo ? CM.redo(view) : historyKeymap.find((k) => k.key === "Mod-y").run(view)),
    aide: () => document.getElementById("aide-dialog").showModal(),
    date: () => replaceSelection(view, new Date().toLocaleDateString("fr-FR", { day: "numeric", month: "long", year: "numeric" })),
    separateur: () => insertBlock(view, "---"),
    math: () => mathCommand(view),
    codeblock: () => {
      const code = selectedText(view) || "code";
      return insertBlock(view, "```bash\n" + code + "\n```", [3, 7]);
    },
    "open-md": () => mdInput.click(),
    latex: () => convertDocument(),
    ameliorer: () => ameliorerTexte(),
    "download-md": download,
    save: () => save(true),
  };

  const menus = [...toolbar.querySelectorAll(".toolbar-menu")];
  const fermerMenus = () => menus.forEach((m) => { m.open = false; });
  document.addEventListener("click", (e) => {
    menus.forEach((m) => { if (m.open && !m.contains(e.target)) m.open = false; });
  });
  // Un seul menu ouvert à la fois.
  menus.forEach((m) => m.addEventListener("toggle", () => { if (m.open) menus.forEach((o) => { if (o !== m) o.open = false; }); }));

  // Menu « Encadré » : un bouton par type.
  const menuEncadres = toolbar.querySelector("[data-menu-encadres]");
  for (const [type, label] of CALLOUTS) {
    const b = document.createElement("button");
    b.type = "button";
    b.setAttribute("role", "menuitem");
    b.dataset.callout = type;
    b.innerHTML = `<span class="callout-pastille" data-callout="${type}"></span>${label} <small class="muted">[!${type}]</small>`;
    b.addEventListener("click", () => { fermerMenus(); if (mode === "reading") setMode(avantLecture || editMode); insererEncadre(view, type); });
    menuEncadres.appendChild(b);
  }

  // Menu « Insérer » : symboles, formules types, date, séparateur, alignement d'un tableau.
  const menuInserer = toolbar.querySelector("[data-menu-inserer]");
  const grille = document.createElement("div");
  grille.className = "symboles";
  for (const s of SYMBOLES) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = s;
    b.title = "Insérer " + s;
    b.addEventListener("click", () => { fermerMenus(); if (mode === "reading") setMode(avantLecture || editMode); replaceSelection(view, s); });
    grille.appendChild(b);
  }
  menuInserer.appendChild(grille);
  const titreFormules = document.createElement("p");
  titreFormules.className = "muted menu-titre";
  titreFormules.textContent = "Formules (LaTeX)";
  menuInserer.appendChild(titreFormules);
  const listeFormules = document.createElement("div");
  listeFormules.className = "formules";
  for (const [label, tex] of FORMULES) {
    const b = document.createElement("button");
    b.type = "button";
    b.setAttribute("role", "menuitem");
    b.innerHTML = `${label} <code>${tex.replace(/</g, "&lt;")}</code>`;
    b.addEventListener("click", () => { fermerMenus(); if (mode === "reading") setMode(avantLecture || editMode); insererFormule(view, tex); });
    listeFormules.appendChild(b);
  }
  menuInserer.appendChild(listeFormules);
  for (const [action, label] of [["date", "Date du jour"], ["separateur", "Ligne de séparation"], ["format-table", "Aligner le tableau (curseur dedans)"]]) {
    const b = document.createElement("button");
    b.type = "button";
    b.setAttribute("role", "menuitem");
    b.dataset.action = action;
    b.textContent = label;
    menuInserer.appendChild(b);
  }

  toolbar.querySelectorAll("[data-action]").forEach((button) => {
    button.addEventListener("click", () => {
      fermerMenus();
      const action = button.dataset.action;
      const keepsMode = ["download-md", "open-md", "aide"].includes(action);
      if (mode === "reading" && !keepsMode) setMode(avantLecture || editMode);
      actions[action]();
      if (!["page-link", "web-link", "attach", "open-md", "download-md", "latex", "ameliorer", "aide"].includes(action)) view.focus();
    });
  });
  form.querySelector("[data-action=save]").addEventListener("click", () => save(true));

  autosaveBox.addEventListener("change", () => {
    store("wiki-editor-autosave", autosaveBox.checked ? "1" : "0");
    if (autosaveBox.checked && isDirty()) save();
  });

  // -- Liens suggérés ---------------------------------------------------------
  // À l'enregistrement, les titres (ou alias) de pages existantes cités sans lien
  // sont proposés ; un clic change la première mention en [[lien]]. Le code, les
  // formules, les liens, les titres de section et les propriétés sont ignorés.
  const zoneLiens = form.querySelector("[data-suggestions-liens]");
  const LETTRE = /[\p{L}\p{N}_]/u;
  const dejaProposes = new Set();
  let fermerSansLier = false;

  function plagesExclues(texte) {
    const plages = [];
    if (texte.startsWith("---\n")) {
      const fin = texte.indexOf("\n---", 4);
      if (fin > 0) plages.push([0, fin + 4]);
    }
    const re = /```[\s\S]*?(?:```|(?![\s\S]))|~~~[\s\S]*?(?:~~~|(?![\s\S]))|`[^`\n]*`|\$\$[\s\S]*?\$\$|\$[^$\n]+\$|!?\[\[[^\]\n]*\]\]|!?\[[^\]\n]*\]\([^)\n]*\)|<[^>\n]*>|https?:\/\/\S+|^#{1,6}[ \t].*$|%%[\s\S]*?%%/gm;
    for (const m of texte.matchAll(re)) plages.push([m.index, m.index + m[0].length]);
    return plages;
  }

  function replierAvecPositions(texte) {
    // Texte replié (minuscules sans accents) et, pour chaque caractère replié, sa place d'origine.
    let replie = "";
    const pos = [];
    for (let i = 0; i < texte.length; i++) {
      const r = normalize(texte[i]);
      replie += r;
      for (let k = 0; k < r.length; k++) pos.push(i);
    }
    pos.push(texte.length);
    return { replie, pos };
  }

  function nomsDe(choix) {
    const noms = new Set([choix.title, ...(choix.aliases || []), choix.link.split("/").pop()]);
    return [...noms].map((n) => (n || "").trim()).filter((n) => n.length >= 4 && !/^\d+$/.test(n));
  }

  function suggestionsDeLiens() {
    const texte = view.state.doc.toString();
    const cibles = new Set();
    for (const m of texte.matchAll(/\[\[([^\]|#^\n]+)/g)) {
      const cible = normalize(m[1].trim());
      cibles.add(cible);
      cibles.add(cible.split("/").pop());
    }
    const parNom = new Map();
    for (const choix of choices) {
      if (choix.link === noteName || choix.link === noteKey) continue;
      const noms = nomsDe(choix);
      const lien = normalize(choix.link);
      if (cibles.has(lien) || cibles.has(lien.split("/").pop()) || noms.some((n) => cibles.has(normalize(n)))) continue;
      for (const n of noms) if (!parNom.has(normalize(n))) parNom.set(normalize(n), choix);
    }
    if (!parNom.size) return [];
    const { replie, pos } = replierAvecPositions(texte);
    const echapper = (x) => x.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const motif = new RegExp([...parNom.keys()].sort((a, b) => b.length - a.length).map(echapper).join("|"), "g");
    const exclues = plagesExclues(texte);
    const trouvees = new Map();
    let m;
    while ((m = motif.exec(replie))) {
      const debut = m.index, fin = debut + m[0].length;
      if ((debut > 0 && LETTRE.test(replie[debut - 1])) || (fin < replie.length && LETTRE.test(replie[fin]))) {
        motif.lastIndex = debut + 1;
        continue;
      }
      const de = pos[debut], a = pos[fin];
      if (exclues.some(([x, y]) => de < y && a > x)) continue;
      const choix = parNom.get(m[0]);
      const t = trouvees.get(choix.link) || { choix, nombre: 0, premiere: { de, a, texte: texte.slice(de, a) } };
      t.nombre++;
      trouvees.set(choix.link, t);
    }
    return [...trouvees.values()]
      .sort((x, y) => y.nombre - x.nombre || x.choix.title.localeCompare(y.choix.title, "fr"))
      .slice(0, 8);
  }

  function lier(link) {
    const s = suggestionsDeLiens().find((x) => x.choix.link === link);
    if (s) {
      const { de, a, texte } = s.premiere;
      const insert = texte.toLowerCase() === s.choix.link.toLowerCase() ? `[[${texte}]]` : `[[${s.choix.link}|${texte}]]`;
      view.dispatch({ changes: { from: de, to: a, insert }, userEvent: "input" });
      status("Lien ajouté : pensez à enregistrer.", "dirty");
    }
    afficherLiens(false);
  }

  function afficherLiens(avantFermeture) {
    if (!zoneLiens) return [];
    const liste = suggestionsDeLiens();
    zoneLiens.replaceChildren();
    if (!liste.length) {
      zoneLiens.hidden = true;
      return liste;
    }
    const intro = document.createElement("span");
    intro.textContent = avantFermeture ? "Avant de fermer : ces pages existent, les lier ?" : "Pages existantes citées sans lien :";
    zoneLiens.append(intro);
    for (const s of liste) {
      dejaProposes.add(s.choix.link);
      const b = document.createElement("button");
      b.type = "button";
      b.className = "button small";
      b.dataset.lier = s.choix.link;
      b.title = `Changer « ${s.premiere.texte} » en lien vers « ${s.choix.title} »` + (s.nombre > 1 ? ` (première des ${s.nombre} mentions)` : "");
      b.textContent = `🔗 ${s.choix.title}` + (s.nombre > 1 ? ` ×${s.nombre}` : "");
      b.addEventListener("click", () => lier(s.choix.link));
      zoneLiens.append(b);
    }
    if (avantFermeture) {
      const fermer = document.createElement("button");
      fermer.type = "button";
      fermer.className = "button small primary";
      fermer.textContent = "Enregistrer et fermer sans lier";
      fermer.addEventListener("click", () => {
        fermerSansLier = true;
        form.requestSubmit(form.querySelector("[data-submit]"));
      });
      zoneLiens.append(fermer);
    }
    const masquer = document.createElement("button");
    masquer.type = "button";
    masquer.className = "link-button editor-liens-masquer";
    masquer.setAttribute("aria-label", "Masquer les suggestions de liens");
    masquer.textContent = "✕";
    masquer.addEventListener("click", () => { zoneLiens.hidden = true; });
    zoneLiens.append(masquer);
    zoneLiens.hidden = false;
    return liste;
  }

  // « Enregistrer et fermer » : le formulaire classique, avec le texte de l'éditeur.
  // S'il reste des pages citées sans lien jamais proposées, on les montre d'abord (une fois).
  form.addEventListener("submit", (e) => {
    if (!fermerSansLier && zoneLiens && suggestionsDeLiens().some((x) => !dejaProposes.has(x.choix.link))) {
      e.preventDefault();
      afficherLiens(true);
      zoneLiens.scrollIntoView({ block: "nearest" });
      return;
    }
    textarea.value = view.state.doc.toString();
    submitting = true;
    oublierBrouillon();
  });
  window.addEventListener("beforeunload", (e) => {
    if (!submitting && isDirty()) e.preventDefault();
  });
})();

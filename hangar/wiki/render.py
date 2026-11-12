"""Conversion du Markdown « façon Obsidian » en HTML.

Syntaxe prise en charge en plus du Markdown classique (tableaux, notes de bas
de page, listes de tâches...) :

- liens internes ``[[Page]]``, ``[[Page|texte]]``, ``[[Page#Titre]]``,
  ``[[Page#^bloc]]`` et ``[texte](Page.md)`` ;
- intégrations ``![[image.png|300]]``, ``![[Note]]``, ``![[Note#Titre]]``,
  audio, vidéo et PDF ;
- callouts ``> [!note] Titre`` (repliables avec ``+`` ou ``-``) ;
- tags ``#tag``, surlignage ``==texte==``, barré ``~~texte~~``,
  commentaires ``%% ... %%``, formules ``$...$`` / ``$$...$$``,
  diagrammes ````mermaid``, propriétés YAML.

Port de ``wiki/render.py`` de Revision (``reverse`` → ``url_for``).
"""

import html
import re
import threading
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit

import markdown
import nh3
from markdown.extensions.toc import slugify_unicode

from .liens import wiki_url
from .vault import (COMMENT_RE, FENCED_CODE_RE, TAG_RE, WIKILINK_RE, Attachment, Note, map_outside_code,
                    parse_link_target)

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".avif"}
AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".webm", ".3gp"}
VIDEO_EXTS = {".mp4", ".webm", ".ogv", ".mov", ".mkv"}
MAX_EMBED_DEPTH = 3

CALLOUT_RE = re.compile(r"^(?P<indent>[ \t]*)>[ \t]?\[!(?P<type>[\w-]+)\](?P<fold>[+-]?)[ \t]*(?P<title>.*)$")
CALLOUT_ALIASES = {
    "summary": "abstract", "tldr": "abstract", "hint": "tip", "important": "tip",
    "check": "success", "done": "success", "help": "question", "faq": "question",
    "caution": "warning", "attention": "warning", "fail": "failure", "missing": "failure",
    "error": "danger", "cite": "quote",
}
FENCE_LINE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})")
HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
MATH_BLOCK_RE = re.compile(r"\$\$(.+?)\$\$", re.S)
MATH_INLINE_RE = re.compile(r"(?<![\\$])\$(?=[^\s$])([^$\n]+?)(?<=[^\s\\])\$(?!\d)")
BLOCK_ID_RE = re.compile(r"(?<=\s)\^([\w-]+)[ \t]*$", re.M)
# Obsidian accepte une liste ou un tableau juste après un paragraphe, pas Python-Markdown.
# Appliquées au texte entier (hors blocs de code) : voir _separer_listes_et_tableaux.
LIST_AFTER_TEXT_RE = re.compile(r"^((?![-*+][ \t]|\d+[.)][ \t]|\|)[^\s>][^\n]*)\n(?=(?:[-*+]|\d+[.)])[ \t]+\S)", re.M)
TABLE_AFTER_TEXT_RE = re.compile(r"^([^\s|>][^\n]*)\n(?=\|)", re.M)
URL_ATTR_RE = re.compile(r'(<(?:a|img|audio|video|source)\b[^>]*?\b(?:href|src)=")([^"]*)(")')
# Images hébergées sur un autre site : jamais chargées par le wiki (100 % local).
IMG_OR_LINK_RE = re.compile(r"<img\b[^>]*>|<a\b[^>]*>|</a\s*>", re.I)
EXTERNAL_URL_RE = re.compile(r"^(?:https?:)?//", re.I)
LEGENDE_MAX = 120
TOKEN_RE = re.compile(r"<p>(obsidianph\d+z)</p>|(obsidianph\d+z)")

MARKDOWN_EXTENSIONS = [
    "extra", "sane_lists", "nl2br", "toc",
    "pymdownx.tasklist", "pymdownx.mark", "pymdownx.tilde", "pymdownx.magiclink",
]
MARKDOWN_CONFIG = {
    "toc": {"slugify": slugify_unicode},
    "pymdownx.tilde": {"subscript": False},
}


# Nettoyage du HTML final (anti-XSS) : le Markdown laisse passer le HTML brut, donc
# une note ou un QCM pourrait contenir <script>, onerror=..., javascript:...
# Seuls les balises et attributs produits par le wiki sont conservés.
ALLOWED_TAGS = {
    "a", "abbr", "audio", "b", "blockquote", "br", "caption", "code", "col", "colgroup", "dd", "del",
    "details", "div", "dl", "dt", "em", "figcaption", "figure", "h1", "h2", "h3", "h4", "h5", "h6", "hr",
    "i", "iframe", "img", "input", "ins", "kbd", "label", "li", "mark", "ol", "p", "pre", "s", "small",
    "source", "span", "strong", "sub", "summary", "sup", "table", "tbody", "td", "tfoot", "th", "thead",
    "tr", "u", "ul", "video",
}
ALLOWED_ATTRIBUTES = {
    "*": {"class", "id", "title"},
    "a": {"href"},
    "img": {"src", "alt", "width", "height", "loading"},
    "audio": {"src", "controls"},
    "video": {"src", "controls", "width", "height"},
    "source": {"src", "type"},
    "iframe": {"src", "allowfullscreen"},
    "input": {"type", "checked", "disabled"},
    "details": {"open", "data-callout"},
    "div": {"data-callout"},
    "ol": {"start"},
    "th": {"align", "style", "colspan", "rowspan"},
    "td": {"align", "style", "colspan", "rowspan"},
}
LOCAL_ONLY = {("iframe", "src"), ("img", "src"), ("audio", "src"), ("video", "src"), ("source", "src")}


def _filter_attribute(tag, attr, value):
    # Médias et cadres : uniquement les fichiers du wiki (pas de ressource externe).
    if (tag, attr) in LOCAL_ONLY and not (value.startswith("/") and not value.startswith("//")):
        return None
    if tag == "input" and attr == "type" and value != "checkbox":
        return None
    return value


def sanitize(fragment):
    return nh3.clean(
        fragment,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        attribute_filter=_filter_attribute,
        url_schemes={"http", "https", "mailto"},
        filter_style_properties={"text-align"},
        link_rel=None,
    )


# Créer un ``markdown.Markdown`` (chargement des extensions) coûte plus cher que la
# conversion d'une petite note : les instances sont réutilisées, une par fil d'exécution.
_parsers = threading.local()


def _markdown_parser():
    pool = getattr(_parsers, "pool", None)
    if pool is None:
        pool = _parsers.pool = []
    if pool:
        return pool.pop()
    return markdown.Markdown(extensions=MARKDOWN_EXTENSIONS, extension_configs=MARKDOWN_CONFIG)


def _release_markdown_parser(md):
    md.reset()
    _parsers.pool.append(md)


def heading_anchor(fragment):
    """Ancre HTML d'un titre (``Titre``) ou d'un bloc (``^id``)."""
    fragment = unquote(fragment).split("#")[-1].strip()
    if fragment.startswith("^"):
        return fragment
    return slugify_unicode(fragment, "-")


def extract_section(text, fragment):
    """Extrait d'une note la section d'un titre ou le paragraphe d'un bloc ``^id``."""
    fragment = fragment.split("#")[-1].strip()
    if fragment.startswith("^"):
        pattern = re.compile(rf"\s\^{re.escape(fragment[1:])}[ \t]*$", re.M)
        return next((b for b in re.split(r"\n[ \t]*\n", text) if pattern.search(b)), "")
    wanted, level, lines = heading_anchor(fragment), None, []
    for line in text.split("\n"):
        m = HEADING_RE.match(line)
        if level is None:
            if m and heading_anchor(m.group(2)) == wanted:
                level = len(m.group(1))
                lines.append(line)
        elif m and len(m.group(1)) <= level:
            break
        else:
            lines.append(line)
    return "\n".join(lines)


def escape(value):
    return html.escape(str(value), quote=True)


def _separer_listes_et_tableaux(text):
    """Ligne vide avant une liste ou un tableau collé à un paragraphe, comme Obsidian.

    À appliquer au texte entier, hors blocs de code seulement : appliquées morceau par morceau
    entre deux ``code`` en ligne, ces règles prenaient la suite d'un code (« `x`, fin | ») pour un
    début de ligne et coupaient le tableau ou la liste en deux."""
    text = LIST_AFTER_TEXT_RE.sub(r"\1\n\n", text)
    return TABLE_AFTER_TEXT_RE.sub(r"\1\n\n", text)


class _LecteurBalise(HTMLParser):
    """Attributs d'une balise HTML isolée (``<img src=… alt=…>``), entités décodées."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.attributs = {}

    def handle_starttag(self, tag, attrs):
        self.attributs = {nom: valeur or "" for nom, valeur in attrs}

    handle_startendtag = handle_starttag


def attributs_html(balise):
    lecteur = _LecteurBalise()
    lecteur.feed(balise)
    lecteur.close()
    return lecteur.attributs


def images_externes_en_liens(fragment):
    """Remplace chaque image hébergée sur un autre site (``![légende](https://…)`` ou
    ``<img src="https://…">``) par un lien « Image externe : légende ↗ » vers son adresse.

    Le nettoyage du HTML retire ces images (aucune ressource externe n'est chargée) : sans ce
    lien, il ne restait qu'une balise vide, sans légende visible ni moyen d'ouvrir l'image. Une
    image placée dans un lien devient un simple texte (pas de lien dans un lien)."""
    profondeur = 0

    def remplacer(m):
        nonlocal profondeur
        balise = m.group(0)
        if balise.startswith("</"):
            profondeur = max(0, profondeur - 1)
            return balise
        if balise[1] in "aA":
            profondeur += 1
            return balise
        attributs = attributs_html(balise)
        src = attributs.get("src", "").strip()
        if not EXTERNAL_URL_RE.match(src):
            return balise
        url = "https:" + src if src.startswith("//") else src
        legende = " ".join(attributs.get("alt", "").split())
        courte = legende if len(legende) <= LEGENDE_MAX else legende[:LEGENDE_MAX].rstrip() + "…"
        texte = f"Image externe : {courte}" if courte else "Image externe"
        hote = urlsplit(url).hostname or url
        titre = f"Image hébergée sur {hote}, pas enregistrée dans le wiki"
        if courte != legende:
            titre = f"{legende} — {titre}"
        if profondeur:
            return f'<span class="image-externe" title="{escape(titre)}">{escape(texte)}</span>'
        return (f'<a class="image-externe" href="{escape(url)}" title="{escape(titre + " : clique pour l’ouvrir")}">'
                f"{escape(texte)} ↗</a>")

    return IMG_OR_LINK_RE.sub(remplacer, fragment)


class Renderer:
    def __init__(self, vault, note, depth=0, parent=None):
        self.vault = vault
        self.note = note
        self.depth = depth
        self.stash = parent.stash if parent else []
        self.chain = (parent.chain if parent else ()) + (note,)
        self.is_root = parent is None
        self.toc = ""

    def child(self, note=None, depth=None):
        return Renderer(self.vault, note or self.note, self.depth if depth is None else depth, parent=self)

    def convert(self, text):
        text = map_outside_code(text, lambda s: COMMENT_RE.sub("", s))
        text = self._callouts(text)
        text = map_outside_code(text, self._inline)
        text = map_outside_code(text, _separer_listes_et_tableaux, FENCED_CODE_RE)
        md = _markdown_parser()
        try:
            out = md.convert(text)
            if md.toc_tokens:
                self.toc = md.toc
        finally:
            _release_markdown_parser(md)
        out = URL_ATTR_RE.sub(self._rewrite_url, out)
        out = images_externes_en_liens(out)
        out = self._unstash(out)
        # Nettoyé une seule fois, sur le HTML final (les intégrations en font partie).
        return sanitize(out) if self.is_root else out

    def convert_inline(self, text):
        out = self.child().convert(text)
        out = re.sub(r"^<p>(.*)</p>$", r"\1", out, flags=re.S)
        return sanitize(out) if self.is_root else out

    # -- Espaces réservés : protège le HTML généré du moteur Markdown ----------

    def _keep(self, fragment):
        self.stash.append(fragment)
        return f"obsidianph{len(self.stash) - 1}z"

    def _unstash(self, out):
        for _ in range(10):
            new = TOKEN_RE.sub(lambda m: self.stash[int((m.group(1) or m.group(2))[10:-1])], out)
            if new == out:
                break
            out = new
        return out

    # -- Syntaxe Obsidian -----------------------------------------------------

    def _inline(self, segment):
        segment = MATH_BLOCK_RE.sub(
            lambda m: "\n\n" + self._keep(f'<div class="math math-block">\\[{escape(m.group(1))}\\]</div>') + "\n\n",
            segment,
        )
        segment = MATH_INLINE_RE.sub(
            lambda m: self._keep(f'<span class="math math-inline">\\({escape(m.group(1))}\\)</span>'), segment
        )
        segment = WIKILINK_RE.sub(self._wikilink, segment)
        segment = TAG_RE.sub(self._tag, segment)
        return BLOCK_ID_RE.sub(
            lambda m: self._keep(f'<span class="block-anchor" id="^{escape(m.group(1))}"></span>'), segment
        )

    def _tag(self, m):
        tag = m.group(1)
        url = wiki_url("wiki.tag", tag.lower())
        return self._keep(f'<a class="tag" href="{escape(url)}">#{escape(tag)}</a>')

    def _wikilink(self, m):
        page, fragment, alias = parse_link_target(m.group(2))
        target = self.vault.resolve(page, self.note)
        if m.group(1):
            return self._keep(self._embed(page, fragment, alias, target))
        return self._keep(self._link(page, fragment, alias, target))

    def _link(self, page, fragment, alias, target):
        if alias:
            label = alias
        elif not page:
            label = fragment.lstrip("^")
        elif fragment:
            label = f"{page} > {fragment.lstrip('^')}"
        else:
            label = page
        if target is None:
            url = wiki_url("wiki.page", page)
            return f'<a class="internal-link is-unresolved" href="{escape(url)}" title="Page à créer">{escape(label)}</a>'
        url = (target.view_url if isinstance(target, Attachment) else target.url) if page else ""
        if fragment and isinstance(target, Note):
            url += "#" + heading_anchor(fragment)
        return f'<a class="internal-link" href="{escape(url)}">{escape(label)}</a>'

    def _embed(self, page, fragment, alias, target):
        if target is None:
            return self._link(page, fragment, alias, target)
        if isinstance(target, Attachment):
            url = escape(target.url)
            if target.suffix in IMAGE_EXTS:
                size = re.fullmatch(r"(\d+)(?:x(\d+))?", alias)
                attrs = ""
                if size:
                    attrs = f' width="{size.group(1)}"' + (f' height="{size.group(2)}"' if size.group(2) else "")
                alt = target.path.stem if size or not alias else alias
                return f'<img class="embed-image" src="{url}" alt="{escape(alt)}"{attrs} loading="lazy">'
            if target.suffix in AUDIO_EXTS and target.suffix != ".webm":
                return f'<audio class="embed-audio" controls src="{url}"></audio>'
            if target.suffix in VIDEO_EXTS:
                return f'<video class="embed-video" controls src="{url}"></video>'
            if target.suffix == ".pdf":
                # Lecteur intégré (PDF.js) : marche aussi sur téléphone, contrairement au lecteur du navigateur.
                page_number = re.fullmatch(r"page=(\d+)", fragment)
                src = target.view_url + "?integre=1" + (f"#page={page_number.group(1)}" if page_number else "")
                return f'<iframe class="embed-pdf" src="{escape(src)}" title="{escape(target.name)}" allowfullscreen></iframe>'
            return self._link(page, fragment, alias, target)

        if self.depth >= MAX_EMBED_DEPTH or (target in self.chain and not fragment):
            return self._link(page, fragment, alias, target)
        text = extract_section(target.body, fragment) if fragment else target.body
        content = self.child(target, self.depth + 1).convert(text)
        link = self._link(page, fragment, alias or target.title, target)
        return (
            f'<div class="markdown-embed"><div class="markdown-embed-title">{link}</div>'
            f'<div class="markdown-embed-content">{content}</div></div>'
        )

    def _callouts(self, text):
        lines, out, fence, i = text.split("\n"), [], None, 0
        while i < len(lines):
            line = lines[i]
            fence_match = FENCE_LINE_RE.match(line)
            if fence_match:
                marker = fence_match.group(1)[0]
                fence = marker if fence is None else (None if marker == fence else fence)
            m = None if fence else CALLOUT_RE.match(line)
            i += 1
            if not m:
                out.append(line)
                continue
            indent, body = m.group("indent"), []
            prefix = re.compile(rf"^{re.escape(indent)}>[ \t]?")
            while i < len(lines) and prefix.match(lines[i]):
                body.append(prefix.sub("", lines[i], count=1))
                i += 1
            out.extend(["", indent + self._keep(self._callout(m, "\n".join(body))), ""])
        return "\n".join(out)

    def _callout(self, m, body):
        kind = m.group("type").lower()
        kind = CALLOUT_ALIASES.get(kind, kind)
        title = self.convert_inline(m.group("title").strip()) if m.group("title").strip() else m.group("type").capitalize()
        content = self.child().convert(body) if body.strip() else ""
        content = f'<div class="callout-content">{content}</div>' if content else ""
        if m.group("fold"):
            is_open = " open" if m.group("fold") == "+" else ""
            return (
                f'<details class="callout" data-callout="{escape(kind)}"{is_open}>'
                f'<summary class="callout-title">{title}</summary>{content}</details>'
            )
        return f'<div class="callout" data-callout="{escape(kind)}"><div class="callout-title">{title}</div>{content}</div>'

    def _rewrite_url(self, m):
        """Fait pointer les liens Markdown relatifs (``Note.md``, ``image.png``) vers le wiki."""
        url = html.unescape(m.group(2))
        if not url or url.startswith(("#", "/")) or re.match(r"^[a-zA-Z][\w+.-]*:", url):
            return m.group(0)
        path, _, fragment = url.partition("#")
        target = self.vault.resolve(unquote(path), self.note)
        if target is None:
            return m.group(0)
        new = target.url
        if fragment and isinstance(target, Note):
            new += "#" + heading_anchor(fragment)
        return m.group(1) + escape(new) + m.group(3)


def render_note(vault, note):
    """Renvoie ``(html, table_des_matières)`` pour une note du coffre."""
    renderer = Renderer(vault, note)
    return renderer.convert(note.body), sanitize(renderer.toc)

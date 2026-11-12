"""Rendu des énoncés de QCM (Markdown façon Obsidian, formules LaTeX, images du coffre).

Revision rend les questions avec le moteur du wiki (`wiki.render.Renderer`). Dans Hangar, le
wiki est un module à part (`hangar.wiki`) : on l'utilise quand il est là et qu'il expose le
même moteur ; sinon (ou s'il échoue), un rendu local suffit aux QCM :

- formules `$…$` et `$$…$$` protégées du Markdown et rendues dans le navigateur par KaTeX
  (même balisage que le wiki : `<span class="math math-inline">\\(…\\)</span>`) ;
- images du coffre `![[schema.png|300]]` (servies par la route `qcm.fichier`), liens `[[Page]]`
  vers le wiki, `==surlignage==`, commentaires `%%…%%` ;
- HTML nettoyé (nh3) : rien d'actif, médias locaux seulement.
"""
from __future__ import annotations

import html
import re
from pathlib import PurePosixPath

import markdown
import nh3
from flask import current_app, url_for

from ..securite import local_url
from . import coffre

MATH_BLOCK_RE = re.compile(r"\$\$(.+?)\$\$", re.S)
MATH_INLINE_RE = re.compile(r"(?<![\\$])\$(?=[^\s$])([^$\n]+?)(?<=[^\s\\])\$(?!\d)")
WIKILINK_RE = re.compile(r"(!?)\[\[([^\[\]\n]+?)\]\]")
COMMENT_RE = re.compile(r"%%.*?%%", re.S)
HIGHLIGHT_RE = re.compile(r"(?<!=)==(?=\S)(.+?)(?<=\S)==(?!=)")
CODE_RE = re.compile(r"^[ \t]*(?P<fence>`{3,}|~{3,})[^\n]*\n.*?^[ \t]*(?P=fence)[ \t]*$|(?P<tick>`+)[^`\n]+?(?P=tick)", re.M | re.S)
TOKEN_RE = re.compile(r"<p>(qcmph\d+z)</p>|(qcmph\d+z)")

ALLOWED_TAGS = {
    "a", "abbr", "audio", "b", "blockquote", "br", "caption", "code", "dd", "del", "details", "div", "dl", "dt",
    "em", "figcaption", "figure", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img", "input", "kbd", "li", "mark",
    "ol", "p", "pre", "s", "small", "source", "span", "strong", "sub", "summary", "sup", "table", "tbody", "td",
    "tfoot", "th", "thead", "tr", "u", "ul", "video",
}
ALLOWED_ATTRIBUTES = {
    "*": {"class", "title"},
    "a": {"href"},
    "img": {"src", "alt", "width", "height", "loading"},
    "audio": {"src", "controls"},
    "video": {"src", "controls", "width", "height"},
    "source": {"src", "type"},
    "input": {"type", "checked", "disabled"},
    "ol": {"start"},
    "th": {"align", "colspan", "rowspan"},
    "td": {"align", "colspan", "rowspan"},
}
LOCAL_ONLY = {("img", "src"), ("audio", "src"), ("video", "src"), ("source", "src")}


def _filtre(tag, attr, value):
    if (tag, attr) in LOCAL_ONLY and not local_url(value):
        return None  # aucune ressource externe (« //site », « /\site » ou « /<tabulation>/site » compris)
    if tag == "input" and attr == "type" and value != "checkbox":
        return None
    return value


def nettoyer(fragment: str) -> str:
    return nh3.clean(fragment, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRIBUTES, attribute_filter=_filtre,
                     url_schemes={"http", "https", "mailto"}, link_rel="noopener noreferrer")


# ---------------------------------------------------------------------------
# Moteur du wiki (s'il est là)
# ---------------------------------------------------------------------------
_WIKI = {"essaye": False, "ok": False}


def _moteur_wiki():
    """(Renderer, Note, get_vault) du module wiki, ou None."""
    if not _WIKI["essaye"]:
        _WIKI["essaye"] = True
        try:
            from hangar.wiki.render import Renderer
            from hangar.wiki.vault import Note, get_vault
            _WIKI.update(ok=True, Renderer=Renderer, Note=Note, get_vault=get_vault)
        except Exception:  # noqa: BLE001 — wiki absent ou d'une autre forme : rendu local
            _WIKI["ok"] = False
    if not _WIKI["ok"] or current_app.config.get("QCM_RENDU_LOCAL"):
        return None
    return _WIKI


def _rendu_wiki(text: str, block: bool):
    w = _moteur_wiki()
    if w is None:
        return None
    try:
        vault = w["get_vault"]()
        note = w["Note"](PurePosixPath("_qcm_.md"), 0, {}, text)
        renderer = w["Renderer"](vault, note)
        return renderer.convert(text) if block else renderer.convert_inline(text)
    except Exception:  # noqa: BLE001 — moteur différent de celui de Revision : rendu local
        current_app.logger.debug("Rendu du wiki indisponible pour les QCM", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Rendu local
# ---------------------------------------------------------------------------
def _hors_code(text, fn):
    out, pos = [], 0
    for m in CODE_RE.finditer(text):
        out.append(fn(text[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(fn(text[pos:]))
    return "".join(out)


def _lien_wiki(page: str) -> str:
    try:
        return url_for("wiki.page", path=page)
    except Exception:  # noqa: BLE001 — route du wiki inconnue
        return ""


def _wikilien(m, garder):
    cible, _, alias = m.group(2).partition("|")
    cible = cible.rstrip("\\").strip()
    page, _, fragment = cible.partition("#")
    if m.group(1):  # intégration : image ou média du coffre
        trouve = coffre.resoudre(page)
        if trouve is not None:
            src = html.escape(url_for("qcm.fichier", path=str(trouve)))
            ext = trouve.suffix.lower()
            if ext in coffre.IMAGE_EXTS:
                taille = re.fullmatch(r"(\d+)(?:x(\d+))?", alias.strip())
                attrs = ""
                if taille:
                    attrs = f' width="{taille.group(1)}"' + (f' height="{taille.group(2)}"' if taille.group(2) else "")
                alt = trouve.stem if taille or not alias else alias
                return garder(f'<img class="embed-image" src="{src}" alt="{html.escape(alt)}"{attrs} loading="lazy">')
            if ext in {".mp3", ".wav", ".ogg", ".m4a", ".flac"}:
                return garder(f'<audio class="embed-audio" controls src="{src}"></audio>')
            if ext in {".mp4", ".webm", ".ogv", ".mov"}:
                return garder(f'<video class="embed-video" controls src="{src}"></video>')
            return garder(f'<a href="{src}">{html.escape(alias or trouve.name)}</a>')
    label = alias.strip() or (f"{page} > {fragment}" if fragment and page else page or fragment)
    url = _lien_wiki(page) if page else ""
    if url:
        return garder(f'<a class="internal-link" href="{html.escape(url)}">{html.escape(label)}</a>')
    return garder(f'<span class="internal-link">{html.escape(label)}</span>')


def rendu_local(text: str, block: bool = True) -> str:
    stash: list[str] = []

    def garder(fragment):
        stash.append(fragment)
        return f"qcmph{len(stash) - 1}z"

    def segment(s):
        s = COMMENT_RE.sub("", s)
        s = MATH_BLOCK_RE.sub(
            lambda m: "\n\n" + garder(f'<div class="math math-block">\\[{html.escape(m.group(1))}\\]</div>') + "\n\n", s)
        s = MATH_INLINE_RE.sub(
            lambda m: garder(f'<span class="math math-inline">\\({html.escape(m.group(1))}\\)</span>'), s)
        s = WIKILINK_RE.sub(lambda m: _wikilien(m, garder), s)
        return HIGHLIGHT_RE.sub(lambda m: garder("<mark>") + m.group(1) + garder("</mark>"), s)

    text = _hors_code(text, segment)
    out = markdown.markdown(text, extensions=["extra", "sane_lists", "nl2br"], output_format="html")
    for _ in range(5):
        new = TOKEN_RE.sub(lambda m: stash[int((m.group(1) or m.group(2))[5:-1])], out)
        if new == out:
            break
        out = new
    if not block:
        out = re.sub(r"^<p>(.*)</p>$", r"\1", out.strip(), flags=re.S)
    return nettoyer(out)


_CACHE: dict = {"cle": None, "html": {}}
_CACHE_MAX = 50_000


def rendre(text, block: bool = True) -> str:
    """HTML d'un énoncé, d'un choix (block=False : sans paragraphe) ou d'une explication.

    Gardé en mémoire tant que le coffre ne change pas (un set de 1 000 questions se rend
    en une fraction de seconde au deuxième affichage)."""
    if not text:
        return ""
    text = str(text)
    racine = coffre.racine()
    cle = coffre.signature(racine)
    if _CACHE["cle"] is not cle or _CACHE.get("racine") != racine:
        _CACHE.update(cle=cle, racine=racine, html={})
    deja = _CACHE["html"].get((text, block))
    if deja is not None:
        return deja
    out = _rendu_wiki(text, block)
    out = out if out is not None else rendu_local(text, block)
    if len(_CACHE["html"]) < _CACHE_MAX:
        _CACHE["html"][(text, block)] = out
    return out

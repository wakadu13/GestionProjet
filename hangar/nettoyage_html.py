"""Nettoyage du HTML rendu depuis du Markdown : une seule règle pour tout Hangar (tickets, pages, e-mails,
réponses de l'IA, wiki, QCM).

Le Markdown laisse passer le HTML brut : une note, un ticket ou une réponse de l'IA pourrait contenir
``<script>``, ``onerror=…``, ``javascript:…``. Chaque rendu garde sa liste de balises et d'attributs (le wiki
a ses callouts, ses cadres PDF…), mais tous passent par ``nettoyer`` :

- adresses des liens : ``http``, ``https`` et ``mailto`` seulement (ni ``javascript:`` ni ``data:``) ;
- médias locaux seulement : une image, un son, une vidéo n'est chargé que s'il est sur Hangar
  (``securite.local_url``) ; un cadre (``iframe``) seulement vers les adresses données (lecteur PDF et
  fichiers du wiki), jamais ailleurs ;
- une image hébergée sur un autre site écrite en HTML (``<img src="https://…">``) devient un lien
  « Image externe : légende ↗ » (rien n'est chargé sans clic), comme ``![…](https://…)`` avec
  ``securite.ExternalImages`` ;
- case à cocher seulement pour ``<input>`` (listes de tâches).
"""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

import nh3

from .securite import local_url

URL_SCHEMES = frozenset({"http", "https", "mailto"})
MEDIAS = frozenset({("img", "src"), ("audio", "src"), ("video", "src"), ("source", "src"), ("iframe", "src")})

IMG_OR_LINK_RE = re.compile(r"<img\b[^>]*>|<a\b[^>]*>|</a\s*>", re.I)
EXTERNAL_URL_RE = re.compile(r"^(?:https?:)?//", re.I)
LEGENDE_MAX = 120


def filtre_attributs(prefixes_cadre: tuple[str, ...] = ()):
    """Filtre d'attributs nh3 : médias locaux seulement, cadres vers ``prefixes_cadre`` seulement."""

    def filtre(tag, attr, value):
        if (tag, attr) in MEDIAS:
            if tag == "iframe" and not prefixes_cadre:
                return None
            if not local_url(value, prefixes_cadre if tag == "iframe" else ()):
                return None  # aucune ressource externe (« //site », « /\site », « /<tabulation>/site » compris)
        if tag == "input" and attr == "type" and value != "checkbox":
            return None
        return value

    return filtre


_FILTRE = filtre_attributs()


def nettoyer(fragment: str, tags, attributes, *, prefixes_cadre: tuple[str, ...] = (),
             link_rel: str | None = "noopener noreferrer", styles=None, images_externes: bool = True,
             lieu: str = "Hangar") -> str:
    """HTML sûr : seules les balises ``tags`` et les attributs ``attributes`` restent, selon la règle commune.

    ``images_externes`` : change d'abord les images d'un autre site en liens (``lieu`` complète leur
    info-bulle : « pas enregistrée dans … ») ; ``styles`` : propriétés CSS gardées dans ``style``."""
    if images_externes:
        fragment = images_externes_en_liens(fragment, lieu)
    return nh3.clean(fragment, tags=set(tags), attributes={k: set(v) for k, v in attributes.items()},
                     attribute_filter=filtre_attributs(prefixes_cadre) if prefixes_cadre else _FILTRE,
                     url_schemes=set(URL_SCHEMES), filter_style_properties=styles, link_rel=link_rel)


# ---------------------------------------------------------------------------
# Images externes écrites en HTML → liens
# ---------------------------------------------------------------------------
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


def _escape(value):
    return html.escape(str(value), quote=True)


def images_externes_en_liens(fragment, lieu="le wiki"):
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
        titre = f"Image hébergée sur {hote}, pas enregistrée dans {lieu}"
        if courte != legende:
            titre = f"{legende} — {titre}"
        if profondeur:
            return f'<span class="image-externe" title="{_escape(titre)}">{_escape(texte)}</span>'
        return (f'<a class="image-externe" href="{_escape(url)}" title="{_escape(titre + " : clique pour l’ouvrir")}">'
                f"{_escape(texte)} ↗</a>")

    return IMG_OR_LINK_RE.sub(remplacer, fragment)

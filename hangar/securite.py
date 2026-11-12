"""Garde-fous de sécurité communs (tickets, wiki, QCM, IA).

- ``clean_color`` : une couleur saisie (état, étiquette, projet, avatar, pense-bête) n'est gardée qu'au format
  ``#rrggbb``, celui des sélecteurs de couleur et des pastilles de l'interface.
- ``local_url`` : adresse sur Hangar même (« /… »), en déjouant les formes qu'un navigateur lit comme une
  adresse externe (« // », « /\\ », tabulation ou retour à la ligne glissés dedans).
- ``media_filter`` / ``ExternalImages`` : une image n'est chargée que si elle est sur Hangar ; une image
  externe d'un texte Markdown devient un simple lien (rien n'est chargé sans clic), ou son seul texte si elle
  est déjà dans un lien (badge, miniature cliquable).
- ``attachment_type`` / ``protect_file`` : un fichier envoyé par un utilisateur n'est affiché dans le navigateur
  que si c'est une image sûre ou un PDF, d'après son extension, jamais d'après le type annoncé par
  l'expéditeur ; il ne peut jamais exécuter de script (nosniff, CSP « sandbox »).
- ``init_app`` : politique d'images de toutes les pages : rien n'est chargé hors de Hangar.
"""
from __future__ import annotations

import os
import re
from urllib.parse import unquote
from xml.etree import ElementTree

from markdown.extensions import Extension
from markdown.treeprocessors import Treeprocessor

# ---------------------------------------------------------------------------
# Couleurs
# ---------------------------------------------------------------------------
COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")


def clean_color(value, default):
    """La couleur reçue si elle est au format #rrggbb, sinon ``default`` (valeur actuelle ou couleur par défaut)."""
    value = (value or "").strip() if isinstance(value, str) else ""
    return value if COLOR_RE.fullmatch(value) else default


# ---------------------------------------------------------------------------
# Adresses locales
# ---------------------------------------------------------------------------
# Le navigateur retire tabulations et retours à la ligne d'une adresse, et lit « \\ » comme « / ».
_IGNORES = re.compile(r"[\t\n\r]")


def local_url(value, prefixes: tuple[str, ...] = ()) -> bool:
    """Adresse sur Hangar même (« /… », ni « //… » ni « /\\… »). Avec ``prefixes``, son chemin doit aussi
    commencer par l'un d'eux, sans segment « .. » ni « . » (même encodé) pour en sortir."""
    v = _IGNORES.sub("", value or "")
    if not v.startswith("/") or v[1:2] in ("/", "\\"):
        return False
    if prefixes:
        path = unquote(re.split(r"[?#]", v, maxsplit=1)[0])
        if "\\" in path or any(seg in (".", "..") for seg in path.split("/")):
            return False
        return path.startswith(prefixes)
    return True


MEDIA_ATTRS = {("img", "src"), ("audio", "src"), ("video", "src"), ("source", "src")}


def media_filter(tag, attr, value):
    """Filtre d'attributs nh3 : images et médias seulement depuis Hangar (aucune ressource externe)."""
    if (tag, attr) in MEDIA_ATTRS and not local_url(value):
        return None
    return value


class _ExternalImagesAsLinks(Treeprocessor):
    def run(self, root):
        sans_texte: list = []
        self._parcourir(root, None, sans_texte)
        for lien in sans_texte:  # lien qui ne contenait qu'une image sans texte : il doit rester cliquable
            if not "".join(lien.itertext()).strip():
                lien.text = (lien.text or "") + "image externe"

    def _parcourir(self, parent, lien, sans_texte):
        for el in list(parent):
            if el.tag != "img" or local_url(el.get("src", "")):
                self._parcourir(el, el if lien is None and el.tag == "a" else lien, sans_texte)
            elif lien is None:
                parent[list(parent).index(el)] = self._en_lien(el)
            else:
                # Image dans un lien ([![texte](https://…/image.png)](https://…/cible), badge, miniature) :
                # un second lien serait imbriqué dans le premier, que nh3 viderait. On garde le lien voulu
                # et on y met le texte de l'image.
                alt = el.get("alt") or ""
                if not alt:
                    sans_texte.append(lien)
                self._en_texte(parent, el, alt)

    @staticmethod
    def _en_lien(el):
        src = el.get("src", "")
        a = ElementTree.Element("a", {"href": src})  # nh3 retire ensuite data:, javascript:…
        if re.match(r"https?://", src, re.I):
            a.set("title", "Image externe, non chargée : " + (src if len(src) <= 200 else src[:199] + "…"))
        a.text = el.get("alt") or "image externe"
        a.tail = el.tail
        return a

    @staticmethod
    def _en_texte(parent, el, texte):
        enfants = list(parent)
        i = enfants.index(el)
        avant = enfants[i - 1].tail if i else parent.text
        if texte and avant and not avant[-1].isspace():
            texte = " " + texte  # deux images côte à côte : « a b », pas « ab »
        texte += el.tail or ""
        if i:
            enfants[i - 1].tail = (enfants[i - 1].tail or "") + texte
        else:
            parent.text = (parent.text or "") + texte
        parent.remove(el)


class ExternalImages(Extension):
    """Extension Markdown : ``![texte](https://…)`` devient un lien « texte » au lieu d'une image chargée
    à l'affichage (qui révélerait à ce site que la page est lue, et ce que contient son adresse). Dans un lien
    (``[![texte](https://…/image.png)](https://…/cible)``), elle laisse son texte au lien, qui garde sa cible."""

    def extendMarkdown(self, md):  # noqa: N802 - nom imposé par python-markdown
        md.treeprocessors.register(_ExternalImagesAsLinks(md), "hangar_images_externes", 15)


# ---------------------------------------------------------------------------
# Fichiers envoyés par les utilisateurs
# ---------------------------------------------------------------------------
# Seuls types affichés dans le navigateur, d'après l'extension (dictionnaire fixe : ``mimetypes``
# dépend du registre sous Windows).
INLINE_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
    ".webp": "image/webp", ".pdf": "application/pdf",
}
# Vignette d'un SVG (<img> n'exécute jamais de script) : téléchargé s'il est ouvert seul.
DOWNLOAD_TYPES = {".svg": "image/svg+xml"}
SANDBOX_CSP = "default-src 'none'; img-src 'self'; media-src 'self'; sandbox"


def attachment_type(filename: str) -> tuple[str, bool]:
    """(type envoyé, affichable dans le navigateur) d'une pièce jointe, d'après son extension."""
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in INLINE_TYPES:
        return INLINE_TYPES[ext], True
    return DOWNLOAD_TYPES.get(ext, "application/octet-stream"), False


def protect_file(response, pdf: bool = False):
    """En-têtes d'un fichier venu d'un utilisateur : le navigateur ne devine pas son type et, ouvert seul,
    il ne peut rien exécuter. Pas de « sandbox » pour un PDF : le lecteur du navigateur ne s'ouvrirait plus."""
    response.headers["X-Content-Type-Options"] = "nosniff"
    if not pdf:
        response.headers["Content-Security-Policy"] = SANDBOX_CSP
    return response


# ---------------------------------------------------------------------------
# Toutes les pages
# ---------------------------------------------------------------------------
PAGE_CSP = "img-src 'self' data: blob:"


def init_app(app) -> None:
    @app.after_request
    def _images_locales(response):
        """Hangar n'affiche aucune image venue d'ailleurs (aucun CDN) : même si un texte en glissait une,
        le navigateur ne la chargerait pas. La politique complète des pages (hangar/web.py, avec
        « img-src 'self' data: blob: ») passe avant ; celle-ci ne sert que si elle est désactivée
        (réglage CSP « non »). Les fichiers servis gardent leur politique plus stricte."""
        h = response.headers
        if "Content-Security-Policy" not in h and "Content-Security-Policy-Report-Only" not in h:
            h["Content-Security-Policy"] = PAGE_CSP
        return response

"""Rendu des énoncés de QCM (Markdown façon Obsidian, formules LaTeX, images du coffre).

Comme dans Revision, les questions sont rendues par le moteur du wiki (``wiki.render.Renderer``) : mêmes
formules, images ``![[schema.png|300]]``, liens ``[[Page]]``, callouts et même nettoyage du HTML (médias
locaux seulement, voir ``hangar.nettoyage_html``). Il n'y a plus de moteur de secours : si le rendu
échoue, l'erreur est notée (page ``/erreurs/``) et le texte est affiché tel quel, sans mise en forme.
"""
from __future__ import annotations

import html
from pathlib import PurePosixPath

from flask import current_app

from ..wiki.render import Renderer
from ..wiki.render import sanitize as nettoyer  # noqa: F401 — même nettoyage que le wiki (tests, IA)
from ..wiki.vault import Note, get_vault
from . import coffre

_NOTE = PurePosixPath("_qcm_.md")


def _rendu_wiki(text: str, block: bool) -> str:
    renderer = Renderer(get_vault(), Note(_NOTE, 0, {}, text))
    return renderer.convert(text) if block else renderer.convert_inline(text)


def _texte_brut(text: str, block: bool) -> str:
    """Texte échappé, retours à la ligne gardés : affiché si le moteur du wiki échoue."""
    out = html.escape(text).replace("\n", "<br>\n")
    return f"<p>{out}</p>" if block else out


_CACHE: dict = {"cle": None, "racine": None, "html": {}}
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
    # Un nouveau parcours du coffre donne une nouvelle liste, égale à l'ancienne si rien n'a changé :
    # comparer le contenu (et non l'objet), sinon le cache serait perdu à chaque parcours.
    if (cle is not _CACHE["cle"] and cle != _CACHE["cle"]) or _CACHE["racine"] != racine:
        _CACHE.update(cle=cle, racine=racine, html={})
    else:
        _CACHE["cle"] = cle  # la liste la plus récente : la prochaine comparaison s'arrête à « is »
    deja = _CACHE["html"].get((text, block))
    if deja is not None:
        return deja
    try:
        out = _rendu_wiki(text, block)
    except Exception as exc:  # noqa: BLE001 — une question ne doit pas empêcher d'afficher le set
        current_app.logger.warning("QCM : rendu impossible d'un énoncé, affiché sans mise en forme", exc_info=True)
        from ..wiki import erreurs
        erreurs.noter(exc, contexte="Rendu d'une question de QCM")
        return _texte_brut(text, block)
    if len(_CACHE["html"]) < _CACHE_MAX:
        _CACHE["html"][(text, block)] = out
    return out

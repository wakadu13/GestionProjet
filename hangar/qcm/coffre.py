"""Accès au coffre (dossier du wiki) pour les QCM : une fine couche au-dessus de ``hangar.wiki.vault``.

Les QCM n'ont ni parcours, ni cache, ni règles propres : noms valides (``clean_key``), dossiers
ignorés, parcours du coffre (``scan``, ``signature``) et son délai (``vault.intervalle_verif``),
``invalider()``, liste des médias (celle du moteur du wiki) et résolution d'un lien façon Obsidian
(``get_vault().resolve``) sont ceux du wiki : une correction faite d'un côté vaut pour l'autre.
"""
from __future__ import annotations

from pathlib import Path, PurePosixPath

from flask import current_app

from ..wiki.render import AUDIO_EXTS, IMAGE_EXTS, VIDEO_EXTS
from ..wiki.vault import (IGNORED_DIRS, INVALID_NAME_RE, Attachment, clean_key, get_vault, intervalle_verif,  # noqa: F401
                          invalider, scan, signature_coffre)

MEDIA_EXTS = IMAGE_EXTS | AUDIO_EXTS | VIDEO_EXTS | {".pdf"}
DOSSIER_QCM = "QCM"


def racine() -> Path:
    return Path(current_app.config["WIKI_DIR"])


def racine_ecriture() -> Path:
    """Comme ``racine()``, pour écrire : lève ``CoffreIntrouvable`` (un ValueError) si le coffre choisi est
    introuvable, au lieu de le recréer vide."""
    from ..wiki.coffre import dossier_pour_ecrire
    return dossier_pour_ecrire(current_app.config)


def signature(root) -> tuple:
    """Fichiers d'un dossier (voir ``scan``) : le parcours partagé avec le wiki (``signature_coffre``), refait
    au plus toutes les ``intervalle_verif()`` secondes ou après un ``invalider()``."""
    return signature_coffre(root, suivre_ecritures=False)


# ---------------------------------------------------------------------------
# Pièces jointes (images des énoncés et des cartes à image masquée)
# ---------------------------------------------------------------------------
def _medias(vault, suffixes=MEDIA_EXTS):
    return [a for a in vault.attachments.values() if a.suffix in suffixes]


def resoudre(cible: str) -> PurePosixPath | None:
    """Fichier média du coffre désigné comme dans un lien Obsidian (même résolution que le wiki) : chemin
    depuis la racine, puis nom de fichier n'importe où (le chemin le plus court)."""
    cible = str(cible or "").strip()
    if not cible.replace("\\", "/").strip("/"):
        return None
    trouve = get_vault().resolve(cible)
    if isinstance(trouve, Attachment) and trouve.suffix in MEDIA_EXTS:
        return trouve.path
    return None


def images(suffixes=IMAGE_EXTS) -> list[PurePosixPath]:
    """Images du coffre (pour l'éditeur d'images masquées)."""
    return sorted((a.path for a in _medias(get_vault(), suffixes)), key=lambda p: str(p).lower())


def lien_court(p: PurePosixPath) -> str:
    """Texte d'un lien vers une pièce jointe : le nom seul s'il est unique, sinon le chemin."""
    vault = get_vault()
    piece = vault.attachments.get(str(p).lower())
    return vault.link_for(piece) if piece is not None else str(p)


def chemin_sur(rel: str) -> Path | None:
    """Chemin absolu d'un fichier du coffre, ou None s'il sort du coffre."""
    root = racine().resolve()
    try:
        cible = (root / rel).resolve()
    except (OSError, ValueError):
        return None
    if not cible.is_relative_to(root) or any(part.startswith(".") for part in PurePosixPath(rel).parts):
        return None
    return cible


def dossier_pieces_jointes(note_key: str) -> str:
    """Dossier où ranger une image envoyée, réglé comme dans Obsidian (``attachmentFolderPath``
    de ``.obsidian/app.json``) : la règle du wiki (``Vault.attachment_folder``)."""
    return get_vault().attachment_folder(note_key)

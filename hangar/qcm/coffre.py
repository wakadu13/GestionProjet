"""Accès au coffre (dossier du wiki) pour les QCM : liste des fichiers, noms valides, images.

Ce module recopie le strict nécessaire de `wiki/vault.py` de Revision (`INVALID_NAME_RE`,
`clean_key`, résolution d'une image par son chemin ou son nom, comme Obsidian). Le parcours du
coffre (avec un délai entre deux parcours) et `invalider()` sont ceux du wiki
(`hangar.wiki.vault`) : un seul parcours sert les pages et les QCM.
"""
from __future__ import annotations

import os
import re
import threading
from pathlib import Path, PurePosixPath

from flask import current_app

IGNORED_DIRS = {".obsidian", ".trash", ".git", "__pycache__"}
INVALID_NAME_RE = re.compile(r'[\\:*?"<>|#^\[\]\x00-\x1f]')
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".avif"}
MEDIA_EXTS = IMAGE_EXTS | {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".mp4", ".webm", ".ogv", ".mov", ".pdf"}
INTERVALLE = 2.0  # secondes entre deux parcours du coffre (une écriture du module force le suivant)
DOSSIER_QCM = "QCM"


def racine() -> Path:
    return Path(current_app.config["WIKI_DIR"])


def racine_ecriture() -> Path:
    """Comme ``racine()``, pour écrire : lève ``CoffreIntrouvable`` (un ValueError) si le coffre choisi est
    introuvable, au lieu de le recréer vide."""
    from ..wiki.coffre import dossier_pour_ecrire
    return dossier_pour_ecrire(current_app.config)


def clean_key(raw):
    """Valide un chemin saisi (``Dossier/Nom``), ou renvoie None."""
    parts = [p.strip() for p in str(raw).replace("\\", "/").strip().removesuffix(".md").split("/")]
    parts = [p for p in parts if p]
    if not parts or any(p.startswith(".") or INVALID_NAME_RE.search(p) for p in parts):
        return None
    return "/".join(parts)


def scan(root) -> tuple:
    """(chemin relatif, date de modification, taille) des fichiers d'un dossier, dossiers cachés exclus.

    Même parcours que le wiki (``hangar.wiki.vault.scan``, avec ``os.scandir``)."""
    from hangar.wiki.vault import scan as scan_wiki  # import tardif : évite un cycle à l'import
    return scan_wiki(root)


def invalider() -> None:
    """Le prochain accès au coffre le relira (un fichier vient d'être écrit)."""
    from hangar.wiki import vault as wiki_vault
    wiki_vault.invalider()


def signature(root) -> tuple:
    """Fichiers d'un dossier (voir scan), parcouru au plus toutes les QCM_VERIF_INTERVALLE secondes.

    Le parcours est celui du wiki (``signature_coffre``) : après quelques secondes d'inactivité, une
    page qui lit le wiki et les QCM ne parcourt plus le coffre qu'une fois."""
    from hangar.wiki.vault import signature_coffre
    intervalle = float(current_app.config.get("QCM_VERIF_INTERVALLE", INTERVALLE))
    return signature_coffre(root, intervalle=intervalle, suivre_ecritures=False)


# ---------------------------------------------------------------------------
# Pièces jointes (images des énoncés et des cartes à image masquée)
# ---------------------------------------------------------------------------
_index_lock = threading.Lock()
_index: dict = {"cle": None, "par_chemin": {}, "par_nom": {}}


def _index_fichiers():
    sig = signature(racine())
    cle = (racine(), sig)
    with _index_lock:
        if _index["cle"] != cle:
            par_chemin, par_nom = {}, {}
            for rel, _m, _s in sig:
                p = PurePosixPath(rel)
                if p.suffix.lower() not in MEDIA_EXTS:
                    continue
                par_chemin[rel.lower()] = p
                par_nom.setdefault(p.name.lower(), []).append(p)
            _index.update(cle=cle, par_chemin=par_chemin, par_nom=par_nom)
        return _index["par_chemin"], _index["par_nom"]


def resoudre(cible: str, dossier: str = "") -> PurePosixPath | None:
    """Fichier média du coffre désigné comme dans un lien Obsidian : chemin relatif au dossier,
    chemin depuis la racine, puis nom de fichier n'importe où (le chemin le plus court)."""
    cible = str(cible or "").strip().replace("\\", "/").lstrip("/")
    if not cible:
        return None
    par_chemin, par_nom = _index_fichiers()
    low = cible.lower()
    essais = [low]
    if dossier:
        rel = os.path.normpath(os.path.join(dossier.lower(), low)).replace("\\", "/")
        if not rel.startswith(".."):
            essais.insert(0, rel)
    for e in essais:
        if e in par_chemin:
            return par_chemin[e]
    nom = low.rsplit("/", 1)[-1]
    candidats = [p for p in par_nom.get(nom, []) if "/" not in low or str(p).lower().endswith(low)]
    if not candidats:
        return None
    return min(candidats, key=lambda p: (len(p.parts), str(p).lower()))


def images(suffixes=IMAGE_EXTS) -> list[PurePosixPath]:
    """Images du coffre (pour l'éditeur d'images masquées)."""
    par_chemin, _ = _index_fichiers()
    return sorted((p for p in par_chemin.values() if p.suffix.lower() in suffixes), key=lambda p: str(p).lower())


def lien_court(p: PurePosixPath) -> str:
    """Texte d'un lien vers une pièce jointe : le nom seul s'il est unique, sinon le chemin."""
    _, par_nom = _index_fichiers()
    return p.name if len(par_nom.get(p.name.lower(), [])) == 1 else str(p)


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
    de ``.obsidian/app.json``) : racine du coffre par défaut, ``./`` pour le dossier de la note."""
    import json
    try:
        config = json.loads((racine() / ".obsidian" / "app.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        config = {}
    reglage = str((config.get("attachmentFolderPath") if isinstance(config, dict) else None) or "/").strip()
    dossier_note = note_key.rpartition("/")[0]
    dossier = f"{dossier_note}/{reglage[2:]}" if reglage == "." or reglage.startswith("./") else reglage
    return clean_key(dossier) or ""

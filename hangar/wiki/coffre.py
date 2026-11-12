"""Dossier du coffre du wiki : choisi, mémorisé, vérifié.

Ordre de priorité :

1. la variable d'environnement ``HANGAR_VAULT`` (pour une session, un script) ;
2. le dossier mémorisé dans ``<données>/coffre.txt`` (``hangar wiki-coffre <dossier>``, ou Réglages › Espace) :
   il vaut pour tous les lancements, ``lancer.bat`` compris ;
3. sinon ``<données>/vault``.

Un coffre choisi (cas 1 et 2) n'est jamais créé ni rempli de pages de bienvenue : si son dossier n'existe pas
(faute de frappe, disque ou clé débranché, dossier synchronisé pas encore là), le wiki le dit au lieu d'ouvrir
un coffre vide, où les notes créées seraient invisibles pour Obsidian.
"""
from __future__ import annotations

import os
from pathlib import Path

FICHIER = "coffre.txt"
VARIABLE = "variable"   # HANGAR_VAULT
REGLAGE = "reglage"     # data/coffre.txt
DEFAUT = "defaut"       # data/vault

ORIGINES = {
    VARIABLE: "réglé par la variable HANGAR_VAULT",
    REGLAGE: "choisi avec « hangar wiki-coffre » ou dans Réglages › Espace",
    DEFAUT: "dossier par défaut, dans les données",
}


def _normaliser(chemin: str) -> Path:
    return Path(os.path.expandvars(chemin.strip().strip('"'))).expanduser().resolve()


def memorise(data_dir) -> str:
    """Chemin écrit dans data/coffre.txt (vide s'il n'y en a pas)."""
    try:
        return (Path(data_dir) / FICHIER).read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def choisir(data_dir) -> tuple[Path, str]:
    """(dossier du coffre, origine : VARIABLE, REGLAGE ou DEFAUT)."""
    variable = os.environ.get("HANGAR_VAULT", "").strip()
    if variable:
        return _normaliser(variable), VARIABLE
    garde = memorise(data_dir)
    if garde:
        return _normaliser(garde), REGLAGE
    return (Path(data_dir) / "vault").resolve(), DEFAUT


def memoriser(data_dir, chemin: str | os.PathLike | None) -> Path | None:
    """Mémorise le coffre (``None`` ou vide : revenir au dossier par défaut). Le dossier doit exister.
    Renvoie le dossier retenu (None pour le défaut). Lève FileNotFoundError / NotADirectoryError."""
    fichier = Path(data_dir) / FICHIER
    if not chemin or not str(chemin).strip():
        fichier.unlink(missing_ok=True)
        return None
    dossier = _normaliser(str(chemin))
    if not dossier.exists():
        raise FileNotFoundError(f"Dossier introuvable : {dossier}")
    if not dossier.is_dir():
        raise NotADirectoryError(f"Ce n'est pas un dossier : {dossier}")
    if dossier == Path(data_dir).resolve() or Path(data_dir).resolve().is_relative_to(dossier):
        raise ValueError("Le coffre ne peut pas être le dossier des données ni le contenir.")
    fichier.write_text(str(dossier) + "\n", encoding="utf-8")
    return dossier


def introuvable(config) -> bool:
    """Le coffre choisi (HANGAR_VAULT ou mémorisé) n'existe pas ou plus."""
    return config.get("WIKI_ORIGINE", DEFAUT) != DEFAUT and not Path(config["WIKI_DIR"]).is_dir()


def appliquer(app, dossier: Path, origine: str) -> None:
    """Change le coffre de l'application en marche (le wiki et les QCM le relisent à la prochaine page)."""
    from .vault import invalider
    app.config["WIKI_DIR"], app.config["WIKI_ORIGINE"] = str(dossier), origine
    if origine == DEFAUT:
        dossier.mkdir(parents=True, exist_ok=True)
    invalider()

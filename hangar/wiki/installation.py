"""Mise en place du coffre : pages de bienvenue au premier lancement, import d'un coffre
existant (dossier local ou dépôt git, par ex. github.com/wakadu13/Revision).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .vault import IGNORED_DIRS, invalider, lire_note

DEFAUT_DIR = Path(__file__).resolve().parent / "defaut"
IGNORES = IGNORED_DIRS | {".obsidian", ".trash", ".git", ".github", "__pycache__", "node_modules"}


def _fichiers(racine: Path):
    """Fichiers d'un dossier (chemins relatifs POSIX), hors dossiers et fichiers cachés."""
    for dirpath, dirnames, filenames in os.walk(racine):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORES and not d.startswith("."))
        for nom in sorted(filenames):
            if nom.startswith("."):
                continue
            yield Path(dirpath, nom).relative_to(racine).as_posix()


def coffre_vide(racine: Path) -> bool:
    return not any(rel.lower().endswith(".md") for rel in _fichiers(racine)) if racine.is_dir() else True


def amorcer(racine) -> list[str]:
    """Coffre sans aucune note : y écrit la page d'accueil et le guide de la syntaxe.
    Renvoie les fichiers créés."""
    racine = Path(racine)
    racine.mkdir(parents=True, exist_ok=True)
    if not coffre_vide(racine):
        return []
    crees = []
    for rel in _fichiers(DEFAUT_DIR):
        cible = racine / rel
        if cible.exists():
            continue
        cible.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(DEFAUT_DIR / rel, cible)
        crees.append(rel)
    invalider()
    return crees


def est_defaut(rel: str, chemin: Path) -> bool:
    """Le fichier est une page de bienvenue restée telle quelle (remplaçable par un import)."""
    modele = DEFAUT_DIR / rel
    try:
        return modele.is_file() and chemin.is_file() and modele.read_bytes() == chemin.read_bytes()
    except OSError:
        return False


@dataclass
class BilanImport:
    copies: list[str] = field(default_factory=list)
    remplaces: list[str] = field(default_factory=list)
    ignores: list[str] = field(default_factory=list)  # existaient déjà (sans --remplacer)
    # Liens symboliques vers un fichier hors du coffre importé ou caché (.git/, .env…), ou cassés : jamais copiés.
    exclus: list[str] = field(default_factory=list)
    echecs: list[str] = field(default_factory=list)  # lecture ou écriture refusée par le système
    source: str = ""
    images_externes: int = 0  # images hébergées ailleurs dans les pages importées
    pages_images_externes: int = 0

    @property
    def notes(self) -> int:
        return sum(1 for r in self.copies + self.remplaces if r.lower().endswith(".md"))


def est_url_git(source: str) -> bool:
    s = source.strip()
    return s.startswith(("http://", "https://", "git@", "ssh://", "git://")) or s.endswith(".git")


def cloner(url: str, destination: Path) -> Path:
    """Clone superficiel d'un dépôt git ; renvoie le dossier du clone."""
    if url.strip().startswith("-"):
        raise ValueError(f"Adresse de dépôt invalide : {url}")
    if shutil.which("git") is None:
        raise RuntimeError("git n'est pas installé : clone le dépôt toi-même puis donne son dossier.")
    # « -- » : l'adresse ne peut jamais être prise pour une option de git.
    r = subprocess.run(["git", "clone", "--depth", "1", "--quiet", "--", url, str(destination)],
                       capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"Échec du clone de {url} : {(r.stderr or r.stdout).strip()[:400]}")
    return destination


def dossier_du_coffre(racine: Path) -> Path:
    """Un dépôt Revision range son coffre dans vault/ : on prend ce dossier s'il existe
    (sauf si le dossier donné est lui-même un coffre Obsidian, avec son .obsidian/)."""
    if (racine / ".obsidian").is_dir():
        return racine
    for nom in ("vault", "Vault", "coffre"):
        if (racine / nom).is_dir():
            return racine / nom
    return racine


def importer_coffre(source: str, destination, remplacer: bool = False, historique_auteur: str = "import") -> BilanImport:
    """Copie un coffre (dossier local ou URL git) dans ``destination`` en ignorant
    ``.obsidian``, ``.trash`` et ``.git``. Un fichier déjà présent n'est remplacé qu'avec
    ``remplacer`` (le texte d'une note remplacée part dans l'historique), sauf les pages de
    bienvenue restées telles quelles, toujours remplacées."""
    from . import historique

    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    bilan = BilanImport(source=source)
    with tempfile.TemporaryDirectory(prefix="hangar-wiki-") as tmp:
        if est_url_git(source):
            depot = cloner(source, Path(tmp) / "depot")
        else:
            depot = Path(source).expanduser().resolve()
            if not depot.is_dir():
                raise FileNotFoundError(f"Dossier introuvable : {source}")
        racine = dossier_du_coffre(depot)
        if racine.resolve() == destination.resolve():
            raise ValueError("La source est déjà le coffre du wiki.")
        limite = depot.resolve()
        for rel in _fichiers(racine):
            source_fichier = racine / rel
            # Lien symbolique (un fichier, ou le dossier vault/ lui-même) : copié seulement s'il mène à
            # un fichier du dossier importé, hors des dossiers et fichiers cachés que l'import ignore. Un lien
            # vers une clé, un mot de passe, .git/config (l'adresse tapée, jeton compris) ou .env deviendrait
            # sinon une page lisible par tous ; un lien cassé arrêtait l'import à mi-chemin.
            reel = source_fichier.resolve()
            if (not reel.is_relative_to(limite) or not reel.is_file()
                    or any(p in IGNORES or p.startswith(".") for p in reel.relative_to(limite).parts)):
                bilan.exclus.append(rel)
                continue
            cible = destination / rel
            try:
                if cible.exists():
                    if cible.is_dir():
                        bilan.ignores.append(rel)
                        continue
                    if not remplacer and not est_defaut(rel, cible):
                        bilan.ignores.append(rel)
                        continue
                    if rel.lower().endswith(".md") and not est_defaut(rel, cible):
                        ancien, _ = lire_note(cible)
                        nouveau, _ = lire_note(source_fichier)
                        if ancien != nouveau:
                            historique.sauvegarder(rel, ancien, historique_auteur)
                    shutil.copy2(source_fichier, cible)
                    bilan.remplaces.append(rel)
                    continue
                cible.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_fichier, cible)
                bilan.copies.append(rel)
            except OSError:
                bilan.echecs.append(rel)
    _retirer_pieces_jointes_orphelines(destination)
    _compter_images_externes(destination, bilan)
    invalider()
    return bilan


def _compter_images_externes(racine: Path, bilan: BilanImport) -> None:
    from .images_externes import trouver

    for rel in bilan.copies + bilan.remplaces:
        if not rel.lower().endswith(".md"):
            continue
        try:
            n = len(trouver(lire_note(racine / rel)[0]))
        except OSError:
            continue
        bilan.images_externes += n
        bilan.pages_images_externes += bool(n)


def _retirer_pieces_jointes_orphelines(racine: Path) -> None:
    """Pièces jointes de bienvenue (logo du guide) restées telles quelles et citées par aucune
    page après l'import (le guide a été remplacé) : retirées, pour ne pas encombrer le coffre."""
    pieces = [rel for rel in _fichiers(DEFAUT_DIR) if not rel.lower().endswith(".md")]
    pieces = [rel for rel in pieces if est_defaut(rel, racine / rel)]
    if not pieces:
        return
    textes = []
    for rel in _fichiers(racine):
        if rel.lower().endswith(".md"):
            try:
                textes.append(lire_note(racine / rel)[0])
            except OSError:
                return  # une page illisible pourrait citer la pièce jointe : on garde tout
    for rel in pieces:
        nom = rel.rsplit("/", 1)[-1]
        if not any(nom in texte for texte in textes):
            (racine / rel).unlink()
            try:
                (racine / rel).parent.rmdir()  # dossier devenu vide
            except OSError:
                pass

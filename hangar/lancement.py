"""Version lancée, journal sur disque et détection d'une mise à jour faite pendant que Hangar tourne.

- ``code_sur_le_disque()`` : commit git du dossier de Hangar (et sa date), lu avec git s'il est installé, sinon
  directement dans ``.git`` (sans date) ;
- ``noter_lancement()`` : au démarrage du serveur, écrit ``data/serveur.json`` (commit, version, heure, port) ;
- ``avertissement_version()`` : au lancement suivant, si Hangar tourne déjà avec un code plus ancien que celui
  du disque (``git pull`` fait depuis), le message qui dit de le relancer ;
- ``ouvrir_journal()`` : erreurs et avertissements écrits dans ``data/journal/hangar.log`` : lancé sans fenêtre
  (démarrage avec la session Windows), Hangar n'a pas de console où les lire.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from datetime import datetime
from functools import lru_cache
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import BASE_DIR, __version__

FICHIER = "serveur.json"


def _git_dir(racine: Path) -> Path | None:
    point_git = racine / ".git"
    if point_git.is_dir():
        return point_git
    if point_git.is_file():  # copie de travail secondaire : « gitdir: … »
        contenu = point_git.read_text(encoding="utf-8", errors="replace").strip()
        if contenu.startswith("gitdir:"):
            return (racine / contenu.removeprefix("gitdir:").strip()).resolve()
    return None


def _lire_ref(git_dir: Path, ref: str) -> str:
    communs = [git_dir]
    commondir = git_dir / "commondir"
    if commondir.is_file():
        communs.append((git_dir / commondir.read_text(encoding="utf-8").strip()).resolve())
    for d in communs:
        f = d / ref
        if f.is_file():
            return f.read_text(encoding="utf-8").strip()
        packed = d / "packed-refs"
        if packed.is_file():
            for ligne in packed.read_text(encoding="utf-8", errors="replace").splitlines():
                if ligne.endswith(" " + ref):
                    return ligne.split(" ", 1)[0]
    return ""


def code_sur_le_disque(racine: Path | None = None) -> dict:
    """{"commit": sha complet ou "", "date": date ISO du commit ou ""} du dossier de Hangar."""
    racine = Path(racine or BASE_DIR)
    if shutil.which("git"):
        try:
            r = subprocess.run(["git", "-C", str(racine), "log", "-1", "--format=%H%n%cI"], capture_output=True,
                               text=True, timeout=5)
            if r.returncode == 0 and r.stdout.strip():
                commit, _, date = r.stdout.strip().partition("\n")
                return {"commit": commit.strip(), "date": date.strip()}
        except (OSError, subprocess.SubprocessError):
            pass
    git_dir = _git_dir(racine)
    if git_dir is None:
        return {"commit": "", "date": ""}
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        commit = _lire_ref(git_dir, head.removeprefix("ref:").strip()) if head.startswith("ref:") else head
    except OSError:
        commit = ""
    return {"commit": commit, "date": ""}


@lru_cache(maxsize=1)
def code_au_demarrage() -> dict:
    """Code lu au premier appel (le serveur l'appelle à son lancement) : la version qui tourne vraiment."""
    return code_sur_le_disque()


def description(code: dict) -> str:
    """« 1.1.0 (a1b2c3d du 12/11/2026) » pour la page À propos et les messages."""
    texte = __version__
    if code.get("commit"):
        texte += f" ({code['commit'][:7]}"
        try:
            texte += f" du {datetime.fromisoformat(code['date']):%d/%m/%Y}" if code.get("date") else ""
        except ValueError:
            pass
        texte += ")"
    return texte


def noter_lancement(data_dir, port: int) -> None:
    code = code_au_demarrage()
    infos = {"version": __version__, "commit": code["commit"], "date_commit": code["date"], "port": port,
             "pid": os.getpid(), "demarre_le": datetime.now().isoformat(timespec="seconds")}
    try:
        (Path(data_dir) / FICHIER).write_text(json.dumps(infos, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        logging.getLogger(__name__).warning("Impossible d'écrire %s", FICHIER, exc_info=True)


def avertissement_version(data_dir) -> str | None:
    """Message si le Hangar qui tourne a été lancé avec un autre code que celui du disque (mise à jour faite)."""
    try:
        lance = json.loads((Path(data_dir) / FICHIER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    disque = code_sur_le_disque()
    if not lance.get("commit") or not disque["commit"] or lance["commit"] == disque["commit"]:
        return None
    quand = lance.get("demarre_le", "")
    try:
        quand = f" le {datetime.fromisoformat(quand):%d/%m à %H:%M}"
    except ValueError:
        quand = ""
    return ("  ⚠ Hangar a été mis à jour depuis son lancement" + quand + ", mais c’est encore l’ancienne version\n"
            "    qui tourne. Pour passer à la nouvelle : ferme la fenêtre de Hangar (ou redémarre l’ordinateur si\n"
            "    Hangar démarre avec ta session), puis relance-le.")


def ouvrir_journal(data_dir) -> Path:
    """Écrit les avertissements et erreurs de Hangar dans data/journal/hangar.log (1 Mo, 3 anciens gardés)."""
    dossier = Path(data_dir) / "journal"
    dossier.mkdir(parents=True, exist_ok=True)
    chemin = dossier / "hangar.log"
    racine = logging.getLogger()
    if not any(getattr(h, "_hangar", False) for h in racine.handlers):
        h = RotatingFileHandler(chemin, maxBytes=1_000_000, backupCount=3, encoding="utf-8", delay=True)
        h._hangar = True
        h.setLevel(logging.WARNING)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s : %(message)s"))
        racine.addHandler(h)
        if racine.level > logging.WARNING:
            racine.setLevel(logging.WARNING)
    return chemin

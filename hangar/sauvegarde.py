"""Sauvegardes de Hangar : copie de la base seule (.db) et sauvegarde complète (.zip).

La sauvegarde complète contient tout ce qu'il faut pour repartir sur une autre machine :

- ``hangar.db`` : la base, copiée avec l'API de sauvegarde de SQLite (cohérente même appli en marche) ;
- ``vault/`` : le coffre du wiki (cours, PDF, images, sets de QCM), même quand ``HANGAR_VAULT`` le
  place hors du dossier des données ;
- ``uploads/``, ``wiki_versions/``, ``qcm_perso/``, ``copies/``, ``agenda/`` et ``vapid_prive.pem``
  (la clé des notifications : sans elle, chaque appareil doit se réabonner).

Ne sont pas copiés : ``backups/`` (les sauvegardes elles-mêmes), ``outbox/`` (les e-mails déjà écrits
en .eml) et ``secret.key`` (la perdre oblige seulement à se reconnecter). La restauration d'une
sauvegarde complète se fait à la main, appli arrêtée : voir ``LISEZMOI.txt`` dans l'archive.

Une copie « sans secrets » vide le mot de passe SMTP et les clés d'API des fournisseurs d'IA
(``SECRETS``), puis compacte la base pour que les anciennes valeurs ne restent pas dans les pages
libérées du fichier.
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from flask import current_app

# Dossiers du dossier des données copiés dans la sauvegarde complète (le coffre est ajouté à part).
DOSSIERS = ("uploads", "wiki_versions", "qcm_perso", "copies", "agenda")
FICHIERS = ("vapid_prive.pem",)
# Jamais copiés, où qu'ils soient (historique git d'un coffre, caches).
IGNORES = {".git", "__pycache__", "node_modules"}
# Formats déjà compressés : rangés tels quels (compresser ne gagne rien et prend du temps).
DEJA_COMPRESSES = {".zip", ".gz", ".7z", ".rar", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".pdf",
                   ".mp3", ".mp4", ".m4a", ".ogg", ".webm", ".docx", ".xlsx", ".pptx", ".odt", ".apkg"}
# Colonnes vidées dans une copie « sans secrets » : (table, colonne).
SECRETS = (("mail_config", "smtp_password"), ("ai_provider", "api_key"))

MOTIF_BASE = re.compile(r"^hangar_(\d{8}_\d{6})(?:_(\d+))?\.db$")
MOTIF_COMPLET = re.compile(r"^hangar_complet_(\d{8}_\d{6})(?:_(\d+))?\.zip$")


@dataclass
class Bilan:
    """Résultat d'une sauvegarde complète."""
    chemin: str = ""
    fichiers: int = 0
    octets: int = 0          # taille des fichiers avant compression
    non_copies: list[str] = field(default_factory=list)  # fichiers illisibles (verrouillés…)
    supprimees: list[str] = field(default_factory=list)  # anciennes archives retirées (--garder)


def _horodatage() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _chemin_libre(dossier: str | os.PathLike, prefixe: str, extension: str) -> Path:
    """``<prefixe>_<date>_<heure>.<ext>`` ; un suffixe _2, _3… si le nom existe déjà (même seconde)."""
    dossier = Path(dossier)
    base = f"{prefixe}_{_horodatage()}"
    chemin, n = dossier / f"{base}.{extension}", 1
    while chemin.exists() or chemin.with_name(chemin.name + ".partiel").exists():
        n += 1
        chemin = dossier / f"{base}_{n}.{extension}"
    return chemin


def chemin_base() -> Path:
    return Path(current_app.config["DATA_DIR"]) / "hangar.db"


def copier_base(destination: str | os.PathLike, sans_secrets: bool = False) -> None:
    """Copie cohérente de la base (API de sauvegarde SQLite) dans ``destination``."""
    src = sqlite3.connect(chemin_base())
    dst = sqlite3.connect(destination)
    try:
        with dst:
            src.backup(dst)
    finally:
        src.close()
        dst.close()
    if sans_secrets:
        retirer_secrets(destination)


def retirer_secrets(chemin: str | os.PathLike) -> None:
    """Vide le mot de passe SMTP et les clés d'API dans une COPIE de la base, puis la compacte."""
    con = sqlite3.connect(chemin)
    try:
        con.execute("PRAGMA journal_mode=DELETE")  # un seul fichier, sans -wal à côté
        for table, colonne in SECRETS:
            colonnes = {r[1] for r in con.execute(f'PRAGMA table_info("{table}")')}
            if colonne in colonnes:
                con.execute(f'UPDATE "{table}" SET "{colonne}" = \'\'')
        con.commit()
        con.execute("VACUUM")  # réécrit le fichier : les anciennes valeurs ne traînent pas dans les pages libres
    finally:
        con.close()


def make_backup(dossier: str | os.PathLike | None = None) -> str:
    """Copie de la base seule dans ``dossier`` (défaut : data/backups/) ; renvoie son chemin.

    Elle se restaure en un clic depuis la page Sauvegardes, mais ne contient ni le wiki, ni les sets de
    QCM, ni les pièces jointes : voir ``make_full_backup``."""
    dossier = Path(dossier or current_app.config["BACKUP_DIR"])
    dossier.mkdir(parents=True, exist_ok=True)
    dest = _chemin_libre(dossier, "hangar", "db")
    copier_base(dest)
    return str(dest)


def _sources() -> list[tuple[Path, str]]:
    """(dossier ou fichier sur le disque, nom dans l'archive)."""
    data = Path(current_app.config["DATA_DIR"])
    out = [(Path(current_app.config["WIKI_DIR"]), "vault")]
    out += [(data / nom, nom) for nom in DOSSIERS]
    out += [(data / nom, nom) for nom in FICHIERS]
    return out


def _exclus() -> set[Path]:
    """Dossiers jamais parcourus, même si le coffre les contient (HANGAR_VAULT sur un dossier parent) :
    les sauvegardes, la boîte d'envoi locale et, dans le coffre, le dossier des données (copié à part)."""
    data = Path(current_app.config["DATA_DIR"]).resolve()
    return {Path(current_app.config["BACKUP_DIR"]).resolve(), data / "outbox", data}


def _lisezmoi(sans_secrets: bool) -> str:
    data = Path(current_app.config["DATA_DIR"])
    coffre = Path(current_app.config["WIKI_DIR"])
    hors_data = coffre.resolve() != (data / "vault").resolve()
    from . import __version__
    lignes = [
        "Sauvegarde complète de Hangar",
        "=============================",
        "",
        f"Créée le {datetime.now():%d/%m/%Y à %H:%M} par Hangar {__version__}.",
        f"Dossier des données d'origine : {data}",
        f"Coffre du wiki d'origine : {coffre}" + (" (réglé par HANGAR_VAULT)" if hors_data else ""),
        "",
        "Contenu",
        "-------",
        "- hangar.db : la base (projets, tickets, agenda, progression des QCM, conversations avec l'IA,",
        "  comptes, réglages).",
        "- vault/ : le coffre du wiki (cours, PDF, images, sets de QCM).",
        "- wiki_versions/ : l'historique des pages du wiki.",
        "- qcm_perso/ : les sets personnels de chaque compte.",
        "- copies/ : les photos des copies corrigées par l'IA.",
        "- uploads/ : les pièces jointes des tickets et des pages.",
        "- agenda/ : les emplois du temps importés.",
        "- vapid_prive.pem : la clé des notifications.",
        "",
        "Pas dedans : les anciennes sauvegardes (backups/), les e-mails de la boîte d'envoi locale",
        "(outbox/) et secret.key (sans elle, il faut seulement se reconnecter).",
        "",
        ("Copie SANS SECRETS : le mot de passe SMTP et les clés d'API des fournisseurs d'IA ont été retirés.\n"
         "Après restauration, ressaisis-les dans Réglages › E-mails automatiques et IA › Fournisseurs."
         if sans_secrets else
         "ATTENTION : cette copie contient le mot de passe SMTP et les clés d'API des fournisseurs d'IA,\n"
         "lisibles par qui ouvre hangar.db. Range-la en lieu sûr et ne la partage pas."),
        "Les mots de passe des comptes ne sont jamais lisibles (seule leur empreinte est enregistrée).",
        "",
        "Restaurer",
        "---------",
        "1. Arrête Hangar (Ctrl+C dans son terminal).",
        f"2. Décompresse cette archive dans le dossier des données ({data}),",
        "   en remplaçant les fichiers existants. Supprime d'abord hangar.db-wal et hangar.db-shm s'ils existent.",
    ]
    if hors_data:
        lignes.append(f"3. Copie le contenu du dossier vault/ de l'archive dans ton coffre ({coffre}).")
        lignes.append("4. Relance Hangar.")
    else:
        lignes.append("3. Relance Hangar.")
    return "\n".join(lignes) + "\n"


def ecrire_archive(fichier, sans_secrets: bool = False, ignorer: set[Path] | None = None) -> Bilan:
    """Écrit la sauvegarde complète dans ``fichier`` (chemin ou objet fichier ouvert en écriture binaire).
    ``ignorer`` : fichiers à ne pas copier (l'archive en cours d'écriture, si elle est dans le coffre)."""
    bilan = Bilan()
    exclus = _exclus()
    ignorer = {p.resolve() for p in (ignorer or set())}
    with tempfile.TemporaryDirectory(prefix="hangar-sauvegarde-") as tmp:
        copie = Path(tmp) / "hangar.db"
        copier_base(copie, sans_secrets=sans_secrets)
        with zipfile.ZipFile(fichier, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6,
                             strict_timestamps=False) as zf:
            zf.writestr("LISEZMOI.txt", _lisezmoi(sans_secrets))
            zf.write(copie, "hangar.db")
            bilan.fichiers, bilan.octets = 1, copie.stat().st_size
            for source, nom in _sources():
                if source.is_file():
                    _ajouter(zf, source, nom, bilan)
                    continue
                if not source.is_dir():
                    continue
                for dossier, sous, fichiers in os.walk(source):
                    racine = Path(dossier)
                    sous[:] = sorted(d for d in sous if d not in IGNORES and (racine / d).resolve() not in exclus)
                    for f in sorted(fichiers):
                        chemin = racine / f
                        if chemin.resolve() in ignorer or MOTIF_COMPLET.match(f.removesuffix(".partiel")):
                            continue  # une sauvegarde complète rangée dans le coffre n'est pas recopiée
                        _ajouter(zf, chemin, f"{nom}/{chemin.relative_to(source).as_posix()}", bilan)
    return bilan


def _ajouter(zf: zipfile.ZipFile, chemin: Path, nom: str, bilan: Bilan) -> None:
    mode = zipfile.ZIP_STORED if chemin.suffix.lower() in DEJA_COMPRESSES else zipfile.ZIP_DEFLATED
    try:
        zf.write(chemin, nom, compress_type=mode)
    except OSError:  # fichier verrouillé (Windows), supprimé entre-temps, illisible
        bilan.non_copies.append(nom)
        return
    bilan.fichiers += 1
    try:
        bilan.octets += chemin.stat().st_size
    except OSError:
        pass


def make_full_backup(dossier: str | os.PathLike | None = None, sans_secrets: bool = False,
                     garder: int | None = None) -> Bilan:
    """Sauvegarde complète ``hangar_complet_<date>.zip`` dans ``dossier`` (défaut : data/backups/).

    L'archive est écrite sous un nom provisoire puis renommée : une copie interrompue (clé USB retirée,
    disque plein) ne passe jamais pour une sauvegarde valable. ``garder`` : ne conserver que les N
    dernières sauvegardes complètes de ce dossier."""
    dossier = Path(dossier or current_app.config["BACKUP_DIR"]).expanduser()
    dossier.mkdir(parents=True, exist_ok=True)
    dest = _chemin_libre(dossier, "hangar_complet", "zip")
    partiel = dest.with_name(dest.name + ".partiel")
    try:
        bilan = ecrire_archive(partiel, sans_secrets=sans_secrets, ignorer={partiel})
        os.replace(partiel, dest)
    except BaseException:
        try:
            partiel.unlink()
        except OSError:
            pass
        raise
    bilan.chemin = str(dest)
    if garder:
        bilan.supprimees = garder_dernieres(dossier, garder, MOTIF_COMPLET)
    return bilan


def garder_dernieres(dossier: str | os.PathLike, n: int, motif: re.Pattern = MOTIF_COMPLET) -> list[str]:
    """Supprime les sauvegardes de ``dossier`` au-delà des ``n`` plus récentes. Seuls les fichiers dont le
    nom suit ``motif`` (créés par Hangar) sont concernés : rien d'autre n'est jamais touché."""
    if n < 1:
        return []
    trouves = []
    for nom in os.listdir(dossier):
        m = motif.match(nom)
        if m:
            trouves.append(((m.group(1), int(m.group(2) or 1)), nom))
    trouves.sort(reverse=True)
    supprimees = []
    for _, nom in trouves[n:]:
        try:
            os.remove(os.path.join(dossier, nom))
            supprimees.append(nom)
        except OSError:
            pass
    return supprimees


def liste(dossier: str | os.PathLike | None = None) -> list[dict]:
    """Sauvegardes du dossier (base seule et complètes), la plus récente d'abord."""
    dossier = Path(dossier or current_app.config["BACKUP_DIR"])
    out = []
    for p in dossier.iterdir() if dossier.is_dir() else []:
        if not p.is_file():
            continue
        if p.suffix == ".db":
            kind = "base"
        elif MOTIF_COMPLET.match(p.name):
            kind = "complet"
        else:
            continue
        st = p.stat()
        out.append({"name": p.name, "kind": kind, "octets": st.st_size, "date": datetime.fromtimestamp(st.st_mtime)})
    out.sort(key=lambda f: (f["date"], f["name"]), reverse=True)
    return out


def fichier_temporaire(sans_secrets: bool = False, complet: bool = True):
    """Sauvegarde à télécharger, écrite dans un fichier temporaire anonyme (effacé à sa fermeture, rien
    n'est gardé dans data/backups/). Renvoie le fichier ouvert, positionné au début."""
    out = tempfile.TemporaryFile()
    try:
        if complet:
            ecrire_archive(out, sans_secrets=sans_secrets)
        else:
            with tempfile.TemporaryDirectory(prefix="hangar-sauvegarde-") as tmp:
                copie = Path(tmp) / "hangar.db"
                copier_base(copie, sans_secrets=sans_secrets)
                with open(copie, "rb") as fh:
                    shutil.copyfileobj(fh, out)
        out.seek(0)
        return out
    except BaseException:
        out.close()
        raise

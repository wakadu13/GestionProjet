"""Corbeille et renommage des pages du wiki.

Corbeille : une page supprimée part dans ``.trash/`` du coffre (comme Obsidian), **à son chemin d'origine**
(``.trash/Maths/TD2.md``) pour pouvoir la remettre en place ; une deuxième page du même chemin devient
``TD2 (corbeille 2).md``. Les pages jetées avant cette règle (rangées à plat) retrouvent leur dossier d'après
leur historique quand il n'y a pas d'ambiguïté. Le texte de chaque page jetée est aussi gardé dans l'historique
(``wiki_versions``), qui permet de la recréer même si ``.trash/`` a été vidé.

Renommage / déplacement : le fichier, son historique, les favoris et « récents » des comptes, les questions
posées au tuteur, la page source des sets de QCM et les liens ``[[…]]`` (et ``[texte](page.md)``) des autres
pages suivent la page. Une page modifiée pour ses liens garde son ancien texte dans l'historique.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote

from .. import heure
from ..extensions import db
from . import historique
from .vault import MDLINK_RE, WIKILINK_RE, Note, invalider, lire_note, map_outside_code

journal = logging.getLogger(__name__)
CORBEILLE = ".trash"
_DOUBLON_RE = re.compile(r" \(corbeille \d+\)$")


# ---------------------------------------------------------------------------
# Corbeille
# ---------------------------------------------------------------------------

def jeter(vault, note) -> Path:
    """Déplace la page dans la corbeille du coffre, à son chemin d'origine ; renvoie son nouveau chemin."""
    source = vault.root / note.path
    cible = vault.root / CORBEILLE / note.path
    n = 1
    while cible.exists():
        n += 1
        cible = cible.with_name(f"{note.path.stem} (corbeille {n}){note.path.suffix}")
    cible.parent.mkdir(parents=True, exist_ok=True)
    source.replace(cible)
    invalider()
    return cible


def _origine(rel: str, sans_fichier: dict[str, list[str]]) -> str:
    """Chemin où remettre une page de la corbeille : son chemin dans .trash, sans « (corbeille N) ». Rangée à
    plat par une ancienne version : le dossier d'origine est retrouvé dans l'historique s'il est unique."""
    chemin = PurePosixPath(rel)
    nom = _DOUBLON_RE.sub("", chemin.stem) + chemin.suffix
    if len(chemin.parts) == 1:
        candidats = sans_fichier.get(nom.lower(), [])
        if len(candidats) == 1:
            return candidats[0]
    return (chemin.parent / nom).as_posix()


def contenu(vault) -> dict:
    """Ce que montre la page Corbeille : {"jetees": pages de .trash/, "historique": pages dont il ne reste que
    l'historique (ni dans le coffre, ni dans la corbeille)}."""
    existantes = {n.path.as_posix().lower() for n in vault.notes.values()}
    perdues = historique.pages_sans_fichier(existantes)
    sans_fichier: dict[str, list[str]] = {}
    for p in perdues:
        sans_fichier.setdefault(PurePosixPath(p["page"]).name.lower(), []).append(p["page"])
    racine = vault.root / CORBEILLE
    jetees, origines = [], set()
    if racine.is_dir():
        for f in racine.rglob("*.md"):
            if not f.is_file():
                continue
            rel = f.relative_to(racine).as_posix()
            origine = _origine(rel, sans_fichier)
            try:
                quand = heure.vers_agenda(datetime.fromtimestamp(f.stat().st_mtime, timezone.utc))
            except OSError:
                continue
            origines.add(origine.lower())
            jetees.append({"rel": rel, "origine": origine, "titre": PurePosixPath(origine).stem,
                           "dossier": origine.rsplit("/", 1)[0] if "/" in origine else "",
                           "date": quand, "occupee": origine.lower() in existantes})
    jetees.sort(key=lambda j: j["date"], reverse=True)
    reste = [dict(p, titre=PurePosixPath(p["page"]).stem, cle=p["page"].removesuffix(".md"))
             for p in perdues if p["page"].lower() not in origines]
    return {"jetees": jetees, "historique": reste}


def restaurer(vault, rel: str) -> str:
    """Remet une page de la corbeille à sa place ; renvoie sa clé. Lève FileNotFoundError (déjà restaurée) ou
    FileExistsError (une page porte déjà ce nom)."""
    racine = (vault.root / CORBEILLE).resolve()
    source = (racine / rel).resolve()
    if not source.is_relative_to(racine) or source.suffix.lower() != ".md" or not source.is_file():
        raise FileNotFoundError(rel)
    existantes = {n.path.as_posix().lower() for n in vault.notes.values()}
    perdues = historique.pages_sans_fichier(existantes)
    sans_fichier: dict[str, list[str]] = {}
    for p in perdues:
        sans_fichier.setdefault(PurePosixPath(p["page"]).name.lower(), []).append(p["page"])
    origine = _origine(source.relative_to(racine).as_posix(), sans_fichier)
    cible = vault.root / origine
    if origine.lower() in existantes or cible.exists():
        raise FileExistsError(origine)
    cible.parent.mkdir(parents=True, exist_ok=True)
    source.replace(cible)
    _retirer_dossiers_vides(source.parent, racine)
    invalider()
    return origine.removesuffix(".md")


def _retirer_dossiers_vides(dossier: Path, jusqua: Path) -> None:
    while dossier.resolve() != jusqua and dossier.is_dir() and not any(dossier.iterdir()):
        dossier.rmdir()
        dossier = dossier.parent


# ---------------------------------------------------------------------------
# Renommage
# ---------------------------------------------------------------------------

@dataclass
class BilanRenommage:
    cle: str = ""
    liens: int = 0
    pages: list[str] = field(default_factory=list)       # pages dont les liens ont été réécrits
    illisibles: list[str] = field(default_factory=list)  # pages qui citent la page mais n'ont pas pu être modifiées


def _texte_du_lien(vault, note, nouvelle_cle: str, ancien: str) -> str:
    """Ce qu'il faut écrire entre [[ ]] pour la page renommée : le nom seul s'il suffit (et que l'ancien lien
    était écrit ainsi), sinon le chemin complet."""
    nom = nouvelle_cle.rsplit("/", 1)[-1]
    autres = [n for n in vault.notes.values() if n is not note and n.name.lower() == nom.lower()]
    texte = nom if "/" not in ancien and not autres else nouvelle_cle
    return texte + (".md" if ancien.lower().endswith(".md") else "")


def reecrire_liens(vault, source: Note, note: Note, nouvelle_cle: str, texte: str) -> tuple[str, int]:
    """Réécrit dans ``texte`` (la page ``source``) les liens qui mènent à ``note`` ; renvoie (texte, nombre).
    Un lien écrit avec un alias de la page ([[Ancien titre]]) reste tel quel : l'alias suit la page."""
    compte = 0

    def vise(cible: str) -> bool:
        cible = cible.strip()
        base = cible.removesuffix(".md").rsplit("/", 1)[-1]
        return bool(cible) and base.lower() == note.name.lower() and vault.resolve(cible, source) is note

    def wikilien(m):
        nonlocal compte
        interieur = m.group(2)
        brut = re.split(r"[|#]", interieur, maxsplit=1)[0]
        coeur = brut.rstrip("\\")  # [[Page\|alias]] dans un tableau
        if not vise(coeur):
            return m.group(0)
        compte += 1
        espaces_avant = coeur[:len(coeur) - len(coeur.lstrip())]
        espaces_apres = coeur[len(coeur.rstrip()):]
        nouveau = espaces_avant + _texte_du_lien(vault, note, nouvelle_cle, coeur.strip()) + espaces_apres
        return f"{m.group(1)}[[{nouveau}{interieur[len(coeur):]}]]"

    def mdlien(m):
        nonlocal compte
        cible = unquote(m.group(1))
        if not vise(cible):
            return m.group(0)
        compte += 1
        nouveau = quote(nouvelle_cle + ".md", safe="/")
        debut, fin = m.span(1)
        return m.group(0)[:debut - m.start()] + nouveau + m.group(0)[fin - m.start():]

    def segment(s):
        return MDLINK_RE.sub(mdlien, WIKILINK_RE.sub(wikilien, s))

    return map_outside_code(texte, segment), compte


def renommer(vault, note: Note, nouvelle_cle: str, auteur: str) -> BilanRenommage:
    """Renomme ou déplace la page ``note`` vers ``nouvelle_cle`` (chemin sans .md, déjà validé et libre)."""
    ancienne_cle, ancien_rel = note.key, note.path.as_posix()
    nouveau_rel = f"{nouvelle_cle}.md"
    bilan = BilanRenommage(cle=nouvelle_cle)
    source = vault.root / note.path
    cible = vault.root / nouveau_rel

    # 1. Liens des autres pages (et de la page elle-même), avant de déplacer : la résolution se fait sur l'index actuel
    sources = set(vault.backlinks.get(note.key.lower(), ())) | {note}
    reecritures = []
    for page in sorted(sources, key=lambda n: n.key.lower()):
        chemin = vault.root / page.path
        try:
            texte, _ = lire_note(chemin)
        except OSError:
            bilan.illisibles.append(page.key)
            continue
        nouveau, n = reecrire_liens(vault, page, note, nouvelle_cle, texte)
        if n:
            reecritures.append((page, chemin, texte, nouveau, n))

    # 2. Le fichier (deux temps si seule la casse change : Windows ne voit pas la différence)
    cible.parent.mkdir(parents=True, exist_ok=True)
    if nouveau_rel.lower() == ancien_rel.lower():
        intermediaire = source.with_name(f".renommage-{source.name}")
        source.replace(intermediaire)
        intermediaire.replace(cible)
    else:
        source.replace(cible)
    historique.deplacer(ancien_rel, nouveau_rel)
    _retirer_dossiers_vides(source.parent, vault.root.resolve())

    # 3. Pages qui citaient l'ancienne (la page elle-même est maintenant à son nouveau chemin)
    for page, chemin, texte, nouveau, n in reecritures:
        if page is note:
            chemin, rel = cible, nouveau_rel
        else:
            rel = page.path.as_posix()
        try:
            historique.sauvegarder(rel, texte, auteur)
            chemin.write_text(nouveau, encoding="utf-8", newline="\n")
        except OSError:
            bilan.illisibles.append(page.key)
            continue
        bilan.liens += n
        if page is not note:
            bilan.pages.append(page.key)

    # 4. Ce que la base et les sets de QCM retiennent par clé
    _cles_en_base(ancienne_cle, nouvelle_cle)
    _sets_de_qcm(vault.root, ancienne_cle, nouvelle_cle)
    invalider()
    return bilan


def _cles_en_base(ancienne: str, nouvelle: str) -> None:
    from ..ai.models import AITutorExchange
    from .models import WikiFavorite, WikiVisit
    for modele in (WikiFavorite, WikiVisit):
        for ligne in modele.query.filter_by(key=ancienne).all():
            deja = modele.query.filter_by(user_id=ligne.user_id, key=nouvelle).first()
            if deja is not None and deja is not ligne:
                db.session.delete(ligne)  # le compte avait déjà la nouvelle clé (ancienne page du même nom)
            else:
                ligne.key = nouvelle
    AITutorExchange.query.filter_by(page=ancienne).update({"page": nouvelle}, synchronize_session=False)
    db.session.commit()


def _sets_de_qcm(racine: Path, ancienne: str, nouvelle: str) -> None:
    """Sets de QCM générés d'après la page (« source_page ») : ils restent rattachés à elle."""
    from .commun import DOSSIER_QCM
    dossier = racine / DOSSIER_QCM
    if not dossier.is_dir():
        return
    for f in dossier.rglob("*.json"):
        try:
            texte = f.read_text(encoding="utf-8")
            if ancienne not in texte:
                continue
            data = json.loads(texte)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and str(data.get("source_page", "")).strip() == ancienne:
            data["source_page"] = nouvelle
            try:
                f.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            except OSError:
                journal.warning("Renommage de « %s » : set de QCM %s non mis à jour", ancienne, f, exc_info=True)

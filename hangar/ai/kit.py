"""Kit de révision d'un contrôle : en une fois, la fiche et le QCM de chaque page d'une collection du wiki qui n'en a
pas encore, générés par l'IA avec les travaux de fond existants (``travaux.demander_fiche`` / ``demander_qcm``).

- Pages proposées : celles posées directement dans le dossier de la collection, sauf les sommaires (« 00 -
  Sommaire »), les fiches (« X (Fiche) ») et les versions de l'IA (« X (IA) »), pour ne pas faire la fiche d'une
  fiche. Une page qui a déjà sa fiche ou un QCM écrit d'après elle (``source_page``) n'est pas recochée.
- Les sets importés de Revision n'indiquent pas leur page source : un set de la catégorie au titre proche de la
  page est signalé et sa case est décochée, pour éviter un doublon payant. Toutes les cases restent modifiables.
- Coût estimé avant de lancer, refusé s'il dépasse ce qui reste sous le plafond mensuel (dépense du mois et
  réserves des lots en cours comprises, plafond du fournisseur aussi). En lot, l'estimation est celle que la file
  réservera au dépôt de chaque lot : le kit accepté ne bute pas ensuite sur le plafond à mi-chemin.
- Les fiches sont écrites directement dans le wiki (« Page (Fiche) ») ; les QCM arrivent en brouillon à relire, et
  prennent à leur publication la date d'examen du contrôle choisi. Une seule notification, à la fin du kit.
"""
from __future__ import annotations

import re
import uuid
from datetime import date, datetime, time

from .. import heure
from ..extensions import db
from ..models import now
from ..texte import cle_tri, plier
from . import core, travaux
from .models import AISettings
from .providers import AIError, Request, Result, Usage

SORTIE_FICHE = 6000          # jetons de sortie prévus pour une fiche (réflexion comprise), hors lot
SORTIE_QCM_BASE = 4000       # … pour un QCM : réflexion, puis environ 300 jetons par question
SORTIE_PAR_QUESTION = 300
NOMBRE_DEFAUT = 15
_IGNOREES = re.compile(r"sommaire|\((fiche|ia(?: \d+)?)\)\s*$", re.I)


def pages_de(vault, dossier: str) -> list:
    """Pages posées directement dans le dossier de la collection, sommaires, fiches et versions de l'IA exclus."""
    d = dossier.strip("/").lower()
    notes = [n for n in vault.notes.values() if n.folder.lower() == d and not _IGNOREES.search(n.name)]
    return sorted(notes, key=lambda n: cle_tri(n.name))


def _mots(texte: str) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9]+", plier(texte or "")) if len(w) > 3}


def _proche(titre_page: str, titres: list[str]) -> str:
    """Titre d'un set qui semble porter sur la page (au moins les deux tiers de ses mots significatifs), ou ""."""
    mots = _mots(titre_page)
    if not mots:
        return ""
    for t in titres:
        if len(mots & _mots(t)) * 3 >= 2 * len(mots):
            return t
    return ""


def controles(user, dossier: str) -> list[dict]:
    """Contrôles à venir des matières reliées à cette collection (lien de matière de l'agenda), le plus proche
    d'abord : [{id, titre, date, categorie}]."""
    from ..agenda import is_exam, subject_key
    from ..models import AgendaEvent, AgendaSubjectLink

    d = dossier.strip("/").lower()
    liens = {lk.subject: lk for lk in AgendaSubjectLink.query.filter_by(user_id=user.id)
             if (lk.wiki_path or "").strip("/").lower() == d}
    if not liens:
        return []
    debut = datetime.combine(heure.today(), time(0))
    out = []
    for e in (AgendaEvent.query.filter(AgendaEvent.user_id == user.id, AgendaEvent.status == "active",
                                       AgendaEvent.cancelled.is_(False), AgendaEvent.start >= debut)
              .order_by(AgendaEvent.start).limit(400)):
        lk = liens.get(subject_key(e.title))
        if lk is not None and is_exam(e):
            out.append({"id": e.id, "titre": e.title, "date": e.start.date(), "categorie": lk.qcm_category or ""})
    return out


def categorie_liee(user, dossier: str) -> str:
    """Catégorie de QCM d'une matière reliée à la collection (où ranger les sets), sinon ""."""
    from ..models import AgendaSubjectLink

    d = dossier.strip("/").lower()
    for lk in AgendaSubjectLink.query.filter_by(user_id=user.id):
        if (lk.wiki_path or "").strip("/").lower() == d and lk.qcm_category:
            return lk.qcm_category
    return ""


def _cout(row, req: Request, sortie: int, lot: bool) -> float:
    if lot:
        return core.estimate_request_cost(row, req, batch=True)  # la réserve que prendra le lot à son dépôt
    res = Result(model=req.model, kind=row.kind,
                 usage=Usage(input_tokens=core.estimate_input_tokens(req), output_tokens=sortie))
    return core.estimate_cost(row, res)


def cout_fiche(row, note, lot: bool = False) -> float:
    req = Request(messages=travaux._msgs(f"Page « {note.title} » :\n\n{note.body}",
                                         "Rédige la fiche de révision de cette page."),
                  model=row.model_for("chat"), system=travaux.CONSIGNE_FICHE,
                  max_tokens=travaux.MAX_TOKENS_GENERATION)
    return _cout(row, req, SORTIE_FICHE, lot)


def cout_qcm(row, note, nombre: int, lot: bool = False) -> float:
    req = Request(messages=travaux._msgs(f"Cours « {note.title} » :\n\n{note.body}",
                                         f"Écris {nombre} questions sur ce cours."),
                  model=row.model_for("chat"), system=travaux.CONSIGNE_QCM, json_schema=travaux.SCHEMA_QCM,
                  max_tokens=travaux.MAX_TOKENS_GENERATION)
    return _cout(row, req, SORTIE_QCM_BASE + SORTIE_PAR_QUESTION * nombre, lot)


def reste_plafond(row) -> float | None:
    """Ce qui reste sous le plafond mensuel (dépense et réserves des lots en cours déduites), et sous celui du
    fournisseur ; None s'il n'y a aucun plafond."""
    restes = []
    cap = AISettings.get().monthly_cap or 0
    if cap > 0:
        restes.append(cap - core.month_cost() - core.reserved_cost())
    if row is not None and row.monthly_cap:
        restes.append(row.monthly_cap - core.month_cost(provider_id=row.id) - core.reserved_cost(row.id))
    return round(max(0.0, min(restes)), 4) if restes else None


def plan(user, vault, dossier: str, nombre: int = NOMBRE_DEFAUT, categorie: str = "") -> dict:
    """Pages de la collection et ce qui leur manque, avec le coût estimé de chaque génération (hors lot et en lot)."""
    from ..qcm import services

    try:
        row = core.pick("chat")
    except AIError:
        row = None
    sets = list(services.bibliotheque(user.id).sets.values())
    par_page: dict[str, list] = {}
    for s in sets:
        if s.source_page:
            par_page.setdefault(s.source_page.strip().lower(), []).append(s)
    voisins = [s.titre for s in sets if not s.source_page and (not categorie or s.categorie == categorie)]
    lignes = []
    for note in pages_de(vault, dossier):
        source = note.path.as_posix()
        fiche_existe = vault.notes.get(travaux.chemin_fiche(note).removesuffix(".md").lower()) is not None
        fiche_en_cours = travaux.en_cours("fiche", source)
        qcm_existants = par_page.get(note.key.lower(), [])
        qcm_en_cours = travaux.en_cours("qcm", source)
        proche = "" if qcm_existants else _proche(note.title, voisins)
        lignes.append({
            "cle": note.key, "titre": note.title, "url": note.url,
            "fiche": {"existe": fiche_existe, "en_cours": fiche_en_cours,
                      "possible": not fiche_existe and not fiche_en_cours, "coche": not fiche_existe and not fiche_en_cours,
                      "cout": cout_fiche(row, note) if row else 0.0, "cout_lot": cout_fiche(row, note, True) if row else 0.0},
            "qcm": {"existants": [s.titre for s in qcm_existants], "en_cours": qcm_en_cours, "proche": proche,
                    "possible": not qcm_existants and not qcm_en_cours,
                    "coche": not qcm_existants and not qcm_en_cours and not proche,
                    "cout": cout_qcm(row, note, nombre) if row else 0.0,
                    "cout_lot": cout_qcm(row, note, nombre, True) if row else 0.0},
        })
    return {"lignes": lignes, "row": row, "reste": reste_plafond(row) if row else None,
            "payant": bool(row) and (row.kind == "anthropic" or bool(row.price_input or row.price_output))}


def lancer(user, vault, dossier: str, fiches: list[str], qcms: list[str], *, nombre: int, lot: bool,
           categorie: str = "", date_examen: date | None = None) -> dict:
    """Vérifie le coût des pages cochées face au plafond restant, puis crée les travaux du kit. Renvoie
    {fiches, qcm, cout} ; lève AIError (code « plafond ») si l'estimation dépasse ce qui reste."""
    row = core.pick("chat")
    pages = {n.key.lower(): n for n in pages_de(vault, dossier)}
    fiches = [pages[c.lower()] for c in dict.fromkeys(fiches) if c.lower() in pages]
    qcms = [pages[c.lower()] for c in dict.fromkeys(qcms) if c.lower() in pages]
    fiches = [n for n in fiches if not travaux.en_cours("fiche", n.path.as_posix())]
    qcms = [n for n in qcms if not travaux.en_cours("qcm", n.path.as_posix())]
    if not fiches and not qcms:
        raise AIError("Coche au moins une fiche ou un QCM à générer.", "vide", 400)
    cout = round(sum(cout_fiche(row, n, lot) for n in fiches) + sum(cout_qcm(row, n, nombre, lot) for n in qcms), 4)
    reste = reste_plafond(row)
    if reste is not None and cout > reste:
        raise AIError(f"Kit trop cher pour ce qui reste du plafond mensuel : environ {cout:.2f} $ pour "
                      f"{reste:.2f} $ disponibles. Décoche des pages, ou relève le plafond dans les réglages de l’IA.",
                      "plafond", 429)
    kit = (uuid.uuid4().hex, dossier.strip("/").rsplit("/", 1)[-1] or dossier)
    faits = {"fiches": 0, "qcm": 0, "cout": cout}
    try:
        for note in fiches:
            travaux.demander_fiche(note, user, lot=lot, kit=kit)
            faits["fiches"] += 1
        for note in qcms:
            travaux.demander_qcm(note=note, nombre=nombre, categorie=travaux.dossier_valide(categorie) or "",
                                 user=user, lot=lot, date_examen=date_examen, kit=kit)
            faits["qcm"] += 1
    except AIError as e:
        e.faits = faits  # plafond atteint en route : ce qui est déjà lancé continue
        raise
    db.session.commit()
    return faits


def date_controle(user, eid) -> tuple[date | None, str]:
    """(jour, catégorie liée) d'un contrôle du compte, ou (None, "")."""
    from ..agenda import _subject_link, is_exam
    from ..models import AgendaEvent

    try:
        e = db.session.get(AgendaEvent, int(eid)) if eid else None
    except (TypeError, ValueError):
        return None, ""
    if e is None or e.user_id != user.id or not is_exam(e) or e.start < datetime.combine(now().date(), time(0)):
        return None, ""
    lk = _subject_link(user, e.title)
    return e.start.date(), (lk.qcm_category if lk else "") or ""

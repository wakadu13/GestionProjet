"""Erreurs du serveur gardées pour les administrateurs : page ``/erreurs/`` (port de ``wiki/erreurs.py`` de Revision).

Chaque page en erreur 500 (gestionnaire d'erreur de ``hangar/__init__.py``) et chaque incident du fil des
travaux de l'IA (``hangar.ai.jobs``) est noté ; les erreurs identiques (même type, même message, même endroit
du code) sont regroupées et comptées. Une erreur réglée qui revient est rouverte.

Rien ici ne doit lui-même lever d'exception : une base indisponible ne doit pas cacher l'erreur d'origine.
L'enregistrement passe par une session à part (sa propre connexion) : il n'emporte jamais les écritures
en cours de la requête ou du travail qui a échoué (à appeler après ``db.session.rollback()``).
"""
from __future__ import annotations

import hashlib
import logging
import sys
import traceback
from pathlib import Path

from ..models import now

log = logging.getLogger(__name__)
MAX_ERREURS = 200
MAX_TRACE = 20_000
RACINE = Path(__file__).resolve().parent.parent.parent  # dossier du projet (au-dessus du paquet hangar)


def noter(exc: BaseException | None = None, contexte: str = "", request=None, user=None,
          dans_la_transaction: bool = False) -> None:
    """Enregistre l'exception ``exc`` (sinon celle en cours de traitement).

    ``request`` : la requête en erreur (adresse, méthode) ; ``contexte`` : à défaut, ce qui tournait
    (ex. « Travail d'IA 12 (fiche) ») ; ``user`` : le compte (par défaut celui de la requête)."""
    try:
        if exc is None:
            _, exc, _ = sys.exc_info()
        if exc is None:
            return
        _enregistrer(exc, contexte, request, user, dans_la_transaction)
    except Exception:  # noqa: BLE001
        log.exception("Erreur impossible à enregistrer")


def signaler(contexte: str, exc: BaseException | None = None, journal: logging.Logger | None = None,
             dans_la_transaction: bool = False) -> None:
    """Garde-fou qui ne doit pas casser la page ou le fil (on continue avec une solution de repli), mais sans
    avaler l'erreur en silence : elle est écrite dans le journal (avec sa trace) ET notée sur ``/erreurs/``.

    À appeler dans un ``except`` : ``exc`` par défaut est l'exception en cours. Ne lève jamais.
    ``dans_la_transaction`` : le travail continue et a peut-être déjà écrit dans la base (SQLite n'accepte alors
    pas d'autre écriture en parallèle) ; l'erreur est ajoutée à sa transaction, enregistrée avec elle."""
    try:
        if exc is None:
            _, exc, _ = sys.exc_info()
        if exc is None:
            return
        cible = journal
        if cible is None:
            try:
                from flask import current_app
                cible = current_app.logger
            except RuntimeError:  # hors application
                cible = log
        cible.error("%s", contexte, exc_info=(type(exc), exc, exc.__traceback__))
    except Exception:  # noqa: BLE001
        pass
    noter(exc, contexte=contexte, dans_la_transaction=dans_la_transaction)


def _enregistrer(exc, contexte, request, user, dans_la_transaction=False):
    from sqlalchemy.orm import Session

    from ..extensions import db

    type_ = type(exc)
    pile = traceback.extract_tb(exc.__traceback__)
    lieu = _lieu(pile)
    message = str(exc)[:2000]
    empreinte = hashlib.sha1(f"{type_.__name__}\x1f{message}\x1f{lieu}".encode("utf-8")).hexdigest()
    trace = "".join(traceback.format_exception(type_, exc, exc.__traceback__))[-MAX_TRACE:]
    if request is not None:
        chemin, methode = request.full_path.rstrip("?")[:500], request.method
    else:
        chemin, methode = contexte[:500], ""
    if user is None and request is not None:
        try:
            from flask_login import current_user
            user = current_user if current_user.is_authenticated else None
        except Exception:  # noqa: BLE001
            user = None
    qui = getattr(user, "username", "") or ""
    if dans_la_transaction:
        with db.session.begin_nested():  # point de reprise : un échec ici n'annule pas le travail en cours
            _ecrire(db.session, empreinte, type_, message, lieu, chemin, methode, qui, trace)
        return
    with Session(db.engine) as s:
        _ecrire(s, empreinte, type_, message, lieu, chemin, methode, qui, trace)
        s.commit()


def _ecrire(s, empreinte, type_, message, lieu, chemin, methode, qui, trace):
    from .models import ErreurServeur

    maintenant = now()
    e = s.query(ErreurServeur).filter_by(empreinte=empreinte).first()
    if e is None:
        s.add(ErreurServeur(empreinte=empreinte, type=type_.__name__[:200], message=message, lieu=lieu[:500],
                            chemin=chemin, methode=methode, utilisateur=qui[:150], trace=trace, nombre=1,
                            premiere=maintenant, derniere=maintenant, reglee=False))
        s.flush()
        anciennes = [pk for (pk,) in s.query(ErreurServeur.id).order_by(ErreurServeur.derniere.desc())
                     .offset(MAX_ERREURS).all()]
        if anciennes:
            s.query(ErreurServeur).filter(ErreurServeur.id.in_(anciennes)).delete(synchronize_session=False)
    else:
        e.nombre = (e.nombre or 0) + 1
        e.derniere, e.chemin, e.methode, e.utilisateur, e.trace, e.reglee = (
            maintenant, chemin, methode, qui[:150], trace, False)


def _lieu(pile) -> str:
    """Endroit du code de Hangar où l'erreur est née : la dernière ligne qui est dans le projet
    (pas dans Python ni dans une bibliothèque), en chemin relatif."""
    for cadre in reversed(pile):
        try:
            chemin = Path(cadre.filename).resolve()
        except (OSError, ValueError):
            continue
        if chemin.is_relative_to(RACINE) and ".venv" not in chemin.parts and "site-packages" not in chemin.parts:
            return f"{chemin.relative_to(RACINE).as_posix()}:{cadre.lineno}"
    return f"{pile[-1].filename}:{pile[-1].lineno}" if pile else ""


def ouvertes() -> int:
    """Nombre d'erreurs à regarder (badge du menu des administrateurs) ; 0 si la table est illisible."""
    try:
        from .models import ErreurServeur
        return ErreurServeur.query.filter_by(reglee=False).count()
    except Exception:  # noqa: BLE001 - le menu ne doit jamais casser une page
        return 0

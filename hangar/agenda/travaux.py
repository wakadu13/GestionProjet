"""Agenda : travaux de fond lancés par le fil des e-mails (rappels, résumé quotidien, maîtrise des QCM)."""
from __future__ import annotations

from datetime import datetime

from flask import current_app

from .. import agenda as A  # le paquet : voir hangar/agenda/__init__.py
from ..extensions import db


def run_jobs(app=None, at: datetime | None = None) -> dict:
    """Appelé par le fil d'envoi des e-mails (toutes les 30 s). Les deux travaux sont protégés séparément :
    une erreur dans les rappels n'empêche pas le résumé quotidien (et inversement). La lecture de la source
    de l'emploi du temps (`check_sources`) est une étape à part du fil, juste avant."""
    from ..mailer import _ctx
    with _ctx(app):
        at = at or A.now()
        out = {"reminders": _job("rappels de l’agenda", A.process_reminders, at),
               "daily": _job("résumé quotidien de l’agenda", A.send_daily_programs, at)}
        _job("maîtrise des QCM le jour des contrôles", _figer_maitrise, at)
        return out


def _figer_maitrise(at: datetime) -> int:
    from .. import controles
    return controles.figer_maitrise(at)


def _job(nom: str, fn, at: datetime) -> int:
    try:
        return fn(at)
    except Exception:  # noqa: BLE001 - noté sur /erreurs/, le fil continue
        db.session.rollback()
        current_app.logger.exception("Erreur dans le fil d’arrière-plan : %s", nom)
        A._mail().noter_erreur(f"Fil des e-mails : {nom}")
        return 0



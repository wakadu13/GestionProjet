"""Travaux d'IA en arrière-plan : longues générations (transformation d'un cours en page, QCM…) faites hors des
requêtes web, une à la fois, avec reprise après un redémarrage du serveur ; pour Claude, envoi possible en lot
(Message Batches : moitié prix, résultat sous 24 heures).

    from hangar.ai import jobs

    @jobs.register_job("qcm")
    def faire_qcm(job, ai):
        r = ai(messages, json_schema=SCHEMA, max_tokens=64000)  # mêmes paramètres que generate() (sans stream)
        return {"set": r.data}                                   # enregistré dans job.result

    job = jobs.enqueue("qcm", {"page": "Cours/BDD"}, user=current_user, title="QCM BDD", batch=True)

En lot, le premier appel `ai(...)` part dans un lot et le travail passe à l'état « batch » ; quand le lot est
terminé, la fonction est relancée et `ai(...)` renvoie directement le résultat reçu (les appels déjà faits sont
rejoués depuis job.calls, sans nouvel appel à l'API).
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Callable

from flask import current_app

from ..extensions import db
from ..models import now
from . import core
from . import providers as P
from .models import AIJob, AIProvider
from .providers import AIError, Result

log = logging.getLogger(__name__)

BATCH_POLL_S = 600
BATCH_MAX_DAYS = 29        # Anthropic garde les résultats d'un lot 29 jours
LOT_ECHEC_DEFINITIF = ("cle", "acces", "modele", "lot")  # relève impossible : inutile de réessayer
ANNULE = "Annulé à ta demande."
_HANDLERS: dict[str, Callable] = {}
_lock = threading.Lock()
_worker: threading.Thread | None = None
_resumed = False


class Deferred(Exception):
    """La requête est partie dans un lot : le travail reprendra à l'arrivée des résultats."""


class Annule(Exception):
    """Le travail a été annulé pendant qu'il tournait (bouton « Annuler ») : on s'arrête sans rien écrire."""


def _annule(job: AIJob) -> bool:
    """Vrai si le travail n'est plus « en cours » en base (annulé depuis une autre requête). Sans écrire d'abord
    les changements en mémoire (un brouillon « draft » pas encore enregistré n'est pas une annulation)."""
    with db.session.no_autoflush:
        return db.session.query(AIJob.status).filter(AIJob.id == job.id).scalar() != "running"


def register_job(kind: str):
    def deco(fn):
        _HANDLERS[kind] = fn
        return fn
    return deco


def enqueue(kind: str, payload: dict | None = None, *, user=None, feature: str = "", title: str = "",
            batch: bool = False, start: bool = True) -> AIJob:
    job = AIJob(kind=kind, status="pending", user_id=core._user_id(user), feature=feature or kind, title=title[:300],
                created_at=now())
    job.payload = payload or {}
    job.options = {"batch": bool(batch)}
    db.session.add(job)
    db.session.commit()
    if start:
        start_worker()
    return job


def _bound_ai(job: AIJob):
    state = {"i": 0, "warnings": []}

    def ai(messages, **kw):
        i = state["i"]
        state["i"] += 1
        calls = job.calls
        if i < len(calls) and calls[i] is not None:
            c = calls[i]
            if "error" in c:
                raise AIError(c["error"], c.get("code") or "lot")
            r = Result.from_dict(c)
            if kw.get("json_schema") is not None and r.data is None:
                r.data = core._parse_json(r, kw["json_schema"])
            return r
        kw.pop("stream", None)
        kw.setdefault("user", job.user_id)
        kw.setdefault("feature", job.feature or job.kind)
        if _annule(job):
            raise Annule()
        if job.options.get("batch"):
            gen_kw = {k: v for k, v in kw.items() if k not in ("user", "feature", "allow_refusal")}
            row, prov, req = core.prepare(messages, **gen_kw)
            if prov.supports_batch:
                # Coût prévu du lot, réservé jusqu'à son retour : un nouveau lot ne part que si la dépense du mois et
                # les réserves des lots en cours restent sous le plafond (les autres appels ne comptent pas les
                # réserves).
                core.check_batch_cap(row)
                reserve = core.estimate_request_cost(row, req, batch=True)
                batch_id, files = prov.batch_submit([(f"job{job.id}-{i}", req)])
                opts = job.options
                opts.update(files=files, batch_call=i, reserve=reserve, batch_at=now().isoformat())
                job.options, job.batch_id, job.provider_id = opts, batch_id, row.id
                job.status, job.message = "batch", "Dans un lot : résultat sous 24 heures, moitié prix."
                db.session.commit()
                raise Deferred()
        r = core.generate(messages, **kw)
        if _annule(job):  # annulé pendant l'appel : rien n'est gardé ni écrit
            raise Annule()
        state["warnings"] += [w for w in r.warnings if w not in state["warnings"]]
        calls = job.calls
        calls += [None] * (i + 1 - len(calls))
        calls[i] = r.to_dict()
        job.calls = calls
        db.session.commit()
        return r

    ai.warnings = state["warnings"]
    return ai


def run_job(job: AIJob) -> None:
    handler = _HANDLERS.get(job.kind)
    if handler is None:
        job.status, job.message, job.finished_at = "error", f"Type de travail inconnu : {job.kind}.", now()
        db.session.commit()
        return
    job.status, job.started_at = "running", job.started_at or now()
    if (job.message or "").startswith("Dans un lot"):
        job.message = ""
    db.session.commit()
    ai = _bound_ai(job)
    try:
        result = handler(job, ai)
        if _annule(job):
            raise Annule()
        if result is not None:
            job.result = result
        if job.status == "running":  # la fonction peut laisser un brouillon (« draft ») à relire
            job.status = "done"
        if ai.warnings:  # ex. cours plus long que la mémoire du modèle local
            job.message = " ".join([job.message or "", *ai.warnings]).strip()[:500]
    except Deferred:
        return
    except Annule:
        db.session.rollback()  # l'annulation (statut et message) est déjà en base
        return
    except AIError as e:
        db.session.rollback()
        job.status, job.message = "error", e.message[:500]
    except Exception:  # noqa: BLE001
        log.exception("Travail d'IA %s (%s)", job.id, job.kind)
        db.session.rollback()
        _noter_erreur(f"Travail d'IA {job.id} ({job.kind})")
        job.status, job.message = "error", "Erreur inattendue (détail dans le journal du serveur)."
    job.finished_at = now()
    db.session.commit()
    notifier(job)


def notifier(job: AIJob) -> None:
    """Notification Hangar (badge et toast) quand un travail long se termine."""
    if not job.user_id or job.options.get("silencieux"):
        return
    from flask import url_for

    from ..models import Notification

    titre = job.title or job.kind
    message = {"done": f"Travail de l’IA terminé : {titre}", "draft": f"À relire avant publication : {titre}",
               "error": f"Travail de l’IA en échec : {titre}"}.get(job.status)
    if message is None:
        return
    if kit := job.options.get("kit"):
        # Kit de révision : une seule notification, quand son dernier travail se termine (pas 30 d'affilée).
        message = _message_kit(job, kit)
        if message is None:
            return
    try:
        url = (url_for("ai.transformations") if job.options.get("kit") else None) or job.options.get("url") \
            or url_for("ai.transformations")
    except Exception:  # noqa: BLE001 - hors requête
        url = "/transformations/"
    try:
        db.session.add(Notification(user_id=job.user_id, kind="ai", message=message[:500], url=url))
        db.session.commit()
    except Exception:  # noqa: BLE001 - la notification ne doit pas faire échouer le travail
        db.session.rollback()
        log.exception("Notification du travail %s", job.id)


def _jobs_du_kit(kit: str) -> list[AIJob]:
    # L'identifiant du kit (32 caractères hexadécimaux) ne peut pas apparaître ailleurs dans les options.
    return [j for j in AIJob.query.filter(AIJob.options_json.contains(kit)) if j.options.get("kit") == kit]


def _message_kit(job: AIJob, kit: str) -> str | None:
    """Message de fin d'un kit de révision, ou None tant qu'un de ses travaux est en attente, en cours ou en lot."""
    lot = _jobs_du_kit(kit)
    if any(j.id != job.id and j.status in ("pending", "running", "batch") for j in lot):
        return None
    statuts = [j.status for j in lot]
    nom = job.options.get("kit_nom") or "ta collection"
    morceaux = []
    if n := statuts.count("draft"):
        morceaux.append(f"{n} QCM à relire")
    if n := statuts.count("done"):
        morceaux.append(f"{n} terminé{'s' if n > 1 else ''}")
    if n := statuts.count("error"):
        morceaux.append(f"{n} en échec")
    return f"Kit de révision prêt ({nom}) : " + ", ".join(morceaux) + "."


def run_pending(limit: int | None = None) -> int:
    """Fait les travaux en attente, ici et maintenant (tests, commande `hangar ia-travaux`)."""
    global _resumed
    if not _resumed:
        # Interrompus par un arrêt du serveur : on les reprend.
        AIJob.query.filter_by(status="running").update({"status": "pending"})
        db.session.commit()
        _resumed = True
    n = 0
    while limit is None or n < limit:
        job = AIJob.query.filter_by(status="pending").order_by(AIJob.created_at, AIJob.id).first()
        if job is None:
            break
        jid = job.id
        try:
            run_job(job)
        except Exception:
            # Erreur hors du gestionnaire (base verrouillée au dernier enregistrement…) : le travail ne doit pas
            # rester « En cours » jusqu'au prochain redémarrage ; il passe en échec, relançable.
            db.session.rollback()
            AIJob.query.filter_by(id=jid, status="running").update({
                "status": "error", "finished_at": now(),
                "message": "Travail interrompu par une erreur inattendue : tu peux le relancer."})
            db.session.commit()
            raise
        n += 1
    return n


def annuler(job: AIJob) -> bool:
    """Bouton « Annuler » : un travail en attente, en cours ou dans un lot passe en échec (relançable). Un lot est
    aussi annulé chez Anthropic (les requêtes déjà traitées restent facturées). Un travail en cours s'arrête avant
    son prochain appel à l'IA, ou dès la réponse de l'appel en cours, sans rien écrire. False s'il n'y a rien à
    annuler ; AIError si Anthropic ne répond pas (le lot continue : réessaie)."""
    if job.status not in ("pending", "running", "batch"):
        return False
    if job.status == "batch" and job.batch_id:
        row = db.session.get(AIProvider, job.provider_id or 0)
        if row is not None:
            prov = P.get(row)
            try:
                prov.batch_cancel(job.batch_id)
            except AIError as e:
                if e.code not in LOT_ECHEC_DEFINITIF:
                    raise AIError(f"Lot non annulé : {e.message}", e.code, e.status) from None
            _retirer_fichiers(prov, job)
    job.status, job.message, job.finished_at = "error", ANNULE, now()
    db.session.commit()
    return True


def check_batches() -> int:
    """Récupère les résultats des lots terminés ; les travaux concernés repassent « en attente ». Un lot impossible
    à relever (clé changée, lot introuvable ou expiré, déposé il y a plus de 29 jours) passe en échec, relançable ;
    une panne passagère (réseau, service) est réessayée au passage suivant. Renvoie le nombre de travaux sortis
    de l'état « Dans un lot »."""
    n = 0
    for job in AIJob.query.filter_by(status="batch").all():
        row = db.session.get(AIProvider, job.provider_id or 0)
        if row is None:
            job.status, job.message = "error", "Fournisseur du lot supprimé."
            db.session.commit()
            continue
        prov = P.get(row)
        try:
            out = prov.batch_poll(job.batch_id)
        except AIError as e:
            log.warning("Lot %s : %s", job.batch_id, e.message)
            if e.code in LOT_ECHEC_DEFINITIF or _lot_perime(job):
                _lot_en_echec(job, prov, e.message)
                n += 1
            continue
        except Exception as e:  # noqa: BLE001 - une erreur imprévue ne doit ni bloquer les autres lots ni le fil
            log.exception("Lot %s", job.batch_id)
            db.session.rollback()
            if _lot_perime(job):
                _lot_en_echec(job, prov, f"erreur inattendue ({type(e).__name__}).")
                n += 1
            continue
        if out is None:
            if _lot_perime(job):
                _lot_en_echec(job, prov, "il n’est toujours pas terminé.")
                n += 1
            continue
        i = int(job.options.get("batch_call") or 0)
        r = out.get(f"job{job.id}-{i}")
        calls = job.calls
        calls += [None] * (i + 1 - len(calls))
        if isinstance(r, Result):
            r.provider, r.provider_id = row.name, row.id
            core.record_usage(row, r, job.user_id, job.feature or job.kind, batch=True)
            calls[i] = r.to_dict()
        elif isinstance(r, AIError):
            calls[i] = {"error": r.message, "code": r.code}
        else:
            calls[i] = {"error": "Lot terminé sans résultat pour ce travail.", "code": "lot"}
        job.calls, job.status, job.batch_id = calls, "pending", ""
        db.session.commit()
        _retirer_fichiers(prov, job)
        n += 1
    return n


def _lot_perime(job: AIJob) -> bool:
    """Lot déposé il y a plus de 29 jours : ses résultats ne sont plus disponibles chez Anthropic."""
    try:
        depot = datetime.fromisoformat(job.options.get("batch_at") or "")
    except (TypeError, ValueError):
        depot = job.started_at or job.created_at
    return depot is not None and now() - depot > timedelta(days=BATCH_MAX_DAYS)


def _lot_en_echec(job: AIJob, prov, raison: str) -> None:
    """Lot impossible à relever (clé changée, lot expiré, trop ancien) : le travail passe en échec, relançable, au
    lieu de rester « Dans un lot » pour toujours."""
    job.status, job.finished_at = "error", now()
    job.message = f"Lot non récupéré : {raison} Tu peux relancer le travail."[:500]
    db.session.commit()
    _retirer_fichiers(prov, job)
    notifier(job)


def start_worker(app=None) -> None:
    """Démarre le fil des travaux s'il ne tourne pas (rien en mode synchrone : tests, AI_JOBS_SYNC)."""
    global _worker
    app = app or current_app._get_current_object()
    if app.config.get("TESTING") or app.config.get("AI_JOBS_SYNC"):
        return
    with _lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=_loop, args=(app,), name="travaux-ia", daemon=True)
        _worker.start()


def resume(app=None) -> None:
    """Relance le fil s'il reste des travaux (après un redémarrage)."""
    if AIJob.query.filter(AIJob.status.in_(("pending", "running", "batch"))).first() is not None:
        start_worker(app)


def _loop(app) -> None:
    global _worker
    with app.test_request_context("/"):  # url_for (adresses des résultats, notifications) hors requête
        last_poll = 0.0
        try:
            while True:
                if run_pending(limit=1):
                    continue
                if AIJob.query.filter_by(status="batch").first() is None:
                    return
                if time.time() - last_poll >= app.config.get("AI_BATCH_POLL_S", BATCH_POLL_S):
                    last_poll = time.time()
                    check_batches()
                    continue
                db.session.remove()
                time.sleep(5)
        except Exception:  # noqa: BLE001 - le fil ne doit pas mourir en laissant _worker occupé
            log.exception("Travaux d'IA : arrêt inattendu")
            db.session.rollback()
            _noter_erreur("Fil des travaux d'IA : arrêt inattendu")
        finally:
            db.session.remove()
            with _lock:
                _worker = None


def _retirer_fichiers(prov, job: AIJob) -> None:
    """Supprime chez le fournisseur les fichiers envoyés pour un lot. Un échec n'arrête rien (ils expirent d'eux-mêmes)
    mais reste dans le journal."""
    try:
        prov.delete_files(job.options.get("files"))
    except Exception:  # noqa: BLE001
        log.warning("Travail d'IA %s : fichiers du lot non supprimés chez le fournisseur", job.id, exc_info=True)


def _noter_erreur(contexte: str) -> None:
    """Incident du fil des travaux : noté aussi sur la page « Erreurs du serveur » (/erreurs/)."""
    from ..wiki import erreurs
    erreurs.noter(contexte=contexte)  # ne lève jamais

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
_HANDLERS: dict[str, Callable] = {}
_lock = threading.Lock()
_worker: threading.Thread | None = None
_resumed = False


class Deferred(Exception):
    """La requête est partie dans un lot : le travail reprendra à l'arrivée des résultats."""


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
    state = {"i": 0}

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
        if job.options.get("batch"):
            gen_kw = {k: v for k, v in kw.items() if k not in ("user", "feature", "allow_refusal")}
            row, prov, req = core.prepare(messages, **gen_kw)
            if prov.supports_batch:
                batch_id, files = prov.batch_submit([(f"job{job.id}-{i}", req)])
                opts = job.options
                opts.update(files=files, batch_call=i)
                job.options, job.batch_id, job.provider_id = opts, batch_id, row.id
                job.status, job.message = "batch", "Dans un lot : résultat sous 24 heures, moitié prix."
                db.session.commit()
                raise Deferred()
        r = core.generate(messages, **kw)
        calls = job.calls
        calls += [None] * (i + 1 - len(calls))
        calls[i] = r.to_dict()
        job.calls = calls
        db.session.commit()
        return r

    return ai


def run_job(job: AIJob) -> None:
    handler = _HANDLERS.get(job.kind)
    if handler is None:
        job.status, job.message, job.finished_at = "error", f"Type de travail inconnu : {job.kind}.", now()
        db.session.commit()
        return
    job.status, job.started_at = "running", job.started_at or now()
    db.session.commit()
    try:
        result = handler(job, _bound_ai(job))
        job.result = result
        job.status = "done"
        if (job.message or "").startswith("Dans un lot"):
            job.message = ""
    except Deferred:
        return
    except AIError as e:
        db.session.rollback()
        job.status, job.message = "error", e.message[:500]
    except Exception:  # noqa: BLE001
        log.exception("Travail d'IA %s (%s)", job.id, job.kind)
        db.session.rollback()
        job.status, job.message = "error", "Erreur inattendue (détail dans le journal du serveur)."
    job.finished_at = now()
    db.session.commit()


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
        run_job(job)
        n += 1
    return n


def check_batches() -> int:
    """Récupère les résultats des lots terminés ; les travaux concernés repassent « en attente »."""
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
            continue
        if out is None:
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
        try:
            prov.delete_files(job.options.get("files"))
        except Exception:  # noqa: BLE001
            pass
        n += 1
    return n


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
    with app.app_context():
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
        finally:
            db.session.remove()
            with _lock:
                _worker = None

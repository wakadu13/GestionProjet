"""Rappel quotidien de révision : état à rappeler, notifications web, API de l'application Android.

Port de `etat_rappel`, `texte_rappel`, `rappel`, des vues `push_*` de `quiz_views.py` et de la
commande `envoyer_rappels` de Revision :

- `GET /api/rappel/` : ce qu'il reste à réviser (QCM, langues), la série et les réponses du jour
  (rappel et widget de l'application Android, avec son jeton ; ou session) ;
- `GET /api/push/cle/`, `POST /api/push/abonner|desabonner|tester/` : abonnement de l'appareil aux
  notifications web, à l'heure choisie (page « Aujourd'hui », `static/qcm/rappel_web.js`) ;
- `envoyer_rappels_dus()` : chaque appareil abonné reçoit au plus un rappel par jour, à partir de
  l'heure choisie, avec ce qu'il reste à réviser et la série ; rien si tout est fait. Appelé par le
  fil d'arrière-plan de Hangar (`mailer.start_worker`) et par `uv run hangar qcm-rappels`.
"""
from __future__ import annotations

from flask import current_app, jsonify, request
from flask_login import current_user, login_required

from ..extensions import db
from . import quiz, services
from .models import AbonnementPush, Activite
from .views import bp

TITRE = "Hangar · révisions"


def etat_rappel(user_id, now=None, library=None) -> dict:
    """Ce qu'il reste à réviser (QCM, langues), série et réponses du jour : rappel de l'application
    Android, notifications web."""
    now = now or quiz.maintenant()
    e = services.etat(user_id, now, library)
    act = Activite.resume(user_id, quiz.jour(now))
    return {"a_revoir": e["a_reviser"] + e["langues_a_revoir"], "qcm": e["a_reviser"],
            "langues": e["langues_a_revoir"], "serie": act["serie"], "aujourdhui": act["aujourdhui"]}


def texte_rappel(etat) -> str | None:
    """Texte de la notification de rappel, ou None s'il n'y a rien à rappeler (tout est révisé et
    on a déjà travaillé aujourd'hui)."""
    n, serie, auj = etat["a_revoir"], etat["serie"], etat["aujourdhui"]
    if n == 0 and auj > 0:
        return None
    texte = (f"{n} élément{'s' if n > 1 else ''} à réviser" if n else "Rien d'urgent : quelques nouveaux mots ?")
    if serie > 0:
        texte += f" · série de {serie} jour{'s' if serie > 1 else ''}" + (" à prolonger" if auj == 0 else "")
    return texte


def envoyer_rappels_dus(now=None, journal=None) -> dict:
    """Envoie les rappels dus ; renvoie les compteurs {envoyes, rien, expires, erreurs}."""
    from . import push

    now = now or quiz.maintenant()
    jour = now.date()
    compte = {"envoyes": 0, "rien": 0, "expires": 0, "erreurs": 0}
    abonnements = AbonnementPush.query.filter(
        (AbonnementPush.dernier_envoi.is_(None)) | (AbonnementPush.dernier_envoi < jour)).all()
    etats: dict = {}
    for a in abonnements:
        if (a.heure, a.minute) > (now.hour, now.minute) or a.user is None or not a.user.active:
            continue
        if a.user_id not in etats:
            etats[a.user_id] = etat_rappel(a.user_id, now)
        texte = texte_rappel(etats[a.user_id])
        if texte is None:
            compte["rien"] += 1
        else:
            try:
                push.envoyer(a, TITRE, texte)
                compte["envoyes"] += 1
            except push.AbonnementExpire:
                db.session.delete(a)
                db.session.commit()
                compte["expires"] += 1
                continue
            except Exception as e:  # noqa: BLE001 — réseau, service de notifications…
                compte["erreurs"] += 1
                if journal:
                    journal(f"{a} : {e}")
                continue
        a.dernier_envoi = jour
        db.session.commit()
    return compte


# ---------------------------------------------------------------------------
# Application Android : rappel quotidien et widget
# ---------------------------------------------------------------------------
@bp.route("/api/rappel/")
def api_rappel():
    """{a_revoir, qcm, langues, serie, aujourdhui, admin} (jeton d'appareil ou session)."""
    if not current_user.is_authenticated:
        return jsonify(error="connexion requise"), 401
    r = jsonify({**etat_rappel(current_user.id), "admin": bool(current_user.is_admin)})
    r.headers["Cache-Control"] = "no-store"
    return r


# ---------------------------------------------------------------------------
# Notifications web : abonnement de l'appareil, test
# ---------------------------------------------------------------------------
@bp.route("/api/push/cle/")
@login_required
def push_cle():
    from . import push

    abonnements = {a.endpoint: {"heure": a.heure, "minute": a.minute}
                   for a in AbonnementPush.query.filter_by(user_id=current_user.id)}
    return jsonify(cle=push.cle_publique(), abonnements=abonnements)


def _json_post():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


@bp.route("/api/push/abonner/", methods=["POST"])
@login_required
def push_abonner():
    data = _json_post()
    try:
        sub = data["subscription"]
        endpoint, p256dh, auth = str(sub["endpoint"]), str(sub["keys"]["p256dh"]), str(sub["keys"]["auth"])
        heure, minute = int(data.get("heure", 19)), int(data.get("minute", 0))
        if not endpoint.startswith("https://") or len(endpoint) > 1000 or not (0 <= heure <= 23 and 0 <= minute <= 59):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        return jsonify(error="abonnement invalide"), 400
    maintenant = quiz.maintenant()
    # Heure déjà passée aujourd'hui : premier rappel demain (pas tout de suite).
    deja = (heure, minute) <= (maintenant.hour, maintenant.minute)
    a = AbonnementPush.query.filter_by(endpoint=endpoint).first()
    if a is None:
        a = AbonnementPush(endpoint=endpoint)
        db.session.add(a)
    a.user_id, a.p256dh, a.auth, a.heure, a.minute = current_user.id, p256dh[:200], auth[:100], heure, minute
    a.agent = request.headers.get("User-Agent", "")[:300]
    a.dernier_envoi = maintenant.date() if deja else None
    db.session.commit()
    return jsonify(ok=True, heure=heure, minute=minute)


@bp.route("/api/push/desabonner/", methods=["POST"])
@login_required
def push_desabonner():
    data = _json_post() or {}
    AbonnementPush.query.filter_by(user_id=current_user.id, endpoint=str(data.get("endpoint", ""))).delete()
    db.session.commit()
    return jsonify(ok=True)


@bp.route("/api/push/tester/", methods=["POST"])
@login_required
def push_tester():
    from . import push

    data = _json_post() or {}
    abonnement = AbonnementPush.query.filter_by(user_id=current_user.id, endpoint=str(data.get("endpoint", ""))).first()
    if abonnement is None:
        return jsonify(error="appareil non abonné"), 404
    texte = texte_rappel(etat_rappel(current_user.id)) or "Tout est à jour ✓"
    try:
        push.envoyer(abonnement, TITRE + " (test)", texte)
    except push.AbonnementExpire:
        db.session.delete(abonnement)
        db.session.commit()
        return jsonify(error="abonnement expiré : réactive le rappel"), 410
    except Exception as e:  # noqa: BLE001
        current_app.logger.warning("Notification de test impossible : %s", e)
        return jsonify(error=f"envoi impossible : {e}"), 502
    return jsonify(ok=True)

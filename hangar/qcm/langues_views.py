"""Vues du module Langues : liste des langues, menu d'une langue, sessions, correction.

Port de `langues_views.py` de Revision. Comme pour les QCM, l'exercice se déroule dans le
navigateur (static/qcm/quiz_langue.js) : la vue « jouer » fournit la session pré-rendue en
JSON. La correction des réponses tapées (tolérance, différences surlignées) est faite ici,
par langues.corriger, et la révision est enregistrée quand on quitte l'exercice (la touche F
peut entre-temps la passer en « Facile »).
"""
from __future__ import annotations

from urllib.parse import urlencode

from flask import abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..texte import cle_tri
from . import fsrs, langues, quiz, rattachement, services
from .models import Activite, QuizProgress, TempsRevision, _get_or_create
from .services import dossier_de
from .views import _render_html, bp

TAILLE_MAX_SESSION = 200


def _sets(langue, dossier="", fichier="", user_id=None):
    """Fichiers d'une langue vus par le compte (communs et personnels), restreints à un
    dossier ou à un seul fichier (paquet)."""
    sets = services.bibliotheque(user_id).sets_langue(langue)
    if fichier:
        sets = [s for s in sets if s.id == fichier]
    elif dossier:
        sets = [s for s in sets if dossier_de(s) == dossier]
    if not sets:
        abort(404, description="Langue introuvable")
    return sets


def _filtre(args):
    """(dossier, fichier) demandés : un fichier l'emporte sur un dossier."""
    fichier = args.get("fichier", "")
    return ("" if fichier else args.get("dossier", "")), fichier


def _url_menu(langue, dossier="", fichier=""):
    url = url_for("qcm.langue_menu", langue=langue)
    filtre = {"fichier": fichier} if fichier else {"dossier": dossier} if dossier else {}
    return f"{url}?{urlencode(filtre)}" if filtre else url


@bp.route("/qcm/langues/")
@login_required
def langues_index():
    """Toutes les langues : session du jour, dossiers et paquets."""
    uid = current_user.id
    now = quiz.maintenant()
    e = services.etat_page(uid)
    liste = []
    for l in e["langues"]:
        par_dossier: dict = {}
        for f in l["fichiers"]:
            par_dossier.setdefault(dossier_de(f), []).append(f)
        dossiers = [{"nom": d, "sets": fs, "resume": services.resume_langue(uid, l["nom"], fs, now)}
                    for d, fs in sorted(par_dossier.items(), key=lambda t: cle_tri(t[0]))]
        liste.append({**l, "dossiers": dossiers})
    return render_template("qcm/langues.html", langues=liste)


@bp.route("/qcm/langues/<langue>/")
@login_required
def langue_menu(langue):
    dossier, fichier = _filtre(request.args)
    sets = _sets(langue, dossier, fichier, current_user.id)
    now = quiz.maintenant()
    r = langues.resume(sets, QuizProgress.map_langue(current_user.id, sets), now)
    modes = [(cle, nom) for cle, nom in langues.MODES.items()
             if cle not in langues.GENRES or any(g["nom"] == nom for g in r["genres"])]
    return render_template(
        "qcm/langue_menu.html", langue=langue, dossier=dossier, fichier=fichier,
        paquet=sets[0] if fichier else None, sets=sets, resume=r, modes=modes, taille=langues.TAILLE_SESSION,
    )


def _payload(item, st, now):
    """Exercice pré-rendu (HTML) pour le moteur côté navigateur. Les réponses des
    saisies restent sur le serveur, qui corrige."""
    exo, d = item.exo, item.exo.donnees

    def html(texte):
        # Rendu en ligne (Markdown + LaTeX), une ligne de texte à la fois.
        return "<br>".join(_render_html(ligne, block=False) for ligne in texte.split("\n"))

    p = {
        "set_id": item.set.id,
        "exo_id": exo.id,
        "resume": exo.resume,
        "etiquette": exo.etiquette,
        "genre": exo.genre,
        "forme": exo.forme,
        "nouveau": item.nouveau,
        "details": [html(t) for t in exo.details],
    }
    if exo.audio:
        p["audio"] = {**exo.audio, "lang": item.set.code_langue}
    if exo.forme == langues.CARTE:
        p.update(recto=html(d["recto"]), verso=html(d["verso"]),
                 previsions={str(n): t for n, t in langues.previsions(st, now).items()})
        if d.get("oral"):
            # Prononciation : vérifiée sur l'appareil (reconnaissance vocale), puis note FSRS.
            p["oral"] = {"reponses": d["oral"]["reponses"], "particules": item.set.particules, "lang": item.set.code_langue}
    elif exo.forme == langues.SAISIE:
        p.update(consigne=html(d["consigne"]), invite=d["invite"],
                 prerempli=d["prerempli"], tableau=d["tableau"],
                 caracteres=item.set.caracteres_speciaux)
        if d.get("indice"):
            p["indice"] = d["indice"]
    elif exo.forme == langues.CHOIX:
        p.update(enonce=html(d["enonce"]), choix=[html(c) for c in d["choix"]], multiple=d["multiple"])
        if d.get("ecoute"):
            # QCM à l'écoute : le mot est lu, pas écrit (montré seulement sans synthèse vocale).
            p.update(ecoute=True, indice=d["indice"])
    elif exo.forme == langues.ORDRE:
        p.update(consigne=html(d["consigne"]), mots=d["mots"], melange=item.melange)
    return p


@bp.route("/qcm/langues/<langue>/jouer/")
@login_required
def langue_play(langue):
    dossier, fichier = _filtre(request.args)
    sets = _sets(langue, dossier, fichier, current_user.id)
    mode = request.args.get("mode", langues.QUOTIDIEN)
    if mode not in langues.MODES:
        abort(404, description="Mode inconnu")
    n = request.args.get("n", "")
    n = min(int(n), TAILLE_MAX_SESSION) if n.isdigit() and int(n) > 0 else langues.TAILLE_SESSION
    now = quiz.maintenant()
    prog = QuizProgress.map_langue(current_user.id, sets)
    session = langues.construire_session(sets, prog, mode, n, now)
    payload = [_payload(e, prog.get((e.set.id, e.exo.id)), now) for e in session]
    titre = f"{langues.MODES[mode]} · {langue}" + (f" · {sets[0].titre}" if fichier else f" · {dossier}" if dossier else "")
    return render_template(
        "qcm/langue_play.html", langue=langue, titre=titre, mode=mode, exercices=payload, count=len(payload),
        intensif=mode == langues.INTENSIF, back_url=_url_menu(langue, dossier, fichier),
    )


def _exercice(data, user_id=None):
    """(set, exercice) désignés par la requête, ou une réponse d'erreur."""
    library = services.bibliotheque(user_id)
    set_langue = library.langues.get(rattachement.resoudre(library, str(data.get("set_id", "")), user_id))
    exo = set_langue.exercice(str(data.get("exo_id", ""))) if set_langue else None
    if exo is None:
        return None, None, (jsonify(error="exercice inconnu"), 404)
    return set_langue, exo, None


@bp.route("/qcm/langue/corriger/", methods=["POST"])
@login_required
def langue_corriger():
    """Corrige une réponse sans l'enregistrer : verdict, réponse attendue, différences."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="requête invalide"), 400
    set_langue, exo, erreur = _exercice(data, current_user.id)
    if erreur:
        return erreur
    reponse = data.get("reponse")
    if isinstance(reponse, list) and not all(isinstance(i, int) and not isinstance(i, bool) for i in reponse):
        return jsonify(error="requête invalide"), 400
    resultat = langues.corriger(set_langue, exo, reponse)
    if resultat is None:
        return jsonify(error="réponse incomplète"), 400
    if exo.forme == langues.CHOIX:
        resultat["reponses"] = exo.donnees["reponses"]
    if exo.forme == langues.SAISIE:
        resultat["autres"] = [_render_html(a, block=False) for a in resultat["autres"]]
    return jsonify(resultat)


@bp.route("/qcm/langue/reponse/", methods=["POST"])
@login_required
def langue_record():
    """Enregistre une révision (note FSRS 1 à 4) : état FSRS et prochaine échéance."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or data.get("note") not in fsrs.NOTES:
        return jsonify(error="requête invalide"), 400
    set_langue, exo, erreur = _exercice(data, current_user.id)
    if erreur:
        return erreur
    uid = current_user.id
    obj = _get_or_create(QuizProgress, user_id=uid, set_id=set_langue.id, question_id=exo.id)
    nouveau = obj.decouverte is None
    obj.record_langue(data["note"])
    Activite.noter(uid, data["note"] != fsrs.A_REVOIR, nouveau=nouveau)
    TempsRevision.noter(uid, set_langue.langue, data.get("duree_ms"))
    db.session.commit()
    return jsonify(stabilite=obj.stabilite, echeance=obj.echeance.isoformat())


@bp.route("/qcm/langues/<langue>/reinitialiser/", methods=["POST"])
@login_required
def langue_reset(langue):
    fichier = request.form.get("fichier", "")
    dossier = "" if fichier else request.form.get("dossier", "")
    sets = _sets(langue, dossier, fichier, current_user.id)
    QuizProgress.query.filter(QuizProgress.user_id == current_user.id,
                              QuizProgress.set_id.in_([s.id for s in sets])).delete(synchronize_session=False)
    db.session.commit()
    precision = f" ({sets[0].titre})" if fichier else f" ({dossier})" if dossier else ""
    flash(f"Progression en {langue}{precision} réinitialisée.", "success")
    return redirect(_url_menu(langue, dossier, fichier))


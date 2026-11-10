"""QCM hors ligne : application installable (PWA) pour réviser sans connexion. Port de
`hors_ligne_views.py` de Revision (mêmes adresses et mêmes formats JSON : l'application Android de
Revision s'en sert aussi, avec son jeton d'appareil).

L'appareil télécharge des sets (questions pré-rendues + progression FSRS + quotas) et des paquets
de langue (exercices complets : il corrige et planifie lui-même), et les garde dans le navigateur
(IndexedDB, static/qcm/qcm_attente.js). Les réponses données hors ligne sont mises en file
d'attente avec leur date, puis envoyées à `synchro` dès que la connexion revient : elles sont
rejouées dans l'ordre chronologique et rejoignent la progression FSRS du compte.

Un service worker (servi sur /qcm/sw.js pour contrôler tout /qcm/) garde en cache la page, les
scripts, les styles, KaTeX et les images des questions ; il affiche aussi les notifications du
rappel quotidien (rappels.py).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from flask import current_app, jsonify, make_response, render_template, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..icons import ICONS
from . import fsrs, langues, planning, quiz, services
from .langues_views import _payload as _payload_langue
from .models import Activite, QuizDeck, QuizProgress, TempsRevision, _get_or_create
from .services import bibliotheque, dossier_de
from .views import bp, question_payload

MAX_REPONSES = 5000  # par requête de synchronisation
MAX_SETS = 500       # par requête de téléchargement

ICONS.setdefault("smartphone", '<rect x="6.5" y="2.5" width="11" height="19" rx="2.2"/><path d="M10.5 18.5h3"/>')
ICONS.setdefault("bell-ring", '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 8.5 3 8.5H3S6 15 6 8"/><path d="M10.3 20a1.9 1.9 0 0 0 3.4 0"/>'
                              '<path d="M2.5 7.5A9 9 0 0 1 5 3.5M21.5 7.5A9 9 0 0 0 19 3.5"/>')


def _date_iso(d):
    """Date locale naïve → ISO 8601 avec le décalage horaire (lue sans ambiguïté par l'appareil)."""
    return d.astimezone().isoformat() if d else None


def _sans_cache(reponse):
    reponse.headers["Cache-Control"] = "no-store"
    return reponse


@bp.route("/qcm/hors-ligne/")
@login_required
def hors_ligne():
    """Coquille de l'application : tout le reste se passe dans le navigateur."""
    rep = make_response(render_template("qcm/hors_ligne.html"))
    rep.headers["Cache-Control"] = "no-cache"
    return rep


@bp.route("/qcm/hors-ligne/catalogue/")
@login_required
def hl_catalogue():
    """Sets disponibles (sans les questions), jeton CSRF et joueur connecté."""
    from .. import csrf_token

    library = bibliotheque(current_user.id)
    sets = [{"id": s.id, "titre": s.titre, "description": s.description, "categorie": s.categorie,
             "langue": s.langue, "total": len(s), "perso": bool(s.proprietaire)}
            for s in library.sorted_sets()]
    fichiers = [{"id": f.id, "titre": f.titre, "description": f.description, "langue": f.langue,
                 "dossier": dossier_de(f), "total": len(f), "type": "langue", "perso": bool(f.proprietaire)}
                for _, f in sorted(library.langues.items())]
    return _sans_cache(jsonify(joueur=current_user.username, joueur_id=current_user.id, csrf=csrf_token(),
                               sets=sets, langues=fichiers))


def payload_hors_ligne(set_langue, exo):
    """Exercice complet pour l'application hors ligne : affichage pré-rendu comme dans une
    session, plus ce qu'il faut pour corriger et planifier sur l'appareil (réponses, tolérance,
    prérequis, facettes par session)."""
    item = langues.ExoSession(set_langue, exo, False)
    p = _payload_langue(item, None, quiz.maintenant())
    p.pop("previsions", None)
    p.pop("nouveau", None)
    d = exo.donnees
    p.update(entree=exo.entree, prerequis=exo.prerequis, max_par_session=exo.max_par_session, resume=exo.resume)
    if exo.forme in (langues.SAISIE, langues.CHOIX, langues.ORDRE):
        p["reponses"] = d["reponses"]
    if exo.forme == langues.SAISIE:
        p["tolerant"] = d["tolerant"]
    return p


def _fichier_langue(user_id, fichier):
    """Fichier de langue pour l'appareil : exercices complets et état FSRS du compte."""
    prog = QuizProgress.map_langue(user_id, [fichier])
    return {
        "id": fichier.id,
        "type": "langue",
        "langue": fichier.langue,
        "titre": fichier.titre,
        "description": fichier.description,
        "dossier": dossier_de(fichier),
        "code_langue": fichier.code_langue,
        "particules": fichier.particules,
        "caracteres": fichier.caracteres_speciaux,
        "nouveaux_par_jour": fichier.nouveaux_par_jour,
        "exercices": [payload_hors_ligne(fichier, e) for e in fichier.exercices],
        "progression": {
            exo_id: {"stabilite": p.stabilite, "difficulte": p.difficulte, "echeance": _date_iso(p.echeance),
                     "decouverte": _date_iso(p.decouverte), "oublis": p.oublis, "last_review": _date_iso(p.last_review),
                     "last_correct": p.last_correct}
            for (_, exo_id), p in prog.items()
        },
    }


@bp.route("/qcm/hors-ligne/sets/")
@login_required
def hl_sets():
    """Sets demandés (?id=…&id=…) avec questions pré-rendues, progression et quotas."""
    uid = current_user.id
    library = bibliotheque(uid)
    decks = QuizDeck.map_for(uid)
    sets, manquants = [], []
    for set_id in request.args.getlist("id")[:MAX_SETS]:
        fichier = library.langues.get(set_id)
        if fichier is not None:
            sets.append(_fichier_langue(uid, fichier))
            continue
        qset = library.get(set_id)
        if qset is None:
            manquants.append(set_id)
            continue
        progress = QuizProgress.map_for(uid, qset.id)
        deck = decks.get(qset.id)
        sets.append({
            "id": qset.id,
            "titre": qset.titre,
            "description": qset.description,
            "categorie": qset.categorie,
            "langue": qset.langue,
            "questions": [question_payload(qset, q, progress) for q in qset.questions],
            "progression": {
                qid: {"box": p.box, "seen": p.seen, "correct": p.correct,
                      "last_review": _date_iso(p.last_review), "premiere_revue": _date_iso(p.premiere_revue),
                      "stabilite": p.stabilite, "difficulte": p.difficulte, "echeance": _date_iso(p.echeance),
                      "decouverte": _date_iso(p.decouverte), "oublis": p.oublis, "last_correct": p.last_correct}
                for qid, p in progress.items()
            },
            "quotas": {"nouvelles_jour": deck.nouvelles_jour if deck else None,
                       "revisions_jour": deck.revisions_jour if deck else None},
        })
    return _sans_cache(jsonify(joueur=current_user.username, sets=sets, manquants=manquants))


def _date_reponse(valeur, now):
    """Date d'une réponse envoyée par l'appareil (ISO 8601) → heure locale naïve, jamais dans le futur.
    Une date sans fuseau est lue en UTC (toISOString() du navigateur)."""
    quand = datetime.fromisoformat(str(valeur).replace("Z", "+00:00"))
    if quand.tzinfo is None:
        quand = quand.replace(tzinfo=timezone.utc)
    return min(quand.astimezone().replace(tzinfo=None), now)


@bp.route("/qcm/hors-ligne/synchro/", methods=["POST"])
@login_required
def hl_synchro():
    """Rejoue les réponses faites hors ligne, dans l'ordre chronologique.

    Corps : {"reponses": [{"uid", "set_id", "question_id", "correct", "note", "duree_ms", "date"}]}
    (module Langues : "exo_id" et une note FSRS 1 à 4). Une réponse plus ancienne que la dernière
    révision connue de la question est ignorée : soit elle a déjà été reçue (renvoi après une
    coupure), soit la question a été revue depuis sur un autre appareil. Renvoie les uid traités
    (enregistrés ou ignorés), que l'appareil peut retirer de sa file d'attente."""
    data = request.get_json(silent=True, force=True)
    reponses = data.get("reponses") if isinstance(data, dict) else None
    if not isinstance(reponses, list):
        return jsonify(error="requête invalide"), 400
    uid = current_user.id
    library = bibliotheque(uid)
    now = quiz.maintenant()
    valides, traitees = [], []
    for r in reponses[:MAX_REPONSES]:
        if not isinstance(r, dict) or "uid" not in r:
            continue
        traitees.append(str(r["uid"]))
        try:
            quand = _date_reponse(r["date"], now)
            if "exo_id" in r:
                # Module Langues : note FSRS 1 à 4.
                note = int(r["note"])
                if note not in fsrs.NOTES:
                    continue
                set_id, exo_id = str(r["set_id"]), str(r["exo_id"])
                fichier = library.langues.get(set_id)
                if fichier is not None and fichier.exercice(exo_id) is not None:
                    valides.append((quand, set_id, exo_id, note, r.get("duree_ms"), True))
                continue
            reponse = r["note"] if r.get("note") in quiz.NOTES else bool(r["correct"])
            set_id, question_id = str(r["set_id"]), str(r["question_id"])
        except (KeyError, ValueError, TypeError):
            continue
        qset = library.get(set_id)
        if qset is not None and any(q.id == question_id for q in qset.questions):
            valides.append((quand, set_id, question_id, reponse, r.get("duree_ms"), False))

    decks = QuizDeck.map_for(uid)
    enregistrees = 0
    for quand, set_id, question_id, reponse, duree, langue in sorted(valides, key=lambda v: v[0]):
        obj = _get_or_create(QuizProgress, user_id=uid, set_id=set_id, question_id=question_id)
        if obj.last_review and quand <= obj.last_review:
            continue
        if langue:
            nouveau = obj.decouverte is None
            obj.record_langue(reponse, quand)
            Activite.noter(uid, reponse != fsrs.A_REVOIR, nouveau=nouveau, quand=quand)
            TempsRevision.noter(uid, library.langues[set_id].langue, duree, quand)
        else:
            deck = decks.get(set_id)
            obj.record(reponse, quand, retention=planning.retention_cible(deck.date_objectif if deck else None, quand))
            Activite.noter(uid, obj.last_correct, nouveau=obj.seen == 1, quand=quand)
            services.chronometrer(uid, set_id, duree, services.matiere_de(library.get(set_id)), quand)
        db.session.flush()
        enregistrees += 1
    db.session.commit()
    return _sans_cache(jsonify(traitees=traitees, enregistrees=enregistrees))


# ---------------------------------------------------------------------------
# Service worker, manifeste, icônes
# ---------------------------------------------------------------------------
def _fichiers_katex():
    dossier = Path(current_app.static_folder) / "vendor" / "katex" / "fonts"
    return sorted(f.name for f in dossier.glob("*.woff2")) if dossier.is_dir() else []


@bp.route("/qcm/sw.js")
def service_worker():
    """Service worker (sans secret) : servi sous /qcm/ pour contrôler toutes les pages des QCM."""
    rep = make_response(render_template("qcm/sw.js", polices_katex=_fichiers_katex()))
    rep.headers["Content-Type"] = "text/javascript; charset=utf-8"
    rep.headers["Cache-Control"] = "no-cache"
    return rep


@bp.route("/qcm/manifest.webmanifest")
def manifeste():
    """Manifeste de l'application installable (écran d'accueil)."""
    icone = lambda taille: url_for("static", filename=f"qcm/icone-{taille}.png")  # noqa: E731
    data = {
        "name": "QCM Hangar",
        "short_name": "QCM",
        "description": "Réviser les QCM de Hangar, même sans connexion.",
        "start_url": url_for("qcm.hors_ligne"),
        "scope": url_for("qcm.index"),
        "display": "standalone",
        "background_color": "#ffffff",
        "theme_color": "#3f76ff",
        "lang": "fr",
        "icons": [
            {"src": icone(192), "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": icone(512), "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    }
    rep = make_response(json.dumps(data, ensure_ascii=False))
    rep.headers["Content-Type"] = "application/manifest+json; charset=utf-8"
    return rep


@bp.app_template_global("qcm_hors_ligne_url")
def _url_hors_ligne(ids=None):
    """Adresse de l'application hors ligne ; avec `ids`, les sets à télécharger tout de suite.
    Dans l'application Android : sa page hors ligne embarquée."""
    from ..appareils import APPLI_LOCAL, dans_appli
    if dans_appli():
        return APPLI_LOCAL + "index.html"
    url = url_for("qcm.hors_ligne")
    if ids:
        from urllib.parse import quote
        url += "#telecharger=" + ",".join(quote(i, safe="") for i in ids)
    return url

"""QCM : choix des sets, sessions, statistiques, planning, page « Aujourd'hui ».

Port de `quiz_views.py` de Revision. L'interaction du quiz (afficher les questions, feedback
immédiat, clavier) se fait dans le navigateur (static/qcm/quiz.js) : la vue « jouer » fournit
les questions pré-rendues en JSON, et chaque réponse est enregistrée par une petite requête POST.

Les autres pages sont dans gestion.py (import, rangement, export, signalements, « Mon espace »),
langues_views.py et masque_views.py, sur le même blueprint.
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import (Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request,
                   send_file, url_for)
from flask_login import current_user, login_required

from ..extensions import db
from ..icons import ICONS
from ..texte import cle_tri
from . import coffre, masque, planning, quiz, rattachement, rendu, services
from .models import _JAMAIS, Activite, ObjectifRevision, QuizDeck, QuizProgress, Signalement, TempsRevision, _get_or_create
from .services import ancre, bibliotheque, file_du_jour, peut_modifier

bp = Blueprint("qcm", __name__)

CATEGORIE_IMPORT = "Importés"
TAILLE_MAX_IMPORT = 2 * 1024 * 1024  # 2 Mo par fichier
QUOTA_MAX = 9999
EXAMEN_QUESTIONS = 20   # questions d'un examen blanc par défaut
EXAMEN_DUREE_MAX = 180  # minutes
REVISION_MAX = 200      # questions d'une révision intelligente (la page se recharge ensuite)

# Icônes propres au module (sans toucher au fichier partagé icons.py)
ICONS.setdefault("flame", '<path d="M12 3c1 3 4 5 4 9a4 4 0 0 1-8 0c0-2 1-3 2-4 0 2 1 3 2 3 0-3-1-5 0-8Z"/>'
                          '<path d="M6.5 13.5A5.5 5.5 0 0 0 12 21a5.5 5.5 0 0 0 5.5-5.5"/>')
ICONS.setdefault("cards", '<rect x="3" y="6" width="13" height="15" rx="2"/><path d="M8 3h11a2 2 0 0 1 2 2v12"/>')
ICONS.setdefault("languages", '<path d="M4 5h8M8 3v2M5.5 5c.8 3 3 5.5 6 7M10.5 5c-.8 3.5-3 6-6.5 7.5"/>'
                              '<path d="m13 21 4-9 4 9M14.4 18h5.2"/>')
ICONS.setdefault("flag-check", '<path d="M4 21V4M4 4h11l-2 4 2 4H4"/><path d="m15 18 2 2 4-4"/>')


@bp.app_template_filter("qcm_slug")
def _qcm_slug(texte):
    """Ancre d'une catégorie (même calcul que services.ancre)."""
    from .paquets import slugify
    return slugify(str(texte).replace("/", "-"))


@bp.app_context_processor
def _contexte_menu():
    """`qcm_menu()` pour le menu de gauche de la section (calculé seulement s'il est affiché)."""
    return {"qcm_menu": _menu}


def _menu():
    if getattr(request, "_qcm_menu", None) is not None:
        return request._qcm_menu
    try:
        e = services.etat_page(current_user.id)
        cats: dict = {}
        for x in e["sets"]:
            if x["set"].langue:
                continue
            c = cats.setdefault(x["set"].categorie, {"nom": x["set"].categorie, "sets": 0, "dues": 0})
            c["sets"] += 1
            c["dues"] += x["revisions"]
        request._qcm_menu = {
            "dues": e["cartes_dues"], "a_reviser": e["a_reviser"],
            "categories": sorted(cats.values(), key=lambda c: cle_tri(c["nom"])),
            "langues": [{"nom": l["nom"], "dues": l["a_revoir"]} for l in e["langues"]],
            "signalements": Signalement.query.filter_by(traite=False).count() if current_user.is_admin else 0,
            "peut_modifier": peut_modifier(current_user),
        }
    except Exception:  # noqa: BLE001 — le menu ne doit jamais empêcher une page de s'afficher
        current_app.logger.exception("Menu des QCM")
        request._qcm_menu = {"dues": 0, "a_reviser": 0, "categories": [], "langues": [], "signalements": 0,
                       "peut_modifier": False}
    return request._qcm_menu


def _ia():
    """L'assistant IA est-il configuré ? (boutons « Explique-moi », correction des QRC, génération)"""
    try:
        from .. import ai
        return bool(ai.disponible())
    except Exception:  # noqa: BLE001
        return False


def _url_ia(endpoint):
    try:
        return url_for(endpoint) if _ia() else ""
    except Exception:  # noqa: BLE001 — route absente
        return ""


def _render_html(text, block=True):
    """Rend un énoncé/choix (Markdown + LaTeX $...$)."""
    return rendu.rendre(text, block)


def question_payload(qset, question, progress):
    """Question pré-rendue (HTML) + réponses, pour le moteur côté navigateur."""
    st = progress.get(question.id)
    return {
        "set_id": qset.id,
        "set_titre": qset.titre,
        "id": question.id,
        "type": question.type,
        "enonce": _render_html(question.enonce) + (masque.html(qset, question.masque) if question.masque else ""),
        "choix": [_render_html(c, block=False) for c in question.choix],
        "reponses": question.reponses,
        "reponses_texte": question.reponses_texte,
        "consigne": question.consigne,
        "verso": _render_html(question.verso),
        "explication": _render_html(question.explication),
        "box": st.box if st else 0,
        "perso": bool(qset.proprietaire),
    }


# ---------------------------------------------------------------------------
# Accueil des QCM
# ---------------------------------------------------------------------------
@bp.route("/qcm/")
@login_required
def index():
    uid = current_user.id
    now = quiz.maintenant()
    e = services.etat_page(uid)
    library = e["library"]
    q = request.args.get("q", "").strip()
    matches = 0
    groupes: dict = {}
    for x in e["sets"]:
        qset = x["set"]
        deck = QuizDeck.pour(e["decks"], uid, qset.id)
        groupes.setdefault((qset.langue, qset.categorie), []).append({
            "set": qset, "summary": x["summary"], "deck": deck, "ancre": ancre(qset.id),
            "match": not q or qset.correspond(q),
        })
    par_langue: dict = {}
    for (langue, cat), entries in groupes.items():
        shown = sum(en["match"] for en in entries)
        matches += shown
        dates = [en["deck"].date_objectif for en in entries if en["deck"].date_objectif]
        total = sum(en["summary"]["total"] for en in entries)
        par_langue.setdefault(langue, []).append({
            "nom": cat, "langue": langue, "sets": entries, "shown": shown,
            "a_reviser": sum(en["summary"]["a_reviser"] for en in entries),
            "revisions": sum(en["summary"]["revisions"] + en["summary"]["reapprendre"] for en in entries),
            "date_objectif": min(dates) if dates else None,
            "maitrise": round(sum(en["summary"]["pct_maitrise"] * en["summary"]["total"] for en in entries) / total) if total else 0,
            "ancre": "cat-" + ancre(cat)[4:],
        })
    categories = sorted(par_langue.pop("", []), key=lambda c: cle_tri(c["nom"]))
    langues = []
    resumes = {l["nom"]: l for l in e["langues"]}
    for nom in library.noms_langues():
        # Une langue peut n'avoir que des sets de QCM rattachés (clé « langue »), sans fichier de langue.
        l = resumes.get(nom, {"nom": nom, "resume": None, "fichiers": []})
        par_dossier: dict = {}
        for f in l["fichiers"]:
            par_dossier.setdefault(services.dossier_de(f), []).append(f)
        dossiers = []
        for dossier, fs in sorted(par_dossier.items(), key=lambda t: cle_tri(t[0])):
            entries = []
            for f in fs:
                texte = quiz.normaliser_texte(" ".join((f.titre, f.description, dossier, nom)))
                match = not q or all(mot in texte for mot in quiz.normaliser_texte(q).split())
                entries.append({"set": f, "search": texte, "match": match, "ancre": ancre(f.id)})
            shown = sum(en["match"] for en in entries)
            matches += shown
            dossiers.append({"nom": dossier, "langue": nom, "sets": entries, "shown": shown,
                             "resume": services.resume_langue(uid, nom, fs, now)})
        langues.append({"nom": nom, "resume": l["resume"], "dossiers": dossiers, "nb": len(l["fichiers"]),
                        "categories": par_langue.get(nom, []),
                        "noms_dossiers": sorted({d["nom"] for d in dossiers} | {c["nom"] for c in par_langue.get(nom, [])}, key=cle_tri)})
    act = Activite.resume(uid, quiz.jour(now))
    examens = services.objectifs_par_categorie(services.objectifs(uid, library, e["decks"], now, 60, tous=e["tous"]))
    return render_template(
        "qcm/index.html", categories=categories, langues=langues, total_sets=len(library.sets),
        total_paquets=len(library.langues), etat=e, act=act, examens=examens[:4], errors=library.errors, q=q,
        matches=matches, can_edit=peut_modifier(current_user), categories_import=_categories_import(),
        ia=_ia(), orphelins=len(rattachement.orphelins(uid, library, suggestions=False)))


def _shuffled(pairs):
    pairs = list(pairs)
    random.shuffle(pairs)
    return pairs


@bp.route("/qcm/jouer/")
@login_required
def play():
    """Construit une session selon le mode demandé et rend le moteur de quiz."""
    uid = current_user.id
    library = bibliotheque(uid)
    now = quiz.maintenant()
    mode = request.args.get("mode", "all")
    limite = request.args.get("n")
    set_id = request.args.get("set")
    duree_min = total_dispo = None
    back_url = url_for("qcm.index")

    if request.args.get("revision"):
        # Révision intelligente : questions dues de tous les sets, priorisées.
        titre = "Révision intelligente"
        pairs = []
        decks = QuizDeck.map_for(uid)
        tous = QuizProgress.map_all(uid)
        categorie = request.args.get("categorie")
        for qset in library.sorted_sets():
            if categorie and qset.categorie != categorie:
                continue
            progress = tous.get(qset.id, {})
            file, _ = file_du_jour(qset, progress, decks.get(qset.id), now)
            for q in file:
                st = progress.get(q.id)
                # Les révisions dues d'abord (les plus menacées d'oubli en tête), puis les nouvelles :
                # avec une session limitée à REVISION_MAX questions, aucune révision due n'est laissée de côté.
                prio = st.priority(now) if st else (0, 2, _JAMAIS)
                pairs.append((prio, qset, q, progress))
        pairs.sort(key=lambda p: p[0])
        selection = [(qset, q, progress) for _, qset, q, progress in pairs]
        if not (limite and limite.isdigit()):
            limite = str(REVISION_MAX)
        if categorie:
            titre = f"Révision intelligente · {categorie}"
        back_url = url_for("qcm.aujourdhui")
    elif mode == "intensif" and request.args.get("categorie"):
        # Révision intensive de toute une catégorie : toutes ses questions.
        categorie = request.args["categorie"]
        langue = request.args.get("langue", "")
        sets = [s for s in library.categories().get(categorie, []) if s.langue == langue]
        if not sets:
            abort(404, description="Catégorie de QCM introuvable")
        titre = f"Révision intensive · {categorie}"
        selection = []
        for qset in sets:
            progress = QuizProgress.map_for(uid, qset.id)
            selection += [(qset, q, progress) for q in qset.questions]
        selection = _shuffled(selection)
    elif mode == "examen":
        # Examen blanc : n questions tirées au sort (pas de flashcards : elles
        # s'auto-évaluent), chronométrées, corrigées à la fin.
        if request.args.get("categorie"):
            categorie = request.args["categorie"]
            langue = request.args.get("langue", "")
            sets = [s for s in library.categories().get(categorie, []) if s.langue == langue]
            if not sets:
                abort(404, description="Catégorie de QCM introuvable")
            titre = f"Examen blanc · {categorie}"
        else:
            qset = library.get(set_id)
            if qset is None:
                abort(404, description="Set de QCM introuvable")
            sets, titre = [qset], f"Examen blanc · {qset.titre}"
        selection = []
        for qset in sets:
            progress = QuizProgress.map_for(uid, qset.id)
            selection += [(qset, q, progress) for q in qset.questions if not q.est_flashcard]
        selection = _shuffled(selection)
        total_dispo = len(selection)
        n = int(limite) if limite and limite.isdigit() else EXAMEN_QUESTIONS
        n = max(1, min(n, total_dispo)) if total_dispo else 0
        selection, limite = selection[:n], None
        duree = request.args.get("duree", "")
        duree_min = max(1, min(int(duree) if duree.isdigit() else n, EXAMEN_DUREE_MAX))
    else:
        qset = library.get(set_id)
        if qset is None:
            abort(404, description="Set de QCM introuvable")
        titre = qset.titre
        progress = QuizProgress.map_for(uid, qset.id)
        questions = list(qset.questions)
        if mode == "review":
            deck = QuizDeck.query.filter_by(user_id=uid, set_id=qset.id).first()
            questions, _ = file_du_jour(qset, progress, deck, now)
        elif mode == "intensif":
            titre = f"Révision intensive · {qset.titre}"
        selection = _shuffled((qset, q, progress) for q in questions)
        back_url = url_for("qcm.index") + "#" + ancre(qset.id)

    reste = 0
    if limite and limite.isdigit():
        reste = max(0, len(selection) - int(limite))
        selection = selection[:int(limite)]

    payload = [question_payload(qset, q, progress) for qset, q, progress in selection]
    examen = mode == "examen"
    return render_template(
        "qcm/play.html", titre=titre, questions=payload, count=len(payload), reste=reste,
        intensif=mode == "intensif" and not request.args.get("revision"), examen=examen,
        expliquer_url=_url_ia("ai.expliquer"), corriger_qrc_url=_url_ia("ai.corriger_qrc"),
        back_url=back_url, duree_min=duree_min, duree_s=(duree_min or 0) * 60, total_dispo=total_dispo,
        examen_params={k: request.args[k] for k in ("set", "categorie", "langue") if request.args.get(k)},
    )


def _date_reponse(valeur, now):
    """Date d'une réponse gardée sur l'appareil puis envoyée plus tard (30 jours au plus)."""
    if not valeur:
        return None
    try:
        d = datetime.fromisoformat(str(valeur).replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is not None:
        d = d.astimezone().replace(tzinfo=None)
    return d if now - timedelta(days=30) <= d <= now else None


@bp.route("/qcm/reponse/", methods=["POST"])
@login_required
def record():
    """Enregistre le résultat d'une question (état FSRS, boîte de Leitner, activité, temps)."""
    data = request.get_json(silent=True) or {}
    try:
        set_id = str(data["set_id"])
        question_id = str(data["question_id"])
        # Flashcards : note d'auto-évaluation ; autres types : juste/faux.
        reponse = data["note"] if data.get("note") in quiz.NOTES else bool(data["correct"])
    except (KeyError, ValueError, TypeError):
        return jsonify(error="requête invalide"), 400
    uid = current_user.id
    library = bibliotheque(uid)
    qset = library.get(rattachement.resoudre(library, set_id, uid))  # set renommé depuis l'affichage de la page
    if qset is None or not any(q.id == question_id for q in qset.questions):
        return jsonify(error="question inconnue"), 404
    set_id = qset.id
    now = quiz.maintenant()
    quand = _date_reponse(data.get("date"), now)
    obj = _get_or_create(QuizProgress, user_id=uid, set_id=set_id, question_id=question_id)
    deck = QuizDeck.query.filter_by(user_id=uid, set_id=set_id).first()
    obj.record(reponse, quand=quand, retention=planning.retention_cible(deck.date_objectif if deck else None, quand or now))
    Activite.noter(uid, obj.last_correct, nouveau=obj.seen == 1, quand=quand)
    services.chronometrer(uid, set_id, data.get("duree_ms"), services.matiere_de(qset), quand)
    db.session.commit()
    return jsonify(box=obj.box, seen=obj.seen, correct=obj.correct, stabilite=obj.stabilite,
                   echeance=obj.echeance.isoformat())


# ---------------------------------------------------------------------------
# Statistiques
# ---------------------------------------------------------------------------
@bp.route("/qcm/stats/")
@login_required
def stats():
    uid = current_user.id
    now = quiz.maintenant()
    e = services.etat_page(uid)
    library = e["library"]
    categories: dict = {}
    for x in e["sets"]:
        categories.setdefault(x["set"].categorie, []).append({"set": x["set"], "summary": x["summary"]})
    aujourdhui = quiz.jour(now)
    prev = services.previsions(uid, library, now, tous=e["tous"])
    semaines, temps, temps_totaux = TempsRevision.tableau(uid, aujourdhui)
    calendrier = Activite.calendrier(uid, aujourdhui)
    temps_max = max([l["total"] for l in temps] + [1])
    # Progressions restées sans fichier (set renommé ou supprimé hors de Hangar) : « Rattacher à… ».
    can_edit = peut_modifier(current_user)
    orphelins = rattachement.orphelins(uid, library, tous=e["tous"])
    choix = [(el.id, f"{rattachement.libelle(el.id)} · {el.titre}")
             for el in sorted((*library.sets.values(), *library.langues.values()), key=lambda el: cle_tri(el.id))
             ] if orphelins else []
    return render_template(
        "qcm/stats.html", categories=sorted(categories.items(), key=lambda t: cle_tri(t[0])),
        boites=range(quiz.BOITE_MAX + 1), ia=_ia(), can_edit=can_edit,
        previsions=prev, previsions_max=max([p["n"] for p in prev] + [1]),
        previsions_total=sum(p["n"] for p in prev), semaines=semaines, temps=temps, temps_totaux=temps_totaux,
        temps_max=temps_max, activite=Activite.resume(uid, aujourdhui), calendrier=calendrier,
        mois=services.mois_du_calendrier(calendrier), etat=e, orphelins=orphelins, choix_rattachement=choix,
    )


@bp.route("/qcm/reinitialiser/", methods=["POST"])
@login_required
def reset():
    set_id = request.form.get("set", "")
    if bibliotheque(current_user.id).get(set_id) is None:
        abort(404, description="Set inconnu")
    QuizProgress.query.filter_by(user_id=current_user.id, set_id=set_id).delete()
    db.session.commit()
    flash("Progression du set réinitialisée.", "success")
    return redirect(request.form.get("next") or url_for("qcm.stats"))


# ---------------------------------------------------------------------------
# Aujourd'hui
# ---------------------------------------------------------------------------
@bp.route("/qcm/aujourdhui/", methods=["GET", "POST"])
@login_required
def aujourdhui():
    """Tout ce qui est dû, les examens à venir, la série, l'objectif du jour."""
    uid = current_user.id
    if request.method == "POST":
        valeur = request.form.get("objectif", "").strip()
        objectif = int(valeur) if valeur.isdigit() and 0 < int(valeur) <= 2000 else None
        o = _get_or_create(ObjectifRevision, user_id=uid)
        o.reponses_jour = objectif
        db.session.commit()
        flash("Objectif du jour enregistré." if objectif else "Objectif du jour retiré.", "success")
        return redirect(url_for("qcm.aujourdhui"))
    now = quiz.maintenant()
    jour = quiz.jour(now)
    e = services.etat_page(uid)
    sets_dus = sorted((x for x in e["sets"] if x["summary"]["a_reviser"]),
                      key=lambda x: (-x["revisions"], -x["summary"]["a_reviser"], cle_tri(x["set"].titre)))
    examens = services.objectifs(uid, e["library"], e["decks"], now, tous=e["tous"])
    activite = Activite.resume(uid, jour)
    objectif = ObjectifRevision.de(uid)
    prev = services.previsions(uid, e["library"], now, 7, tous=e["tous"])
    return render_template(
        "qcm/aujourdhui.html", jour=jour, etat=e, sets_dus=sets_dus[:10], autres_sets=max(0, len(sets_dus) - 10),
        langues=e["langues"], examens=examens, activite=activite, objectif=objectif,
        pct_objectif=min(100, round(activite["aujourdhui"] / objectif * 100)) if objectif else 0,
        minutes=TempsRevision.minutes_du_jour(uid, jour), previsions=prev,
        previsions_max=max([p["n"] for p in prev] + [1]), revision_max=REVISION_MAX,
    )


# ---------------------------------------------------------------------------
# Planning : quotas journaliers et dates d'examen
# ---------------------------------------------------------------------------
@bp.route("/qcm/planning/", methods=["GET", "POST"])
@login_required
def planning_page():
    """Planning de tous les sets (GET) ; enregistre les quotas et la date d'examen d'un set ou
    la date d'examen d'une catégorie (POST)."""
    uid = current_user.id
    library = bibliotheque(uid)
    retour = request.form.get("next") or url_for("qcm.planning_page")
    if request.method == "POST":
        if request.form.get("categorie") is not None and not request.form.get("set"):
            return _plan_categorie(library, uid, retour)
        set_id = request.form.get("set", "")
        if library.get(set_id) is None:
            abort(404, description="Set inconnu")
        retour = retour.split("#")[0] + "#" + ancre(set_id)
        quotas = {}
        for champ, nom in (("nouvelles_jour", "nouvelles questions"), ("revisions_jour", "révisions")):
            valeur = request.form.get(champ, "").strip()
            try:
                quotas[champ] = int(valeur) if valeur else None
                if valeur and not 1 <= quotas[champ] <= QUOTA_MAX:
                    raise ValueError
            except ValueError:
                flash(f"Nombre de {nom} par jour invalide : entre 1 et {QUOTA_MAX}, ou vide.", "error")
                return redirect(retour)
        objectif = request.form.get("date_objectif", "").strip()
        try:
            objectif = date.fromisoformat(objectif) if objectif else None
        except ValueError:
            flash("Date d'examen invalide.", "error")
            return redirect(retour)
        deck = _get_or_create(QuizDeck, user_id=uid, set_id=set_id)
        deck.nouvelles_jour, deck.revisions_jour = quotas["nouvelles_jour"], quotas["revisions_jour"]
        deck.date_objectif = objectif
        deck.auto_quota = request.form.get("auto") == "1" and objectif is not None
        db.session.commit()
        flash("Planning enregistré.", "success")
        return redirect(retour)
    now = quiz.maintenant()
    e = services.etat_page(uid)
    tous = e["tous"]  # progression déjà lue par etat()
    estimations = {}
    categories: dict = {}
    for x in e["sets"]:
        deck = QuizDeck.pour(e["decks"], uid, x["set"].id)
        est = planning.estimer(x["set"], tous.get(x["set"].id, {}), deck, now) if deck.configure else None
        if est is not None:
            estimations[x["set"].id] = est
        cat = categories.setdefault((x["set"].langue, x["set"].categorie), {
            "nom": x["set"].categorie, "langue": x["set"].langue, "sets": [], "dates": set()})
        cat["sets"].append({"set": x["set"], "summary": x["summary"], "deck": deck, "e": est, "ancre": ancre(x["set"].id)})
        if deck.date_objectif:
            cat["dates"].add(deck.date_objectif)
    cats = []
    for c in sorted(categories.values(), key=lambda c: (cle_tri(c["langue"]), cle_tri(c["nom"]))):
        c["date_objectif"] = min(c["dates"]) if c["dates"] else None
        c["configures"] = sum(1 for s in c["sets"] if s["deck"].configure)
        c["ancre"] = "cat-" + ancre(c["nom"])[4:]
        cats.append(c)
    # Les sets datés ont déjà leur estimation (boucle ci-dessus) : pas de seconde simulation.
    examens = services.objectifs_par_categorie(services.objectifs(uid, e["library"], e["decks"], now, tous=tous,
                                                                  estimations=estimations))
    return render_template("qcm/planning.html", categories=cats, examens=examens, today=quiz.jour(now))


def _plan_categorie(library, uid, retour):
    """Date d'examen appliquée à tous les sets d'une catégorie."""
    categorie, langue = request.form.get("categorie", ""), request.form.get("langue", "")
    sets = [s for s in library.categories().get(categorie, []) if s.langue == langue]
    if not sets:
        abort(404, description="Catégorie inconnue")
    objectif = request.form.get("date_objectif", "").strip()
    try:
        objectif = date.fromisoformat(objectif) if objectif else None
    except ValueError:
        flash("Date d'examen invalide.", "error")
        return redirect(retour)
    for s in sets:
        deck = _get_or_create(QuizDeck, user_id=uid, set_id=s.id)
        deck.date_objectif = objectif
        if objectif is None:
            deck.auto_quota = False
    db.session.commit()
    combien = "le set" if len(sets) == 1 else f"les {len(sets)} sets"
    flash(f"Date d'examen {'fixée au ' + objectif.strftime('%d/%m/%Y') if objectif else 'retirée'} "
          f"pour {combien} de « {categorie} ».", "success")
    return redirect(retour)


def _categories_import():
    """Sous-dossiers de QCM/ du coffre, proposés comme catégories à l'import."""
    racine = coffre.racine() / coffre.DOSSIER_QCM
    if not racine.is_dir():
        return []
    return sorted((d.name for d in racine.iterdir() if d.is_dir() and not d.name.startswith(".")), key=cle_tri)


# ---------------------------------------------------------------------------
# Fichiers du coffre (images des énoncés et des cartes à image masquée)
# ---------------------------------------------------------------------------
@bp.route("/qcm/fichiers/<path:path>")
@login_required
def fichier(path):
    """Média du coffre cité par une question (images, sons, vidéos) : jamais exécuté (SVG compris)."""
    if Path(path).suffix.lower() not in coffre.MEDIA_EXTS:
        abort(404)
    chemin = coffre.chemin_sur(path)
    if chemin is None or not chemin.is_file():
        abort(404)
    rep = send_file(chemin, conditional=True, max_age=3600)
    rep.headers["Cache-Control"] = "private, max-age=3600"
    rep.headers["X-Content-Type-Options"] = "nosniff"
    if chemin.suffix.lower() == ".svg":
        rep.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'"
    return rep


# Les autres pages du module, sur le même blueprint.
from . import gestion, hors_ligne, langues_views, masque_views, rappels  # noqa: E402,F401

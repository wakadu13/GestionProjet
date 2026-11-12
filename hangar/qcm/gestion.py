"""Gestion des sets de QCM et des paquets de langue : import (JSON, Anki, listes de mots),
rangement (déplacer, supprimer), export Anki, signalements de questions, et « Mon espace »
(sets et listes de mots de chaque compte, visibles de lui seul).

Port de `qcm_gestion_views.py` de Revision.
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlencode

from flask import Response, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..texte import cle_tri
from . import anki, coffre, langues, paquets, quiz, rattachement
from .coffre import DOSSIER_QCM, clean_key, invalider
from .models import AliasSet, EmpreinteSet, QuizDeck, QuizProgress, Signalement
from .services import ancre, bibliotheque, peut_modifier
from .views import CATEGORIE_IMPORT, TAILLE_MAX_IMPORT, _categories_import, _ia, _render_html, bp, question_payload

MAX_FICHIERS_PERSO = 200
MAX_OCTETS_PERSO = 20 * 2**20
CATEGORIE_PERSO = "Mes sets"
NOM_INVALIDE = 'un seul nom de dossier, sans / \\ : * ? " < > | # ^ [ ].'


def _exiger_modification():
    if not peut_modifier(current_user):
        abort(403, description="Les invités ne peuvent pas modifier les QCM du coffre.")


def _importer_fichier(fichier, categorie, remplacer, racine=None, prefixe=DOSSIER_QCM, lieu=None, texte=None):
    """Valide puis enregistre un set déposé, dans le coffre (``racine`` None :
    QCM/<catégorie>/) ou dans un espace personnel. Renvoie (succès, message).

    ``fichier`` : FileStorage (ou None avec ``texte``, contenu JSON déjà prêt)."""
    nom = PurePosixPath(str(getattr(fichier, "filename", "") or "set.json").replace("\\", "/")).name
    if not nom.lower().endswith(".json"):
        return False, f"{nom} : seuls les fichiers .json sont acceptés."
    stem = clean_key(nom[:-5])
    if stem is None or "/" in stem:
        return False, f"{nom} : nom de fichier invalide."
    if texte is None:
        brut = fichier.read(TAILLE_MAX_IMPORT + 1)
        if len(brut) > TAILLE_MAX_IMPORT:
            return False, f"{nom} : fichier trop gros (2 Mo maximum)."
        try:
            texte = brut.decode("utf-8-sig")
        except UnicodeDecodeError:
            return False, f"{nom} : le fichier doit être encodé en UTF-8."

    rel = PurePosixPath(*([prefixe] if prefixe else []), categorie, f"{stem}.json")
    # Validation avec le chargeur des sets, dans un dossier temporaire.
    with tempfile.TemporaryDirectory() as tmp:
        essai = Path(tmp, rel)
        essai.parent.mkdir(parents=True)
        essai.write_text(texte, encoding="utf-8")
        try:
            qset = quiz.charger(essai, tmp)
        except Exception as e:  # noqa: BLE001 — fichier mal formé : message, pas d'erreur 500
            return False, quiz.erreur_chargement(rel, e)

    try:
        destination = Path(racine or coffre.racine_ecriture(), rel)
    except ValueError as e:  # coffre introuvable : rien n'est créé à sa place
        return False, str(e)
    if destination.exists() and not remplacer:
        return False, (f"{rel} existe déjà : coche « Remplacer les sets existants » pour l'écraser "
                       "(la progression est conservée pour les questions dont l'id ne change pas).")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(texte, encoding="utf-8", newline="\n")
    invalider()
    n = len(qset)
    unite = "question" if isinstance(qset, quiz.QuestionSet) else "exercice"
    return True, f"« {qset.titre} » importé dans {lieu or rel} ({n} {unite}{'s' if n > 1 else ''})."


def _importer_apkg(fichier, categorie, remplacer, racine=None, prefixe=DOSSIER_QCM, lieu=None):
    """Paquet Anki (.apkg) → set de cartes enregistré comme un import JSON."""
    nom = Path(fichier.filename or "paquet.apkg").name
    try:
        set_json = anki.lire_apkg(fichier.read(anki.TAILLE_MAX_APKG + 1), nom)
    except anki.ImportAnkiImpossible as e:
        return False, str(e)
    fichier.filename = (paquets.slug(set_json["titre"]) or Path(nom).stem or "anki") + ".json"
    return _importer_fichier(fichier, categorie, remplacer, racine, prefixe, lieu,
                             texte=json.dumps(set_json, ensure_ascii=False, indent=2) + "\n")


def _importer_un(fichier, categorie, remplacer, **kw):
    if (fichier.filename or "").lower().endswith(".apkg"):
        return _importer_apkg(fichier, categorie, remplacer, **kw)
    return _importer_fichier(fichier, categorie, remplacer, **kw)


@bp.route("/qcm/importer/", methods=["GET", "POST"])
@login_required
def importer():
    """Import de sets (JSON, Anki .apkg) et de listes de mots dans le coffre (admins et membres)."""
    _exiger_modification()
    if request.method == "POST":
        if request.form.get("action") == "liste":
            paquet, options, ignorees, erreur = paquets.lire_liste(request.form, request.files.get("fichier"))
            if erreur:
                flash(erreur, "error")
                return redirect(url_for("qcm.importer") + "#liste")
            try:
                rel = paquets.ecrire_paquet_langue(options["dossier"] or paquets.DOSSIER_VOCABULAIRE, paquet)
            except paquets.PaquetInvalide as e:
                flash(str(e), "error")
                return redirect(url_for("qcm.importer") + "#liste")
            flash(paquets.message_paquet(paquet, ignorees), "success")
            return redirect(url_for("qcm.langue_menu", langue=options["langue"]) + "?"
                            + urlencode({"fichier": rel.removesuffix(".json")}))
        categorie = request.form.get("categorie", "").strip() or CATEGORIE_IMPORT
        cle = clean_key(categorie)
        fichiers = [f for f in request.files.getlist("fichiers") if f and f.filename]
        if cle is None or "/" in cle:
            flash("Catégorie invalide : " + NOM_INVALIDE, "error")
        elif not fichiers:
            flash("Aucun fichier reçu.", "error")
        else:
            remplacer = request.form.get("remplacer") == "1"
            for f in fichiers:
                ok, message = _importer_un(f, cle, remplacer)
                flash(message, "success" if ok else "error")
        return redirect(request.form.get("next") or url_for("qcm.importer"))
    library = bibliotheque(current_user.id)
    return render_template(
        "qcm/importer.html", categories_import=_categories_import(),
        langues=sorted(set(library.noms_langues()) | {"Anglais"}, key=cle_tri),
        dossiers_langue=sorted({s.path.parent.name for s in library.langues.values() if not s.proprietaire}
                               | {paquets.DOSSIER_VOCABULAIRE}, key=cle_tri),
        cartes=_cartes(), ia=_ia(), pages=_pages_wiki() if _ia() else [],
    )


def _cartes():
    return [(t, langues.nom_type(t, "Anglais"), t in paquets.CARTES_PAR_DEFAUT) for t in langues.TYPES_VOCABULAIRE]


def _pages_wiki():
    """Pages du coffre proposées à la génération de QCM par l'IA (clé, titre)."""
    out = []
    for rel, _m, _s in coffre.signature(coffre.racine()):
        if rel.lower().endswith(".md") and not rel.startswith(DOSSIER_QCM + "/"):
            out.append((rel[:-3], PurePosixPath(rel).stem))
    return sorted(out, key=lambda p: cle_tri(p[0]))[:3000]


# ---------------------------------------------------------------------------
# Rangement : déplacer un set dans un dossier, supprimer un set ou un dossier
# ---------------------------------------------------------------------------
def _element(library, set_id):
    """Set de QCM ou fichier de langue désigné par son identifiant."""
    return library.get(set_id) or library.langues.get(set_id)


def _dossier(element):
    return element.path.parent.name or quiz.CATEGORIE_DEFAUT


def _retirer_dossiers_vides(dossier, racine):
    """Supprime les dossiers devenus vides, en remontant jusqu'à QCM/ (exclu)."""
    arret = {racine, racine / DOSSIER_QCM}
    while dossier not in arret and racine in dossier.parents and dossier.is_dir() and not any(dossier.iterdir()):
        dossier.rmdir()
        dossier = dossier.parent


def _oublier(set_ids):
    """Efface la progression et les réglages de sets (pour tous les comptes), et de quoi y rattacher
    des réponses envoyées plus tard (empreinte, anciens noms) : elles seront abandonnées."""
    QuizProgress.query.filter(QuizProgress.set_id.in_(set_ids)).delete(synchronize_session=False)
    QuizDeck.query.filter(QuizDeck.set_id.in_(set_ids)).delete(synchronize_session=False)
    EmpreinteSet.query.filter(EmpreinteSet.set_id.in_(set_ids)).delete(synchronize_session=False)
    AliasSet.query.filter(AliasSet.nouveau.in_(set_ids)).delete(synchronize_session=False)


def _retour(defaut):
    nxt = request.form.get("next") or ""
    return nxt if nxt.startswith("/") and not nxt.startswith("//") else defaut


@bp.route("/qcm/deplacer/", methods=["POST"])
@login_required
def deplacer():
    """Range un set (QCM ou fichier de langue) dans un dossier, créé au besoin.

    Les sets d'une langue vont dans QCM/<langue>/<dossier>, les autres dans
    QCM/<dossier>. La progression suit le set (son identifiant change), avec les explications de
    l'IA. Celle restée sous le nouveau nom sans fichier n'est plus effacée : fusionnée question par
    question si c'est celle de ce set (renommé autrefois hors de Hangar), mise à part sinon (un
    autre set, supprimé depuis : toujours listée sur la page des statistiques)."""
    _exiger_modification()
    base = quiz.get_library()
    rattachement.verifier(base)  # renommages faits hors de Hangar d'abord
    library = base.commune()
    element = _element(library, request.form.get("set", ""))
    if element is None:
        abort(404, description="Set inconnu")
    retour = _retour(url_for("qcm.index"))
    dossier = clean_key(request.form.get("dossier", ""))
    if dossier is None or "/" in dossier:
        flash("Dossier invalide : " + NOM_INVALIDE, "error")
        return redirect(retour.split("#")[0] + "#" + ancre(element.id))

    langue = clean_key(element.langue) if element.langue else None
    parties = [DOSSIER_QCM] + ([langue] if langue and "/" not in langue and langue != dossier else []) + [dossier]
    rel = PurePosixPath(*parties, element.path.name)
    try:
        racine = coffre.racine_ecriture()
    except ValueError as e:  # coffre introuvable
        flash(str(e), "error")
        return redirect(retour.split("#")[0] + "#" + ancre(element.id))
    if rel == element.path:
        flash(f"« {element.titre} » est déjà dans le dossier {dossier}.", "info")
        return redirect(retour.split("#")[0] + "#" + ancre(element.id))
    if (racine / rel).exists():
        flash(f"{rel} existe déjà : renomme ou supprime d'abord ce fichier.", "error")
        return redirect(retour.split("#")[0] + "#" + ancre(element.id))

    source = racine / element.path
    (racine / rel).parent.mkdir(parents=True, exist_ok=True)
    shutil.move(source, racine / rel)
    _retirer_dossiers_vides(source.parent, racine)
    invalider()
    nouvel_id = rel.with_suffix("").as_posix()
    a_part = None
    if rattachement.a_des_restes(db.session, nouvel_id) and not rattachement.meme_set(db.session, nouvel_id, element):
        a_part = rattachement.mettre_a_part(db.session, nouvel_id)
    rattachement.rattacher(db.session, element.id, nouvel_id)
    db.session.commit()
    flash(f"« {element.titre} » rangé dans le dossier {dossier} ({rel}).", "success")
    if a_part:
        flash(f"Ce dossier gardait la progression d’un autre set « {rel.stem} », supprimé depuis : elle est "
              f"gardée à part sous « {PurePosixPath(a_part).name} ». Retrouve-la dans les statistiques "
              "pour la rattacher au bon set ou l’oublier.", "info")
    return redirect(retour.split("#")[0] + "#" + ancre(nouvel_id))


@bp.route("/qcm/supprimer/", methods=["POST"])
@login_required
def supprimer():
    """Supprime un set, ou tout un dossier (catégorie) d'une langue ou des cours,
    avec la progression associée."""
    _exiger_modification()
    library = quiz.get_library().commune()
    set_id = request.form.get("set", "")
    if set_id:
        element = _element(library, set_id)
        cibles = [element] if element else []
        libelle = f"« {element.titre} »" if element else ""
    else:
        categorie = request.form.get("categorie", "")
        langue = request.form.get("langue", "")
        cibles = [e for e in (*library.sets.values(), *library.langues.values())
                  if e.langue == langue and _dossier(e) == categorie]
        libelle = f"Le dossier {categorie} ({len(cibles)} set{'s' if len(cibles) > 1 else ''})"
    if not cibles:
        abort(404, description="Set inconnu")

    racine = coffre.racine()
    for element in cibles:
        chemin = racine / element.path
        chemin.unlink(missing_ok=True)
        _retirer_dossiers_vides(chemin.parent, racine)
    invalider()
    _oublier([e.id for e in cibles])
    db.session.commit()
    flash(f"{libelle} supprimé.", "success")
    return redirect(_retour(url_for("qcm.index")).split("#")[0])


@bp.route("/qcm/progression-orpheline/", methods=["POST"])
@login_required
def progression_orpheline():
    """Progression restée sans fichier (set renommé, déplacé ou supprimé hors de Hangar, pas
    reconnu tout seul) : « rattacher » à un set du coffre, ou « oublier » (sa propre progression).

    Le rattachement vaut pour tous les comptes (avec l'alias, pour les réponses hors ligne) quand
    les deux sets sont dans le même espace et que le compte peut le ranger : sets communs pour les
    admins et membres, son espace personnel pour chacun. Sinon, seule sa propre progression suit."""
    uid = current_user.id
    library = bibliotheque(uid)
    ancien = request.form.get("ancien", "")
    retour = _retour(url_for("qcm.stats")).split("#")[0] + "#orphelins"
    if not ancien or _element(library, ancien) is not None or ancien in library.ids_invalides:
        abort(404, description="Progression inconnue")
    action = request.form.get("action", "")
    if action == "oublier":
        QuizProgress.query.filter_by(user_id=uid, set_id=ancien).delete(synchronize_session=False)
        QuizDeck.query.filter_by(user_id=uid, set_id=ancien).delete(synchronize_session=False)
        db.session.commit()
        flash(f"Progression de « {rattachement.libelle(ancien)} » oubliée.", "success")
        return redirect(retour)
    if action != "rattacher":
        abort(400)
    cible = _element(library, request.form.get("nouveau", ""))
    if cible is None:
        abort(404, description="Set inconnu")
    pour_tous = rattachement.rattachement_global(ancien, cible.id, uid, peut_modifier(current_user))
    rattachement.rattacher(db.session, ancien, cible.id, user_id=None if pour_tous else uid)
    db.session.commit()
    flash(f"Progression de « {rattachement.libelle(ancien)} » rattachée à « {cible.titre} »"
          + ("." if pour_tous else " (la tienne seulement)."), "success")
    return redirect(retour)


# ---------------------------------------------------------------------------
# Export Anki / texte tabulé / JSON
# ---------------------------------------------------------------------------
@bp.route("/qcm/exporter/")
@login_required
def exporter():
    """Un set de QCM ou un paquet de langue en .apkg (Anki), en .tsv (Quizlet, tableur) ou en .json."""
    library = bibliotheque(current_user.id)
    set_id = request.args.get("set", "")
    element = library.get(set_id) or library.langues.get(set_id)
    if element is None:
        abort(404)
    nom = paquets.slugify(element.titre) or "export"
    fmt = request.args.get("format", "apkg")
    if fmt == "json":
        return Response(element.fichier.read_bytes(), mimetype="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{nom}.json"'})
    donnees = None
    if not isinstance(element, quiz.QuestionSet):
        _, donnees = quiz.lire_json(element.fichier, element.fichier.parent)
    titre, notes = anki.exporter(element, donnees)
    if fmt == "tsv":
        return Response(anki.tsv(notes), mimetype="text/tab-separated-values",
                        headers={"Content-Disposition": f'attachment; filename="{nom}.tsv"'})
    contenu = anki.apkg(set_id, titre, element.description, notes)
    return Response(contenu, mimetype="application/apkg",
                    headers={"Content-Disposition": f'attachment; filename="{nom}.apkg"'})


# ---------------------------------------------------------------------------
# Questions signalées
# ---------------------------------------------------------------------------
@bp.route("/qcm/signaler/", methods=["POST"])
@login_required
def signaler():
    """Un compte signale une question (erreur, ambiguïté) ; un signalement ouvert par
    compte et par question, le commentaire remplace le précédent."""
    data = request.get_json(silent=True) or {}
    try:
        set_id, question_id = str(data["set_id"]), str(data["question_id"])
        commentaire = str(data.get("commentaire") or "").strip()[:2000]
    except (KeyError, ValueError, TypeError):
        return jsonify(error="requête invalide"), 400
    qset = bibliotheque(current_user.id).get(set_id)
    if qset is None or not any(q.id == question_id for q in qset.questions):
        return jsonify(error="question inconnue"), 404
    if qset.proprietaire:
        return jsonify(error="set personnel : corrige-le dans « Mon espace »"), 400
    s = Signalement.query.filter_by(set_id=set_id, question_id=question_id, user_id=current_user.id,
                                    traite=False).first()
    cree = s is None
    if cree:
        s = Signalement(set_id=set_id, question_id=question_id, user_id=current_user.id, commentaire=commentaire,
                        traite=False)
        db.session.add(s)
    elif commentaire:
        s.commentaire = commentaire
    db.session.commit()
    return jsonify(ok=True, nouveau=cree)


def _ligne_signalement(library, s):
    qset = library.get(s.set_id)
    question = next((q for q in qset.questions if q.id == s.question_id), None) if qset else None
    ligne = {"s": s, "set": qset, "question": question, "html": None, "proposition": None}
    if question is not None:
        ligne["html"] = question_payload(qset, question, {})
    if s.proposition:
        p = s.proposition
        ligne["proposition"] = {
            "type": p.get("type", ""),
            "enonce": _render_html(p.get("question", "")),
            "consigne": p.get("consigne", ""),
            "choix": [(_render_html(c, block=False), i in p.get("reponses", [])) for i, c in enumerate(p.get("choix", []))],
            "reponses_texte": p.get("reponses_texte", []),
            "verso": _render_html(p.get("verso", "")),
            "explication": _render_html(p.get("explication", "")),
        }
    return ligne


# Champs d'une question remplacés par une correction (claude.CHAMPS_QUESTION de Revision)
CHAMPS_QUESTION = ("type", "question", "choix", "reponses", "reponses_texte", "consigne", "verso", "explication")


def _remplacer_question(qset, question_id, nouvelle):
    """Remplace une question dans le fichier JSON du set (``nouvelle`` : champs JSON), ou
    la retire si ``nouvelle`` est None. Les identifiants implicites (q1, q2…) des
    questions suivantes sont figés pour que leur progression reste rattachée."""
    racine = coffre.racine()
    rel, data = quiz.lire_json(racine / qset.path, racine)  # sets communs seulement (signalements)
    questions = data["questions"]
    for i, q in enumerate(questions, start=1):
        if str(q.get("id", f"q{i}")) == question_id:
            if nouvelle is None:
                for j in range(i, len(questions)):
                    questions[j].setdefault("id", f"q{j + 1}")
                del questions[i - 1]
            else:
                garde = {k: v for k, v in q.items() if k not in CHAMPS_QUESTION}
                questions[i - 1] = {**garde, **nouvelle}
            break
    else:
        abort(404)
    if not questions:
        raise ValueError("C'était la dernière question du set : supprime plutôt le set.")
    quiz.set_depuis_json(data, rel)  # validation avant d'écrire
    paquets.enregistrer_json(rel, data)


def _correcteur_ia():
    """Fonction de l'agent IA qui propose une question corrigée, si elle existe :
    ``corriger_question(qset, question, commentaires) -> (question JSON, note, …)``."""
    try:
        from .. import ai
    except Exception:  # noqa: BLE001
        return None
    if not _ia():
        return None
    fn = getattr(ai, "corriger_question", None)
    if fn is None:
        for nom in ("qcm", "demandes", "claude_demandes"):
            try:
                mod = __import__(f"hangar.ai.{nom}", fromlist=["corriger_question"])
                fn = getattr(mod, "corriger_question", None)
            except Exception:  # noqa: BLE001
                fn = None
            if fn:
                break
    return fn


@bp.route("/qcm/signalements/", methods=["GET", "POST"])
@login_required
def signalements():
    """Questions signalées (admins) : corriger avec l'IA, appliquer, retirer la question, clore."""
    if not current_user.is_admin:
        abort(403)
    library = quiz.get_library()
    if request.method == "POST":
        s = db.session.get(Signalement, request.form.get("pk", type=int) or 0)
        if s is None:
            abort(404)
        action = request.form.get("action", "")
        qset = library.get(s.set_id)
        question = next((q for q in qset.questions if q.id == s.question_id), None) if qset else None
        try:
            if action == "proposer" and question is not None:
                fn = _correcteur_ia()
                if fn is None:
                    raise ValueError("L'assistant IA ne propose pas de correction pour l'instant (fournisseur non configuré).")
                commentaires = [c for (c,) in db.session.query(Signalement.commentaire).filter(
                    Signalement.set_id == s.set_id, Signalement.question_id == s.question_id,
                    Signalement.traite.is_(False)) if c]
                resultat = fn(qset, question, commentaires)
                nouvelle, note = (resultat[0], resultat[1]) if isinstance(resultat, (tuple, list)) else (resultat, "")
                s.proposition, s.proposition_note = nouvelle, note or ""
                db.session.commit()
                flash("L'IA propose une correction : relis-la, puis applique-la ou non.", "success")
            elif action in ("appliquer", "retirer") and question is not None:
                if action == "appliquer" and not s.proposition:
                    raise ValueError("Aucune correction à appliquer.")
                _remplacer_question(qset, s.question_id, s.proposition if action == "appliquer" else None)
                _clore(s)
                flash("Question corrigée dans le set." if action == "appliquer" else "Question retirée du set.", "success")
            elif action == "clore":
                _clore(s)
                flash("Signalement clos.", "success")
            else:
                flash("Question introuvable : le set a changé.", "error")
        except Exception as e:  # noqa: BLE001 — erreur de l'IA ou du fichier : message à l'admin
            db.session.rollback()
            flash(str(e) or "Action impossible.", "error")
        return redirect(url_for("qcm.signalements"))
    ouverts = [_ligne_signalement(library, s) for s in Signalement.ouverts()]
    traites = Signalement.query.filter_by(traite=True).order_by(Signalement.traite_le.desc()).limit(20).all()
    return render_template("qcm/signalements.html", ouverts=ouverts, traites=traites,
                           ia=_correcteur_ia() is not None)


def _clore(s):
    Signalement.query.filter_by(set_id=s.set_id, question_id=s.question_id, traite=False).update(
        {"traite": True, "traite_le": quiz.maintenant()}, synchronize_session=False)
    db.session.commit()


# ---------------------------------------------------------------------------
# Espace personnel : sets et paquets importés par chaque compte, visibles de lui seul
# ---------------------------------------------------------------------------
def fichiers_perso(user_id):
    dossier = quiz.dossier_perso(user_id)
    return list(dossier.rglob("*.json")) if dossier.is_dir() else []


def taille_perso(user_id):
    """Octets occupés par l'espace personnel (sets, paquets et images)."""
    dossier = quiz.dossier_perso(user_id)
    return sum(f.stat().st_size for f in dossier.rglob("*") if f.is_file()) if dossier.is_dir() else 0


def _ecrire_paquet_perso(user_id, dossier, paquet):
    """Écrit un paquet de langue dans l'espace personnel ; renvoie son identifiant."""
    racine = quiz.dossier_perso(user_id)
    base = (paquets.slug(paquet.get("titre", "")) or "vocabulaire")[:60]
    for i in range(1, 1000):
        rel = PurePosixPath(dossier, f"{base}.json" if i == 1 else f"{base}-{i}.json")
        if not (racine / rel).exists():
            break
    try:
        langues.set_depuis_json(paquet, rel)
    except ValueError as e:
        raise paquets.PaquetInvalide(f"Paquet invalide : {e}") from None
    cible = racine / rel
    cible.parent.mkdir(parents=True, exist_ok=True)
    cible.write_text(json.dumps(paquet, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    invalider()
    return quiz.PERSO + quiz.nom_perso(user_id) + "/" + rel.with_suffix("").as_posix()


@bp.route("/qcm/mon-espace/", methods=["GET", "POST"])
@login_required
def perso():
    """« Mon espace » : importer ses propres sets (JSON, Anki) et listes de mots, les supprimer."""
    uid = current_user.id
    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "supprimer":
            element = _element(bibliotheque(uid), request.form.get("set", ""))
            if element is None or element.proprietaire != quiz.nom_perso(uid):
                abort(404, description="Set inconnu")
            element.fichier.unlink(missing_ok=True)
            _retirer_dossiers_vides(element.fichier.parent, quiz.dossier_perso(uid))
            _oublier([element.id])
            db.session.commit()
            invalider()
            flash(f"« {element.titre} » supprimé de ton espace.", "success")
            return redirect(url_for("qcm.perso"))
        if len(fichiers_perso(uid)) >= MAX_FICHIERS_PERSO or taille_perso(uid) >= MAX_OCTETS_PERSO:
            flash(f"Ton espace est plein ({MAX_FICHIERS_PERSO} fichiers, {MAX_OCTETS_PERSO // 2**20} Mo) : "
                  "supprimes-en d'abord.", "error")
            return redirect(url_for("qcm.perso"))
        if action == "json":
            categorie = clean_key(request.form.get("categorie", "").strip() or CATEGORIE_PERSO)
            fichiers = [f for f in request.files.getlist("fichiers") if f and f.filename]
            if categorie is None or "/" in categorie:
                flash("Dossier invalide : " + NOM_INVALIDE, "error")
            elif not fichiers:
                flash("Aucun fichier reçu.", "error")
            else:
                place = MAX_FICHIERS_PERSO - len(fichiers_perso(uid))
                for fichier in fichiers[:place]:
                    if taille_perso(uid) >= MAX_OCTETS_PERSO:
                        flash(f"{fichier.filename} : ton espace est plein ({MAX_OCTETS_PERSO // 2**20} Mo).", "error")
                        break
                    ok, message = _importer_un(fichier, categorie, request.form.get("remplacer") == "1",
                                               racine=quiz.dossier_perso(uid), prefixe="",
                                               lieu=f"ton espace, dossier « {categorie} »")
                    flash(message, "success" if ok else "error")
                if len(fichiers) > place:
                    flash(f"{len(fichiers) - place} fichier(s) non importé(s) : {MAX_FICHIERS_PERSO} fichiers au plus.", "error")
            return redirect(url_for("qcm.perso"))
        if action == "liste":
            paquet, options, ignorees, erreur = paquets.lire_liste(request.form, request.files.get("fichier"))
            if erreur:
                flash(erreur, "error")
                return redirect(url_for("qcm.perso"))
            try:
                set_id = _ecrire_paquet_perso(uid, options["dossier"] or paquets.DOSSIER_VOCABULAIRE, paquet)
            except paquets.PaquetInvalide as e:
                flash(str(e), "error")
                return redirect(url_for("qcm.perso"))
            flash(paquets.message_paquet(paquet, ignorees), "success")
            return redirect(url_for("qcm.langue_menu", langue=options["langue"]) + "?" + urlencode({"fichier": set_id}))
        return redirect(url_for("qcm.perso"))

    library = bibliotheque(uid)
    elements = []
    for e in library.personnels(uid):
        est_langue = not isinstance(e, quiz.QuestionSet)
        elements.append({"set": e, "langue": est_langue, "dossier": e.path.parent.name, "taille": len(e),
                         "jouer": (url_for("qcm.langue_menu", langue=e.langue) + "?" + urlencode({"fichier": e.id}))
                         if est_langue else url_for("qcm.play", set=e.id, mode="all")})
    return render_template(
        "qcm/perso.html", elements=elements, erreurs=library.erreurs_perso.get(quiz.nom_perso(uid), []),
        max_fichiers=MAX_FICHIERS_PERSO, nombre=len(fichiers_perso(uid)),
        taille=taille_perso(uid), max_octets=MAX_OCTETS_PERSO,
        langues=sorted(set(library.noms_langues()) | {"Anglais"}, key=cle_tri), cartes=_cartes(),
        dossiers=sorted({x["dossier"] for x in elements}, key=cle_tri),
        dossiers_langue=sorted({x["dossier"] for x in elements} | {paquets.DOSSIER_VOCABULAIRE}, key=cle_tri),
    )

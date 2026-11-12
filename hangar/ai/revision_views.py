"""Fonctions d'IA de Revision, aux mêmes adresses : explication d'une question de QCM, correction d'une réponse
rédigée, cartes d'une page, génération de QCM et de vocabulaire (travaux de fond), import d'une liste de mots,
« Améliorer » de l'éditeur, tuteur d'une page, « Demander au wiki », copie photographiée, suivi et relecture
des travaux.

Port de ``claude_views.py``, ``copie_views.py`` et ``explorer_views.py`` de Revision. Droits : lecture et
questions pour tous les comptes ; ce qui écrit dans le coffre (génération, cartes, transformations, relecture,
« Améliorer ») pour les administrateurs et les membres, pas les invités.
"""
from __future__ import annotations

from pathlib import Path

from flask import (
    abort, current_app, flash, jsonify, redirect, render_template, request, send_file, url_for,
)
from flask_login import current_user, login_required
from markupsafe import escape

from ..extensions import db
from ..models import now
from ..texte import cle_tri
from . import core, demandes, jobs, travaux
from .models import AICopie, AIJob, AITutorExchange
from .providers import AIError
from .views import bp

MAX_PHOTOS = 4
PLAFOND_TRAVAUX = ("Plafond mensuel de l’IA atteint : réessaie le mois prochain ou relève le plafond dans les réglages "
                   "de l’IA.")
TAILLE_MAX_PHOTO = 15 * 1024 * 1024
COTE_MAX = 1568   # au-delà, l'API réduit l'image de toute façon
GARDER_COPIES = 30
HISTORIQUE_AFFICHE = 10
HISTORIQUE_REJOUE = 3


def _dispo() -> bool:
    from . import disponible
    return disponible()


def _editeur() -> bool:
    return current_user.is_authenticated and not current_user.is_guest


def _erreur(e: AIError):
    status = {"plafond": 429, "indisponible": 503, "limite": 429}.get(e.code, 502)
    return jsonify(ok=False, error=e.message), status


def _sans_ia():
    return jsonify(ok=False, error="Aucun fournisseur d’IA n’est configuré : un administrateur peut en ajouter un "
                                   "dans les réglages de l’IA."), 503


def _html(texte: str) -> str:
    """Markdown d'une réponse -> HTML (moteur du wiki : formules, encadrés, liens [[…]])."""
    from ..qcm.rendu import rendre
    return rendre(texte)


def _avertissement(av: list[str]) -> tuple[str, str]:
    """(texte, html) des avertissements recueillis par core.avertissements(), ex. une demande plus grosse que la
    mémoire d'Ollama, qui a pu en oublier le début : montrés au-dessus de la réponse, jamais gardés avec elle."""
    return " ".join(av), "".join(f'<p class="small warning-text">{escape(a)}</p>' for a in av)


def _vault():
    from ..wiki.vault import get_vault
    return get_vault()


def _texte_page(vault, note) -> str:
    """Texte d'une page du wiki, quel que soit son encodage (« ANSI » du Bloc-notes…), comme dans l'éditeur.
    Lève ``OSError`` si l'ordinateur refuse de l'ouvrir (fichier verrouillé, OneDrive hors connexion…)."""
    from ..wiki.vault import lire_note
    return lire_note(vault.root / note.path)[0]


def _page_inaccessible() -> str:
    from ..wiki.views import FICHIER_INACCESSIBLE
    return FICHIER_INACCESSIBLE


def _json_body() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _question(set_id, question_id):
    from ..qcm.services import bibliotheque
    qset = bibliotheque(current_user.id).get(set_id)
    question = next((q for q in qset.questions if q.id == question_id), None) if qset else None
    return qset, question


# ---------------------------------------------------------------------------
# QCM : explication, réponse rédigée, cartes, génération, vocabulaire
# ---------------------------------------------------------------------------

@bp.route("/qcm/expliquer/", methods=["POST"])
@login_required
def expliquer():
    """« Explique-moi » sur une question de QCM : explication par l'IA, gardée (une seule fois par question, pour
    tous les comptes). Une explication coupée par le plafond de longueur n'est pas gardée ; un administrateur peut
    refaire une explication gardée (« refaire » : l'ancienne est effacée)."""
    from ..qcm.models import Explication

    data = _json_body()
    set_id, question_id = str(data.get("set_id") or ""), str(data.get("question_id") or "")
    if not set_id or not question_id:
        return jsonify(ok=False, error="requête invalide"), 400
    qset, question = _question(set_id, question_id)
    if question is None:
        return jsonify(ok=False, error="question inconnue"), 404
    admin = bool(current_user.is_admin)
    existante = Explication.query.filter_by(set_id=set_id, question_id=question_id).first()
    if existante is not None and not (admin and data.get("refaire")):
        return jsonify(html=_html(existante.texte), modele=existante.modele, gardee=True, refaire=admin)
    if not _dispo():
        return _sans_ia()
    try:
        with core.avertissements() as av:
            texte, usage, modele = demandes.expliquer(qset, question, user=current_user)
    except AIError as e:
        return _erreur(e)
    if not demandes.est_coupee(texte):  # coupée : montrée cette fois-ci, la prochaine demande refera l'appel
        e = Explication.query.filter_by(set_id=set_id, question_id=question_id).first() or \
            Explication(set_id=set_id, question_id=question_id)
        e.texte, e.modele, e.jetons_entree, e.jetons_sortie = texte, modele, usage[0], usage[1]
        db.session.add(e)
        db.session.commit()
    avertissement, html_avertissement = _avertissement(av)
    return jsonify(html=html_avertissement + _html(texte), modele=modele, gardee=False, refaire=admin,
                   avertissement=avertissement)


@bp.route("/qcm/corriger-qrc/", methods=["POST"])
@login_required
def corriger_qrc():
    """Réponse rédigée (QRC) différente du corrigé : verdict de l'IA (rôle « rapide ») sur le fond."""
    data = _json_body()
    set_id, question_id = str(data.get("set_id") or ""), str(data.get("question_id") or "")
    reponse = str(data.get("reponse") or "").strip()
    qset, question = _question(set_id, question_id)
    if question is None or not question.est_qrc or not reponse:
        return jsonify(ok=False, error="question inconnue"), 404
    if not _dispo():
        return _sans_ia()
    if core.cap_reached():
        return jsonify(ok=False, error="Plafond mensuel de l’IA atteint : correction stricte."), 429
    try:
        with core.avertissements() as av:
            verdict, retour, _usage, modele = demandes.corriger_qrc(qset, question, reponse, user=current_user)
    except AIError as e:
        return _erreur(e)
    return jsonify(verdict=verdict, retour=retour, correct=verdict == "juste", modele=modele,
                   avertissement=_avertissement(av)[0])


@bp.route("/qcm/cartes-de-la-page/", methods=["POST"])
@login_required
def cartes_page():
    """Écrit (ou met à jour) le set de cartes tirées d'une page : encadrés et surlignages, sans IA."""
    from ..qcm import paquets
    from ..wiki import cartes

    if not _editeur():
        abort(403)
    vault = _vault()
    note = vault.notes.get(request.form.get("page", "").strip().removesuffix(".md").lower())
    if note is None:
        flash("Page introuvable.", "error")
        return redirect(url_for("wiki.home"))
    try:
        questions = cartes.extraire(_texte_page(vault, note))
    except OSError:
        flash(_page_inaccessible(), "error")
        return redirect(note.url)
    if not questions:
        flash("Rien à tirer de cette page : ajoute des encadrés à titre (> [!definition] Titre) ou des ==surlignages==.",
              "info")
        return redirect(note.url)
    chemin = cartes.chemin_du_set(note)
    existait = (vault.root / chemin).exists()
    try:
        paquets.enregistrer_json(chemin, cartes.set_de_la_page(note, questions))
    except paquets.PaquetInvalide as e:
        flash(str(e), "error")
        return redirect(note.url)
    n = len(questions)
    flash(f"{'Cartes mises à jour' if existait else 'Cartes créées'} : {n} question{'s' if n > 1 else ''} "
          f"dans « {note.title} — cartes » ({chemin}).", "success")
    return redirect(note.url)


@bp.route("/qcm/generer/", methods=["GET", "POST"])
@login_required
def generer_qcm():
    """QCM (d'après une page du wiki ou un texte collé), variantes des questions ratées, ou paquet de vocabulaire
    (d'après des mots collés), générés par l'IA en arrière-plan."""
    from ..qcm import paquets

    if request.method == "GET":
        return redirect(url_for("qcm.importer") + "#generer")
    if not _editeur():
        abort(403)
    if not _dispo():
        flash("Ajoute d’abord un fournisseur d’IA dans les réglages.", "error")
        return redirect(url_for("ai.reglages"))
    genre = request.form.get("genre")
    retour = redirect(url_for("qcm.importer") + "#generer")
    if core.cap_reached():  # vérifié dès le dépôt (les réserves des lots en cours : voir travaux._enqueue)
        flash(PLAFOND_TRAVAUX, "error")
        return retour

    if genre == "qcm":
        cle = request.form.get("page", "").strip().removesuffix(".md")
        texte = request.form.get("texte", "").strip()
        nombre = request.form.get("nombre", "")
        nombre = int(nombre) if nombre.isdigit() and int(nombre) in travaux.NOMBRES_QUESTIONS else 20
        categorie = request.form.get("categorie", "").strip()
        if categorie and travaux.dossier_valide(categorie) is None:
            flash('Catégorie invalide : un seul nom de dossier, sans / \\ : * ? " < > | # ^ [ ].', "error")
            return retour
        note = None
        if cle:
            note = _vault().notes.get(cle.lower())
            if note is None:
                flash(f"Page introuvable : « {cle} ».", "error")
                return retour
        elif not texte:
            flash("Choisis une page du wiki ou colle le texte du cours.", "error")
            return retour
        elif len(texte) > travaux.MAX_TEXTE_COLLE:
            flash("Texte trop long (300 000 caractères au plus) : génère le QCM d’une partie à la fois.", "error")
            return retour
        try:  # en lot : refusé si les lots en cours réservent déjà le reste du plafond
            travaux.demander_qcm(note=note, texte="" if note else texte, nombre=nombre,
                                 categorie=travaux.dossier_valide(categorie) or "",
                                 titre=request.form.get("titre", "").strip()[:120], user=current_user,
                                 publier=request.form.get("publier") == "1", lot=request.form.get("lot") == "1")
        except AIError as e:
            flash(e.message, "error")
            return retour
        flash(f"L’IA écrit {nombre} questions sur « {note.title if note else 'le texte collé'} ».", "success")
    elif genre == "variantes":
        from ..qcm.models import QuizProgress
        from ..qcm.services import bibliotheque, question_ratee

        qset = bibliotheque(current_user.id).get(request.form.get("set", ""))
        if qset is None:
            flash("Set introuvable.", "error")
            return redirect(url_for("qcm.stats"))
        progress = QuizProgress.map_for(current_user.id, qset.id)
        ratees = sorted((st for st in progress.values() if question_ratee(st)),
                        key=lambda st: (st.correct / st.seen if st.seen else 0, -st.seen))
        ids = [st.question_id for st in ratees if any(q.id == st.question_id for q in qset.questions)][:travaux.MAX_VARIANTES]
        if not ids:
            flash(f"Aucune question ratée dans « {qset.titre} » : rien à varier.", "info")
            return redirect(url_for("qcm.stats"))
        travaux.demander_variantes(qset, ids, user=current_user)
        flash(f"L’IA écrit {len(ids)} variante{'s' if len(ids) > 1 else ''} des questions ratées de « {qset.titre} » "
              "(brouillon à relire).", "success")
    elif genre == "vocabulaire":
        mots = request.form.get("mots", "").strip()
        lignes = [x for x in mots.splitlines() if x.strip()]
        options, erreur = paquets.options_vocabulaire(request.form)
        if not lignes:
            erreur = "Colle au moins un mot."
        elif len(lignes) > travaux.MAX_MOTS:
            erreur = f"Trop de mots ({len(lignes)}) : {travaux.MAX_MOTS} au plus par paquet."
        if erreur:
            flash(erreur, "error")
            return retour
        try:
            travaux.demander_vocabulaire(mots="\n".join(x.strip() for x in lignes)[:20_000], user=current_user,
                                         publier=request.form.get("publier") == "1", lot=request.form.get("lot") == "1",
                                         langue=options["langue"], types=options["types"], dossier=options["dossier"],
                                         titre=options["titre"])
        except AIError as e:
            flash(e.message, "error")
            return retour
        flash(f"L’IA prépare un paquet de {len(lignes)} mot{'s' if len(lignes) > 1 else ''} en "
              f"{options['langue'].lower()}.", "success")
    else:
        flash("Demande inconnue.", "error")
        return retour
    return redirect(url_for("ai.transformations"))


@bp.route("/qcm/importer-vocabulaire/", methods=["GET", "POST"])
@login_required
def importer_vocabulaire():
    """Paquet de vocabulaire d'après une liste collée ou un fichier exporté (Quizlet, Anki, tableur), sans IA."""
    from ..qcm import paquets

    if request.method == "GET":
        return redirect(url_for("qcm.importer") + "#liste")
    if not _editeur():
        abort(403)
    retour = redirect(url_for("qcm.importer") + "#liste")
    paquet, options, ignorees, erreur = paquets.lire_liste(request.form, request.files.get("fichier"))
    if erreur:
        flash(erreur, "error")
        return retour
    try:
        rel = paquets.ecrire_paquet_langue(options["dossier"] or paquets.DOSSIER_VOCABULAIRE, paquet)
    except paquets.PaquetInvalide as e:
        flash(str(e), "error")
        return retour
    flash(paquets.message_paquet(paquet, ignorees), "success")
    return redirect(url_for("qcm.langue_menu", langue=options["langue"], fichier=rel.removesuffix(".json")))


# ---------------------------------------------------------------------------
# Wiki : « Améliorer », tuteur, « Demander au wiki »
# ---------------------------------------------------------------------------

@bp.route("/claude/ameliorer/", methods=["POST"])
@login_required
def ameliorer():
    """« Améliorer la rédaction » dans l'éditeur : le passage sélectionné (ou la page), relu par l'IA."""
    if not _editeur():
        return jsonify(ok=False, error="Réservé aux membres : les invités ne modifient pas le wiki."), 403
    if not _dispo():
        return _sans_ia()
    texte = request.form.get("texte", "") if request.form else str(_json_body().get("texte") or "")
    if not texte.strip():
        return jsonify(ok=False, error="Sélectionne d’abord le texte à améliorer."), 400
    try:
        resultat, usage = demandes.ameliorer(texte, user=current_user)
    except AIError as e:
        return jsonify(ok=False, error=e.message), 429 if e.code == "plafond" else 400
    return jsonify(texte=resultat, jetons=list(usage))


@bp.route("/claude/tuteur/", methods=["POST"])
@login_required
def tuteur():
    """Question au tuteur sur une page : réponse ancrée sur la page, gardée dans l'historique du compte."""
    data = _json_body()
    cle, question = str(data.get("page") or "").strip().removesuffix(".md"), str(data.get("question") or "").strip()
    vault = _vault()
    note = vault.notes.get(cle.lower())
    if note is None or not question:
        return jsonify(ok=False, error="page inconnue ou question vide"), 404
    if not _dispo():
        return _sans_ia()
    if core.cap_reached():
        return jsonify(ok=False, error="Plafond mensuel de l’IA atteint : le tuteur reviendra le mois prochain."), 429
    precedents = (AITutorExchange.query.filter_by(user_id=current_user.id, page=note.key)
                  .order_by(AITutorExchange.id.desc()).limit(demandes.HISTORIQUE_TUTEUR).all())[::-1]
    try:
        page_texte = _texte_page(vault, note)
    except OSError:
        return jsonify(ok=False, error=_page_inaccessible()), 409
    try:
        with core.avertissements() as av:
            texte, usage, modele = demandes.tuteur(note, page_texte, question,
                                                   [(e.question, e.reponse) for e in precedents], user=current_user)
    except AIError as e:
        return _erreur(e)
    db.session.add(AITutorExchange(user_id=current_user.id, page=note.key, question=question[:demandes.MAX_CHARS_QUESTION],
                                   reponse=texte, modele=modele, jetons_entree=usage[0], jetons_sortie=usage[1],
                                   cree_le=now()))
    db.session.commit()
    html = _html(texte)
    coupee = len(page_texte) > demandes.budget_cours("rapide")  # moins pour un modèle local
    if coupee:
        html = ('<p class="muted small">Page longue : le tuteur n’en a lu qu’une partie, choisie d’après ta question.'
                '</p>' + html)
    avertissement, html_avertissement = _avertissement(av)
    return jsonify(html=html_avertissement + html, modele=modele, coupee=coupee, avertissement=avertissement)


def echange_html(e: AITutorExchange) -> dict:
    """Réponse rendue, citations [n] changées en liens vers les sources."""
    from ..wiki import demander as recherche

    sources = e.sources
    html, cites = recherche.lier_citations(_html(e.reponse), sources)
    return {"id": e.id, "question": e.question, "html": html, "quand": e.cree_le, "modele": e.modele,
            "sources": [{**s, "n": i, "cite": i in cites} for i, s in enumerate(sources, 1)]}


@bp.route("/demander/", methods=["GET", "POST"])
@login_required
def demander():
    """« Demander au wiki » : page (GET) ; question posée à l'IA (POST JSON {question}) d'après les extraits des
    pages et des PDF du coffre, avec citations [n] cliquables."""
    from ..wiki import demander as recherche

    vault = _vault()
    echanges = AITutorExchange.query.filter_by(user_id=current_user.id, page=AITutorExchange.PAGE_WIKI)
    if request.method != "POST":
        recents = echanges.order_by(AITutorExchange.id.desc()).limit(HISTORIQUE_AFFICHE).all()
        return render_template("ai/demander.html", dispo=_dispo(), plafond=_dispo() and core.cap_reached(),
                               question=request.args.get("q", "")[:500], echanges=[echange_html(e) for e in recents])
    question = str(_json_body().get("question") or "").strip()
    if not question:
        return jsonify(ok=False, error="Question vide."), 400
    if not _dispo():
        return _sans_ia()
    if core.cap_reached():
        return jsonify(ok=False, error="Plafond mensuel de l’IA atteint : réessaie le mois prochain."), 429
    precedents = echanges.order_by(AITutorExchange.id.desc()).limit(HISTORIQUE_REJOUE).all()[::-1]
    # Les questions précédentes aident à retrouver les pages d'une question de suite (« et pourquoi ? »).
    sources = recherche.rassembler(vault, question, [e.question for e in precedents[-1:]])
    try:
        with core.avertissements() as av:
            texte, usage, modele = demandes.demander_wiki(question, sources,
                                                          [(e.question, e.reponse) for e in precedents],
                                                          user=current_user)
    except AIError as e:
        return _erreur(e)
    e = AITutorExchange(user_id=current_user.id, page=AITutorExchange.PAGE_WIKI,
                        question=question[:demandes.MAX_CHARS_QUESTION], reponse=texte, modele=modele,
                        jetons_entree=usage[0], jetons_sortie=usage[1], cree_le=now())
    e.sources = [{k: s[k] for k in ("titre", "url", "type")} for s in sources]
    db.session.add(e)
    db.session.commit()
    rendu = echange_html(e)
    avertissement, html_avertissement = _avertissement(av)
    return jsonify(html=html_avertissement + rendu["html"], sources=rendu["sources"], modele=modele,
                   avertissement=avertissement)


# ---------------------------------------------------------------------------
# « Corriger ma copie »
# ---------------------------------------------------------------------------

def _dossier_copies(user_id) -> Path:
    return Path(current_app.config["DATA_DIR"]) / "copies" / str(int(user_id))


def _bloc_copie(c: AICopie) -> dict:
    vault = _vault()
    return {"c": c, "html": _html(c.correction), "page": vault.notes.get(c.page.lower()) if c.page else None,
            "photos": range(c.photos)}


def _supprimer_copie(c: AICopie) -> None:
    for n in range(c.photos):
        (_dossier_copies(c.user_id) / f"{c.id}-{n}.jpg").unlink(missing_ok=True)
    db.session.delete(c)


@bp.route("/copie/", methods=["GET", "POST"])
@login_required
def copie():
    """Page « Corriger ma copie » (GET) ; correction d'une copie envoyée (POST multipart, réponse JSON)."""
    vault = _vault()
    mes = AICopie.query.filter_by(user_id=current_user.id).order_by(AICopie.id.desc())
    if request.method != "POST":
        cle = request.args.get("page", "")
        return render_template("ai/copie.html", dispo=_dispo(), plafond=_dispo() and core.cap_reached(),
                               max_photos=MAX_PHOTOS, page=vault.notes.get(cle.lower()) if cle else None,
                               pages=sorted((n.key for n in vault.notes.values()), key=cle_tri),
                               corrections=[_bloc_copie(c) for c in mes.limit(GARDER_COPIES)])
    if not _dispo():
        return _sans_ia()
    if core.cap_reached():
        return jsonify(ok=False, error="Plafond mensuel de l’IA atteint : réessaie le mois prochain."), 429
    fichiers = [f for f in request.files.getlist("photos") if f and f.filename]
    if not fichiers:
        return jsonify(ok=False, error="Ajoute au moins une photo de la copie."), 400
    if len(fichiers) > MAX_PHOTOS:
        return jsonify(ok=False, error=f"{MAX_PHOTOS} photos au plus."), 400
    photos = []
    for f in fichiers:
        brut = f.read(TAILLE_MAX_PHOTO + 1)
        if len(brut) > TAILLE_MAX_PHOTO:
            return jsonify(ok=False, error=f"{f.filename} : photo trop grosse (15 Mo au plus)."), 400
        try:
            photos.append(core.prepare_image(brut, max_side=COTE_MAX)[0])
        except ValueError:
            return jsonify(ok=False, error=f"{f.filename} : ce n’est pas une photo lisible (JPEG, PNG ou WebP)."), 400
    enonce = request.form.get("enonce", "").strip()[:4000]
    cle = request.form.get("page", "").strip().removesuffix(".md")
    note = vault.notes.get(cle.lower()) if cle else None
    if cle and note is None:
        return jsonify(ok=False, error=f"Page inconnue : {cle}"), 400
    try:
        cours = _texte_page(vault, note) if note else ""
    except OSError:
        return jsonify(ok=False, error=_page_inaccessible()), 409
    try:
        with core.avertissements() as av:
            texte, usage, modele = demandes.corriger_copie(photos, enonce, cours, note.title if note else "",
                                                           user=current_user)
    except AIError as e:
        return _erreur(e)
    c = AICopie(user_id=current_user.id, enonce=enonce, page=note.key if note else "", photos=len(photos),
                correction=texte, modele=modele, jetons_entree=usage[0], jetons_sortie=usage[1], cree_le=now())
    db.session.add(c)
    db.session.commit()
    dossier = _dossier_copies(current_user.id)
    dossier.mkdir(parents=True, exist_ok=True)
    for n, photo in enumerate(photos):
        (dossier / f"{c.id}-{n}.jpg").write_bytes(photo)
    for ancienne in mes.offset(GARDER_COPIES).all():
        _supprimer_copie(ancienne)
    db.session.commit()
    avertissement, html_avertissement = _avertissement(av)
    bloc = _bloc_copie(c)
    bloc["html"] = html_avertissement + bloc["html"]
    return jsonify(ok=True, bloc=render_template("ai/_copie.html", b=bloc, ouvert=True), avertissement=avertissement)


@bp.route("/copie/photo/<int:pk>/<int:n>")
@login_required
def copie_photo(pk, n):
    c = db.session.get(AICopie, pk)
    if c is None or c.user_id != current_user.id or n >= c.photos:
        abort(404)
    chemin = _dossier_copies(c.user_id) / f"{c.id}-{n}.jpg"
    if not chemin.is_file():
        abort(404)
    reponse = send_file(chemin, mimetype="image/jpeg")
    reponse.headers["Cache-Control"] = "private, max-age=86400"
    return reponse


@bp.route("/copie/<int:pk>/supprimer/", methods=["POST"])
@login_required
def copie_supprimer(pk):
    c = db.session.get(AICopie, pk)
    if c is None or c.user_id != current_user.id:
        abort(404)
    _supprimer_copie(c)
    db.session.commit()
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# Travaux : transformations de cours, fiches, QCM générés, relecture
# ---------------------------------------------------------------------------

@bp.route("/transformations/", methods=["GET", "POST"])
@login_required
def transformations():
    """Suivi des travaux de l'IA ; transformer un PDF ou un cours LaTeX en page ; fiche d'une page ; relancer un
    échec ; publier ou supprimer un brouillon ; vérifier les lots."""
    if not _editeur():
        abort(403)
    vault = _vault()
    if request.method == "POST":
        f = request.form
        envoi = request.files.get("fichier")
        nouveau = bool(f.get("fiche") or f.get("pdf") or (envoi is not None and envoi.filename))
        if jid := f.get("annuler", type=int):
            try:
                annule = jobs.annuler(db.session.get(AIJob, jid) or abort(404))
            except AIError as e:
                flash(e.message, "error")
            else:
                flash("Travail annulé." if annule else "Ce travail n’est plus en cours : rien à annuler.",
                      "success" if annule else "info")
        elif not _dispo() and not (f.get("supprimer") or f.get("publier")):
            flash("Aucun fournisseur d’IA : ajoute-en un dans les réglages.", "error")
        elif nouveau and core.cap_reached():
            flash(PLAFOND_TRAVAUX, "error")
        elif jid := f.get("relancer", type=int):
            if travaux.relancer(db.session.get(AIJob, jid) or abort(404)):
                flash("Travail relancé.", "success")
            else:
                flash("Ce travail n’est pas en échec : rien à relancer.", "info")
        elif f.get("verifier_lots"):
            avant = [j.id for j in AIJob.query.filter_by(status="batch")]
            n = jobs.check_batches()
            jobs.start_worker()
            echecs = AIJob.query.filter(AIJob.id.in_(avant), AIJob.status == "error").count() if avant else 0
            if echecs:
                flash(f"{echecs} lot{'s' if echecs > 1 else ''} en échec : lis le message du travail, puis "
                      "relance-le si besoin.", "error")
            if n > echecs:
                flash(f"{n - echecs} travail{'aux' if n - echecs > 1 else ''} récupéré{'s' if n - echecs > 1 else ''}.",
                      "success")
            elif not echecs:
                flash("Les lots ne sont pas encore traités : reviens plus tard.", "info")
        elif jid := f.get("publier", type=int):
            job = db.session.get(AIJob, jid) or abort(404)
            try:
                travaux.publier(job)
                flash(f"« {travaux.nom(job)} » publié.", "success")
            except AIError as e:
                flash(e.message, "error")
        elif jid := f.get("supprimer", type=int):
            job = db.session.get(AIJob, jid) or abort(404)
            if job.status == "draft":
                travaux.supprimer_brouillon(job)
                flash("Brouillon supprimé.", "success")
        elif cle := f.get("fiche"):
            note = vault.notes.get(cle.strip().removesuffix(".md").lower())
            if note is None:
                flash(f"Page introuvable : « {cle} ».", "error")
            elif travaux.en_cours("fiche", note.path.as_posix()):
                flash(f"La fiche de « {note.title} » est déjà en préparation.", "info")
            else:
                travaux.demander_fiche(note, current_user)
                flash(f"L’IA rédige la fiche de révision de « {note.title} ».", "success")
        elif (fichier := request.files.get("fichier")) is not None and fichier.filename:
            from ..wiki import uploads

            dossier = uploads.clean_folder(f.get("dossier", ""))
            suffixe = Path(fichier.filename).suffix.lower()
            if dossier is None:
                flash("Dossier de destination invalide.", "error")
            elif suffixe not in (".pdf", ".tex", ".latex"):
                flash("Envoie un PDF ou un fichier LaTeX (.tex).", "error")
            else:
                res = uploads.import_file(vault, uploads.FichierEnvoye(fichier), fichier.filename, dossier,
                                          auteur=current_user.username)
                results = uploads.finish(_vault(), [res])
                if not res.ok:
                    flash(f"{res.name} : {res.message}", "error")
                elif travaux.transformer_cours(results, current_user, lot=f.get("lot") == "1"):
                    flash(f"{res.path} importé · transformation par l’IA "
                          + ("dans un lot (sous 24 h)." if f.get("lot") == "1" else "lancée."), "success")
        elif chemin := f.get("pdf"):
            pdf = vault.attachments.get(chemin.strip().lower())
            if pdf is None or pdf.suffix != ".pdf":
                flash("PDF introuvable dans le wiki.", "error")
            elif travaux.en_cours("page", pdf.path.as_posix()):
                flash(f"{pdf.name} est déjà en cours de transformation.", "info")
            else:
                lot = f.get("lot") == "1"
                try:
                    travaux.demander_pdf(pdf, current_user, lot=lot)
                except AIError as e:
                    flash(e.message, "error")
                else:
                    flash(f"Transformation de {pdf.name} "
                          + ("déposée dans un lot (résultat sous 24 h)." if lot else "lancée."), "success")
        return redirect(url_for("ai.transformations"))

    try:
        jobs.resume()  # reprend après un redémarrage du serveur ; surveille les lots
    except Exception:  # noqa: BLE001
        pass
    lignes = travaux.lignes_travaux(50)
    return render_template("ai/travaux.html", lignes=lignes, dispo=_dispo(),
                           en_cours=any(x["job"].status in ("pending", "running") for x in lignes),
                           lots=any(x["job"].status == "batch" for x in lignes),
                           pdfs=[a.path.as_posix() for a in vault.pdfs()],
                           pages=sorted((n.key for n in vault.notes.values()), key=cle_tri),
                           dossiers=sorted({n.folder for n in vault.notes.values() if n.folder}, key=cle_tri))


@bp.route("/transformations/<int:jid>/relire/", methods=["GET", "POST"])
@login_required
def relire(jid):
    """Relecture d'un brouillon (QCM ou paquet de vocabulaire généré) : garder, corriger, publier."""
    from ..qcm import quiz

    if not _editeur():
        abort(403)
    job = db.session.get(AIJob, jid) or abort(404)
    res = job.result or {}
    if job.status != "draft" or not res.get("contenu"):
        flash("Ce travail n’est plus un brouillon.", "info")
        return redirect(url_for("ai.transformations"))
    contenu = res["contenu"]
    vocabulaire = job.kind == "vocabulaire"
    if request.method == "POST":
        f = request.form
        if f.get("action") == "supprimer":
            travaux.supprimer_brouillon(job)
            flash("Brouillon supprimé.", "success")
            return redirect(url_for("ai.transformations"))
        corrige = dict(contenu)
        corrige["titre"] = f.get("titre", "").strip() or contenu.get("titre", "")
        corrige["description"] = f.get("description", "").strip()
        gardes, refus = [], []
        if not vocabulaire:
            for i, q in enumerate(contenu["questions"]):
                if f.get(f"q{i}-garder") != "1":
                    continue
                nouvelle = _question_relue(f, i, q)
                try:
                    quiz.question_depuis_json(nouvelle, i + 1, f"question {i + 1}")
                except ValueError as e:
                    refus.append(str(e))
                    continue
                gardes.append(nouvelle)
            corrige["questions"] = gardes
            total = len(contenu["questions"])
        else:
            for i, fiche in enumerate(contenu["vocabulaire"]):
                if f.get(f"v{i}-garder") != "1":
                    continue
                nouvelle = dict(fiche)
                nouvelle["mot"] = f.get(f"v{i}-mot", "").strip() or fiche["mot"]
                trads = [x.strip() for x in f.get(f"v{i}-traductions", "").split(";") if x.strip()]
                nouvelle["traductions"] = trads or fiche.get("traductions", [])
                for champ in ("exemple", "exemple_traduction", "note"):
                    valeur = f.get(f"v{i}-{champ}", "").strip()
                    if valeur:
                        nouvelle[champ] = valeur
                    else:
                        nouvelle.pop(champ, None)
                gardes.append(nouvelle)
            corrige["vocabulaire"] = gardes
            total = len(contenu["vocabulaire"])
        if refus:
            flash("Questions non publiées : " + " · ".join(refus), "error")
        if not gardes:
            flash("Rien à publier : garde au moins un élément.", "error")
            return redirect(url_for("ai.relire", jid=job.id))
        try:
            travaux.publier(job, corrige)
        except AIError as e:
            flash(e.message, "error")
            return redirect(url_for("ai.relire", jid=job.id))
        job.message = f"Publié après relecture : {len(gardes)} sur {total} gardé{'s' if len(gardes) > 1 else ''}."
        db.session.commit()
        flash(f"« {corrige['titre']} » publié ({len(gardes)} sur {total}).", "success")
        url, _ = travaux.resultat(job)
        return redirect(url or url_for("ai.transformations"))

    questions = []
    if not vocabulaire:
        for i, q in enumerate(contenu["questions"]):
            questions.append({"i": i, "q": q, "choix": list(enumerate(q.get("choix", []))),
                              "reponses_texte": " / ".join(q.get("reponses_texte", [])), "qcr": q.get("type") == "QCR"})
    return render_template("ai/relire.html", job=job, nom=travaux.nom(job), genre=travaux.GENRES.get(job.kind, job.kind),
                           contenu=contenu, questions=questions,
                           fiches=list(enumerate(contenu.get("vocabulaire", []))) if vocabulaire else [],
                           avertissement=(job.message or "").replace("À relire avant publication.", "").strip())


@bp.route("/ia/kit/", methods=["GET", "POST"])
@login_required
def kit_revision():
    """Kit de révision d'une collection du wiki (avant un contrôle) : fiche et QCM des pages qui n'en ont pas,
    cases à décocher, coût estimé face au plafond restant, puis travaux de fond (en lot si demandé)."""
    from . import kit

    if not _editeur():
        abort(403)
    if not _dispo():
        flash("Ajoute d’abord un fournisseur d’IA dans les réglages.", "error")
        return redirect(url_for("ai.reglages"))
    vault = _vault()
    v = request.values
    dossier = v.get("dossier", "").strip().strip("/")
    pages = kit.pages_de(vault, dossier) if dossier else []
    if not pages:
        if dossier:
            flash(f"Aucune page à réviser dans « {dossier} ».", "info")
        collections = sorted({n.folder for n in vault.notes.values() if n.folder}, key=cle_tri)
        return render_template("ai/kit.html", dossier="", collections=collections)
    controles = kit.controles(current_user, dossier)
    if "evenement" in v:
        eid = v.get("evenement", type=int)
    else:  # par défaut, le prochain contrôle d'une matière reliée à cette collection
        eid = controles[0]["id"] if controles else None
    date_examen, categorie = kit.date_controle(current_user, eid)
    if date_examen is None:
        eid = None
    if eid and not any(c["id"] == eid for c in controles):  # contrôle d'une matière reliée à une autre collection
        controles.insert(0, {"id": eid, "titre": "Contrôle choisi", "date": date_examen, "categorie": categorie})
    categorie = categorie or kit.categorie_liee(current_user, dossier)
    nombre = v.get("nombre", type=int)
    nombre = nombre if nombre in travaux.NOMBRES_QUESTIONS else kit.NOMBRE_DEFAUT
    retour = url_for("ai.kit_revision", dossier=dossier, evenement=eid or 0, nombre=nombre)

    if request.method == "POST":
        from . import lots_possibles
        lot = v.get("lot") == "1" and lots_possibles()
        try:
            faits = kit.lancer(current_user, vault, dossier, request.form.getlist("fiche"),
                               request.form.getlist("qcm"), nombre=nombre, lot=lot, categorie=categorie,
                               date_examen=date_examen)
        except AIError as e:
            faits = getattr(e, "faits", None)
            if faits and (faits["fiches"] or faits["qcm"]):
                flash(f"{e.message} Déjà lancés : {faits['fiches']} fiche(s) et {faits['qcm']} QCM.", "error")
                return redirect(url_for("ai.transformations"))
            flash(e.message, "error")
            return redirect(retour)
        parts = []
        if faits["fiches"]:
            parts.append(f"{faits['fiches']} fiche{'s' if faits['fiches'] > 1 else ''}")
        if faits["qcm"]:
            parts.append(f"{faits['qcm']} QCM")
        flash(f"Kit de révision lancé : {' et '.join(parts)} (environ {faits['cout']:.2f} $"
              + (", dans des lots à moitié prix, sous 24 h" if lot else "") + "). Les QCM arriveront en brouillon à "
              "relire ; une notification te préviendra à la fin.", "success")
        return redirect(url_for("ai.transformations"))

    p = kit.plan(current_user, vault, dossier, nombre, categorie)
    return render_template("ai/kit.html", dossier=dossier, plan=p, controles=controles, eid=eid,
                           date_examen=date_examen, categorie=categorie, nombre=nombre,
                           nombres=travaux.NOMBRES_QUESTIONS)


def _question_relue(f, i, q):
    """Question du brouillon après correction dans le formulaire de relecture."""
    nouvelle = {k: v for k, v in q.items() if k not in ("choix", "reponses", "reponses_texte", "verso", "explication",
                                                        "consigne")}
    nouvelle["question"] = f.get(f"q{i}-question", "").strip() or q.get("question", "")
    type_q = str(q.get("type", "QCM")).upper()
    if type_q in ("QCM", "QCR"):
        choix = [f.get(f"q{i}-choix-{j}", "").strip() for j in range(len(q.get("choix", [])))]
        bonnes = [j for j in range(len(choix)) if f.get(f"q{i}-bonne-{j}") == "1"] if type_q == "QCR" \
            else [int(f.get(f"q{i}-bonne", -1))] if f.get(f"q{i}-bonne", "").isdigit() else []
        # Un choix vidé disparaît ; les indices des bonnes réponses suivent.
        garde = [j for j, c in enumerate(choix) if c]
        nouvelle["choix"] = [choix[j] for j in garde]
        nouvelle["reponses"] = sorted(garde.index(j) for j in bonnes if j in garde)
    elif type_q in ("TROU", "QRC"):
        nouvelle["reponses_texte"] = [x.strip() for x in f.get(f"q{i}-reponses", "").split("/") if x.strip()]
        if f.get(f"q{i}-consigne", "").strip():
            nouvelle["consigne"] = f.get(f"q{i}-consigne", "").strip()
    elif type_q == "FLASHCARD":
        nouvelle["verso"] = f.get(f"q{i}-verso", "").strip()
    explication = f.get(f"q{i}-explication", "").strip()
    if explication:
        nouvelle["explication"] = explication
    return nouvelle

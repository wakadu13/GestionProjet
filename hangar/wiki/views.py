"""Wiki : routes (port des vues de Revision : views.py, pdf_views.py, explorer_views.py).

Pages d'une note, édition et aperçu, nouvelle page, import de fichiers, sommaire, tags,
recherche (notes et texte des PDF), graphe des liens, historique des versions, lecteur de
PDF et fichiers du coffre. Les adresses sont celles de Revision (/wiki/, /modifier/,
/recherche/, /pdf/…). Les fonctions d'IA (tuteur, « Demander au wiki », génération de QCM,
« Améliorer ») sont dans le module ``hangar.ai`` : le wiki n'affiche leurs boutons que si
``hangar.ai.disponible()``.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from pathlib import PurePosixPath

from flask import (Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, send_file,
                   url_for)
from flask_login import current_user

from .. import ai
from ..extensions import db
from ..icons import ICONS
from ..securite import protect_file
from ..texte import cle_tri
from . import commun, erreurs, historique, installation, texte_pdf, uploads
from . import latex as latex_converter
from .commun import can_edit, deny, render, wants_json
from .render import Renderer
from .vault import Note, clean_key, get_vault, lire_note, split_frontmatter, toujours_verifier

bp = Blueprint("wiki", __name__)

METHODES_SURES = {"GET", "HEAD", "OPTIONS", "TRACE"}
MAX_IMPORT = 110 * 1024 * 1024  # un envoi d'import (lots de 40 Mo, pièce jointe de 100 Mo au plus)
DOSSIER_ANNOTATIONS = "Annotations"
MAX_CHARS_ANNOTATION = 2000
# Pièces jointes affichées dans la page ; tout le reste (HTML, SVG, JS...) est téléchargé,
# pour qu'un fichier déposé dans le coffre ne puisse pas exécuter de script au nom du site.
INLINE_TYPES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif", ".pdf",
    ".mp3", ".wav", ".ogg", ".m4a", ".flac", ".webm", ".3gp", ".mp4", ".ogv", ".mov", ".mkv",
}
PROPRIETES_CACHEES = {"title", "tags", "tag", "aliases", "alias", "cssclasses", "cssclass", "icon", "emoji",
                      "banner", "cover"}
# Fichier d'une page que le système refuse d'ouvrir (OSError à la lecture ou à l'écriture).
FICHIER_INACCESSIBLE = ("L'ordinateur refuse d'ouvrir le fichier de cette page (il est verrouillé par un autre "
                        "programme, ou pas encore téléchargé par OneDrive). Réessaie dans un moment.")

# Icônes propres au wiki (sans toucher au fichier partagé icons.py)
ICONS.setdefault("wiki-graph", '<circle cx="6" cy="6" r="2.5"/><circle cx="18" cy="7" r="2.5"/><circle cx="12" cy="18" r="2.5"/>'
                               '<path d="M8.3 7.1 15.6 6.6"/><path d="m7.2 8.2 3.8 7.6"/><path d="m16.8 9.2-3.6 6.8"/>')
ICONS.setdefault("wiki-maximize", '<path d="M8 3H5a2 2 0 0 0-2 2v3"/><path d="M21 8V5a2 2 0 0 0-2-2h-3"/>'
                                  '<path d="M3 16v3a2 2 0 0 0 2 2h3"/><path d="M16 21h3a2 2 0 0 0 2-2v-3"/>')
ICONS.setdefault("wiki-file-text", '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/>'
                                   '<path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="M10 9H8"/><path d="M16 13H8"/><path d="M16 17H8"/>')
ICONS.setdefault("wiki-pdf", '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/>'
                             '<path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="M8 17v-4h1.5a1.5 1.5 0 0 1 0 3H8"/>')
ICONS.setdefault("wiki-sigma", '<path d="M18 7V4H6l6 8-6 8h12v-3"/>')
ICONS.setdefault("wiki-panel", '<rect x="3.5" y="4.5" width="17" height="15" rx="2"/><path d="M9.5 4.5v15"/>')
ICONS.setdefault("wiki-list-tree", '<path d="M21 12h-8"/><path d="M21 6H8"/><path d="M21 18h-8"/>'
                                   '<path d="M3 6v4c0 1.1.9 2 2 2h3"/><path d="M3 10v6c0 1.1.9 2 2 2h3"/>')


# ---------------------------------------------------------------------------
# Crochets : mise en place, connexion, coffre relu pendant les requêtes qui écrivent
# ---------------------------------------------------------------------------

@bp.record_once
def _installer(state):
    """Premier lancement : coffre vide → page d'accueil et guide de la syntaxe."""
    app = state.app
    app.config.setdefault("WIKI_VERIF_INTERVALLE", 2.0)
    if app.testing:
        return
    try:
        installation.amorcer(app.config["WIKI_DIR"])
    except OSError as exc:  # coffre en lecture seule : le wiki reste lisible
        app.logger.warning("Wiki : impossible de préparer le coffre (%s)", exc)


@bp.before_app_request
def _avant_requete():
    # Import de fichiers : envois plus gros que la limite générale (lots de 40 Mo, PDF de 100 Mo).
    if request.endpoint == "wiki.importer":
        request.max_content_length = MAX_IMPORT
    # Pendant une requête qui peut écrire (POST…), le coffre est relu à chaque accès, et le
    # prochain accès après elle le relira aussi : les fichiers écrits sont visibles tout de suite.
    if request.method not in METHODES_SURES:
        verif = toujours_verifier()
        verif.__enter__()
        g._wiki_verif = verif


@bp.teardown_app_request
def _apres_requete(_exc):
    verif = g.pop("_wiki_verif", None)
    if verif is not None:
        verif.__exit__(None, None, None)


# Lecture hors ligne : le service worker, le manifeste et la page « hors connexion » sont servis sans
# compte (le navigateur les demande lui-même, et la page hors connexion ne montre rien du wiki).
PUBLICS = {"wiki.service_worker", "wiki.manifeste", "wiki.hors_connexion"}


@bp.before_request
def _connexion():
    if request.endpoint in PUBLICS:
        return None
    if not current_user.is_authenticated:
        return current_app.login_manager.unauthorized()
    return None


@bp.after_request
def _cache_prive(response):
    """Pages et fichiers du wiki : jamais gardés par un proxy pour les resservir à quelqu'un d'autre."""
    if request.endpoint not in PUBLICS:
        cc = response.headers.get("Cache-Control", "")
        if "private" not in cc and "no-store" not in cc:
            response.headers["Cache-Control"] = f"private, {cc}" if cc else "private"
    return response


@bp.app_context_processor
def _gabarits():
    return {"wiki_sidebar": commun.sidebar_data, "wiki_icone": commun.icone, "cache_hors_ligne": "hangar-wiki-v1",
            "erreurs_ouvertes": erreurs.ouvertes}


# Panneau IA : la page du wiki affichée lui est décrite (texte Markdown, titre, adresse).
ai.register_context(commun.contexte_ia)


# ---------------------------------------------------------------------------
# Lecture d'une note
# ---------------------------------------------------------------------------

def _valeur_propriete(value):
    if isinstance(value, bool):
        return "Oui" if value else "Non"
    if isinstance(value, datetime):
        return value.strftime("%d/%m/%Y %H:%M")
    if isinstance(value, date):
        return value.strftime("%d/%m/%Y")
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(_valeur_propriete(v)) for v in value)
    if isinstance(value, dict):
        return ", ".join(f"{k} : {_valeur_propriete(v)}" for k, v in value.items())
    return "" if value is None else str(value)


def proprietes(note):
    """Propriétés YAML de la note à afficher en tableau (hors titre, tags, alias, icône)."""
    lignes = []
    for key, value in note.meta.items():
        if str(key).lower() in PROPRIETES_CACHEES:
            continue
        texte = _valeur_propriete(value)
        lien = texte if re.match(r"^https?://\S+$", texte) else ""
        lignes.append({"cle": str(key), "valeur": texte, "lien": lien})
    return lignes


def fil_ariane(parts):
    """Dossiers de la page : [(nom, adresse du sommaire filtré sur ce dossier)]."""
    sortie, chemin = [], ""
    for part in parts:
        chemin = f"{chemin}/{part}" if chemin else part
        sortie.append((part, url_for("wiki.sommaire", dossier=chemin)))
    return sortie


def _qcm_de_la_page(note):
    """Sets de QCM écrits d'après cette page (``source_page``), avec la progression du compte :
    maîtrise et boutons Réviser / Tout / Examen du panneau « Réviser ce cours »."""
    try:
        from ..qcm import quiz, services
        from ..qcm.models import QuizDeck, QuizProgress
        cle = note.key.lower()
        sets = [s for s in services.bibliotheque(current_user.id).sets.values()
                if (s.source_page or "").strip().lower() == cle]
        if not sets:
            return []
        maintenant, decks = quiz.maintenant(), QuizDeck.map_for(current_user.id)
        return [{"set": s, "ancre": services.ancre(s.id),
                 "summary": services.resume_set(s, QuizProgress.map_for(current_user.id, s.id), maintenant,
                                                decks.get(s.id))}
                for s in sorted(sets, key=lambda s: cle_tri(s.titre))]
    except Exception:  # noqa: BLE001 - la section QCM ne doit jamais empêcher la lecture d'une page
        current_app.logger.exception("Wiki : QCM de la page %s", note.key)
        return []


def _cartes_de_la_page(vault, note):
    """Cartes qu'on peut tirer de la page (encadrés, surlignages) et set déjà écrit, s'il existe."""
    from . import cartes
    try:
        texte, _ = lire_note(vault.root / note.path)
        nombre = len(cartes.extraire(texte))
        if not nombre:
            return None
        from ..qcm import quiz
        existe = quiz.get_library().commune().get(cartes.chemin_du_set(note).removesuffix(".json")) is not None
    except Exception:  # noqa: BLE001
        return None
    return {"nombre": nombre, "existe": existe}


def _tuteur_de_la_page(note):
    """Historique du tuteur sur cette page pour le compte, si une IA est configurée."""
    if not commun.ai_dispo():
        return None
    try:
        from ..ai import core, demandes
        from ..ai.models import AITutorExchange
        from ..ai.revision_views import echange_html
        echanges = (AITutorExchange.query.filter_by(user_id=current_user.id, page=note.key)
                    .order_by(AITutorExchange.id.desc()).limit(demandes.HISTORIQUE_TUTEUR).all())[::-1]
        return {"echanges": [echange_html(e) for e in echanges], "plafond": core.cap_reached()}
    except Exception:  # noqa: BLE001 - le tuteur ne doit jamais empêcher la lecture d'une page
        current_app.logger.exception("Wiki : tuteur de la page %s", note.key)
        return None


def _fiche_en_cours(note) -> bool:
    try:
        from ..ai import travaux
        return bool(travaux.en_cours("fiche", note.path.as_posix()))
    except Exception:  # noqa: BLE001
        return False


def _note_page(vault, note):
    content, toc = vault.render(note)
    backlinks = sorted(vault.backlinks.get(note.key.lower(), ()), key=lambda n: cle_tri(n.title))
    fiche = vault.notes.get(note.path.with_name(f"{note.path.stem} (Fiche)").with_suffix("").as_posix().lower())
    mots = len(re.findall(r"\w+", note.clean_body))
    commun.noter_visite(note.key)
    edition = can_edit()
    return render("wiki/page.html", vault=vault, note=note, content=content, toc=toc, backlinks=backlinks,
                  fiche=fiche, favori=commun.est_favori(note), mots=mots, lecture=max(1, round(mots / 220)),
                  proprietes=proprietes(note), crumbs=fil_ariane(note.folders), icone=commun.icone(note),
                  qcm_page=_qcm_de_la_page(note), cartes=_cartes_de_la_page(vault, note) if edition else None,
                  tuteur=_tuteur_de_la_page(note), perimee=note.path.as_posix() in vault.illisibles,
                  fiche_en_cours=edition and fiche is None and commun.ai_dispo() and _fiche_en_cours(note))


@bp.route("/wiki/")
def home():
    vault = get_vault()
    note = vault.home()
    if note is not None:
        return _note_page(vault, note)
    return render("wiki/index.html", vault=vault, recent=vault.recent_notes(12))


@bp.route("/wiki/<path:path>")
def page(path):
    vault = get_vault()
    note = vault.notes.get(path.lower().removesuffix(".md"))
    if note is None:
        # Lien court (/wiki/Ma note) vers une note rangée dans un sous-dossier.
        target = vault.resolve(path)
        if target is not None:
            return redirect(target.url if isinstance(target, Note) else target.view_url)
        backlinks = sorted(vault.unresolved.get(path.lower(), ()), key=lambda n: cle_tri(n.title))
        return render("wiki/missing.html", vault=vault, name=path, filename=f"{path}.md", backlinks=backlinks,
                      can_create=clean_key(path) is not None, crumbs=fil_ariane(path.split("/")[:-1])), 404
    return _note_page(vault, note)


@bp.route("/favoris-wiki/<path:path>", methods=["POST"])
def favori(path):
    """Ajoute la page aux favoris du compte, ou l'en retire (étoile)."""
    from .models import WikiFavorite
    vault = get_vault()
    note = vault.notes.get(path.lower().removesuffix(".md"))
    if note is None:
        abort(404)
    fav = WikiFavorite.query.filter_by(user_id=current_user.id, key=note.key).first()
    if fav is None:
        db.session.add(WikiFavorite(user_id=current_user.id, key=note.key))
    else:
        db.session.delete(fav)
    db.session.commit()
    if wants_json():
        return jsonify(ok=True, favorite=fav is None)
    return redirect(note.url)


# ---------------------------------------------------------------------------
# Édition
# ---------------------------------------------------------------------------

@bp.route("/modifier/<path:path>", methods=["GET", "POST"])
def edit(path):
    """Création ou modification d'une note depuis le navigateur."""
    if not can_edit():
        deny()
    vault = get_vault()
    key = clean_key(path)
    if key is None:
        abort(404, description="Nom de page invalide")
    note = vault.notes.get(key.lower())
    if note is None and "/" not in key:
        # /modifier/Ma note pour une note rangée dans un sous-dossier.
        target = vault.resolve(key)
        if isinstance(target, Note):
            return redirect(url_for("wiki.edit", path=target.key))
    if note is not None:
        key = note.key
    file = vault.root / f"{key}.md"
    version = str(file.stat().st_mtime_ns) if file.is_file() else ""
    error = ""
    encodage = "utf-8"

    if request.method == "POST":
        content = request.form.get("content", "").replace("\r\n", "\n")
        if request.form.get("version", "") != version:
            error = ("Cette page a été modifiée ailleurs depuis l'ouverture de l'éditeur. "
                     "Copie ton texte, recharge la page, puis réapplique tes changements.")
            if wants_json():
                return jsonify(ok=False, error=error), 409
        else:
            if not file.resolve().is_relative_to(vault.root.resolve()):
                abort(400)
            # « seance » : l'éditeur a déjà enregistré un changement depuis son ouverture ; la
            # version vérifiée ci-dessus prouve alors que la page contient encore son propre texte,
            # et ses enregistrements rapprochés peuvent être regroupés dans l'historique.
            seance = request.form.get("seance") == "1"
            change = True
            try:
                file.parent.mkdir(parents=True, exist_ok=True)
                if file.is_file():
                    # Page d'un autre encodage (« ANSI » du Bloc-notes…) : enregistrée en UTF-8, et
                    # son texte d'origine gardé dans l'historique (jamais regroupé) même s'il n'a pas changé.
                    ancien, ancien_encodage = lire_note(file)
                    change = ancien != content
                    if change or ancien_encodage != "utf-8":
                        historique.sauvegarder(f"{key}.md", ancien, current_user.username, edition=True,
                                               regrouper=seance and ancien_encodage == "utf-8")
                file.write_text(content, encoding="utf-8", newline="\n")
            except OSError:
                error = FICHIER_INACCESSIBLE + " Ton texte est toujours dans l'éditeur."
                if wants_json():
                    return jsonify(ok=False, error=error), 409
            else:
                commun.noter_visite(key, modifiee=True)
                if wants_json():
                    # Ctrl+S dans l'éditeur : on reste sur la page, avec la nouvelle version.
                    return jsonify(ok=True, version=str(file.stat().st_mtime_ns), url=url_for("wiki.page", path=key),
                                   seance="1" if change or seance else "")
                flash("Page enregistrée.", "success")
                return redirect(url_for("wiki.page", path=key))
    elif file.is_file():
        try:
            content, encodage = lire_note(file)
        except OSError:
            # Surtout pas d'éditeur vide : l'enregistrer effacerait la page.
            flash(FICHIER_INACCESSIBLE, "error")
            return redirect(url_for("wiki.page", path=key))
        content = content.lstrip("﻿")
    else:
        content = ""

    folders = key.split("/")[:-1]
    return render("wiki/edit.html", vault=vault, note=note, key=key, folders=folders, name=key.rsplit("/", 1)[-1],
                  is_new=note is None, content=content, version=version, error=error,
                  autre_encodage=encodage != "utf-8",
                  crumbs=fil_ariane(folders),
                  link_choices=vault.link_choices(),
                  attachment_choices=vault.attachment_choices(uploads.ATTACHMENT_EXTS),
                  tag_choices=[t for t, _ in vault.all_tags()],
                  preview_url=url_for("wiki.preview", path=key), import_url=url_for("wiki.importer"),
                  latex_url=url_for("wiki.latex"),
                  ameliorer_url=url_for("ai.ameliorer") if commun.ai_dispo() else "")


@bp.route("/apercu/<path:path>", methods=["POST"])
def preview(path):
    """Rendu HTML du texte en cours d'édition (aperçu, côte à côte, lecture)."""
    if not can_edit():
        deny()
    key = clean_key(path) or "Aperçu"
    meta, body = split_frontmatter(request.form.get("content", "").replace("\r\n", "\n"))
    note = Note(PurePosixPath(f"{key}.md"), 0, meta, body)
    return Renderer(get_vault(), note).convert(body)


@bp.route("/latex/", methods=["GET", "POST"])
def latex():
    """Conversion d'un document LaTeX en Markdown : pour l'éditeur (JSON), ou page « Convertir
    un cours LaTeX » qui crée directement la page du wiki."""
    if not can_edit():
        deny()
    if request.method == "GET":
        vault = get_vault()
        return render("wiki/latex.html", vault=vault, folders=_dossiers(vault))
    source = request.form.get("source", "")
    fichier = request.files.get("fichier")
    if fichier and fichier.filename:
        try:
            source = fichier.read().decode("utf-8-sig")
        except UnicodeDecodeError:
            source = ""
    erreur = ""
    if not source.strip():
        erreur = "Aucun texte LaTeX à convertir."
    else:
        try:
            markdown_ = latex_converter.convert(source.replace("\r\n", "\n"))
        except (RecursionError, ValueError, IndexError):
            erreur = "Ce LaTeX n'a pas pu être converti."
    if wants_json():
        if erreur:
            return jsonify(ok=False, error=erreur), 400
        return jsonify(ok=True, markdown=markdown_)
    if erreur:
        flash(erreur, "error")
        return redirect(url_for("wiki.latex"))
    nom = (request.form.get("nom") or (fichier.filename.rsplit(".", 1)[0] if fichier and fichier.filename else "")).strip()
    key = clean_key(f"{request.form.get('dossier', '')}/{nom}") if nom and "/" not in nom else None
    if key is None:
        flash('Nom de page invalide : caractères interdits / \\ : * ? " < > | # ^ [ ]', "error")
        return redirect(url_for("wiki.latex"))
    vault = get_vault()
    file = vault.root / f"{key}.md"
    if key.lower() in vault.notes or file.exists():
        flash("Une page porte déjà ce nom : choisis-en un autre.", "error")
        return redirect(url_for("wiki.latex"))
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(markdown_, encoding="utf-8", newline="\n")
    commun.noter_visite(key, modifiee=True)
    flash("Cours LaTeX converti en page.", "success")
    return redirect(url_for("wiki.page", path=key))


def _dossiers(vault):
    """Dossiers proposés pour ranger une page (hors dossier des QCM)."""
    return [f for f in vault.folders() if not commun.est_qcm(f)]


@bp.route("/nouvelle/")
def new():
    """Formulaire « Nouvelle page » : choisit un nom et un dossier."""
    if not can_edit():
        deny()
    vault = get_vault()
    name = request.args.get("name", "").strip()
    folder = request.args.get("folder", "").strip()
    error = ""
    if name:
        key = clean_key(f"{folder}/{name}")
        if key is None or "/" in name:
            error = 'Le nom ne doit pas contenir les caractères / \\ : * ? " < > | # ^ [ ]'
        elif key.lower() in vault.notes:
            error = "Une page porte déjà ce nom : choisis-en un autre."
        else:
            return redirect(url_for("wiki.edit", path=key))
    return render("wiki/new.html", vault=vault, folders=_dossiers(vault), name=name, folder=folder, error=error)


@bp.route("/supprimer-page/<path:path>", methods=["POST"])
def supprimer(path):
    """Met la page à la corbeille du coffre (.trash/, comme Obsidian) ; son texte reste dans l'historique."""
    if not can_edit():
        deny()
    vault = get_vault()
    note = vault.notes.get(path.lower().removesuffix(".md"))
    if note is None:
        abort(404)
    source = vault.root / note.path
    try:
        texte, _ = lire_note(source)
    except OSError:
        flash(FICHIER_INACCESSIBLE, "error")
        return redirect(note.url)
    historique.sauvegarder(note.path.as_posix(), texte, current_user.username)
    corbeille = vault.root / ".trash"
    corbeille.mkdir(exist_ok=True)
    cible = corbeille / note.path.name
    i = 1
    while cible.exists():
        cible = corbeille / f"{note.path.stem} {i}{note.path.suffix}"
        i += 1
    source.replace(cible)
    flash(f"« {note.title} » est dans la corbeille du coffre (.trash).", "success")
    return redirect(url_for("wiki.home"))


# ---------------------------------------------------------------------------
# Import de fichiers
# ---------------------------------------------------------------------------

@bp.route("/importer/", methods=["GET", "POST"])
def importer():
    """Import de notes .md (et de leurs images, PDF…) par glisser-déposer.

    Champs : ``fichiers`` (les fichiers), ``chemins`` (chemin relatif de chacun, pour garder
    les sous-dossiers d'un dossier déposé), ``dossier`` (destination), ``remplacer`` (écraser
    les fichiers existants), ``pour_note`` (image collée dans l'éditeur : rangée comme
    Obsidian, sous un nom libre)."""
    if not can_edit():
        deny()
    vault = get_vault()
    if request.method != "POST":
        from . import images_externes as module
        images, pages = module.inventaire(vault)
        return render("wiki/import.html", vault=vault, folders=_dossiers(vault),
                      folder=request.args.get("dossier", ""),
                      max_note=uploads.MAX_NOTE_SIZE // 1024 // 1024,
                      max_attachment=uploads.MAX_ATTACHMENT_SIZE // 1024 // 1024,
                      images_externes={"images": images, "pages": pages, "etat": module.etat(vault.root)})

    files = [uploads.FichierEnvoye(f) for f in request.files.getlist("fichiers")]
    paths = request.form.getlist("chemins")
    note_key = clean_key(request.form.get("pour_note", ""))
    rename = note_key is not None
    folder = vault.attachment_folder(note_key) if rename else uploads.clean_folder(request.form.get("dossier", ""))
    error = ""
    if folder is None:
        error = f"Dossier de destination invalide : caractères interdits {uploads.FORBIDDEN_CHARS}, ou dossier caché."
    elif not files:
        error = "Aucun fichier reçu."
    if error:
        if wants_json():
            return jsonify(ok=False, error=error), 400
        flash(error, "error")
        return redirect(url_for("wiki.importer"))

    replace = request.form.get("remplacer") == "1"
    results = [
        uploads.import_file(vault, upload, paths[i] if i < len(paths) else "", folder, replace=replace, rename=rename,
                            auteur=current_user.username)
        for i, upload in enumerate(files)
    ]
    results = uploads.finish(get_vault(), results)
    if request.form.get("claude") == "1" and not rename and commun.ai_dispo():
        # Cours importés (PDF, LaTeX) transformés en pages par l'IA, en arrière-plan (module hangar.ai).
        from ..ai import travaux
        travaux.transformer_cours(results, current_user, lot=request.form.get("lot") == "1")
    if wants_json():
        return jsonify(ok=True, results=[r.as_dict() for r in results])

    done = [r for r in results if r.ok]
    if done:
        notes = sum(r.is_note for r in done)
        pl = "s" if len(done) > 1 else ""
        flash(f"{len(done)} fichier{pl} importé{pl} dont {notes} page{'s' if notes > 1 else ''}.", "success")
    for r in results:
        if not r.ok and not r.ignored:
            flash(f"{r.name} : {r.message}", "error")
    if len(done) == 1 and done[0].url and (done[0].is_note or done[0].path.lower().endswith(".pdf")):
        return redirect(done[0].url)
    return redirect(url_for("wiki.importer"))


@bp.route("/importer/images-externes/", methods=["POST"])
def images_externes():
    """Télécharge dans le coffre les images hébergées sur d'autres sites (action volontaire, en
    arrière-plan) et réécrit les pages qui les citent ; le résultat s'affiche dans la page Importer."""
    if not can_edit():
        deny()
    from . import images_externes as module
    if module.lancer(current_user.username):
        flash("Téléchargement des images lancé : le résultat s'affiche ici (recharge la page pour suivre).", "success")
    else:
        flash("Le téléchargement des images est déjà en cours.", "info")
    return redirect(url_for("wiki.importer") + "#images-externes")


# ---------------------------------------------------------------------------
# Parcourir : sommaire, tags, recherche, graphe
# ---------------------------------------------------------------------------

def _sous_arbre(tree, dossier):
    node = tree
    for part in [p for p in dossier.split("/") if p]:
        node = next((f for f in node["folders"] if f["name"].lower() == part.lower()), None)
        if node is None:
            return None
    return node


@bp.route("/sommaire/")
def sommaire():
    vault = get_vault()
    dossier = request.args.get("dossier", "").strip("/")
    root = _sous_arbre(vault.tree, dossier) if dossier else vault.tree
    if root is None:
        abort(404, description="Dossier introuvable")
    if not dossier:
        root = dict(root, folders=[f for f in root["folders"] if f["name"].lower() != commun.DOSSIER_QCM.lower()])
    return render("wiki/sommaire.html", vault=vault, root=root, dossier=dossier,
                  crumbs=fil_ariane(dossier.split("/")[:-1]) if dossier else [],
                  total=len(vault.notes), recent=vault.recent_notes(8),
                  illisibles=vault.illisibles if can_edit() else [])


@bp.route("/tags/")
def tags():
    vault = get_vault()
    return render("wiki/tags.html", vault=vault, tags=vault.all_tags())


@bp.route("/tags/<path:tag>")
def tag(tag):
    vault = get_vault()
    notes = vault.notes_with_tag(tag)
    if not notes:
        abort(404, description="Tag inconnu")
    return render("wiki/tag.html", vault=vault, tag=tag, notes=notes)


@bp.route("/recherche/")
def search():
    """Recherche dans les notes et dans le texte des PDF (page par page)."""
    vault = get_vault()
    query = request.args.get("q", "").strip()
    results = vault.search(query) if query else []
    pdfs = texte_pdf.chercher(vault, query) if query else []
    if len(results) == 1 and not pdfs and results[0][0].title.lower() == query.lower():
        return redirect(results[0][0].url)
    restants = len(texte_pdf.a_indexer(vault))
    if restants:
        texte_pdf.lancer()
    return render("wiki/search.html", vault=vault, query=query, results=results, pdfs=pdfs, pdf_restants=restants)


def resultats_palette(texte, limite=6):
    """Pages du wiki pour la recherche globale (Ctrl+K) de Hangar."""
    vault = get_vault()
    return [{"title": note.title, "sub": "Wiki" + (f" · {note.folder}" if note.folder else ""),
             "emoji": commun.icone(note), "url": note.url}
            for note, _ in vault.search(texte, limit=limite)]


@bp.route("/graphe/")
def graphe():
    """Graphe des liens entre pages (``?page=<clé>`` : centré sur une page et ses voisines)."""
    vault = get_vault()
    centre = vault.notes.get(request.args.get("page", "").lower())
    return render("wiki/graphe.html", vault=vault, centre=centre)


@bp.route("/graphe.json")
def graphe_donnees():
    """Pages (titre, adresse, dossier) et liens entre elles, pour graphe.js."""
    vault = get_vault()
    notes = vault.sorted_notes()
    index = {n.key.lower(): i for i, n in enumerate(notes)}
    liens = set()
    for cle, sources in vault.backlinks.items():
        j = index.get(cle)
        if j is None:
            continue
        for source in sources:
            i = index[source.key.lower()]
            liens.add((min(i, j), max(i, j)))
    noeuds = [{"t": n.title, "u": n.url, "k": n.key, "d": n.folders[0] if n.folders else ""} for n in notes]
    return jsonify(noeuds=noeuds, liens=sorted(liens))


# ---------------------------------------------------------------------------
# Historique des pages
# ---------------------------------------------------------------------------

@bp.route("/historique/")
def historique_global():
    """Dernières modifications du wiki : versions gardées et pages modifiées récemment."""
    vault = get_vault()
    versions = []
    for v in historique.recentes(60):
        note = vault.notes.get(v["page"].lower().removesuffix(".md"))
        versions.append({**v, "note": note})
    return render("wiki/historique_global.html", vault=vault, versions=versions, recent=vault.recent_notes(20))


@bp.route("/historique/<path:path>", endpoint="historique", methods=["GET", "POST"])
def historique_page(path):
    """Versions précédentes d'une page : différences avec la page actuelle, restauration."""
    vault = get_vault()
    note = vault.notes.get(path.lower().removesuffix(".md"))
    if note is None:
        abort(404, description="Page introuvable")
    rel = note.path.as_posix()
    fichier = vault.root / note.path
    versions = historique.lister(rel)
    if request.method == "POST":
        if not can_edit():
            deny()
        if request.form.get("action") == "restaurer":
            contenu = historique.lire(rel, request.form.get("v", ""))
            if contenu is None:
                abort(404, description="Version introuvable")
            try:
                actuel, _ = lire_note(fichier)
                if actuel != contenu:
                    historique.sauvegarder(rel, actuel, current_user.username)
                    fichier.write_text(contenu, encoding="utf-8", newline="\n")
                    commun.noter_visite(note.key, modifiee=True)
            except OSError:
                flash(FICHIER_INACCESSIBLE, "error")
            else:
                flash("Version restaurée ; la version précédente reste dans l'historique.", "success")
        return redirect(note.url)
    choisie = next((v for v in versions if v["id"] == request.args.get("v")), versions[0] if versions else None)
    contenu_version = historique.lire(rel, choisie["id"]) if choisie else ""
    try:
        actuel = lire_note(fichier)[0] if fichier.is_file() else ""
    except OSError:
        flash(FICHIER_INACCESSIBLE, "error")
        return redirect(note.url)
    return render("wiki/historique.html", vault=vault, note=note, versions=versions, choisie=choisie,
                  contenu_version=contenu_version or "", crumbs=fil_ariane(note.folders),
                  diff=historique.differences(contenu_version or "", actuel) if choisie else [],
                  max_versions=historique.MAX_VERSIONS)



# ---------------------------------------------------------------------------
# PDF et pièces jointes
# ---------------------------------------------------------------------------

@bp.route("/pdf/<path:path>")
def pdf(path):
    """Lecteur de PDF (PDF.js) : page complète, ou version compacte avec ``?integre=1`` pour
    les ``![[cours.pdf]]`` des notes."""
    vault = get_vault()
    attachment = vault.attachments.get(path.lower())
    if attachment is None or attachment.suffix != ".pdf":
        abort(404, description="PDF introuvable")
    full = vault.root / attachment.path
    context = {"attachment": attachment, "title": attachment.path.stem, "size": full.stat().st_size if full.is_file() else 0}
    if request.args.get("integre") == "1":
        return render_template("wiki/pdf_integre.html", integre=True, can_edit=False, **context)
    backlinks = sorted(vault.attachment_backlinks.get(str(attachment.path).lower(), ()), key=lambda n: cle_tri(n.title))
    page_cours = vault.notes.get(str(attachment.path.with_suffix("")).lower())
    return render("wiki/pdf.html", vault=vault, backlinks=backlinks, page_cours=page_cours,
                  crumbs=fil_ariane(attachment.path.parent.parts),
                  pages_wiki=sorted((n.key for n in vault.notes.values()), key=cle_tri) if can_edit() else [],
                  **context)


@bp.route("/fichiers/<path:path>")
def file(path):
    vault = get_vault()
    attachment = vault.attachments.get(path.lower())
    if attachment is None:
        abort(404, description="Fichier introuvable")
    full = (vault.root / attachment.path).resolve()
    # Lien symbolique pointant hors du coffre : refusé.
    if not full.is_relative_to(vault.root.resolve()) or not full.is_file():
        abort(404, description="Fichier introuvable")
    inline = attachment.suffix in INLINE_TYPES
    response = send_file(full, as_attachment=not inline, download_name=attachment.name, conditional=True, max_age=0)
    # Même ouvert directement, le fichier ne peut rien exécuter (même règle que les pièces jointes des tickets).
    return protect_file(response, pdf=attachment.suffix == ".pdf")


@bp.route("/pdf-annotation/", methods=["POST"])
def pdf_annotation():
    """Passage sélectionné dans un PDF → carte recto/verso (set « <PDF> — annotations » dans
    QCM/Annotations/) ou citation ajoutée à une page du wiki (créée au besoin)."""
    if not can_edit():
        deny()
    try:
        data = json.loads(request.get_data(as_text=True) or "{}")
        chemin, cible = str(data["pdf"]), str(data.get("cible", "carte"))
        texte = " ".join(str(data.get("texte", "")).split())[:MAX_CHARS_ANNOTATION]
        page_pdf = int(data.get("page") or 0)
    except (KeyError, ValueError, TypeError):
        return jsonify(ok=False, error="requête invalide"), 400
    vault = get_vault()
    pdf_ = vault.attachments.get(chemin.lower())
    if pdf_ is None or pdf_.suffix != ".pdf":
        return jsonify(ok=False, error="PDF introuvable"), 404
    if not texte:
        return jsonify(ok=False, error="Sélectionne d'abord un passage du PDF."), 400
    stem = pdf_.path.stem
    source = f"{stem}, p. {page_pdf}" if page_pdf else stem
    page_cours = vault.notes.get(str(pdf_.path.with_suffix("")).lower())

    if cible == "carte":
        rel = f"{commun.DOSSIER_QCM}/{DOSSIER_ANNOTATIONS}/{stem}.json"
        fichier = vault.root / rel
        if not fichier.resolve().is_relative_to(vault.root.resolve()):
            return jsonify(ok=False, error="chemin invalide"), 400
        if fichier.is_file():
            try:
                set_json = json.loads(fichier.read_text(encoding="utf-8"))
            except ValueError:
                return jsonify(ok=False, error=f"Le fichier {rel} est illisible."), 500
        else:
            set_json = {"titre": f"{stem} — annotations", "description": f"Passages gardés depuis le PDF {pdf_.name}.",
                        "genere_par": "Annotations du lecteur PDF", "questions": []}
            if page_cours is not None:
                set_json["source_page"] = page_cours.key
        recto = str(data.get("recto") or "").strip()[:500] or f"{source} : que dit ce passage ?"
        empreinte = hashlib.sha1(f"{page_pdf}\x1f{texte}".encode("utf-8")).hexdigest()[:10]
        questions = set_json.setdefault("questions", [])
        if not isinstance(questions, list):
            return jsonify(ok=False, error=f"Le fichier {rel} n'est pas un set de questions."), 400
        questions.append({"id": f"a{empreinte}", "type": "FLASHCARD", "question": recto,
                          "verso": f"{texte}\n\n— *{source}*"})
        fichier.parent.mkdir(parents=True, exist_ok=True)
        fichier.write_text(json.dumps(set_json, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        n = len(questions)
        url = ""
        try:
            url = url_for("qcm.index")
        except Exception:  # noqa: BLE001 - section QCM absente
            pass
        return jsonify(ok=True, message=f"Carte ajoutée à « {set_json.get('titre', stem)} » ({n} carte{'s' if n > 1 else ''}).",
                       url=url)

    if cible == "note":
        voulue = str(data.get("page_wiki") or "").strip().removesuffix(".md")
        cle = clean_key(voulue) if voulue else (page_cours.key if page_cours else clean_key(str(pdf_.path.with_suffix(""))))
        if cle is None:
            return jsonify(ok=False, error="Nom de page invalide."), 400
        note = vault.notes.get(cle.lower())
        fichier = vault.root / (note.path if note is not None else PurePosixPath(f"{cle}.md"))
        citation = f"> [!quote] {source}\n> {texte}\n> — [[{vault.link_for(pdf_)}]]\n"
        if fichier.is_file():
            try:
                ancien, _ = lire_note(fichier)
            except OSError:
                return jsonify(ok=False, error=FICHIER_INACCESSIBLE), 409
            # Ajout à la fin : l'ancien texte reste entier dans la page, les citations prises
            # d'affilée peuvent donc partager une version.
            historique.sauvegarder(fichier.relative_to(vault.root).as_posix(), ancien, current_user.username,
                                   edition=True, regrouper=True)
            nouveau = ancien.rstrip("\n") + "\n\n" + citation
            titre = note.title if note is not None else cle.rsplit("/", 1)[-1]
        else:
            fichier.parent.mkdir(parents=True, exist_ok=True)
            titre = cle.rsplit("/", 1)[-1]
            nouveau = f"# {titre}\n\n{citation}"
        fichier.write_text(nouveau, encoding="utf-8", newline="\n")
        return jsonify(ok=True, message=f"Citation ajoutée à « {titre} ».", url=url_for("wiki.page", path=cle))
    return jsonify(ok=False, error="cible inconnue"), 400


def lien_wiki(key: str) -> str:
    """Adresse d'une page du wiki d'après sa clé (pour les autres modules : QCM, IA…)."""
    return url_for("wiki.page", path=key) if key else url_for("wiki.home")


# Routes ajoutées au blueprint : lecture hors ligne (PWA), palette de commandes, documentation,
# erreurs du serveur, anciennes adresses de Revision.
from . import pwa, vues_revision  # noqa: E402,F401

__all__ = ["bp", "resultats_palette", "lien_wiki"]

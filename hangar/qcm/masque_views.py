"""Éditeur de cartes à image masquée : choisir une image du coffre ou en envoyer une,
tracer les zones, les nommer. Le set est écrit dans l'espace personnel (tout compte)
ou dans le coffre, sous QCM/<dossier>/ (admins et membres).

Port de `masque_views.py` de Revision."""
from __future__ import annotations

import json
from pathlib import PurePosixPath

from flask import abort, jsonify, render_template, request, send_file, url_for
from flask_login import current_user, login_required

from . import coffre, masque, paquets, quiz
from .coffre import DOSSIER_QCM, clean_key, invalider
from .gestion import MAX_FICHIERS_PERSO, MAX_OCTETS_PERSO, fichiers_perso, taille_perso
from .services import bibliotheque, peut_modifier
from .views import bp

DOSSIER_DEFAUT = "Images masquées"


def _peut_modifier(qset):
    if qset.proprietaire:
        return qset.proprietaire == quiz.nom_perso(current_user.id)
    return peut_modifier(current_user)


def _question_masque(donnees):
    return next((q for q in donnees.get("questions", []) if isinstance(q, dict) and str(q.get("type", "")).upper() == "MASQUE"), None)


def _images_perso(user_id):
    dossier = quiz.dossier_perso(user_id) / masque.DOSSIER_IMAGES_PERSO
    if not dossier.is_dir():
        return []
    return [{"lien": masque.PERSO_IMAGE + f.name, "dossier": "Mon espace", "url": url_for("qcm.image_perso", nom=f.name)}
            for f in sorted(dossier.iterdir(), key=lambda f: f.name.lower()) if f.suffix.lower() in masque.FORMATS]


def _images_coffre():
    return [{"lien": coffre.lien_court(p), "dossier": str(p.parent) if p.parent.parts else "",
             "url": url_for("qcm.fichier", path=str(p))} for p in coffre.images(masque.FORMATS)]


@bp.route("/qcm/image-masquee/", methods=["GET", "POST"])
@login_required
def masque_editeur():
    """Page de l'éditeur (GET, ``?set=`` pour modifier) ; enregistrement (POST, réponse JSON)."""
    uid = current_user.id
    existant = None
    set_id = request.args.get("set") or request.form.get("set") or ""
    if set_id:
        existant = bibliotheque(uid).get(set_id)
        if existant is None or not existant.est_masque:
            abort(404, description="Set inconnu")
        if not _peut_modifier(existant):
            abort(403)
    if request.method == "POST":
        return _enregistrer(uid, existant)
    donnees = None
    if existant is not None:
        _, contenu = quiz.lire_json(existant.fichier, existant.fichier.parent)
        q = _question_masque(contenu) or {}
        donnees = {"set": existant.id, "titre": existant.titre, "question": q.get("question", ""), "image": q.get("image", ""),
                   "image_url": masque.url_image(existant, str(q.get("image", ""))),
                   "masquer_tout": bool(q.get("masquer_tout", True)), "zones": q.get("zones", []),
                   "jouer": url_for("qcm.play", set=existant.id, mode="all")}
    dossiers = sorted({s.categorie for s in bibliotheque(uid).sets.values() if s.est_masque} | {DOSSIER_DEFAUT}, key=str.lower)
    return render_template(
        "qcm/masque.html", donnees=donnees, images=_images_perso(uid) + _images_coffre(), dossiers=dossiers,
        enonce_defaut=masque.ENONCE_DEFAUT, max_zones=masque.MAX_ZONES, can_edit=peut_modifier(current_user))


def _enregistrer(uid, existant):
    def erreur(message):
        return jsonify(error=message), 400

    try:
        zones = json.loads(request.form.get("zones", "[]"))
    except ValueError:
        return erreur("Zones illisibles.")
    titre = " ".join(request.form.get("titre", "").split())[:120]
    if not titre:
        return erreur("Donne un titre au set.")
    enonce = " ".join(request.form.get("question", "").split())[:300] or masque.ENONCE_DEFAUT
    if existant is not None:
        destination = "perso" if existant.proprietaire else "coffre"
    else:
        destination = request.form.get("destination", "perso")
        if destination not in ("perso", "coffre"):
            destination = "perso"
        if destination == "coffre" and not peut_modifier(current_user):
            abort(403)
    dossier = clean_key(request.form.get("dossier", "").strip() or DOSSIER_DEFAUT)
    if dossier is None or "/" in dossier:
        return erreur('Dossier invalide : un seul nom, sans / \\ : * ? " < > | # ^ [ ].')
    perso_racine = quiz.dossier_perso(uid)
    if destination == "perso" and existant is None and (
            len(fichiers_perso(uid)) >= MAX_FICHIERS_PERSO or taille_perso(uid) >= MAX_OCTETS_PERSO):
        return erreur(f"Ton espace est plein ({MAX_FICHIERS_PERSO} fichiers, {MAX_OCTETS_PERSO // 2**20} Mo) : supprimes-en d'abord.")

    image = request.form.get("image", "").strip()
    fichier = request.files.get("fichier")
    if fichier is not None and fichier.filename:
        try:
            if destination == "perso":
                if taille_perso(uid) >= MAX_OCTETS_PERSO:
                    return erreur(f"Ton espace est plein ({MAX_OCTETS_PERSO // 2**20} Mo).")
                image = masque.PERSO_IMAGE + masque.enregistrer_image(fichier, perso_racine / masque.DOSSIER_IMAGES_PERSO)
            else:
                dossier_images = coffre.dossier_pieces_jointes(f"{DOSSIER_QCM}/{dossier}/schema")
                racine = coffre.racine()
                nom = masque.enregistrer_image(fichier, racine / dossier_images if dossier_images else racine)
                image = f"{dossier_images}/{nom}" if dossier_images else nom
                invalider()
        except ValueError as e:
            return erreur(str(e))
    if not image:
        return erreur("Choisis une image.")
    if image.startswith(masque.PERSO_IMAGE):
        if destination != "perso":
            return erreur("Une image de ton espace ne peut servir qu'à un set de ton espace.")
        if not (perso_racine / masque.DOSSIER_IMAGES_PERSO / image[len(masque.PERSO_IMAGE):]).is_file():
            return erreur("Image introuvable dans ton espace.")
    elif coffre.resoudre(image) is None:
        return erreur(f"Image introuvable dans le coffre : {image}")

    question = {"id": "masque", "type": "MASQUE", "question": enonce, "image": image,
                "masquer_tout": request.form.get("masquer_tout") == "1", "zones": zones}
    if existant is not None:
        _, contenu = quiz.lire_json(existant.fichier, existant.fichier.parent)
        ancienne = _question_masque(contenu)
        contenu["titre"] = titre
        contenu["questions"] = [question if q is ancienne else q for q in contenu.get("questions", [])]
    else:
        contenu = {"titre": titre, "description": "Cartes à image masquée.", "questions": [question]}
    try:
        quiz.set_depuis_json(contenu, PurePosixPath("set.json"))
    except ValueError as e:
        return erreur(str(e).replace("set.json » ", "").replace("question #1 » ", ""))

    if existant is not None:
        cible = existant.fichier
    elif destination == "perso":
        cible = perso_racine / dossier / masque.nom_libre(perso_racine / dossier, (paquets.slug(titre) or "image-masquee") + ".json")
    else:
        cible = coffre.racine() / paquets.chemin_libre(dossier, titre, "image-masquee")
    cible.parent.mkdir(parents=True, exist_ok=True)
    cible.write_text(json.dumps(contenu, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    invalider()
    qset = next((s for s in quiz.get_library().pour(uid).sets.values() if s.fichier == cible), None)
    if qset is None:
        return erreur("Set enregistré mais introuvable : recharge la page.")
    n = len(zones)
    return jsonify(ok=True, set=qset.id, image=image, image_url=masque.url_image(qset, image),
                   jouer=url_for("qcm.play", set=qset.id, mode="all"),
                   message=f"« {titre} » enregistré : {n} carte{'s' if n > 1 else ''}.")


@bp.route("/qcm/mon-espace/images/<nom>")
@login_required
def image_perso(nom):
    """Image de l'espace personnel du compte connecté (seul lui la voit)."""
    if "/" in nom or "\\" in nom or nom.startswith("."):
        abort(404)
    chemin = quiz.dossier_perso(current_user.id) / masque.DOSSIER_IMAGES_PERSO / nom
    if chemin.suffix.lower() not in masque.FORMATS or not chemin.is_file():
        abort(404)
    rep = send_file(chemin, max_age=86400)
    rep.headers["Cache-Control"] = "private, max-age=86400"
    return rep

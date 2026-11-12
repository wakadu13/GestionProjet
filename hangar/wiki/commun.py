"""Outils communs aux vues du wiki : droits, réponses JSON, menu de la section (collections,
favoris, récents), contexte transmis au panneau IA.

Port de ``wiki/vues_communes.py`` de Revision, adapté aux comptes de Hangar : lecture pour
tous les comptes, modification pour les administrateurs et les membres (pas les invités).
"""
from __future__ import annotations

from urllib.parse import unquote

from flask import abort, g, render_template, request
from flask_login import current_user

from ..extensions import db
from ..models import now
from .vault import get_vault

DOSSIER_QCM = "QCM"  # sets de QCM du coffre : rangés dans leur propre section, pas dans les collections


def can_edit(user=None) -> bool:
    """Modifier le wiki : administrateurs et membres, pas les invités."""
    user = user or current_user
    return bool(user and user.is_authenticated and not user.is_guest)


def deny():
    abort(403, description="Les invités peuvent lire le wiki mais pas le modifier.")


def wants_json() -> bool:
    """Requête envoyée par le JavaScript de l'éditeur ou de l'import (réponse JSON)."""
    return ("application/json" in request.headers.get("Accept", "")
            or request.headers.get("X-Requested-With") == "fetch")


def ai_dispo() -> bool:
    """Un fournisseur d'IA est configuré (les boutons d'IA ne s'affichent qu'alors)."""
    try:
        from .. import ai
        return bool(ai.disponible())
    except Exception:  # l'assistant ne doit jamais empêcher une page de s'afficher
        return False


COOKIE_MENU = "wiki_menu"


def menu_epingle() -> bool:
    """Menu de gauche gardé affiché pendant la lecture (bouton ▯ du menu du wiki, choix retenu par cookie)."""
    return request.cookies.get(COOKIE_MENU) == "epingle"


def render(template, **context):
    vault = context.pop("vault", None) or get_vault()
    return render_template(template, vault=vault, can_edit=can_edit(), ai_dispo=ai_dispo(), **context)


def est_qcm(chemin: str) -> bool:
    return chemin.split("/", 1)[0].lower() == DOSSIER_QCM.lower()


# ---------------------------------------------------------------------------
# Pages courantes, favoris et récents
# ---------------------------------------------------------------------------

def page_courante(vault=None):
    """(note, pièce jointe) affichée par la requête en cours, pour le menu de gauche."""
    if "wiki_courante" in g:
        return g.wiki_courante
    vault = vault or get_vault()
    ep = request.endpoint or ""
    path = (request.view_args or {}).get("path", "")
    note = attachment = None
    if ep == "wiki.home":
        note = vault.home()
    elif ep in ("wiki.page", "wiki.edit", "wiki.historique") and path:
        note = vault.notes.get(path.lower().removesuffix(".md"))
    elif ep == "wiki.pdf" and path:
        attachment = vault.attachments.get(path.lower())
    g.wiki_courante = (note, attachment)
    return g.wiki_courante


ENTETE_PRECHARGEMENT = "X-Hangar-Prechargement"


def prechargement() -> bool:
    """Page demandée par « ⤓ Hors ligne » (hors_ligne_wiki.js) pour être gardée sur l'appareil, pas
    ouverte par le compte : elle n'entre pas dans les « Récents », et ses parties personnelles (QCM de
    la page, échanges avec le tuteur) ne sont ni calculées ni gardées dans le cache."""
    return request.headers.get(ENTETE_PRECHARGEMENT) == "1"


def noter_visite(key, modifiee=False):
    """Retient la page (sa clé) dans les « Récents » du compte (vue ou modifiée)."""
    from .models import WikiVisit
    if not current_user.is_authenticated or not key:
        return
    try:
        v = WikiVisit.query.filter_by(user_id=current_user.id, key=key).first()
        if v is None:
            v = WikiVisit(user_id=current_user.id, key=key, views=0)
            db.session.add(v)
        v.visited_at = now()
        v.views = (v.views or 0) + 1
        if modifiee:
            v.edited_at = now()
        db.session.commit()
    except Exception:  # noqa: BLE001 - une base occupée ne doit pas empêcher la lecture
        db.session.rollback()


def est_favori(note) -> bool:
    from .models import WikiFavorite
    if note is None or not current_user.is_authenticated:
        return False
    return WikiFavorite.query.filter_by(user_id=current_user.id, key=note.key).first() is not None


def sidebar_data():
    """Données du menu de la section Wiki (calculées une fois par requête)."""
    if "wiki_sidebar" in g:
        return g.wiki_sidebar
    from .models import WikiFavorite, WikiVisit
    vault = get_vault()
    note, attachment = page_courante(vault)
    tree = vault.tree
    # Dossiers ouverts : ceux qui contiennent la page ou le PDF affiché.
    parts = note.folders if note is not None else (attachment.path.parent.parts if attachment is not None else ())
    ouverts, chemin = set(), ""
    for part in parts:
        chemin += part + "/"
        ouverts.add(chemin)
    favoris, recents = [], []
    if current_user.is_authenticated:
        for f in WikiFavorite.query.filter_by(user_id=current_user.id).order_by(WikiFavorite.created_at):
            n = vault.notes.get(f.key.lower())
            if n is not None:
                favoris.append(n)
        for v in (WikiVisit.query.filter_by(user_id=current_user.id)
                  .order_by(WikiVisit.visited_at.desc()).limit(12)):
            n = vault.notes.get(v.key.lower())
            if n is not None and len(recents) < 8:
                recents.append(n)
    home = vault.home()
    g.wiki_sidebar = {
        "collections": [f for f in tree["folders"] if f["name"].lower() != DOSSIER_QCM.lower()],
        "racine": [n for n in tree["notes"] if n is not home],
        "racine_pdf": tree["files"],
        "ouverts": ouverts,
        "note": note,
        "attachment": attachment,
        "favoris": favoris,
        "recents": recents,
        "home": home,
        "total": len(vault.notes),
        "can_edit": can_edit(),
        "ai": ai_dispo(),
        # Pages de lecture : menu caché, sauf s'il a été épinglé (cookie posé par plein_ecran.js).
        "menu_epingle": menu_epingle(),
    }
    return g.wiki_sidebar


def icone(note) -> str:
    """Emoji de la page (propriété ``icon`` ou ``emoji``), comme l'icône des pages de Plane."""
    if note is None:
        return ""
    valeur = note.meta.get("icon") or note.meta.get("emoji") or ""
    valeur = str(valeur).strip()
    return valeur if 0 < len(valeur) <= 8 else ""


# ---------------------------------------------------------------------------
# Contexte pour le panneau IA (hangar.ai.register_context)
# ---------------------------------------------------------------------------

def contexte_ia(path, args, user):
    """Décrit la page du wiki affichée (adresse ``/wiki/<page>`` ou ``/modifier/<page>``)
    pour le panneau IA : {type, key, title, url, text} (texte Markdown sans les propriétés)."""
    try:
        path = unquote(str(path or ""))
        for prefixe in ("/wiki/", "/modifier/", "/historique/"):
            if path == prefixe.rstrip("/") or path.startswith(prefixe):
                cle = path[len(prefixe):].strip("/")
                break
        else:
            return None
        vault = get_vault()
        if not cle:
            if prefixe != "/wiki/":
                return None
            note = vault.home()
        else:
            note = vault.notes.get(cle.lower().removesuffix(".md"))
            if note is None:
                from .vault import Note
                cible = vault.resolve(cle)
                note = cible if isinstance(cible, Note) else None
        if note is None:
            return None
        return {"type": "wiki_page", "key": note.key, "title": note.title, "url": note.url, "text": note.body}
    except Exception:  # noqa: BLE001 - le panneau IA ne doit pas casser la page
        return None

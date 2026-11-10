"""Modèles du wiki (SQLAlchemy).

Les pages elles-mêmes sont des fichiers .md du coffre (``WIKI_DIR``) : la base ne garde
que ce qui est propre à chaque compte (favoris, pages récentes) et l'index du texte des
PDF pour la recherche. Une page est désignée par sa clé (chemin dans le coffre, sans .md).
"""
from __future__ import annotations

from ..extensions import db
from ..models import now


class WikiFavorite(db.Model):
    """Page du wiki mise en favori (étoile) par un compte."""

    __tablename__ = "wiki_favorite"
    __table_args__ = (db.UniqueConstraint("user_id", "key", name="uq_wiki_favorite"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    key = db.Column(db.String(500), nullable=False)
    created_at = db.Column(db.DateTime, default=now)


class WikiVisit(db.Model):
    """Dernière visite (et dernière modification) d'une page par un compte : section « Récents »."""

    __tablename__ = "wiki_visit"
    __table_args__ = (db.UniqueConstraint("user_id", "key", name="uq_wiki_visit"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    key = db.Column(db.String(500), nullable=False)
    visited_at = db.Column(db.DateTime, default=now, index=True)
    edited_at = db.Column(db.DateTime)
    views = db.Column(db.Integer, default=0)


class WikiPdfText(db.Model):
    """Texte d'un PDF du coffre, page par page (recherche, « Demander au wiki »).

    Port du modèle ``TextePdf`` de Revision : un PDF n'est relu que si sa signature
    (date et taille du fichier) change."""

    __tablename__ = "wiki_pdf_text"

    id = db.Column(db.Integer, primary_key=True)
    chemin = db.Column(db.String(500), unique=True, nullable=False)
    signature = db.Column(db.String(60), default="")
    pages = db.Column(db.JSON, default=list)
    erreur = db.Column(db.String(300), default="")
    indexe_le = db.Column(db.DateTime, default=now, onupdate=now)


class ErreurServeur(db.Model):
    """Erreur du serveur (page en erreur 500, incident du fil des travaux de l'IA), regroupée par empreinte
    (même type, même message, même endroit du code) : page ``/erreurs/`` des administrateurs.

    Port du modèle ``ErreurServeur`` de Revision ; voir ``hangar.wiki.erreurs``."""

    __tablename__ = "erreur_serveur"

    id = db.Column(db.Integer, primary_key=True)
    empreinte = db.Column(db.String(40), unique=True, nullable=False)
    type = db.Column(db.String(200), default="")
    message = db.Column(db.Text, default="")
    lieu = db.Column(db.String(500), default="")         # fichier:ligne du code
    chemin = db.Column(db.String(500), default="")       # adresse demandée (dernière occurrence)
    methode = db.Column(db.String(10), default="")
    utilisateur = db.Column(db.String(150), default="")
    trace = db.Column(db.Text, default="")
    nombre = db.Column(db.Integer, default=1)
    premiere = db.Column(db.DateTime, default=now)
    derniere = db.Column(db.DateTime, default=now, index=True)
    reglee = db.Column(db.Boolean, default=False, index=True)

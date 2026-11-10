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

"""Adresses des pages, fichiers et tags du wiki.

``url_for`` a besoin d'une requête en cours (ou de SERVER_NAME) : hors requête
(commande en ligne, fil d'arrière-plan qui prépare des sources pour l'IA…),
l'adresse est construite à la main, de la même façon que Flask.
"""
from __future__ import annotations

from urllib.parse import quote

from flask import url_for

# Point d'entrée -> (préfixe de l'adresse, nom du paramètre de la route)
ROUTES = {
    "wiki.page": ("/wiki/", "path"),
    "wiki.file": ("/fichiers/", "path"),
    "wiki.pdf": ("/pdf/", "path"),
    "wiki.tag": ("/tags/", "tag"),
    "wiki.edit": ("/modifier/", "path"),
    "wiki.historique": ("/historique/", "path"),
    "wiki.preview": ("/apercu/", "path"),
}


def wiki_url(endpoint: str, value: str) -> str:
    """Adresse d'une page (``wiki.page``), d'un fichier, d'un PDF ou d'un tag du coffre."""
    prefix, param = ROUTES[endpoint]
    try:
        return url_for(endpoint, **{param: value})
    except RuntimeError:  # hors requête : même résultat que le convertisseur « path » de Werkzeug
        return prefix + quote(value, safe="/")

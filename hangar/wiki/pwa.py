"""Lecture hors ligne du wiki (PWA) : service worker à la racine, manifeste, page « hors connexion »,
pages et PDF d'un dossier à garder sur l'appareil (port de ``wiki/pwa_views.py`` de Revision).

Le service worker (``/sw.js``, portée ``/``) ne s'occupe que des adresses de lecture du wiki (pages,
PDF, sommaire, tags, graphe, documentation, fichiers du coffre) et des fichiers statiques : les autres
sections de Hangar, les requêtes POST et l'API passent sans lui. Les QCM hors ligne ont leur propre
application (``/qcm/…``), jamais touchée par ce service worker.
"""
from __future__ import annotations

import json

from flask import abort, jsonify, make_response, render_template, request, url_for

from .. import __version__
from .render import IMAGE_EXTS
from .vault import get_vault
from .views import bp

CACHE = "hangar-wiki-v1"
THEME = "#7852ee"


def _statique(nom: str, versionne: bool = True) -> str:
    """Adresse d'un fichier statique, telle que les gabarits l'écrivent : ``url_for`` y ajoute l'empreinte
    du fichier (``?v=…``, web.py). Le cache du service worker est indexé par l'adresse exacte ;
    ``versionne=False`` pour un fichier demandé sans ``?v=`` (polices de KaTeX lues par sa feuille de
    style, module et décodeurs de PDF.js importés par adresse relative)."""
    return url_for("static", filename=nom) if versionne else url_for("static", filename=nom, v=None)


def fichiers_lecteur_pdf() -> list[str]:
    """Scripts et feuilles du lecteur de PDF (PDF.js), pour lire un PDF gardé hors ligne."""
    from pathlib import Path
    wasm = Path(__file__).resolve().parent.parent / "static" / "wiki" / "vendor" / "pdfjs" / "wasm"
    return [_statique("wiki/lecteur_pdf.js"), _statique("wiki/vendor/pdfjs/pdf.min.js", False),
            _statique("wiki/vendor/pdfjs/pdf.worker.min.js"),
            _statique("wiki/vendor/pdfjs/pdf_viewer.min.css")] + sorted(
        _statique(f"wiki/vendor/pdfjs/wasm/{f.name}", False) for f in wasm.glob("*.wasm"))


def a_precharger() -> list[str]:
    """Ce que le service worker garde dès son installation : la page « hors connexion » et ce qu'il faut
    pour afficher une page du wiki (feuilles de style, scripts, formules, coloration du code, icônes)."""
    from pathlib import Path

    from ..web import adresse_icones
    from .views import vendor_wiki
    fonts = Path(__file__).resolve().parent.parent / "static" / "vendor" / "katex" / "fonts"
    vendor = vendor_wiki()
    return [
        url_for("wiki.hors_connexion"), url_for("wiki.manifeste"), adresse_icones(),
        _statique("css/app.css"), _statique("js/app.js"), _statique("js/appareil.js"),
        _statique("ai/ai.css"), _statique("ai/ai.js"),
        _statique("wiki/wiki.css"), _statique("wiki/wiki.js"), _statique("wiki/plein_ecran.js"),
        _statique("wiki/palette.js"), _statique("wiki/tuteur.js"), _statique("wiki/hors_ligne_wiki.js"),
        _statique("wiki/menu_wiki.js"),
        _statique("wiki/icone-192.png"), _statique("wiki/icone-512.png"),
        _statique("vendor/highlight/github.min.css"), _statique("vendor/highlight/github-dark.min.css"),
        vendor["highlight/highlight.min.js"], vendor["katex/katex.min.css"], vendor["katex/katex.min.js"],
    ] + sorted(_statique(f"vendor/katex/fonts/{f.name}", False) for f in fonts.glob("*.woff2"))


@bp.route("/sw.js")
def service_worker():
    """Service worker du wiki (sans secret), servi à la racine pour pouvoir garder aussi /pdf/ et /fichiers/."""
    corps = render_template("wiki/sw.js", cache=CACHE, hors_connexion=url_for("wiki.hors_connexion"),
                            precharger=a_precharger(), version=__version__)
    r = make_response(corps)
    r.headers["Content-Type"] = "text/javascript; charset=utf-8"
    r.headers["Cache-Control"] = "no-cache"
    r.headers["Service-Worker-Allowed"] = "/"
    return r


@bp.route("/manifest.webmanifest")
def manifeste():
    """Manifeste de l'application installable (écran d'accueil d'un téléphone, iPhone compris)."""
    data = {
        "name": "Hangar", "short_name": "Hangar",
        "description": "Agenda, cours, QCM et projets : le wiki se lit même sans connexion.",
        "start_url": url_for("wiki.home"), "scope": "/", "display": "standalone",
        "background_color": "#ffffff", "theme_color": THEME, "lang": "fr",
        "icons": [
            {"src": _statique("wiki/icone-192.png"), "sizes": "192x192", "type": "image/png",
             "purpose": "any maskable"},
            {"src": _statique("wiki/icone-512.png"), "sizes": "512x512", "type": "image/png",
             "purpose": "any maskable"},
        ],
    }
    r = make_response(json.dumps(data, ensure_ascii=False))
    r.headers["Content-Type"] = "application/manifest+json"
    return r


@bp.route("/hors-connexion/")
def hors_connexion():
    """Page servie par le service worker quand une page du wiki n'est ni accessible ni en cache : elle liste
    ce qui est lisible sur l'appareil (aucun contenu du wiki dans la page elle-même)."""
    return render_template("wiki/hors_connexion.html", cache=CACHE, theme_color=THEME)


@bp.route("/hors-ligne/dossier.json")
def dossier_hors_ligne():
    """Adresses à mettre en cache pour lire un dossier sans connexion : ses pages, images et PDF (et le
    lecteur de PDF s'il y en a)."""
    vault = get_vault()
    dossier = request.args.get("dossier", "").replace("\\", "/").strip("/")
    prefixe = f"{dossier}/" if dossier else ""
    pages = [n.url for n in vault.sorted_notes() if n.path.as_posix().startswith(prefixe)]
    fichiers, pdf = [], 0
    for a in sorted(vault.attachments.values(), key=lambda a: str(a.path).lower()):
        if not a.path.as_posix().startswith(prefixe) or a.suffix not in IMAGE_EXTS | {".pdf"}:
            continue
        fichiers.append(a.url)
        if a.suffix == ".pdf":
            fichiers.append(a.view_url)
            pdf += 1
    if not pages and not fichiers:
        abort(404, description="Dossier introuvable ou vide")
    outils = fichiers_lecteur_pdf() if pdf else []
    return jsonify(dossier=dossier, urls=pages + fichiers + outils, pages=len(pages), pdf=pdf, cache=CACHE)

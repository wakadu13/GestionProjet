"""Mettre Hangar en ligne : derrière un proxy (Caddy, nginx…) ou un tunnel Cloudflare, en HTTPS.

Réglages lus une fois, au démarrage (``create_app``), dans des variables d'environnement que
``uv run hangar run --cloudflare``, ``--behind-proxy`` et ``--https`` posent aussi :

- ``HANGAR_CLOUDFLARE=1`` : tunnel Cloudflare ; l'adresse du visiteur est lue dans CF-Connecting-IP ;
- ``HANGAR_BEHIND_PROXY=1`` : autre proxy ; adresse du visiteur = dernière adresse de X-Forwarded-For ;
- ``HANGAR_HTTPS=1`` : les visiteurs arrivent en HTTPS ; cookies « Secure » et en-tête HSTS.

Sans ces variables (cas normal : cet ordinateur ou le réseau local en http), aucun en-tête de proxy n'est cru :
un visiteur ne peut pas se faire passer pour une autre adresse. Avec un proxy, ``ProxyFix`` fait voir à
Hangar l'adresse publique (https://…, liens des jetons d'API et des exports) et l'adresse du visiteur ;
``blocage_connexion.ip_client`` lit les mêmes en-têtes, de la même façon.
"""
from __future__ import annotations

import os

VARIABLES = ("HANGAR_CLOUDFLARE", "HANGAR_BEHIND_PROXY", "HANGAR_HTTPS")
HSTS = "max-age=31536000"  # un an ; sans includeSubDomains : le domaine peut servir d'autres sites


def actif(nom: str) -> bool:
    return os.environ.get(nom, "").strip().lower() in ("1", "true", "oui", "yes")


def init_app(app) -> None:
    cloudflare = actif("HANGAR_CLOUDFLARE")
    proxy = cloudflare or actif("HANGAR_BEHIND_PROXY")
    https = actif("HANGAR_HTTPS")
    app.config.update(HANGAR_PROXY=proxy, HANGAR_HTTPS=https)
    if https:
        app.config.update(SESSION_COOKIE_SECURE=True, REMEMBER_COOKIE_SECURE=True)
    if proxy:
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    if https:
        @app.after_request
        def _hsts(response):
            response.headers.setdefault("Strict-Transport-Security", HSTS)
            return response


ENTETES_PROXY = ("X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "X-Real-IP", "Forwarded",
                 "CF-Connecting-IP")


def via_proxy(app, request) -> bool:
    """La requête passe-t-elle par un proxy ou un tunnel (réglé, ou non : en-têtes de transfert présents) ?"""
    return bool(app.config.get("HANGAR_PROXY")) or any(h in request.headers for h in ENTETES_PROXY)

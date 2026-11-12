"""Réponses HTTP de Hangar : en-têtes de sécurité, cache des fichiers statiques, icônes, compression.

- **En-têtes de sécurité** sur chaque réponse : ``X-Content-Type-Options: nosniff``,
  ``X-Frame-Options: SAMEORIGIN``, ``Referrer-Policy: same-origin`` et une politique de contenu (CSP) :
  scripts du site seulement, et les blocs ``<script>`` écrits dans les gabarits portent le jeton
  ``nonce="{{ csp_nonce() }}"`` (sans lui, un script injecté dans une page ne s'exécute pas) ; aucune
  connexion vers un autre site. ``HANGAR_CSP=rapport`` la passe en mode « rapport seulement » (les
  blocages sont seulement signalés dans la console du navigateur), ``HANGAR_CSP=non`` l'enlève.
- **Fichiers statiques** : ``url_for('static', filename=…)`` ajoute ``?v=`` suivi d'une empreinte du
  fichier (date et taille) ; une adresse ainsi versionnée est gardée un an par le navigateur, une mise à
  jour du fichier (même sans changement de version de Hangar) change l'adresse. Les autres réponses
  ``send_file`` (pièces jointes, sauvegardes, export, .ics) ne sont pas touchées.
- **Icônes** : une seule feuille d'icônes (``/icones.svg?v=…``, gardée un an) ; chaque icône d'une page
  n'est plus qu'une référence ``<use href>`` (voir ``icons.icon``).
- **Compression gzip** des pages, feuilles de style, scripts et JSON, pour un navigateur d'une autre
  machine (téléphone, réseau local, site exposé) ; jamais pour le flux de l'assistant IA
  (``text/event-stream``) ni pour les réponses envoyées au fil de l'eau. ``HANGAR_COMPRESSION`` :
  ``auto`` (par défaut : pas pour un navigateur de la machine elle-même), ``toujours`` ou ``jamais``.
"""
from __future__ import annotations

import gzip
import hashlib
import os
import secrets
import threading
import time
from urllib.parse import urlsplit

from flask import Flask, current_app, g, has_request_context, make_response, request
from werkzeug.security import safe_join

UN_AN = 365 * 24 * 3600
CACHE_LONG = f"public, max-age={UN_AN}, immutable"
APPLI_ANDROID = "https://appassets.androidplatform.net"  # pages embarquées de l'appli (voir appareils.py)

# ---------------------------------------------------------------------------
# Fichiers statiques versionnés
# ---------------------------------------------------------------------------
_jetons: dict[tuple[str, str], tuple[float, str | None]] = {}
_RELIRE_S = 2.0  # une modification d'un fichier statique est vue au plus 2 s après


def jeton_statique(dossier: str, nom: str) -> str | None:
    """Empreinte courte (date et taille) d'un fichier statique, ou None s'il n'existe pas."""
    cle = (dossier, nom)
    maintenant = time.monotonic()
    connu = _jetons.get(cle)
    if connu is not None and maintenant - connu[0] < _RELIRE_S:
        return connu[1]
    chemin = safe_join(dossier, nom)
    jeton = None
    if chemin:
        try:
            st = os.stat(chemin)
        except OSError:
            pass
        else:
            jeton = hashlib.blake2s(f"{st.st_mtime_ns}:{st.st_size}".encode(), digest_size=5).hexdigest()
    _jetons[cle] = (maintenant, jeton)
    return jeton


def _ajouter_version(app: Flask):
    def ajouter(endpoint, values):
        if endpoint != "static" or "v" in values:
            return
        nom = values.get("filename") or ""
        if not nom or nom.endswith("/"):
            return  # dossier (adresse de base complétée par un script) : pas d'empreinte
        jeton = jeton_statique(app.static_folder, nom)
        if jeton:
            values["v"] = jeton
    return ajouter


# ---------------------------------------------------------------------------
# Feuille d'icônes
# ---------------------------------------------------------------------------
_sprite = {"n": -1, "corps": b"", "version": ""}
_sprite_lock = threading.Lock()


def feuille_icones() -> tuple[bytes, str]:
    """(contenu SVG, version) de la feuille d'icônes, refaite si un module a ajouté une icône."""
    from .icons import ICONS
    with _sprite_lock:
        if _sprite["n"] != len(ICONS):
            symboles = "".join(
                f'<symbol id="{nom}" viewBox="0 0 24 24" stroke="currentColor" stroke-linecap="round" '
                f'stroke-linejoin="round">{corps}</symbol>' for nom, corps in sorted(ICONS.items()))
            corps = f'<svg xmlns="http://www.w3.org/2000/svg">{symboles}</svg>'.encode()
            _sprite.update(n=len(ICONS), corps=corps, version=hashlib.blake2s(corps, digest_size=5).hexdigest())
        return _sprite["corps"], _sprite["version"]


def adresse_icones() -> str:
    """Adresse versionnée de la feuille d'icônes (``/icones.svg?v=…``)."""
    racine = request.script_root if has_request_context() else ""
    return f"{racine}/icones.svg?v={feuille_icones()[1]}"


def _servir_icones():
    corps, version = feuille_icones()
    r = make_response(corps)
    r.headers["Content-Type"] = "image/svg+xml; charset=utf-8"
    r.set_etag(version)
    r.headers["Cache-Control"] = CACHE_LONG if request.args.get("v") == version else "no-cache"
    return r.make_conditional(request)


# ---------------------------------------------------------------------------
# En-têtes de sécurité
# ---------------------------------------------------------------------------
def csp_nonce() -> str:
    """Jeton des blocs ``<script>`` écrits dans les gabarits (un par requête)."""
    if "csp_nonce" not in g:
        g.csp_nonce = secrets.token_urlsafe(16)
    return g.csp_nonce


def politique(nonce: str, appli: bool = False) -> str:
    """Politique de sécurité du contenu (CSP) des pages de Hangar.

    Images : celles d'autres sites restent permises (Markdown des tickets et des pages de projet) ;
    tout le reste ne vient que de Hangar. ``'wasm-unsafe-eval'`` : décodeurs WebAssembly du lecteur PDF."""
    formulaires = "'self'" + (f" {APPLI_ANDROID}" if appli else "")
    return "; ".join((
        "default-src 'self'",
        f"script-src 'self' 'nonce-{nonce}' 'wasm-unsafe-eval'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob: https: http:",
        "font-src 'self' data:",
        "media-src 'self' data: blob:",
        "connect-src 'self'",
        "worker-src 'self' blob:",
        "frame-src 'self' blob:",
        "object-src 'none'",
        "base-uri 'self'",
        f"form-action {formulaires}",
        "frame-ancestors 'self'",
    ))


def appareil_local() -> bool:
    """Hangar ouvert sur la machine qui le fait tourner (adresse 127.0.0.1 ou localhost) : à la
    déconnexion, l'effacement des pages gardées sur l'appareil n'est alors pas coché d'office."""
    hote = urlsplit("//" + (request.host or "")).hostname or ""
    return hote in ("127.0.0.1", "localhost", "::1")


def _dans_appli() -> bool:
    try:
        from .appareils import dans_appli
        return bool(dans_appli())
    except Exception:  # noqa: BLE001 - module de l'appli absent
        return False


def _securite(response):
    h = response.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "SAMEORIGIN")
    h.setdefault("Referrer-Policy", "same-origin")
    mode = current_app.config.get("CSP", "active")
    if mode == "non" or "Content-Security-Policy" in h or "Content-Security-Policy-Report-Only" in h:
        return  # une route qui pose sa propre politique (fichiers du coffre) la garde
    entete = "Content-Security-Policy-Report-Only" if mode == "rapport" else "Content-Security-Policy"
    h[entete] = politique(csp_nonce(), appli=_dans_appli())


# ---------------------------------------------------------------------------
# Compression
# ---------------------------------------------------------------------------
COMPRESSIBLES = {
    "text/html", "text/css", "text/plain", "text/javascript", "application/javascript", "application/json",
    "application/manifest+json", "image/svg+xml", "application/xml", "text/xml", "text/markdown",
}
TAILLE_MIN = 1024
_statiques_gz: dict[tuple[str, int, int], bytes] = {}
_statiques_gz_octets = 0
_MAX_STATIQUES_GZ = 32 * 1024 * 1024  # copies compressées gardées en mémoire (fichiers statiques)
_gz_lock = threading.Lock()


def _client_local() -> bool:
    adresse = request.remote_addr or ""
    return adresse.startswith("127.") or adresse in ("::1", "localhost")


def _statique_compresse(chemin: str) -> bytes | None:
    global _statiques_gz_octets
    try:
        st = os.stat(chemin)
    except OSError:
        return None
    cle = (chemin, st.st_mtime_ns, st.st_size)
    with _gz_lock:
        if cle in _statiques_gz:
            return _statiques_gz[cle]
    try:
        with open(chemin, "rb") as f:
            donnees = gzip.compress(f.read(), 6)
    except OSError:
        return None
    with _gz_lock:
        if _statiques_gz_octets + len(donnees) > _MAX_STATIQUES_GZ:
            _statiques_gz.clear()
            _statiques_gz_octets = 0
        _statiques_gz[cle] = donnees
        _statiques_gz_octets += len(donnees)
    return donnees


def _compresser(response):
    if (response.status_code != 200 or request.method == "HEAD" or "Content-Encoding" in response.headers
            or response.mimetype not in COMPRESSIBLES):
        return response
    response.vary.add("Accept-Encoding")
    mode = current_app.config.get("COMPRESSION", "auto")
    if mode == "jamais" or (mode == "auto" and _client_local()) or not request.accept_encodings["gzip"]:
        return response
    if response.direct_passthrough:
        # Fichier statique (send_file) : copie compressée gardée en mémoire tant que le fichier ne change pas.
        if request.endpoint != "static":
            return response
        chemin = safe_join(current_app.static_folder, (request.view_args or {}).get("filename", ""))
        donnees = _statique_compresse(chemin) if chemin else None
        if donnees is None:
            return response
        ancien = response.response
        if hasattr(ancien, "close"):
            ancien.close()
        response.direct_passthrough = False
    elif response.is_streamed:
        return response  # envoyée au fil de l'eau (flux de l'IA, téléchargements) : jamais retenue
    else:
        brut = response.get_data()
        if len(brut) < TAILLE_MIN:
            return response
        donnees = gzip.compress(brut, 5)
    response.set_data(donnees)
    response.headers["Content-Encoding"] = "gzip"
    etag, faible = response.get_etag()
    if etag and not faible:
        response.set_etag(etag, weak=True)  # même contenu, autre encodage
    return response


# ---------------------------------------------------------------------------
# Branchement
# ---------------------------------------------------------------------------
def init_app(app: Flask) -> None:
    """À appeler avant l'enregistrement des blueprints : ce crochet de réponse passe alors en dernier
    (compression après tous les autres)."""
    mode = (os.environ.get("HANGAR_CSP") or "active").strip().lower()
    app.config.setdefault("CSP", mode if mode in ("active", "rapport", "non") else "active")
    mode = (os.environ.get("HANGAR_COMPRESSION") or "auto").strip().lower()
    app.config.setdefault("COMPRESSION", mode if mode in ("auto", "toujours", "jamais") else "auto")
    app.url_defaults(_ajouter_version(app))
    app.add_url_rule("/icones.svg", "icones", _servir_icones)
    app.jinja_env.globals.update(csp_nonce=csp_nonce, adresse_icones=adresse_icones, appareil_local=appareil_local)

    @app.after_request
    def _reponse(response):
        if request.endpoint == "static":
            v = request.args.get("v")
            nom = (request.view_args or {}).get("filename", "")
            if v and response.status_code in (200, 304) and v == jeton_statique(app.static_folder, nom):
                response.headers["Cache-Control"] = CACHE_LONG
        _securite(response)
        if response.mimetype == "text/event-stream":
            return response
        return _compresser(response)

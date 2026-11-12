"""Notifications web (Web Push) : rappel quotidien de révision sur l'ordinateur, le téléphone et
l'iPhone (page installée sur l'écran d'accueil), sans l'application Android. Port de `push.py`
de Revision.

Les clés VAPID du serveur sont créées au premier usage dans DATA_DIR/vapid_prive.pem (à garder :
les changer oblige chaque appareil à se réabonner). Les rappels dus partent du fil d'arrière-plan
de Hangar (`mailer.start_worker`) ou de `uv run hangar qcm-rappels` (rappels.py).

`HANGAR_PUSH_CONTACT` (une adresse `mailto:` ou `https://`) remplace au besoin le contact donné aux
services de notifications ; par défaut l'adresse d'expédition des e-mails ou l'adresse publique de
Hangar (réglages des e-mails automatiques).
"""
from __future__ import annotations

import base64
import json
import os
import threading
from pathlib import Path

from flask import current_app

_verrou = threading.Lock()


def _chemin_cle() -> Path:
    return Path(current_app.config["DATA_DIR"]) / "vapid_prive.pem"


def _vapid():
    from py_vapid import Vapid02

    chemin = _chemin_cle()
    with _verrou:
        if not chemin.is_file():
            v = Vapid02()
            v.generate_keys()
            chemin.parent.mkdir(parents=True, exist_ok=True)
            v.save_key(str(chemin))
            try:
                os.chmod(chemin, 0o600)
            except OSError:
                pass
        return Vapid02.from_file(str(chemin))


def cle_publique() -> str:
    """Clé publique (base64url, point non compressé) pour PushManager.subscribe()."""
    from cryptography.hazmat.primitives import serialization

    brut = _vapid().public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(brut).rstrip(b"=").decode("ascii")


def contact() -> str:
    """« sub » des jetons VAPID : HANGAR_PUSH_CONTACT, sinon l'expéditeur des e-mails ou l'adresse
    publique de Hangar (HTTPS), sinon une adresse générique."""
    valeur = os.environ.get("HANGAR_PUSH_CONTACT", "").strip()
    if valeur:
        return valeur
    try:
        from ..models import MailConfig
        cfg = MailConfig.get()
        if cfg.from_address and "@" in cfg.from_address:
            return "mailto:" + cfg.from_address.strip()
        if (cfg.base_url or "").startswith("https://"):
            return cfg.base_url.rstrip("/")
    except Exception:  # noqa: BLE001 — réglages illisibles : contact générique
        pass
    return "mailto:admin@example.org"


class AbonnementExpire(Exception):
    """Le service de notifications ne connaît plus cet appareil (abonnement à supprimer)."""


class AdresseNonAutorisee(Exception):
    """L'adresse de l'abonnement n'est pas celle d'un service de notifications connu."""


class EnvoiRefuse(Exception):
    """Le service de notifications refuse l'envoi (403 : le plus souvent, clés du serveur changées
    depuis l'abonnement de l'appareil)."""


# Services de notifications des navigateurs : c'est le navigateur qui fournit l'adresse de
# l'abonnement, et le serveur y envoie une requête. Seuls ces hôtes sont acceptés (sinon un compte
# pourrait faire appeler par le serveur une adresse de son choix : box, NAS, service local).
HOTES_PUSH = {
    "fcm.googleapis.com",                   # Chrome, Edge (Android), Opera, Samsung Internet, Brave…
    "android.googleapis.com",               # anciens abonnements de Chrome
    "updates.push.services.mozilla.com",    # Firefox
    "push.services.mozilla.com",
}
SUFFIXES_PUSH = (".notify.windows.com", ".push.apple.com")  # Edge (Windows), Safari (macOS, iPhone)


def adresse_autorisee(endpoint: str) -> bool:
    """Vrai si ``endpoint`` est une adresse https d'un service de notifications connu (nom d'hôte
    analysé, ni port ni identifiants)."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(str(endpoint))
        hote, port = (parts.hostname or "").rstrip("."), parts.port
    except ValueError:
        return False
    if parts.scheme != "https" or parts.username is not None or parts.password is not None or port not in (None, 443):
        return False
    return hote in HOTES_PUSH or any(hote.endswith(suffixe) for suffixe in SUFFIXES_PUSH)


def _session_sans_redirection():
    """Session HTTP qui ne suit aucune redirection : la requête part vers l'adresse contrôlée, et
    nulle part ailleurs."""
    import requests

    session = requests.Session()
    session.max_redirects = 0
    return session


TTL_DEFAUT = 12 * 3600  # rappel quotidien : encore utile dans la soirée
TAG_DEFAUT = "rappel-quotidien"


def envoyer(abonnement, titre: str, texte: str, url: str = "/qcm/aujourdhui/", ttl: int = TTL_DEFAUT,
            tag: str = TAG_DEFAUT) -> None:
    """Envoie une notification à un appareil ; AbonnementExpire si l'abonnement est mort,
    AdresseNonAutorisee si son adresse n'est pas celle d'un service connu, EnvoiRefuse sur un 403.

    `ttl` : durée (en secondes) pendant laquelle le service garde la notification d'un appareil hors
    ligne (au-delà, elle ne sert plus : « cours dans 15 min » reçu le soir) ; `tag` : une notification
    remplace à l'écran la précédente du même tag (chaque rappel de l'agenda a le sien)."""
    from pywebpush import WebPushException, webpush

    if not adresse_autorisee(abonnement.endpoint):
        raise AdresseNonAutorisee(abonnement.endpoint[:80])
    session = _session_sans_redirection()
    try:
        webpush(
            subscription_info={"endpoint": abonnement.endpoint, "keys": {"p256dh": abonnement.p256dh, "auth": abonnement.auth}},
            data=json.dumps({"titre": titre, "texte": texte, "url": url, "tag": tag or TAG_DEFAUT}, ensure_ascii=False),
            vapid_private_key=str(_chemin_cle()) if _chemin_cle().is_file() else _vapid(),
            vapid_claims={"sub": contact()},
            ttl=max(60, min(int(ttl), 28 * 24 * 3600)),
            timeout=15,
            requests_session=session,
        )
    except WebPushException as e:
        statut = getattr(getattr(e, "response", None), "status_code", None)
        if statut in (404, 410):
            raise AbonnementExpire(str(e)) from None
        if statut == 403:
            raise EnvoiRefuse(str(e)) from None
        raise
    finally:
        session.close()

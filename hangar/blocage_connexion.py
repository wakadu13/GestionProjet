"""Protection de la connexion contre les essais de mots de passe (comme django-axes dans Revision).

- 5 mots de passe faux pour un même identifiant depuis une même adresse IP → connexion bloquée
  30 minutes pour ce couple ;
- 20 échecs depuis une même adresse, tous identifiants confondus → l'adresse est bloquée 30 minutes
  (essais en série sur plusieurs comptes) ;
- une connexion réussie efface les échecs de ce couple ; un administrateur débloque depuis son
  profil (« Connexions bloquées ») ou par `uv run hangar connexions-debloquer [identifiant]` ;
  changer le mot de passe d'un compte (lui-même ou un admin) efface aussi ses blocages.

Le formulaire de connexion (`views/auth.py`) et la connexion de l'application Android
(`/api/appareil/connexion/`, `appareils.py`) passent tous deux par ici.

Adresse IP : celle de la connexion, ou, derrière un proxy, seulement ce qu'il écrit lui-même
(impossible à falsifier, comme `accounts/ip.py` de Revision) :
- `HANGAR_CLOUDFLARE=1` : en-tête CF-Connecting-IP (réécrit par Cloudflare) ;
- `HANGAR_BEHIND_PROXY=1` : dernière adresse de X-Forwarded-For (celle qu'a vue notre proxy).
"""
from __future__ import annotations

import ipaddress
import os
from datetime import datetime, timedelta

from flask import request
from sqlalchemy import func

from .extensions import db

LIMITE = 5        # échecs pour un identifiant depuis une adresse
LIMITE_IP = 20    # échecs depuis une adresse, tous identifiants confondus
DUREE = timedelta(minutes=30)


class EchecConnexion(db.Model):
    """Échecs de connexion récents d'un identifiant depuis une adresse IP."""
    __tablename__ = "connexion_echec"
    __table_args__ = (db.UniqueConstraint("identifiant", "ip", name="uq_connexion_echec"),)

    id = db.Column(db.Integer, primary_key=True)
    identifiant = db.Column(db.String(120), nullable=False, index=True)
    ip = db.Column(db.String(64), nullable=False, default="", index=True)
    echecs = db.Column(db.Integer, default=0)
    premier = db.Column(db.DateTime)
    dernier = db.Column(db.DateTime)


def _maintenant():
    return datetime.now().replace(microsecond=0)


def _env(nom):
    return os.environ.get(nom, "").strip().lower() in ("1", "true", "oui", "yes")


def _adresse(valeur):
    try:
        return str(ipaddress.ip_address(valeur.strip()))
    except ValueError:
        return None


def ip_client() -> str:
    """Adresse du visiteur (voir l'en-tête du module pour les proxys)."""
    cloudflare = _env("HANGAR_CLOUDFLARE")
    if cloudflare:
        ip = _adresse(request.headers.get("CF-Connecting-IP", ""))
        if ip:
            return ip
    if cloudflare or _env("HANGAR_BEHIND_PROXY"):
        ip = _adresse(request.headers.get("X-Forwarded-For", "").split(",")[-1])
        if ip:
            return ip
    return request.remote_addr or ""


def _cle(identifiant):
    return (identifiant or "").strip().lower()[:120]


def bloque(identifiant: str, ip: str, now: datetime | None = None) -> int:
    """Minutes de blocage restantes pour ce couple (identifiant, adresse), 0 s'il peut essayer."""
    now = now or _maintenant()
    depuis = now - DUREE
    ligne = EchecConnexion.query.filter_by(identifiant=_cle(identifiant), ip=ip or "").first()
    fins = []
    if ligne and (ligne.echecs or 0) >= LIMITE and ligne.dernier and ligne.dernier > depuis:
        fins.append(ligne.dernier + DUREE)
    if ip:
        total, dernier = db.session.query(func.sum(EchecConnexion.echecs), func.max(EchecConnexion.dernier)).filter(
            EchecConnexion.ip == ip, EchecConnexion.dernier > depuis).one()
        if (total or 0) >= LIMITE_IP and dernier:
            fins.append(dernier + DUREE)
    if not fins:
        return 0
    return max(1, -(-int((max(fins) - now).total_seconds()) // 60))


def noter_echec(identifiant: str, ip: str, now: datetime | None = None) -> None:
    """Compte un mot de passe faux (une série plus ancienne que DUREE repart de zéro)."""
    now = now or _maintenant()
    ligne = EchecConnexion.query.filter_by(identifiant=_cle(identifiant), ip=ip or "").first()
    if ligne is None:
        ligne = EchecConnexion(identifiant=_cle(identifiant), ip=ip or "", echecs=0, premier=now)
        db.session.add(ligne)
    elif ligne.dernier is None or ligne.dernier <= now - DUREE:
        ligne.echecs, ligne.premier = 0, now
    ligne.echecs = (ligne.echecs or 0) + 1
    ligne.dernier = now
    db.session.commit()


def reussite(identifiant: str, ip: str) -> None:
    """Connexion réussie : les échecs de ce couple sont oubliés (AXES_RESET_ON_SUCCESS)."""
    EchecConnexion.query.filter_by(identifiant=_cle(identifiant), ip=ip or "").delete(synchronize_session=False)
    db.session.commit()


def debloquer(identifiant: str | None = None, ip: str | None = None, commit: bool = True) -> int:
    """Efface les échecs (d'un identifiant, d'une adresse, ou tous) ; renvoie le nombre de lignes."""
    q = EchecConnexion.query
    if identifiant is not None:
        q = q.filter_by(identifiant=_cle(identifiant))
    if ip is not None:
        q = q.filter_by(ip=ip)
    n = q.delete(synchronize_session=False)
    if commit:
        db.session.commit()
    return n


def bloques(now: datetime | None = None) -> list[dict]:
    """Blocages en cours (profil des admins) : [{identifiant, ip, echecs, jusqua, minutes, adresse}]."""
    now = now or _maintenant()
    depuis = now - DUREE
    out = []
    lignes = EchecConnexion.query.filter(EchecConnexion.dernier > depuis).order_by(EchecConnexion.dernier.desc()).all()
    par_ip: dict = {}
    for l in lignes:
        par_ip[l.ip] = par_ip.get(l.ip, 0) + (l.echecs or 0)
        if (l.echecs or 0) >= LIMITE:
            out.append({"identifiant": l.identifiant, "ip": l.ip, "echecs": l.echecs, "jusqua": l.dernier + DUREE,
                        "minutes": bloque(l.identifiant, l.ip, now), "adresse": False})
    for ip, total in par_ip.items():
        if ip and total >= LIMITE_IP:
            out.append({"identifiant": "", "ip": ip, "echecs": total, "jusqua": None,
                        "minutes": bloque("", ip, now), "adresse": True})
    return out


def essais_restants(identifiant: str, ip: str, now: datetime | None = None) -> int:
    """Mots de passe faux encore permis avant le blocage (le plus proche des deux plafonds).

    Les identifiants inexistants comptent aussi (noter_echec) : le nombre ne révèle pas quels comptes existent."""
    now = now or _maintenant()
    depuis = now - DUREE
    ligne = EchecConnexion.query.filter_by(identifiant=_cle(identifiant), ip=ip or "").first()
    reste = LIMITE - ((ligne.echecs or 0) if ligne and ligne.dernier and ligne.dernier > depuis else 0)
    if ip:
        total = db.session.query(func.sum(EchecConnexion.echecs)).filter(
            EchecConnexion.ip == ip, EchecConnexion.dernier > depuis).scalar()
        reste = min(reste, LIMITE_IP - (total or 0))
    return max(0, reste)


def message(minutes: int) -> str:
    """Message de la page de connexion pendant un blocage : il dit comment débloquer tout de suite."""
    return (f"Trop d’essais de mot de passe incorrects : connexion bloquée pendant encore {minutes} minute"
            f"{'s' if minutes > 1 else ''}. Réessaie plus tard. Pour débloquer tout de suite : sur le PC qui fait "
            f"tourner Hangar, ouvre une fenêtre de commande dans le dossier de Hangar et tape "
            f"« uv run hangar connexions-debloquer ». Un administrateur peut aussi la débloquer depuis son "
            f"profil (Connexions bloquées).")

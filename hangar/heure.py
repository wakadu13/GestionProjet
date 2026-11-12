"""Heure de l'agenda : « maintenant » et « aujourd'hui » dans le fuseau de l'emploi du temps.

Les séances, tâches et rappels sont enregistrés en heure locale « naïve » (sans fuseau), celle de l'emploi
du temps : Europe/Paris. Pour les comparer au présent, l'horloge doit donner cette même heure, quel que soit
le réglage de la machine (serveur ou Raspberry Pi resté en UTC, portable à l'étranger…). Toutes les
comparaisons au présent passent donc par `now()` et `today()`.

- `HANGAR_TZ` (variable d'environnement) : fuseau de l'agenda, Europe/Paris par défaut ; `local` = heure de
  la machine (comportement des versions précédentes).
- Base des fuseaux introuvable (Windows sans le paquet tzdata) ou nom inconnu : Europe/Paris est calculée
  avec la règle européenne de l'heure d'été ; un autre fuseau retombe sur l'heure de la machine.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache

DEFAUT = "Europe/Paris"
LOCAL = "local"


def nom_configure() -> str:
    """Fuseau demandé (HANGAR_TZ), Europe/Paris par défaut."""
    return (os.environ.get("HANGAR_TZ") or "").strip() or DEFAUT


@lru_cache(maxsize=8)
def _zone(nom: str):
    if nom.lower() == LOCAL:
        return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(nom)
    except Exception:  # noqa: BLE001 - base des fuseaux absente (Windows sans tzdata) ou nom inconnu
        return None


def _dernier_dimanche(annee: int, mois: int) -> date:
    d = date(annee, mois, 31)
    return d - timedelta(days=(d.weekday() + 1) % 7)


def paris_sans_base(utc: datetime) -> datetime:
    """Heure de Paris sans base des fuseaux : heure d'été du dernier dimanche de mars au dernier dimanche
    d'octobre, à 1 h UTC (règle européenne)."""
    u = utc.replace(tzinfo=None)
    debut = datetime.combine(_dernier_dimanche(u.year, 3), datetime.min.time()) + timedelta(hours=1)
    fin = datetime.combine(_dernier_dimanche(u.year, 10), datetime.min.time()) + timedelta(hours=1)
    return u + timedelta(hours=2 if debut <= u < fin else 1)


def vers_agenda(quand: datetime) -> datetime:
    """Date « aware » (avec fuseau) → heure naïve de l'agenda. Une date naïve est supposée déjà dans
    l'heure de l'agenda et rendue telle quelle."""
    if quand.tzinfo is None:
        return quand
    nom = nom_configure()
    zone = _zone(nom)
    if zone is not None:
        return quand.astimezone(zone).replace(tzinfo=None)
    if nom == DEFAUT:
        return paris_sans_base(quand.astimezone(timezone.utc))
    return quand.astimezone().replace(tzinfo=None)  # heure de la machine


def depuis_agenda(naive: datetime) -> datetime:
    """Heure naïve de l'agenda → date « aware » portant le décalage du fuseau de l'agenda."""
    if naive.tzinfo is not None:
        return naive
    nom = nom_configure()
    zone = _zone(nom)
    if zone is not None:
        return naive.replace(tzinfo=zone)
    if nom == DEFAUT:
        # Sans base des fuseaux : décalage +1 h (hiver) ou +2 h (été), celui qui retombe sur la même heure.
        for heures in (1, 2):
            utc = naive - timedelta(hours=heures)
            if paris_sans_base(utc) == naive:
                return naive.replace(tzinfo=timezone(timedelta(hours=heures)))
        return naive.replace(tzinfo=timezone(timedelta(hours=2)))  # heure sautée au passage à l'été
    return naive.astimezone()  # heure de la machine


def _maintenant_utc() -> datetime:
    return datetime.now(timezone.utc)


def now() -> datetime:
    """Maintenant, heure naïve de l'agenda (à la seconde)."""
    return vers_agenda(_maintenant_utc()).replace(microsecond=0)


def today() -> date:
    """Aujourd'hui, dans le fuseau de l'agenda."""
    return now().date()


def nom_iana() -> str:
    """Nom du fuseau des heures enregistrées, pour les fichiers .ics (Europe/Paris si `local` ou inconnu)."""
    nom = nom_configure()
    return nom if _zone(nom) is not None else DEFAUT


def description() -> dict:
    """Ce qu'affiche « Rappels et e-mails » : fuseau utilisé, heure actuelle, et s'il diffère de la machine."""
    nom = nom_configure()
    zone = _zone(nom)
    if zone is not None or nom == DEFAUT:
        libelle = "heure de Paris" if nom == DEFAUT else f"fuseau {nom}"
        introuvable = False
    else:
        libelle = "heure de cet ordinateur"
        introuvable = nom.lower() != LOCAL
    t = now()
    machine = datetime.now().replace(microsecond=0)
    return {"nom": nom, "libelle": libelle, "maintenant": t, "introuvable": introuvable,
            "machine_differente": abs((machine - t).total_seconds()) >= 60, "machine": machine}

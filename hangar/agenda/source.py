"""Agenda : emploi du temps récupéré tout seul, depuis un dossier surveillé ou une adresse .ics."""
from __future__ import annotations

import hashlib
import json
import os
import time as _clock
from datetime import datetime, timedelta

from flask import current_app

from .. import agenda as A  # le paquet : voir hangar/agenda/__init__.py
from .. import digest, ics
from ..extensions import db
from ..models import AgendaEvent, AgendaSettings, Notification, User


# ---------------------------------------------------------------------------
# Emploi du temps récupéré tout seul : dossier surveillé ou adresse .ics
# ---------------------------------------------------------------------------
# Chaque nouveau fichier passe par `import_calendar` (le dernier fait foi, garde-fous compris). Une empreinte
# des séances évite de créer un import quand rien n'a changé (un même planning re-téléchargé change souvent
# d'horodatage, pas de séances). Réglage réservé aux administrateurs : une adresse ou un dossier choisis par
# n'importe quel compte feraient lire au serveur des adresses du réseau local ou des fichiers du PC.

SOURCE_KINDS = {"folder": "Dossier surveillé (ex. Téléchargements)", "url": "Adresse d’abonnement .ics"}
SOURCE_URL_EVERY = timedelta(hours=3)        # une adresse est relue toutes les 3 h (le dossier : à chaque passage)
SOURCE_RECHECK_NOTE = timedelta(hours=1)     # dossier inchangé : « dernière lecture » notée au plus une fois par heure
SOURCE_MAX_BYTES = 5 * 1024 * 1024
SOURCE_TIMEOUT = 20                          # chaque opération réseau (connexion, chaque lecture)
SOURCE_DEADLINE = 60                         # lecture entière d'une adresse : au-delà, abandon (fil d'arrière-plan)
SOURCE_CHUNK = 64 * 1024
SOURCE_SILENT_DAYS = 6                       # source muette depuis une semaine : le rappel d'import revient
SOURCE_SETTLE_SECONDS = 5                    # fichier modifié il y a moins que ça : sans doute en cours d'écriture


def calendar_fingerprint(cal) -> str:
    """Empreinte des séances d'un fichier (pas de ses octets : l'horodatage d'export change à chaque fois)."""
    rows = sorted([e.uid, {k: A._norm(k, v) for k, v in e.values().items()}] for e in cal.events)
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def foreign_reason(cal, user: User) -> str | None:
    """Pourquoi un fichier trouvé tout seul n'est sans doute pas l'emploi du temps (None : il peut l'être).

    Écartés : les invitations (METHOD:REQUEST…), et, quand un planning est déjà là, un fichier qui n'a ni
    séance déjà connue ni type de cours de l'école (CM, TD_TP, CO_ECRIT…). Sinon, en mode « semaines »,
    l'invitation à une réunion tombée dans le dossier retirerait toutes les autres séances de sa semaine."""
    if A.is_invitation(cal):
        return ("c’est une invitation (réunion, rendez-vous…), pas ton emploi du temps : "
                "importe-la à la main si tu veux l’ajouter")
    known = {uid for (uid,) in db.session.query(AgendaEvent.uid).filter(AgendaEvent.user_id == user.id)}
    if not known:
        return None  # premier planning : rien à retirer, rien à craindre
    if any(e.uid in known for e in cal.events) or any(e.kind in ics.KINDS for e in cal.events):
        return None
    return ("il ne ressemble pas à ton emploi du temps (aucune séance connue, aucun type de cours reconnu) : "
            "il a été laissé de côté pour ne rien retirer par erreur")


def _source_pick_file(folder: str) -> tuple[str, str] | None:
    """Fichier .ics le plus récent du dossier (« planning (1).ics » après un nouveau téléchargement) :
    (chemin, signature nom|date|taille), ou None s'il n'y en a pas."""
    best = None
    with os.scandir(folder) as it:
        for e in it:
            if not e.name.lower().endswith(".ics") or e.name.startswith("."):
                continue
            try:
                if not e.is_file():
                    continue
                st = e.stat()
            except OSError:
                continue
            if best is None or st.st_mtime_ns > best[1].st_mtime_ns:
                best = (e.path, st)
    if best is None:
        return None
    path, st = best
    return path, f"{os.path.basename(path)}|{st.st_mtime_ns}|{st.st_size}"[:300]


def _source_file_busy(path: str) -> bool:
    """Fichier sans doute en cours de téléchargement : vide, modifié à l'instant, ou accompagné de son
    « .part » (Firefox crée le fichier final vide et écrit dans « planning.ics.part »). Il est relu au
    passage suivant, sans rien signaler."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    if st.st_size == 0 or os.path.exists(path + ".part"):
        return True
    age = _clock.time() - st.st_mtime
    return 0 <= age < SOURCE_SETTLE_SECONDS


def source_url(value: str) -> str:
    """Adresse à lire (webcal:// des liens d'abonnement → https://) ; ValueError si elle n'est pas http(s)."""
    from urllib.parse import urlsplit
    v = (value or "").strip()
    if v.lower().startswith("webcal://"):
        v = "https://" + v[len("webcal://"):]
    parts = urlsplit(v)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("L’adresse doit commencer par https:// (ou webcal://).")
    return v


def _fetch_url(url: str) -> bytes:
    """Contenu de l'adresse. `SOURCE_TIMEOUT` borne chaque opération réseau, `SOURCE_DEADLINE` la lecture
    entière : un serveur qui envoie au compte-gouttes ne retient pas le fil d'arrière-plan (rappels de
    l'agenda) ; TimeoutError au-delà."""
    import urllib.request
    deadline = _clock.monotonic() + SOURCE_DEADLINE
    req = urllib.request.Request(url, headers={"User-Agent": "Hangar (agenda)", "Accept": "text/calendar, */*"})
    chunks: list[bytes] = []
    size = 0
    with urllib.request.urlopen(req, timeout=SOURCE_TIMEOUT) as r:  # noqa: S310 - adresse choisie par un administrateur
        while True:
            if _clock.monotonic() > deadline:
                raise TimeoutError("l’adresse met trop de temps à envoyer le planning")
            chunk = r.read1(SOURCE_CHUNK)  # une seule lecture réseau à la fois (read() attendrait tout le bloc)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > SOURCE_MAX_BYTES:
                break
    data = b"".join(chunks)
    if len(data) > SOURCE_MAX_BYTES:
        raise ValueError("le fichier est trop gros pour un emploi du temps")
    return data


def _source_note(user: User, message: str, url: str = "/agenda/imports", at: datetime | None = None) -> None:
    db.session.add(Notification(user_id=user.id, kind="update", message=message[:500], url=url, created_at=at or A.now()))


def check_source(user: User, at: datetime | None = None, force: bool = False) -> str:
    """Lit la source de l'emploi du temps de `user` et importe un nouveau fichier. Retourne ce qui s'est passé :
    "off" (pas de source), "wait" (pas encore l'heure), "busy" (fichier en cours de téléchargement),
    "same" (rien de nouveau), "imported", "ignored"
    (fichier écarté par le garde-fou) ou "error" (message dans `source_error`).

    `force` (« Vérifier maintenant », enregistrement du réglage) : lit tout de suite, même un fichier déjà vu
    ou au contenu déjà importé. L'empreinte et le fichier vu ne sont validés qu'avec un import réussi : une
    panne pendant l'import (base occupée…) laisse le fichier à relire au passage suivant."""
    at = at or A.now()
    s = A.settings_of(user)
    kind, where = s.source_kind or "", (s.source_path or "").strip()
    if kind not in SOURCE_KINDS or not where:
        return "off"
    if kind == "url" and not force and s.source_tried_at and at - s.source_tried_at < SOURCE_URL_EVERY:
        return "wait"
    s.source_tried_at = at

    def fail(msg: str) -> str:
        changed = s.source_error != msg
        s.source_error = msg[:500]
        if changed:  # une seule notification par problème (pas toutes les 30 s)
            _source_note(user, f"Emploi du temps automatique : {msg}", at=at)
        db.session.commit()
        return "error"

    filename, seen = "planning.ics", None
    if kind == "folder":
        if not os.path.isdir(where):
            return fail("le dossier surveillé est introuvable. Vérifie son chemin dans « Emploi du temps ».")
        try:
            picked = _source_pick_file(where)
        except OSError:
            return fail("le dossier surveillé n’est pas lisible.")
        if picked is None:  # aucun .ics pour l'instant : rien à signaler
            s.source_error = ""
            if s.source_checked_at is None or at - s.source_checked_at >= SOURCE_RECHECK_NOTE:
                s.source_checked_at = at
            db.session.commit()
            return "same"
        path, seen = picked
        if _source_file_busy(path):  # source_seen non noté : relu au prochain passage
            s.source_error = ""
            db.session.commit()
            return "busy"
        if seen == s.source_seen and not force:
            s.source_error = ""
            if s.source_checked_at is None or at - s.source_checked_at >= SOURCE_RECHECK_NOTE:
                s.source_checked_at = at
            db.session.commit()
            return "same"
        try:
            with open(path, "rb") as fh:
                raw = fh.read(SOURCE_MAX_BYTES + 1)
        except OSError:
            return fail(f"le fichier {os.path.basename(path)} n’est pas lisible.")
        if len(raw) > SOURCE_MAX_BYTES:
            return fail(f"le fichier {os.path.basename(path)} est trop gros pour un emploi du temps.")
        filename = os.path.basename(path)  # source_seen : noté plus bas, une fois le fichier traité
    else:
        try:
            url = source_url(where)
            raw = _fetch_url(url)
        except ValueError as exc:
            return fail(f"adresse refusée : {exc}")
        except Exception:  # noqa: BLE001 - réseau, serveur de l'école : on réessaiera plus tard
            return fail("l’adresse du planning ne répond pas pour l’instant. Nouvel essai dans quelques heures.")
        from urllib.parse import urlsplit
        filename = os.path.basename(urlsplit(url).path) or "planning.ics"
    try:
        cal = ics.parse(raw)
        if not cal.events:
            raise ics.ICSError("aucune séance dans le fichier")
    except ics.ICSError:
        return fail(f"le fichier {filename} n’est pas un emploi du temps lisible.")
    fp = calendar_fingerprint(cal)
    s.source_error = ""
    s.source_checked_at = at
    if fp == s.source_fingerprint and not force:
        if seen is not None:
            s.source_seen = seen
        db.session.commit()
        return "same"
    reason = foreign_reason(cal, user)
    if reason:
        s.source_fingerprint, s.source_changed_at = fp, at
        if seen is not None:
            s.source_seen = seen
        _source_note(user, f"Fichier {filename} ignoré : {reason}.", at=at)
        db.session.commit()
        return "ignored"
    db.session.commit()  # heure de lecture notée, même si l'import échoue
    try:
        # Empreinte et fichier vu posés dans la transaction de l'import (validés par son commit)
        s.source_fingerprint, s.source_changed_at = fp, at
        if seen is not None:
            s.source_seen = seen
        imp = A.import_calendar(user, raw, filename, "weeks", at=at)
    except Exception as exc:  # noqa: BLE001 - ICSError, base occupée, bogue : relu au prochain passage
        db.session.rollback()
        s = A.settings_of(user)
        s.source_fingerprint, s.source_seen = "", ""  # nouvel essai au prochain passage
        s.source_tried_at = at
        if isinstance(exc, ics.ICSError):
            return fail(str(exc))
        msg = (f"le fichier {filename} n’a pas pu être importé (erreur passagère ?). "
               "Nouvel essai au prochain passage ; si le problème dure, importe-le à la main.")
        if s.source_error != msg:  # une trace par panne, pas toutes les 30 s
            current_app.logger.exception("Import automatique de l’emploi du temps (compte %s)", user.id)
            A._mail().noter_erreur("Lecture automatique de l’emploi du temps")
        return fail(msg)
    parts = [x for x in (digest.pluriel(imp.modified, "séance modifiée", "séances modifiées") if imp.modified else "",
                         digest.pluriel(imp.added, "ajoutée") if imp.added else "",
                         digest.pluriel(imp.removed, "retirée") if imp.removed else "",
                         digest.pluriel(imp.restored, "rétablie") if imp.restored else "") if x]
    if parts or imp.warnings:
        msg = "Emploi du temps mis à jour : " + (", ".join(parts) if parts else "aucun changement de séance") + "."
        if getattr(imp, "guarded_weeks", None):
            msg += " Par prudence, des séances n’ont pas été retirées : vois le détail."
        _source_note(user, msg, url=f"/agenda/imports/{imp.id}", at=at)
        db.session.commit()
    return "imported"


def check_sources(at: datetime | None = None) -> int:
    """Passage du fil d'arrière-plan : lit la source de chaque compte qui en a une. Retourne le nombre d'imports."""
    at = at or A.now()
    n = 0
    rows = AgendaSettings.query.filter(AgendaSettings.source_kind.in_(tuple(SOURCE_KINDS)),
                                       AgendaSettings.source_path != "").all()
    for user_id in [r.user_id for r in rows]:
        user = db.session.get(User, user_id)
        if user is None or not user.active or not user.is_admin:
            continue  # réglage réservé aux administrateurs : un compte rétrogradé n'est plus lu
        try:
            n += check_source(user, at) == "imported"
        except Exception:  # noqa: BLE001 - un compte en panne n'empêche pas les autres
            db.session.rollback()
            current_app.logger.exception("Lecture automatique de l’emploi du temps (compte %s)", user_id)
            A._mail().noter_erreur("Lecture automatique de l’emploi du temps")
    return n


def source_status(s: AgendaSettings, at: datetime | None = None) -> dict:
    """État de la source automatique, pour la page « Emploi du temps »."""
    at = at or A.now()
    kind = s.source_kind if s.source_kind in SOURCE_KINDS else ""
    return {"kind": kind, "path": s.source_path or "", "label": SOURCE_KINDS.get(kind, ""),
            "checked_at": s.source_checked_at, "changed_at": s.source_changed_at, "error": s.source_error or "",
            "kinds": SOURCE_KINDS}



"""Agenda : export iCalendar (.ics) et abonnement en lecture seule par adresse secrète."""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from .. import agenda as A  # le paquet : voir hangar/agenda/__init__.py
from .. import heure, ics
from ..extensions import db
from ..models import AgendaEvent, AgendaReminder, AgendaSettings, AgendaTask, User


# ---------------------------------------------------------------------------
# Export iCalendar (pour l'ajouter à Google Agenda, Outlook, téléphone…)
# ---------------------------------------------------------------------------

# Écriture iCalendar : voir hangar/ics.py (noms gardés pour le code existant)
_ics_text = ics.escape_text
_fold = ics.fold

RRULES = {"daily": "FREQ=DAILY", "weekdays": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", "weekly": "FREQ=WEEKLY",
          "biweekly": "FREQ=WEEKLY;INTERVAL=2", "monthly": "FREQ=MONTHLY"}


def export_ics(user: User, include_events: bool = True, include_tasks: bool = True,
               at: datetime | None = None, include_team: bool = True) -> str:
    """Agenda au format iCalendar (RFC 5545) : lignes repliées à 75 octets, textes échappés, DTSTAMP en UTC,
    fuseau de l'agenda décrit (VTIMEZONE : Europe/Paris, ou HANGAR_TZ calculé avec zoneinfo ; à défaut, heures
    en UTC « Z »), fin de répétition en UTC pour une tâche à heure fixe, rappels
    des tâches sans heure calés sur l'heure de référence (8 h par défaut) comme dans Hangar."""
    at = at or A.now()
    stamp = ics.utc_stamp()
    tz = heure.nom_iana()  # fuseau des heures enregistrées : Europe/Paris par défaut
    s = A.settings_of_id(user.id)
    allday = s.allday_hour if s.allday_hour is not None else 8
    esc = ics.escape_text

    def alarm(text: str, trig: str) -> list[str]:
        return ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{esc(text)}", trig, "END:VALARM"]

    def fixed(r: AgendaReminder) -> str | None:  # rappel à date fixe encore à venir : déclencheur absolu
        if r.offset_minutes is not None or r.remind_at is None or r.status != "pending" or r.remind_at < at:
            return None
        return f"TRIGGER;VALUE=DATE-TIME:{ics.utc_value(ics.local_to_utc(r.remind_at, tz))}"

    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Hangar//Agenda//FR", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", f"X-WR-CALNAME:{esc('Agenda de ' + user.display_name)}", f"X-WR-TIMEZONE:{tz}"]
    events = (AgendaEvent.query.filter_by(user_id=user.id, status="active").order_by(AgendaEvent.start).all()
              if include_events else [])
    tasks = (AgendaTask.query.filter(AgendaTask.user_id == user.id, AgendaTask.due_date.isnot(None),
                                     AgendaTask.done_at.is_(None)).all() if include_tasks else [])
    # Années couvertes par le VTIMEZONE : celles des heures écrites. ics.vtimezone ramène la fenêtre entre 1900
    # et 9998 (dates saisies sans borne) et reconnaît chaque règle sur 28 ans de plus : la dernière règle
    # continue ensuite, juste aussi pour « vendredi avant le dernier dimanche » (tâches répétées sans fin)
    annees = [at.year] + [ev.start.year for ev in events] + [t.due_date.year for t in tasks if t.start_time]
    vtz = ics.vtimezone(tz, min(annees), max(annees) + 2)
    lines += vtz

    def heure_ics(prop: str, quand: datetime) -> str:
        """DTSTART/DTEND à heure fixe : avec TZID décrit par le VTIMEZONE, sinon en UTC (« Z »), car un TZID
        sans définition est interdit (RFC 5545) et lu de travers par Outlook."""
        if vtz:
            return f"{prop};TZID={tz}:{quand:%Y%m%dT%H%M%S}"
        return f"{prop}:{ics.utc_value(ics.local_to_utc(quand, tz))}"

    if include_events:
        for ev in events:
            if ev.cancelled:
                continue
            desc = "\n".join(x for x in (
                A.kind_label(ev.kind), f"Intervenants : {ev.teachers}" if ev.teachers else "",
                f"Groupes : {ev.groups}" if ev.groups else "", ev.description, ev.note) if x)
            lines += ["BEGIN:VEVENT", f"UID:{esc(ev.uid)}@hangar", f"DTSTAMP:{stamp}"]
            if ev.all_day:
                end = ev.end if ev.end.date() > ev.start.date() else ev.start + timedelta(days=1)
                lines += [f"DTSTART;VALUE=DATE:{ev.start:%Y%m%d}", f"DTEND;VALUE=DATE:{end:%Y%m%d}"]
            else:
                lines.append(heure_ics("DTSTART", ev.start))
                if ev.end > ev.start:  # sans DTEND : séance d'un instant (une fin égale au début est interdite)
                    lines.append(heure_ics("DTEND", ev.end))
            title = ev.title + (f" ({A.kind_label(ev.kind, True)})" if ev.kind else "")
            lines += [f"SUMMARY:{esc(title)}", f"LOCATION:{esc(ev.location)}", f"DESCRIPTION:{esc(desc)}"]
            if ev.tentative:
                lines.append("STATUS:TENTATIVE")
            for r in ev.reminders:
                if r.offset_minutes is not None:  # Hangar cale ces rappels sur le début de la séance
                    lines += alarm(title, ics.trigger(-r.offset_minutes))
                elif fixed(r):
                    lines += alarm(title, fixed(r))
            lines.append("END:VEVENT")
    if include_tasks:
        for t in tasks:
            lines += ["BEGIN:VEVENT", f"UID:tache-{t.id}@hangar", f"DTSTAMP:{stamp}"]
            if t.start_time:
                st = datetime.combine(t.due_date, t.start_time)
                en = A.task_end(t)
                lines.append(heure_ics("DTSTART", st))
                if en and en > st:
                    lines.append(heure_ics("DTEND", en))
            else:
                lines += [f"DTSTART;VALUE=DATE:{t.due_date:%Y%m%d}",
                          f"DTEND;VALUE=DATE:{t.due_date + timedelta(days=1):%Y%m%d}"]
            lines += [f"SUMMARY:{esc('✓ ' + t.title)}", f"DESCRIPTION:{esc(t.notes)}"]
            if t.category:
                lines.append(f"CATEGORIES:{esc(t.category)}")
            repeated = t.recurrence in RRULES
            if repeated:
                rule = RRULES[t.recurrence]
                if t.recurrence_until:
                    if t.start_time:  # début avec fuseau : la RFC exige une fin en date-heure UTC
                        last = ics.local_to_utc(datetime.combine(t.recurrence_until, t.start_time), tz)
                        rule += f";UNTIL={ics.utc_value(last)}"
                    else:
                        rule += f";UNTIL={t.recurrence_until:%Y%m%d}"
                lines.append(f"RRULE:{rule}")
            for r in t.reminders:
                if r.offset_minutes is not None:
                    # Tâche sans heure : Hangar rappelle par rapport à l'heure de référence (8 h), pas à minuit
                    lead = 0 if t.start_time else allday * 60
                    lines += alarm(t.title, ics.trigger(lead - r.offset_minutes))
                elif not repeated and fixed(r):
                    lines += alarm(t.title, fixed(r))
            lines.append("END:VEVENT")
    if include_team:  # échéances de mes tickets, fins de sprint (journée entière, sans rappel)
        for it in A.team_items(user, at.date() - timedelta(days=60), at.date() + timedelta(days=400), at.date()):
            title = f"{it.label} · {it.title}" if it.kind == "ticket" else f"{it.title} ({it.label})"
            lines += ["BEGIN:VEVENT", f"UID:{it.uid}@hangar", f"DTSTAMP:{stamp}",
                      f"DTSTART;VALUE=DATE:{it.date:%Y%m%d}", f"DTEND;VALUE=DATE:{it.date + timedelta(days=1):%Y%m%d}",
                      f"SUMMARY:{esc(title)}", f"DESCRIPTION:{esc(A._mail().absolute(it.url))}",
                      "TRANSP:TRANSPARENT", "CATEGORIES:Projet", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return ics.serialize(lines)


# ---------------------------------------------------------------------------
# Abonnement en lecture seule (adresse secrète à coller dans l'agenda du téléphone, Outlook…)
# ---------------------------------------------------------------------------

FEED_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{32,64}")


def regenerate_feed_token(user: User) -> str:
    """Crée (ou remplace : l'ancienne adresse cesse aussitôt de marcher) le jeton de l'abonnement. Jeton dédié,
    qui ne donne que la lecture de l'agenda (jamais un jeton d'API, qui ouvrirait aussi l'écriture)."""
    import secrets
    s = A.settings_of(user)
    s.feed_token = secrets.token_urlsafe(32)
    db.session.commit()
    return s.feed_token


def disable_feed(user: User) -> None:
    s = A.settings_of(user)
    s.feed_token = None
    db.session.commit()


def feed_user(token: str) -> User | None:
    """Compte dont `token` est le jeton d'abonnement (None : jeton inconnu, coupé, ou compte désactivé)."""
    import hmac
    if not token or not FEED_TOKEN_RE.fullmatch(token):
        return None
    s = AgendaSettings.query.filter_by(feed_token=token).first()
    if s is None or not s.feed_token or not hmac.compare_digest(s.feed_token, token):
        return None
    user = db.session.get(User, s.user_id)
    return user if user is not None and user.active else None



"""Agenda : réglages de l'import, couleurs, libellés et formats de dates communs aux autres modules."""
from __future__ import annotations

import hashlib
from datetime import date

from .. import agenda as A  # le paquet : voir hangar/agenda/__init__.py
from .. import ics
from ..models import AgendaEvent, AgendaSettings, AgendaTask
from ..utils import DAYS, DAYS_FULL, MONTHS, MONTHS_FULL


IMPORT_MODES = {
    "weeks": "Semaines couvertes par le fichier (recommandé)",
    "future": "Tout le planning à venir",
    "merge": "Ajouter et mettre à jour seulement (ne rien retirer)",
}
FIELD_LABELS = {
    "title": "Matière", "kind": "Type", "session_no": "Séance", "tentative": "Prévisionnel", "cancelled": "Annulé",
    "start": "Début", "end": "Fin", "all_day": "Journée entière", "location": "Salle", "groups": "Groupes",
    "teachers": "Intervenants", "description": "Description",
}
COMPARED = ("title", "kind", "session_no", "tentative", "cancelled", "start", "end", "all_day", "location",
            "groups", "teachers", "description")
BOOL_FIELDS = {"tentative", "cancelled", "all_day"}
META_FIELDS = ("summary", "status", "change_kind", "changed_import_id", "changes_json")

SUBJECT_COLORS = ["#3f76ff", "#8b5cf6", "#0ea5e9", "#f97316", "#14b8a6", "#6366f1", "#a855f7", "#22c55e",
                  "#ca8a04", "#06b6d4", "#d946ef", "#65a30d", "#ec4899", "#0d9488"]
KIND_COLORS = {"SPORT": "#16a34a", "MILT": "#64748b", "CONF": "#9333ea", "TPA": "#94a3b8", "BRIEFING": "#94a3b8"}
EXAM_COLOR = "#dc2626"
STATUS_LABELS = {"pending": "Programmé", "sent": "Envoyé", "missed": "Manqué (application fermée)",
                 "skipped": "Ignoré (déjà passé)", "error": "Pas envoyé (erreur)"}
# Garde-fou de l'import : une semaine où le fichier n'apporte que si peu de séances n'en perd pas la majorité
GUARD_MAX_SESSIONS = 2
# Moyens d'un rappel : la notification dans l'application part toujours ; en plus, un e-mail, ou une
# notification du système (« push ») sur chaque appareil abonné.
REMINDER_CHANNELS = {"email": "E-mail + notification dans l’application",
                     "app": "Notification dans l’application seulement",
                     "push": "Notification sur mes appareils (ordinateur, téléphone)"}
CHANNEL_SHORT = {"email": "e-mail", "app": "appli", "push": "notification"}


def valid_channel(channel, default: str = "email") -> str:
    return channel if channel in REMINDER_CHANNELS else default


def channel_short(channel) -> str:
    return CHANNEL_SHORT.get(channel, "e-mail")


# ---------------------------------------------------------------------------
# Formatage
# ---------------------------------------------------------------------------

def hm(t) -> str:
    return f"{t:%H:%M}" if t else ""


def day_short(d: date) -> str:
    return f"{DAYS[d.weekday()]} {d.day} {MONTHS[d.month - 1]}"


def day_long(d: date) -> str:
    return f"{DAYS_FULL[d.weekday()]} {d.day} {MONTHS_FULL[d.month - 1]}"


def event_when(ev: AgendaEvent) -> str:
    if ev.multi_day:
        if ev.all_day:
            if ev.last_day == ev.start.date():
                return f"{day_long(ev.start.date())} (journée entière)"
            return f"du {day_short(ev.start.date())} au {day_short(ev.last_day)}"
        return f"du {day_short(ev.start.date())} {hm(ev.start)} au {day_short(ev.last_day)} {hm(ev.end)}"
    return f"{day_long(ev.start.date())} · {hm(ev.start)} – {hm(ev.end)}"


def task_when(task: AgendaTask, d: date | None = None) -> str:
    d = d or task.due_date
    if d is None:
        return "Sans date"
    s = day_long(d)
    if task.start_time:
        s += f" · {hm(task.start_time)}" + (f" – {hm(task.end_time)}" if task.end_time else "")
    return s


def due_label(d: date | None, today: date | None = None) -> str:
    if d is None:
        return ""
    today = today or date.today()
    diff = (d - today).days
    if diff == 0:
        return "aujourd’hui"
    if diff == 1:
        return "demain"
    if diff == -1:
        return "hier"
    if 1 < diff < 7:
        return DAYS_FULL[d.weekday()]
    if diff < 0:
        return f"il y a {-diff} j"
    return day_short(d)


def duration_label(minutes: int) -> str:
    h, m = divmod(int(minutes), 60)
    if h and m:
        return f"{h} h {m:02d}"
    return f"{h} h" if h else f"{m} min"


def kind_label(kind: str, short: bool = False) -> str:
    return ics.kind_label(kind, short)


def is_exam(ev: AgendaEvent) -> bool:
    return ics.is_exam(ev.kind, ev.title)


def event_color(ev: AgendaEvent) -> str:
    if is_exam(ev):
        return EXAM_COLOR
    if ev.kind in KIND_COLORS:
        return KIND_COLORS[ev.kind]
    h = int(hashlib.md5(A._ancienne_cle(ev.title).encode()).hexdigest()[:8], 16)
    return SUBJECT_COLORS[h % len(SUBJECT_COLORS)]


def settings_of(user) -> AgendaSettings:
    return AgendaSettings.of(user)



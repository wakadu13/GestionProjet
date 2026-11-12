"""Agenda personnel : emploi du temps importé (.ics), tâches, rappels par e-mail, vues calendrier.

Règle des imports : **le dernier fichier importé fait foi.**
- une séance du fichier (repérée par son UID) est créée ou mise à jour (notes et rappels personnels conservés) ;
- une séance déjà connue mais absente du nouveau fichier est marquée « retirée » si elle tombe dans une
  semaine couverte par le fichier, à partir d'aujourd'hui (le passé n'est jamais retiré). Une semaine n'est
  couverte que si le fichier y apporte au moins une vraie séance : d'une seule journée et pas « prévisionnelle ».
  Un bloc de plusieurs jours (formation militaire…) ou une prévision isolée ajoute et met à jour, sans jamais
  décider d'un retrait ;
- garde-fou : si une semaine voisine perdait la majorité de ses séances alors que le fichier n'y apporte qu'une
  ou deux séances, rien n'y est retiré et l'import le signale dans ses avertissements ;
- chaque import garde l'état d'avant : le dernier import peut être annulé (les rappels reviennent aussi).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from flask import current_app
from markupsafe import escape

from . import ics
from .extensions import db
from .models import (
    PRIORITY_ORDER, RECURRENCES, REMINDER_OFFSETS, AgendaEvent, AgendaImport, AgendaReminder, AgendaSettings,
    AgendaSubjectLink, AgendaTask, Notification, User, now,
)
from .utils import DAYS, DAYS_FULL, MONTHS, MONTHS_FULL, normalize

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
    h = int(hashlib.md5(normalize(ev.title).encode()).hexdigest()[:8], 16)
    return SUBJECT_COLORS[h % len(SUBJECT_COLORS)]


def settings_of(user) -> AgendaSettings:
    return AgendaSettings.of(user)


# ---------------------------------------------------------------------------
# Import de l'emploi du temps
# ---------------------------------------------------------------------------

def _norm(f: str, v):
    if f in BOOL_FIELDS:
        return bool(v)
    if isinstance(v, datetime):
        return v.isoformat(timespec="minutes")
    if isinstance(v, date):
        return v.isoformat()
    return v if v is not None else ""


def _snapshot(ev: AgendaEvent) -> dict:
    snap = {f: _norm(f, getattr(ev, f)) for f in COMPARED}
    for f in META_FIELDS:
        snap[f] = getattr(ev, f)
    return snap


def _restore(ev: AgendaEvent, snap: dict) -> None:
    for k, v in snap.items():
        if k in ("start", "end") and isinstance(v, str):
            v = datetime.fromisoformat(v)
        setattr(ev, k, v)


def _store_file(user: User, raw: bytes, filename: str, at: datetime) -> str:
    folder = os.path.join(current_app.config["DATA_DIR"], "agenda", str(user.id))
    os.makedirs(folder, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename(filename or "planning.ics")).strip("._")[-80:]
    name = f"{at:%Y%m%d_%H%M%S}_{safe or 'planning.ics'}"
    with open(os.path.join(folder, name), "wb") as fh:
        fh.write(raw)
    return name


def stored_path(imp: AgendaImport) -> str | None:
    if not imp.stored_name:
        return None
    p = os.path.join(current_app.config["DATA_DIR"], "agenda", str(imp.user_id), os.path.basename(imp.stored_name))
    return p if os.path.exists(p) else None


def _is_real_session(e) -> bool:
    """Vraie séance pour décider de la couverture d'une semaine : d'une seule journée et pas « prévisionnelle »."""
    if e.tentative or e.all_day:
        return False
    end = e.end or e.start
    last = (end - timedelta(microseconds=1)).date() if end > e.start else e.start.date()
    return last == e.start.date()


def covered_weeks(events) -> dict[date, int]:
    """{lundi : nombre de vraies séances du fichier dans la semaine} (voir `_is_real_session`)."""
    weeks: dict[date, int] = {}
    for e in events:
        if _is_real_session(e):
            w = ics.week_start(e.start.date())
            weeks[w] = weeks.get(w, 0) + 1
    return weeks


def _revive_reminders(ev: AgendaEvent, since: datetime | None, at: datetime) -> None:
    """Séance de retour dans le planning : les rappels ignorés pendant qu'elle était retirée (depuis `since`)
    repartent, tant que la séance n'est pas finie."""
    if since is None or not ev.is_active or ev.end <= at:
        return
    for r in ev.reminders:
        if r.status == "skipped" and r.remind_at is not None and r.remind_at >= since - timedelta(minutes=5):
            r.status, r.sent_at = "pending", None


def import_calendar(user: User, raw: bytes | str, filename: str = "planning.ics", mode: str = "weeks",
                    at: datetime | None = None) -> AgendaImport:
    """Importe un fichier .ics : le contenu du fichier fait foi pour les semaines qu'il couvre vraiment."""
    at = at or now()
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    cal = ics.parse(raw)
    if not cal.events:
        raise ics.ICSError("Aucune séance trouvée dans ce fichier.")
    mode = mode if mode in IMPORT_MODES else "weeks"
    s = settings_of(user)  # créé (et validé) avant de toucher aux séances
    parsed = {e.uid: e for e in cal.events}
    weeks = covered_weeks(cal.events)
    if weeks:
        lo, hi = min(weeks), max(weeks) + timedelta(days=6)
    else:  # seulement des blocs de plusieurs jours ou des prévisions : période affichée, rien n'est retiré
        starts = sorted(e.start.date() for e in cal.events)
        lo, hi = ics.week_start(starts[0]), ics.week_start(starts[-1]) + timedelta(days=6)

    # Périodes déjà couvertes par les imports précédents : une séance qui y apparaît est « ajoutée »
    # (à signaler), sinon c'est une « nouvelle » semaine (rien de particulier à signaler).
    covered = [(i.range_start, i.range_end) for i in
               AgendaImport.query.filter_by(user_id=user.id, undone_at=None) if i.range_start and i.range_end]

    imp = AgendaImport(user_id=user.id, filename=(filename or "planning.ics")[:255], mode=mode, range_start=lo,
                       range_end=hi, total=len(parsed), warnings="\n".join(cal.warnings), created_at=at,
                       added=0, modified=0, removed=0, restored=0, unchanged=0)
    db.session.add(imp)
    db.session.flush()

    existing = {e.uid: e for e in AgendaEvent.query.filter_by(user_id=user.id)}
    known_by_week: dict[date, int] = {}  # séances actives par semaine avant l'import (garde-fou)
    for e in existing.values():
        if e.status == "active":
            w = ics.week_start(e.start.date())
            known_by_week[w] = known_by_week.get(w, 0) + 1
    details, touched, revived = [], [], []
    for uid, pe in parsed.items():
        vals = pe.values()
        ev = existing.get(uid)
        if ev is None:
            d0 = pe.start.date()
            notable = any(a <= d0 <= b for a, b in covered)
            ev = AgendaEvent(user_id=user.id, uid=uid, created_import_id=imp.id, changed_import_id=imp.id,
                             change_kind="added" if notable else "new", created_at=at, updated_at=at,
                             status="active", **vals)
            db.session.add(ev)
            db.session.flush()
            imp.added += 1
            details.append({"type": "added", "notable": notable, "event_id": ev.id, "after": _snapshot(ev)})
            continue
        before = _snapshot(ev)
        diffs = {f: [before[f], _norm(f, vals[f])] for f in COMPARED if before[f] != _norm(f, vals[f])}
        was_removed = ev.status != "active"
        if not diffs and not was_removed:
            ev.summary = vals["summary"]
            imp.unchanged += 1
            continue
        for k, v in vals.items():
            setattr(ev, k, v)
        kind = "restored" if was_removed else "modified"
        ev.status, ev.change_kind, ev.changed_import_id, ev.updated_at = "active", kind, imp.id, at
        ev.changes_json = json.dumps(diffs, ensure_ascii=False) if diffs else ""
        if kind == "restored":
            imp.restored += 1
            removal = db.session.get(AgendaImport, before.get("changed_import_id") or 0)
            revived.append((ev, removal.created_at if removal is not None else None))
        else:
            imp.modified += 1
        details.append({"type": kind, "event_id": ev.id, "fields": list(diffs), "before": before,
                        "after": _snapshot(ev)})
        touched.append(ev)

    imp.guarded_weeks = []  # semaines épargnées par le garde-fou (pour le message après l'import)
    if mode != "merge":
        floor = at.date()  # le passé n'est jamais retiré
        to_remove: dict[date, list[AgendaEvent]] = {}
        for uid, ev in existing.items():
            if uid in parsed or ev.status != "active":
                continue
            d0 = ev.start.date()
            w = ics.week_start(d0)
            if d0 < floor or (mode == "weeks" and w not in weeks):
                continue
            to_remove.setdefault(w, []).append(ev)
        if mode == "weeks" and weeks:
            main = max(weeks.values())
            guard_lines = []
            for w, evs in sorted(to_remove.items()):
                n, known = weeks[w], known_by_week.get(w, 0)
                # Semaine voisine (pas la semaine principale du fichier) qui perdrait la majorité de ses séances
                if n <= GUARD_MAX_SESSIONS and n < main and 2 * len(evs) > known:
                    imp.guarded_weeks.append(w)
                    guard_lines.append(
                        f"Semaine du {day_short(w)} : le fichier n’y apporte que {n} séance(s), alors que "
                        f"{len(evs)} des {known} séances déjà connues en auraient été retirées. Par prudence, "
                        "rien n’a été retiré cette semaine-là : importe le fichier de cette semaine pour la mettre à jour.")
                    del to_remove[w]
            if guard_lines:
                imp.warnings = "\n".join(([imp.warnings] if imp.warnings else []) + guard_lines)
        removing = {ev.id for evs in to_remove.values() for ev in evs}
        for uid, ev in existing.items():
            if ev.id not in removing:
                continue
            before = _snapshot(ev)
            ev.status, ev.change_kind, ev.changed_import_id, ev.updated_at = "removed", "removed", imp.id, at
            ev.changes_json = ""
            imp.removed += 1
            details.append({"type": "removed", "event_id": ev.id, "before": before})

    imp.details_json = json.dumps(details, ensure_ascii=False)
    try:
        imp.stored_name = _store_file(user, raw, filename, at)
    except OSError:
        imp.stored_name = ""
    for ev in touched:
        sync_event_reminders(ev, at)
    for ev, since in revived:
        _revive_reminders(ev, since, at)
    ensure_exam_reminders(user, at)
    db.session.commit()
    if s.import_email and imp.changed:
        queue_import_email(user, imp, at)
        db.session.commit()
    return imp


def latest_import(user: User) -> AgendaImport | None:
    return (AgendaImport.query.filter_by(user_id=user.id, undone_at=None)
            .order_by(AgendaImport.id.desc()).first())


def undo_import(imp: AgendaImport, at: datetime | None = None) -> None:
    """Rétablit l'état d'avant l'import (uniquement pour le dernier import), rappels compris : les rappels
    automatiques des contrôles sont recréés et les rappels ignorés pendant le retrait repartent."""
    at = at or now()
    last = AgendaImport.query.filter_by(user_id=imp.user_id, undone_at=None).order_by(AgendaImport.id.desc()).first()
    if last is None or last.id != imp.id:
        raise ValueError("Seul le dernier import peut être annulé.")
    for d in reversed(imp.details):
        ev = db.session.get(AgendaEvent, d.get("event_id"))
        if ev is None:
            continue
        if d["type"] == "added":
            AgendaTask.query.filter_by(event_id=ev.id).update({"event_id": None})
            db.session.delete(ev)
        else:
            _restore(ev, d["before"])
            ev.updated_at = at
            sync_event_reminders(ev, at)
            if d["type"] == "removed":
                _revive_reminders(ev, imp.created_at, at)
    imp.undone_at = at
    db.session.flush()
    user = db.session.get(User, imp.user_id)
    if user is not None:
        ensure_exam_reminders(user, at)
    db.session.commit()


def recent_import_ids(user: User, at: datetime | None = None, days: int = 7) -> set[int]:
    at = at or now()
    rows = AgendaImport.query.filter(AgendaImport.user_id == user.id, AgendaImport.undone_at.is_(None),
                                     AgendaImport.created_at >= at - timedelta(days=days)).all()
    return {r.id for r in rows}


def change_badge(ev: AgendaEvent, recent: set[int]) -> tuple[str, str] | None:
    """(libellé, classe) du badge de changement récent, ou None."""
    if ev.changed_import_id not in recent:
        return None
    return {"added": ("Ajouté", "accent"), "modified": ("Modifié", "warning"), "removed": ("Retiré", "danger"),
            "restored": ("Rétabli", "accent")}.get(ev.change_kind)


def describe_change(field_name: str, before, after) -> str:
    def fmt(v):
        if field_name in BOOL_FIELDS:
            return "oui" if v else "non"
        if field_name in ("start", "end") and v:
            dt = datetime.fromisoformat(v)
            return f"{day_short(dt.date())} {hm(dt)}"
        return v or "—"
    return f"{FIELD_LABELS.get(field_name, field_name)} : {fmt(before)} → {fmt(after)}"


def change_lines(changes: dict) -> list[str]:
    out = []
    if "start" in changes and "end" in changes:
        b0, a0 = changes["start"]
        b1, a1 = changes["end"]
        bs, as_ = datetime.fromisoformat(b0), datetime.fromisoformat(a0)
        be, ae = datetime.fromisoformat(b1), datetime.fromisoformat(a1)
        out.append(f"Horaire : {day_short(bs.date())} {hm(bs)}–{hm(be)} → {day_short(as_.date())} {hm(as_)}–{hm(ae)}")
        changes = {k: v for k, v in changes.items() if k not in ("start", "end")}
    out += [describe_change(k, *v) for k, v in changes.items()]
    return out


# ---------------------------------------------------------------------------
# Tâches (y compris répétées)
# ---------------------------------------------------------------------------

def next_date(d: date, rec: str, month_day: int | None = None) -> date:
    """Occurrence suivante. `month_day` : jour d'origine d'une répétition mensuelle (le 31 reste le 31 quand
    le mois le permet, au lieu de rester bloqué au 28 après février)."""
    if rec == "daily":
        return d + timedelta(days=1)
    if rec == "weekdays":
        n = d + timedelta(days=1)
        while n.weekday() >= 5:
            n += timedelta(days=1)
        return n
    if rec == "weekly":
        return d + timedelta(days=7)
    if rec == "biweekly":
        return d + timedelta(days=14)
    if rec == "monthly":
        y, m = (d.year + (d.month // 12), d.month % 12 + 1)
        return date(y, m, min(month_day or d.day, monthrange(y, m)[1]))
    return d


def month_day(task: AgendaTask) -> int | None:
    """Jour du mois d'origine d'une tâche mensuelle (les tâches d'avant cette colonne gardent le jour de leur date)."""
    if task.recurrence != "monthly":
        return None
    return task.recurrence_day or (task.due_date.day if task.due_date else None)


def task_next_date(task: AgendaTask, d: date) -> date:
    return next_date(d, task.recurrence, month_day(task))


def occurrence_dates(task: AgendaTask, limit: int = 1500):
    """Dates des occurrences d'une tâche à partir de son échéance (une seule si elle n'est pas répétée)."""
    d = task.due_date
    if d is None:
        return
    if not task.recurrence:
        yield d
        return
    for _ in range(limit):
        if task.recurrence_until and d > task.recurrence_until:
            return
        yield d
        d = task_next_date(task, d)


@dataclass
class Occ:
    """Occurrence d'une tâche à une date (les suivantes d'une tâche répétée sont « fantômes »)."""
    task: AgendaTask
    date: date
    ghost: bool = False


def task_occurrences(user: User, d0: date, d1: date, include_done: bool = True) -> list[Occ]:
    q = AgendaTask.query.filter(AgendaTask.user_id == user.id, AgendaTask.due_date.isnot(None))
    if not include_done:
        q = q.filter(AgendaTask.done_at.is_(None))
    out = []
    for t in q:
        if t.recurrence and not t.done_at:
            d, first, n = t.due_date, True, 0
            while d <= d1 and n < 400:
                if t.recurrence_until and d > t.recurrence_until:
                    break
                if d >= d0:
                    out.append(Occ(t, d, ghost=not first))
                first = False
                d = task_next_date(t, d)
                n += 1
        elif d0 <= t.due_date <= d1:
            out.append(Occ(t, t.due_date))
    return out


def complete_task(task: AgendaTask, at: datetime | None = None) -> date | None:
    """Coche une tâche. Pour une tâche répétée, passe à l'occurrence suivante (retournée)."""
    at = at or now()
    if task.done_at:
        task.done_at = None
        task.updated_at = at
        resync_task(task, at)
        return None
    if task.recurrence and task.due_date:
        if task.recurrence == "monthly" and not task.recurrence_day:
            task.recurrence_day = task.due_date.day
        n = task_next_date(task, task.due_date)
        while n < at.date():
            n = task_next_date(task, n)
        if not task.recurrence_until or n <= task.recurrence_until:
            task.due_date = n
            task.done_count = (task.done_count or 0) + 1
            task.updated_at = at
            resync_task(task, at, reset=True)
            return n
    task.done_at = at
    task.updated_at = at
    return None


def undo_complete(task: AgendaTask, previous: date | None, at: datetime | None = None) -> bool:
    """Annule la coche d'une tâche répétée (bouton « Annuler » après « Prochaine fois : … ») : l'échéance revient
    à `previous`. Retourne False si ce n'est pas possible (tâche non répétée, date incohérente)."""
    at = at or now()
    if (not task.recurrence or task.done_at or previous is None or task.due_date is None
            or not task.due_date - timedelta(days=400) <= previous < task.due_date):
        return False
    task.due_date = previous
    task.done_count = max((task.done_count or 0) - 1, 0)
    task.updated_at = at
    resync_task(task, at, reset=True)
    return True


def task_start(task: AgendaTask, d: date | None = None) -> datetime | None:
    d = d or task.due_date
    if d is None:
        return None
    return datetime.combine(d, task.start_time) if task.start_time else datetime.combine(d, time(0))


def task_end(task: AgendaTask, d: date | None = None) -> datetime | None:
    d = d or task.due_date
    if d is None:
        return None
    if task.start_time is None:
        return datetime.combine(d + timedelta(days=1), time(0))
    if task.end_time and task.end_time > task.start_time:
        return datetime.combine(d, task.end_time)
    return datetime.combine(d, task.start_time) + timedelta(hours=1)


def open_task_groups(user: User, today: date) -> dict[str, list[AgendaTask]]:
    tasks = AgendaTask.query.filter(AgendaTask.user_id == user.id, AgendaTask.done_at.is_(None)).all()
    groups = {"overdue": [], "today": [], "tomorrow": [], "week": [], "later": [], "nodate": []}
    for t in tasks:
        if t.due_date is None:
            groups["nodate"].append(t)
        elif t.due_date < today:
            groups["overdue"].append(t)
        elif t.due_date == today:
            groups["today"].append(t)
        elif t.due_date == today + timedelta(days=1):
            groups["tomorrow"].append(t)
        elif t.due_date <= today + timedelta(days=7):
            groups["week"].append(t)
        else:
            groups["later"].append(t)
    key = lambda t: (t.due_date or date.max, t.start_time or time(23, 59), PRIORITY_ORDER.get(t.priority, 9), t.id)  # noqa: E731
    for g in groups.values():
        g.sort(key=key)
    groups["nodate"].sort(key=lambda t: (PRIORITY_ORDER.get(t.priority, 9), -t.id))
    return groups


TASK_GROUP_LABELS = [("overdue", "En retard"), ("today", "Aujourd’hui"), ("tomorrow", "Demain"),
                     ("week", "Dans les 7 jours"), ("later", "Plus tard"), ("nodate", "Sans date")]


def categories(user: User) -> list[str]:
    rows = (db.session.query(AgendaTask.category).filter(AgendaTask.user_id == user.id, AgendaTask.category != "")
            .distinct().all())
    return sorted({r[0] for r in rows if r[0]}, key=str.lower)


# ---------------------------------------------------------------------------
# Rappels
# ---------------------------------------------------------------------------

def task_anchor(task: AgendaTask, s: AgendaSettings, d: date | None = None) -> datetime | None:
    """Moment de référence des rappels « X min avant » (occurrence `d`, par défaut l'échéance)."""
    d = d or task.due_date
    if d is None:
        return None
    return datetime.combine(d, task.start_time or time(s.allday_hour if s.allday_hour is not None else 8))


def task_reminder_time(task: AgendaTask, offset: int, s: AgendaSettings, after: datetime) -> datetime | None:
    """Heure du rappel (`offset` minutes avant) de la première occurrence dont le rappel tombe après `after`.
    None si la série est finie."""
    for d in occurrence_dates(task):
        t = task_anchor(task, s, d) - timedelta(minutes=offset)
        if t > after:
            return t
    return None


def _resync_recurring(rem: AgendaReminder, task: AgendaTask, s: AgendaSettings, at: datetime, reset: bool) -> None:
    """Tâche répétée : le rappel vise la première occurrence à venir, même si une occurrence précédente n'a pas
    été cochée (l'échéance n'avance pas, la tâche reste en retard), et jamais une occurrence déjà rappelée."""
    after = at - timedelta(minutes=5, microseconds=1)
    if rem.sent_at is not None:
        after = max(after, rem.sent_at)
    new = task_reminder_time(task, rem.offset_minutes, s, after)
    if new is None:  # série terminée : comme une tâche simple
        _resync(rem, task_anchor(task, s), at, reset=reset)
        return
    if new != rem.remind_at or reset:
        rem.remind_at, rem.status = new, "pending"


def _resync(rem: AgendaReminder, anchor: datetime | None, at: datetime, reset: bool = False) -> None:
    if rem.offset_minutes is None:
        return
    new = anchor - timedelta(minutes=rem.offset_minutes) if anchor else None
    if new != rem.remind_at or reset:
        rem.remind_at = new
        if new is None:
            rem.status, rem.sent_at = "pending", None
        elif new >= at - timedelta(minutes=5):
            rem.status, rem.sent_at = "pending", None
        elif rem.status == "pending" or reset:
            rem.status = "skipped"


def resync_task(task: AgendaTask, at: datetime | None = None, reset: bool = False) -> None:
    at = at or now()
    s = settings_of_id(task.user_id)
    anchor = task_anchor(task, s)
    for r in task.reminders:
        if task.recurrence and anchor is not None and r.offset_minutes is not None:
            _resync_recurring(r, task, s, at, reset)
        else:
            _resync(r, anchor, at, reset=reset)


def sync_event_reminders(ev: AgendaEvent, at: datetime | None = None) -> None:
    at = at or now()
    for r in ev.reminders:
        _resync(r, ev.start, at)


def set_task_reminders(task: AgendaTask, offsets: list[int], channel: str, at: datetime | None = None) -> None:
    """Remplace les rappels « relatifs » d'une tâche par ceux demandés (les rappels à date fixe sont gardés)."""
    at = at or now()
    channel = channel if channel in ("email", "app") else "email"
    wanted = sorted({int(o) for o in offsets if o is not None and int(o) >= 0})
    for r in list(task.reminders):
        if r.offset_minutes is not None and r.offset_minutes not in wanted:
            task.reminders.remove(r)
            db.session.delete(r)
    have = {r.offset_minutes for r in task.reminders if r.offset_minutes is not None}
    for o in wanted:
        if o not in have:
            task.reminders.append(AgendaReminder(user_id=task.user_id, offset_minutes=o, channel=channel, created_at=at))
    for r in task.reminders:
        r.channel = channel
    db.session.flush()
    resync_task(task, at)


def settings_of_id(user_id: int) -> AgendaSettings:
    return db.session.get(AgendaSettings, user_id) or AgendaSettings(user_id=user_id, allday_hour=8)


def add_fixed_reminder(user_id: int, when: datetime, channel: str = "email", task: AgendaTask | None = None,
                       event: AgendaEvent | None = None, at: datetime | None = None) -> AgendaReminder:
    at = at or now()
    r = AgendaReminder(user_id=user_id, offset_minutes=None, remind_at=when,
                       channel=channel if channel in ("email", "app") else "email", created_at=at,
                       status="pending" if when >= at - timedelta(minutes=5) else "skipped")
    if task is not None:
        task.reminders.append(r)
    if event is not None:
        event.reminders.append(r)
    db.session.add(r)
    return r


def add_event_reminder(ev: AgendaEvent, offset: int, channel: str = "email", auto: bool = False,
                       at: datetime | None = None) -> AgendaReminder:
    at = at or now()
    r = AgendaReminder(user_id=ev.user_id, offset_minutes=offset, channel=channel, auto=auto, created_at=at)
    ev.reminders.append(r)
    db.session.add(r)
    _resync(r, ev.start, at, reset=True)
    return r


def ensure_exam_reminders(user: User, at: datetime | None = None) -> None:
    """Crée / ajuste / retire les rappels automatiques des contrôles selon les préférences."""
    at = at or now()
    s = settings_of(user)
    offset = s.exam_reminder or 0
    evs = AgendaEvent.query.filter(AgendaEvent.user_id == user.id, AgendaEvent.start > at).all()
    for ev in evs:
        autos = [r for r in ev.reminders if r.auto]
        wanted = offset > 0 and ev.status == "active" and not ev.cancelled and is_exam(ev)
        if not wanted:
            for r in autos:
                if r.status == "pending":
                    ev.reminders.remove(r)
                    db.session.delete(r)
            continue
        if autos:
            r = autos[0]
            if r.offset_minutes != offset:
                r.offset_minutes = offset
                _resync(r, ev.start, at, reset=True)
        else:
            add_event_reminder(ev, offset, channel=s.reminder_channel or "email", auto=True, at=at)


FIXED_REMINDER_GRACE = timedelta(hours=24)  # un rappel à date fixe part encore jusqu'à 24 h après son heure


def _reminder_occurrence(r: AgendaReminder) -> date | None:
    """Occurrence de la tâche visée par un rappel « X min avant » (une tâche répétée change d'occurrence)."""
    t = r.task
    if t is None or t.due_date is None:
        return None
    if not t.recurrence or r.offset_minutes is None or r.remind_at is None:
        return t.due_date
    return max((r.remind_at + timedelta(minutes=r.offset_minutes)).date(), t.due_date)


def reminder_target_end(r: AgendaReminder) -> datetime | None:
    if r.task is not None:
        return task_end(r.task, _reminder_occurrence(r))
    if r.event is not None:
        return r.event.end
    return None


def reminder_deadline(r: AgendaReminder) -> datetime | None:
    """Au-delà, le rappel ne sert plus : fin de la séance (ou de l'occurrence de la tâche) pour un rappel
    « X min avant » ; 24 h après son heure pour un rappel à date fixe (qui peut tomber après l'échéance :
    relance d'une tâche en retard)."""
    if r.offset_minutes is None:
        return r.remind_at + FIXED_REMINDER_GRACE if r.remind_at else None
    return reminder_target_end(r)


def _rearm(r: AgendaReminder, at: datetime) -> None:
    """Tâche répétée : après l'envoi (ou un rappel manqué), le rappel passe à l'occurrence suivante, sans faire
    avancer l'échéance (une occurrence non cochée reste en retard)."""
    t = r.task
    if t is None or not t.recurrence or r.offset_minutes is None or t.done_at or r.remind_at is None:
        return
    nxt = task_reminder_time(t, r.offset_minutes, settings_of_id(t.user_id), max(at, r.remind_at))
    if nxt is not None:
        r.remind_at, r.status = nxt, "pending"


def _process_reminder(r: AgendaReminder, at: datetime) -> int:
    user = db.session.get(User, r.user_id)
    if user is None or not user.active or (r.task is None and r.event is None):
        db.session.delete(r)
        return 0
    if (r.task is not None and r.task.done_at) or (r.event is not None and not r.event.is_active):
        r.status = "skipped"
        return 0
    end = reminder_deadline(r)
    if end is not None and end <= at:  # l'application était fermée : trop tard, on n'envoie plus
        r.status = "missed"
        _rearm(r, at)
        return 0
    r.email = send_reminder(r, user, at)
    r.status, r.sent_at = "sent", at
    _rearm(r, at)
    return 1


def process_reminders(at: datetime | None = None) -> int:
    """Envoie les rappels arrivés à échéance. Retourne le nombre de rappels envoyés.

    Chaque rappel est validé à part : un rappel en erreur passe à l'état « error » (noté sur /erreurs/) sans
    empêcher les autres de partir ni revenir en boucle à chaque passage du fil."""
    at = at or now()
    due = (AgendaReminder.query.filter(AgendaReminder.status == "pending", AgendaReminder.remind_at.isnot(None),
                                       AgendaReminder.remind_at <= at).order_by(AgendaReminder.remind_at).all())
    sent = 0
    for rid in [r.id for r in due]:
        try:
            r = db.session.get(AgendaReminder, rid)
            if r is None or r.status != "pending":
                continue
            sent += _process_reminder(r, at)
            db.session.commit()
        except Exception:  # noqa: BLE001 - un rappel en panne ne doit pas bloquer les autres
            db.session.rollback()
            current_app.logger.exception("Rappel de l’agenda n° %s", rid)
            _mail().noter_erreur(f"Rappel de l’agenda n° {rid}")
            r = db.session.get(AgendaReminder, rid)
            if r is not None:
                r.status = "error"
                db.session.commit()
    db.session.commit()
    return sent


# ---------------------------------------------------------------------------
# E-mails
# ---------------------------------------------------------------------------

def _mail():
    from . import mailer
    return mailer


def mail_status(user: User, at: datetime | None = None) -> dict:
    """État de l'envoi des e-mails pour l'agenda (affiché dans l'interface), avec les e-mails de ce compte
    en échec (encore utiles à renvoyer) ou en attente d'un nouvel essai."""
    from .models import EmailMessage, MailConfig
    m = _mail()
    cfg = MailConfig.get()
    smtp = cfg.mode == "smtp" and bool(cfg.smtp_host)
    at = at or now()
    recent = (EmailMessage.query.filter(EmailMessage.to_user_id == user.id,
                                        EmailMessage.status.in_(("failed", "queued")),
                                        EmailMessage.created_at >= at - timedelta(days=30))
              .order_by(EmailMessage.id.desc()).all())
    failed = [x for x in recent if x.status == "failed" and m.deadline(x) > at]
    retrying = [x for x in recent if x.status == "queued" and x.attempts]
    return {"address": user.contact_email if m.valid_email(user.contact_email) else None, "smtp": smtp,
            "host": cfg.smtp_host, "ok": bool(m.valid_email(user.contact_email)) and smtp,
            "failed": len(failed), "retrying": len(retrying),
            "error": m.plain_error((failed or retrying)[0].error) if (failed or retrying) else ""}


def reminder_status_label(r: AgendaReminder) -> str:
    """Libellé de l'état d'un rappel : « Envoyé » seulement si son e-mail est vraiment parti."""
    if r.status == "sent" and r.email is not None:
        st = r.email.status
        if st == "failed":
            return "E-mail en échec"
        if st == "queued":
            return "E-mail en attente d’un nouvel essai" if r.email.attempts else "E-mail en cours d’envoi"
        if st == "outbox":
            return "Envoyé (boîte d’envoi locale)"
    return STATUS_LABELS.get(r.status, r.status)


def _row(label: str, value: str) -> str:
    if not value:
        return ""
    return (f'<tr><td style="padding:3px 12px 3px 0;color:#6b7280;white-space:nowrap;vertical-align:top">{escape(label)}</td>'
            f'<td style="padding:3px 0">{value}</td></tr>')


def _md(text: str) -> str:
    from .utils import render_markdown
    return str(render_markdown(text, link_refs=False)) if text else ""


def send_reminder(r: AgendaReminder, user: User, at: datetime):
    """Notification dans l'application et, selon le moyen choisi, e-mail mis en file (retourné, sinon None)."""
    m = _mail()
    cfg = m._cfg()
    if r.task is not None:
        t = r.task
        title, when = t.title, task_when(t, _reminder_occurrence(r))
        rows = _row("Quand", escape(when))
        if t.event is not None:
            rows += _row("Séance liée", escape(f"{t.event.title} ({event_when(t.event)})"))
        if t.category:
            rows += _row("Catégorie", escape(t.category))
        body = f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin:4px 0 10px">{rows}</table>'
        if t.notes:
            body += f'<div style="border-left:3px solid #e4e7ec;padding:2px 12px;color:#374151">{_md(t.notes)}</div>'
        path = f"/agenda/tasks?focus={t.id}"
    else:
        ev = r.event
        title, when = ev.title + (f" ({kind_label(ev.kind)})" if ev.kind else ""), event_when(ev)
        rows = (_row("Quand", escape(when)) + _row("Salle", escape(ev.location)) + _row("Intervenants", escape(ev.teachers))
                + _row("Groupes", escape(ev.groups)))
        body = f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin:4px 0 10px">{rows}</table>'
        if ev.description:
            body += f'<p style="color:#374151;white-space:pre-line">{escape(ev.description)}</p>'
        if ev.note:
            body += (f'<div style="border-left:3px solid #3f76ff;padding:2px 12px;color:#374151"><b>Ma note</b>'
                     f'{_md(ev.note)}</div>')
        path = f"/agenda/calendar?date={ev.start.date().isoformat()}"
    short = when.split(" · ")[-1] if " · " in when else when
    db.session.add(Notification(user_id=user.id, kind="reminder", message=f"⏰ Rappel : {title} — {when}"[:500],
                                url=path, created_at=at))
    if r.channel == "email" and m.valid_email(user.contact_email):
        html = m.layout(f"⏰ {title}", f"<p>Petit rappel ({escape(r.label.lower())}) :</p>{body}",
                        button=("Ouvrir mon agenda", m.absolute(path, cfg)), cfg=cfg)
        return m.queue_email(user.contact_email, f"⏰ Rappel : {title} — {short}"[:300], html, kind="reminder",
                             to_user=user, expires_at=reminder_deadline(r))
    return None


def _event_line(ev: AgendaEvent) -> str:
    color = event_color(ev)
    meta = " · ".join(x for x in (kind_label(ev.kind), ev.location, ev.teachers) if x)
    when = "Journée" if ev.multi_day else f"{hm(ev.start)}–{hm(ev.end)}"
    flag = ' <span style="color:#d97706">(prévisionnel)</span>' if ev.tentative else ""
    if ev.cancelled or ev.status != "active":
        flag = ' <span style="color:#dc2626">(retiré / annulé)</span>'
    return (f'<tr><td style="padding:6px 10px 6px 0;white-space:nowrap;font-family:Menlo,Consolas,monospace;'
            f'font-size:12.5px;vertical-align:top">{when}</td><td style="padding:6px 0;border-left:3px solid {color};'
            f'padding-left:10px"><b>{escape(ev.title)}</b>{flag}<div style="color:#6b7280;font-size:12.5px">'
            f'{escape(meta)}</div></td></tr>')


def _task_line(t: AgendaTask, today: date, d: date | None = None) -> str:
    """Ligne d'une tâche dans le résumé ; `d` : date de l'occurrence listée (par défaut l'échéance)."""
    d = d or t.due_date
    when = hm(t.start_time) if t.start_time else ""
    late = (f' <span style="color:#dc2626">(prévue {escape(due_label(d, today))})</span>'
            if d and d < today else "")
    return (f'<li style="margin:4px 0">{"<b>" + when + "</b> " if when else ""}{escape(t.title)}{late}'
            f'{" <span style=color:#6b7280>· " + escape(t.category) + "</span>" if t.category else ""}</li>')


# ---------------------------------------------------------------------------
# Résumé quotidien : sections de l'agenda (voir hangar/digest.py)
# ---------------------------------------------------------------------------

from . import digest  # noqa: E402


@digest.section("agenda.cours", ordre=10)
def _digest_cours(ctx):
    a = datetime.combine(ctx.jour, time(0))
    evs = [e for e in events_between(ctx.user, a, a + timedelta(days=1), include_removed=False) if not e.cancelled]
    if not evs:
        return None
    total = sum(e.minutes for e in evs if not e.multi_day)
    timed = [e for e in evs if not e.multi_day]
    html = ""
    if timed:
        html += (f'<div style="color:#6b7280;font-size:12.5px;margin-bottom:4px">{duration_label(total)} de cours, '
                 f'de {hm(timed[0].start)} à {hm(max(e.end for e in timed))}</div>')
    html += ('<table role="presentation" cellpadding="0" cellspacing="0" width="100%">'
             + "".join(_event_line(e) for e in evs) + "</table>")
    return digest.Section("agenda.cours", "Emploi du temps", html, compte=len(evs),
                          libelle=digest.pluriel(len(evs), "séance"), couleur="#3f76ff")


@digest.section("agenda.taches", ordre=20)
def _digest_taches(ctx):
    occs = task_occurrences(ctx.user, ctx.jour, ctx.jour, include_done=False)
    overdue = (AgendaTask.query.filter(AgendaTask.user_id == ctx.user.id, AgendaTask.done_at.is_(None),
                                       AgendaTask.due_date < ctx.aujourdhui).order_by(AgendaTask.due_date).all())
    listed = {o.task.id for o in occs}  # une tâche répétée déjà listée pour le jour n'est pas relistée en retard
    overdue = [t for t in overdue if t.id not in listed]
    if not (occs or overdue):
        return None
    html = ""
    if occs:
        occs.sort(key=lambda o: (o.task.start_time or time(23, 59), o.task.title.lower()))
        html += ('<ul style="padding-left:18px;margin:0">'
                 + "".join(_task_line(o.task, ctx.aujourdhui, o.date) for o in occs) + "</ul>")
    if overdue:
        html += ('<div style="font-size:12.5px;font-weight:600;color:#dc2626;margin:10px 0 2px">'
                 f'En retard ({len(overdue)})</div><ul style="padding-left:18px;margin:0">'
                 + "".join(_task_line(t, ctx.aujourdhui) for t in overdue[:12]) + "</ul>")
    n = len(occs)
    libelle = digest.pluriel(n, "tâche") if n else digest.pluriel(len(overdue), "en retard", "en retard")
    if n and overdue:
        libelle += f" (+{len(overdue)} en retard)"
    return digest.Section("agenda.taches", "Tâches", html, compte=n or len(overdue), libelle=libelle,
                          couleur="#dc2626" if overdue else "#8b5cf6")


@digest.section("agenda.controles", ordre=40)
def _digest_controles(ctx):
    a = datetime.combine(ctx.jour, time(0))
    exams = [e for e in events_between(ctx.user, a + timedelta(days=1), a + timedelta(days=8), include_removed=False)
             if is_exam(e) and not e.cancelled]
    if not exams:
        return None
    items = [f"{escape(day_short(e.start.date()))} {hm(e.start)} — <b>{escape(e.title)}</b>"
             f"{' · ' + escape(e.location) if e.location else ''}" for e in exams]
    return digest.Section("agenda.controles", "Contrôles dans les 7 jours", digest.liste_html(items),
                          compte=len(exams), couleur="#dc2626", a_faire=False, tuile=False)


@digest.section("agenda.changements", ordre=45)
def _digest_changements(ctx):
    recent = recent_import_ids(ctx.user, ctx.at, days=2)
    if not recent:
        return None
    a = datetime.combine(ctx.aujourdhui, time(0))
    evs = (AgendaEvent.query.filter(AgendaEvent.user_id == ctx.user.id, AgendaEvent.changed_import_id.in_(recent),
                                    AgendaEvent.change_kind.in_(("added", "modified", "removed", "restored")),
                                    AgendaEvent.start >= a, AgendaEvent.start < a + timedelta(days=8))
           .order_by(AgendaEvent.start).all())
    if not evs:
        return None
    labels = {"added": "ajouté", "modified": "modifié", "removed": "retiré", "restored": "rétabli"}
    items = []
    for e in evs[:12]:
        extra = ""
        if e.change_kind in ("modified", "restored") and e.changes:
            extra = f"<br><span style='color:#6b7280'>{escape(' · '.join(change_lines(e.changes)))}</span>"
        items.append(f"<b>{labels[e.change_kind]}</b> — {escape(day_short(e.start.date()))} {hm(e.start)} : "
                     f"{escape(e.title)}{extra}")
    return digest.Section("agenda.changements", "Changements récents du planning", digest.liste_html(items),
                          compte=len(evs), couleur="#d97706", a_faire=False, tuile=False)


def build_day_email(user: User, day: date, at: datetime) -> tuple[str, str] | None:
    """Résumé quotidien (toutes les sections enregistrées). None s'il n'y a rien à faire ce jour-là."""
    built = digest.construire(user, day, at)
    return (built[0], built[1]) if built else None


def send_daily_programs(at: datetime | None = None) -> int:
    """Envoie le résumé quotidien aux comptes qui l'ont activé (une fois par jour, à l'heure choisie)."""
    at = at or now()
    m = _mail()
    n = 0
    for user in User.query.filter(User.active.is_(True), User.contact_email.isnot(None)).all():
        if not m.valid_email(user.contact_email):
            continue
        s = settings_of(user)
        if s.daily_mode not in ("morning", "evening") or at.hour < (s.daily_hour or 0) or s.last_daily_date == at.date():
            continue
        day = at.date() if s.daily_mode == "morning" else at.date() + timedelta(days=1)
        built = build_day_email(user, day, at)
        s.last_daily_date = at.date()
        if built:  # en cas de panne passagère, nouvel essai jusqu'à la fin de la journée résumée
            m.queue_email(user.contact_email, built[0], built[1], kind="agenda", to_user=user,
                          expires_at=datetime.combine(day + timedelta(days=1), time(0)))
            n += 1
    db.session.commit()
    return n


def digest_enabled(user: User) -> bool:
    s = db.session.get(AgendaSettings, user.id)
    mode = s.daily_mode if s is not None else AgendaSettings.daily_mode.default.arg
    return mode in ("morning", "evening")


def queue_import_email(user: User, imp: AgendaImport, at: datetime) -> None:
    m = _mail()
    if not m.valid_email(user.contact_email):
        return
    cfg = m._cfg()
    items = []
    for d in imp.details:
        if d["type"] == "added" and not d.get("notable"):
            continue
        snap = d.get("after") or d.get("before")
        start = datetime.fromisoformat(snap["start"])
        if start.date() < at.date():
            continue
        label = {"added": "Ajouté", "modified": "Modifié", "removed": "Retiré", "restored": "Rétabli"}[d["type"]]
        extra = ""
        if d["type"] == "modified":
            extra = "<br><span style='color:#6b7280'>" + escape(" · ".join(
                change_lines({f: [d["before"][f], d["after"][f]] for f in d.get("fields", [])}))) + "</span>"
        items.append(f"<li style='margin:5px 0'><b>{label}</b> — {escape(day_short(start.date()))} {hm(start)} : "
                     f"{escape(snap['title'])}{extra}</li>")
    new_weeks = sum(1 for d in imp.details if d["type"] == "added" and not d.get("notable"))
    if not items and not new_weeks:
        return
    content = (f"<p>Bonjour {escape(user.short_name)},</p><p>Ton emploi du temps a été mis à jour "
               f"(fichier <i>{escape(imp.filename)}</i>).</p>")
    if items:
        content += "<h2 style='font-size:14px;margin:14px 0 6px'>Changements à venir</h2><ul style='padding-left:18px'>" + "".join(items[:40]) + "</ul>"
    if new_weeks:
        content += f"<p>{new_weeks} nouvelle(s) séance(s) ajoutée(s) pour des semaines pas encore importées.</p>"
    html = m.layout("Emploi du temps mis à jour", content,
                    button=("Voir le détail", m.absolute(f"/agenda/imports/{imp.id}", cfg)), cfg=cfg)
    m.queue_email(user.contact_email, f"🔄 Emploi du temps : {imp.modified} modifié(s), {imp.removed} retiré(s), "
                  f"{imp.added} ajouté(s)", html, kind="agenda", to_user=user)


def run_jobs(app=None, at: datetime | None = None) -> dict:
    """Appelé par le fil d'envoi des e-mails (toutes les 30 s). Les deux travaux sont protégés séparément :
    une erreur dans les rappels n'empêche pas le résumé quotidien (et inversement)."""
    from .mailer import _ctx
    with _ctx(app):
        at = at or now()
        return {"reminders": _job("rappels de l’agenda", process_reminders, at),
                "daily": _job("résumé quotidien de l’agenda", send_daily_programs, at)}


def _job(nom: str, fn, at: datetime) -> int:
    try:
        return fn(at)
    except Exception:  # noqa: BLE001 - noté sur /erreurs/, le fil continue
        db.session.rollback()
        current_app.logger.exception("Erreur dans le fil d’arrière-plan : %s", nom)
        _mail().noter_erreur(f"Fil des e-mails : {nom}")
        return 0


# ---------------------------------------------------------------------------
# Requêtes et vues calendrier
# ---------------------------------------------------------------------------

def events_between(user: User, a: datetime, b: datetime, include_removed: bool = True) -> list[AgendaEvent]:
    q = AgendaEvent.query.filter(AgendaEvent.user_id == user.id, AgendaEvent.start < b,
                                 db.or_(AgendaEvent.end > a, AgendaEvent.start >= a))
    if not include_removed:
        q = q.filter(AgendaEvent.status == "active")
    return q.order_by(AgendaEvent.start, AgendaEvent.end.desc()).all()


@dataclass
class Block:
    type: str            # event | task
    obj: object          # AgendaEvent | Occ
    start: datetime
    end: datetime
    col: int = 0
    ncols: int = 1
    top: int = 0         # minutes depuis le début de la grille
    height: int = 0


def _layout_columns(blocks: list[Block]) -> None:
    """Répartit les créneaux qui se chevauchent en colonnes côte à côte."""
    blocks.sort(key=lambda b: (b.start, -(b.end - b.start).total_seconds()))
    cluster: list[Block] = []
    cols: list[datetime] = []
    cluster_end = None

    def flush():
        for c in cluster:
            c.ncols = max(1, len(cols))

    for b in blocks:
        if cluster_end is not None and b.start >= cluster_end:
            flush()
            cluster, cols, cluster_end = [], [], None
        for i, end in enumerate(cols):
            if end <= b.start:
                b.col, cols[i] = i, b.end
                break
        else:
            b.col = len(cols)
            cols.append(b.end)
        cluster.append(b)
        cluster_end = b.end if cluster_end is None else max(cluster_end, b.end)
    flush()


def week_view(user: User, ref: date, s: AgendaSettings, at: datetime | None = None) -> dict:
    at = at or now()
    monday = ics.week_start(ref)
    sunday = monday + timedelta(days=6)
    a = datetime.combine(monday, time(0))
    evs = events_between(user, a, a + timedelta(days=7), include_removed=bool(s.show_removed))
    occs = task_occurrences(user, monday, sunday)
    timed: dict[date, list[Block]] = {monday + timedelta(days=i): [] for i in range(7)}
    allday: list[dict] = []
    for ev in evs:
        if ev.multi_day:
            allday.append({"type": "event", "obj": ev, "d0": max(ev.start.date(), monday), "d1": min(ev.last_day, sunday)})
        elif ev.start.date() in timed:
            timed[ev.start.date()].append(Block("event", ev, ev.start, max(ev.end, ev.start + timedelta(minutes=15))))
    for o in occs:
        t = o.task
        if t.start_time is None:
            allday.append({"type": "task", "obj": o, "d0": o.date, "d1": o.date})
        else:
            st = datetime.combine(o.date, t.start_time)
            en = (datetime.combine(o.date, t.end_time) if t.end_time and t.end_time > t.start_time
                  else st + timedelta(minutes=30))
            timed[o.date].append(Block("task", o, st, en))
    weekend = bool(s.show_weekends) or any(timed[monday + timedelta(days=i)] for i in (5, 6)) or any(
        x["type"] == "task" and x["d0"].weekday() >= 5 for x in allday) or any(
        x["type"] == "event" and x["d0"].weekday() >= 5 and x["d1"] == x["d0"] for x in allday)
    ndays = 7 if weekend else 5
    days = [monday + timedelta(days=i) for i in range(ndays)]
    h0 = s.day_start_hour if s.day_start_hour is not None else 8
    h1 = s.day_end_hour if s.day_end_hour is not None else 19
    for d in days:
        for b in timed[d]:
            h0 = min(h0, b.start.hour)
            end_h = b.end.hour + (1 if b.end.minute or b.end.second else 0) if b.end.date() == d else 24
            h1 = max(h1, end_h)
    h0, h1 = max(0, min(h0, 23)), min(24, max(h1, h0 + 1))
    cols = []
    for d in days:
        blocks = timed[d]
        _layout_columns(blocks)
        base = datetime.combine(d, time(h0))
        for b in blocks:
            b.top = int((b.start - base).total_seconds() // 60)
            b.height = max(int((min(b.end, datetime.combine(d, time(0)) + timedelta(hours=h1)) - b.start)
                               .total_seconds() // 60), 18)
        cols.append({"date": d, "today": d == at.date(), "weekend": d.weekday() >= 5, "blocks": blocks,
                     "minutes": sum(b.obj.minutes for b in blocks if b.type == "event" and b.obj.is_active)})
    # Bandeau « journée » : rangées empilées sans chevauchement
    lanes: list[int] = []
    bars = []
    for x in sorted(allday, key=lambda x: (x["d0"], -(x["d1"] - x["d0"]).days)):
        i0 = (x["d0"] - monday).days
        i1 = min((x["d1"] - monday).days, ndays - 1)
        if i0 >= ndays:
            continue
        for li, last in enumerate(lanes):
            if last < i0:
                lanes[li] = i1
                break
        else:
            li = len(lanes)
            lanes.append(i1)
        bars.append({**x, "c0": i0, "c1": i1, "lane": li,
                     "cont_left": x["type"] == "event" and x["obj"].start.date() < monday,
                     "cont_right": x["type"] == "event" and x["obj"].last_day > days[-1]})
    now_top = None
    if monday <= at.date() <= days[-1] and h0 <= at.hour < h1:
        now_top = (at.hour - h0) * 60 + at.minute
    iso = monday.isocalendar()
    end_d = days[-1]
    if monday.month == end_d.month:
        title = f"{monday.day} – {end_d.day} {MONTHS_FULL[end_d.month - 1]} {end_d.year}"
    else:
        title = f"{monday.day} {MONTHS[monday.month - 1]} – {end_d.day} {MONTHS[end_d.month - 1]} {end_d.year}"
    total = sum(c["minutes"] for c in cols)
    # Week-end affiché seulement pour des tâches « journée » : colonnes plus étroites
    narrow_we = ndays == 7 and not s.show_weekends and not any(c["blocks"] for c in cols[5:])
    return {"monday": monday, "days": cols, "bars": bars, "lanes": len(lanes), "h0": h0, "h1": h1, "narrow_we": narrow_we,
            "hours": list(range(h0, h1)), "now_top": now_top, "title": title, "week_no": iso[1],
            "prev": monday - timedelta(days=7), "next": monday + timedelta(days=7), "total_minutes": total,
            "ndays": ndays}


def _day_items(evs: list[AgendaEvent], occs: list[Occ], d: date) -> list[dict]:
    items = []
    for ev in evs:
        if ev.start.date() <= d <= ev.last_day:
            items.append({"type": "event", "obj": ev, "multi": ev.multi_day,
                          "time": "" if ev.multi_day else hm(ev.start), "sort": (0 if ev.multi_day else 1, ev.start.time())})
    for o in occs:
        if o.date == d:
            t = o.task
            items.append({"type": "task", "obj": o, "multi": False, "time": hm(t.start_time),
                          "sort": (1 if t.start_time else 2, t.start_time or time(23, 59))})
    items.sort(key=lambda x: x["sort"])
    return items


def month_view(user: User, ref: date, s: AgendaSettings, at: datetime | None = None) -> dict:
    at = at or now()
    first = ref.replace(day=1)
    last = first.replace(day=monthrange(first.year, first.month)[1])
    start = ics.week_start(first)
    end = ics.week_start(last) + timedelta(days=6)
    a = datetime.combine(start, time(0))
    evs = events_between(user, a, datetime.combine(end + timedelta(days=1), time(0)), include_removed=bool(s.show_removed))
    occs = task_occurrences(user, start, end)
    weeks, d = [], start
    while d <= end:
        week = []
        for _ in range(7):
            week.append({"date": d, "out": d.month != first.month, "today": d == at.date(), "weekend": d.weekday() >= 5,
                         "items": _day_items(evs, occs, d)})
            d += timedelta(days=1)
        weeks.append(week)
    prev_m = (first - timedelta(days=1)).replace(day=1)
    next_m = last + timedelta(days=1)
    return {"weeks": weeks, "title": f"{MONTHS_FULL[first.month - 1]} {first.year}", "prev": prev_m, "next": next_m,
            "first": first}


def list_view(user: User, ref: date, s: AgendaSettings, days: int = 14, at: datetime | None = None) -> dict:
    at = at or now()
    a = datetime.combine(ref, time(0))
    end = ref + timedelta(days=days - 1)
    evs = events_between(user, a, datetime.combine(end + timedelta(days=1), time(0)), include_removed=bool(s.show_removed))
    occs = task_occurrences(user, ref, end)
    out = []
    for i in range(days):
        d = ref + timedelta(days=i)
        items = _day_items(evs, occs, d)
        if items or d == at.date():
            out.append({"date": d, "today": d == at.date(), "items": items,
                        "minutes": sum(x["obj"].minutes for x in items
                                       if x["type"] == "event" and not x["multi"] and x["obj"].is_active)})
    return {"days": out, "start": ref, "end": end, "prev": ref - timedelta(days=days), "next": ref + timedelta(days=days)}


def dashboard(user: User, at: datetime | None = None, ref: date | None = None) -> dict:
    at = at or now()
    today = at.date()
    s = settings_of(user)
    t0 = datetime.combine(today, time(0))
    today_evs = events_between(user, t0, t0 + timedelta(days=1), include_removed=bool(s.show_removed))
    tomorrow = today + timedelta(days=1)
    tomorrow_evs = [e for e in events_between(user, t0 + timedelta(days=1), t0 + timedelta(days=2), include_removed=False)
                    if not e.cancelled and not e.multi_day]
    today_occs = task_occurrences(user, today, today)
    agenda_today = _day_items(today_evs, today_occs, today)
    for x in agenda_today:
        if x["type"] == "event" and not x["multi"]:
            ev = x["obj"]
            x["state"] = "past" if ev.end <= at else ("now" if ev.start <= at else "next")
    upcoming = (AgendaEvent.query.filter(AgendaEvent.user_id == user.id, AgendaEvent.status == "active",
                                         AgendaEvent.cancelled.is_(False), AgendaEvent.end > at)
                .order_by(AgendaEvent.start).limit(40).all())
    upcoming = [e for e in upcoming if not e.multi_day]
    current = next((e for e in upcoming if e.start <= at), None)
    nxt = next((e for e in upcoming if e.start > at), None)
    groups = open_task_groups(user, today)
    exams = [e for e in AgendaEvent.query.filter(AgendaEvent.user_id == user.id, AgendaEvent.status == "active",
                                                 AgendaEvent.cancelled.is_(False), AgendaEvent.start >= t0,
                                                 AgendaEvent.start < t0 + timedelta(days=45))
             .order_by(AgendaEvent.start) if is_exam(e)]
    week = week_view(user, ref or today, s, at)
    this_week = week if ics.week_start(ref or today) == ics.week_start(today) else week_view(user, today, s, at)
    last = latest_import(user)
    recent = recent_import_ids(user, at)
    changes = []
    if recent:
        changes = (AgendaEvent.query.filter(AgendaEvent.user_id == user.id, AgendaEvent.changed_import_id.in_(recent),
                                            AgendaEvent.change_kind.in_(("added", "modified", "removed", "restored")),
                                            AgendaEvent.start >= t0)
                   .order_by(AgendaEvent.start).limit(12).all())
    pending = AgendaReminder.query.filter(AgendaReminder.user_id == user.id, AgendaReminder.status == "pending",
                                          AgendaReminder.remind_at > at).count()
    next_reminder = (AgendaReminder.query.filter(AgendaReminder.user_id == user.id, AgendaReminder.status == "pending",
                                                 AgendaReminder.remind_at > at)
                     .order_by(AgendaReminder.remind_at).first())
    nudge = False
    if s.weekly_import_nudge:
        if last is None:
            nudge = True
        else:
            covered_to = max((i.range_end for i in AgendaImport.query.filter_by(user_id=user.id, undone_at=None)
                              if i.range_end), default=None)
            nudge = (at - last.created_at).days >= 6 or covered_to is None or covered_to < today
    today_minutes = sum(e.minutes for e in today_evs if e.is_active and not e.multi_day)
    return {
        "s": s, "at": at, "today": today, "agenda_today": agenda_today, "tomorrow": tomorrow,
        "tomorrow_evs": tomorrow_evs, "current": current, "next": nxt, "groups": groups, "exams": exams,
        "week": week, "last_import": last, "recent": recent, "changes": changes, "pending_reminders": pending,
        "next_reminder": next_reminder, "nudge": nudge, "today_minutes": today_minutes,
        "week_minutes": this_week["total_minutes"],
        "open_count": sum(len(v) for v in groups.values()),
    }


# ---------------------------------------------------------------------------
# Données pour l'interface (fenêtres de détail)
# ---------------------------------------------------------------------------

def reminder_payload(r: AgendaReminder) -> dict:
    when = f"{day_short(r.remind_at.date())} {hm(r.remind_at)}" if r.remind_at else ""
    return {"id": r.id, "label": r.label, "status": r.status, "statusLabel": reminder_status_label(r),
            "when": when, "auto": bool(r.auto), "channel": r.channel, "offset": r.offset_minutes}


def event_payload(ev: AgendaEvent, recent: set[int], url_for) -> dict:
    badge = change_badge(ev, recent)
    tasks = AgendaTask.query.filter_by(event_id=ev.id).order_by(AgendaTask.due_date).all()
    return {
        "id": ev.id, "title": ev.title, "kind": kind_label(ev.kind), "kindCode": ev.kind, "session": ev.session_no,
        "tentative": bool(ev.tentative), "cancelled": bool(ev.cancelled), "removed": ev.status != "active",
        "when": event_when(ev), "date": ev.start.date().isoformat(), "start": hm(ev.start), "end": hm(ev.end),
        "multi": ev.multi_day, "location": ev.location, "teachers": ev.teachers, "groups": ev.groups,
        "description": ev.description, "note": ev.note or "", "color": event_color(ev), "exam": is_exam(ev),
        "badge": badge[0] if badge else "", "badgeClass": badge[1] if badge else "",
        "changes": change_lines(ev.changes) if badge and ev.change_kind in ("modified", "restored") else [],
        "reminders": [reminder_payload(r) for r in ev.reminders],
        "tasks": [{"id": t.id, "title": t.title, "done": t.is_done, "date": t.due_date.isoformat() if t.due_date else ""}
                  for t in tasks],
        "urls": {"note": url_for("agenda.event_note", eid=ev.id), "reminder": url_for("agenda.event_reminder", eid=ev.id),
                 "link": url_for("agenda.subject_link", eid=ev.id), "exam": url_for("agenda.event_exam", eid=ev.id)},
        "links": _safe_links(ev),
    }


def _safe_links(ev: AgendaEvent) -> dict:
    try:
        from flask_login import current_user as _cu
        return subject_links(_cu, ev) if _cu and _cu.is_authenticated else {}
    except Exception:  # noqa: BLE001 - la fenêtre de détail reste utilisable sans les liens
        return {}


def task_payload(t: AgendaTask, url_for) -> dict:
    return {
        "id": t.id, "title": t.title, "notes": t.notes or "", "date": t.due_date.isoformat() if t.due_date else "",
        "start": hm(t.start_time), "end": hm(t.end_time), "priority": t.priority or "none",
        "category": t.category or "", "color": t.color or "", "recurrence": t.recurrence or "",
        "until": t.recurrence_until.isoformat() if t.recurrence_until else "", "event": t.event_id or "",
        "eventLabel": f"{t.event.title} · {hm(t.event.start)}" if t.event else "",
        "done": t.is_done, "reminders": [reminder_payload(r) for r in t.reminders],
        "channel": (t.reminders[0].channel if t.reminders else ""),
        "urls": {"save": url_for("agenda.task_update", tid=t.id), "delete": url_for("agenda.task_delete", tid=t.id),
                 "toggle": url_for("agenda.task_toggle", tid=t.id)},
    }


def day_events_payload(user: User, d: date) -> list[dict]:
    a = datetime.combine(d, time(0))
    return [{"id": e.id, "label": f"{hm(e.start)}–{hm(e.end)} · {e.title}" + (f" ({kind_label(e.kind, True)})" if e.kind else ""),
             "start": hm(e.start), "end": hm(e.end), "multi": e.multi_day}
            for e in events_between(user, a, a + timedelta(days=1), include_removed=False) if not e.cancelled]


# ---------------------------------------------------------------------------
# Export iCalendar (pour l'ajouter à Google Agenda, Outlook, téléphone…)
# ---------------------------------------------------------------------------

def _ics_text(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> list[str]:
    out, cur = [], ""
    for ch in line:
        if len((cur + ch).encode("utf-8")) > 74:
            out.append(cur)
            cur = " " + ch
        else:
            cur += ch
    out.append(cur)
    return out


RRULES = {"daily": "FREQ=DAILY", "weekdays": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", "weekly": "FREQ=WEEKLY",
          "biweekly": "FREQ=WEEKLY;INTERVAL=2", "monthly": "FREQ=MONTHLY"}


def export_ics(user: User, include_events: bool = True, include_tasks: bool = True) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Hangar//Agenda//FR", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", f"X-WR-CALNAME:{_ics_text('Agenda de ' + user.display_name)}",
             f"X-WR-TIMEZONE:{ics.DEFAULT_TZ}"]
    if include_events:
        for ev in AgendaEvent.query.filter_by(user_id=user.id, status="active").order_by(AgendaEvent.start):
            if ev.cancelled:
                continue
            desc = "\n".join(x for x in (
                kind_label(ev.kind), f"Intervenants : {ev.teachers}" if ev.teachers else "",
                f"Groupes : {ev.groups}" if ev.groups else "", ev.description, ev.note) if x)
            lines += ["BEGIN:VEVENT", f"UID:{_ics_text(ev.uid)}@hangar", f"DTSTAMP:{stamp}"]
            if ev.all_day:
                lines += [f"DTSTART;VALUE=DATE:{ev.start:%Y%m%d}", f"DTEND;VALUE=DATE:{ev.end:%Y%m%d}"]
            else:
                lines += [f"DTSTART;TZID={ics.DEFAULT_TZ}:{ev.start:%Y%m%dT%H%M%S}",
                          f"DTEND;TZID={ics.DEFAULT_TZ}:{ev.end:%Y%m%dT%H%M%S}"]
            title = ev.title + (f" ({kind_label(ev.kind, True)})" if ev.kind else "")
            lines += [f"SUMMARY:{_ics_text(title)}", f"LOCATION:{_ics_text(ev.location)}",
                      f"DESCRIPTION:{_ics_text(desc)}"]
            if ev.tentative:
                lines.append("STATUS:TENTATIVE")
            for r in ev.reminders:
                if r.offset_minutes is not None:
                    lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_ics_text(title)}",
                              f"TRIGGER:-PT{r.offset_minutes}M", "END:VALARM"]
            lines.append("END:VEVENT")
    if include_tasks:
        for t in AgendaTask.query.filter(AgendaTask.user_id == user.id, AgendaTask.due_date.isnot(None),
                                         AgendaTask.done_at.is_(None)):
            lines += ["BEGIN:VEVENT", f"UID:tache-{t.id}@hangar", f"DTSTAMP:{stamp}"]
            if t.start_time:
                st = datetime.combine(t.due_date, t.start_time)
                en = task_end(t)
                lines += [f"DTSTART;TZID={ics.DEFAULT_TZ}:{st:%Y%m%dT%H%M%S}",
                          f"DTEND;TZID={ics.DEFAULT_TZ}:{en:%Y%m%dT%H%M%S}"]
            else:
                lines += [f"DTSTART;VALUE=DATE:{t.due_date:%Y%m%d}",
                          f"DTEND;VALUE=DATE:{t.due_date + timedelta(days=1):%Y%m%d}"]
            lines += [f"SUMMARY:{_ics_text('✓ ' + t.title)}", f"DESCRIPTION:{_ics_text(t.notes)}"]
            if t.category:
                lines.append(f"CATEGORIES:{_ics_text(t.category)}")
            if t.recurrence in RRULES:
                rule = RRULES[t.recurrence]
                if t.recurrence_until:
                    rule += f";UNTIL={t.recurrence_until:%Y%m%d}"
                lines.append(f"RRULE:{rule}")
            for r in t.reminders:
                if r.offset_minutes is not None:
                    lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_ics_text(t.title)}",
                              f"TRIGGER:-PT{r.offset_minutes}M", "END:VALARM"]
            lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    out = []
    for ln in lines:
        out += _fold(ln)
    return "\r\n".join(out) + "\r\n"


__all__ = ["IMPORT_MODES", "RECURRENCES", "REMINDER_OFFSETS", "import_calendar", "undo_import", "week_view",
           "month_view", "list_view", "dashboard", "run_jobs", "export_ics"]


# ---------------------------------------------------------------------------
# Matières : séance de l'emploi du temps ↔ collection du wiki ↔ catégorie de QCM
# ---------------------------------------------------------------------------

_STOP = {"de", "du", "des", "la", "le", "les", "et", "en", "sur", "aux", "au", "un", "une", "a", "l", "d", "niveau"}
_IGNORED_FOLDERS = {"qcm", "image", "images", "fichiers", "guide", "attachments", "assets"}


def subject_key(title: str) -> str:
    return normalize(title)[:300]


def _words(text: str) -> list[str]:
    out = []
    for w in normalize(re.sub(r"([a-z])([A-Z])", r"\1 \2", text or "")).split():
        if w not in _STOP and not w.isdigit():
            out.append(w[:-1] if len(w) > 4 and w.endswith("s") else w)
    return out


def _similarity(title: str, candidate: str) -> int:
    """Score 0–100 de ressemblance entre un intitulé de cours et un nom de dossier / catégorie."""
    a, b = _words(title), _words(candidate)
    if not a or not b:
        return 0
    ca, cb = "".join(a), "".join(b)
    if ca == cb:
        return 100
    score = 0
    inter = set(a) & set(b)
    if inter:
        score = max(score, int(80 * len(inter) / len(set(a) | set(b))) + 20)
    acronym = "".join(w[0] for w in a if len(w) > 2)
    for tok in b:
        if len(tok) >= 2 and len(acronym) >= 2 and (acronym.startswith(tok) or tok.startswith(acronym)):
            score = max(score, 65)
    if len(cb) >= 4 and (cb in ca or ca in cb):
        score = max(score, 55)
    return min(score, 99)


def wiki_collections() -> list[dict]:
    """Dossiers du coffre proposés comme « cours » : [{path, label, url}] (vide si le wiki est indisponible)."""
    from flask import g
    if "_wiki_collections" in g:
        return g.get("_wiki_collections")
    g._wiki_collections = _wiki_collections()
    return g._wiki_collections


def _wiki_collections() -> list[dict]:
    try:
        from flask import url_for as _url
        from .wiki.vault import get_vault
        vault = get_vault()
    except Exception:  # noqa: BLE001 - wiki absent ou coffre illisible
        return []
    out = []
    for path in sorted(vault.folders(), key=lambda p: p.lower()):
        parts = [p for p in str(path).split("/") if p]
        if not parts or len(parts) > 3 or parts[0].lower() in _IGNORED_FOLDERS or any(p.startswith(".") for p in parts):
            continue
        out.append({"path": "/".join(parts), "label": parts[-1], "group": "/".join(parts[:-1]),
                    "url": _url("wiki.sommaire", dossier="/".join(parts))})
    return out


def _wiki_target(path: str) -> dict | None:
    """Lien vers une collection : sa page « 00 - Sommaire … » si elle existe, sinon le sommaire du dossier."""
    if not path:
        return None
    try:
        from flask import url_for as _url
        from .wiki.vault import get_vault
        vault = get_vault()
    except Exception:  # noqa: BLE001
        return None
    prefix = path.strip("/").lower() + "/"
    notes = [n for n in vault.notes.values() if str(n.path).lower().startswith(prefix)
             and str(n.path).count("/") == path.count("/") + 1]
    home = next((n for n in sorted(notes, key=lambda n: str(n.path).lower()) if "sommaire" in n.name.lower()), None)
    label = path.rsplit("/", 1)[-1]
    if home is not None:
        return {"label": label, "url": home.url, "path": path}
    return {"label": label, "url": _url("wiki.sommaire", dossier=path), "path": path}


def qcm_categories(user) -> dict[str, int]:
    """{catégorie de sets : cartes dues aujourd'hui} pour un compte (vide si le module QCM est indisponible)."""
    from flask import g
    key = f"_qcm_cats_{user.id}"
    if key in g:
        return g.get(key)
    cats: dict[str, int] = {}
    try:
        from .qcm.services import bibliotheque, resume_du_jour
        for cat in bibliotheque(user.id).categories():
            cats.setdefault(cat, 0)
        for st in resume_du_jour(user).get("par_set", []):
            cats[st.get("categorie", "")] = cats.get(st.get("categorie", ""), 0) + int(st.get("cartes") or 0)
    except Exception:  # noqa: BLE001
        cats = {}
    setattr(g, key, cats)
    return cats


def subject_links(user, ev: AgendaEvent) -> dict:
    """Liens et suggestions d'une séance pour la fenêtre de détail."""
    link = AgendaSubjectLink.query.filter_by(user_id=user.id, subject=subject_key(ev.title)).first()
    out = {"subject": ev.title, "wiki": None, "qcm": None, "wikiSuggest": [], "qcmSuggest": [], "wikiAll": [],
           "qcmAll": []}
    cols = wiki_collections()
    cats = qcm_categories(user)
    if link and link.wiki_path:
        out["wiki"] = _wiki_target(link.wiki_path)
    if link and link.qcm_category:
        from flask import url_for as _url
        out["qcm"] = {"label": link.qcm_category, "due": cats.get(link.qcm_category, 0),
                      "url": _url("qcm.index") + "#cat-" + re.sub(r"[^a-z0-9]+", "-", normalize(link.qcm_category)).strip("-")}
    teachers = [w for w in _words(ev.teachers or "") if len(w) > 3]

    def score(cand: str) -> int:
        sc = _similarity(ev.title, cand)
        cw = "".join(_words(cand))
        if any(t in cw for t in teachers):  # « TELECOM_SANTORI » pour un cours de M. Santori
            sc = max(sc, 60)
        return sc

    if out["wiki"] is None and cols:
        scored = sorted(((max(score(c["label"]), score(c["path"])), c) for c in cols), key=lambda x: -x[0])
        out["wikiSuggest"] = [c["path"] for s, c in scored[:3] if s >= 45]
        out["wikiAll"] = [c["path"] for c in cols]
    if out["qcm"] is None and cats:
        scored = sorted(((score(c), c) for c in cats), key=lambda x: -x[0])
        out["qcmSuggest"] = [c for s, c in scored[:3] if s >= 45]
        out["qcmAll"] = sorted(cats, key=str.lower)
    return out


def set_subject_link(user, title: str, wiki_path: str | None = None, qcm_category: str | None = None) -> AgendaSubjectLink:
    key = subject_key(title)
    link = AgendaSubjectLink.query.filter_by(user_id=user.id, subject=key).first()
    if link is None:
        link = AgendaSubjectLink(user_id=user.id, subject=key, title=title[:300], created_at=now())
        db.session.add(link)
    if wiki_path is not None:
        link.wiki_path = wiki_path.strip("/")[:500]
    if qcm_category is not None:
        link.qcm_category = qcm_category[:300]
    link.updated_at = now()
    return link


def set_exam_objective(user, ev: AgendaEvent) -> int:
    """Date d'examen des sets de la catégorie de QCM liée = jour du contrôle. Retourne le nombre de sets."""
    link = AgendaSubjectLink.query.filter_by(user_id=user.id, subject=subject_key(ev.title)).first()
    if link is None or not link.qcm_category:
        return 0
    from .qcm.models import QuizDeck
    from .qcm.services import bibliotheque
    sets = bibliotheque(user.id).categories().get(link.qcm_category, [])
    for s in sets:
        deck = QuizDeck.query.filter_by(user_id=user.id, set_id=s.id).first()
        if deck is None:
            deck = QuizDeck(user_id=user.id, set_id=s.id)
            db.session.add(deck)
        deck.date_objectif = ev.start.date()
    db.session.commit()
    return len(sets)

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
import time as _clock
import unicodedata
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

from flask import current_app
from markupsafe import escape

from . import ics
from .extensions import db
from .models import (
    PRIORITY_ORDER, RECURRENCES, REMINDER_OFFSETS, AgendaEvent, AgendaGrade, AgendaImport, AgendaReminder,
    AgendaSettings, AgendaSubjectLink, AgendaTask, Notification, User, now,
)
from .texte import cle_tri
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
    h = int(hashlib.md5(_ancienne_cle(ev.title).encode()).hexdigest()[:8], 16)
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


def is_invitation(cal) -> bool:
    """Message d'agenda (invitation, réponse, annulation : METHOD:REQUEST, REPLY, CANCEL…) plutôt qu'un
    calendrier publié (METHOD:PUBLISH, ou rien).

    METHOD seul ne suffit pas : certains logiciels de planning exportent tout l'emploi du temps en
    METHOD:REQUEST. Un tel fichier n'est traité comme une invitation que s'il en a l'allure : au plus
    GUARD_MAX_SESSIONS séances, ou aucune séance d'un type de cours de l'école (CM, TD_TP…)."""
    if not cal.method or cal.method == "PUBLISH":
        return False
    if len(cal.events) <= GUARD_MAX_SESSIONS:
        return True
    return not any(e.kind in ics.KINDS for e in cal.events)


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
    if is_invitation(cal) and mode != "merge":
        # Invitation à une réunion (METHOD:REQUEST…) : elle ne décrit pas toute la semaine, elle ne doit rien
        # retirer (sinon les autres cours de la semaine disparaîtraient).
        mode = "merge"
        cal.warnings.append("Ce fichier est une invitation (réunion, rendez-vous…), pas un emploi du temps complet : "
                            "ses séances ont été ajoutées sans rien retirer du planning.")
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
    floor = at.date()  # le passé n'est jamais retiré
    # Garde-fou : séances actives par semaine avant l'import, comptées comme celles qu'on pourrait
    # retirer (à partir d'aujourd'hui). Compter aussi les jours passés rendrait le garde-fou inopérant
    # en fin de semaine (4 séances retirées sur 10 « connues », alors que ce sont toutes celles qui restent).
    known_by_week: dict[date, int] = {}
    for e in existing.values():
        if e.status == "active" and e.start.date() >= floor:
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
                # Semaine voisine (pas la semaine principale du fichier) qui perdrait la majorité des
                # séances qui lui restent
                if n <= GUARD_MAX_SESSIONS and n < main and 2 * len(evs) > known:
                    imp.guarded_weeks.append(w)
                    guard_lines.append(
                        f"Semaine du {day_short(w)} : le fichier n’y apporte que {n} séance(s), alors que "
                        f"{len(evs)} des {known} séances encore prévues en auraient été retirées. Par prudence, "
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
    db.session.flush()
    quota_off: list[str] = []
    imp.exam_notes = "\n".join(sync_exam_objectives(user, at, exam_changes(details), quota_off)
                               + _replanifier_revisions(user, at))
    imp.exam_quota_sets = "\n".join(quota_off)
    db.session.commit()
    if s.import_email and imp.changed:
        queue_import_email(user, imp, at)
        db.session.commit()
    return imp


def latest_import(user: User) -> AgendaImport | None:
    return (AgendaImport.query.filter_by(user_id=user.id, undone_at=None)
            .order_by(AgendaImport.id.desc()).first())


def undo_import(imp: AgendaImport, at: datetime | None = None) -> list[str]:
    """Rétablit l'état d'avant l'import (uniquement pour le dernier import), rappels compris : les rappels
    automatiques des contrôles sont recréés et les rappels ignorés pendant le retrait repartent. Les dates
    d'examen des QCM suivent aussi (retourne les phrases qui le disent)."""
    at = at or now()
    last = AgendaImport.query.filter_by(user_id=imp.user_id, undone_at=None).order_by(AgendaImport.id.desc()).first()
    if last is None or last.id != imp.id:
        raise ValueError("Seul le dernier import peut être annulé.")
    blocs_notes: list[str] = []
    for d in reversed(imp.details):
        ev = db.session.get(AgendaEvent, d.get("event_id"))
        if ev is None:
            continue
        if d["type"] == "added":
            # Blocs « Réviser … » générés pour cette séance (hangar/controles.py) : ils partent avec elle,
            # sinon ils resteraient dans l'agenda sans contrôle ; ceux déjà faits restent (historique).
            n = AgendaTask.query.filter(AgendaTask.event_id == ev.id, AgendaTask.revision_auto.is_(True),
                                        AgendaTask.done_at.is_(None)).delete(synchronize_session="fetch")
            if n:
                blocs_notes.append(f"{ev.title} : contrôle retiré du planning, {n} bloc(s) de révision supprimé(s).")
            AgendaTask.query.filter_by(event_id=ev.id).update({"event_id": None})
            AgendaGrade.query.filter_by(event_id=ev.id).update({"event_id": None})  # la note reste
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
    notes = []
    if user is not None:
        ensure_exam_reminders(user, at)
        db.session.flush()
        notes = sync_exam_objectives(user, at, exam_changes(imp.details, undo=True))
        notes += _restore_auto_quota(user, (imp.exam_quota_sets or "").splitlines())
        notes += _replanifier_revisions(user, at)
    db.session.commit()
    return blocs_notes + notes


def _replanifier_revisions(user: User, at: datetime) -> list[str]:
    """Blocs de révision générés (hangar/controles.py) replacés après un import ou son annulation."""
    try:
        from . import controles
        return controles.replanifier_apres_import(user, at)
    except Exception:  # noqa: BLE001 - l'import passe quand même
        current_app.logger.exception("Blocs de révision après un import")
        return []


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
    return sorted({r[0] for r in rows if r[0]}, key=cle_tri)


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
    channel = valid_channel(channel)
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
                       channel=valid_channel(channel), created_at=at,
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
    r = AgendaReminder(user_id=ev.user_id, offset_minutes=offset, channel=valid_channel(channel), auto=auto,
                       created_at=at)
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


def _repair_recurring_reminders(at: datetime) -> None:
    """Rappels « X min avant » d'une tâche répétée encore ouverte restés bloqués sur une occurrence passée
    (envoyés, manqués, ignorés ou en erreur : état laissé par les versions précédentes, qui ne passaient pas à
    l'occurrence suivante tant que la tâche n'était pas cochée) : ils visent de nouveau la prochaine occurrence.
    L'occurrence déjà visée n'est jamais rappelée une seconde fois ; une série terminée n'est pas touchée."""
    rows = (AgendaReminder.query.join(AgendaTask, AgendaReminder.task_id == AgendaTask.id)
            .filter(AgendaReminder.status.in_(("sent", "missed", "skipped", "error")),
                    AgendaReminder.offset_minutes.isnot(None), AgendaReminder.remind_at.isnot(None),
                    AgendaReminder.remind_at <= at, AgendaTask.recurrence.isnot(None), AgendaTask.recurrence != "",
                    AgendaTask.done_at.is_(None), AgendaTask.due_date.isnot(None),
                    db.or_(AgendaTask.recurrence_until.is_(None),
                           AgendaTask.recurrence_until >= at.date() - timedelta(days=1)))
            .all())
    for r in rows:
        t = r.task
        after = max(at - timedelta(minutes=5, microseconds=1), r.remind_at, r.sent_at or r.remind_at)
        nxt = task_reminder_time(t, r.offset_minutes, settings_of_id(t.user_id), after)
        if nxt is not None:
            r.remind_at, r.status = nxt, "pending"


def process_reminders(at: datetime | None = None) -> int:
    """Envoie les rappels arrivés à échéance. Retourne le nombre de rappels envoyés.

    Chaque rappel est validé à part : un rappel en erreur passe à l'état « error » (noté sur /erreurs/) sans
    empêcher les autres de partir ni revenir en boucle à chaque passage du fil."""
    at = at or now()
    try:
        _repair_recurring_reminders(at)
        db.session.commit()
    except Exception:  # noqa: BLE001 - la réparation ne doit pas empêcher les rappels de partir
        db.session.rollback()
        current_app.logger.exception("Réparation des rappels des tâches répétées")
        _mail().noter_erreur("Réparation des rappels des tâches répétées")
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
    """Notification dans l'application et, selon le moyen choisi, e-mail mis en file (retourné, sinon None)
    ou notification push sur les appareils abonnés."""
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
    if r.channel == "push":
        detail = when
        if r.event is not None and r.event.location:
            detail += f" · {r.event.location}"
        send_push(user, f"⏰ {title}", detail, path, tag=f"agenda-rappel-{r.id}",
                  ttl=_push_ttl(reminder_deadline(r), at))
        return None
    if r.channel == "email" and m.accepte_emails(user):  # « Aucun e-mail » : la notification dans l'appli suffit
        html = m.layout(f"⏰ {title}", f"<p>Petit rappel ({escape(r.label.lower())}) :</p>{body}",
                        button=("Ouvrir mon agenda", m.absolute(path, cfg)), cfg=cfg)
        return m.queue_email(user.contact_email, f"⏰ Rappel : {title} — {short}"[:300], html, kind="reminder",
                             to_user=user, expires_at=reminder_deadline(r))
    return None


PUSH_TTL_MAX = 24 * 3600


def _push_ttl(deadline: datetime | None, at: datetime) -> int:
    """Durée de validité d'une notification push : jusqu'à la fin de la séance ou de la tâche (au-delà, un
    téléphone resté hors ligne la recevrait pour rien), entre 1 min et 24 h."""
    if deadline is None:
        return 3600
    return int(max(60, min((deadline - at).total_seconds(), PUSH_TTL_MAX)))


def push_devices(user: User) -> list:
    """Appareils de `user` abonnés aux rappels de l'agenda (vide si le module QCM est indisponible)."""
    try:
        from .qcm.models import AbonnementPush
    except Exception:  # noqa: BLE001
        return []
    return AbonnementPush.query.filter(AbonnementPush.user_id == user.id, AbonnementPush.agenda.is_(True)).all()


def send_push(user: User, title: str, text: str, path: str, tag: str, ttl: int = 3600) -> int:
    """Envoie une notification du système à chaque appareil abonné (service des notifications web des QCM,
    qui n'accepte que les adresses des services connus). Un appareil inconnu du service est oublié ; une panne
    passagère est notée au journal sans bloquer les autres. Retourne le nombre d'appareils atteints."""
    from .qcm import push
    sent = 0
    for a in push_devices(user):
        try:
            push.envoyer(a, title[:120], text[:300], path, ttl=ttl, tag=tag)
            sent += 1
        except (push.AbonnementExpire, push.AdresseNonAutorisee):
            db.session.delete(a)
        except Exception as exc:  # noqa: BLE001 - réseau, service de notifications : la notification interne reste
            current_app.logger.warning("Notification push d’un rappel impossible : %s", exc)
    return sent


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
    # Résumé du soir (jour résumé = demain) : ce qui reste à faire aujourd'hui n'est ni « demain » ni « en retard »
    rest = []
    if ctx.jour > ctx.aujourdhui:
        rest = [o for o in task_occurrences(ctx.user, ctx.aujourdhui, ctx.aujourdhui, include_done=False)
                if o.task.id not in listed]
        rest.sort(key=lambda o: (o.task.start_time or time(23, 59), o.task.title.lower()))
    if not (occs or overdue or rest):
        return None
    html = ""
    if occs:
        occs.sort(key=lambda o: (o.task.start_time or time(23, 59), cle_tri(o.task.title)))
        html += ('<ul style="padding-left:18px;margin:0">'
                 + "".join(_task_line(o.task, ctx.aujourdhui, o.date) for o in occs) + "</ul>")
    if rest:
        html += ('<div style="font-size:12.5px;font-weight:600;color:#d97706;margin:10px 0 2px">'
                 f'Reste à faire aujourd’hui ({len(rest)})</div><ul style="padding-left:18px;margin:0">'
                 + "".join(_task_line(o.task, ctx.aujourdhui, o.date) for o in rest[:12]) + "</ul>")
    if overdue:
        html += ('<div style="font-size:12.5px;font-weight:600;color:#dc2626;margin:10px 0 2px">'
                 f'En retard ({len(overdue)})</div><ul style="padding-left:18px;margin:0">'
                 + "".join(_task_line(t, ctx.aujourdhui) for t in overdue[:12]) + "</ul>")
    n = len(occs)
    if n:
        libelle, extra = digest.pluriel(n, "tâche"), [(rest, "aujourd’hui"), (overdue, "en retard")]
    elif rest:
        libelle, extra = digest.pluriel(len(rest), "tâche") + " aujourd’hui", [(overdue, "en retard")]
    else:
        libelle, extra = digest.pluriel(len(overdue), "en retard", "en retard"), []
    extra = [f"+{len(x)} {lab}" for x, lab in extra if x]
    if extra:
        libelle += f" ({', '.join(extra)})"
    return digest.Section("agenda.taches", "Tâches", html, compte=n or len(rest) or len(overdue), libelle=libelle,
                          couleur="#dc2626" if overdue else "#8b5cf6")


@digest.section("agenda.controles", ordre=40)
def _digest_controles(ctx):
    a = datetime.combine(ctx.jour, time(0))
    exams = [e for e in events_between(ctx.user, a + timedelta(days=1), a + timedelta(days=8), include_removed=False)
             if is_exam(e) and not e.cancelled]
    if not exams:
        return None
    try:  # statut de préparation (prêt, à commencer, en retard…), calculé sans adresse web
        from . import controles
        statuts = controles.indicateurs(ctx.user, exams, ctx.at)
    except Exception:  # noqa: BLE001 - la section reste utile sans les statuts
        controles, statuts = None, {}
    items = []
    for e in exams:
        st = statuts.get(e.id)
        etat = ""
        if controles is not None and st and st not in ("passe", "a_relier"):
            couleur = {"pret": "#16a34a", "retard": "#dc2626", "a_commencer": "#d97706"}.get(st, "#3f76ff")
            etat = (f' · <a href="{escape(ctx.lien(f"/agenda/events/{e.id}/preparer"))}" style="color:{couleur};'
                    f'font-weight:600">{escape(controles.libelle(st).lower())}</a>')
        items.append(f"{escape(day_short(e.start.date()))} {hm(e.start)} — <b>{escape(e.title)}</b>"
                     f"{' · ' + escape(e.location) if e.location else ''}{etat}")
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


DAILY_LATEST_HOUR = 18  # après 18 h, plus de résumé « de la journée » : elle est presque finie


def daily_covered(s: AgendaSettings) -> date | None:
    """Dernier jour couvert par un résumé déjà envoyé. Les versions précédentes ne gardaient que le jour de
    l'envoi : en mode soir, le résumé envoyé ce jour-là couvrait le lendemain."""
    if s.last_daily_day is not None:
        return s.last_daily_day
    if s.last_daily_date is None:
        return None
    return s.last_daily_date + timedelta(days=1) if s.daily_mode == "evening" else s.last_daily_date


def daily_target(s: AgendaSettings, at: datetime) -> date | None:
    """Jour à résumer maintenant, ou None.

    - Le matin : le jour même, à partir de l'heure choisie et jusqu'à 18 h (au-delà, la journée est presque
      finie : on n'envoie plus le programme d'une journée passée).
    - La veille au soir : le lendemain, à partir de l'heure choisie. Si Hangar était fermé hier soir, le résumé
      du jour part à la première ouverture, jusqu'à 18 h (rattrapage).
    Un jour déjà couvert par un résumé n'est jamais renvoyé."""
    if s.daily_mode not in ("morning", "evening"):
        return None
    today, hour = at.date(), s.daily_hour or 0
    if s.daily_mode == "morning":
        day = today if hour <= at.hour < max(DAILY_LATEST_HOUR, hour + 1) else None
    elif at.hour >= hour:
        day = today + timedelta(days=1)
    else:
        day = today if at.hour < min(DAILY_LATEST_HOUR, hour) else None
    covered = daily_covered(s)
    if day is None or (covered is not None and covered >= day):
        return None
    return day


def set_daily_schedule(s: AgendaSettings, mode: str, hour: int, at: datetime) -> None:
    """Change le mode ou l'heure du résumé quotidien sans envoi immédiat : le jour que le résumé couvrirait
    tout de suite est noté comme déjà couvert (activé à 10 h pour 7 h : premier résumé demain). Un jour déjà
    couvert le reste (changer l'heure après avoir reçu le résumé du jour ne le renvoie pas)."""
    if mode == s.daily_mode and hour == s.daily_hour:
        return
    covered = daily_covered(s)
    sent_on, s.last_daily_date, s.last_daily_day = s.last_daily_date, None, None
    s.daily_mode, s.daily_hour = mode, hour
    target = daily_target(s, at)
    s.last_daily_date = sent_on
    s.last_daily_day = max((d for d in (covered, target) if d is not None), default=None)


def send_daily_programs(at: datetime | None = None) -> int:
    """Envoie le résumé quotidien aux comptes qui l'ont activé (une fois par jour résumé, voir `daily_target`).

    Rien pour un compte qui a choisi « Aucun e-mail » dans son profil, ni pour un compte qui n'a jamais ouvert
    Hangar (pas encore de préférences d'agenda : le résumé n'est pas activé à son insu)."""
    at = at or now()
    m = _mail()
    n = 0
    for user in User.query.filter(User.active.is_(True), User.contact_email.isnot(None)).all():
        if not m.accepte_emails(user):
            continue
        s = db.session.get(AgendaSettings, user.id)
        day = daily_target(s, at) if s is not None else None
        if day is None:
            continue
        built = build_day_email(user, day, at)
        s.last_daily_date, s.last_daily_day = at.date(), day
        if built:  # en cas de panne passagère, nouvel essai jusqu'à la fin de la journée résumée
            m.queue_email(user.contact_email, built[0], built[1], kind="agenda", to_user=user,
                          expires_at=datetime.combine(day + timedelta(days=1), time(0)))
            n += 1
    db.session.commit()
    return n


def digest_enabled(user: User) -> bool:
    """Le résumé quotidien part-il pour ce compte ? (il remplace alors le rappel quotidien des échéances)"""
    s = db.session.get(AgendaSettings, user.id)
    return s is not None and s.daily_mode in ("morning", "evening")


def queue_import_email(user: User, imp: AgendaImport, at: datetime) -> None:
    m = _mail()
    if not m.accepte_emails(user):
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
SOURCE_TIMEOUT = 20
SOURCE_SILENT_DAYS = 6                       # source muette depuis une semaine : le rappel d'import revient
SOURCE_SETTLE_SECONDS = 5                    # fichier modifié il y a moins que ça : sans doute en cours d'écriture


def calendar_fingerprint(cal) -> str:
    """Empreinte des séances d'un fichier (pas de ses octets : l'horodatage d'export change à chaque fois)."""
    rows = sorted([e.uid, {k: _norm(k, v) for k, v in e.values().items()}] for e in cal.events)
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def foreign_reason(cal, user: User) -> str | None:
    """Pourquoi un fichier trouvé tout seul n'est sans doute pas l'emploi du temps (None : il peut l'être).

    Écartés : les invitations (METHOD:REQUEST…), et, quand un planning est déjà là, un fichier qui n'a ni
    séance déjà connue ni type de cours de l'école (CM, TD_TP, CO_ECRIT…). Sinon, en mode « semaines »,
    l'invitation à une réunion tombée dans le dossier retirerait toutes les autres séances de sa semaine."""
    if is_invitation(cal):
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
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "Hangar (agenda)", "Accept": "text/calendar, */*"})
    with urllib.request.urlopen(req, timeout=SOURCE_TIMEOUT) as r:  # noqa: S310 - adresse choisie par un administrateur
        data = r.read(SOURCE_MAX_BYTES + 1)
    if len(data) > SOURCE_MAX_BYTES:
        raise ValueError("le fichier est trop gros pour un emploi du temps")
    return data


def _source_note(user: User, message: str, url: str = "/agenda/imports", at: datetime | None = None) -> None:
    db.session.add(Notification(user_id=user.id, kind="update", message=message[:500], url=url, created_at=at or now()))


def check_source(user: User, at: datetime | None = None, force: bool = False) -> str:
    """Lit la source de l'emploi du temps de `user` et importe un nouveau fichier. Retourne ce qui s'est passé :
    "off" (pas de source), "wait" (pas encore l'heure), "busy" (fichier en cours de téléchargement),
    "same" (rien de nouveau), "imported", "ignored"
    (fichier écarté par le garde-fou) ou "error" (message dans `source_error`)."""
    at = at or now()
    s = settings_of(user)
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

    filename = "planning.ics"
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
        s.source_seen, filename = seen, os.path.basename(path)
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
    if fp == s.source_fingerprint:
        db.session.commit()
        return "same"
    s.source_fingerprint, s.source_changed_at = fp, at
    reason = foreign_reason(cal, user)
    if reason:
        _source_note(user, f"Fichier {filename} ignoré : {reason}.", at=at)
        db.session.commit()
        return "ignored"
    db.session.commit()
    try:
        imp = import_calendar(user, raw, filename, "weeks", at=at)
    except ics.ICSError as exc:
        db.session.rollback()
        s = settings_of(user)
        s.source_fingerprint = ""  # nouvel essai au prochain passage
        return fail(str(exc))
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
    at = at or now()
    n = 0
    rows = AgendaSettings.query.filter(AgendaSettings.source_kind.in_(tuple(SOURCE_KINDS)),
                                       AgendaSettings.source_path != "").all()
    for user_id in [r.user_id for r in rows]:
        user = db.session.get(User, user_id)
        if user is None or not user.active:
            continue
        try:
            n += check_source(user, at) == "imported"
        except Exception:  # noqa: BLE001 - un compte en panne n'empêche pas les autres
            db.session.rollback()
            current_app.logger.exception("Lecture automatique de l’emploi du temps (compte %s)", user_id)
            _mail().noter_erreur("Lecture automatique de l’emploi du temps")
    return n


def source_status(s: AgendaSettings, at: datetime | None = None) -> dict:
    """État de la source automatique, pour la page « Emploi du temps »."""
    at = at or now()
    kind = s.source_kind if s.source_kind in SOURCE_KINDS else ""
    return {"kind": kind, "path": s.source_path or "", "label": SOURCE_KINDS.get(kind, ""),
            "checked_at": s.source_checked_at, "changed_at": s.source_changed_at, "error": s.source_error or "",
            "kinds": SOURCE_KINDS}


def run_jobs(app=None, at: datetime | None = None) -> dict:
    """Appelé par le fil d'envoi des e-mails (toutes les 30 s). Les deux travaux sont protégés séparément :
    une erreur dans les rappels n'empêche pas le résumé quotidien (et inversement). La lecture de la source
    de l'emploi du temps (`check_sources`) est une étape à part du fil, juste avant."""
    from .mailer import _ctx
    with _ctx(app):
        at = at or now()
        out = {"reminders": _job("rappels de l’agenda", process_reminders, at),
               "daily": _job("résumé quotidien de l’agenda", send_daily_programs, at)}
        _job("maîtrise des QCM le jour des contrôles", _figer_maitrise, at)
        return out


def _figer_maitrise(at: datetime) -> int:
    from . import controles
    return controles.figer_maitrise(at)


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


# ---------------------------------------------------------------------------
# Projet d'équipe dans l'agenda : échéances de mes tickets, fins de sprint
# ---------------------------------------------------------------------------

TICKET_COLOR = "#3f76ff"
SPRINT_COLOR = "#0d9488"


@dataclass
class TeamItem:
    """Élément « ticket » de l'agenda (lecture seule) : échéance d'un ticket qui m'est assigné, ou fin d'un
    sprint (cycle) d'un projet dont je suis membre."""
    kind: str            # ticket | sprint
    date: date
    title: str
    label: str           # clé du ticket (« PP-12 ») ou nom du projet
    url: str
    uid: str             # identifiant stable pour l'export .ics
    overdue: bool = False
    color: str = TICKET_COLOR
    notes: str = ""      # notes de la tâche « Bloquer un créneau » (lien vers le ticket)
    minutes: int = 0     # (comme une séance : aucune heure de cours)
    is_active: bool = False


def _url(endpoint: str, fallback: str, **kw) -> str:
    try:
        from flask import url_for
        return url_for(endpoint, **kw)
    except Exception:  # noqa: BLE001 - hors requête (fil d'arrière-plan) : adresse écrite à la main
        return fallback


def team_items(user: User, d0: date, d1: date, today: date | None = None) -> list[TeamItem]:
    """Tickets ouverts qui me sont assignés avec une échéance entre `d0` et `d1`, et fins des sprints des projets
    dont je suis membre (même requête que le résumé quotidien : `digest.tickets_assignes`). Vide si je ne vois
    aucun projet, ou si le module des projets est indisponible."""
    today = today or date.today()
    out: list[TeamItem] = []
    try:
        issues = digest.tickets_assignes(user) or []
    except Exception:  # noqa: BLE001 - l'agenda ne doit jamais tomber à cause des projets
        current_app.logger.exception("Tickets dans l’agenda")
        return []
    for i in issues:
        d = i.target_date
        if d is None or i.is_closed or not d0 <= d <= d1:
            continue
        url = _url("issues.detail", f"/projects/{i.project_id}/issues/{i.sequence_id}", pid=i.project_id,
                   seq=i.sequence_id)
        out.append(TeamItem("ticket", d, i.title, i.key, url, f"ticket-{i.id}", overdue=d < today,
                            notes=f"Ticket [{i.key}]({url}) : {i.title}"))
    try:
        from .models import Cycle, Project, ProjectMember
        rows = (Cycle.query.join(Project, Cycle.project_id == Project.id)
                .join(ProjectMember, db.and_(ProjectMember.project_id == Project.id, ProjectMember.user_id == user.id))
                .filter(Project.archived_at.is_(None), Project.cycles_enabled.isnot(False),
                        Cycle.archived_at.is_(None), Cycle.end_date.isnot(None), Cycle.end_date >= d0,
                        Cycle.end_date <= d1).all())
    except Exception:  # noqa: BLE001
        current_app.logger.exception("Sprints dans l’agenda")
        rows = []
    for c in rows:
        url = _url("cycles.detail", f"/projects/{c.project_id}/cycles/{c.id}", pid=c.project_id, cid=c.id)
        out.append(TeamItem("sprint", c.end_date, f"Fin du sprint « {c.name} »", c.project.name, url,
                            f"sprint-{c.id}", color=SPRINT_COLOR, notes=f"Sprint [{c.name}]({url}) ({c.project.name})"))
    out.sort(key=lambda x: (x.date, x.kind != "sprint", x.label))
    return out


@dataclass
class Block:
    type: str            # event | task (les tickets ne sont que dans le bandeau « journée »)
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
    for it in team_items(user, monday, sunday, at.date()):  # tickets et fins de sprint : bandeau « journée »
        allday.append({"type": "ticket", "obj": it, "d0": it.date, "d1": it.date})
    weekend = bool(s.show_weekends) or any(timed[monday + timedelta(days=i)] for i in (5, 6)) or any(
        x["type"] in ("task", "ticket") and x["d0"].weekday() >= 5 for x in allday) or any(
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


def _day_items(evs: list[AgendaEvent], occs: list[Occ], d: date, team: list[TeamItem] = ()) -> list[dict]:
    items = []
    for it in team:  # tickets à rendre ce jour-là, fins de sprint (journée entière)
        if it.date == d:
            items.append({"type": "ticket", "obj": it, "multi": False, "time": "", "sort": (0, time(0))})
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
    team = team_items(user, start, end, at.date())
    weeks, d = [], start
    while d <= end:
        week = []
        for _ in range(7):
            week.append({"date": d, "out": d.month != first.month, "today": d == at.date(), "weekend": d.weekday() >= 5,
                         "items": _day_items(evs, occs, d, team)})
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
    team = team_items(user, ref, end, at.date())
    out = []
    for i in range(days):
        d = ref + timedelta(days=i)
        items = _day_items(evs, occs, d, team)
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
    agenda_today = _day_items(today_evs, today_occs, today, team_items(user, today, today, today))
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
        auto = s.source_kind in SOURCE_KINDS and bool((s.source_path or "").strip())
        if last is None and not auto:
            nudge = True
        else:
            covered_to = max((i.range_end for i in AgendaImport.query.filter_by(user_id=user.id, undone_at=None)
                              if i.range_end), default=None)
            stale = covered_to is None or covered_to < today
            if auto:
                # Source automatique : rappel seulement si elle est muette depuis une semaine (illisible, ou plus
                # aucun nouveau fichier alors que le planning ne couvre plus aujourd'hui)
                silent = SOURCE_SILENT_DAYS
                since_change = max((x for x in (s.source_changed_at, last.created_at if last else None) if x),
                                   default=None)
                nudge = (s.source_checked_at is None or (at - s.source_checked_at).days >= silent
                         or (stale and (since_change is None or (at - since_change).days >= silent)))
            else:
                nudge = (at - last.created_at).days >= 6 or stale
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


def preload_payloads(user, events, tasks) -> dict:
    """Ce que lisent les fenêtres de détail de toutes les séances et tâches d'une page, chargé en
    quelques requêtes (et non trois par séance) : rappels des séances et des tâches, tâches liées
    aux séances, liens de matière du compte. À passer à `event_payload`."""
    from sqlalchemy.orm.attributes import set_committed_value

    events = [e for e in events if e is not None]
    tasks = [t for t in tasks if t is not None]

    def par_paquets(ids):
        ids = sorted(set(ids))
        return (ids[k:k + 500] for k in range(0, len(ids), 500))  # limite de paramètres SQLite

    def remplir(objets, colonne, ordre):
        # Collection `reminders` remplie sans requête par objet (comme un chargement groupé).
        a_charger = {o.id: o for o in objets if "reminders" not in o.__dict__}
        lus: dict[int, list] = {i: [] for i in a_charger}
        for ids in par_paquets(a_charger):
            for r in AgendaReminder.query.filter(colonne.in_(ids)).order_by(*ordre):
                lus[getattr(r, colonne.key)].append(r)
        for i, o in a_charger.items():
            set_committed_value(o, "reminders", lus[i])

    remplir(events, AgendaReminder.event_id, (AgendaReminder.id,))
    remplir(tasks, AgendaReminder.task_id, (AgendaReminder.remind_at, AgendaReminder.id))
    liees: dict[int, list] = {}
    for ids in par_paquets(e.id for e in events):
        for t in AgendaTask.query.filter(AgendaTask.event_id.in_(ids)).order_by(AgendaTask.due_date, AgendaTask.id):
            liees.setdefault(t.event_id, []).append(t)
    liens = {l.subject: l for l in AgendaSubjectLink.query.filter_by(user_id=user.id)} if events else {}
    return {"tasks": liees, "links": liens}


def event_payload(ev: AgendaEvent, recent: set[int], url_for, pre: dict | None = None) -> dict:
    """Données de la fenêtre de détail d'une séance. `pre` : `preload_payloads` de la page (sinon
    tout est lu ici, séance par séance)."""
    badge = change_badge(ev, recent)
    if pre is not None:
        tasks = pre["tasks"].get(ev.id, [])
    else:
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
        "links": _safe_links(ev, pre),
        **_exam_payload(ev, url_for),
    }


def _exam_payload(ev: AgendaEvent, url_for) -> dict:
    """Contrôle : page « Préparer ce contrôle » et, une fois passé, la note (hangar/controles.py)."""
    if not is_exam(ev):
        return {}
    out = {"prepare": url_for("agenda.event_prepare", eid=ev.id)}
    if ev.start <= now():
        g = AgendaGrade.query.filter_by(user_id=ev.user_id, event_id=ev.id).first()
        out["grade"] = {"url": url_for("agenda.event_grade", eid=ev.id),
                        "value": g.value if g is not None else None,
                        "scale": g.scale if g is not None and g.scale else 20,
                        "coef": g.coefficient if g is not None and g.coefficient else 1}
    return out


def _safe_links(ev: AgendaEvent, pre: dict | None = None) -> dict:
    try:
        from flask_login import current_user as _cu
        if not (_cu and _cu.is_authenticated):
            return {}
        if pre is None:
            return subject_links(_cu, ev)
        # Page entière : les listes complètes (wikiAll, qcmAll) sont à la racine de ses données.
        return subject_links(_cu, ev, links=pre["links"], listes=False)
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
        "revise": _revise_url(t, url_for),
    }


def _revise_url(t: AgendaTask, url_for) -> str:
    """Bloc « Réviser … » d'un contrôle dont la matière est reliée à une catégorie de QCM : adresse de la
    révision de cette catégorie (bouton « Réviser » de la fenêtre de la tâche) ; sinon vide."""
    if not t.revision_auto or t.event is None:
        return ""
    link = _subject_link(SimpleNamespace(id=t.user_id), t.event.title)
    if link is None or not link.qcm_category:
        return ""
    return url_for("qcm.play", revision=1, categorie=link.qcm_category)


def day_events_payload(user: User, d: date) -> list[dict]:
    a = datetime.combine(d, time(0))
    return [{"id": e.id, "label": f"{hm(e.start)}–{hm(e.end)} · {e.title}" + (f" ({kind_label(e.kind, True)})" if e.kind else ""),
             "start": hm(e.start), "end": hm(e.end), "multi": e.multi_day}
            for e in events_between(user, a, a + timedelta(days=1), include_removed=False) if not e.cancelled]


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
    fuseau Europe/Paris décrit (VTIMEZONE), fin de répétition en UTC pour une tâche à heure fixe, rappels
    des tâches sans heure calés sur l'heure de référence (8 h par défaut) comme dans Hangar."""
    at = at or now()
    stamp = ics.utc_stamp()
    tz = ics.DEFAULT_TZ
    s = settings_of_id(user.id)
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
    lines += ics.vtimezone(tz)
    if include_events:
        for ev in AgendaEvent.query.filter_by(user_id=user.id, status="active").order_by(AgendaEvent.start):
            if ev.cancelled:
                continue
            desc = "\n".join(x for x in (
                kind_label(ev.kind), f"Intervenants : {ev.teachers}" if ev.teachers else "",
                f"Groupes : {ev.groups}" if ev.groups else "", ev.description, ev.note) if x)
            lines += ["BEGIN:VEVENT", f"UID:{esc(ev.uid)}@hangar", f"DTSTAMP:{stamp}"]
            if ev.all_day:
                end = ev.end if ev.end.date() > ev.start.date() else ev.start + timedelta(days=1)
                lines += [f"DTSTART;VALUE=DATE:{ev.start:%Y%m%d}", f"DTEND;VALUE=DATE:{end:%Y%m%d}"]
            else:
                lines.append(f"DTSTART;TZID={tz}:{ev.start:%Y%m%dT%H%M%S}")
                if ev.end > ev.start:  # sans DTEND : séance d'un instant (une fin égale au début est interdite)
                    lines.append(f"DTEND;TZID={tz}:{ev.end:%Y%m%dT%H%M%S}")
            title = ev.title + (f" ({kind_label(ev.kind, True)})" if ev.kind else "")
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
        for t in AgendaTask.query.filter(AgendaTask.user_id == user.id, AgendaTask.due_date.isnot(None),
                                         AgendaTask.done_at.is_(None)):
            lines += ["BEGIN:VEVENT", f"UID:tache-{t.id}@hangar", f"DTSTAMP:{stamp}"]
            if t.start_time:
                st = datetime.combine(t.due_date, t.start_time)
                en = task_end(t)
                lines.append(f"DTSTART;TZID={tz}:{st:%Y%m%dT%H%M%S}")
                if en and en > st:
                    lines.append(f"DTEND;TZID={tz}:{en:%Y%m%dT%H%M%S}")
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
        for it in team_items(user, at.date() - timedelta(days=60), at.date() + timedelta(days=400), at.date()):
            title = f"{it.label} · {it.title}" if it.kind == "ticket" else f"{it.title} ({it.label})"
            lines += ["BEGIN:VEVENT", f"UID:{it.uid}@hangar", f"DTSTAMP:{stamp}",
                      f"DTSTART;VALUE=DATE:{it.date:%Y%m%d}", f"DTEND;VALUE=DATE:{it.date + timedelta(days=1):%Y%m%d}",
                      f"SUMMARY:{esc(title)}", f"DESCRIPTION:{esc(_mail().absolute(it.url))}",
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
    s = settings_of(user)
    s.feed_token = secrets.token_urlsafe(32)
    db.session.commit()
    return s.feed_token


def disable_feed(user: User) -> None:
    s = settings_of(user)
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


__all__ = ["IMPORT_MODES", "RECURRENCES", "REMINDER_OFFSETS", "import_calendar", "undo_import", "week_view",
           "month_view", "list_view", "dashboard", "run_jobs", "export_ics"]


# ---------------------------------------------------------------------------
# Matières : séance de l'emploi du temps ↔ collection du wiki ↔ catégorie de QCM
# ---------------------------------------------------------------------------

_STOP = {"de", "du", "des", "la", "le", "les", "et", "en", "sur", "aux", "au", "un", "une", "a", "l", "d", "niveau"}
_IGNORED_FOLDERS = {"qcm", "image", "images", "fichiers", "guide", "attachments", "assets"}


def subject_key(title: str) -> str:
    return normalize(title)[:300]


def _ancienne_cle(title: str) -> str:
    """Titre normalisé comme avant que ``normalize`` écrive « œ » en « oe » et lise « ’ » comme
    « ' » (« Manœuvre » donnait « manuvre », « Droit de l’homme » « droit de lhomme »). Garde les
    liens de matière enregistrés avant et la couleur des séances (calculée sur ce titre)."""
    text = unicodedata.normalize("NFKD", str(title or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _subject_link(user, title: str) -> AgendaSubjectLink | None:
    """Lien de matière d'un titre de séance, y compris s'il a été enregistré avec l'ancienne clé."""
    key, old = subject_key(title), _ancienne_cle(title)[:300]
    links = AgendaSubjectLink.query.filter(AgendaSubjectLink.user_id == user.id,
                                           AgendaSubjectLink.subject.in_({key, old})).all()
    return next((l for l in links if l.subject == key), links[0] if links else None)


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
    for path in sorted(vault.folders(), key=cle_tri):
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


def _par_requete(cle, calcul):
    """Valeur calculée une fois par requête (rangée sur `request` : g peut survivre à plusieurs
    requêtes) ; recalculée à chaque appel hors requête."""
    from flask import has_request_context, request
    if not has_request_context():
        return calcul()
    memo = getattr(request, "_agenda_memo", None)
    if memo is None:
        memo = request._agenda_memo = {}
    if cle not in memo:
        memo[cle] = calcul()
    return memo[cle]


def qcm_categories(user) -> list[str]:
    """Catégories de sets de QCM d'un compte, dans l'ordre de la bibliothèque (vide si le module QCM
    est indisponible). Les noms seulement : les cartes dues sont dans `qcm_due`."""
    def calcul():
        try:
            from .qcm.services import bibliotheque
            return list(bibliotheque(user.id).categories())
        except Exception:  # noqa: BLE001
            return []
    return _par_requete(("qcm_categories", user.id), calcul)


def qcm_due(user) -> dict[str, int]:
    """{catégorie : cartes à réviser aujourd'hui}, comme le résumé des QCM du tableau de bord (dont il
    partage le calcul), sans l'estimation des examens. Calculé seulement quand une séance affichée
    est reliée à une catégorie."""
    def calcul():
        try:
            from .qcm.services import cartes_par_categorie, etat_du_jour_page
            return cartes_par_categorie(etat_du_jour_page(user.id))
        except Exception:  # noqa: BLE001
            return {}
    return _par_requete(("qcm_due", user.id), calcul)


def subject_lists(user) -> dict:
    """Toutes les collections du wiki et catégories de QCM proposées dans les fenêtres de détail :
    une fois pour toute la page (et non recopiées dans chaque séance)."""
    return {"wikiAll": [c["path"] for c in wiki_collections()], "qcmAll": sorted(qcm_categories(user), key=str.lower)}


def _suggestions(user, ev: AgendaEvent, kind: str, cols, cats) -> list[str]:
    """Trois meilleures collections du wiki (kind « wiki ») ou catégories de QCM (« qcm ») pour une
    séance : calculées une fois par matière et par page (le même cours revient chaque semaine)."""
    def calcul():
        teachers = [w for w in _words(ev.teachers or "") if len(w) > 3]

        def score(cand: str) -> int:
            sc = _similarity(ev.title, cand)
            cw = "".join(_words(cand))
            if any(t in cw for t in teachers):  # « TELECOM_SANTORI » pour un cours de M. Santori
                sc = max(sc, 60)
            return sc

        if kind == "wiki":
            scored = sorted(((max(score(c["label"]), score(c["path"])), c) for c in cols), key=lambda x: -x[0])
            return [c["path"] for s, c in scored[:3] if s >= 45]
        scored = sorted(((score(c), c) for c in cats), key=lambda x: -x[0])
        return [c for s, c in scored[:3] if s >= 45]
    return _par_requete(("suggestions", kind, user.id, ev.title, ev.teachers or ""), calcul)


def subject_links(user, ev: AgendaEvent, links: dict | None = None, listes: bool = True) -> dict:
    """Liens et suggestions d'une séance pour la fenêtre de détail.

    `links` : liens de matière du compte déjà chargés ({clé : lien}, voir `preload_payloads`) ;
    `listes` : joindre toutes les collections et catégories (wikiAll, qcmAll) ; une page entière les
    met une seule fois à la racine de ses données (`subject_lists`)."""
    key = subject_key(ev.title)
    if links is not None:
        link = links.get(key) or links.get(_ancienne_cle(ev.title)[:300])
    else:
        link = _subject_link(user, ev.title)
    out = {"subject": ev.title, "wiki": None, "qcm": None, "wikiSuggest": [], "qcmSuggest": []}
    if listes:
        out.update(wikiAll=[], qcmAll=[])
    cols = wiki_collections()
    cats = qcm_categories(user)
    if link and link.wiki_path:
        out["wiki"] = _wiki_target(link.wiki_path)
    if link and link.qcm_category:
        from flask import url_for as _url
        from .qcm.views import _qcm_slug  # même ancre que la page QCM (id="cat-{{ cat.nom|qcm_slug }}")
        out["qcm"] = {"label": link.qcm_category, "due": qcm_due(user).get(link.qcm_category, 0),
                      "url": _url("qcm.index") + "#cat-" + _qcm_slug(link.qcm_category)}
    if out["wiki"] is None and cols:
        out["wikiSuggest"] = _suggestions(user, ev, "wiki", cols, cats)
        if listes:
            out["wikiAll"] = [c["path"] for c in cols]
    if out["qcm"] is None and cats:
        out["qcmSuggest"] = _suggestions(user, ev, "qcm", cols, cats)
        if listes:
            out["qcmAll"] = sorted(cats, key=cle_tri)
    return out


def set_subject_link(user, title: str, wiki_path: str | None = None, qcm_category: str | None = None) -> AgendaSubjectLink:
    key = subject_key(title)
    link = _subject_link(user, title)
    if link is not None and link.subject != key:
        link.subject = key  # enregistré avec l'ancienne clé : mis à jour en passant
    if link is None:
        link = AgendaSubjectLink(user_id=user.id, subject=key, title=title[:300], created_at=now())
        db.session.add(link)
    if wiki_path is not None:
        link.wiki_path = wiki_path.strip("/")[:500]
    if qcm_category is not None:
        link.qcm_category = qcm_category[:300]
    link.updated_at = now()
    return link


def _category_decks(user, category: str, create: bool = True) -> list:
    """Réglages de révision (QuizDeck) des sets d'une catégorie de QCM, créés au besoin. Sans valider la base."""
    from .qcm.models import QuizDeck
    from .qcm.services import bibliotheque
    sets = bibliotheque(user.id).categories().get(category, [])
    have = {d.set_id: d for d in QuizDeck.query.filter(QuizDeck.user_id == user.id,
                                                        QuizDeck.set_id.in_([st.id for st in sets] or [""]))}
    out = []
    for st in sets:
        deck = have.get(st.id)
        if deck is None and create:
            deck = QuizDeck(user_id=user.id, set_id=st.id)
            db.session.add(deck)
        if deck is not None:
            out.append(deck)
    return out


def set_exam_objective(user, ev: AgendaEvent) -> int:
    """Date d'examen des sets de la catégorie de QCM liée = jour du contrôle. Retourne le nombre de sets."""
    link = _subject_link(user, ev.title)
    if link is None or not link.qcm_category:
        return 0
    decks = _category_decks(user, link.qcm_category)
    for deck in decks:
        deck.date_objectif = ev.start.date()
    db.session.commit()
    return len(decks)


def _exam_snap(snap: dict | None) -> tuple[str, date] | None:
    """(clé de matière, jour) d'un instantané de séance s'il s'agit d'un contrôle actif, sinon None."""
    if not snap or not snap.get("start") or snap.get("cancelled") or snap.get("status", "active") != "active":
        return None
    if not ics.is_exam(snap.get("kind") or "", snap.get("title") or ""):
        return None
    return subject_key(snap.get("title") or ""), datetime.fromisoformat(snap["start"]).date()


def exam_changes(details: list, undo: bool = False) -> dict[str, dict]:
    """Contrôles touchés par un import : {matière: {"old": jours quittés, "moves": {ancien jour: nouveau jour},
    "touched": un contrôle ajouté ou modifié}}. `undo` : pour l'annulation, l'état « après » de l'import est
    celui qu'on quitte et l'état « avant » celui qu'on retrouve."""
    out: dict[str, dict] = {}
    for d in details:
        before, after = d.get("before"), d.get("after")
        if undo:
            before, after = after, (d.get("before") if d.get("type") != "added" else None)
        old, new = _exam_snap(before), _exam_snap(after)
        for ex in (old, new):
            if ex is not None:
                out.setdefault(ex[0], {"old": set(), "moves": {}, "touched": False})
        if old is not None:
            out[old[0]]["old"].add(old[1])
        if new is not None:
            out[new[0]]["touched"] = True
            if old is not None and old[0] == new[0] and old[1] != new[1]:
                out[old[0]]["moves"][old[1]] = new[1]
    return out


def _restore_auto_quota(user, set_ids: list[str]) -> list[str]:
    """Annulation d'un import : remet l'ajustement automatique des sets que cet import avait coupé (quand ils
    ont retrouvé une date d'examen)."""
    if not set_ids:
        return []
    from .qcm.models import QuizDeck
    n = 0
    for deck in QuizDeck.query.filter(QuizDeck.user_id == user.id, QuizDeck.set_id.in_(set_ids)):
        if deck.date_objectif is not None and not deck.auto_quota:
            deck.auto_quota = True
            n += 1
    return [f"Ajustement automatique (« Ajuster auto. ») réactivé pour {n} set(s) de QCM."] if n else []


def sync_exam_objectives(user, at: datetime, changes: dict[str, dict],
                         quota_off: list[str] | None = None) -> list[str]:
    """Fait suivre le planning à la date d'examen des sets de QCM des matières reliées (sans valider la base).

    Pour chaque matière touchée (voir `exam_changes`) et reliée à une catégorie de QCM, on vise le prochain
    contrôle à venir :
    - une date d'examen qui valait l'ancien jour d'un contrôle déplacé suit le contrôle ;
    - un contrôle retiré : la date passe au prochain contrôle ; s'il n'y en a plus, elle est effacée et
      l'ajustement automatique coupé (les sets concernés sont ajoutés à `quota_off`, pour l'annulation) ;
    - un set sans date (ou avec une date passée) est préparé pour le prochain contrôle ;
    - une date saisie à la main (qui ne vaut aucun ancien jour de contrôle) n'est jamais modifiée.
    Retourne les phrases à afficher dans le compte rendu de l'import."""
    if not changes:
        return []
    links = [lk for lk in AgendaSubjectLink.query.filter_by(user_id=user.id)
             if lk.qcm_category and lk.subject in changes]
    if not links:
        return []
    today = at.date()
    upcoming: dict[str, list[date]] = {}
    for e in AgendaEvent.query.filter(AgendaEvent.user_id == user.id, AgendaEvent.status == "active",
                                      AgendaEvent.cancelled.is_(False),
                                      AgendaEvent.start >= datetime.combine(today, time(0))):
        if is_exam(e):
            upcoming.setdefault(subject_key(e.title), []).append(e.start.date())
    notes = []
    for link in links:
        row = changes[link.subject]
        dates = sorted(set(upcoming.get(link.subject, [])))
        nxt = dates[0] if dates else None
        old = row["old"] - set(dates)  # jours qui ne portent plus aucun contrôle de la matière
        touched = row["touched"]
        if not old and not (touched and nxt):
            continue
        try:
            decks = _category_decks(user, link.qcm_category, create=nxt is not None and touched)
        except Exception:  # noqa: BLE001 - module QCM indisponible : l'import passe quand même
            current_app.logger.exception("Dates d'examen des QCM après un import")
            continue
        moved, carried, prepared, cleared = {}, {}, 0, 0
        for deck in decks:
            d = deck.date_objectif
            if d in old:  # la date suivait un contrôle qui a bougé ou disparu
                target = row["moves"].get(d)
                bucket = moved if target in dates else carried  # contrôle déplacé, ou retiré
                target = target if target in dates else nxt
                if target is None:
                    if deck.auto_quota and quota_off is not None:
                        quota_off.append(deck.set_id)
                    deck.date_objectif, deck.auto_quota = None, False
                    cleared += 1
                else:
                    deck.date_objectif = target
                    bucket[(d, target)] = bucket.get((d, target), 0) + 1
            elif touched and nxt is not None and (d is None or d < today):
                deck.date_objectif = nxt
                prepared += 1
        name = link.title or link.subject
        for (d, target), n in sorted(moved.items()):
            notes.append(f"{name} : contrôle déplacé du {d:%d/%m} au {target:%d/%m}, date d’examen recalée pour "
                         f"{n} set(s) de QCM.")
        for (d, target), n in sorted(carried.items()):
            notes.append(f"{name} : contrôle du {d:%d/%m} retiré : date d’examen reportée au prochain contrôle "
                         f"({target:%d/%m}) pour {n} set(s) de QCM.")
        if prepared:
            notes.append(f"{name} : date d’examen fixée au {nxt:%d/%m} (prochain contrôle) pour {prepared} set(s) "
                         "de QCM.")
        if cleared:
            notes.append(f"{name} : plus de contrôle prévu, date d’examen effacée pour {cleared} set(s) de QCM.")
    return notes

"""Agenda : rappels des tâches et des séances (heure d'envoi, rappels des contrôles, envoi au bon moment)."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from flask import current_app

from .. import agenda as A  # le paquet : voir hangar/agenda/__init__.py
from ..extensions import db
from ..models import AgendaEvent, AgendaReminder, AgendaSettings, AgendaTask, User


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
    for d in A.occurrence_dates(task):
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
    at = at or A.now()
    s = settings_of_id(task.user_id)
    anchor = task_anchor(task, s)
    for r in task.reminders:
        if task.recurrence and anchor is not None and r.offset_minutes is not None:
            _resync_recurring(r, task, s, at, reset)
        else:
            _resync(r, anchor, at, reset=reset)


def sync_event_reminders(ev: AgendaEvent, at: datetime | None = None) -> None:
    at = at or A.now()
    for r in ev.reminders:
        _resync(r, ev.start, at)


def set_task_reminders(task: AgendaTask, offsets: list[int], channel: str, at: datetime | None = None) -> None:
    """Remplace les rappels « relatifs » d'une tâche par ceux demandés (les rappels à date fixe sont gardés)."""
    at = at or A.now()
    channel = A.valid_channel(channel)
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
    at = at or A.now()
    r = AgendaReminder(user_id=user_id, offset_minutes=None, remind_at=when,
                       channel=A.valid_channel(channel), created_at=at,
                       status="pending" if when >= at - timedelta(minutes=5) else "skipped")
    if task is not None:
        task.reminders.append(r)
    if event is not None:
        event.reminders.append(r)
    db.session.add(r)
    return r


def add_event_reminder(ev: AgendaEvent, offset: int, channel: str = "email", auto: bool = False,
                       at: datetime | None = None) -> AgendaReminder:
    at = at or A.now()
    r = AgendaReminder(user_id=ev.user_id, offset_minutes=offset, channel=A.valid_channel(channel), auto=auto,
                       created_at=at)
    ev.reminders.append(r)
    db.session.add(r)
    _resync(r, ev.start, at, reset=True)
    return r


def ensure_exam_reminders(user: User, at: datetime | None = None) -> None:
    """Crée / ajuste / retire les rappels automatiques des contrôles selon les préférences."""
    at = at or A.now()
    s = A.settings_of(user)
    offset = s.exam_reminder or 0
    evs = AgendaEvent.query.filter(AgendaEvent.user_id == user.id, AgendaEvent.start > at).all()
    for ev in evs:
        autos = [r for r in ev.reminders if r.auto]
        wanted = offset > 0 and ev.status == "active" and not ev.cancelled and A.is_exam(ev)
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
        return A.task_end(r.task, _reminder_occurrence(r))
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
    r.email = A.send_reminder(r, user, at)
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
    at = at or A.now()
    try:
        _repair_recurring_reminders(at)
        db.session.commit()
    except Exception:  # noqa: BLE001 - la réparation ne doit pas empêcher les rappels de partir
        db.session.rollback()
        current_app.logger.exception("Réparation des rappels des tâches répétées")
        A._mail().noter_erreur("Réparation des rappels des tâches répétées")
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
            A._mail().noter_erreur(f"Rappel de l’agenda n° {rid}")
            r = db.session.get(AgendaReminder, rid)
            if r is not None:
                r.status = "error"
                db.session.commit()
    db.session.commit()
    return sent



"""Agenda : tâches (y compris répétées), occurrences, tâche cochée ou décochée."""
from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .. import agenda as A  # le paquet : voir hangar/agenda/__init__.py
from ..extensions import db
from ..models import PRIORITY_ORDER, AgendaTask, User
from ..texte import cle_tri


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
    at = at or A.now()
    if task.done_at:
        task.done_at = None
        task.updated_at = at
        A.resync_task(task, at)
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
            A.resync_task(task, at, reset=True)
            return n
    task.done_at = at
    task.updated_at = at
    return None


def undo_complete(task: AgendaTask, previous: date | None, at: datetime | None = None) -> bool:
    """Annule la coche d'une tâche répétée (bouton « Annuler » après « Prochaine fois : … ») : l'échéance revient
    à `previous`. Retourne False si ce n'est pas possible (tâche non répétée, date incohérente)."""
    at = at or A.now()
    if (not task.recurrence or task.done_at or previous is None or task.due_date is None
            or not task.due_date - timedelta(days=400) <= previous < task.due_date):
        return False
    task.due_date = previous
    task.done_count = max((task.done_count or 0) - 1, 0)
    task.updated_at = at
    A.resync_task(task, at, reset=True)
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


def bloc_manque(t: AgendaTask, today: date) -> bool:
    """Bloc « Réviser … » placé par « Planifier mes révisions » (hangar/controles.py), passé sans être coché,
    **et dont le contrôle est passé** (ou retiré, annulé, supprimé) : il est **manqué**, pas en retard. Il reste
    dans l'agenda à son jour (historique), mais ne compte ni dans « En retard », ni dans la pastille, ni dans le
    résumé quotidien, ni sur la page Tâches, ni dans la recherche « @ » : il n'a plus d'objet.

    Avant le contrôle, un bloc sauté reste **en retard** : son travail n'est replacé nulle part (les blocs sont
    fixés par « Planifier mes révisions », la replanification après un import ne déplace que les blocs en
    conflit), il doit donc rester rappelé."""
    if not t.revision_auto or t.done_at is not None or t.due_date is None or t.due_date >= today:
        return False
    ev = t.event
    return ev is None or not ev.is_active or ev.start.date() < today


def compte_taches_dues(user: User, today: date) -> tuple[int, int]:
    """(tâches pour aujourd'hui ou en retard, dont en retard) : pastille des tâches. Blocs manqués exclus."""
    due = [t for t in AgendaTask.query.filter(AgendaTask.user_id == user.id, AgendaTask.done_at.is_(None),
                                               AgendaTask.due_date <= today) if not bloc_manque(t, today)]
    return len(due), sum(1 for t in due if t.due_date < today)


def open_task_groups(user: User, today: date) -> dict[str, list[AgendaTask]]:
    tasks = AgendaTask.query.filter(AgendaTask.user_id == user.id, AgendaTask.done_at.is_(None)).all()
    groups = {"overdue": [], "today": [], "tomorrow": [], "week": [], "later": [], "nodate": []}
    for t in tasks:
        if bloc_manque(t, today):
            continue  # bloc de révision manqué : ni à faire ni en retard (voir bloc_manque)
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



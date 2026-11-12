"""Agenda : semaine, mois, liste, tableau de bord, tickets et sprints de l'équipe, fenêtres de détail."""
from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

from flask import current_app

from .. import agenda as A  # le paquet : voir hangar/agenda/__init__.py
from .. import digest, ics
from ..extensions import db
from ..models import (
    AgendaEvent, AgendaGrade, AgendaImport, AgendaReminder, AgendaSettings, AgendaSubjectLink, AgendaTask, User,
)
from ..utils import MONTHS, MONTHS_FULL
from .taches import Occ


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
        from ..models import Cycle, Project, ProjectMember
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
    at = at or A.now()
    monday = ics.week_start(ref)
    sunday = monday + timedelta(days=6)
    a = datetime.combine(monday, time(0))
    evs = events_between(user, a, a + timedelta(days=7), include_removed=bool(s.show_removed))
    occs = A.task_occurrences(user, monday, sunday)
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
                          "time": "" if ev.multi_day else A.hm(ev.start), "sort": (0 if ev.multi_day else 1, ev.start.time())})
    for o in occs:
        if o.date == d:
            t = o.task
            items.append({"type": "task", "obj": o, "multi": False, "time": A.hm(t.start_time),
                          "sort": (1 if t.start_time else 2, t.start_time or time(23, 59))})
    items.sort(key=lambda x: x["sort"])
    return items


def month_view(user: User, ref: date, s: AgendaSettings, at: datetime | None = None) -> dict:
    at = at or A.now()
    first = ref.replace(day=1)
    last = first.replace(day=monthrange(first.year, first.month)[1])
    start = ics.week_start(first)
    end = ics.week_start(last) + timedelta(days=6)
    a = datetime.combine(start, time(0))
    evs = events_between(user, a, datetime.combine(end + timedelta(days=1), time(0)), include_removed=bool(s.show_removed))
    occs = A.task_occurrences(user, start, end)
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
    at = at or A.now()
    a = datetime.combine(ref, time(0))
    end = ref + timedelta(days=days - 1)
    evs = events_between(user, a, datetime.combine(end + timedelta(days=1), time(0)), include_removed=bool(s.show_removed))
    occs = A.task_occurrences(user, ref, end)
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
    at = at or A.now()
    today = at.date()
    s = A.settings_of(user)
    t0 = datetime.combine(today, time(0))
    today_evs = events_between(user, t0, t0 + timedelta(days=1), include_removed=bool(s.show_removed))
    tomorrow = today + timedelta(days=1)
    tomorrow_evs = [e for e in events_between(user, t0 + timedelta(days=1), t0 + timedelta(days=2), include_removed=False)
                    if not e.cancelled and not e.multi_day]
    today_occs = A.task_occurrences(user, today, today)
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
    groups = A.open_task_groups(user, today)
    exams = [e for e in AgendaEvent.query.filter(AgendaEvent.user_id == user.id, AgendaEvent.status == "active",
                                                 AgendaEvent.cancelled.is_(False), AgendaEvent.start >= t0,
                                                 AgendaEvent.start < t0 + timedelta(days=45))
             .order_by(AgendaEvent.start) if A.is_exam(e)]
    week = week_view(user, ref or today, s, at)
    this_week = week if ics.week_start(ref or today) == ics.week_start(today) else week_view(user, today, s, at)
    last = A.latest_import(user)
    recent = A.recent_import_ids(user, at)
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
        auto = s.source_kind in A.SOURCE_KINDS and bool((s.source_path or "").strip())
        if last is None and not auto:
            nudge = True
        else:
            covered_to = max((i.range_end for i in AgendaImport.query.filter_by(user_id=user.id, undone_at=None)
                              if i.range_end), default=None)
            stale = covered_to is None or covered_to < today
            if auto:
                # Source automatique : rappel seulement si elle est muette depuis une semaine (illisible, ou plus
                # aucun nouveau fichier alors que le planning ne couvre plus aujourd'hui)
                silent = A.SOURCE_SILENT_DAYS
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
    when = f"{A.day_short(r.remind_at.date())} {A.hm(r.remind_at)}" if r.remind_at else ""
    return {"id": r.id, "label": r.label, "status": r.status, "statusLabel": A.reminder_status_label(r),
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
    badge = A.change_badge(ev, recent)
    if pre is not None:
        tasks = pre["tasks"].get(ev.id, [])
    else:
        tasks = AgendaTask.query.filter_by(event_id=ev.id).order_by(AgendaTask.due_date).all()
    return {
        "id": ev.id, "title": ev.title, "kind": A.kind_label(ev.kind), "kindCode": ev.kind, "session": ev.session_no,
        "tentative": bool(ev.tentative), "cancelled": bool(ev.cancelled), "removed": ev.status != "active",
        "when": A.event_when(ev), "date": ev.start.date().isoformat(), "start": A.hm(ev.start), "end": A.hm(ev.end),
        "multi": ev.multi_day, "location": ev.location, "teachers": ev.teachers, "groups": ev.groups,
        "description": ev.description, "note": ev.note or "", "color": A.event_color(ev), "exam": A.is_exam(ev),
        "badge": badge[0] if badge else "", "badgeClass": badge[1] if badge else "",
        "changes": A.change_lines(ev.changes) if badge and ev.change_kind in ("modified", "restored") else [],
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
    if not A.is_exam(ev):
        return {}
    out = {"prepare": url_for("agenda.event_prepare", eid=ev.id)}
    if ev.start <= A.now():
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
            return A.subject_links(_cu, ev)
        # Page entière : les listes complètes (wikiAll, qcmAll) sont à la racine de ses données.
        return A.subject_links(_cu, ev, links=pre["links"], listes=False)
    except Exception:  # noqa: BLE001 - la fenêtre de détail reste utilisable sans les liens
        return {}


def task_payload(t: AgendaTask, url_for) -> dict:
    return {
        "id": t.id, "title": t.title, "notes": t.notes or "", "date": t.due_date.isoformat() if t.due_date else "",
        "start": A.hm(t.start_time), "end": A.hm(t.end_time), "priority": t.priority or "none",
        "category": t.category or "", "color": t.color or "", "recurrence": t.recurrence or "",
        "until": t.recurrence_until.isoformat() if t.recurrence_until else "", "event": t.event_id or "",
        "eventLabel": f"{t.event.title} · {A.hm(t.event.start)}" if t.event else "",
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
    link = A._subject_link(SimpleNamespace(id=t.user_id), t.event.title)
    if link is None or not link.qcm_category:
        return ""
    return url_for("qcm.play", revision=1, categorie=link.qcm_category)


def day_events_payload(user: User, d: date) -> list[dict]:
    a = datetime.combine(d, time(0))
    return [{"id": e.id, "label": f"{A.hm(e.start)}–{A.hm(e.end)} · {e.title}" + (f" ({A.kind_label(e.kind, True)})" if e.kind else ""),
             "start": A.hm(e.start), "end": A.hm(e.end), "multi": e.multi_day}
            for e in events_between(user, a, a + timedelta(days=1), include_removed=False) if not e.cancelled]



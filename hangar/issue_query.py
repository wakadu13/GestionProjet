"""Filtrage, tri, regroupement et mises en page (liste, tableau, calendrier, Gantt) des tickets."""
from __future__ import annotations

import calendar
import re
from datetime import date, timedelta
from urllib.parse import urlencode

from flask import session
from sqlalchemy.orm import selectinload

from . import heure
from .extensions import db
from .models import (
    MODULE_STATUS_COLORS, PRIORITIES, PRIORITY_ORDER, STATE_GROUPS, Cycle, Issue,
    Label, Module, Project, State, User, issue_subscribers,
)
from .utils import cle_tri, contient, week_bounds

LAYOUTS = [
    ("list", "Liste", "list"),
    ("board", "Tableau", "kanban"),
    ("spreadsheet", "Tableur", "table"),
    ("calendar", "Calendrier", "calendar"),
    ("gantt", "Gantt", "gantt"),
]
GROUP_OPTIONS = [
    ("state", "État"), ("priority", "Priorité"), ("assignee", "Assigné"), ("label", "Étiquette"),
    ("cycle", "Cycle"), ("module", "Module"), ("created_by", "Créé par"), ("project", "Projet"),
    ("none", "Aucun regroupement"),
]
ORDER_OPTIONS = [
    ("manual", "Ordre manuel"), ("-created", "Plus récents"), ("created", "Plus anciens"),
    ("-updated", "Dernière mise à jour"), ("priority", "Priorité"), ("target", "Échéance"),
    ("start", "Date de début"), ("title", "Titre"), ("key", "Identifiant"),
]
DATE_PRESETS = [
    ("overdue", "En retard"), ("today", "Aujourd’hui"), ("this_week", "Cette semaine"),
    ("next_week", "Semaine prochaine"), ("this_month", "Ce mois-ci"), ("none", "Sans date"),
]
DND_GROUPS = {"state", "state_group", "priority", "assignee", "label", "cycle", "module"}
FILTER_KEYS = ["state", "state_group", "priority", "assignee", "label", "cycle", "module", "created_by",
               "project", "start", "target", "subscribed"]
DISPLAY_KEYS = ["layout", "group_by", "order_by", "sub_issues", "show_closed", "month", "zoom"]


def base_query():
    return (Issue.query
            .filter(Issue.archived_at.is_(None), Issue.is_visible_clause())
            .options(selectinload(Issue.assignees), selectinload(Issue.labels), selectinload(Issue.modules),
                     selectinload(Issue.state), selectinload(Issue.project), selectinload(Issue.cycle),
                     selectinload(Issue.children)))


def parse_filters(args) -> dict:
    f: dict = {}
    for k in FILTER_KEYS:
        vals = []
        for raw in args.getlist(k):
            vals.extend(v for v in raw.split(",") if v)
        if vals:
            f[k] = vals
    q = (args.get("q") or "").strip()
    if q:
        f["q"] = q
    return f


def _ids(vals, me):
    out, none = [], False
    for v in vals:
        if v == "none":
            none = True
        elif v == "me":
            out.append(me.id)
        else:
            try:
                out.append(int(v))
            except ValueError:
                pass
    return out, none


def _date_clause(column, presets):
    today = heure.today()
    ws, we = week_bounds(today)
    clauses = []
    for p in presets:
        if p == "overdue":
            clauses.append(db.and_(column < today, column.isnot(None)))
        elif p == "today":
            clauses.append(column == today)
        elif p == "this_week":
            clauses.append(column.between(ws, we))
        elif p == "next_week":
            clauses.append(column.between(ws + timedelta(days=7), we + timedelta(days=7)))
        elif p == "this_month":
            first = today.replace(day=1)
            last = today.replace(day=calendar.monthrange(today.year, today.month)[1])
            clauses.append(column.between(first, last))
        elif p == "none":
            clauses.append(column.is_(None))
    return db.or_(*clauses) if clauses else None


def apply_filters(q, f: dict, me: User):
    if "state" in f:
        ids, _ = _ids(f["state"], me)
        q = q.filter(Issue.state_id.in_(ids))
    if "state_group" in f:
        q = q.filter(Issue.state_id.in_(db.select(State.id).where(State.group.in_(f["state_group"]))))
    if "priority" in f:
        q = q.filter(Issue.priority.in_(f["priority"]))
    if "assignee" in f:
        ids, none = _ids(f["assignee"], me)
        conds = []
        if ids:
            conds.append(Issue.assignees.any(User.id.in_(ids)))
        if none:
            conds.append(~Issue.assignees.any())
        q = q.filter(db.or_(*conds))
    if "label" in f:
        ids, none = _ids(f["label"], me)
        conds = []
        if ids:
            conds.append(Issue.labels.any(Label.id.in_(ids)))
        if none:
            conds.append(~Issue.labels.any())
        q = q.filter(db.or_(*conds))
    if "module" in f:
        ids, none = _ids(f["module"], me)
        conds = []
        if ids:
            conds.append(Issue.modules.any(Module.id.in_(ids)))
        if none:
            conds.append(~Issue.modules.any())
        q = q.filter(db.or_(*conds))
    if "cycle" in f:
        ids, none = _ids(f["cycle"], me)
        conds = []
        if ids:
            conds.append(Issue.cycle_id.in_(ids))
        if none:
            conds.append(Issue.cycle_id.is_(None))
        q = q.filter(db.or_(*conds))
    if "created_by" in f:
        ids, _ = _ids(f["created_by"], me)
        q = q.filter(Issue.created_by_id.in_(ids))
    if "project" in f:
        ids, _ = _ids(f["project"], me)
        q = q.filter(Issue.project_id.in_(ids))
    if "subscribed" in f:
        q = q.filter(Issue.id.in_(db.select(issue_subscribers.c.issue_id)
                                  .where(issue_subscribers.c.user_id == me.id)))
    for key, col in (("start", Issue.start_date), ("target", Issue.target_date)):
        if key in f:
            clause = _date_clause(col, f[key])
            if clause is not None:
                q = q.filter(clause)
            if "overdue" in f[key]:
                q = q.filter(Issue.state_id.in_(
                    db.select(State.id).where(~State.group.in_(("completed", "cancelled")))))
    if "q" in f:
        text = f["q"]
        m = re.fullmatch(r"([A-Za-z][A-Za-z0-9]{0,9})-(\d+)", text)
        if m:
            q = q.filter(Issue.project_id.in_(db.select(Project.id).where(
                Project.identifier == m.group(1).upper())), Issue.sequence_id == int(m.group(2)))
        elif text.isdigit():
            q = q.filter(db.or_(Issue.sequence_id == int(text), contient(Issue.title, text)))
        else:
            q = q.filter(db.or_(contient(Issue.title, text), contient(Issue.description, text)))
    return q


def display_options(args, scope_key: str, default_layout="list") -> dict:
    """Options d’affichage, mémorisées par périmètre (projet, cycle...) dans la session."""
    store = session.get("display", {})
    saved = store.get(scope_key, {})
    opts = {
        "layout": args.get("layout") or saved.get("layout") or default_layout,
        "group_by": args.get("group_by") or saved.get("group_by") or "state",
        "order_by": args.get("order_by") or saved.get("order_by") or "manual",
        "sub_issues": (args.get("sub_issues") or saved.get("sub_issues") or "1") == "1",
        "show_closed": (args.get("show_closed") or saved.get("show_closed") or "1") == "1",
        "month": args.get("month") or "",
        "zoom": args.get("zoom") or saved.get("zoom") or "week",
    }
    if opts["layout"] not in {k for k, _, _ in LAYOUTS}:
        opts["layout"] = "list"
    if opts["group_by"] not in {k for k, _ in GROUP_OPTIONS}:
        opts["group_by"] = "state"
    if opts["order_by"] not in {k for k, _ in ORDER_OPTIONS}:
        opts["order_by"] = "manual"
    store[scope_key] = {k: (("1" if opts[k] else "0") if isinstance(opts[k], bool) else opts[k])
                        for k in ("layout", "group_by", "order_by", "sub_issues", "show_closed", "zoom")}
    session["display"] = store
    return opts


def fetch(q, opts: dict) -> list[Issue]:
    if not opts.get("sub_issues", True):
        q = q.filter(Issue.parent_id.is_(None))
    issues = q.all()
    if not opts.get("show_closed", True):
        issues = [i for i in issues if not i.is_closed]
    return sort_issues(issues, opts.get("order_by", "manual"))


def sort_issues(issues, order_by: str):
    far = date(9999, 12, 31)
    keys = {
        "manual": lambda i: (i.sort_order or 0, i.id),
        "-created": lambda i: i.created_at,
        "created": lambda i: i.created_at,
        "-updated": lambda i: i.updated_at or i.created_at,
        "priority": lambda i: (PRIORITY_ORDER.get(i.priority, 9), i.sort_order or 0),
        "target": lambda i: (i.target_date or far, PRIORITY_ORDER.get(i.priority, 9)),
        "start": lambda i: (i.start_date or far, i.target_date or far),
        "title": lambda i: cle_tri(i.title),
        "key": lambda i: (i.project.identifier, i.sequence_id),
    }
    reverse = order_by in ("-created", "-updated")
    return sorted(issues, key=keys.get(order_by, keys["manual"]), reverse=reverse)


def _group(key, title, kind, color=None, obj=None):
    return {"key": str(key), "title": title, "kind": kind, "color": color, "obj": obj, "issues": []}


def group_issues(issues, group_by: str, projects: list[Project], show_empty=True):
    """Retourne une liste de groupes {key, title, kind, color, obj, issues}."""
    single = projects[0] if len(projects) == 1 else None
    if group_by == "state" and single is None:
        group_by = "state_group"
    groups: dict[str, dict] = {}
    order: list[str] = []

    def add(g):
        if g["key"] not in groups:
            groups[g["key"]] = g
            order.append(g["key"])
        return groups[g["key"]]

    if group_by == "none":
        g = add(_group("all", "Tous les tickets", "none"))
        g["issues"] = list(issues)
        return [g]

    if group_by == "state":
        for s in single.states:
            add(_group(s.id, s.name, "state", s.color, s))
        for i in issues:
            if i.state_id and str(i.state_id) in groups:
                groups[str(i.state_id)]["issues"].append(i)
    elif group_by == "state_group":
        for k, label, color in STATE_GROUPS:
            add(_group(k, label, "state_group", color, k))
        for i in issues:
            groups[i.state_group]["issues"].append(i)
    elif group_by == "priority":
        for k, label, color in PRIORITIES:
            add(_group(k, label, "priority", color, k))
        for i in issues:
            groups.get(i.priority or "none", groups["none"])["issues"].append(i)
    elif group_by == "assignee":
        members = {}
        for p in projects:
            for u in p.member_users:
                members[u.id] = u
        for u in sorted(members.values(), key=lambda u: cle_tri(u.display_name)):
            add(_group(u.id, u.display_name, "user", u.avatar_color, u))
        none = _group("none", "Non assigné", "user")
        for i in issues:
            if not i.assignees:
                none["issues"].append(i)
            for u in i.assignees:
                add(_group(u.id, u.display_name, "user", u.avatar_color, u))["issues"].append(i)
        add(none)
    elif group_by == "label":
        for p in projects:
            for l in p.labels:
                title = l.name if single else f"{l.name} · {p.identifier}"
                add(_group(l.id, title, "label", l.color, l))
        none = _group("none", "Sans étiquette", "label")
        for i in issues:
            if not i.labels:
                none["issues"].append(i)
            for l in i.labels:
                add(_group(l.id, l.name, "label", l.color, l))["issues"].append(i)
        add(none)
    elif group_by == "cycle":
        for p in projects:
            for c in p.cycles:
                if c.archived_at is None:
                    add(_group(c.id, c.name if single else f"{c.name} · {p.identifier}", "cycle", None, c))
        none = _group("none", "Sans cycle", "cycle")
        for i in issues:
            if i.cycle_id is None:
                none["issues"].append(i)
            else:
                add(_group(i.cycle_id, i.cycle.name, "cycle", None, i.cycle))["issues"].append(i)
        add(none)
    elif group_by == "module":
        for p in projects:
            for m in p.modules:
                if m.archived_at is None:
                    add(_group(m.id, m.name if single else f"{m.name} · {p.identifier}", "module",
                               MODULE_STATUS_COLORS.get(m.status), m))
        none = _group("none", "Sans module", "module")
        for i in issues:
            if not i.modules:
                none["issues"].append(i)
            for m in i.modules:
                add(_group(m.id, m.name, "module", m.status_color, m))["issues"].append(i)
        add(none)
    elif group_by == "created_by":
        for i in issues:
            u = i.created_by
            key = u.id if u else "none"
            add(_group(key, u.display_name if u else "Inconnu", "user", u.avatar_color if u else None, u))[
                "issues"].append(i)
    elif group_by == "project":
        for p in projects:
            add(_group(p.id, p.name, "project", p.color, p))
        for i in issues:
            add(_group(i.project_id, i.project.name, "project", i.project.color, i.project))["issues"].append(i)

    result = [groups[k] for k in order]
    if not show_empty:
        result = [g for g in result if g["issues"]]
    return result


# ---------------------------------------------------------------------------
# Calendrier et Gantt
# ---------------------------------------------------------------------------

def calendar_weeks(month_str: str, issues):
    today = heure.today()
    try:
        y, m = (int(x) for x in month_str.split("-"))
        first = date(y, m, 1)
    except (ValueError, AttributeError):
        first = today.replace(day=1)
    start = first - timedelta(days=first.weekday())
    last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
    end = last + timedelta(days=6 - last.weekday())
    by_day: dict[date, list] = {}
    for i in issues:
        if i.target_date:
            by_day.setdefault(i.target_date, []).append(i)
    weeks, d = [], start
    while d <= end:
        week = []
        for _ in range(7):
            week.append({"date": d, "in_month": d.month == first.month, "is_today": d == today,
                         "issues": by_day.get(d, [])})
            d += timedelta(days=1)
        weeks.append(week)
    prev_m = (first - timedelta(days=1)).replace(day=1)
    next_m = (last + timedelta(days=1))
    return {
        "weeks": weeks, "first": first, "prev": prev_m.strftime("%Y-%m"), "next": next_m.strftime("%Y-%m"),
        "today": today.strftime("%Y-%m"), "undated": sum(1 for i in issues if not i.target_date),
    }


def gantt_data(issues, zoom: str = "week"):
    dated = [i for i in issues if i.start_date or i.target_date]
    today = heure.today()
    if dated:
        lo = min((i.start_date or i.target_date) for i in dated)
        hi = max((i.target_date or i.start_date) for i in dated)
    else:
        lo, hi = today - timedelta(days=7), today + timedelta(days=30)
    lo = min(lo, today) - timedelta(days=3)
    hi = max(hi, today) + timedelta(days=7)
    lo = lo - timedelta(days=lo.weekday())
    if (hi - lo).days > 400:
        hi = lo + timedelta(days=400)
    day_w = {"day": 36, "week": 22, "month": 8}.get(zoom, 22)
    days = (hi - lo).days + 1
    rows = []
    for i in sorted(issues, key=lambda x: (x.start_date or x.target_date or date(9999, 1, 1), x.sequence_id)):
        s = i.start_date or i.target_date
        e = i.target_date or i.start_date
        bar = None
        if s and e:
            if e < s:
                s, e = e, s
            s_c, e_c = max(s, lo), min(e, hi)
            if s_c <= e_c:
                bar = {"left": (s_c - lo).days * day_w, "width": ((e_c - s_c).days + 1) * day_w,
                       "only_start": i.target_date is None, "only_end": i.start_date is None}
        rows.append({"issue": i, "bar": bar})
    months, d = [], lo
    while d <= hi:
        nxt = (d.replace(day=1) + timedelta(days=32)).replace(day=1)
        span_end = min(nxt - timedelta(days=1), hi)
        months.append({"date": d, "left": (d - lo).days * day_w, "width": ((span_end - d).days + 1) * day_w})
        d = nxt
    day_list = [lo + timedelta(days=k) for k in range(days)]
    return {"rows": rows, "start": lo, "end": hi, "day_w": day_w, "width": days * day_w, "days": day_list,
            "months": months, "today_left": (today - lo).days * day_w if lo <= today <= hi else None,
            "zoom": zoom}


def query_string(args, **overrides) -> str:
    """Reconstruit la chaîne de requête avec des remplacements (None = supprimer)."""
    items = []
    keys = set(args.keys()) | set(overrides.keys())
    for k in sorted(keys):
        if k in overrides:
            v = overrides[k]
            if v is None or v == "":
                continue
            vals = v if isinstance(v, list) else [v]
        else:
            vals = args.getlist(k)
        for v in vals:
            items.append((k, v))
    return urlencode(items)


def remove_filter_value(args, key, value) -> str:
    vals = []
    for raw in args.getlist(key):
        vals.extend(v for v in raw.split(",") if v and v != value)
    return query_string(args, **{key: vals or None})


def filter_chips(f: dict, projects: list[Project], me: User):
    """Libellés lisibles des filtres actifs."""
    from .models import PRIORITY_LABELS, STATE_GROUP_LABELS
    names = {"state": "État", "state_group": "Groupe d’état", "priority": "Priorité", "assignee": "Assigné",
             "label": "Étiquette", "cycle": "Cycle", "module": "Module", "created_by": "Créé par",
             "project": "Projet", "start": "Début", "target": "Échéance", "subscribed": "Abonné"}
    presets = dict(DATE_PRESETS)
    chips = []
    for key, vals in f.items():
        if key == "q":
            continue
        for v in vals:
            label = v
            if v == "none":
                label = "Aucun"
            elif v == "me":
                label = "Moi"
            elif key == "state":
                s = db.session.get(State, int(v)) if v.isdigit() else None
                label = s.name if s else v
            elif key == "state_group":
                label = STATE_GROUP_LABELS.get(v, v)
            elif key == "priority":
                label = PRIORITY_LABELS.get(v, v)
            elif key in ("assignee", "created_by"):
                u = db.session.get(User, int(v)) if v.isdigit() else None
                label = u.display_name if u else v
            elif key == "label":
                l = db.session.get(Label, int(v)) if v.isdigit() else None
                label = l.name if l else v
            elif key == "cycle":
                c = db.session.get(Cycle, int(v)) if v.isdigit() else None
                label = c.name if c else v
            elif key == "module":
                m = db.session.get(Module, int(v)) if v.isdigit() else None
                label = m.name if m else v
            elif key == "project":
                p = db.session.get(Project, int(v)) if v.isdigit() else None
                label = p.name if p else v
            elif key in ("start", "target"):
                label = presets.get(v, v)
            elif key == "subscribed":
                label = "Oui"
            chips.append({"key": key, "value": v, "name": names.get(key, key), "label": label})
    return chips

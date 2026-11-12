"""Planning : feuille de route type Gantt (modules en couloirs, jalons, dépendances, cycles)."""
from __future__ import annotations

import calendar
from datetime import date, timedelta

from flask import Blueprint, render_template, request, session
from flask_login import current_user, login_required

from .. import heure
from .. import issue_query as IQ
from ..models import (
    MODULE_STATUS_COLORS, STATE_GROUP_COLORS, Issue, IssueRelation,
)
from ..texte import cle_tri
from ..utils import MONTHS, can, fmt_date, get_project, visible_projects

bp = Blueprint("planning", __name__)

ROW_H = 34
ZOOMS = [("day", "Jour", 34), ("week", "Semaine", 16), ("month", "Mois", 5), ("quarter", "Trimestre", 2)]
ZOOM_W = {k: w for k, _, w in ZOOMS}
GROUPS = [("module", "Module"), ("assignee", "Assigné"), ("cycle", "Cycle"), ("none", "Aucun")]
FAR = date(9999, 12, 31)


def _options(scope_key: str) -> dict:
    """Regroupement / échelle / masquage, mémorisés par périmètre dans la session."""
    store = session.get("planning", {})
    saved = store.get(scope_key, {})
    opts = {
        "group": request.args.get("group") or saved.get("group") or "module",
        "zoom": request.args.get("zoom") or saved.get("zoom") or "week",
        "hide_done": (request.args.get("hide_done") or saved.get("hide_done") or "0") == "1",
    }
    if opts["group"] not in dict(GROUPS):
        opts["group"] = "module"
    if opts["zoom"] not in ZOOM_W:
        opts["zoom"] = "week"
    store[scope_key] = {"group": opts["group"], "zoom": opts["zoom"], "hide_done": "1" if opts["hide_done"] else "0"}
    session["planning"] = store
    return opts


def _span(issue: Issue):
    s = issue.start_date or issue.target_date
    e = issue.target_date or issue.start_date
    if s and e and e < s:
        s, e = e, s
    return s, e


def _is_milestone(issue: Issue) -> bool:
    if any(l.name.lower() == "jalon" for l in issue.labels):
        return bool(issue.target_date or issue.start_date)
    return bool(issue.start_date and issue.target_date and issue.start_date == issue.target_date)


def _group_issues(project, issues, group_by):
    """Retourne [(clé, titre, type, objet, couleur, [tickets])] pour un projet."""
    def by_start(i):
        s, _ = _span(i)
        return (s or FAR, i.sequence_id)

    groups = []
    if group_by == "none":
        return [("all", "", "none", None, None, sorted(issues, key=by_start))]
    if group_by == "module":
        mods = sorted([m for m in project.modules if m.archived_at is None],
                      key=lambda m: (m.start_date or m.target_date or FAR, m.name.lower()))
        order = {m.id: k for k, m in enumerate(mods)}
        buckets = {m.id: [] for m in mods}
        none = []
        for i in issues:
            ms = [m for m in i.modules if m.id in order]
            if ms:
                buckets[min(ms, key=lambda m: order[m.id]).id].append(i)
            else:
                none.append(i)
        for m in mods:
            groups.append((f"m{m.id}", m.name, "module", m, MODULE_STATUS_COLORS.get(m.status),
                           sorted(buckets[m.id], key=by_start)))
        if none:
            groups.append(("m0", "Sans module", "module", None, "#a3a3a3", sorted(none, key=by_start)))
    elif group_by == "assignee":
        users = {}
        for i in issues:
            for u in i.assignees:
                users[u.id] = u
        ordered = sorted(users.values(), key=lambda u: cle_tri(u.display_name))
        buckets = {u.id: [] for u in ordered}
        none = []
        for i in issues:
            if i.assignees:
                first = min(i.assignees, key=lambda u: u.display_name.lower())
                buckets[first.id].append(i)
            else:
                none.append(i)
        for u in ordered:
            groups.append((f"u{u.id}", u.display_name, "user", u, u.avatar_color, sorted(buckets[u.id], key=by_start)))
        if none:
            groups.append(("u0", "Non assigné", "user", None, "#a3a3a3", sorted(none, key=by_start)))
    elif group_by == "cycle":
        cycles = sorted([c for c in project.cycles if c.archived_at is None],
                        key=lambda c: (c.start_date or FAR, c.name.lower()))
        ids = {c.id for c in cycles}
        buckets = {c.id: [] for c in cycles}
        none = []
        for i in issues:
            if i.cycle_id in ids:
                buckets[i.cycle_id].append(i)
            else:
                none.append(i)
        for c in cycles:
            if buckets[c.id] or c.status in ("current", "upcoming"):
                groups.append((f"c{c.id}", c.name, "cycle", c, "#3f76ff", sorted(buckets[c.id], key=by_start)))
        if none:
            groups.append(("c0", "Sans cycle", "cycle", None, "#a3a3a3", sorted(none, key=by_start)))
    return groups


def build_planning(projects, issues, group_by="module", zoom="week", multi=False):
    """Calcule toutes les positions (pixels) de la feuille de route."""
    dw = ZOOM_W.get(zoom, 16)
    today = heure.today()
    by_project = {p.id: [] for p in projects}
    for i in issues:
        by_project.setdefault(i.project_id, []).append(i)
    sections = []
    dates = [today]
    for p in projects:
        groups = _group_issues(p, by_project.get(p.id, []), group_by)
        sections.append((p, groups))
        dates += [d for d in (p.start_date, p.target_date) if d]
        for _, _, kind, obj, _, items in groups:
            if kind == "module" and obj is not None:
                dates += [d for d in (obj.start_date, obj.target_date) if d]
            if kind == "cycle" and obj is not None:
                dates += [d for d in (obj.start_date, obj.end_date) if d]
            for i in items:
                dates += [d for d in _span(i) if d]
    lo, hi = min(dates) - timedelta(days=7), max(dates) + timedelta(days=21)
    if zoom in ("month", "quarter"):
        lo = lo.replace(day=1)
        hi = hi.replace(day=calendar.monthrange(hi.year, hi.month)[1])
        if zoom == "quarter":
            lo = lo.replace(month=((lo.month - 1) // 3) * 3 + 1)
    else:
        lo = lo - timedelta(days=lo.weekday())
    if (hi - lo).days > 1100:
        hi = lo + timedelta(days=1100)
    ndays = (hi - lo).days + 1

    def x(d):
        return (d - lo).days * dw

    def bar(s, e):
        if not s or not e:
            return None
        s_c, e_c = max(s, lo), min(e, hi)
        if s_c > e_c:
            return None
        return {"left": x(s_c), "width": max(((e_c - s_c).days + 1) * dw, 4)}

    rows = []
    issue_rows = {}
    for p, groups in sections:
        pkey = f"p{p.id}"
        p_issues = by_project.get(p.id, [])
        if multi:
            s_list = [_span(i)[0] for i in p_issues if _span(i)[0]]
            e_list = [_span(i)[1] for i in p_issues if _span(i)[1]]
            s = p.start_date or (min(s_list) if s_list else None)
            e = p.target_date or (max(e_list) if e_list else None)
            done = sum(1 for i in p_issues if i.state_group == "completed")
            active = sum(1 for i in p_issues if i.state_group != "cancelled")
            rows.append({"kind": "project", "key": pkey, "parents": "", "project": p, "title": p.name,
                         "count": len(p_issues), "pct": round(100 * done / active) if active else 0,
                         "bar": bar(s, e), "color": p.color, "start": s, "end": e})
        for gkey, title, kind, obj, color, items in groups:
            full_gkey = f"{pkey}-{gkey}"
            parents = pkey if multi else ""
            if kind != "none":
                spans = [_span(i) for i in items]
                s_list = [s for s, _ in spans if s]
                e_list = [e for _, e in spans if e]
                if kind == "module" and obj is not None:
                    s = obj.start_date or (min(s_list) if s_list else None)
                    e = obj.target_date or (max(e_list) if e_list else None)
                elif kind == "cycle" and obj is not None:
                    s, e = obj.start_date, obj.end_date
                else:
                    s = min(s_list) if s_list else None
                    e = max(e_list) if e_list else None
                if s and e and e < s:
                    s, e = e, s
                active = [i for i in items if i.state_group != "cancelled"]
                done = sum(1 for i in active if i.state_group == "completed")
                late = sum(1 for i in items if i.is_overdue)
                rows.append({"kind": "group", "gkind": kind, "key": full_gkey, "parents": parents, "obj": obj,
                             "title": title, "count": len(items), "late": late, "color": color or "#a3a3a3",
                             "pct": round(100 * done / len(active)) if active else 0,
                             "bar": bar(s, e), "start": s, "end": e, "project": p})
                child_parents = (parents + " " + full_gkey).strip()
            else:
                child_parents = parents
            for i in items:
                s, e = _span(i)
                ms = _is_milestone(i)
                b = bar(s, e)
                if b and ms:
                    center = x(min(max(e, lo), hi)) + dw / 2
                    b = {"left": center - 8, "width": 16}
                row = {"kind": "issue", "key": f"i{i.id}", "parents": child_parents, "issue": i, "bar": b,
                       "milestone": ms, "start": s, "end": e,
                       "color": i.state.color if i.state else STATE_GROUP_COLORS.get(i.state_group, "#a3a3a3")}
                issue_rows[i.id] = (len(rows), row)
                rows.append(row)

    # Dépendances « bloqué par » : flèche de la fin du bloquant vers le début du ticket bloqué
    deps = []
    ids = list(issue_rows)
    if ids:
        rels = IssueRelation.query.filter(IssueRelation.relation_type == "blocked_by",
                                          IssueRelation.issue_id.in_(ids), IssueRelation.related_id.in_(ids)).all()
        for r in rels:
            (ri, blocked), (rj, blocker) = issue_rows[r.issue_id], issue_rows[r.related_id]
            if not blocked["bar"] or not blocker["bar"]:
                continue
            violated = bool(blocked["start"] and blocker["end"] and blocked["start"] <= blocker["end"])
            x1 = blocker["bar"]["left"] + blocker["bar"]["width"]
            y1 = rj * ROW_H + ROW_H / 2
            x2 = blocked["bar"]["left"]
            y2 = ri * ROW_H + ROW_H / 2
            deps.append({"from": r.related_id, "to": r.issue_id, "violated": violated,
                         "path": arrow_path(x1, y1, x2, y2)})

    # En-têtes : mois / trimestres, puis jours / semaines / mois
    top, sub = [], []
    if zoom == "quarter":
        d = lo
        while d <= hi:
            q_start = d.replace(month=((d.month - 1) // 3) * 3 + 1, day=1)
            q_next = (q_start.replace(day=28) + timedelta(days=95)).replace(day=1)
            q_next = q_next.replace(month=((q_next.month - 1) // 3) * 3 + 1)
            end = min(q_next - timedelta(days=1), hi)
            top.append({"left": x(d), "width": ((end - d).days + 1) * dw,
                        "label": f"T{(q_start.month - 1) // 3 + 1} {q_start.year}"})
            d = end + timedelta(days=1)
    else:
        d = lo
        while d <= hi:
            nxt = (d.replace(day=1) + timedelta(days=32)).replace(day=1)
            end = min(nxt - timedelta(days=1), hi)
            top.append({"left": x(d), "width": ((end - d).days + 1) * dw,
                        "label": f"{MONTHS[d.month - 1]} {d.year}"})
            d = end + timedelta(days=1)
    if zoom == "day":
        for k in range(ndays):
            d = lo + timedelta(days=k)
            sub.append({"left": x(d), "width": dw, "label": str(d.day), "we": d.weekday() >= 5,
                        "today": d == today})
    elif zoom in ("week", "month"):
        d = lo + timedelta(days=(7 - lo.weekday()) % 7)
        while d <= hi:
            label = f"{d.day}/{d.month}" if zoom == "week" else str(d.day)
            sub.append({"left": x(d), "width": 7 * dw, "label": label, "we": False,
                        "today": d <= today < d + timedelta(days=7)})
            d += timedelta(days=7)
    else:
        d = lo
        while d <= hi:
            nxt = (d.replace(day=1) + timedelta(days=32)).replace(day=1)
            end = min(nxt - timedelta(days=1), hi)
            sub.append({"left": x(d), "width": ((end - d).days + 1) * dw, "label": MONTHS[d.month - 1],
                        "we": False, "today": d <= today <= end})
            d = end + timedelta(days=1)

    # Bandes des cycles (réparties sur plusieurs lignes si elles se chevauchent)
    lanes_end: list[date] = []
    bands = []
    for p, _ in sections:
        if not p.cycles_enabled:
            continue
        for c in sorted(p.cycles, key=lambda c: c.start_date or FAR):
            if c.archived_at or not c.start_date or not c.end_date or c.end_date < lo or c.start_date > hi:
                continue
            lane = next((k for k, end in enumerate(lanes_end) if end < c.start_date), None)
            if lane is None:
                lane = len(lanes_end)
                lanes_end.append(c.end_date)
            else:
                lanes_end[lane] = c.end_date
            b = bar(c.start_date, c.end_date)
            bands.append({"cycle": c, "lane": lane, "left": b["left"], "width": b["width"],
                          "label": c.name if not multi else f"{c.name} · {p.identifier}",
                          "current": c.status == "current", "project": p})
    weekends = []
    if zoom in ("day", "week"):
        d = lo
        while d <= hi:
            if d.weekday() == 5:
                weekends.append({"left": x(d), "width": 2 * dw})
            d += timedelta(days=1)
    month_lines = [t["left"] for t in (top if zoom != "quarter" else sub)][1:]
    return {
        "rows": rows, "deps": deps, "deps_min": [{"f": d["from"], "t": d["to"]} for d in deps], "top": top, "sub": sub, "bands": bands, "band_lanes": max(len(lanes_end), 1),
        "weekends": weekends, "month_lines": month_lines, "start": lo, "end": hi, "dw": dw,
        "width": ndays * dw, "height": len(rows) * ROW_H, "row_h": ROW_H,
        "today_left": x(today) + dw / 2 if lo <= today <= hi else None, "zoom": zoom,
        "issue_count": len(issue_rows),
        "dated": sum(1 for _, r in issue_rows.values() if r["bar"]),
    }


def arrow_path(x1, y1, x2, y2) -> str:
    """Chemin orthogonal entre deux barres (sortie à droite, entrée à gauche)."""
    if x2 - x1 >= 16:
        mid = x1 + 8
        return f"M{x1:.1f},{y1:.1f} H{mid:.1f} V{y2:.1f} H{x2 - 2:.1f}"
    ymid = y2 - ROW_H / 2 if y2 > y1 else y2 + ROW_H / 2
    return (f"M{x1:.1f},{y1:.1f} h8 V{ymid:.1f} H{x2 - 10:.1f} V{y2:.1f} H{x2 - 2:.1f}")


def _render(projects, scope_key, project=None):
    multi = project is None
    opts = _options(scope_key)
    f = IQ.parse_filters(request.args)
    pids = [p.id for p in projects] or [0]
    q = IQ.apply_filters(IQ.base_query().filter(Issue.project_id.in_(pids)), f, current_user)
    issues = q.all()
    if opts["hide_done"]:
        issues = [i for i in issues if not i.is_closed]
    shown_projects = projects
    if multi and "project" in f:
        shown_projects = [p for p in projects if str(p.id) in f["project"]] or projects
    if multi:
        shown_projects = [p for p in shown_projects if any(i.project_id == p.id for i in issues)
                          or p.start_date or p.target_date]
    plan = build_planning(shown_projects, issues, opts["group"], opts["zoom"], multi=multi)
    editable = {p.id for p in projects if can(current_user, "edit", p)}
    members = {}
    for p in projects:
        for u in p.member_users:
            members[u.id] = u
    labels = [(l, p) for p in projects for l in p.labels]
    undated = [i for i in issues if not (i.start_date or i.target_date)]
    chips = IQ.filter_chips(f, projects, current_user)
    return render_template(
        "planning/index.html", project=project, projects=projects, plan=plan, opts=opts, filters=f,
        ZOOMS=ZOOMS, GROUPS=GROUPS, editable=editable, members=sorted(members.values(), key=lambda u: u.display_name),
        labels=labels, undated=undated, fmt_date=fmt_date, chips=chips,
    )


@bp.route("/planning")
@login_required
def workspace():
    return _render(visible_projects(current_user), "ws")


@bp.route("/projects/<int:pid>/planning")
@login_required
def index(pid):
    project = get_project(pid)
    return _render([project], f"p:{pid}", project=project)

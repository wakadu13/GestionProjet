"""Analytique : vue d'ensemble, analyse personnalisée (axe X × segment), charge de l'équipe."""
from collections import OrderedDict, defaultdict
from datetime import date, datetime, timedelta

from flask import Blueprint, Response, render_template, request
from flask_login import current_user, login_required

from .. import charts
from .. import issue_query as IQ
from ..models import (
    MODULE_STATUS_COLORS, PALETTE, PRIORITIES, PRIORITY_COLORS, PRIORITY_LABELS, STATE_GROUP_COLORS,
    STATE_GROUP_LABELS, STATE_GROUPS, Issue, WorkLog,
)
from ..texte import cle_tri
from ..utils import fmt_date, fmt_minutes, get_project, normalize, visible_projects, MONTHS

bp = Blueprint("analytics", __name__)

DIMENSIONS = OrderedDict([
    ("state_group", "Groupe d’état"), ("state", "État"), ("priority", "Priorité"), ("assignee", "Assigné"),
    ("label", "Étiquette"), ("cycle", "Cycle"), ("module", "Module"), ("project", "Projet"),
    ("created_by", "Créé par"), ("target_month", "Mois d’échéance"), ("created_month", "Mois de création"),
])
METRICS = OrderedDict([("count", "Nombre de tickets"), ("estimate", "Estimation (points)"),
                       ("time", "Temps passé (heures)")])


def dim_values(i: Issue, dim: str):
    if dim == "state_group":
        g = i.state_group
        return [(g, STATE_GROUP_LABELS[g], STATE_GROUP_COLORS[g])]
    if dim == "state":
        return [(i.state.name, i.state.name, i.state.color)] if i.state else [("?", "Sans état", "#a3a3a3")]
    if dim == "priority":
        return [(i.priority, PRIORITY_LABELS.get(i.priority, i.priority), PRIORITY_COLORS.get(i.priority))]
    if dim == "assignee":
        return [(u.id, u.display_name, u.avatar_color) for u in i.assignees] or [("none", "Non assigné", "#a3a3a3")]
    if dim == "label":
        return [(l.name, l.name, l.color) for l in i.labels] or [("none", "Sans étiquette", "#a3a3a3")]
    if dim == "cycle":
        return [(i.cycle.id, i.cycle.name, None)] if i.cycle else [("none", "Sans cycle", "#a3a3a3")]
    if dim == "module":
        return [(m.id, m.name, MODULE_STATUS_COLORS.get(m.status)) for m in i.modules] or \
            [("none", "Sans module", "#a3a3a3")]
    if dim == "project":
        return [(i.project_id, i.project.name, i.project.color)]
    if dim == "created_by":
        u = i.created_by
        return [(u.id, u.display_name, u.avatar_color)] if u else [("none", "Inconnu", "#a3a3a3")]
    if dim == "target_month":
        d = i.target_date
        return [(d.strftime("%Y-%m"), f"{MONTHS[d.month - 1]} {d.year}", None)] if d else \
            [("~", "Sans échéance", "#a3a3a3")]
    if dim == "created_month":
        d = i.created_at
        return [(d.strftime("%Y-%m"), f"{MONTHS[d.month - 1]} {d.year}", None)]
    return [("all", "Tous", None)]


def metric_value(i: Issue, metric: str) -> float:
    if metric == "estimate":
        return i.estimate or 0
    if metric == "time":
        return round(i.time_spent_minutes / 60, 1)
    return 1


def _scope(pid):
    if pid:
        project = get_project(pid)
        return project, [project]
    return None, visible_projects(current_user)


@bp.route("/analytics")
@bp.route("/projects/<int:pid>/analytics")
@login_required
def index(pid=None):
    project, projects = _scope(pid)
    tab = request.args.get("tab", "overview")
    period = int(request.args.get("period", 30) or 30)
    pids = [p.id for p in projects] or [0]
    sel = request.args.get("project")
    if not project and sel and sel.isdigit() and int(sel) in pids:
        pids = [int(sel)]
    issues = IQ.base_query().filter(Issue.project_id.in_(pids)).all()
    ctx = {"project": project, "projects": projects, "tab": tab, "period": period, "sel": sel,
           "DIMENSIONS": DIMENSIONS, "METRICS": METRICS}
    if tab == "custom":
        ctx.update(_custom(issues, project))
        if "csv_response" in ctx:
            return ctx["csv_response"]
    elif tab == "team":
        ctx.update(_team(issues, projects if not sel else [p for p in projects if p.id in pids], period))
    elif tab == "trends":
        ctx.update(_trends(issues, projects if not sel else [p for p in projects if p.id in pids], period))
    else:
        ctx.update(_overview(issues, period, project))
    return render_template("analytics/index.html", **ctx)


def _overview(issues, period, project):
    today = date.today()
    start = today - timedelta(days=period - 1)
    counts = {g: 0 for g, _, _ in STATE_GROUPS}
    prio = {k: 0 for k, _, _ in PRIORITIES}
    labels = {}
    for i in issues:
        counts[i.state_group] += 1
        if not i.is_closed:
            prio[i.priority or "none"] += 1
        for l in i.labels:
            d = labels.setdefault(l.name, {"color": l.color, "total": 0, "open": 0})
            d["total"] += 1
            d["open"] += 0 if i.is_closed else 1
    total = len(issues)
    done = counts["completed"]
    overdue = sum(1 for i in issues if i.is_overdue)
    lead_times = [(i.completed_at - i.created_at).days for i in issues
                  if i.completed_at and i.created_at and i.state_group == "completed"]
    days = [start + timedelta(days=k) for k in range(period)]
    created_series, done_series = [], []
    for d in days:
        created_series.append(sum(1 for i in issues if i.created_at and i.created_at.date() <= d))
        done_series.append(sum(1 for i in issues if i.completed_at and i.completed_at.date() <= d
                               and i.state_group == "completed"))
    trend = charts.line_chart([fmt_date(d, False) for d in days], [
        {"name": "Créés (cumul)", "color": "#3f76ff", "values": created_series, "fill": True},
        {"name": "Terminés (cumul)", "color": "#16a34a", "values": done_series, "fill": True},
    ], height=240)
    created_period = sum(1 for i in issues if i.created_at and i.created_at.date() >= start)
    done_period = sum(1 for i in issues if i.completed_at and i.completed_at.date() >= start
                      and i.state_group == "completed")
    # Activité par semaine (tickets terminés)
    weeks = []
    w0 = today - timedelta(days=today.weekday()) - timedelta(weeks=11)
    for k in range(12):
        ws = w0 + timedelta(weeks=k)
        we = ws + timedelta(days=6)
        weeks.append((f"{ws.day}/{ws.month}", sum(1 for i in issues if i.completed_at
                                                  and ws <= i.completed_at.date() <= we), "#16a34a"))
    breakdown = []
    if project:
        for m in project.modules:
            if m.archived_at is None:
                pr = m.progress()
                breakdown.append({"name": m.name, "total": pr["total"], "done": pr["done"], "pct": pr["pct"],
                                  "color": m.status_color, "sub": m.status_label})
    else:
        by_p = {}
        for i in issues:
            d = by_p.setdefault(i.project, {"total": 0, "done": 0, "cancel": 0})
            d["total"] += 1
            d["done"] += 1 if i.state_group == "completed" else 0
            d["cancel"] += 1 if i.state_group == "cancelled" else 0
        for p, d in sorted(by_p.items(), key=lambda x: x[0].name):
            denom = d["total"] - d["cancel"]
            breakdown.append({"name": f"{p.emoji} {p.name}", "total": d["total"], "done": d["done"],
                              "pct": round(100 * d["done"] / denom) if denom else 0, "color": p.color,
                              "sub": p.identifier})
    return {
        "kpis": {"total": total, "open": total - done - counts["cancelled"], "done": done, "overdue": overdue,
                 "rate": round(100 * done / (total - counts["cancelled"])) if total - counts["cancelled"] else 0,
                 "lead": round(sum(lead_times) / len(lead_times), 1) if lead_times else None,
                 "created_period": created_period, "done_period": done_period},
        "donut": charts.donut([(lab, counts[g], col) for g, lab, col in STATE_GROUPS], size=180, thickness=24,
                              center_label="tickets"),
        "counts": counts,
        "prio_chart": charts.bar_chart([(PRIORITY_LABELS[k], prio[k], PRIORITY_COLORS[k]) for k, _, _ in PRIORITIES]),
        "trend": trend,
        "weekly": charts.bar_chart(weeks, height=200),
        "labels": sorted(labels.items(), key=lambda x: -x[1]["total"])[:12],
        "breakdown": breakdown,
    }


def _custom(issues, project):
    x = request.args.get("x", "assignee")
    seg = request.args.get("segment", "state_group")
    metric = request.args.get("metric", "count")
    if x not in DIMENSIONS:
        x = "assignee"
    if seg not in DIMENSIONS and seg != "none":
        seg = "state_group"
    if metric not in METRICS:
        metric = "count"
    cats: "OrderedDict[str, str]" = OrderedDict()
    segs: "OrderedDict[str, tuple]" = OrderedDict()
    matrix: dict = {}
    for i in issues:
        val = metric_value(i, metric)
        for xk, xl, _ in dim_values(i, x):
            cats.setdefault(str(xk), xl)
            for sk, sl, sc in (dim_values(i, seg) if seg != "none" else [("all", METRICS[metric], "#3f76ff")]):
                segs.setdefault(str(sk), (sl, sc))
                matrix[(str(xk), str(sk))] = matrix.get((str(xk), str(sk)), 0) + val
    if x in ("target_month", "created_month"):
        cats = OrderedDict(sorted(cats.items()))
    if seg == "state_group":
        segs = OrderedDict((g, (STATE_GROUP_LABELS[g], STATE_GROUP_COLORS[g])) for g, _, _ in STATE_GROUPS
                           if g in segs)
    elif seg == "priority":
        segs = OrderedDict((k, (PRIORITY_LABELS[k], PRIORITY_COLORS[k])) for k, _, _ in PRIORITIES if k in segs)
    series = []
    for idx, (sk, (sl, sc)) in enumerate(segs.items()):
        series.append((sl, sc or PALETTE[idx % len(PALETTE)], [matrix.get((ck, sk), 0) for ck in cats]))
    table = [{"label": cl, "values": [matrix.get((ck, sk), 0) for sk in segs],
              "total": round(sum(matrix.get((ck, sk), 0) for sk in segs), 1)} for ck, cl in cats.items()]
    if request.args.get("format") == "csv":
        import csv
        import io

        from ..utils import csv_safe
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow([DIMENSIONS[x]] + [csv_safe(s[0]) for s in segs.values()] + ["Total"])
        for row in table:
            w.writerow([csv_safe(row["label"])] + row["values"] + [row["total"]])
        return {"csv_response": Response("﻿" + buf.getvalue(), mimetype="text/csv",
                                         headers={"Content-Disposition": "attachment; filename=analytique.csv"})}
    return {"x": x, "segment": seg, "metric": metric,
            "chart": charts.stacked_bars(list(cats.values()), series) if cats else None,
            "series": series, "table": table, "seg_labels": [s for s in segs.values()]}


def _team(issues, projects, period):
    today = date.today()
    start = today - timedelta(days=period - 1)
    members = {}
    for p in projects:
        for u in p.member_users:
            members[u.id] = u
    pids = [p.id for p in projects] or [0]
    logs = WorkLog.query.join(Issue).filter(Issue.project_id.in_(pids), WorkLog.date >= start).all()
    rows = []
    for u in sorted(members.values(), key=lambda u: cle_tri(u.display_name)):
        mine = [i for i in issues if any(a.id == u.id for a in i.assignees)]
        open_ = [i for i in mine if not i.is_closed]
        done_p = [i for i in mine if i.completed_at and i.completed_at.date() >= start and i.state_group == "completed"]
        minutes = sum(w.minutes for w in logs if w.user_id == u.id)
        rows.append({
            "user": u, "open": len(open_), "started": sum(1 for i in open_ if i.state_group == "started"),
            "overdue": sum(1 for i in open_ if i.is_overdue), "done": len(done_p),
            "points": sum(i.estimate or 0 for i in open_), "minutes": minutes,
            "groups": {g: sum(1 for i in mine if i.state_group == g) for g, _, _ in STATE_GROUPS},
            "total": len(mine),
        })
    unassigned = sum(1 for i in issues if not i.assignees and not i.is_closed)
    max_open = max([r["open"] for r in rows] + [1])
    chart = charts.stacked_bars(
        [r["user"].display_name for r in rows],
        [(lab, col, [r["groups"][g] for r in rows]) for g, lab, col in STATE_GROUPS],
        height=260) if rows else None
    return {"rows": rows, "unassigned": unassigned, "max_open": max_open, "team_chart": chart,
            "fmt_minutes": fmt_minutes}


@bp.app_template_filter("pct")
def _pct(v, total):
    return round(100 * v / total) if total else 0


# ---------------------------------------------------------------------------
# Tendances : flux cumulé, burn-up, vélocité, délais
# ---------------------------------------------------------------------------

def _guess_group(name: str) -> str:
    n = normalize(name)
    if "annul" in n or "cancel" in n:
        return "cancelled"
    if "termin" in n or "done" in n or "fait" in n or "clos" in n:
        return "completed"
    if "cours" in n or "revue" in n or "progress" in n or "review" in n:
        return "started"
    if "backlog" in n:
        return "backlog"
    return "unstarted"


def state_history(issues):
    """Pour chaque ticket : (date de création, groupe initial, [(date, nouveau groupe)]).

    Reconstruit depuis l'historique des changements d'état ; à défaut, utilise la date de clôture.
    """
    from ..models import Activity, State
    ids = [i.id for i in issues] or [0]
    pids = {i.project_id for i in issues} or {0}
    names = {(s.project_id, s.name.lower()): s.group for s in State.query.filter(State.project_id.in_(pids))}

    def grp(pid, name):
        if not name:
            return None
        return names.get((pid, name.lower())) or _guess_group(name)

    changes = defaultdict(list)
    for a in (Activity.query.filter(Activity.issue_id.in_(ids), Activity.verb == "updated",
                                    Activity.field == "state").order_by(Activity.created_at)):
        changes[a.issue_id].append(a)
    out = {}
    for i in issues:
        created = (i.created_at or datetime.now()).date()
        ch = changes.get(i.id, [])
        if ch:
            initial = grp(i.project_id, ch[0].old_value) or "unstarted"
            seq = [(a.created_at.date(), grp(i.project_id, a.new_value) or i.state_group) for a in ch]
        elif i.state_group == "completed" and i.completed_at:
            initial, seq = "unstarted", [(i.completed_at.date(), "completed")]
        elif i.state_group == "cancelled":
            initial, seq = "unstarted", [((i.updated_at or i.created_at or datetime.now()).date(), "cancelled")]
        else:
            initial, seq = i.state_group, []
        out[i.id] = (created, initial, seq)
    return out


def group_on(hist, d):
    created, g, seq = hist
    if d < created:
        return None
    for cd, ng in seq:
        if cd <= d:
            g = ng
        else:
            break
    return g


def _trends(issues, projects, period):
    today = date.today()
    first = min([i.created_at.date() for i in issues if i.created_at] or [today])
    start = max(today - timedelta(days=period - 1), first - timedelta(days=1))
    days = [start + timedelta(days=k) for k in range((today - start).days + 1)]
    if len(days) < 2:
        days = [today - timedelta(days=1), today]
    labels = [fmt_date(d, False) for d in days]
    hist = state_history(issues)
    order = ["completed", "started", "unstarted", "backlog"]
    counts = {g: [0] * len(days) for g in order + ["cancelled"]}
    for h in hist.values():
        for k, d in enumerate(days):
            g = group_on(h, d)
            if g:
                counts[g][k] += 1
    cfd = charts.stacked_area(labels, [(STATE_GROUP_LABELS[g], STATE_GROUP_COLORS[g], counts[g]) for g in order])
    scope = [sum(counts[g][k] for g in order) for k in range(len(days))]
    done = counts["completed"]
    burnup = charts.line_chart(labels, [
        {"name": "Périmètre (hors annulés)", "color": "#3f76ff", "values": scope},
        {"name": "Terminés (cumul)", "color": "#16a34a", "values": done, "fill": True},
    ], height=240)

    # Rythme récent et projection de fin
    window = min(14, len(days) - 1)
    rate = (done[-1] - done[-1 - window]) / window if window else 0
    remaining = scope[-1] - done[-1]
    forecast = None
    if remaining <= 0:
        forecast = "terminé"
    elif rate > 0:
        forecast = fmt_date(today + timedelta(days=round(remaining / rate)), True)

    # Vélocité par cycle (cycles commencés)
    cycles = sorted([c for p in projects if p.cycles_enabled for c in p.cycles
                     if c.archived_at is None and c.start_date and c.end_date and c.start_date <= today],
                    key=lambda c: c.start_date)[-12:]
    cats, done_c, open_c, pts = [], [], [], []
    for c in cycles:
        its = [i for i in issues if i.cycle_id == c.id and i.state_group != "cancelled"]
        d_ = [i for i in its if i.state_group == "completed"]
        cats.append(c.name if len(projects) == 1 else f"{c.name} · {c.project.identifier}")
        done_c.append(len(d_))
        open_c.append(len(its) - len(d_))
        pts.append(sum(i.estimate or 0 for i in d_))
    finished = [n for c, n in zip(cycles, done_c) if c.status == "completed"]
    velocity = charts.stacked_bars(cats, [("Terminés", STATE_GROUP_COLORS["completed"], done_c),
                                          ("Non terminés", "#a3a3a3", open_c)], height=250) if cats else None
    points_chart = charts.bar_chart([(c, p, "#8b5cf6") for c, p in zip(cats, pts)], height=210) if any(pts) else None

    # Délai de réalisation et âge des tickets ouverts
    buckets = [("≤ 1 j", 0, 1), ("2–3 j", 2, 3), ("4–7 j", 4, 7), ("8–14 j", 8, 14), ("15–30 j", 15, 30),
               ("31–60 j", 31, 60), ("> 60 j", 61, 10 ** 6)]
    leads = sorted((i.completed_at.date() - i.created_at.date()).days for i in issues
                   if i.state_group == "completed" and i.completed_at and i.created_at)
    ages = [(today - i.created_at.date()).days for i in issues if not i.is_closed and i.created_at]
    lead_chart = charts.bar_chart([(lab, sum(1 for v in leads if lo <= v <= hi), "#0ea5e9")
                                   for lab, lo, hi in buckets], height=210) if leads else None
    age_chart = charts.bar_chart([(lab, sum(1 for v in ages if lo <= v <= hi), "#f59e0b")
                                  for lab, lo, hi in buckets], height=210) if ages else None
    weeks = max(len(days) / 7, 1)
    return {
        "cfd": cfd, "burnup": burnup, "velocity": velocity, "points_chart": points_chart,
        "lead_chart": lead_chart, "age_chart": age_chart,
        "trend_kpis": {
            "wip": counts["started"][-1], "done_period": done[-1] - done[0],
            "throughput": round((done[-1] - done[0]) / weeks, 1),
            "median_lead": leads[len(leads) // 2] if leads else None,
            "avg_velocity": round(sum(finished) / len(finished), 1) if finished else None,
            "remaining": remaining, "forecast": forecast, "rate": round(rate * 7, 1),
            "oldest_open": max(ages) if ages else None, "days": len(days),
        },
        "trend_order": order,
    }

"""Cycles (sprints) : liste, détail avec burndown, transfert des tickets non terminés."""
from datetime import timedelta

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from .. import charts, heure
from .. import issue_query as IQ
from ..extensions import db
from ..models import STATE_GROUPS, Cycle, Issue, now
from ..services import to_int, update_issue
from ..utils import can, daterange, fmt_date, get_project, parse_date
from .home import _layout_context

bp = Blueprint("cycles", __name__, url_prefix="/projects/<int:pid>/cycles")


def _cycle(pid, cid, need="view"):
    project = get_project(pid, need)
    if not project.cycles_enabled:
        abort(404)
    c = db.session.get(Cycle, cid)
    if c is None or c.project_id != project.id:
        abort(404)
    return project, c


@bp.route("/")
@login_required
def index(pid):
    project = get_project(pid)
    if not project.cycles_enabled:
        flash("Les cycles sont désactivés pour ce projet.", "info")
        return redirect(url_for("issues.index", pid=pid))
    show_archived = request.args.get("archived") == "1"
    cycles = [c for c in project.cycles if (c.archived_at is not None) == show_archived]
    buckets = {"current": [], "upcoming": [], "completed": [], "draft": []}
    for c in cycles:
        buckets[c.status].append(c)
    buckets["completed"].sort(key=lambda c: c.end_date, reverse=True)
    return render_template("cycles/index.html", project=project, buckets=buckets, show_archived=show_archived,
                           can_edit=can(current_user, "edit", project))


def _overlaps(project, start, end, exclude_id=None):
    if not start or not end:
        return None
    for c in project.cycles:
        if c.id == exclude_id or c.archived_at or not c.start_date or not c.end_date:
            continue
        if start <= c.end_date and end >= c.start_date:
            return c
    return None


@bp.route("/new", methods=["POST"])
@login_required
def create(pid):
    project = get_project(pid, "edit")
    name = (request.form.get("name") or "").strip()
    start, end = parse_date(request.form.get("start_date")), parse_date(request.form.get("end_date"))
    if not name:
        flash("Le nom du cycle est obligatoire.", "error")
    elif start and end and end < start:
        flash("La date de fin doit être postérieure à la date de début.", "error")
    elif (clash := _overlaps(project, start, end)) is not None:
        flash(f"Les dates chevauchent le cycle « {clash.name} ».", "error")
    else:
        c = Cycle(project=project, name=name[:160], description=request.form.get("description", ""),
                  start_date=start, end_date=end, owner=current_user)
        db.session.add(c)
        db.session.commit()
        flash(f"Cycle « {c.name} » créé.", "success")
        return redirect(url_for("cycles.detail", pid=pid, cid=c.id))
    return redirect(url_for("cycles.index", pid=pid))


@bp.route("/<int:cid>")
@login_required
def detail(pid, cid):
    project, cycle = _cycle(pid, cid)
    f = IQ.parse_filters(request.args)
    opts = IQ.display_options(request.args, f"c:{cid}")
    if opts["layout"] in ("calendar", "gantt") and not request.args.get("layout"):
        opts["layout"] = "list"
    q = IQ.apply_filters(IQ.base_query().filter(Issue.cycle_id == cid), f, current_user)
    issues = IQ.fetch(q, opts)
    ctx = _layout_context(issues, opts, [project], f)
    stats = cycle_stats(cycle)
    other_cycles = [c for c in project.cycles if c.id != cycle.id and c.archived_at is None
                    and c.status in ("current", "upcoming", "draft")]
    candidates = [i for i in IQ.base_query().filter(Issue.project_id == pid, Issue.cycle_id.is_(None))
                  .order_by(Issue.sequence_id).all() if not i.is_closed]
    return render_template("cycles/detail.html", project=project, cycle=cycle, stats=stats,
                           other_cycles=other_cycles, candidates=candidates,
                           can_edit=can(current_user, "edit", project), **ctx)


def cycle_stats(cycle: Cycle) -> dict:
    issues = cycle.active_issues
    prog = cycle.progress()
    by_assignee, by_label = {}, {}
    for i in issues:
        for u in (i.assignees or [None]):
            key = u.display_name if u else "Non assigné"
            d = by_assignee.setdefault(key, {"user": u, "total": 0, "done": 0})
            d["total"] += 1
            d["done"] += 1 if i.state_group == "completed" else 0
        for l in i.labels:
            d = by_label.setdefault(l.name, {"label": l, "total": 0, "done": 0})
            d["total"] += 1
            d["done"] += 1 if i.state_group == "completed" else 0
    burndown = None
    if cycle.start_date and cycle.end_date:
        burndown = _burndown(cycle, issues)
    points_total = sum(i.estimate or 0 for i in issues)
    points_done = sum(i.estimate or 0 for i in issues if i.state_group == "completed")
    return {"progress": prog, "by_assignee": sorted(by_assignee.values(), key=lambda d: -d["total"]),
            "by_label": sorted(by_label.values(), key=lambda d: -d["total"]), "burndown": burndown,
            "points_total": points_total, "points_done": points_done,
            "donut": charts.donut([(lab, prog["counts"][g], col) for g, lab, col in STATE_GROUPS],
                                  size=150, thickness=18, center_value=f"{prog['pct']} %",
                                  center_label="terminé")}


def _burndown(cycle, issues):
    days = list(daterange(cycle.start_date, cycle.end_date))
    if len(days) < 2:
        days = [cycle.start_date, cycle.start_date + timedelta(days=1)]
    scope = [i for i in issues if i.state_group != "cancelled"]
    total = len(scope)
    today = heure.today()
    ideal = [round(total - total * k / (len(days) - 1), 2) for k in range(len(days))]
    actual = []
    for d in days:
        if d > today:
            actual.append(None)
            continue
        remaining = sum(1 for i in scope if not (i.completed_at and i.completed_at.date() <= d))
        actual.append(remaining)
    labels = [fmt_date(d, False) for d in days]
    today_idx = (today - cycle.start_date).days if cycle.start_date <= today <= cycle.end_date else None
    return charts.line_chart(labels, [
        {"name": "Idéal", "color": "#a3a3a3", "values": ideal, "dashed": True},
        {"name": "Restant", "color": "#3f76ff", "values": actual, "fill": True},
    ], height=220, today_index=today_idx)


@bp.route("/<int:cid>/edit", methods=["POST"])
@login_required
def edit(pid, cid):
    project, cycle = _cycle(pid, cid, "edit")
    start, end = parse_date(request.form.get("start_date")), parse_date(request.form.get("end_date"))
    if start and end and end < start:
        flash("La date de fin doit être postérieure à la date de début.", "error")
    elif (clash := _overlaps(project, start, end, exclude_id=cycle.id)) is not None:
        flash(f"Les dates chevauchent le cycle « {clash.name} ».", "error")
    else:
        cycle.name = (request.form.get("name") or cycle.name).strip()[:160]
        cycle.description = request.form.get("description", "")
        cycle.start_date, cycle.end_date = start, end
        db.session.commit()
        flash("Cycle mis à jour.", "success")
    return redirect(url_for("cycles.detail", pid=pid, cid=cid))


@bp.route("/<int:cid>/<action>", methods=["POST"])
@login_required
def action(pid, cid, action):
    project, cycle = _cycle(pid, cid, "edit")
    if action == "archive":
        cycle.archived_at = None if cycle.archived_at else now()
        db.session.commit()
        flash("Cycle archivé." if cycle.archived_at else "Cycle restauré.", "info")
        return redirect(url_for("cycles.index", pid=pid))
    if action == "delete":
        name = cycle.name
        for i in cycle.issues:
            i.cycle = None
        db.session.delete(cycle)
        db.session.commit()
        flash(f"Cycle « {name} » supprimé (les tickets sont conservés).", "info")
        return redirect(url_for("cycles.index", pid=pid))
    if action == "transfer":
        target = db.session.get(Cycle, to_int(request.form.get("target_id")) or 0)
        if not target or target.project_id != project.id:
            flash("Choisis un cycle de destination.", "error")
        else:
            n = 0
            for i in list(cycle.active_issues):
                if not i.is_closed:
                    update_issue(i, {"cycle_id": target.id}, current_user)
                    n += 1
            db.session.commit()
            flash(f"{n} ticket(s) non terminé(s) transféré(s) vers « {target.name} ».", "success")
        return redirect(url_for("cycles.detail", pid=pid, cid=cid))
    if action == "add-issues":
        n = 0
        for iid in request.form.getlist("issue_ids"):
            i = db.session.get(Issue, to_int(iid) or 0)
            if i and i.project_id == project.id:
                update_issue(i, {"cycle_id": cycle.id}, current_user)
                n += 1
        db.session.commit()
        flash(f"{n} ticket(s) ajouté(s) au cycle.", "success")
        return redirect(url_for("cycles.detail", pid=pid, cid=cid))
    abort(404)

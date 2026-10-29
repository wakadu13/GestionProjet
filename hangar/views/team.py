"""Équipe : annuaire, profils, charge de travail, disponibilités, gestion des comptes."""
import random
import secrets
from datetime import date, timedelta

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from .. import charts
from .. import issue_query as IQ
from ..extensions import db
from ..models import (
    PALETTE, PRIORITIES, PRIORITY_COLORS, PRIORITY_LABELS, ROLES, STATE_GROUPS, Absence, Activity, Issue,
    ProjectMember, User, WorkLog, issue_subscribers,
)
from ..services import to_int
from ..utils import (back_url, fmt_date, parse_date, require_admin, visible_projects, week_bounds)
from .auth import _validate_user_fields

bp = Blueprint("team", __name__, url_prefix="/team")


def _member_stats(users, pids):
    today = date.today()
    ws, we = week_bounds(today)
    issues = IQ.base_query().filter(Issue.project_id.in_(pids or [0])).all()
    logs = WorkLog.query.filter(WorkLog.date >= ws, WorkLog.date <= we).all()
    absences = Absence.query.filter(Absence.end_date >= today).all()
    out = {}
    for u in users:
        mine = [i for i in issues if any(a.id == u.id for a in i.assignees)]
        open_ = [i for i in mine if not i.is_closed]
        week_min = sum(w.minutes for w in logs if w.user_id == u.id)
        out[u.id] = {
            "open": len(open_), "overdue": sum(1 for i in open_ if i.is_overdue),
            "started": sum(1 for i in open_ if i.state_group == "started"),
            "done": sum(1 for i in mine if i.state_group == "completed"),
            "due_week": sum(1 for i in open_ if i.target_date and ws <= i.target_date <= we),
            "week_minutes": week_min,
            "capacity_pct": round(100 * week_min / (u.capacity_hours * 60)) if u.capacity_hours else 0,
            "projects": len([m for m in u.memberships if m.project.archived_at is None]),
            "away": next((a for a in absences if a.user_id == u.id and a.start_date <= today), None),
            "next_absence": next((a for a in sorted(absences, key=lambda a: a.start_date)
                                  if a.user_id == u.id and a.start_date > today), None),
        }
    return out


@bp.route("/")
@login_required
def index():
    show_inactive = request.args.get("inactive") == "1" and current_user.is_admin
    q = User.query
    if not show_inactive:
        q = q.filter(User.active.is_(True))
    role = request.args.get("role")
    if role in ROLES:
        q = q.filter(User.role == role)
    users = sorted(q.all(), key=lambda u: u.display_name.lower())
    pids = [p.id for p in visible_projects(current_user)]
    stats = _member_stats(users, pids)
    return render_template("team/index.html", users=users, stats=stats, show_inactive=show_inactive, role=role,
                           ROLES=ROLES, PALETTE=PALETTE)


@bp.route("/workload")
@login_required
def workload():
    weeks_n = int(request.args.get("weeks", 6) or 6)
    projects = visible_projects(current_user)
    sel = to_int(request.args.get("project"))
    if sel:
        projects = [p for p in projects if p.id == sel] or projects
    pids = [p.id for p in projects]
    members = {}
    for p in projects:
        for u in p.member_users:
            members[u.id] = u
    users = sorted(members.values(), key=lambda u: u.display_name.lower())
    today = date.today()
    ws, _ = week_bounds(today)
    weeks = [ws + timedelta(weeks=k) for k in range(weeks_n)]
    issues = [i for i in IQ.base_query().filter(Issue.project_id.in_(pids or [0])).all() if not i.is_closed]
    absences = Absence.query.filter(Absence.end_date >= ws).all()
    grid = []
    for u in users:
        mine = [i for i in issues if any(a.id == u.id for a in i.assignees)]
        cells = []
        for w in weeks:
            we = w + timedelta(days=6)
            due = [i for i in mine if i.target_date and w <= i.target_date <= we]
            # tickets « en cours » sur la semaine (période début-échéance qui chevauche)
            active = [i for i in mine if i.start_date and i.target_date and i.start_date <= we and i.target_date >= w]
            away_days = 0
            for a in absences:
                if a.user_id == u.id:
                    s, e = max(a.start_date, w), min(a.end_date, we - timedelta(days=2))
                    if s <= e:
                        away_days += sum(1 for k in range((e - s).days + 1) if (s + timedelta(days=k)).weekday() < 5)
            load = len({i.id for i in due + active})
            cells.append({"week": w, "due": due, "active": active, "load": load, "away": away_days,
                          "points": sum(i.estimate or 0 for i in {i.id: i for i in due + active}.values())})
        overdue = [i for i in mine if i.is_overdue]
        undated = [i for i in mine if not i.target_date]
        grid.append({"user": u, "cells": cells, "overdue": overdue, "undated": undated, "open": len(mine)})
    unassigned = [i for i in issues if not i.assignees]
    return render_template("team/workload.html", grid=grid, weeks=weeks, weeks_n=weeks_n, unassigned=unassigned,
                           projects=visible_projects(current_user), sel=sel)


@bp.route("/<username>")
@login_required
def profile(username):
    u = User.query.filter_by(username=username).first_or_404()
    tab = request.args.get("tab", "overview")
    projects = visible_projects(current_user)
    pids = [p.id for p in projects] or [0]
    base = IQ.base_query().filter(Issue.project_id.in_(pids))
    assigned = base.filter(Issue.assignees.any(User.id == u.id)).all()
    created = IQ.base_query().filter(Issue.project_id.in_(pids), Issue.created_by_id == u.id).all()
    subscribed = IQ.base_query().filter(Issue.project_id.in_(pids), Issue.id.in_(
        db.select(issue_subscribers.c.issue_id).where(issue_subscribers.c.user_id == u.id))).all()
    groups = {g: 0 for g, _, _ in STATE_GROUPS}
    prio = {k: 0 for k, _, _ in PRIORITIES}
    for i in assigned:
        groups[i.state_group] += 1
        if not i.is_closed:
            prio[i.priority or "none"] += 1
    activity = (Activity.query.filter(Activity.actor_id == u.id, Activity.project_id.in_(pids))
                .order_by(Activity.created_at.desc()).limit(60).all())
    # Contributions des 16 dernières semaines (activités par jour)
    today = date.today()
    start = today - timedelta(days=today.weekday()) - timedelta(weeks=15)
    acts_all = Activity.query.filter(Activity.actor_id == u.id, Activity.created_at >= start).all()
    per_day = {}
    for a in acts_all:
        per_day[a.created_at.date()] = per_day.get(a.created_at.date(), 0) + 1
    heat = []
    for w in range(16):
        col = []
        for d in range(7):
            day = start + timedelta(weeks=w, days=d)
            col.append({"date": day, "n": per_day.get(day, 0), "future": day > today})
        heat.append(col)
    logs = WorkLog.query.filter_by(user_id=u.id).order_by(WorkLog.date.desc()).limit(100).all()
    ws, we = week_bounds(today)
    week_minutes = sum(w.minutes for w in logs if ws <= w.date <= we)
    absences = Absence.query.filter_by(user_id=u.id).order_by(Absence.start_date.desc()).all()
    memberships = [m for m in u.memberships if m.project.archived_at is None and m.project_id in pids]
    lists = {"assigned": assigned, "created": created, "subscribed": subscribed}
    shown = IQ.sort_issues(lists.get(tab, []), "priority") if tab in lists else []
    return render_template(
        "team/profile.html", u=u, tab=tab, assigned=assigned, created=created, subscribed=subscribed,
        groups=groups, activity=activity, heat=heat, logs=logs, week_minutes=week_minutes, absences=absences,
        memberships=memberships, shown=shown,
        donut=charts.donut([(lab, groups[g], col) for g, lab, col in STATE_GROUPS], size=150, thickness=18,
                           center_label="assignés"),
        prio_chart=charts.bar_chart([(PRIORITY_LABELS[k], prio[k], PRIORITY_COLORS[k]) for k, _, _ in PRIORITIES],
                                    height=180),
        ROLES=ROLES,
    )


# --- Disponibilités --------------------------------------------------------

@bp.route("/absences", methods=["POST"])
@login_required
def absence_add():
    uid = to_int(request.form.get("user_id")) or current_user.id
    if uid != current_user.id and not current_user.is_admin:
        abort(403)
    s, e = parse_date(request.form.get("start_date")), parse_date(request.form.get("end_date"))
    if not s or not e or e < s:
        flash("Dates d’absence invalides.", "error")
    else:
        db.session.add(Absence(user_id=uid, start_date=s, end_date=e, kind=request.form.get("kind") or "Congés",
                               note=(request.form.get("note") or "")[:255]))
        db.session.commit()
        flash(f"Absence enregistrée du {fmt_date(s, True)} au {fmt_date(e, True)}.", "success")
    return redirect(back_url(url_for("team.index")))


@bp.route("/absences/<int:aid>/delete", methods=["POST"])
@login_required
def absence_delete(aid):
    a = db.session.get(Absence, aid) or abort(404)
    if a.user_id != current_user.id and not current_user.is_admin:
        abort(403)
    db.session.delete(a)
    db.session.commit()
    return redirect(back_url(url_for("team.index")))


# --- Administration des comptes ---------------------------------------------

@bp.route("/invite", methods=["POST"])
@login_required
def invite():
    require_admin()
    form = request.form.to_dict()
    temp = form.get("password") or secrets.token_urlsafe(6)
    form["password"] = temp
    errors, username, password = _validate_user_fields(form)
    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("team.index"))
    u = User(email=User.internal_email(username), username=username, first_name=form.get("first_name", "").strip(),
             last_name=form.get("last_name", "").strip(), role=form.get("role") if form.get("role") in ROLES else "member",
             job_title=form.get("job_title", "").strip(), avatar_color=random.choice(PALETTE),
             must_change_password=True)
    u.set_password(password)
    db.session.add(u)
    db.session.flush()
    from ..models import Project
    for pid in request.form.getlist("project_ids"):
        p = db.session.get(Project, to_int(pid) or 0)
        if p:
            db.session.add(ProjectMember(project=p, user=u, role="guest" if u.role == "guest" else "member"))
    db.session.commit()
    flash(f"Compte créé pour {u.display_name}. Identifiant : {u.username} — mot de passe provisoire : {temp} "
          "(à changer à la première connexion).", "success")
    return redirect(url_for("team.index"))


@bp.route("/<int:uid>/admin", methods=["POST"])
@login_required
def admin_action(uid):
    require_admin()
    u = db.session.get(User, uid) or abort(404)
    action = request.form.get("action")
    admins = User.query.filter_by(role="admin", active=True).count()
    if action == "update":
        role = request.form.get("role")
        if role in ROLES:
            if u.role == "admin" and role != "admin" and admins <= 1:
                flash("Il doit rester au moins un administrateur.", "error")
                return redirect(back_url(url_for("team.index")))
            u.role = role
            if role == "guest":
                for m in u.memberships:
                    m.role = "guest"
        u.job_title = request.form.get("job_title", u.job_title)
        u.capacity_hours = to_int(request.form.get("capacity_hours")) or u.capacity_hours
        flash(f"Profil de {u.display_name} mis à jour.", "success")
    elif action == "toggle":
        if u.id == current_user.id:
            flash("Tu ne peux pas désactiver ton propre compte.", "error")
        else:
            u.active = not u.active
            flash(f"Compte {'réactivé' if u.active else 'désactivé'}.", "info")
    elif action == "reset":
        temp = secrets.token_urlsafe(6)
        u.set_password(temp)
        u.must_change_password = True
        flash(f"Nouveau mot de passe provisoire pour {u.username} : {temp}", "success")
    elif action == "delete":
        if u.id == current_user.id:
            flash("Tu ne peux pas supprimer ton propre compte.", "error")
        else:
            name = u.display_name
            db.session.delete(u)
            flash(f"Compte de {name} supprimé.", "info")
            db.session.commit()
            return redirect(url_for("team.index"))
    db.session.commit()
    return redirect(back_url(url_for("team.index")))

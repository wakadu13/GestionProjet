"""Intake : boîte de réception des demandes à trier (accepter, refuser, reporter, doublon)."""
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..models import Issue, now
from ..services import log_activity, notify, to_int
from ..utils import can, get_project, parse_date

bp = Blueprint("intake", __name__, url_prefix="/projects/<int:pid>/intake")


@bp.route("/")
@login_required
def index(pid):
    project = get_project(pid)
    if not project.intake_enabled:
        flash("L’intake est désactivé pour ce projet.", "info")
        return redirect(url_for("issues.index", pid=pid))
    tab = request.args.get("tab", "open")
    q = Issue.query.filter(Issue.project_id == pid, Issue.intake_status.isnot(None), Issue.archived_at.is_(None))
    if tab == "open":
        q = q.filter(Issue.intake_status.in_(("pending", "snoozed")))
    else:
        q = q.filter(Issue.intake_status.in_(("accepted", "declined", "duplicate")))
    items = q.order_by(Issue.created_at.desc()).all()
    # Les demandes reportées dont la date est passée redeviennent en attente
    for i in items:
        if i.intake_status == "snoozed" and i.intake_snoozed_until and i.intake_snoozed_until <= __import__("datetime").date.today():
            i.intake_status = "pending"
    db.session.commit()
    if not can(current_user, "edit", project):
        items = [i for i in items if i.created_by_id == current_user.id]
    selected = None
    sel_id = to_int(request.args.get("issue"))
    if sel_id:
        selected = next((i for i in items if i.id == sel_id), None)
    if selected is None and items:
        selected = items[0]
    others = Issue.query.filter(Issue.project_id == pid, Issue.is_visible_clause(),
                                Issue.archived_at.is_(None)).order_by(Issue.sequence_id.desc()).limit(300).all()
    return render_template("intake/index.html", project=project, items=items, selected=selected, tab=tab,
                           others=others, can_triage=can(current_user, "edit", project),
                           can_submit=can(current_user, "comment", project))


@bp.route("/<int:iid>/<action>", methods=["POST"])
@login_required
def triage(pid, iid, action):
    project = get_project(pid, "edit")
    issue = db.session.get(Issue, iid)
    if issue is None or issue.project_id != project.id or issue.intake_status is None:
        abort(404)
    if action == "accept":
        issue.intake_status = "accepted"
        st = project.default_state()
        if st:
            issue.state = st
        log_activity(issue, current_user, "intake_accepted")
        notify([issue.created_by], current_user, issue, "intake",
               f"{current_user.display_name} a accepté ta demande {issue.key} · {issue.title}")
        flash(f"Demande acceptée : elle devient le ticket {issue.key}.", "success")
    elif action == "decline":
        issue.intake_status = "declined"
        cancelled = project.first_state_of("cancelled")
        if cancelled:
            issue.state = cancelled
        log_activity(issue, current_user, "intake_declined")
        notify([issue.created_by], current_user, issue, "intake",
               f"{current_user.display_name} a refusé ta demande « {issue.title} »")
        flash("Demande refusée.", "info")
    elif action == "snooze":
        until = parse_date(request.form.get("until"))
        if not until:
            flash("Choisis une date de report.", "error")
        else:
            issue.intake_status = "snoozed"
            issue.intake_snoozed_until = until
            log_activity(issue, current_user, "intake_snoozed", new=until.strftime("%d/%m/%Y"))
            flash("Demande reportée.", "info")
    elif action == "duplicate":
        other = db.session.get(Issue, to_int(request.form.get("duplicate_of")) or 0)
        if not other or other.project_id != project.id:
            flash("Choisis le ticket d’origine.", "error")
        else:
            issue.intake_status = "duplicate"
            issue.intake_duplicate_of = other
            log_activity(issue, current_user, "intake_duplicate", new=other.key)
            flash(f"Demande marquée comme doublon de {other.key}.", "info")
    elif action == "reopen":
        issue.intake_status = "pending"
        flash("Demande remise en attente.", "info")
    else:
        abort(404)
    issue.updated_at = now()
    db.session.commit()
    return redirect(url_for("intake.index", pid=pid, issue=issue.id))

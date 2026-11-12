"""Accueil, tickets de l'espace, notifications, recherche, favoris, pense-bêtes."""
from datetime import date, timedelta

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from .. import digest
from .. import issue_query as IQ
from ..extensions import db
from ..models import (
    Activity, Comment, Cycle, Favorite, Issue, Module, Notification, Page, Sticky,
    User, blockers_map, now,
)
from ..securite import clean_color
from ..utils import back_url, contient, contient_plie, visible_projects, week_bounds

bp = Blueprint("home", __name__)


@bp.route("/home")
@login_required
def index():
    """Accueil de la gestion de projet (le tableau de bord de l'agenda est à la racine « / »)."""
    me = current_user
    projects = visible_projects(me)
    pids = [p.id for p in projects]
    today = date.today()
    ws, we = week_bounds(today)
    mine = (IQ.base_query().filter(Issue.project_id.in_(pids), Issue.assignees.any(User.id == me.id)).all()
            if pids else [])
    open_mine = [i for i in mine if not i.is_closed]
    overdue = digest.tickets_a_echeance(me, today, issues=mine)["retard"]  # même règle que l'agenda et le résumé
    upcoming = sorted([i for i in open_mine if i.target_date and i.target_date >= today],
                      key=lambda i: i.target_date)[:12]
    in_progress = [i for i in open_mine if i.state_group == "started"]
    due_week = [i for i in open_mine if i.target_date and ws <= i.target_date <= we]
    done_week = [i for i in mine if i.completed_at and i.completed_at.date() >= ws]
    created_by_me = (IQ.base_query().filter(Issue.project_id.in_(pids), Issue.created_by_id == me.id)
                     .order_by(Issue.created_at.desc()).limit(8).all() if pids else [])
    activity = (Activity.query.filter(Activity.project_id.in_(pids))
                .order_by(Activity.created_at.desc()).limit(25).all() if pids else [])
    current_cycles = [c for p in projects if p.cycles_enabled for c in p.cycles
                      if c.archived_at is None and c.status == "current"]
    my_projects = [p for p in projects if p.member(me)]
    stickies = Sticky.query.filter_by(user_id=me.id).order_by(Sticky.updated_at.desc()).all()
    recent_pages = (Page.query.filter(Page.project_id.in_(pids), Page.archived_at.is_(None),
                                      db.or_(Page.is_public.is_(True), Page.owner_id == me.id))
                    .order_by(Page.updated_at.desc()).limit(5).all() if pids else [])
    return render_template(
        "home/index.html", overdue=overdue, upcoming=upcoming, in_progress=in_progress, done_week=done_week,
        due_week=due_week,
        open_mine=open_mine, created_by_me=created_by_me, activity=activity, current_cycles=current_cycles,
        my_projects=my_projects, stickies=stickies, recent_pages=recent_pages,
    )


@bp.route("/issues")
@login_required
def workspace_issues():
    """Tickets de tous les projets (vues : assignés, créés, abonnés, tous)."""
    me = current_user
    scope = request.args.get("scope", "assigned")
    projects = visible_projects(me)
    pids = [p.id for p in projects]
    f = IQ.parse_filters(request.args)
    opts = IQ.display_options(request.args, f"ws:{scope}", default_layout="list")
    if opts["group_by"] == "state":
        opts["group_by"] = "state_group"
    q = IQ.base_query().filter(Issue.project_id.in_(pids or [0]))
    if scope == "assigned":
        q = q.filter(Issue.assignees.any(User.id == me.id))
    elif scope == "created":
        q = q.filter(Issue.created_by_id == me.id)
    elif scope == "subscribed":
        f.setdefault("subscribed", ["1"])
    q = IQ.apply_filters(q, f, me)
    issues = IQ.fetch(q, opts)
    in_scope = projects
    if "project" in f:
        in_scope = [p for p in projects if str(p.id) in f["project"]] or projects
    ctx = _layout_context(issues, opts, in_scope, f)
    return render_template("issues/workspace.html", scope=scope, projects=projects, **ctx)


def _layout_context(issues, opts, projects, f):
    ctx = {"issues": issues, "opts": opts, "filters": f, "scope_projects": projects,
           "chips": IQ.filter_chips(f, projects, current_user)}
    if opts["layout"] in ("list", "board"):
        ctx["groups"] = IQ.group_issues(issues, opts["group_by"], projects,
                                        show_empty=opts["layout"] == "board" or len(projects) == 1)
        if opts["layout"] == "board":
            # Cadenas des cartes bloquées : une requête pour tout le tableau (et non une par carte).
            ctx["blockers_map"] = blockers_map(i.id for i in issues)
    elif opts["layout"] == "calendar":
        ctx["cal"] = IQ.calendar_weeks(opts["month"], issues)
    elif opts["layout"] == "gantt":
        ctx["gantt"] = IQ.gantt_data(issues, opts["zoom"])
    return ctx


@bp.route("/notifications")
@login_required
def notifications():
    tab = request.args.get("tab", "all")
    q = Notification.query.filter(Notification.user_id == current_user.id)
    if tab == "archived":
        q = q.filter(Notification.archived_at.isnot(None))
    else:
        q = q.filter(Notification.archived_at.is_(None))
        q = q.filter(db.or_(Notification.snoozed_until.is_(None), Notification.snoozed_until <= now()))
        if tab == "unread":
            q = q.filter(Notification.read_at.is_(None))
        elif tab == "mentions":
            q = q.filter(Notification.kind == "mention")
        elif tab == "assigned":
            q = q.filter(Notification.kind == "assigned")
    notes = q.order_by(Notification.created_at.desc()).limit(300).all()
    snoozed = Notification.query.filter(Notification.user_id == current_user.id,
                                        Notification.archived_at.is_(None),
                                        Notification.snoozed_until > now()).count()
    return render_template("home/notifications.html", notes=notes, tab=tab, snoozed=snoozed)


def _own_notification(nid):
    n = db.session.get(Notification, nid)
    if not n or n.user_id != current_user.id:
        abort(404)
    return n


@bp.route("/notifications/<int:nid>/open")
@login_required
def notification_open(nid):
    n = _own_notification(nid)
    n.read_at = n.read_at or now()
    db.session.commit()
    return redirect(n.url or url_for("home.notifications"))


@bp.route("/notifications/<int:nid>/<action>", methods=["POST"])
@login_required
def notification_action(nid, action):
    n = _own_notification(nid)
    if action == "read":
        n.read_at = None if n.read_at else now()
    elif action == "archive":
        n.archived_at = None if n.archived_at else now()
        n.read_at = n.read_at or now()
    elif action == "snooze":
        days = int(request.form.get("days", 1))
        n.snoozed_until = now() + timedelta(days=days)
    elif action == "delete":
        db.session.delete(n)
    db.session.commit()
    if request.headers.get("X-Requested-With") == "fetch":
        return {"ok": True}
    return redirect(back_url(url_for("home.notifications")))


@bp.route("/notifications/read-all", methods=["POST"])
@login_required
def notifications_read_all():
    Notification.query.filter(Notification.user_id == current_user.id,
                              Notification.read_at.is_(None)).update({"read_at": now()})
    db.session.commit()
    flash("Toutes les notifications sont marquées comme lues.", "success")
    return redirect(back_url(url_for("home.notifications")))


@bp.route("/search")
@login_required
def search():
    text = (request.args.get("q") or "").strip()
    results = search_everything(text, limit=50) if text else None
    return render_template("home/search.html", q=text, results=results)


def search_everything(text: str, limit=8):
    me = current_user
    projects = visible_projects(me)
    pids = [p.id for p in projects] or [0]
    f = {"q": text}
    issues = IQ.apply_filters(IQ.base_query().filter(Issue.project_id.in_(pids)), f, me) \
        .order_by(Issue.updated_at.desc()).limit(limit).all()
    return {
        "issues": issues,
        "projects": [p for p in projects if contient_plie(text, p.name) or contient_plie(text, p.identifier)][:limit],
        "cycles": Cycle.query.filter(Cycle.project_id.in_(pids), contient(Cycle.name, text)).limit(limit).all(),
        "modules": Module.query.filter(Module.project_id.in_(pids), contient(Module.name, text)).limit(limit).all(),
        "pages": Page.query.filter(Page.project_id.in_(pids), Page.archived_at.is_(None),
                                   db.or_(Page.is_public.is_(True), Page.owner_id == me.id),
                                   db.or_(contient(Page.title, text), contient(Page.content, text))).limit(limit).all(),
        "members": User.query.filter(User.active.is_(True), db.or_(
            contient(db.func.coalesce(User.first_name, "") + " " + db.func.coalesce(User.last_name, ""), text),
            contient(User.username, text))).limit(limit).all(),
        "comments": Comment.query.join(Issue).filter(Issue.project_id.in_(pids), contient(Comment.body, text))
        .order_by(Comment.created_at.desc()).limit(limit).all(),
    }


@bp.route("/favorite/<etype>/<int:eid>", methods=["POST"])
@login_required
def toggle_favorite(etype, eid):
    if etype not in ("project", "cycle", "module", "view", "page"):
        abort(400)
    f = Favorite.query.filter_by(user_id=current_user.id, entity_type=etype, entity_id=eid).first()
    if f:
        db.session.delete(f)  # retirer un favori reste toujours possible, même devenu invisible
        state = False
    else:
        from ..utils import favorite_target
        if favorite_target(current_user, etype, eid) is None:
            abort(404)  # objet inexistant ou que tu n'as pas le droit de voir
        db.session.add(Favorite(user_id=current_user.id, entity_type=etype, entity_id=eid))
        state = True
    db.session.commit()
    if request.headers.get("X-Requested-With") == "fetch":
        return {"ok": True, "favorite": state}
    return redirect(back_url(url_for("home.index")))


@bp.route("/stickies", methods=["POST"])
@login_required
def sticky_create():
    s = Sticky(user_id=current_user.id, content=request.form.get("content", ""),
               color=clean_color(request.form.get("color"), "#fef3c7"))
    db.session.add(s)
    db.session.commit()
    return redirect(url_for("home.index") + "#stickies")


@bp.route("/stickies/<int:sid>", methods=["POST"])
@login_required
def sticky_update(sid):
    s = db.session.get(Sticky, sid)
    if not s or s.user_id != current_user.id:
        abort(404)
    if request.form.get("delete"):
        db.session.delete(s)
    else:
        if "content" in request.form:
            s.content = request.form["content"]
        if "color" in request.form:
            s.color = clean_color(request.form["color"], s.color)
        s.updated_at = now()
    db.session.commit()
    if request.headers.get("X-Requested-With") == "fetch":
        return {"ok": True}
    return redirect(url_for("home.index") + "#stickies")


@bp.route("/shortcuts")
@login_required
def shortcuts():
    return render_template("home/shortcuts.html")

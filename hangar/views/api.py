"""API interne (JSON) utilisée par l'interface : édition en ligne, glisser-déposer, recherche."""
from flask import Blueprint, abort, current_app, jsonify, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..models import Comment, Issue, Notification, Reaction
from ..services import create_issue, move_issue, serialize_issue, to_int, update_issue
from ..utils import can, excerpt, render_markdown, visible_projects
from .home import search_everything

bp = Blueprint("api", __name__, url_prefix="/api")

PROPS = ("state", "priority", "assignees", "labels", "cycle", "modules", "estimate")


def render_props(issue: Issue) -> dict:
    tpl = current_app.jinja_env.get_template("issues/_props.html")
    mod = tpl.make_module({"current_user": current_user})
    return {k: str(getattr(mod, f"prop_{k}")(issue)) for k in PROPS}


def _issue(iid, need="edit") -> Issue:
    issue = db.session.get(Issue, iid)
    if issue is None:
        abort(404)
    if not can(current_user, need, issue.project):
        abort(403)
    return issue


@bp.route("/issues/<int:iid>", methods=["GET"])
@login_required
def issue_get(iid):
    issue = _issue(iid, "view")
    return jsonify(ok=True, issue=serialize_issue(issue))


@bp.route("/issues/<int:iid>", methods=["PATCH", "POST"])
@login_required
def issue_update(iid):
    issue = _issue(iid)
    data = request.get_json(silent=True) or {}
    allowed = {"title", "description", "state_id", "priority", "assignee_ids", "label_ids", "module_ids",
               "start_date", "target_date", "estimate", "cycle_id", "parent_id"}
    changed = update_issue(issue, {k: v for k, v in data.items() if k in allowed}, current_user)
    db.session.commit()
    return jsonify(ok=True, changed=changed, props=render_props(issue), issue=serialize_issue(issue))


@bp.route("/issues/<int:iid>/move", methods=["POST"])
@login_required
def issue_move(iid):
    issue = _issue(iid)
    d = request.get_json(silent=True) or {}
    changed = move_issue(issue, d.get("group_by"), d.get("from"), d.get("to"), d.get("prev"), d.get("next"),
                         current_user)
    db.session.commit()
    return jsonify(ok=True, changed=changed, props=render_props(issue), issue=serialize_issue(issue))


@bp.route("/issues/quick", methods=["POST"])
@login_required
def issue_quick():
    d = request.get_json(silent=True) or {}
    from ..models import Project
    project = db.session.get(Project, to_int(d.get("project_id")) or 0)
    if project is None or not can(current_user, "edit", project):
        abort(403)
    title = (d.get("title") or "").strip()
    if not title:
        return jsonify(ok=False, error="Titre manquant"), 400
    data = {"title": title}
    gb, key = d.get("group_by"), d.get("group_key")
    if key not in (None, "", "none", "all"):
        if gb == "state":
            data["state_id"] = key
        elif gb == "state_group":
            st = project.first_state_of(key)
            if st:
                data["state_id"] = st.id
        elif gb == "priority":
            data["priority"] = key
        elif gb == "assignee":
            data["assignee_ids"] = [key]
        elif gb == "label":
            data["label_ids"] = [key]
        elif gb == "cycle":
            data["cycle_id"] = key
        elif gb == "module":
            data["module_ids"] = [key]
    for k in ("cycle_id", "module_ids", "target_date", "parent_id"):
        if d.get(k):
            data[k] = d[k]
    issue = create_issue(project, data, current_user)
    db.session.commit()
    return jsonify(ok=True, key=issue.key, url=url_for("issues.detail", pid=project.id, seq=issue.sequence_id))


@bp.route("/reactions", methods=["POST"])
@login_required
def reaction_toggle():
    d = request.get_json(silent=True) or {}
    emoji = (d.get("emoji") or "")[:16]
    if not emoji:
        abort(400)
    if d.get("comment_id"):
        c = db.session.get(Comment, to_int(d["comment_id"]) or 0) or abort(404)
        if not can(current_user, "comment", c.issue.project):
            abort(403)
        existing = Reaction.query.filter_by(comment_id=c.id, user_id=current_user.id, emoji=emoji).first()
        target = {"comment_id": c.id}
    else:
        i = _issue(to_int(d.get("issue_id")) or 0, "comment")
        existing = Reaction.query.filter_by(issue_id=i.id, user_id=current_user.id, emoji=emoji).first()
        target = {"issue_id": i.id}
    if existing:
        db.session.delete(existing)
    else:
        db.session.add(Reaction(user_id=current_user.id, emoji=emoji, **target))
    db.session.commit()
    return jsonify(ok=True)


@bp.route("/search")
@login_required
def search():
    text = (request.args.get("q") or "").strip()
    if not text:
        recent = (Issue.query.filter(Issue.project_id.in_([p.id for p in visible_projects(current_user)] or [0]),
                                     Issue.archived_at.is_(None), Issue.is_visible_clause())
                  .order_by(Issue.updated_at.desc()).limit(6).all())
        return jsonify(ok=True, issues=[_issue_hit(i) for i in recent], projects=[], pages=[], members=[],
                       cycles=[], modules=[])
    r = search_everything(text, limit=6)
    return jsonify(
        ok=True,
        issues=[_issue_hit(i) for i in r["issues"]],
        projects=[{"title": p.name, "sub": p.identifier, "emoji": p.emoji, "url": url_for("issues.index", pid=p.id)}
                  for p in r["projects"]],
        pages=[{"title": p.title, "sub": p.project.name, "emoji": p.emoji,
                "url": url_for("pages.view", pid=p.project_id, page_id=p.id)} for p in r["pages"]],
        members=[{"title": u.display_name, "sub": "@" + u.username, "initials": u.initials,
                  "color": u.avatar_color, "url": url_for("team.profile", username=u.username)}
                 for u in r["members"]],
        cycles=[{"title": c.name, "sub": c.project.name, "url": url_for("cycles.detail", pid=c.project_id, cid=c.id)}
                for c in r["cycles"]],
        modules=[{"title": m.name, "sub": m.project.name,
                  "url": url_for("modules.detail", pid=m.project_id, mid=m.id)} for m in r["modules"]],
    )


def _issue_hit(i: Issue):
    return {"id": i.id, "title": i.title, "key": i.key, "sub": i.project.name,
            "state_color": i.state.color if i.state else "#a3a3a3", "state_group": i.state_group,
            "url": url_for("issues.detail", pid=i.project_id, seq=i.sequence_id)}


@bp.route("/issues/lookup")
@login_required
def lookup():
    pid = to_int(request.args.get("project"))
    text = (request.args.get("q") or "").strip()
    exclude = to_int(request.args.get("exclude"))
    pids = [p.id for p in visible_projects(current_user)]
    q = Issue.query.filter(Issue.archived_at.is_(None), Issue.is_visible_clause(), Issue.project_id.in_(pids or [0]))
    if pid:
        q = q.filter(Issue.project_id == pid)
    if text:
        if text.isdigit():
            q = q.filter(db.or_(Issue.sequence_id == int(text), Issue.title.ilike(f"%{text}%")))
        elif "-" in text and text.rsplit("-", 1)[1].isdigit():
            q = q.filter(Issue.sequence_id == int(text.rsplit("-", 1)[1]))
        else:
            q = q.filter(Issue.title.ilike(f"%{text}%"))
    if exclude:
        q = q.filter(Issue.id != exclude)
    return jsonify(ok=True, issues=[_issue_hit(i) for i in q.order_by(Issue.updated_at.desc()).limit(15)])


@bp.route("/markdown", methods=["POST"])
@login_required
def markdown_preview():
    d = request.get_json(silent=True) or {}
    return jsonify(ok=True, html=str(render_markdown(d.get("text", ""))))


@bp.route("/me/theme", methods=["POST"])
@login_required
def theme():
    d = request.get_json(silent=True) or {}
    if d.get("theme") in ("system", "light", "dark"):
        current_user.theme = d["theme"]
        db.session.commit()
    return jsonify(ok=True, theme=current_user.theme)


@bp.route("/notifications/summary")
@login_required
def notifications_summary():
    q = Notification.query.filter(Notification.user_id == current_user.id, Notification.archived_at.is_(None),
                                  Notification.read_at.is_(None))
    latest = q.order_by(Notification.created_at.desc()).first()
    return jsonify(ok=True, unread=q.count(),
                   latest={"id": latest.id, "message": excerpt(latest.message, 120)} if latest else None)


@bp.route("/members")
@login_required
def members():
    from ..models import User
    users = User.query.filter_by(active=True).order_by(User.first_name, User.last_name).all()
    return jsonify(ok=True, members=[{"username": u.username, "name": u.display_name, "initials": u.initials,
                                      "color": u.avatar_color} for u in users])

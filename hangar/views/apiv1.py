"""API REST publique (v1) authentifiée par jeton : intégration avec d'autres outils/scripts.

En-tête : Authorization: Bearer hgr_xxx   (ou X-API-Key: hgr_xxx)
"""
from functools import wraps

from flask import Blueprint, abort, g, jsonify, request

from ..extensions import db
from ..models import APIToken, Comment, Issue, Project, now
from ..services import (create_issue, delete_issue, description_conflict, description_conflict_message,
                        log_activity, serialize_issue, to_int, update_issue)
from ..utils import can, can_delete_issues, visible_projects

bp = Blueprint("apiv1", __name__, url_prefix="/api/v1")


def token_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        raw = request.headers.get("X-API-Key") or ""
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            raw = auth[7:].strip()
        if not raw:
            return jsonify(ok=False, error="Jeton manquant"), 401
        t = APIToken.query.filter_by(token_hash=APIToken.hash(raw)).first()
        if t is None or (t.expires_at and t.expires_at < now()) or not t.user.active:
            return jsonify(ok=False, error="Jeton invalide ou expiré"), 401
        t.last_used_at = now()
        db.session.commit()
        g.api_user = t.user
        return fn(*a, **kw)
    return wrapper


def _project(pid, need="view"):
    p = db.session.get(Project, pid)
    if p is None:
        abort(404)
    if not can(g.api_user, need, p):
        abort(403)
    return p


@bp.route("/")
def root():
    return jsonify(ok=True, name="Hangar API", version=1, endpoints=[
        "GET /api/v1/me", "GET /api/v1/projects", "GET /api/v1/projects/<id>",
        "GET /api/v1/projects/<id>/issues", "POST /api/v1/projects/<id>/issues",
        "GET|PATCH|DELETE /api/v1/issues/<id>", "POST /api/v1/issues/<id>/comments",
    ])


@bp.route("/me")
@token_required
def me():
    u = g.api_user
    return jsonify(ok=True, user={"id": u.id, "username": u.username, "name": u.display_name,
                                  "role": u.role})


@bp.route("/projects")
@token_required
def projects():
    return jsonify(ok=True, projects=[_project_json(p) for p in visible_projects(g.api_user)])


def _project_json(p):
    return {
        "id": p.id, "name": p.name, "identifier": p.identifier, "description": p.description,
        "states": [{"id": s.id, "name": s.name, "group": s.group, "color": s.color} for s in p.states],
        "labels": [{"id": l.id, "name": l.name, "color": l.color} for l in p.labels],
        "members": [{"id": u.id, "username": u.username, "name": u.display_name} for u in p.member_users],
        "cycles": [{"id": c.id, "name": c.name, "start": c.start_date.isoformat() if c.start_date else None,
                    "end": c.end_date.isoformat() if c.end_date else None, "status": c.status} for c in p.cycles],
        "modules": [{"id": m.id, "name": m.name, "status": m.status} for m in p.modules],
    }


@bp.route("/projects/<int:pid>")
@token_required
def project(pid):
    return jsonify(ok=True, project=_project_json(_project(pid)))


@bp.route("/projects/<int:pid>/issues", methods=["GET", "POST"])
@token_required
def issues(pid):
    p = _project(pid, "edit" if request.method == "POST" else "view")
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        if not (data.get("title") or "").strip():
            return jsonify(ok=False, error="title requis"), 400
        i = create_issue(p, data, g.api_user)
        db.session.commit()
        return jsonify(ok=True, issue=serialize_issue(i)), 201
    q = Issue.query.filter(Issue.project_id == pid, Issue.archived_at.is_(None), Issue.is_visible_clause())
    if request.args.get("state_group"):
        from ..models import State
        q = q.filter(Issue.state_id.in_(db.select(State.id).where(State.group == request.args["state_group"])))
    if request.args.get("assignee"):
        from ..models import User
        q = q.filter(Issue.assignees.any(User.username == request.args["assignee"]))
    limit = min(to_int(request.args.get("limit")) or 200, 1000)
    return jsonify(ok=True, issues=[serialize_issue(i) for i in q.order_by(Issue.sequence_id).limit(limit)])


def _issue(iid, need="view"):
    i = db.session.get(Issue, iid)
    if i is None:
        abort(404)
    _project(i.project_id, need)
    return i


@bp.route("/issues/<int:iid>", methods=["GET", "PATCH", "DELETE"])
@token_required
def issue(iid):
    if request.method == "GET":
        return jsonify(ok=True, issue=serialize_issue(_issue(iid)))
    i = _issue(iid, "edit")
    if request.method == "DELETE":
        if not can_delete_issues(g.api_user, i.project):
            return jsonify(ok=False, error="Suppression réservée à l’administrateur et au responsable du projet "
                                          "(archive le ticket à la place)."), 403
        delete_issue(i)
        db.session.commit()
        return jsonify(ok=True)
    data = request.get_json(silent=True) or {}
    if description_conflict(i, data):  # « description_version » : empreinte de la description lue avant
        return jsonify(ok=False, conflict=True, error=description_conflict_message(i, g.api_user),
                       issue=serialize_issue(i)), 409
    changed = update_issue(i, data, g.api_user)
    db.session.commit()
    return jsonify(ok=True, changed=changed, issue=serialize_issue(i))


@bp.route("/issues/<int:iid>/comments", methods=["GET", "POST"])
@token_required
def comments(iid):
    i = _issue(iid, "comment" if request.method == "POST" else "view")
    if request.method == "POST":
        body = ((request.get_json(silent=True) or {}).get("body") or "").strip()
        if not body:
            return jsonify(ok=False, error="body requis"), 400
        c = Comment(issue=i, author=g.api_user, body=body)
        db.session.add(c)
        log_activity(i, g.api_user, "commented", new=body[:200])
        db.session.commit()
    return jsonify(ok=True, comments=[{"id": c.id, "author": c.author.username if c.author else None,
                                       "body": c.body, "created_at": c.created_at.isoformat()}
                                      for c in i.comments])

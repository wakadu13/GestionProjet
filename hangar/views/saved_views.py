"""Vues enregistrées : combinaisons de filtres et d'affichage réutilisables."""
from urllib.parse import parse_qsl, urlencode

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from .. import issue_query as IQ
from ..extensions import db
from ..models import SavedView
from ..utils import back_url, can, get_project, visible_projects

bp = Blueprint("saved_views", __name__)

KEEP = set(IQ.FILTER_KEYS) | {"q", "layout", "group_by", "order_by", "sub_issues", "show_closed", "scope"}


def _clean_query(raw: str) -> str:
    raw = raw.lstrip("?")
    pairs = [(k, v) for k, v in parse_qsl(raw, keep_blank_values=False) if k in KEEP]
    return urlencode(pairs)


def _visible(v: SavedView) -> bool:
    return v.is_public or v.owner_id == current_user.id


@bp.route("/projects/<int:pid>/views")
@login_required
def index(pid):
    project = get_project(pid)
    views = [v for v in db.session.query(SavedView).filter_by(project_id=pid).order_by(SavedView.name) if _visible(v)]
    return render_template("views/index.html", project=project, views=views, count_for=count_for,
                           can_edit=can(current_user, "edit", project))


@bp.route("/views")
@login_required
def global_index():
    views = [v for v in db.session.query(SavedView).filter(SavedView.project_id.is_(None)).order_by(SavedView.name)
             if _visible(v)]
    return render_template("views/index.html", project=None, views=views, count_for=count_for,
                           can_edit=not current_user.is_guest)


@bp.route("/views/save", methods=["POST"])
@login_required
def save():
    pid = request.form.get("project_id")
    project = get_project(int(pid), "edit") if pid else None
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Donne un nom à la vue.", "error")
        return redirect(back_url(url_for("home.index")))
    vid = request.form.get("view_id")
    v = db.session.get(SavedView, int(vid)) if vid else None
    if v is None:
        v = SavedView(project=project, owner=current_user)
        db.session.add(v)
    elif v.owner_id != current_user.id and not current_user.is_admin:
        abort(403)
    v.name = name[:160]
    v.description = (request.form.get("description") or "")[:500]
    if "query" in request.form:
        v.query = _clean_query(request.form.get("query", ""))
    v.is_public = request.form.get("is_public", "1") == "1"
    db.session.commit()
    flash(f"Vue « {v.name} » enregistrée.", "success")
    return redirect(url_for("saved_views.open", pid=project.id, vid=v.id) if project
                    else url_for("saved_views.open_global", vid=v.id))


@bp.route("/projects/<int:pid>/views/<int:vid>")
@login_required
def open(pid, vid):
    get_project(pid)
    v = db.session.get(SavedView, vid)
    if v is None or v.project_id != pid or not _visible(v):
        abort(404)
    q = v.query + ("&" if v.query else "") + f"view={v.id}"
    return redirect(url_for("issues.index", pid=pid) + "?" + q)


@bp.route("/views/<int:vid>")
@login_required
def open_global(vid):
    v = db.session.get(SavedView, vid)
    if v is None or v.project_id is not None or not _visible(v):
        abort(404)
    q = v.query
    if "scope=" not in q:
        q = (q + "&" if q else "") + "scope=all"
    return redirect(url_for("home.workspace_issues") + "?" + q + f"&view={v.id}")


@bp.route("/views/<int:vid>/delete", methods=["POST"])
@login_required
def delete(vid):
    v = db.session.get(SavedView, vid) or abort(404)
    if v.owner_id != current_user.id and not current_user.is_admin:
        abort(403)
    pid = v.project_id
    db.session.delete(v)
    db.session.commit()
    flash("Vue supprimée.", "info")
    return redirect(url_for("saved_views.index", pid=pid) if pid else url_for("saved_views.global_index"))


def count_for(v: SavedView) -> int:
    """Nombre de tickets correspondant à une vue (affiché sur les cartes)."""
    from werkzeug.datastructures import MultiDict
    args = MultiDict(parse_qsl(v.query))
    f = IQ.parse_filters(args)
    q = IQ.base_query()
    if v.project_id:
        q = q.filter_by(project_id=v.project_id)
    else:
        q = q.filter(IQ.Issue.project_id.in_([p.id for p in visible_projects(current_user)] or [0]))
    return IQ.apply_filters(q, f, current_user).count()

"""Modules : regroupements fonctionnels de tickets (lots, phases, fonctionnalités)."""
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from .. import charts
from .. import issue_query as IQ
from ..extensions import db
from ..models import MODULE_STATUSES, STATE_GROUPS, Issue, Module, User, now
from ..services import to_int, update_issue
from ..utils import can, get_project, parse_date
from .home import _layout_context

bp = Blueprint("modules", __name__, url_prefix="/projects/<int:pid>/modules")


def _module(pid, mid, need="view"):
    project = get_project(pid, need)
    if not project.modules_enabled:
        abort(404)
    m = db.session.get(Module, mid)
    if m is None or m.project_id != project.id:
        abort(404)
    return project, m


@bp.route("/")
@login_required
def index(pid):
    project = get_project(pid)
    if not project.modules_enabled:
        flash("Les modules sont désactivés pour ce projet.", "info")
        return redirect(url_for("issues.index", pid=pid))
    show_archived = request.args.get("archived") == "1"
    status = request.args.get("status")
    layout = request.args.get("layout", "grid")
    mods = [m for m in project.modules if (m.archived_at is not None) == show_archived]
    if status:
        mods = [m for m in mods if m.status == status]
    sort = request.args.get("sort", "name")
    if sort == "progress":
        mods.sort(key=lambda m: -m.progress()["pct"])
    elif sort == "target":
        from datetime import date
        mods.sort(key=lambda m: m.target_date or date(9999, 1, 1))
    return render_template("modules/index.html", project=project, modules=mods, show_archived=show_archived,
                           status=status, layout=layout, sort=sort, statuses=MODULE_STATUSES,
                           can_edit=can(current_user, "edit", project))


def _apply_form(m, form, project):
    m.name = (form.get("name") or m.name or "").strip()[:160]
    m.description = form.get("description", "")
    m.status = form.get("status") if form.get("status") in dict((k, 1) for k, _, _ in MODULE_STATUSES) else "planned"
    m.lead_id = to_int(form.get("lead_id"))
    m.start_date = parse_date(form.get("start_date"))
    m.target_date = parse_date(form.get("target_date"))
    member_ids = {u.id for u in project.member_users}
    m.members = [db.session.get(User, uid) for uid in
                 (to_int(x) for x in form.getlist("member_ids")) if uid in member_ids]


@bp.route("/new", methods=["POST"])
@login_required
def create(pid):
    project = get_project(pid, "edit")
    if not (request.form.get("name") or "").strip():
        flash("Le nom du module est obligatoire.", "error")
        return redirect(url_for("modules.index", pid=pid))
    m = Module(project=project)
    _apply_form(m, request.form, project)
    db.session.add(m)
    db.session.commit()
    flash(f"Module « {m.name} » créé.", "success")
    return redirect(url_for("modules.detail", pid=pid, mid=m.id))


@bp.route("/<int:mid>")
@login_required
def detail(pid, mid):
    project, module = _module(pid, mid)
    f = IQ.parse_filters(request.args)
    opts = IQ.display_options(request.args, f"m:{mid}")
    q = IQ.apply_filters(IQ.base_query().filter(Issue.modules.any(Module.id == mid)), f, current_user)
    issues = IQ.fetch(q, opts)
    ctx = _layout_context(issues, opts, [project], f)
    prog = module.progress()
    donut = charts.donut([(lab, prog["counts"][g], col) for g, lab, col in STATE_GROUPS], size=150, thickness=18,
                         center_value=f"{prog['pct']} %", center_label="terminé")
    by_assignee = {}
    for i in module.active_issues:
        for u in (i.assignees or [None]):
            key = u.display_name if u else "Non assigné"
            d = by_assignee.setdefault(key, {"user": u, "total": 0, "done": 0})
            d["total"] += 1
            d["done"] += 1 if i.state_group == "completed" else 0
    candidates = [i for i in IQ.base_query().filter(Issue.project_id == pid,
                                                    ~Issue.modules.any(Module.id == mid))
                  .order_by(Issue.sequence_id).all() if not i.is_closed]
    return render_template("modules/detail.html", project=project, module=module, prog=prog, donut=donut,
                           by_assignee=sorted(by_assignee.values(), key=lambda d: -d["total"]),
                           candidates=candidates, statuses=MODULE_STATUSES,
                           can_edit=can(current_user, "edit", project), **ctx)


@bp.route("/<int:mid>/edit", methods=["POST"])
@login_required
def edit(pid, mid):
    project, module = _module(pid, mid, "edit")
    if "status_only" in request.form:
        st = request.form.get("status")
        if st in dict((k, 1) for k, _, _ in MODULE_STATUSES):
            module.status = st
    else:
        _apply_form(module, request.form, project)
    db.session.commit()
    flash("Module mis à jour.", "success")
    return redirect(request.form.get("next") or url_for("modules.detail", pid=pid, mid=mid))


@bp.route("/<int:mid>/<action>", methods=["POST"])
@login_required
def action(pid, mid, action):
    project, module = _module(pid, mid, "edit")
    if action == "archive":
        module.archived_at = None if module.archived_at else now()
        db.session.commit()
        flash("Module archivé." if module.archived_at else "Module restauré.", "info")
        return redirect(url_for("modules.index", pid=pid))
    if action == "delete":
        name = module.name
        db.session.delete(module)
        db.session.commit()
        flash(f"Module « {name} » supprimé (les tickets sont conservés).", "info")
        return redirect(url_for("modules.index", pid=pid))
    if action == "add-issues":
        n = 0
        for iid in request.form.getlist("issue_ids"):
            i = db.session.get(Issue, to_int(iid) or 0)
            if i and i.project_id == project.id and module not in i.modules:
                update_issue(i, {"module_ids": [m.id for m in i.modules] + [module.id]}, current_user)
                n += 1
        db.session.commit()
        flash(f"{n} ticket(s) ajouté(s) au module.", "success")
        return redirect(url_for("modules.detail", pid=pid, mid=mid))
    abort(404)

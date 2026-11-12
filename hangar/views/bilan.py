"""Bilan : synthèse d'avancement sur une période, bilans enregistrés, exports Markdown / Word."""
import json

from datetime import date

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required

from ..bilan import MANUAL_SECTIONS, PERIODS, bilan_docx, bilan_markdown, compute_bilan, period_bounds, scope_projects
from ..extensions import db
from ..models import BilanReport, Project, now
from ..utils import can, slugify, visible_projects

bp = Blueprint("bilan", __name__)


def _live(pid=None):
    projects = scope_projects(current_user, pid, request.args.get("project"))
    kind = request.args.get("period", "since_start")
    start, end, label, kind = period_bounds(kind, projects, request.args.get("start"), request.args.get("end"))
    data = compute_bilan(projects, start, end, label)
    return projects, kind, start, end, data


def _covered_ids(r: BilanReport) -> set[int] | None:
    """Projets dont le bilan contient les données (``scope.projects`` du contenu figé), None si inconnus."""
    data = r.data
    scope = data.get("scope") if isinstance(data, dict) else None
    projects = scope.get("projects") if isinstance(scope, dict) else None
    if not isinstance(projects, list):
        return None
    try:
        ids = {int(p["id"]) for p in projects}
    except (KeyError, TypeError, ValueError):
        return None
    if r.project_id:
        ids.add(r.project_id)
    return ids


def can_read_report(r: BilanReport, user) -> bool:
    """Un bilan enregistré ne se lit que si l'on voit tous les projets qu'il couvre ; sinon (ou si on ne sait
    pas lesquels il couvre), il reste réservé à son auteur et aux administrateurs."""
    if r.project_id and (r.project is None or not can(user, "view", r.project)):
        return False
    if user.is_admin or (r.created_by_id is not None and r.created_by_id == user.id):
        return True
    ids = _covered_ids(r)
    if ids is None:
        return False
    projects = {p.id: p for p in Project.query.filter(Project.id.in_(ids or [0]))}
    return all(pid in projects and can(user, "view", projects[pid]) for pid in ids)


def _reports(pid):
    q = db.session.query(BilanReport)
    q = q.filter(BilanReport.project_id == pid) if pid else q.filter(BilanReport.project_id.is_(None))
    return [r for r in q.order_by(BilanReport.created_at.desc()).all() if can_read_report(r, current_user)]


def _render_live(pid=None):
    projects, kind, start, end, data = _live(pid)
    project = projects[0] if pid else None
    can_save = can(current_user, "edit", project) if project else not current_user.is_guest
    return render_template("bilan/index.html", project=project, data=data, kind=kind, start=start, end=end,
                           periods=PERIODS, reports=_reports(pid), can_save=can_save,
                           sections=MANUAL_SECTIONS, all_projects=visible_projects(current_user),
                           sel=request.args.get("project", ""))


@bp.route("/bilan")
@login_required
def index():
    return _render_live()


@bp.route("/projects/<int:pid>/bilan")
@login_required
def project(pid):
    return _render_live(pid)


def _filename(title):
    return (slugify(title)[:60] or "bilan") + f"_{date.today():%Y%m%d}"


def _export(data, manual, title, fmt):
    if fmt == "md":
        return Response(bilan_markdown(data, manual, title), mimetype="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename={_filename(title)}.md"})
    if fmt == "docx":
        return send_file(bilan_docx(data, manual, title), as_attachment=True, download_name=f"{_filename(title)}.docx",
                         mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    abort(404)


@bp.route("/bilan/export.<fmt>")
@login_required
def export_live(fmt):
    pid = request.args.get("pid", type=int)
    projects, kind, start, end, data = _live(pid)
    title = f"Bilan — {data['scope']['label']}"
    return _export(data, {}, title, fmt)


@bp.route("/bilan/save", methods=["POST"])
@login_required
def save():
    pid = request.form.get("pid", type=int)
    if pid:
        p = db.session.get(Project, pid)
        if p is None or not can(current_user, "edit", p):
            abort(403)
    elif current_user.is_guest:
        abort(403)
    projects = scope_projects(current_user, pid, request.form.get("project"))
    start, end, label, kind = period_bounds(request.form.get("period", "since_start"), projects,
                                            request.form.get("start"), request.form.get("end"))
    data = compute_bilan(projects, start, end, label)
    title = (request.form.get("title") or "").strip() or f"Bilan — {data['scope']['label']} — {label}"
    r = BilanReport(project_id=pid, title=title[:200], period_start=start, period_end=end,
                    data_json=json.dumps(data, ensure_ascii=False), created_by=current_user)
    for key, _ in MANUAL_SECTIONS:
        setattr(r, key, request.form.get(key, ""))
    db.session.add(r)
    db.session.commit()
    flash("Bilan enregistré : il reste consultable tel quel, même quand les tickets évoluent.", "success")
    return redirect(url_for("bilan.report", rid=r.id))


def _report(rid, need_edit=False):
    r = db.session.get(BilanReport, rid)
    if r is None:
        abort(404)
    if not can_read_report(r, current_user):
        abort(403)
    if need_edit and r.created_by_id != current_user.id and not current_user.is_admin:
        if not (r.project_id and can(current_user, "admin", r.project)):
            abort(403)
    return r


def _refresh_projects(r: BilanReport) -> list[Project]:
    """Projets à recalculer : ceux que le bilan couvrait déjà (et que la personne qui actualise voit encore),
    jamais les projets visibles par cette personne : un administrateur ferait sinon entrer ses projets privés
    dans le bilan d'un membre."""
    if r.project_id:
        return [r.project]
    ids = _covered_ids(r)
    if ids is None:  # contenu illisible : on se limite à ce que voyait l'auteur
        projects = visible_projects(r.created_by or current_user)
    else:
        projects = Project.query.filter(Project.id.in_(ids or [0])).order_by(Project.name).all()
    return [p for p in projects if can(current_user, "view", p)]


def _manual(r):
    return {k: getattr(r, k) for k, _ in MANUAL_SECTIONS}


@bp.route("/bilan/reports/<int:rid>", methods=["GET", "POST"])
@login_required
def report(rid):
    if request.method == "POST":
        r = _report(rid, need_edit=True)
        if request.form.get("delete"):
            pid = r.project_id
            db.session.delete(r)
            db.session.commit()
            flash("Bilan supprimé.", "info")
            return redirect(url_for("bilan.project", pid=pid) if pid else url_for("bilan.index"))
        r.title = (request.form.get("title") or r.title).strip()[:200]
        for key, _ in MANUAL_SECTIONS:
            if key in request.form:
                setattr(r, key, request.form.get(key, ""))
        if request.form.get("refresh"):
            projects = _refresh_projects(r)
            data = compute_bilan(projects, r.period_start, r.period_end, r.data.get("period", {}).get("label"))
            r.data_json = json.dumps(data, ensure_ascii=False)
        r.updated_at = now()
        db.session.commit()
        flash("Bilan mis à jour.", "success")
        return redirect(url_for("bilan.report", rid=r.id))
    r = _report(rid)
    return render_template("bilan/report.html", r=r, data=r.data, sections=MANUAL_SECTIONS, project=r.project,
                           can_edit=(r.created_by_id == current_user.id or current_user.is_admin
                                     or (r.project_id and can(current_user, "admin", r.project))))


@bp.route("/bilan/reports/<int:rid>/export.<fmt>")
@login_required
def export_report(rid, fmt):
    r = _report(rid)
    return _export(r.data, _manual(r), r.title, fmt)




@bp.route("/bilan/reports/<int:rid>/send", methods=["POST"])
@login_required
def send_report(rid):
    """Envoie un bilan enregistré par e-mail (file d'envoi : SMTP ou boîte d'envoi locale)."""
    from markupsafe import escape

    from .. import mailer
    from ..models import User
    from ..utils import render_markdown
    if current_user.is_guest:
        abort(403)
    r = _report(rid)
    cfg = mailer._cfg()
    addresses = []
    for uid in request.form.getlist("user_ids"):
        u = db.session.get(User, int(uid)) if uid.isdigit() else None
        if u and u.contact_email:
            addresses.append((u.contact_email, u))
    bad = []
    for a in mailer.split_addresses(request.form.get("others")):
        (addresses.append((a, None)) if mailer.valid_email(a) else bad.append(a))
    if not addresses:
        flash("Choisis au moins un destinataire avec une adresse e-mail valide.", "error")
        return redirect(url_for("bilan.report", rid=rid))
    note = (request.form.get("message") or "").strip()
    body = mailer.absolutize_links(str(render_markdown(bilan_markdown(r.data, _manual(r), r.title))), cfg)
    intro = f"<p>{escape(note)}</p>" if note else ""
    html = mailer.layout(r.title, intro + f'<div style="border-top:1px solid #e4e7ec;padding-top:6px">{body}</div>',
                         button=("Ouvrir le bilan dans Hangar", mailer.absolute(url_for("bilan.report", rid=r.id), cfg)),
                         cfg=cfg)
    seen = set()
    for addr, u in addresses:
        if addr.lower() in seen:
            continue
        seen.add(addr.lower())
        mailer.queue_email(addr, f"[Bilan] {r.title}", html, kind="bilan", to_user=u, created_by=current_user)
    db.session.commit()
    where = "envoyé(s) par SMTP" if (cfg.enabled and cfg.mode == "smtp") else "déposé(s) dans la boîte d’envoi locale (data/outbox)"
    flash(f"{len(seen)} e-mail(s) {where} dans quelques secondes.", "success")
    for b in bad:
        flash(f"Adresse ignorée (invalide) : {b}", "error")
    return redirect(url_for("bilan.report", rid=rid))

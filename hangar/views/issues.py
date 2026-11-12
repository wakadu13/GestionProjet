"""Tickets : vues (liste, tableau, tableur, calendrier, Gantt), détail, commentaires, fichiers..."""
import os
import uuid
from datetime import date, datetime, timedelta

from flask import (Blueprint, Response, abort, current_app, flash, redirect, render_template, request,
                   send_file, url_for)
from flask_login import current_user, login_required
from werkzeug.utils import secure_filename

from .. import issue_query as IQ
from ..extensions import db
from ..models import (
    RELATION_TYPES, Activity, Attachment, Comment, Issue, IssueLink, IssueRelation, Project, SavedView, WorkLog,
    group_reactions, now,
)
from ..securite import attachment_type, protect_file
from ..services import (
    add_relation, archive_issue, create_issue, description_conflict, description_conflict_message, duplicate_issue,
    delete_issue, handle_mentions, log_activity, move_issue_to_project, notify_subscribers, restore_issue,
    run_automations, text_version, to_int, update_issue,
)
from ..utils import (back_url, can, can_delete_issues, fmt_minutes, get_project, issue_url, parse_duration,
                     visible_projects)
from .home import _layout_context

bp = Blueprint("issues", __name__)


def get_issue(pid, seq, need="view") -> Issue:
    project = get_project(pid, need)
    issue = Issue.query.filter_by(project_id=project.id, sequence_id=seq).first()
    if issue is None:
        abort(404)
    return issue


@bp.route("/projects/<int:pid>/issues")
@login_required
def index(pid):
    project = get_project(pid)
    run_automations(project)
    view = None
    if request.args.get("view"):
        view = db.session.get(SavedView, to_int(request.args.get("view")) or 0)
        if view is not None and (view.project_id != pid or not (view.is_public or view.owner_id == current_user.id)):
            view = None  # vue d'un autre projet, ou vue privée de quelqu'un d'autre : ni nom ni description
    f = IQ.parse_filters(request.args)
    opts = IQ.display_options(request.args, f"p:{pid}")
    q = IQ.apply_filters(IQ.base_query().filter(Issue.project_id == pid), f, current_user)
    issues = IQ.fetch(q, opts)
    ctx = _layout_context(issues, opts, [project], f)
    return render_template("issues/index.html", project=project, view=view, **ctx)


@bp.route("/projects/<int:pid>/archives")
@login_required
def archives(pid):
    project = get_project(pid)
    issues = (Issue.query.filter(Issue.project_id == pid, Issue.archived_at.isnot(None))
              .order_by(Issue.archived_at.desc()).all())
    return render_template("issues/archives.html", project=project, issues=issues)


@bp.route("/i/<key>")
@login_required
def by_key(key):
    try:
        ident, num = key.rsplit("-", 1)
        num = int(num)
    except ValueError:
        abort(404)
    project = Project.query.filter_by(identifier=ident.upper()).first_or_404()
    return redirect(url_for("issues.detail", pid=project.id, seq=num))


def _form_data(form):
    data = {
        "title": form.get("title", ""),
        "description": form.get("description", ""),
        "priority": form.get("priority") or "none",
        "assignee_ids": form.getlist("assignee_ids"),
        "label_ids": form.getlist("label_ids"),
        "module_ids": form.getlist("module_ids"),
        "start_date": form.get("start_date"),
        "target_date": form.get("target_date"),
        "estimate": form.get("estimate"),
        "cycle_id": form.get("cycle_id"),
        "parent_id": form.get("parent_id"),
    }
    if form.get("state_id"):
        data["state_id"] = form.get("state_id")
    return data


@bp.route("/issues/new", methods=["POST"])
@login_required
def create():
    project = get_project(to_int(request.form.get("project_id")) or 0, "view")
    intake = bool(request.form.get("intake"))
    if intake:
        if not can(current_user, "comment", project) or not project.intake_enabled:
            abort(403)
    elif not can(current_user, "edit", project):
        abort(403)
    if not (request.form.get("title") or "").strip():
        flash("Le titre est obligatoire.", "error")
        return redirect(back_url(url_for("issues.index", pid=project.id)))
    issue = create_issue(project, _form_data(request.form), current_user, intake=intake)
    db.session.commit()
    if intake:
        flash(f"Demande « {issue.title} » envoyée dans l’intake.", "success")
        return redirect(url_for("intake.index", pid=project.id, issue=issue.id))
    flash(f"Ticket {issue.key} créé.", "success")
    if request.form.get("create_more"):
        return redirect(back_url(url_for("issues.index", pid=project.id)) + "#create-more")
    if request.form.get("open_after") or not request.form.get("next"):
        return redirect(issue_url(issue))
    return redirect(back_url(issue_url(issue)))


@bp.route("/projects/<int:pid>/issues/<int:seq>")
@login_required
def detail(pid, seq):
    return _detail_page(get_issue(pid, seq))


def _detail_page(issue: Issue, desc_conflict: dict | None = None):
    """Page d'un ticket. `desc_conflict` : description refusée (modifiée entre-temps par quelqu'un d'autre) à
    remettre dans l'éditeur, à côté de la version actuelle."""
    project = issue.project
    tab = request.args.get("tab", "all")
    timeline = []
    if tab in ("all", "comments"):
        timeline += [("comment", c.created_at, c) for c in issue.comments]
    if tab in ("all", "activity"):
        timeline += [("activity", a.created_at, a) for a in issue.activities if a.verb != "commented"]
    if tab == "worklogs":
        timeline += [("worklog", w.created_at, w) for w in issue.worklogs]
    timeline.sort(key=lambda t: (t[1] or datetime.min))
    worklog_by_user = {}
    for w in issue.worklogs:
        worklog_by_user[w.user] = worklog_by_user.get(w.user, 0) + w.minutes
    return render_template(
        "issues/detail.html", issue=issue, project=project, timeline=timeline, tab=tab,
        relations=issue.relations(), relation_types=RELATION_TYPES, group_reactions=group_reactions,
        worklog_by_user=sorted(worklog_by_user.items(), key=lambda x: -x[1]),
        can_edit=can(current_user, "edit", project), can_comment=can(current_user, "comment", project),
        other_projects=[p for p in visible_projects(current_user) if p.id != project.id
                        and can(current_user, "edit", p)],
        desc_version=text_version(issue.description), desc_conflict=desc_conflict,
    ), 409 if desc_conflict else 200


@bp.route("/projects/<int:pid>/issues/<int:seq>/edit", methods=["GET", "POST"])
@login_required
def edit(pid, seq):
    if request.method == "GET":  # liens relatifs (?tab=…) de la page réaffichée après un conflit
        tab = request.args.get("tab")
        return redirect(url_for("issues.detail", pid=pid, seq=seq, **({"tab": tab} if tab else {})))
    issue = get_issue(pid, seq, "edit")
    data = {}
    for k in ("title", "description", "description_version"):
        if k in request.form:
            data[k] = request.form[k]
    if description_conflict(issue, data):
        message = description_conflict_message(issue, current_user)
        if request.headers.get("X-Requested-With") == "fetch":
            return {"ok": False, "conflict": True, "error": message,
                    "description": issue.description or "", "description_version": text_version(issue.description)}, 409
        return _detail_page(issue, {"text": request.form.get("description", ""), "message": message})
    update_issue(issue, data, current_user)
    db.session.commit()
    if request.headers.get("X-Requested-With") == "fetch":
        return {"ok": True, "description_version": text_version(issue.description)}
    return redirect(issue_url(issue))


@bp.route("/projects/<int:pid>/issues/<int:seq>/description/<int:aid>/restore", methods=["POST"])
@login_required
def description_restore(pid, seq, aid):
    """Remet une ancienne description (gardée dans l'historique) ; la description remplacée y reste à son tour."""
    issue = get_issue(pid, seq, "edit")
    a = db.session.get(Activity, aid)
    if a is None or a.issue_id != issue.id or a.field != "description" or a.old_value is None:
        abort(404)
    if update_issue(issue, {"description": a.old_value}, current_user):
        db.session.commit()
        flash("Ancienne description remise. Celle qu’elle remplace reste dans l’historique.", "success")
    return redirect(issue_url(issue))


# --- Commentaires -----------------------------------------------------------

@bp.route("/projects/<int:pid>/issues/<int:seq>/comments", methods=["POST"])
@login_required
def comment_add(pid, seq):
    issue = get_issue(pid, seq, "comment")
    body = (request.form.get("body") or "").strip()
    if body:
        c = Comment(issue=issue, author=current_user, body=body)
        db.session.add(c)
        a = log_activity(issue, current_user, "commented", new=body[:200])
        issue.subscribe(current_user)
        db.session.flush()
        handle_mentions(body, issue, current_user)
        notify_subscribers(issue, current_user, a, kind="comment")
        issue.updated_at = now()
        db.session.commit()
    return redirect(issue_url(issue) + "#bottom")


def _own_comment(cid):
    c = db.session.get(Comment, cid)
    if c is None:
        abort(404)
    if c.author_id != current_user.id and not can(current_user, "admin", c.issue.project):
        abort(403)
    return c


@bp.route("/comments/<int:cid>/edit", methods=["POST"])
@login_required
def comment_edit(cid):
    c = _own_comment(cid)
    body = (request.form.get("body") or "").strip()
    if body:
        c.body = body
        c.updated_at = now()
        db.session.commit()
    return redirect(issue_url(c.issue) + f"#comment-{c.id}")


@bp.route("/comments/<int:cid>/delete", methods=["POST"])
@login_required
def comment_delete(cid):
    c = _own_comment(cid)
    issue = c.issue
    db.session.delete(c)
    db.session.commit()
    flash("Commentaire supprimé.", "info")
    return redirect(issue_url(issue))


# --- Pièces jointes ---------------------------------------------------------

@bp.route("/projects/<int:pid>/issues/<int:seq>/attachments", methods=["POST"])
@login_required
def attachment_add(pid, seq):
    issue = get_issue(pid, seq, "comment")
    files = request.files.getlist("files")
    folder = os.path.join(current_app.config["UPLOAD_DIR"], str(issue.project_id))
    os.makedirs(folder, exist_ok=True)
    n = 0
    for f in files:
        if not f or not f.filename:
            continue
        name = secure_filename(f.filename) or "fichier"
        stored = f"{uuid.uuid4().hex}_{name}"
        path = os.path.join(folder, stored)
        f.save(path)
        att = Attachment(issue=issue, filename=f.filename[:255], stored_name=f"{issue.project_id}/{stored}",
                         size=os.path.getsize(path), mimetype=f.mimetype or "application/octet-stream",
                         uploaded_by=current_user)
        db.session.add(att)
        log_activity(issue, current_user, "attachment_added", new=f.filename)
        n += 1
    db.session.commit()
    if n:
        flash(f"{n} fichier(s) ajouté(s).", "success")
    return redirect(issue_url(issue) + "#attachments")


def _attachment(aid, need="view"):
    a = db.session.get(Attachment, aid)
    if a is None:
        abort(404)
    if not can(current_user, need, a.issue.project):
        abort(403)
    return a


@bp.route("/attachments/<int:aid>")
@login_required
def attachment_get(aid):
    a = _attachment(aid)
    path = os.path.join(current_app.config["UPLOAD_DIR"], a.stored_name)
    if not os.path.exists(path):
        abort(404)
    # Type et affichage décidés d'après l'extension, pas d'après le type annoncé par l'expéditeur : un SVG
    # (ou une page HTML) ouvert dans le navigateur exécuterait son script avec la session de celui qui clique.
    mimetype, viewable = attachment_type(a.filename)
    inline = request.args.get("inline") == "1" and viewable
    response = send_file(path, mimetype=mimetype, as_attachment=not inline, download_name=a.filename)
    return protect_file(response, pdf=inline and mimetype == "application/pdf")


@bp.route("/attachments/<int:aid>/delete", methods=["POST"])
@login_required
def attachment_delete(aid):
    a = _attachment(aid, "comment")
    if a.uploaded_by_id != current_user.id and not can(current_user, "edit", a.issue.project):
        abort(403)
    issue = a.issue
    from ..services import remove_attachment_file
    remove_attachment_file(a.stored_name, exclude_ids=[a.id])
    log_activity(issue, current_user, "attachment_removed", old=a.filename)
    db.session.delete(a)
    db.session.commit()
    return redirect(issue_url(issue) + "#attachments")


# --- Liens, relations, sous-tickets, temps passé ----------------------------

@bp.route("/projects/<int:pid>/issues/<int:seq>/links", methods=["POST"])
@login_required
def link_add(pid, seq):
    issue = get_issue(pid, seq, "edit")
    url = (request.form.get("url") or "").strip()
    if url:
        if "://" not in url and not url.startswith("/"):
            url = "https://" + url
        title = (request.form.get("title") or "").strip() or url
        db.session.add(IssueLink(issue=issue, url=url[:1000], title=title[:255], created_by=current_user))
        log_activity(issue, current_user, "link_added", new=title)
        db.session.commit()
    return redirect(issue_url(issue) + "#links")


@bp.route("/links/<int:lid>/delete", methods=["POST"])
@login_required
def link_delete(lid):
    link = db.session.get(IssueLink, lid) or abort(404)
    if not can(current_user, "edit", link.issue.project):
        abort(403)
    issue = link.issue
    log_activity(issue, current_user, "link_removed", old=link.title)
    db.session.delete(link)
    db.session.commit()
    return redirect(issue_url(issue) + "#links")


def _find_issue_ref(ref, default_project):
    ref = (ref or "").strip().upper()
    if not ref:
        return None
    if "-" in ref:
        ident, _, num = ref.rpartition("-")
        p = Project.query.filter_by(identifier=ident).first()
    else:
        p, num = default_project, ref
    if not p or not num.isdigit() or not can(current_user, "view", p):
        return None
    return Issue.query.filter_by(project_id=p.id, sequence_id=int(num)).first()


@bp.route("/projects/<int:pid>/issues/<int:seq>/relations", methods=["POST"])
@login_required
def relation_add(pid, seq):
    issue = get_issue(pid, seq, "edit")
    other = db.session.get(Issue, to_int(request.form.get("related_id")) or 0)
    if other is not None and not can(current_user, "view", other.project):
        other = None  # ticket d'un projet qu'on ne voit pas : introuvable, comme par sa clé
    other = other or _find_issue_ref(request.form.get("ref"), issue.project)
    rtype = request.form.get("relation_type", "relates_to")
    if other is None:
        flash("Ticket introuvable.", "error")
    elif add_relation(issue, other, rtype, current_user) is None:
        flash("Relation impossible (déjà existante ou invalide).", "error")
    db.session.commit()
    return redirect(issue_url(issue) + "#relations")


@bp.route("/relations/<int:rid>/delete", methods=["POST"])
@login_required
def relation_delete(rid):
    rel = db.session.get(IssueRelation, rid) or abort(404)
    if not can(current_user, "edit", rel.issue.project):
        abort(403)
    back = request.form.get("next")
    label = RELATION_TYPES[rel.relation_type][0]
    log_activity(rel.issue, current_user, "relation_removed", label, rel.related.key, None)
    db.session.delete(rel)
    db.session.commit()
    return redirect(back if back and back.startswith("/") else issue_url(rel.issue))


@bp.route("/projects/<int:pid>/issues/<int:seq>/subissues", methods=["POST"])
@login_required
def subissue_add(pid, seq):
    issue = get_issue(pid, seq, "edit")
    title = (request.form.get("title") or "").strip()
    ref = request.form.get("ref")
    if title:
        child = create_issue(issue.project, {"title": title, "parent_id": issue.id,
                                             "cycle_id": issue.cycle_id}, current_user)
        log_activity(issue, current_user, "subissue_added", new=child.key)
    elif ref:
        child = _find_issue_ref(ref, issue.project)
        if child is None or child.project_id != issue.project_id:
            flash("Ticket introuvable dans ce projet.", "error")
        else:
            update_issue(child, {"parent_id": issue.id}, current_user)
    db.session.commit()
    return redirect(issue_url(issue) + "#subissues")


@bp.route("/projects/<int:pid>/issues/<int:seq>/worklogs", methods=["POST"])
@login_required
def worklog_add(pid, seq):
    issue = get_issue(pid, seq, "edit")
    minutes = parse_duration(request.form.get("duration", ""))
    if minutes <= 0:
        flash("Durée invalide (ex. 1h30, 45m, 2).", "error")
    else:
        from ..utils import parse_date
        w = WorkLog(issue=issue, user=current_user, minutes=minutes,
                    date=parse_date(request.form.get("date")) or date.today(),
                    description=(request.form.get("description") or "")[:255])
        db.session.add(w)
        log_activity(issue, current_user, "worklog", new=fmt_minutes(minutes))
        db.session.commit()
        flash(f"{fmt_minutes(minutes)} enregistrées.", "success")
    return redirect(issue_url(issue) + "#worklogs")


@bp.route("/worklogs/<int:wid>/delete", methods=["POST"])
@login_required
def worklog_delete(wid):
    w = db.session.get(WorkLog, wid) or abort(404)
    if w.user_id != current_user.id and not can(current_user, "admin", w.issue.project):
        abort(403)
    issue = w.issue
    db.session.delete(w)
    db.session.commit()
    return redirect(back_url(issue_url(issue)))


# --- Actions ---------------------------------------------------------------

@bp.route("/projects/<int:pid>/issues/<int:seq>/subscribe", methods=["POST"])
@login_required
def subscribe(pid, seq):
    issue = get_issue(pid, seq)
    if issue.is_subscribed(current_user):
        issue.subscribers.remove(current_user)
        state = False
    else:
        issue.subscribers.append(current_user)
        state = True
    db.session.commit()
    if request.headers.get("X-Requested-With") == "fetch":
        return {"ok": True, "subscribed": state}
    return redirect(issue_url(issue))


@bp.route("/projects/<int:pid>/issues/<int:seq>/<action>", methods=["POST"])
@login_required
def action(pid, seq, action):
    issue = get_issue(pid, seq, "edit")
    project = issue.project
    if action == "archive":
        archive_issue(issue, current_user)
        db.session.commit()
        flash(f"{issue.key} archivé.", "info")
        return redirect(url_for("issues.index", pid=pid))
    if action == "restore":
        restore_issue(issue, current_user)
        db.session.commit()
        flash(f"{issue.key} restauré.", "success")
        return redirect(issue_url(issue))
    if action == "delete":
        if not can_delete_issues(current_user, project):
            flash("Seuls l’administrateur et le responsable du projet peuvent supprimer un ticket. "
                  "Tu peux l’archiver à la place.", "error")
            return redirect(issue_url(issue))
        key = issue.key
        delete_issue(issue)
        db.session.commit()
        flash(f"{key} supprimé définitivement.", "info")
        return redirect(url_for("issues.index", pid=pid))
    if action == "duplicate":
        new = duplicate_issue(issue, current_user)
        db.session.commit()
        flash(f"{issue.key} dupliqué en {new.key}.", "success")
        return redirect(issue_url(new))
    if action == "move":
        target = get_project(to_int(request.form.get("project_id")) or 0, "edit")
        if target.id != project.id:
            move_issue_to_project(issue, target, current_user)
            db.session.commit()
            flash(f"Ticket déplacé vers {target.name} ({issue.key}).", "success")
        return redirect(issue_url(issue))
    abort(404)


@bp.route("/issues/bulk", methods=["POST"])
@login_required
def bulk():
    ids = [to_int(x) for x in request.form.getlist("ids") if to_int(x)]
    act = request.form.get("bulk_action")
    value = request.form.get("value")
    n = 0
    for issue in Issue.query.filter(Issue.id.in_(ids)).all():
        if not can(current_user, "edit", issue.project):
            continue
        if act == "state":
            # valeur = groupe d’état (commun à tous les projets) ou id d’état
            st = issue.project.first_state_of(value) if not str(value).isdigit() else None
            update_issue(issue, {"state_id": st.id if st else value}, current_user)
        elif act == "priority":
            update_issue(issue, {"priority": value}, current_user)
        elif act == "assignee":
            uid = to_int(value)
            ids_ = [u.id for u in issue.assignees]
            if uid and uid not in ids_:
                update_issue(issue, {"assignee_ids": ids_ + [uid]}, current_user)
        elif act == "unassign":
            update_issue(issue, {"assignee_ids": []}, current_user)
        elif act == "label":
            lid = to_int(value)
            ids_ = [l.id for l in issue.labels]
            if lid and lid not in ids_:
                update_issue(issue, {"label_ids": ids_ + [lid]}, current_user)
        elif act == "cycle":
            update_issue(issue, {"cycle_id": value}, current_user)
        elif act == "module":
            mid = to_int(value)
            ids_ = [m.id for m in issue.modules]
            if mid and mid not in ids_:
                update_issue(issue, {"module_ids": ids_ + [mid]}, current_user)
        elif act == "target_date":
            update_issue(issue, {"target_date": value}, current_user)
        elif act == "archive":
            archive_issue(issue, current_user)
        elif act == "delete":
            if not can_delete_issues(current_user, issue.project):
                continue
            delete_issue(issue)
        else:
            continue
        n += 1
    db.session.commit()
    flash(f"{n} ticket(s) mis à jour.", "success" if n else "info")
    return redirect(back_url(url_for("home.workspace_issues")))


# --- Import / export -------------------------------------------------------

@bp.route("/projects/<int:pid>/issues/export.<fmt>")
@login_required
def export(pid, fmt):
    from .. import importer
    project = get_project(pid)
    f = IQ.parse_filters(request.args)
    q = IQ.apply_filters(IQ.base_query().filter(Issue.project_id == pid), f, current_user)
    if request.args.get("archived") == "1":
        q = Issue.query.filter(Issue.project_id == pid)
    issues = IQ.sort_issues(q.all(), "key")
    stamp = date.today().isoformat()
    base = f"{project.identifier}_tickets_{stamp}"
    if fmt == "csv":
        return Response(importer.export_csv(issues), mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename={base}.csv"})
    if fmt == "xlsx":
        return send_file(importer.export_xlsx(issues, project.name), as_attachment=True,
                         download_name=f"{base}.xlsx",
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    if fmt == "json":
        return Response(importer.export_json(issues), mimetype="application/json",
                        headers={"Content-Disposition": f"attachment; filename={base}.json"})
    if fmt == "md":
        return Response(importer.export_markdown(issues, project), mimetype="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename={base}.md"})
    abort(404)


@bp.route("/projects/<int:pid>/issues/import", methods=["POST"])
@login_required
def import_issues(pid):
    from .. import importer
    project = get_project(pid, "edit")
    file = request.files.get("file")
    if not file or not file.filename:
        flash("Choisis un fichier .xlsx, .csv ou .json.", "error")
        return redirect(url_for("projects.settings", pid=pid, section="import"))
    update_existing = bool(request.form.get("update_existing"))
    try:
        rows = importer.read_table(file.stream, file.filename)
    except Exception as exc:  # fichier illisible
        flash(f"Import impossible : {exc}", "error")
        return redirect(url_for("projects.settings", pid=pid, section="import"))
    backup = None
    if update_existing:
        # Filet de sécurité : la mise à jour réécrit des tickets existants.
        from .settings import make_backup
        try:
            backup = os.path.basename(make_backup())
        except Exception:  # noqa: BLE001 - disque plein, dossier protégé…
            flash("Sauvegarde de sécurité impossible : rien n’a été importé. Réessaie, ou décoche "
                  "« Mettre à jour les tickets existants » pour seulement ajouter des tickets.", "error")
            return redirect(url_for("projects.settings", pid=pid, section="import"))
    try:
        report = importer.import_rows(project, rows, current_user,
                                      create_members=bool(request.form.get("create_members")),
                                      update_existing=update_existing)
        db.session.commit()
    except Exception as exc:  # contenu inattendu
        db.session.rollback()
        flash(f"Import impossible : {exc}", "error")
        return redirect(url_for("projects.settings", pid=pid, section="import"))
    flash(f"Import terminé : {report['created']} créé(s), {report['updated']} mis à jour, "
          f"{report['skipped']} ignoré(s).", "success")
    if backup:
        flash(f"Une sauvegarde de la base a été faite juste avant ({backup}) : un administrateur peut la "
              "restaurer depuis Sauvegardes.", "info")
    for w in report["warnings"][:8]:
        flash(w, "info")
    return redirect(url_for("issues.index", pid=pid))


@bp.route("/projects/<int:pid>/calendar.ics")
@login_required
def ical(pid):
    project = get_project(pid)
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Hangar//FR", "CALSCALE:GREGORIAN",
             f"X-WR-CALNAME:{project.name}"]
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    for i in IQ.base_query().filter(Issue.project_id == pid, Issue.target_date.isnot(None)).all():
        start = i.start_date or i.target_date
        end = i.target_date + timedelta(days=1)
        summary = f"{i.key} {i.title}".replace(",", "\\,").replace(";", "\\;")
        lines += ["BEGIN:VEVENT", f"UID:hangar-{i.id}@local", f"DTSTAMP:{stamp}",
                  f"DTSTART;VALUE=DATE:{start:%Y%m%d}", f"DTEND;VALUE=DATE:{end:%Y%m%d}",
                  f"SUMMARY:{summary}",
                  f"DESCRIPTION:État : {i.state.name if i.state else ''} - "
                  f"Assignés : {', '.join(u.display_name for u in i.assignees) or 'personne'}",
                  f"URL:{request.host_url.rstrip('/')}{issue_url(i)}", "END:VEVENT"]
    for c in project.cycles:
        if c.start_date and c.end_date:
            lines += ["BEGIN:VEVENT", f"UID:hangar-cycle-{c.id}@local", f"DTSTAMP:{stamp}",
                      f"DTSTART;VALUE=DATE:{c.start_date:%Y%m%d}",
                      f"DTEND;VALUE=DATE:{c.end_date + timedelta(days=1):%Y%m%d}",
                      f"SUMMARY:Cycle : {c.name}", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return Response("\r\n".join(lines), mimetype="text/calendar",
                    headers={"Content-Disposition": f"attachment; filename={project.identifier}.ics"})

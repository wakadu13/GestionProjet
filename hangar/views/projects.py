"""Projets : liste, création, adhésion, paramètres (membres, états, étiquettes, fonctionnalités...)."""
import re

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..models import (
    ESTIMATE_TYPES, PALETTE, ROLES, STATE_GROUP_LABELS, Issue, Label, Project, ProjectMember, State, User, now,
)
from ..securite import clean_color
from ..services import create_project, ensure_member, log_activity, to_int, unsubscribe_from_project
from ..utils import back_url, contient_plie, get_project, parse_date, project_role, tri_fr, visible_projects

bp = Blueprint("projects", __name__, url_prefix="/projects")

SECTIONS = [
    ("general", "Général", "settings"),
    ("members", "Membres", "users"),
    ("features", "Fonctionnalités", "zap"),
    ("states", "États", "check-circle"),
    ("labels", "Étiquettes", "tag"),
    ("estimates", "Estimations", "estimate"),
    ("automations", "Automatisations", "refresh"),
    ("import", "Import / export", "upload"),
    ("danger", "Zone sensible", "alert"),
]


@bp.route("/")
@login_required
def index():
    tab = request.args.get("tab", "all")
    sort = request.args.get("sort", "name")
    projects = visible_projects(current_user, include_archived=(tab == "archived"))
    if tab == "archived":
        projects = [p for p in projects if p.archived_at]
    elif tab == "mine":
        projects = [p for p in projects if p.member(current_user)]
    text = (request.args.get("q") or "").strip()
    if text:
        projects = [p for p in projects if contient_plie(text, p.name) or contient_plie(text, p.identifier)]
    if sort == "recent":
        projects.sort(key=lambda p: p.created_at, reverse=True)
    elif sort == "members":
        projects.sort(key=lambda p: len(p.members), reverse=True)
    return render_template("projects/index.html", projects=projects, tab=tab, sort=sort, q=text)


def _valid_identifier(ident, exclude_id=None):
    if not re.fullmatch(r"[A-Z][A-Z0-9]{1,9}", ident or ""):
        return "L’identifiant doit contenir 2 à 10 caractères (lettres majuscules et chiffres, commençant par une lettre)."
    q = Project.query.filter(Project.identifier == ident)
    if exclude_id:
        q = q.filter(Project.id != exclude_id)
    if q.first():
        return "Cet identifiant est déjà utilisé par un autre projet."
    return None


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    if current_user.is_guest:
        abort(403)
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        ident = (request.form.get("identifier") or "").strip().upper()
        err = None if name else "Le nom du projet est obligatoire."
        err = err or _valid_identifier(ident)
        if err:
            flash(err, "error")
            return render_template("projects/new.html", form=request.form, users=_active_users())
        lead = db.session.get(User, to_int(request.form.get("lead_id")) or 0) or current_user
        member_ids = [int(x) for x in request.form.getlist("member_ids") if x.isdigit()]
        if lead.id not in member_ids:
            member_ids.append(lead.id)
        p = create_project(
            name, ident, current_user, description=request.form.get("description", ""),
            emoji=request.form.get("emoji") or "🚀", color=clean_color(request.form.get("color"), "#3f76ff"),
            network=request.form.get("network", "public"), lead=lead, member_ids=member_ids,
            start_date=parse_date(request.form.get("start_date")),
            target_date=parse_date(request.form.get("target_date")),
            estimate_type=request.form.get("estimate_type", "points"),
        )
        db.session.commit()
        flash(f"Projet « {p.name} » créé.", "success")
        return redirect(url_for("issues.index", pid=p.id))
    return render_template("projects/new.html", form={}, users=_active_users())


def _active_users():
    return User.query.filter_by(active=True).order_by(tri_fr(User.first_name), tri_fr(User.last_name)).all()


@bp.route("/<int:pid>")
@login_required
def detail(pid):
    get_project(pid)
    return redirect(url_for("issues.index", pid=pid))


@bp.route("/<int:pid>/join", methods=["POST"])
@login_required
def join(pid):
    p = get_project(pid)
    if current_user.is_guest:
        abort(403)
    ensure_member(p, current_user)
    db.session.commit()
    flash(f"Tu as rejoint le projet « {p.name} ».", "success")
    return redirect(url_for("issues.index", pid=pid))


@bp.route("/<int:pid>/leave", methods=["POST"])
@login_required
def leave(pid):
    p = get_project(pid)
    m = p.member(current_user)
    if m:
        admins = [x for x in p.members if x.role == "admin"]
        if m.role == "admin" and len(admins) == 1 and len(p.members) > 1:
            flash("Tu es le seul administrateur : nomme un autre administrateur avant de quitter.", "error")
            return redirect(back_url(url_for("projects.index")))
        unsubscribe_from_project(p, current_user)
        db.session.delete(m)
        db.session.commit()
        flash(f"Tu as quitté « {p.name} ».", "info")
    return redirect(url_for("projects.index"))


# ---------------------------------------------------------------------------
# Paramètres
# ---------------------------------------------------------------------------

@bp.route("/<int:pid>/settings", methods=["GET", "POST"])
@bp.route("/<int:pid>/settings/<section>", methods=["GET", "POST"])
@login_required
def settings(pid, section="general"):
    p = get_project(pid, "view")
    if section not in {s for s, _, _ in SECTIONS}:
        abort(404)
    is_admin = project_role(current_user, p) == "admin"
    if request.method == "POST":
        if not is_admin:
            abort(403)
        handler = globals().get(f"_save_{section}")
        if handler:
            resp = handler(p)
            db.session.commit()
            if resp is not None:
                return resp
        return redirect(url_for("projects.settings", pid=pid, section=section))
    ctx = {"project": p, "section": section, "sections": SECTIONS, "is_admin": is_admin,
           "ROLES": ROLES, "STATE_GROUP_LABELS": STATE_GROUP_LABELS, "ESTIMATE_TYPES": ESTIMATE_TYPES,
           "PALETTE": PALETTE}
    if section == "members":
        member_ids = {m.user_id for m in p.members}
        ctx["candidates"] = [u for u in _active_users() if u.id not in member_ids]
    if section == "states":
        ctx["state_counts"] = dict(db.session.query(Issue.state_id, db.func.count(Issue.id))
                                   .filter(Issue.project_id == p.id).group_by(Issue.state_id).all())
    if section == "labels":
        from ..models import issue_labels
        ctx["label_counts"] = dict(db.session.query(issue_labels.c.label_id, db.func.count())
                                   .group_by(issue_labels.c.label_id).all())
    return render_template("projects/settings.html", **ctx)


def _save_general(p):
    name = (request.form.get("name") or "").strip()
    ident = (request.form.get("identifier") or "").strip().upper()
    err = _valid_identifier(ident, exclude_id=p.id)
    if not name or err:
        flash(err or "Le nom est obligatoire.", "error")
        return None
    p.name = name
    p.identifier = ident
    p.description = request.form.get("description", "")
    p.emoji = request.form.get("emoji") or p.emoji
    p.color = clean_color(request.form.get("color"), p.color)
    p.network = request.form.get("network", "public")
    p.lead_id = to_int(request.form.get("lead_id"))
    p.default_assignee_id = to_int(request.form.get("default_assignee_id"))
    p.start_date = parse_date(request.form.get("start_date"))
    p.target_date = parse_date(request.form.get("target_date"))
    flash("Paramètres enregistrés.", "success")
    return None


def _save_features(p):
    for f in ("cycles_enabled", "modules_enabled", "views_enabled", "pages_enabled", "intake_enabled",
              "time_tracking_enabled"):
        setattr(p, f, bool(request.form.get(f)))
    flash("Fonctionnalités mises à jour.", "success")


def _save_estimates(p):
    t = request.form.get("estimate_type", "none")
    if t in ESTIMATE_TYPES:
        p.estimate_type = t
        flash("Système d’estimation mis à jour.", "success")


def _save_automations(p):
    p.auto_archive_months = to_int(request.form.get("auto_archive_months")) or 0
    p.auto_close_months = to_int(request.form.get("auto_close_months")) or 0
    flash("Automatisations enregistrées.", "success")


def _save_members(p):
    action = request.form.get("action")
    if action == "add":
        role = request.form.get("role", "member")
        for uid in request.form.getlist("user_ids"):
            u = db.session.get(User, to_int(uid) or 0)
            if u:
                m = ensure_member(p, u, role if role in ROLES else "member")
                if u.role == "guest":
                    m.role = "guest"
        flash("Membres ajoutés.", "success")
    elif action == "role":
        m = db.session.get(ProjectMember, to_int(request.form.get("member_id")) or 0)
        role = request.form.get("role")
        if m and m.project_id == p.id and role in ROLES:
            if m.role == "admin" and role != "admin" and sum(1 for x in p.members if x.role == "admin") == 1:
                flash("Le projet doit conserver au moins un administrateur.", "error")
            else:
                m.role = "guest" if m.user.role == "guest" else role
                flash("Rôle modifié.", "success")
    elif action == "remove":
        m = db.session.get(ProjectMember, to_int(request.form.get("member_id")) or 0)
        if m and m.project_id == p.id:
            if m.role == "admin" and sum(1 for x in p.members if x.role == "admin") == 1:
                flash("Impossible de retirer le dernier administrateur.", "error")
            else:
                for i in Issue.query.filter(Issue.project_id == p.id, Issue.assignees.any(User.id == m.user_id)):
                    i.assignees = [u for u in i.assignees if u.id != m.user_id]
                unsubscribe_from_project(p, m.user)
                db.session.delete(m)
                flash("Membre retiré du projet.", "info")


def _save_states(p):
    action = request.form.get("action")
    if action == "add":
        name = (request.form.get("name") or "").strip()
        group = request.form.get("group", "unstarted")
        if name and group in STATE_GROUP_LABELS:
            seqs = [s.sequence for s in p.states if s.group == group]
            from ..models import STATE_GROUP_ORDER
            base = (STATE_GROUP_ORDER[group] + 1) * 10000
            db.session.add(State(project=p, name=name[:80], group=group,
                                 color=clean_color(request.form.get("color"), "#a3a3a3"),
                                 description=request.form.get("description", "")[:255],
                                 sequence=(max(seqs) + 100) if seqs else base))
            flash(f"État « {name} » ajouté.", "success")
    elif action == "edit":
        s = db.session.get(State, to_int(request.form.get("state_id")) or 0)
        if s and s.project_id == p.id:
            s.name = (request.form.get("name") or s.name).strip()[:80]
            s.color = clean_color(request.form.get("color"), s.color)
            s.description = request.form.get("description", "")[:255]
            flash("État modifié.", "success")
    elif action == "default":
        sid = to_int(request.form.get("state_id"))
        for s in p.states:
            s.is_default = s.id == sid
        flash("État par défaut modifié.", "success")
    elif action in ("up", "down"):
        s = db.session.get(State, to_int(request.form.get("state_id")) or 0)
        if s and s.project_id == p.id:
            same = [x for x in p.states if x.group == s.group]
            idx = same.index(s)
            j = idx - 1 if action == "up" else idx + 1
            if 0 <= j < len(same):
                same[idx].sequence, same[j].sequence = same[j].sequence, same[idx].sequence
    elif action == "delete":
        s = db.session.get(State, to_int(request.form.get("state_id")) or 0)
        if s and s.project_id == p.id:
            if s.is_default:
                flash("Impossible de supprimer l’état par défaut.", "error")
            elif len([x for x in p.states if x.group == s.group]) == 1:
                flash("Chaque groupe doit conserver au moins un état.", "error")
            else:
                target = next(x for x in p.states if x.group == s.group and x.id != s.id)
                Issue.query.filter_by(state_id=s.id).update({"state_id": target.id})
                db.session.delete(s)
                flash(f"État supprimé ; ses tickets ont été déplacés vers « {target.name} ».", "info")


def _save_labels(p):
    action = request.form.get("action")
    if action == "add":
        name = (request.form.get("name") or "").strip()
        if name:
            if any(l.name.lower() == name.lower() for l in p.labels):
                flash("Cette étiquette existe déjà.", "error")
            else:
                db.session.add(Label(project=p, name=name[:80], color=clean_color(request.form.get("color"), "#3f76ff"),
                                     parent_id=to_int(request.form.get("parent_id")),
                                     description=request.form.get("description", "")[:255]))
                flash(f"Étiquette « {name} » créée.", "success")
    elif action == "edit":
        l = db.session.get(Label, to_int(request.form.get("label_id")) or 0)
        if l and l.project_id == p.id:
            l.name = (request.form.get("name") or l.name).strip()[:80]
            l.color = clean_color(request.form.get("color"), l.color)
            parent = to_int(request.form.get("parent_id"))
            l.parent_id = parent if parent != l.id else None
            flash("Étiquette modifiée.", "success")
    elif action == "delete":
        l = db.session.get(Label, to_int(request.form.get("label_id")) or 0)
        if l and l.project_id == p.id:
            Label.query.filter_by(parent_id=l.id).update({"parent_id": None})
            db.session.delete(l)
            flash("Étiquette supprimée.", "info")


def _save_danger(p):
    action = request.form.get("action")
    if action == "archive":
        p.archived_at = now()
        flash(f"Projet « {p.name} » archivé.", "info")
        return redirect(url_for("projects.index"))
    if action == "restore":
        p.archived_at = None
        flash(f"Projet « {p.name} » restauré.", "success")
    if action == "delete":
        if request.form.get("confirm", "").strip().upper() != p.identifier:
            flash("Confirmation incorrecte : saisis l’identifiant du projet pour le supprimer.", "error")
            return None
        name = p.name
        db.session.delete(p)
        flash(f"Projet « {name} » supprimé définitivement.", "info")
        return redirect(url_for("projects.index"))
    return None

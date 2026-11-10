"""Initiatives et Workgraph (inspirés de Plane).

- /initiatives : liste (tableau ou cartes), filtres, création ;
- /initiatives/<id> : détail (propriétés, projets et modules liés, jalons, dépendances, mises à jour) ;
- /workgraph : graphe Espace → Initiatives → Projets → Modules (ou tableau hiérarchique), onglets
  Initiative / Projet / Équipe.

Droits : lecture pour tous les comptes ; création et modification pour les administrateurs et membres
(pas les invités) ; suppression par un administrateur, l'auteur ou le responsable de l'initiative.
"""
from __future__ import annotations

from datetime import date

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from .. import workgraph as W
from ..extensions import db
from ..models import (
    HEALTH_LABELS, HEALTH_RANK, HEALTHS, INITIATIVE_STATUS_LABELS, INITIATIVE_STATUSES, Initiative, InitiativeUpdate,
    Module, Project, User,
)
from ..services import to_int
from ..utils import is_safe_next, parse_date, visible_projects

bp = Blueprint("workgraph", __name__)


# ---------------------------------------------------------------------------
# Droits et utilitaires
# ---------------------------------------------------------------------------

def can_edit_initiatives(user=None) -> bool:
    user = user or current_user
    return bool(user and user.is_authenticated and not user.is_guest)


def can_delete_initiative(ini: Initiative, user=None) -> bool:
    user = user or current_user
    return can_edit_initiatives(user) and (user.is_admin or ini.created_by_id == user.id or ini.lead_id == user.id)


def _require_edit():
    if not can_edit_initiatives():
        abort(403)


def _initiative(iid: int) -> Initiative:
    ini = db.session.get(Initiative, iid)
    if ini is None:
        abort(404)
    return ini


def _users():
    return sorted(User.query.filter_by(active=True).all(), key=lambda u: u.display_name.lower())


def _next(default: str) -> str:
    nxt = request.form.get("next") or request.args.get("next")
    return nxt if is_safe_next(nxt) else default


def _wants_json() -> bool:
    return request.headers.get("X-Requested-With") == "fetch" or request.accept_mimetypes.best == "application/json"


def _apply_form(ini: Initiative, form, creating: bool = False) -> str | None:
    """Recopie le formulaire complet ; retourne un message d'erreur ou None."""
    name = (form.get("name") or "").strip()[:200]
    if not name:
        return "Le nom de l’initiative est obligatoire."
    start, target = parse_date(form.get("start_date")), parse_date(form.get("target_date"))
    if start and target and target < start:
        return "L’échéance doit être postérieure à la date de début."
    ini.name = name
    ini.emoji = (form.get("emoji") or "").strip()[:16] or "💡"
    ini.description = form.get("description", ini.description or "")
    if form.get("status") in INITIATIVE_STATUS_LABELS:
        ini.status = form.get("status")
    lead = db.session.get(User, to_int(form.get("lead_id")) or 0)
    ini.lead = lead if lead and lead.active else None
    ini.start_date, ini.target_date = start, target
    if "health_override" in form:
        ini.health_override = form.get("health_override") if form.get("health_override") in HEALTH_LABELS else None
    if creating:
        visible = {p.id: p for p in visible_projects(current_user)}
        ini.projects = [visible[pid] for pid in (to_int(x) for x in form.getlist("project_ids")) if pid in visible]
    return None


def _apply_field(ini: Initiative, field: str, value: str | None) -> str | None:
    """Modification d'une seule propriété (sélecteurs du panneau latéral)."""
    if field == "status":
        if value not in INITIATIVE_STATUS_LABELS:
            return "Statut inconnu."
        ini.status = value
    elif field == "health":
        ini.health_override = value if value in HEALTH_LABELS else None
    elif field == "lead_id":
        lead = db.session.get(User, to_int(value) or 0)
        ini.lead = lead if lead and lead.active else None
    elif field in ("start_date", "target_date"):
        d = parse_date(value)
        other = ini.target_date if field == "start_date" else ini.start_date
        if d and other and ((field == "start_date" and other < d) or (field == "target_date" and d < other)):
            return "L’échéance doit être postérieure à la date de début."
        setattr(ini, field, d)
    elif field == "emoji":
        ini.emoji = (value or "").strip()[:16] or "💡"
    elif field == "name":
        if not (value or "").strip():
            return "Le nom de l’initiative est obligatoire."
        ini.name = value.strip()[:200]
    else:
        return "Propriété inconnue."
    return None


def _graph_filters(args) -> dict:
    return {"status": [s for s in args.getlist("status") if s in INITIATIVE_STATUS_LABELS],
            "health": [h for h in args.getlist("health") if h in HEALTH_LABELS],
            "hide_closed": args.get("hide_closed") == "1",
            "mine": args.get("mine") == "1"}


# ---------------------------------------------------------------------------
# Workgraph
# ---------------------------------------------------------------------------

@bp.route("/workgraph")
@login_required
def index():
    view = "table" if request.args.get("view") == "table" else "graph"
    tab = request.args.get("tab") if request.args.get("tab") in W.TABS else "initiative"
    group = "members" if request.args.get("group") == "members" else "lead"
    f = _graph_filters(request.args)
    graph = W.build_graph(current_user, tab, group, f)
    rows = W.table_rows(graph) if view == "table" else []
    n_filters = len(f["status"]) + len(f["health"]) + int(f["hide_closed"]) + int(f["mine"])
    return render_template("workgraph/index.html", graph=graph, rows=rows, view=view, tab=tab, group=group,
                           f=f, n_filters=n_filters, sel=request.args.get("sel", ""), tabs=W.TABS,
                           can_edit=can_edit_initiatives(), users=_users(), projects=visible_projects(current_user),
                           statuses=INITIATIVE_STATUSES, healths=HEALTHS)


@bp.route("/workgraph/data.json")
@login_required
def data():
    tab = request.args.get("tab") if request.args.get("tab") in W.TABS else "initiative"
    group = "members" if request.args.get("group") == "members" else "lead"
    return jsonify(W.build_graph(current_user, tab, group, _graph_filters(request.args)))


# ---------------------------------------------------------------------------
# Liste des initiatives
# ---------------------------------------------------------------------------

@bp.route("/initiatives")
@login_required
def initiatives():
    snap = W.Snapshot()
    every = Initiative.query.all()
    S = {i.id: snap.initiative(i) for i in every}
    if [i for i in every if W.refresh_health(i, S[i.id]["health"])]:
        db.session.commit()
    status, health = request.args.get("status"), request.args.get("health")
    mine = request.args.get("mine") == "1"
    q = (request.args.get("q") or "").strip()
    layout = "cards" if request.args.get("layout") == "cards" else "list"
    sort = request.args.get("sort") if request.args.get("sort") in ("status", "name", "progress", "target",
                                                                     "health") else "status"
    inis = every
    if mine:
        inis = [i for i in inis if i.lead_id == current_user.id]
    if q:
        ql = q.lower()
        inis = [i for i in inis if ql in i.name.lower() or ql in (i.description or "").lower()]
    status_counts = {k: sum(1 for i in inis if i.status == k) for k, _, _ in INITIATIVE_STATUSES}
    if status:
        inis = [i for i in inis if i.status == status]
    health_counts = {k: sum(1 for i in inis if S[i.id]["health"] == k) for k, _, _ in HEALTHS}
    if health:
        inis = [i for i in inis if S[i.id]["health"] == health]
    keys = {
        "status": lambda i: (W.GRAPH_STATUS_ORDER.get(i.status, 9), i.name.lower()),
        "name": lambda i: i.name.lower(),
        "progress": lambda i: (-S[i.id]["pct"], i.name.lower()),
        "target": lambda i: (S[i.id]["target"] or date(9999, 1, 1), i.name.lower()),
        "health": lambda i: (-HEALTH_RANK.get(S[i.id]["health"] or "", -1), i.name.lower()),
    }
    inis = sorted(inis, key=keys[sort])
    visible = {p.id for p in visible_projects(current_user)}
    return render_template("workgraph/initiatives.html", inis=inis, S=S, total=len(every), status=status,
                           health=health, mine=mine, q=q, layout=layout, sort=sort, status_counts=status_counts,
                           health_counts=health_counts, visible=visible, statuses=INITIATIVE_STATUSES,
                           healths=HEALTHS, users=_users(), projects=visible_projects(current_user),
                           can_edit=can_edit_initiatives())


@bp.route("/initiatives/new", methods=["POST"])
@login_required
def initiative_create():
    _require_edit()
    ini = Initiative(created_by=current_user, status="planned")
    err = _apply_form(ini, request.form, creating=True)
    if err:
        flash(err, "error")
        return redirect(_next(url_for("workgraph.initiatives")))
    db.session.add(ini)
    db.session.commit()
    flash(f"Initiative « {ini.name} » créée.", "success")
    return redirect(url_for("workgraph.initiative_detail", iid=ini.id))


# ---------------------------------------------------------------------------
# Détail d'une initiative
# ---------------------------------------------------------------------------

@bp.route("/initiatives/<int:iid>")
@login_required
def initiative_detail(iid):
    ini = _initiative(iid)
    snap = W.Snapshot()
    s = snap.initiative(ini)
    if W.refresh_health(ini, s["health"]):
        db.session.commit()
    vis = visible_projects(current_user)
    visible = {p.id for p in vis}
    projects = []
    for p in ini.projects:
        if p.id in visible:
            st = snap.project_stats(p.id)
            projects.append({"p": p, "st": st, "health": W.health_info(snap.project_health(p, st)[0])})
    hidden = sum(1 for p in ini.projects if p.id not in visible)
    modules = []
    for m in ini.modules:
        if m.project_id in visible:
            st = snap.module_stats(m.id)
            modules.append({"m": m, "st": st, "health": W.health_info(snap.module_health(m, st)[0])})
    linked_p = {p.id for p in ini.projects}
    linked_m = {m.id for m in ini.modules}
    cand_projects = [p for p in vis if p.id not in linked_p]
    cand_modules = [(p, [m for m in p.modules if m.archived_at is None and m.id not in linked_m]) for p in vis]
    cand_modules = [(p, ms) for p, ms in cand_modules if ms]
    graph_deps = _initiative_neighbours(ini, snap)
    return render_template("workgraph/initiative.html", ini=ini, s=s, projects=projects, modules=modules,
                           hidden=hidden, jalons=W.milestones(ini, snap, s["issue_ids"]),
                           cand_projects=cand_projects, cand_modules=cand_modules, neighbours=graph_deps,
                           users=_users(), statuses=INITIATIVE_STATUSES, healths=HEALTHS,
                           can_edit=can_edit_initiatives(), can_delete=can_delete_initiative(ini),
                           today=snap.today)


def _initiative_neighbours(ini: Initiative, snap: W.Snapshot) -> dict:
    """Autres initiatives qui bloquent celle-ci ou qu'elle bloque (au moins un ticket)."""
    mine = snap.initiative(ini)["issue_ids"]
    blocks, blocked_by = {}, {}
    others = [o for o in Initiative.query.all() if o.id != ini.id]
    sets = {o.id: snap.initiative(o)["issue_ids"] for o in others}
    for a, b in snap.blocked_by:  # a bloqué par b
        open_ = not snap.issues[a].is_closed and not snap.issues[b].is_closed
        for o in others:
            if b in mine and a in sets[o.id] and a not in mine and b not in sets[o.id]:
                d = blocks.setdefault(o.id, {"ini": o, "n": 0, "open": 0})
                d["n"] += 1
                d["open"] += open_
            if a in mine and b in sets[o.id] and b not in mine and a not in sets[o.id]:
                d = blocked_by.setdefault(o.id, {"ini": o, "n": 0, "open": 0})
                d["n"] += 1
                d["open"] += open_
    return {"blocks": list(blocks.values()), "blocked_by": list(blocked_by.values())}


@bp.route("/initiatives/<int:iid>/edit", methods=["POST"])
@login_required
def initiative_edit(iid):
    _require_edit()
    ini = _initiative(iid)
    field = request.form.get("field")
    err = _apply_field(ini, field, request.form.get("value")) if field else _apply_form(ini, request.form)
    if err:
        db.session.rollback()
        if _wants_json():
            return jsonify({"ok": False, "error": err}), 400
        flash(err, "error")
    else:
        db.session.commit()
        if _wants_json():
            return jsonify({"ok": True})
        flash("Initiative mise à jour.", "success")
    return redirect(_next(url_for("workgraph.initiative_detail", iid=ini.id)))


@bp.route("/initiatives/<int:iid>/delete", methods=["POST"])
@login_required
def initiative_delete(iid):
    ini = _initiative(iid)
    if not can_delete_initiative(ini):
        abort(403)
    name = ini.name
    db.session.delete(ini)
    db.session.commit()
    flash(f"Initiative « {name} » supprimée (les projets et modules sont conservés).", "info")
    return redirect(url_for("workgraph.initiatives"))


@bp.route("/initiatives/<int:iid>/link", methods=["POST"])
@login_required
def initiative_link(iid):
    _require_edit()
    ini = _initiative(iid)
    vis = {p.id: p for p in visible_projects(current_user)}
    n = 0
    for pid in (to_int(x) for x in request.form.getlist("project_ids")):
        p = vis.get(pid)
        if p is not None and p not in ini.projects:
            ini.projects.append(p)
            n += 1
    for mid in (to_int(x) for x in request.form.getlist("module_ids")):
        m = db.session.get(Module, mid or 0)
        if m is not None and m.project_id in vis and m not in ini.modules:
            ini.modules.append(m)
            n += 1
    db.session.commit()
    flash(f"{n} élément(s) lié(s) à l’initiative." if n else "Rien à lier.", "success" if n else "info")
    return redirect(_next(url_for("workgraph.initiative_detail", iid=ini.id)))


@bp.route("/initiatives/<int:iid>/unlink", methods=["POST"])
@login_required
def initiative_unlink(iid):
    _require_edit()
    ini = _initiative(iid)
    kind, oid = request.form.get("kind"), to_int(request.form.get("id"))
    if kind == "project":
        ini.projects = [p for p in ini.projects if p.id != oid]
    elif kind == "module":
        ini.modules = [m for m in ini.modules if m.id != oid]
    else:
        abort(400)
    db.session.commit()
    flash("Lien retiré.", "info")
    return redirect(_next(url_for("workgraph.initiative_detail", iid=ini.id)))


@bp.route("/initiatives/<int:iid>/updates", methods=["POST"])
@login_required
def initiative_update_add(iid):
    _require_edit()
    ini = _initiative(iid)
    s = W.Snapshot().initiative(ini)
    body = (request.form.get("body") or "").strip()
    health = request.form.get("health")
    if health not in HEALTH_LABELS:
        health = s["health"] or "on_track"
    if not body:
        flash("Écris quelques mots pour la mise à jour.", "error")
        return redirect(url_for("workgraph.initiative_detail", iid=ini.id) + "#updates")
    db.session.add(InitiativeUpdate(initiative=ini, author=current_user, health=health, body=body[:20000],
                                    progress=s["pct"]))
    db.session.commit()
    flash("Mise à jour publiée.", "success")
    return redirect(url_for("workgraph.initiative_detail", iid=ini.id) + "#updates")


@bp.route("/initiatives/<int:iid>/updates/<int:uid>/delete", methods=["POST"])
@login_required
def initiative_update_delete(iid, uid):
    _require_edit()
    ini = _initiative(iid)
    up = db.session.get(InitiativeUpdate, uid)
    if up is None or up.initiative_id != ini.id:
        abort(404)
    if not (current_user.is_admin or up.author_id == current_user.id):
        abort(403)
    db.session.delete(up)
    db.session.commit()
    flash("Mise à jour supprimée.", "info")
    return redirect(url_for("workgraph.initiative_detail", iid=ini.id) + "#updates")


# Utilisé par les gabarits : liste des projets visibles pour le formulaire de création
@bp.app_template_global("wg_projects_of")
def wg_projects_of(ini: Initiative, visible: set) -> list[Project]:
    return [p for p in ini.projects if p.id in visible]

"""Construction des tickets du projet EAE à partir des documents réellement produits par l'équipe.

Les tickets ont été rédigés en lisant les documents du dossier « Projet Pluri » (fiche de cadrage, notes de
réunion, matrices RGPD, benchmark, plan d'actions, mails prêts à envoyer…) — et non à partir du planning
prévisionnel Excel. Le fichier `seed_data/tickets_documents.json` contient ces tickets ; ce module les charge
dans un projet en joignant les documents sources aux tickets.
"""
from __future__ import annotations

import json
import mimetypes
import os
import shutil
import uuid
from datetime import date, datetime, time, timedelta
from pathlib import Path

from flask import current_app

from .extensions import db
from .models import (
    PALETTE, Activity, Attachment, Comment, Cycle, Issue, IssueRelation, Label, Module, Notification, Page,
    Project, SavedView, User, issue_labels, issue_modules,
)
from .services import add_relation, ensure_member, log_activity
from .utils import parse_date

DATA_FILE = Path(__file__).resolve().parent / "seed_data" / "tickets_documents.json"
ROOT = Path(__file__).resolve().parent.parent.parent  # dossier « Projet Pluri »

MODULES = [
    ("Cadrage & organisation", "Finalité, périmètre, fiche de cadrage, organisation de l’équipe et rituels.", "#3f76ff"),
    ("État de l’art & benchmark", "État de l’art du recueil de données et benchmark des solutions clés en main.", "#8b5cf6"),
    ("Recueil terrain (entretiens, sondages)", "Entretiens, questionnaires et sondages auprès des services, cadres et élèves.", "#14b8a6"),
    ("Cartographie des flux & procédures", "Flux de transmission des pièces, procédures, redondances et irritants.", "#f97316"),
    ("RGPD, droits & conformité", "Données sensibles et de santé, matrice des droits, cycle de vie, DPO.", "#ef4444"),
    ("Démonstrateur & outils", "Démonstrateur Démarches Numériques, guides, kit de mesure, données fictives.", "#22c55e"),
    ("Restitution & communication", "Points d’étape, restitutions, livrables et communication avec les interlocuteurs.", "#eab308"),
]
LABEL_COLORS = {
    "Livrable": "#3f76ff", "Réunion": "#64748b", "Entretien": "#14b8a6", "Sondage": "#06b6d4", "RGPD": "#ef4444",
    "Santé": "#ec4899", "Benchmark": "#8b5cf6", "Démonstrateur": "#22c55e", "Communication": "#eab308",
    "Jalon": "#f97316", "Documentation": "#6366f1", "Action": "#a855f7",
}
STATE_MAP = {"done": "completed", "review": "started", "in_progress": "started", "todo": "unstarted",
             "backlog": "backlog"}


def _state(project, kind):
    if kind == "review":
        for s in project.states:
            if s.name.lower() == "en revue":
                return s
    if kind == "in_progress":
        for s in project.states:
            if s.name.lower() == "en cours":
                return s
    if kind == "todo":
        return project.default_state()
    return project.first_state_of(STATE_MAP.get(kind, "unstarted")) or project.default_state()


def clear_project(project: Project):
    """Supprime tickets, modules, étiquettes et vues d'un projet (les cycles et pages sont conservés)."""
    ids = [i.id for i in Issue.query.filter_by(project_id=project.id)]
    if ids:
        atts = Attachment.query.filter(Attachment.issue_id.in_(ids)).all()
        own = [a.id for a in atts]
        for name in {a.stored_name for a in atts}:
            from .services import remove_attachment_file
            remove_attachment_file(name, exclude_ids=own)
        IssueRelation.query.filter(db.or_(IssueRelation.issue_id.in_(ids), IssueRelation.related_id.in_(ids))) \
            .delete(synchronize_session=False)
        Notification.query.filter(Notification.issue_id.in_(ids)).delete(synchronize_session=False)
        db.session.execute(issue_labels.delete().where(issue_labels.c.issue_id.in_(ids)))
        db.session.execute(issue_modules.delete().where(issue_modules.c.issue_id.in_(ids)))
        Issue.query.filter(Issue.id.in_(ids)).update({"parent_id": None, "intake_duplicate_of_id": None},
                                                     synchronize_session=False)
        db.session.flush()
        for i in Issue.query.filter(Issue.id.in_(ids)):
            db.session.delete(i)
    for m in list(project.modules):
        db.session.delete(m)
    for l in list(project.labels):
        db.session.delete(l)
    for v in db.session.query(SavedView).filter(SavedView.project_id == project.id):
        db.session.delete(v)
    Activity.query.filter(Activity.project_id == project.id, Activity.issue_id.is_(None)).delete()
    project.issue_sequence = 0
    db.session.flush()


_COPIES: dict = {}


def _attach(issue, rel_path, actor, when):
    src = ROOT / rel_path
    if not src.is_file():
        return None
    key = (issue.project_id, str(src))
    if key not in _COPIES:  # un même document cité par plusieurs tickets n'est copié qu'une fois
        folder = Path(current_app.config["UPLOAD_DIR"]) / str(issue.project_id)
        folder.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in src.name)
        stored = f"{uuid.uuid4().hex}_{safe}"
        shutil.copy2(src, folder / stored)
        _COPIES[key] = f"{issue.project_id}/{stored}"
    att = Attachment(issue=issue, filename=src.name, stored_name=_COPIES[key],
                     size=src.stat().st_size, mimetype=mimetypes.guess_type(src.name)[0] or "application/octet-stream",
                     uploaded_by=actor, created_at=when)
    db.session.add(att)
    return att


def load_documents(project: Project, actor: User, data: dict | None = None, attach=True, say=print) -> dict:
    """Charge les tickets issus des documents dans `project`. Retourne un petit rapport."""
    if data is None:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    _COPIES.clear()
    users = {u.username: u for u in User.query.all()}
    start_default = project.start_date or date.today()

    # Modules et étiquettes
    mods = {}
    for name, desc, _col in MODULES:
        m = Module(project=project, name=name, description=desc, status="planned")
        db.session.add(m)
        mods[name] = m
    labels = {}

    def label(name):
        if name not in labels:
            l = Label(project=project, name=name, color=LABEL_COLORS.get(name, PALETTE[len(labels) % len(PALETTE)]))
            db.session.add(l)
            labels[name] = l
        return labels[name]

    db.session.flush()
    by_ref, report = {}, {"tickets": 0, "attachments": 0, "relations": 0, "missing_docs": []}
    tickets = data.get("tickets", [])
    for t in tickets:
        seq = project.next_sequence()
        st = _state(project, t.get("state", "todo"))
        completed = parse_date(t.get("completed_date"))
        start = parse_date(t.get("start_date"))
        target = parse_date(t.get("target_date"))
        if t.get("state") == "done" and not completed:
            completed = target or start or start_default
        created_d = start or (completed - timedelta(days=max(1, (t.get("estimate_days") or 3))) if completed else None) \
            or start_default
        created_d = max(min(created_d, completed or created_d, date.today()), start_default - timedelta(days=7))
        created = datetime.combine(created_d, time(9, 0))
        issue = Issue(project=project, sequence_id=seq, title=t["title"][:255], description=t.get("description") or "",
                      priority=t.get("priority") if t.get("priority") in ("urgent", "high", "medium", "low", "none") else "none",
                      state=st, start_date=start, target_date=target, estimate=t.get("estimate_days"),
                      external_id=t.get("ref"), created_by=actor, created_at=created, updated_at=created,
                      sort_order=seq * 1000)
        if t.get("state") == "done" and completed:
            issue.completed_at = datetime.combine(completed, time(17, 0))
            issue.updated_at = issue.completed_at
        db.session.add(issue)
        db.session.flush()
        for uname in t.get("assignees") or []:
            u = users.get(uname)
            if u:
                ensure_member(project, u)
                issue.assignees.append(u)
                issue.subscribe(u)
        issue.subscribe(actor)
        m = mods.get(t.get("module"))
        if m:
            issue.modules.append(m)
        for ln in t.get("labels") or []:
            issue.labels.append(label(ln))
        a = log_activity(issue, actor, "imported", new="documents du projet")
        a.created_at = created
        if issue.completed_at:
            a2 = log_activity(issue, actor, "updated", "state", "À faire", st.name)
            a2.created_at = issue.completed_at
        elif t.get("state") in ("in_progress", "review"):
            a2 = log_activity(issue, actor, "updated", "state", "À faire", st.name)
            a2.created_at = created + timedelta(days=1)
        if attach:
            for rel in t.get("documents") or []:
                if _attach(issue, rel, actor, issue.completed_at or created):
                    report["attachments"] += 1
                else:
                    report["missing_docs"].append(rel)
        for txt in t.get("comments") or []:
            author = issue.assignees[0] if issue.assignees else actor
            db.session.add(Comment(issue=issue, author=author, body=txt,
                                   created_at=(issue.completed_at or created) + timedelta(minutes=5)))
        by_ref[t.get("ref")] = issue
        report["tickets"] += 1
    db.session.flush()
    # Hiérarchie et dépendances
    for t in tickets:
        issue = by_ref.get(t.get("ref"))
        parent = by_ref.get(t.get("parent"))
        if issue and parent and parent.id != issue.id:
            issue.parent = parent
        for dep in t.get("depends_on") or []:
            other = by_ref.get(dep)
            if issue and other and other.id != issue.id:
                if add_relation(issue, other, "blocked_by", actor):
                    report["relations"] += 1
    db.session.flush()
    # Modules : dates et statut d'après leurs tickets
    from .importer import refresh_module
    for m in mods.values():
        if not m.issues:
            db.session.delete(m)  # phase sans document : pas de module vide
        else:
            refresh_module(m)
    # Cycles : rattachement par échéance (ou date de fin)
    cycles = [c for c in project.cycles if c.start_date and c.end_date]
    for issue in by_ref.values():
        ref = issue.target_date or (issue.completed_at.date() if issue.completed_at else None)
        for c in cycles:
            if ref and c.start_date <= ref <= c.end_date:
                issue.cycle = c
                break
    # Pages proposées (comptes rendus, synthèses)
    existing = {p.title.lower() for p in project.pages}
    for pg in data.get("pages", []):
        title = (pg.get("title") or "").strip()
        if not title or title.lower() in existing:
            continue
        content = pg.get("summary_markdown")
        if not content and pg.get("source", "").endswith(".docx") and (ROOT / pg["source"]).is_file():
            from .importer import docx_to_markdown
            try:
                content = docx_to_markdown((ROOT / pg["source"]).read_bytes())
            except Exception:
                content = None
        if not content:
            continue
        if pg.get("source"):
            content += f"\n\n---\n_Source : `{pg['source']}`_\n"
        db.session.add(Page(project=project, title=title[:255], emoji=pg.get("emoji") or "📄", content=content,
                            owner=actor, updated_by=actor, is_public=True))
        existing.add(title.lower())
    # Vues enregistrées utiles
    views = [
        ("Mes tâches ouvertes", "assignee=me&state_group=unstarted&state_group=started&layout=list&group_by=state"),
        ("En retard", "target=overdue&layout=list&group_by=assignee"),
        ("À attribuer", "assignee=none&state_group=backlog&state_group=unstarted&layout=list&group_by=module"),
        ("Livrables produits", f"label={labels['Livrable'].id}&state_group=completed&layout=list&group_by=module"
         if "Livrable" in labels else "state_group=completed&layout=list&group_by=module"),
        ("Actions à mener", "state_group=backlog&state_group=unstarted&layout=board&group_by=module"),
        ("Planning (Gantt)", "layout=gantt&order_by=start"),
    ]
    if "Jalon" in labels:
        views.append(("Jalons", f"label={labels['Jalon'].id}&layout=list&group_by=state&order_by=target"))
    for name, q in views:
        db.session.add(SavedView(project=project, name=name, query=q, owner=actor, is_public=True))
    # Pas de notifications en rafale pour un chargement initial
    Notification.query.filter(Notification.project_id == project.id).delete()
    db.session.flush()
    say(f"✓ {report['tickets']} tickets créés d’après les documents, {report['attachments']} documents joints, "
        f"{report['relations']} dépendances")
    if report["missing_docs"]:
        absents = sorted(set(report["missing_docs"]))
        say(f"  ! {len(absents)} document(s) source non joint(s) : les fichiers du dossier « Projet Pluri » "
            f"ne sont pas dans le dépôt (cherchés dans {ROOT}). Les tickets sont créés quand même ; pour joindre "
            f"les documents, place ce dossier à côté de hangar/ puis relance « hangar rebuild-docs ». "
            f"Exemples : {', '.join(absents[:3])}")
    return report

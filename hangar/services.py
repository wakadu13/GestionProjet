"""Logique métier : création/modification de tickets, historique, notifications."""
from __future__ import annotations

import hashlib
from datetime import date

from flask import url_for
from markupsafe import Markup, escape

from .extensions import db
from .models import (
    PALETTE, PRIORITY_LABELS, Activity, Cycle, Issue, IssueRelation, Label, Module, Notification,
    Project, ProjectMember, State, User, estimate_label, now,
)
from .utils import can, extract_mentions, fmt_date, fmt_minutes, parse_date, single_line

DEFAULT_STATES = [
    ("Backlog", "#a3a3a3", "backlog", False),
    ("À faire", "#3f76ff", "unstarted", True),
    ("En cours", "#f59e0b", "started", False),
    ("En revue", "#8b5cf6", "started", False),
    ("Terminé", "#16a34a", "completed", False),
    ("Annulé", "#dc2626", "cancelled", False),
]


def clean_title(value) -> str:
    """Titre de ticket sur une seule ligne : les retours à la ligne (cellule Excel avec Alt+Entrée, API…) et
    les espaces en trop deviennent une seule espace. Un retour à la ligne casserait l'objet des e-mails de
    notification et l'export .ics."""
    from .utils import single_line  # caractères de contrôle (tabulation, saut de ligne collé…) compris
    return " ".join(single_line(str(value or "")).split())[:255]


def to_int(v):
    try:
        if v in (None, "", "none", "null"):
            return None
        return int(v)
    except (TypeError, ValueError):
        return None


def to_int_list(v) -> list[int]:
    if v is None:
        return []
    if isinstance(v, (str, int)):
        v = [x for x in str(v).split(",") if x]
    out = []
    for x in v:
        i = to_int(x)
        if i is not None and i not in out:
            out.append(i)
    return out


# ---------------------------------------------------------------------------
# Projets
# ---------------------------------------------------------------------------

def create_project(name, identifier, creator, description="", emoji="🚀", color="#3f76ff",
                   network="public", lead=None, member_ids=(), start_date=None, target_date=None,
                   estimate_type="points"):
    project = Project(
        name=name.strip(), identifier=identifier.strip().upper(), description=description,
        emoji=emoji or "🚀", color=color, network=network, created_by=creator, lead=lead or creator,
        start_date=start_date, target_date=target_date, estimate_type=estimate_type,
    )
    db.session.add(project)
    for i, (sname, scolor, group, default) in enumerate(DEFAULT_STATES):
        db.session.add(State(project=project, name=sname, color=scolor, group=group,
                             sequence=(i + 1) * 1000, is_default=default))
    db.session.add(ProjectMember(project=project, user=creator, role="admin"))
    added = {creator.id}
    for uid in member_ids:
        if uid not in added:
            u = db.session.get(User, uid)
            if u:
                db.session.add(ProjectMember(project=project, user=u, role="member" if u.role != "guest" else "guest"))
                added.add(uid)
    db.session.flush()
    log_activity(None, creator, "project_created", project=project, new=project.name)
    return project


def ensure_member(project: Project, user: User, role="member"):
    m = ProjectMember.query.filter_by(project_id=project.id, user_id=user.id).first()
    if m is None:
        m = ProjectMember(project=project, user=user, role="guest" if user.role == "guest" else role)
        db.session.add(m)
    return m


# ---------------------------------------------------------------------------
# Historique & notifications
# ---------------------------------------------------------------------------

def log_activity(issue, actor, verb, field=None, old=None, new=None, project=None):
    a = Activity(issue=issue, project=project or (issue.project if issue else None), actor=actor, verb=verb,
                 field=field, old_value=None if old is None else str(old),
                 new_value=None if new is None else str(new))
    db.session.add(a)
    if issue is not None:
        # Toute action tracée (temps saisi, sous-ticket, lien, pièce jointe, relation…) compte comme une
        # activité du ticket : la fermeture automatique ne doit pas le croire abandonné.
        issue.updated_at = now()
    return a


def unsubscribe_from_project(project: Project, user: User) -> None:
    """Retire `user` des abonnés de tous les tickets du projet (il le quitte ou en est retiré)."""
    from .models import issue_subscribers
    ids = [i for (i,) in db.session.query(Issue.id).filter(Issue.project_id == project.id)]
    if ids:
        db.session.execute(issue_subscribers.delete().where(issue_subscribers.c.user_id == user.id,
                                                            issue_subscribers.c.issue_id.in_(ids)))


def notify(users, actor, issue, kind, message, url=None, project=None):
    seen = set()
    scope = project or (issue.project if issue else None)
    for u in users:
        if u is None or not u.active or (actor is not None and u.id == actor.id) or u.id in seen:
            continue
        seen.add(u.id)
        if scope is not None and not can(u, "view", scope):
            continue  # retiré du projet, parti, projet devenu privé, invité cité : rien ne doit lui parvenir
        n = Notification(
            user=u, actor=actor, issue=issue, project=project or (issue.project if issue else None),
            kind=kind, message=message[:500],
            url=url or (url_for("issues.detail", pid=issue.project_id, seq=issue.sequence_id) if issue else None),
        )
        db.session.add(n)
        try:  # e-mail éventuel (simple mise en file, jamais bloquant)
            from .mailer import on_notification
            on_notification(u, actor, issue, kind, n.message, n.url)
        except Exception:
            pass


def notify_subscribers(issue, actor, activity, kind="update"):
    text = activity_plain(activity)
    notify(issue.subscribers, actor, issue, kind,
           f"{actor.display_name if actor else 'Quelqu’un'} {text} · {issue.key} {issue.title}")


def handle_mentions(text, issue, actor, where="un commentaire"):
    users = [u for u in extract_mentions(text) if can(u, "view", issue.project)]
    if users:
        for u in users:
            issue.subscribe(u)
        notify(users, actor, issue, "mention",
               f"{actor.display_name} t’a mentionné dans {where} · {issue.key} {issue.title}")


ACT_FIELDS = {
    "title": "le titre", "description": "la description", "state": "l’état", "priority": "la priorité",
    "start_date": "la date de début", "target_date": "l’échéance", "estimate": "l’estimation",
    "cycle": "le cycle", "parent": "le ticket parent",
}


def _b(v):
    return f"<b>{escape(v)}</b>"


def activity_text(a: Activity) -> Markup:
    """Phrase HTML décrivant une activité (sans l’auteur)."""
    v, f, old, new = a.verb, a.field, a.old_value or "", a.new_value or ""
    if v == "created":
        s = "a créé le ticket"
    elif v == "project_created":
        s = f"a créé le projet {_b(new)}"
    elif v == "commented":
        s = "a commenté"
    elif v == "updated":
        if f == "title":
            s = f"a renommé le ticket en {_b(new)}"
        elif f == "description":
            s = "a mis à jour la description"
        elif f == "state":
            s = f"a changé l’état de {_b(old)} à {_b(new)}" if old else f"a défini l’état sur {_b(new)}"
        elif f == "priority":
            s = f"a défini la priorité sur {_b(PRIORITY_LABELS.get(new, new))}"
        elif f in ("assignees", "labels", "modules"):
            name = new[1:]
            adding = new.startswith("+")
            if f == "assignees":
                s = f"a assigné {_b(name)}" if adding else f"a retiré {_b(name)} des assignés"
            elif f == "labels":
                s = f"a ajouté l’étiquette {_b(name)}" if adding else f"a retiré l’étiquette {_b(name)}"
            else:
                s = f"a ajouté le ticket au module {_b(name)}" if adding else f"a retiré le ticket du module {_b(name)}"
        elif f in ("start_date", "target_date"):
            label = ACT_FIELDS[f]
            s = f"a fixé {label} au {_b(fmt_date(parse_date(new), True))}" if new else f"a retiré {label}"
        elif f == "estimate":
            s = f"a estimé le ticket à {_b(new)}" if new else "a retiré l’estimation"
        elif f == "cycle":
            s = f"a ajouté le ticket au cycle {_b(new)}" if new else f"a retiré le ticket du cycle {_b(old)}"
        elif f == "parent":
            s = f"a défini le ticket parent {_b(new)}" if new else f"a retiré le ticket parent {_b(old)}"
        elif f == "project":
            s = f"a déplacé le ticket depuis le projet {_b(old)}"
        else:
            s = f"a modifié {escape(ACT_FIELDS.get(f, f or ''))}"
    elif v == "attachment_added":
        s = f"a ajouté la pièce jointe {_b(new)}"
    elif v == "attachment_removed":
        s = f"a supprimé la pièce jointe {_b(old)}"
    elif v == "link_added":
        s = f"a ajouté le lien {_b(new)}"
    elif v == "link_removed":
        s = f"a supprimé le lien {_b(old)}"
    elif v == "relation_added":
        s = f"a ajouté la relation « {escape(f)} » avec {_b(new)}"
    elif v == "relation_removed":
        s = f"a supprimé la relation « {escape(f)} » avec {_b(old)}"
    elif v == "subissue_added":
        s = f"a ajouté le sous-ticket {_b(new)}"
    elif v == "subissue_removed":
        s = f"a détaché le sous-ticket {_b(old)}"
    elif v == "worklog":
        s = f"a enregistré {_b(new)} de travail"
    elif v == "archived":
        s = "a archivé le ticket"
    elif v == "restored":
        s = "a restauré le ticket"
    elif v == "duplicated":
        s = f"a créé ce ticket en dupliquant {_b(old)}"
    elif v == "intake_created":
        s = "a soumis ce ticket dans l’intake"
    elif v == "intake_accepted":
        s = "a accepté le ticket depuis l’intake"
    elif v == "intake_declined":
        s = "a refusé le ticket dans l’intake"
    elif v == "intake_snoozed":
        s = f"a reporté le ticket au {_b(new)}"
    elif v == "intake_duplicate":
        s = f"a marqué le ticket comme doublon de {_b(new)}"
    elif v == "emailed":
        s = f"a envoyé un e-mail {escape(new)}"
    elif v == "imported":
        s = f"a importé le ticket ({escape(new)})"
    else:
        s = escape(v)
    return Markup(s)


def activity_plain(a: Activity) -> str:
    import re
    return re.sub(r"<[^>]+>", "", str(activity_text(a)))


# ---------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------

def text_version(*parts) -> str:
    """Empreinte courte d'un texte tel qu'il a été chargé (description, titre + contenu d'une page). Renvoyée par
    le formulaire, elle montre si quelqu'un a modifié le texte entre-temps. Les fins de ligne Windows et les
    blancs aux extrémités ne comptent pas."""
    norm = "\x00".join((p or "").replace("\r\n", "\n").strip() for p in parts)
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:20]


def description_conflict(issue: Issue, data: dict) -> bool:
    """Contrôle avant d'écrire `data["description"]`. `data["description_version"]` (retiré de `data`) est
    l'empreinte de la description chargée par l'éditeur ; sans elle, pas de contrôle (anciens clients).
    Renvoie True si la description a changé depuis et que le texte envoyé l'écraserait. Si l'éditeur n'a rien
    changé au texte chargé, la description actuelle est gardée (la clé est retirée de `data`)."""
    base = data.pop("description_version", None)
    if "description" not in data or not base:
        return False
    current = text_version(issue.description)
    sent = text_version(data.get("description"))
    if base == current or sent == current:
        return False
    if sent == base:  # texte inchangé dans cet éditeur : ne pas remettre l'ancienne version
        data.pop("description")
        return False
    return True


def description_conflict_message(issue: Issue, user: User) -> str:
    """Message affiché quand `description_conflict` refuse un enregistrement."""
    a = (Activity.query.filter_by(issue_id=issue.id, verb="updated", field="description")
         .order_by(Activity.id.desc()).first())
    who = a.actor if a else None
    if who is not None and who.id == user.id:
        what = "Tu as modifié la description ailleurs (autre onglet ou autre appareil) pendant que tu l’écrivais ici."
    elif who is not None:
        what = f"{who.display_name} a modifié la description pendant que tu l’écrivais."
    else:
        what = "Quelqu’un a modifié la description pendant que tu l’écrivais."
    return (what + " Rien n’a été écrasé : compare ton texte avec la version actuelle, reprends ce qui manque, "
            "puis enregistre à nouveau.")


def _descendant_ids(issue: Issue) -> set[int]:
    out, stack = set(), list(issue.children)
    while stack:
        c = stack.pop()
        if c.id in out:
            continue
        out.add(c.id)
        stack.extend(c.children)
    return out


def update_issue(issue: Issue, data: dict, actor: User, log: bool = True) -> list[str]:
    """Applique des modifications validées et trace l’historique. Retourne les champs modifiés."""
    project = issue.project
    changed: list[str] = []
    acts: list[Activity] = []

    def rec(field, old, new):
        changed.append(field)
        if log:
            acts.append(log_activity(issue, actor, "updated", field, old, new))

    if "title" in data:
        t = clean_title(data.get("title"))
        if t and t != issue.title:
            rec("title", issue.title, t)
            issue.title = t

    if "description" in data:
        d = data.get("description") or ""
        if d.strip() != (issue.description or "").strip():
            rec("description", issue.description or "", d)  # l'ancien texte reste dans l'historique
            issue.description = d
            handle_mentions(d, issue, actor, "la description")

    if "state_id" in data:
        st = db.session.get(State, to_int(data["state_id"]) or 0)
        if st and st.project_id == project.id and st.id != issue.state_id:
            old = issue.state
            rec("state", old.name if old else "", st.name)
            issue.state = st
            if st.group == "completed":
                issue.completed_at = now()
            elif old is not None and old.group == "completed":
                issue.completed_at = None

    if "priority" in data:
        p = data.get("priority") or "none"
        if p in PRIORITY_LABELS and p != issue.priority:
            rec("priority", issue.priority, p)
            issue.priority = p

    if "assignee_ids" in data:
        allowed = {m.user_id: m.user for m in project.members}
        wanted = [allowed[i] for i in to_int_list(data["assignee_ids"]) if i in allowed]
        current = {u.id for u in issue.assignees}
        new_ids = {u.id for u in wanted}
        added = [u for u in wanted if u.id not in current]
        removed = [u for u in issue.assignees if u.id not in new_ids]
        for u in removed:
            issue.assignees.remove(u)
            rec("assignees", None, "-" + u.display_name)
        for u in added:
            issue.assignees.append(u)
            issue.subscribe(u)
            rec("assignees", None, "+" + u.display_name)
        if added:
            notify(added, actor, issue, "assigned",
                   f"{actor.display_name} t’a assigné {issue.key} · {issue.title}")

    if "label_ids" in data:
        allowed = {l.id: l for l in project.labels}
        wanted = [allowed[i] for i in to_int_list(data["label_ids"]) if i in allowed]
        new_ids = {l.id for l in wanted}
        current = {l.id for l in issue.labels}
        for l in [l for l in issue.labels if l.id not in new_ids]:
            issue.labels.remove(l)
            rec("labels", None, "-" + l.name)
        for l in wanted:
            if l.id not in current:
                issue.labels.append(l)
                rec("labels", None, "+" + l.name)

    if "module_ids" in data:
        allowed = {m.id: m for m in project.modules}
        wanted = [allowed[i] for i in to_int_list(data["module_ids"]) if i in allowed]
        new_ids = {m.id for m in wanted}
        current = {m.id for m in issue.modules}
        for m in [m for m in issue.modules if m.id not in new_ids]:
            issue.modules.remove(m)
            rec("modules", None, "-" + m.name)
        for m in wanted:
            if m.id not in current:
                issue.modules.append(m)
                rec("modules", None, "+" + m.name)

    for field in ("start_date", "target_date"):
        if field in data:
            d = parse_date(data.get(field))
            if d != getattr(issue, field):
                rec(field, getattr(issue, field), d.isoformat() if d else "")
                setattr(issue, field, d)

    if "estimate" in data:
        e = to_int(data.get("estimate"))
        if e != issue.estimate:
            rec("estimate", estimate_label(project.estimate_type, issue.estimate),
                estimate_label(project.estimate_type, e))
            issue.estimate = e

    if "cycle_id" in data:
        cid = to_int(data.get("cycle_id"))
        cyc = db.session.get(Cycle, cid) if cid else None
        if cyc is not None and cyc.project_id != project.id:
            cyc = None
        if (cyc.id if cyc else None) != issue.cycle_id:
            rec("cycle", issue.cycle.name if issue.cycle else "", cyc.name if cyc else "")
            issue.cycle = cyc

    if "parent_id" in data:
        pid = to_int(data.get("parent_id"))
        parent = db.session.get(Issue, pid) if pid else None
        if parent is not None and (parent.project_id != project.id or parent.id == issue.id
                                   or parent.id in _descendant_ids(issue)):
            parent = None
        if (parent.id if parent else None) != issue.parent_id:
            rec("parent", issue.parent.key if issue.parent else "", parent.key if parent else "")
            if parent is not None:
                log_activity(parent, actor, "subissue_added", new=issue.key)
            elif issue.parent is not None:
                log_activity(issue.parent, actor, "subissue_removed", old=issue.key)
            issue.parent = parent

    if changed:
        issue.updated_at = now()
        issue.subscribe(actor)
        for a in acts:
            if a.field not in ("assignees",):  # l’assignation a sa propre notification
                notify_subscribers(issue, actor, a)
    return changed


def create_issue(project: Project, data: dict, actor: User, intake: bool = False) -> Issue:
    title = clean_title(data.get("title")) or "Sans titre"
    max_sort = db.session.query(db.func.max(Issue.sort_order)).filter(Issue.project_id == project.id).scalar()
    issue = Issue(sequence_id=project.next_sequence(), title=title, description=data.get("description") or "",
                  priority="none", created_by=actor, sort_order=(max_sort or 0) + 1000, project_id=project.id)
    db.session.add(issue)
    issue.project = project
    if intake:
        issue.intake_status = "pending"
        issue.intake_source = data.get("intake_source") or "Formulaire"
        issue.state = project.first_state_of("backlog") or project.default_state()
    else:
        issue.state = project.default_state()
    db.session.flush()
    log_activity(issue, actor, "intake_created" if intake else "created")
    rest = {k: v for k, v in data.items() if k not in ("title", "description")}
    if "assignee_ids" not in rest and project.default_assignee_id and not intake:
        rest["assignee_ids"] = [project.default_assignee_id]
    update_issue(issue, rest, actor, log=False)
    issue.subscribe(actor)
    handle_mentions(issue.description, issue, actor, "un nouveau ticket")
    if intake:
        admins = [m.user for m in project.members if m.role == "admin"]
        notify(admins, actor, issue, "intake",
               f"{actor.display_name} a soumis « {issue.title} » dans l’intake de {project.name}",
               url=url_for("intake.index", pid=project.id, issue=issue.id))
    return issue


def duplicate_issue(issue: Issue, actor: User) -> Issue:
    data = {
        "title": f"{issue.title} (copie)", "description": issue.description, "state_id": issue.state_id,
        "priority": issue.priority, "assignee_ids": [u.id for u in issue.assignees],
        "label_ids": [l.id for l in issue.labels], "module_ids": [m.id for m in issue.modules],
        "start_date": issue.start_date, "target_date": issue.target_date, "estimate": issue.estimate,
        "cycle_id": issue.cycle_id, "parent_id": issue.parent_id,
    }
    new = create_issue(issue.project, data, actor)
    log_activity(new, actor, "duplicated", old=issue.key)
    return new


def move_issue_to_project(issue: Issue, target: Project, actor: User) -> Issue:
    old_project = issue.project
    old_key = issue.key
    # Numéro pris AVANT de changer le ticket de projet : next_sequence écrit en base (flush), et le ticket ne
    # doit pas y arriver dans le projet cible avec son ancien numéro.
    seq = target.next_sequence()
    issue.project = target
    issue.sequence_id = seq
    st = target.first_state_of(issue.state_group) or target.default_state()
    issue.state = st
    issue.labels = [l2 for l in issue.labels for l2 in target.labels if l2.name.lower() == l.name.lower()]
    issue.modules = []
    issue.cycle = None
    issue.parent = None
    member_ids = {m.user_id for m in target.members}
    issue.assignees = [u for u in issue.assignees if u.id in member_ids]
    for c in list(issue.children):
        c.parent = None
    IssueRelation.query.filter(db.or_(IssueRelation.issue_id == issue.id,
                                      IssueRelation.related_id == issue.id)).delete()
    for a in issue.activities:
        a.project = target
    log_activity(issue, actor, "updated", "project", f"{old_project.name} ({old_key})", target.name)
    issue.updated_at = now()
    return issue


def add_relation(issue: Issue, other: Issue, rtype: str, actor: User):
    from .models import RELATION_TYPES
    if other.id == issue.id or rtype not in RELATION_TYPES:
        return None
    exists = IssueRelation.query.filter(
        db.or_(db.and_(IssueRelation.issue_id == issue.id, IssueRelation.related_id == other.id),
               db.and_(IssueRelation.issue_id == other.id, IssueRelation.related_id == issue.id))).first()
    if exists:
        return None
    rel = IssueRelation(issue=issue, related=other, relation_type=rtype)
    db.session.add(rel)
    label_src, label_dst = RELATION_TYPES[rtype]
    log_activity(issue, actor, "relation_added", label_src, None, other.key)
    log_activity(other, actor, "relation_added", label_dst, None, issue.key)
    return rel


def compute_sort_order(prev_issue: Issue | None, next_issue: Issue | None) -> float:
    if prev_issue and next_issue:
        return (prev_issue.sort_order + next_issue.sort_order) / 2
    if prev_issue:
        return prev_issue.sort_order + 1000
    if next_issue:
        return next_issue.sort_order - 1000
    return 65535


def move_issue(issue: Issue, group_by: str, from_key, to_key, prev_id, next_id, actor: User):
    """Glisser-déposer (tableau kanban / liste) : change la valeur du regroupement + l’ordre manuel."""
    data = {}
    if from_key != to_key and to_key is not None:
        if group_by == "state":
            data["state_id"] = to_key
        elif group_by == "state_group":
            st = issue.project.first_state_of(to_key)
            if st:
                data["state_id"] = st.id
        elif group_by == "priority":
            data["priority"] = to_key
        elif group_by == "cycle":
            data["cycle_id"] = None if to_key == "none" else to_key
        elif group_by in ("assignee", "label", "module"):
            attr = {"assignee": ("assignees", "assignee_ids"), "label": ("labels", "label_ids"),
                    "module": ("modules", "module_ids")}[group_by]
            ids = [o.id for o in getattr(issue, attr[0])]
            fk, tk = to_int(from_key), to_int(to_key)
            if fk in ids:
                ids.remove(fk)
            if to_key == "none":
                ids = []
            elif tk is not None and tk not in ids:
                ids.append(tk)
            data[attr[1]] = ids
    changed = update_issue(issue, data, actor) if data else []
    prev_issue = db.session.get(Issue, to_int(prev_id)) if to_int(prev_id) else None
    next_issue = db.session.get(Issue, to_int(next_id)) if to_int(next_id) else None
    if prev_issue or next_issue:
        issue.sort_order = compute_sort_order(prev_issue, next_issue)
    return changed


def archive_issue(issue: Issue, actor: User):
    issue.archived_at = now()
    log_activity(issue, actor, "archived")


def restore_issue(issue: Issue, actor: User):
    issue.archived_at = None
    log_activity(issue, actor, "restored")


def delete_issue(issue: Issue) -> None:
    """Suppression définitive d'un ticket (fiche, suppression en masse, API) : ses fichiers joints quittent le
    disque, ses relations disparaissent avec lui. Le droit est vérifié par l'appelant (utils.can_delete_issues)."""
    own = [a.id for a in issue.attachments]
    for a in issue.attachments:
        remove_attachment_file(a.stored_name, exclude_ids=own)
    IssueRelation.query.filter(db.or_(IssueRelation.issue_id == issue.id,
                                      IssueRelation.related_id == issue.id)).delete()
    for c in list(issue.children):
        c.parent = None
    db.session.delete(issue)


def remove_attachment_file(stored_name: str, exclude_ids=()) -> None:
    """Supprime un fichier joint du disque s'il n'est plus utilisé par une autre pièce jointe."""
    import os
    from flask import current_app
    from .models import Attachment
    q = Attachment.query.filter(Attachment.stored_name == stored_name)
    if exclude_ids:
        q = q.filter(~Attachment.id.in_(list(exclude_ids)))
    if q.first() is None:
        path = os.path.join(current_app.config["UPLOAD_DIR"], stored_name)
        if os.path.exists(path):
            os.remove(path)


def run_automations(project: Project):
    """Archivage / fermeture automatiques (appelé à l’ouverture du projet)."""
    from datetime import timedelta
    changed = 0
    if project.auto_archive_months:
        limit = now() - timedelta(days=30 * project.auto_archive_months)
        for i in Issue.query.join(State, Issue.state_id == State.id).filter(
                Issue.project_id == project.id, Issue.archived_at.is_(None),
                State.group.in_(("completed", "cancelled")), Issue.updated_at < limit):
            i.archived_at = now()
            log_activity(i, None, "archived")
            changed += 1
    if project.auto_close_months and not project.is_archived:
        months = project.auto_close_months
        limit = now() - timedelta(days=30 * months)
        cancelled = project.first_state_of("cancelled")
        open_groups = ("backlog", "unstarted", "started")
        if cancelled:
            for i in Issue.query.join(State, Issue.state_id == State.id).filter(
                    Issue.project_id == project.id, Issue.archived_at.is_(None),
                    Issue.is_visible_clause(),  # une demande d'invité en attente n'est pas abandonnée
                    State.group.in_(open_groups), Issue.updated_at < limit):
                if any(c.archived_at is None and c.state_group in open_groups for c in i.children):
                    continue  # le ticket avance par ses sous-tickets
                old = i.state.name
                i.state = cancelled
                log_activity(i, None, "updated", "state", old, cancelled.name)
                notify(list(i.assignees) + list(i.subscribers), None, i, "update",
                       f"Fermé automatiquement après {months} mois sans activité · {i.key} {i.title}")
                changed += 1
    if changed:
        db.session.commit()
    return changed


def get_or_create_label(project: Project, name: str) -> Label:
    name = name.strip()[:80]
    for l in project.labels:
        if l.name.lower() == name.lower():
            return l
    l = Label(project=project, name=name, color=PALETTE[len(project.labels) % len(PALETTE)])
    db.session.add(l)
    project.labels.append(l)
    return l


def get_or_create_module(project: Project, name: str) -> Module:
    name = name.strip()[:160]
    for m in project.modules:
        if m.name.lower() == name.lower():
            return m
    m = Module(project=project, name=name, status="planned")
    db.session.add(m)
    project.modules.append(m)
    return m


def serialize_issue(i: Issue) -> dict:
    return {
        "id": i.id, "key": i.key, "project_id": i.project_id, "sequence_id": i.sequence_id,
        "title": i.title, "description": i.description,
        "description_version": text_version(i.description),  # à renvoyer avec une nouvelle description
        "state": {"id": i.state.id, "name": i.state.name, "group": i.state.group, "color": i.state.color}
        if i.state else None,
        "priority": i.priority,
        "assignees": [{"id": u.id, "name": u.display_name, "username": u.username} for u in i.assignees],
        "labels": [{"id": l.id, "name": l.name, "color": l.color} for l in i.labels],
        "modules": [{"id": m.id, "name": m.name} for m in i.modules],
        "cycle": {"id": i.cycle.id, "name": i.cycle.name} if i.cycle else None,
        "parent": i.parent.key if i.parent else None,
        "start_date": i.start_date.isoformat() if i.start_date else None,
        "target_date": i.target_date.isoformat() if i.target_date else None,
        "estimate": i.estimate,
        "time_spent": fmt_minutes(i.time_spent_minutes),
        "created_by": i.created_by.username if i.created_by else None,
        "created_at": i.created_at.isoformat() if i.created_at else None,
        "updated_at": i.updated_at.isoformat() if i.updated_at else None,
        "completed_at": i.completed_at.isoformat() if i.completed_at else None,
        "archived": i.archived_at is not None,
        "url": url_for("issues.detail", pid=i.project_id, seq=i.sequence_id),
    }


def today():
    return date.today()

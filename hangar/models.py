"""Modèle de données de Hangar.

Hiérarchie : Espace de travail > Projets > Tickets (work items), avec cycles (sprints),
modules, vues enregistrées, pages (wiki), intake (triage), notifications, temps passé.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import date, datetime, timedelta

from flask_login import UserMixin
from sqlalchemy import func
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db


def now() -> datetime:
    return datetime.now().replace(microsecond=0)


# ---------------------------------------------------------------------------
# Référentiels
# ---------------------------------------------------------------------------

STATE_GROUPS = [
    ("backlog", "Backlog", "#a3a3a3"),
    ("unstarted", "Non commencé", "#3f76ff"),
    ("started", "En cours", "#f59e0b"),
    ("completed", "Terminé", "#16a34a"),
    ("cancelled", "Annulé", "#dc2626"),
]
STATE_GROUP_LABELS = {k: v for k, v, _ in STATE_GROUPS}
STATE_GROUP_COLORS = {k: c for k, _, c in STATE_GROUPS}
STATE_GROUP_ORDER = {k: i for i, (k, _, _) in enumerate(STATE_GROUPS)}
CLOSED_GROUPS = ("completed", "cancelled")

PRIORITIES = [
    ("urgent", "Urgente", "#ef4444"),
    ("high", "Haute", "#f97316"),
    ("medium", "Moyenne", "#eab308"),
    ("low", "Basse", "#3b82f6"),
    ("none", "Aucune", "#a3a3a3"),
]
PRIORITY_LABELS = {k: v for k, v, _ in PRIORITIES}
PRIORITY_COLORS = {k: c for k, _, c in PRIORITIES}
PRIORITY_ORDER = {k: i for i, (k, _, _) in enumerate(PRIORITIES)}

MODULE_STATUSES = [
    ("backlog", "Backlog", "#a3a3a3"),
    ("planned", "Planifié", "#3f76ff"),
    ("in-progress", "En cours", "#f59e0b"),
    ("paused", "En pause", "#737373"),
    ("completed", "Terminé", "#16a34a"),
    ("cancelled", "Annulé", "#dc2626"),
]
MODULE_STATUS_LABELS = {k: v for k, v, _ in MODULE_STATUSES}
MODULE_STATUS_COLORS = {k: c for k, _, c in MODULE_STATUSES}

ROLES = {"admin": "Administrateur", "member": "Membre", "guest": "Invité"}
ROLE_RANK = {"guest": 1, "member": 2, "admin": 3}

ESTIMATE_TYPES = {
    "none": ("Désactivées", []),
    "points": ("Points (0 à 8)", [(v, str(v)) for v in range(0, 9)]),
    "fibonacci": ("Fibonacci", [(v, str(v)) for v in (1, 2, 3, 5, 8, 13, 21)]),
    "tshirt": ("Tailles de t-shirt", list(zip(range(1, 7), ["XS", "S", "M", "L", "XL", "XXL"]))),
    "hours": ("Heures", [(v, f"{v} h") for v in (1, 2, 3, 4, 6, 8, 12, 16, 24, 40)]),
    "days": ("Jours-homme", [(v, f"{v} j") for v in (1, 2, 3, 4, 5, 6, 7, 8, 10, 12, 15, 20)]),
}

INTAKE_STATUSES = {
    "pending": "En attente",
    "accepted": "Accepté",
    "declined": "Refusé",
    "snoozed": "Reporté",
    "duplicate": "Doublon",
}

RELATION_TYPES = {
    # type: (libellé côté source, libellé côté cible)
    "blocked_by": ("Bloqué par", "Bloque"),
    "relates_to": ("Lié à", "Lié à"),
    "duplicate_of": ("Doublon de", "A pour doublon"),
}

PALETTE = [
    "#3f76ff", "#8b5cf6", "#ec4899", "#ef4444", "#f97316", "#eab308",
    "#22c55e", "#14b8a6", "#06b6d4", "#0ea5e9", "#6366f1", "#a855f7",
    "#f43f5e", "#84cc16", "#64748b", "#d946ef",
]


def estimate_label(estimate_type: str, value) -> str:
    if value is None:
        return ""
    for v, label in ESTIMATE_TYPES.get(estimate_type, ("", []))[1]:
        if v == value:
            return label
    return str(value)


# ---------------------------------------------------------------------------
# Tables d'association
# ---------------------------------------------------------------------------

def _assoc(name, left, right):
    return db.Table(
        name,
        db.Column(f"{left}_id", db.Integer, db.ForeignKey(f"{left}.id", ondelete="CASCADE"), primary_key=True),
        db.Column(f"{right}_id", db.Integer, db.ForeignKey(f"{right}.id", ondelete="CASCADE"), primary_key=True),
    )


issue_assignees = _assoc("issue_assignees", "issue", "user")
issue_labels = _assoc("issue_labels", "issue", "label")
issue_modules = _assoc("issue_modules", "issue", "module")
issue_subscribers = _assoc("issue_subscribers", "issue", "user")
module_members = _assoc("module_members", "module", "user")


# ---------------------------------------------------------------------------
# Espace de travail et utilisateurs
# ---------------------------------------------------------------------------

class Workspace(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, default="Mon espace")
    emoji = db.Column(db.String(16), default="✈️")
    description = db.Column(db.Text, default="")
    allow_signup = db.Column(db.Boolean, default=False)
    week_starts_monday = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=now)

    @staticmethod
    def get() -> "Workspace":
        ws = db.session.get(Workspace, 1)
        if ws is None:
            ws = Workspace(id=1)
            db.session.add(ws)
            db.session.commit()
        return ws


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(200), unique=True, nullable=False)
    username = db.Column(db.String(60), unique=True, nullable=False)
    first_name = db.Column(db.String(80), default="")
    last_name = db.Column(db.String(80), default="")
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), default="member")  # rôle dans l'espace
    job_title = db.Column(db.String(120), default="")
    bio = db.Column(db.Text, default="")
    avatar_color = db.Column(db.String(10), default="#3f76ff")
    theme = db.Column(db.String(10), default="system")
    capacity_hours = db.Column(db.Integer, default=35)  # capacité hebdomadaire
    active = db.Column(db.Boolean, default=True)
    must_change_password = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=now)
    last_login_at = db.Column(db.DateTime)
    last_seen_notifications = db.Column(db.DateTime)
    # E-mails (facultatif : la connexion reste par identifiant + mot de passe)
    contact_email = db.Column(db.String(200))
    email_prefs = db.Column(db.String(10), default="instant")  # instant | digest | off
    email_reminders = db.Column(db.Boolean, default=True)

    memberships = db.relationship("ProjectMember", back_populates="user", cascade="all, delete-orphan")

    # Flask-Login
    @property
    def is_active(self):  # type: ignore[override]
        return bool(self.active)

    @staticmethod
    def internal_email(username: str) -> str:
        """Valeur technique : les comptes se connectent par identifiant, sans adresse e-mail."""
        return f"{username}@hangar.local"

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    @property
    def display_name(self) -> str:
        full = f"{self.first_name or ''} {self.last_name or ''}".strip()
        return full or self.username

    @property
    def short_name(self) -> str:
        return self.first_name or self.last_name or self.username

    @property
    def initials(self) -> str:
        if self.first_name and self.last_name:
            return (self.first_name[0] + self.last_name[0]).upper()
        name = self.display_name
        return name[:2].upper()

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_guest(self) -> bool:
        return self.role == "guest"

    def __repr__(self):
        return f"<User {self.username}>"


class APIToken(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    token_hash = db.Column(db.String(64), unique=True, nullable=False)
    prefix = db.Column(db.String(12), nullable=False)
    created_at = db.Column(db.DateTime, default=now)
    last_used_at = db.Column(db.DateTime)
    expires_at = db.Column(db.DateTime)

    user = db.relationship("User")

    @staticmethod
    def hash(raw: str) -> str:
        return hashlib.sha256(raw.encode()).hexdigest()

    @classmethod
    def generate(cls, user, name, days=None):
        raw = "hgr_" + secrets.token_urlsafe(32)
        token = cls(
            user=user,
            name=name,
            token_hash=cls.hash(raw),
            prefix=raw[:10],
            expires_at=(now() + timedelta(days=days)) if days else None,
        )
        return token, raw


# ---------------------------------------------------------------------------
# Projets
# ---------------------------------------------------------------------------

class Project(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    identifier = db.Column(db.String(10), unique=True, nullable=False)
    description = db.Column(db.Text, default="")
    emoji = db.Column(db.String(16), default="🚀")
    color = db.Column(db.String(10), default="#3f76ff")
    network = db.Column(db.String(10), default="public")  # public | private
    lead_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"))
    default_assignee_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"))
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"))
    start_date = db.Column(db.Date)
    target_date = db.Column(db.Date)
    created_at = db.Column(db.DateTime, default=now)
    archived_at = db.Column(db.DateTime)
    issue_sequence = db.Column(db.Integer, default=0)

    # Fonctionnalités activables
    cycles_enabled = db.Column(db.Boolean, default=True)
    modules_enabled = db.Column(db.Boolean, default=True)
    views_enabled = db.Column(db.Boolean, default=True)
    pages_enabled = db.Column(db.Boolean, default=True)
    intake_enabled = db.Column(db.Boolean, default=True)
    time_tracking_enabled = db.Column(db.Boolean, default=True)
    estimate_type = db.Column(db.String(20), default="points")

    # Automatisations
    auto_archive_months = db.Column(db.Integer, default=0)  # 0 = désactivé
    auto_close_months = db.Column(db.Integer, default=0)

    lead = db.relationship("User", foreign_keys=[lead_id])
    default_assignee = db.relationship("User", foreign_keys=[default_assignee_id])
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    members = db.relationship("ProjectMember", back_populates="project", cascade="all, delete-orphan")
    states = db.relationship("State", back_populates="project", cascade="all, delete-orphan",
                             order_by="State.sequence")
    labels = db.relationship("Label", back_populates="project", cascade="all, delete-orphan",
                             order_by="Label.name")
    issues = db.relationship("Issue", back_populates="project", cascade="all, delete-orphan",
                             foreign_keys="Issue.project_id")
    cycles = db.relationship("Cycle", back_populates="project", cascade="all, delete-orphan",
                             order_by="Cycle.start_date")
    modules = db.relationship("Module", back_populates="project", cascade="all, delete-orphan",
                              order_by="Module.name")
    pages = db.relationship("Page", back_populates="project", cascade="all, delete-orphan")
    views = db.relationship("SavedView", back_populates="project", cascade="all, delete-orphan")

    @property
    def member_users(self):
        return sorted((m.user for m in self.members if m.user.active), key=lambda u: u.display_name.lower())

    def member(self, user) -> "ProjectMember | None":
        for m in self.members:
            if m.user_id == user.id:
                return m
        return None

    def default_state(self) -> "State | None":
        for s in self.states:
            if s.is_default:
                return s
        for s in self.states:
            if s.group == "unstarted":
                return s
        return self.states[0] if self.states else None

    def first_state_of(self, group: str) -> "State | None":
        for s in self.states:
            if s.group == group:
                return s
        return None

    def next_sequence(self) -> int:
        self.issue_sequence = (self.issue_sequence or 0) + 1
        return self.issue_sequence

    @property
    def estimate_values(self):
        return ESTIMATE_TYPES.get(self.estimate_type or "none", ("", []))[1]

    @property
    def is_archived(self):
        return self.archived_at is not None

    def stats(self):
        """Compte les tickets actifs par groupe d'état."""
        rows = (
            db.session.query(State.group, func.count(Issue.id))
            .join(Issue, Issue.state_id == State.id)
            .filter(Issue.project_id == self.id, Issue.archived_at.is_(None), Issue.is_visible_clause())
            .group_by(State.group)
            .all()
        )
        counts = {g: 0 for g, _, _ in STATE_GROUPS}
        for g, n in rows:
            counts[g] = n
        total = sum(counts.values())
        done = counts["completed"]
        active_total = total - counts["cancelled"]
        return {
            "counts": counts,
            "total": total,
            "done": done,
            "open": total - done - counts["cancelled"],
            "progress": round(100 * done / active_total) if active_total else 0,
        }


class ProjectMember(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id", ondelete="CASCADE"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    role = db.Column(db.String(20), default="member")
    joined_at = db.Column(db.DateTime, default=now)

    project = db.relationship("Project", back_populates="members")
    user = db.relationship("User", back_populates="memberships")

    __table_args__ = (db.UniqueConstraint("project_id", "user_id"),)


class State(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id", ondelete="CASCADE"), nullable=False)
    name = db.Column(db.String(80), nullable=False)
    color = db.Column(db.String(10), default="#a3a3a3")
    group = db.Column(db.String(20), default="unstarted")
    description = db.Column(db.String(255), default="")
    sequence = db.Column(db.Float, default=0)
    is_default = db.Column(db.Boolean, default=False)

    project = db.relationship("Project", back_populates="states")

    @property
    def group_label(self):
        return STATE_GROUP_LABELS.get(self.group, self.group)


class Label(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id", ondelete="CASCADE"), nullable=False)
    name = db.Column(db.String(80), nullable=False)
    color = db.Column(db.String(10), default="#3f76ff")
    description = db.Column(db.String(255), default="")
    parent_id = db.Column(db.Integer, db.ForeignKey("label.id", ondelete="SET NULL"))

    project = db.relationship("Project", back_populates="labels")
    parent = db.relationship("Label", remote_side=[id])


# ---------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------

class Issue(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id", ondelete="CASCADE"), nullable=False, index=True)
    sequence_id = db.Column(db.Integer, nullable=False)
    title = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text, default="")
    state_id = db.Column(db.Integer, db.ForeignKey("state.id", ondelete="SET NULL"))
    priority = db.Column(db.String(10), default="none")
    start_date = db.Column(db.Date)
    target_date = db.Column(db.Date)
    estimate = db.Column(db.Integer)
    parent_id = db.Column(db.Integer, db.ForeignKey("issue.id", ondelete="SET NULL"))
    cycle_id = db.Column(db.Integer, db.ForeignKey("cycle.id", ondelete="SET NULL"))
    sort_order = db.Column(db.Float, default=65535)
    external_id = db.Column(db.String(60))
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"))
    created_at = db.Column(db.DateTime, default=now)
    updated_at = db.Column(db.DateTime, default=now, onupdate=now)
    completed_at = db.Column(db.DateTime)
    archived_at = db.Column(db.DateTime)
    # Intake : None = ticket normal ; sinon pending / accepted / declined / snoozed / duplicate
    intake_status = db.Column(db.String(12))
    intake_snoozed_until = db.Column(db.Date)
    intake_duplicate_of_id = db.Column(db.Integer, db.ForeignKey("issue.id", ondelete="SET NULL"))
    intake_source = db.Column(db.String(60))

    project = db.relationship("Project", back_populates="issues", foreign_keys=[project_id])
    state = db.relationship("State")
    cycle = db.relationship("Cycle", back_populates="issues")
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    parent = db.relationship("Issue", remote_side=[id], foreign_keys=[parent_id], backref="children")
    intake_duplicate_of = db.relationship("Issue", remote_side=[id], foreign_keys=[intake_duplicate_of_id])
    assignees = db.relationship("User", secondary=issue_assignees, order_by="User.first_name")
    labels = db.relationship("Label", secondary=issue_labels, order_by="Label.name")
    modules = db.relationship("Module", secondary=issue_modules, back_populates="issues")
    subscribers = db.relationship("User", secondary=issue_subscribers)
    comments = db.relationship("Comment", back_populates="issue", cascade="all, delete-orphan",
                               order_by="Comment.created_at")
    activities = db.relationship("Activity", back_populates="issue", cascade="all, delete-orphan",
                                 order_by="Activity.created_at")
    links = db.relationship("IssueLink", back_populates="issue", cascade="all, delete-orphan")
    attachments = db.relationship("Attachment", back_populates="issue", cascade="all, delete-orphan")
    worklogs = db.relationship("WorkLog", back_populates="issue", cascade="all, delete-orphan",
                               order_by="WorkLog.date.desc()")
    reactions = db.relationship("Reaction", back_populates="issue", cascade="all, delete-orphan",
                                foreign_keys="Reaction.issue_id")

    __table_args__ = (db.UniqueConstraint("project_id", "sequence_id"),)

    @staticmethod
    def is_visible_clause():
        """Tickets visibles dans les listes (hors intake non accepté)."""
        return db.or_(Issue.intake_status.is_(None), Issue.intake_status == "accepted")

    @property
    def key(self) -> str:
        return f"{self.project.identifier}-{self.sequence_id}"

    @property
    def state_group(self) -> str:
        return self.state.group if self.state else "backlog"

    @property
    def is_closed(self) -> bool:
        return self.state_group in CLOSED_GROUPS

    @property
    def is_overdue(self) -> bool:
        return bool(self.target_date and self.target_date < date.today() and not self.is_closed)

    @property
    def due_soon(self) -> bool:
        return bool(self.target_date and not self.is_closed
                    and date.today() <= self.target_date <= date.today() + timedelta(days=3))

    @property
    def visible_children(self):
        return [c for c in self.children if c.archived_at is None]

    @property
    def children_progress(self):
        kids = [c for c in self.visible_children if c.state_group != "cancelled"]
        if not kids:
            return None
        done = sum(1 for c in kids if c.state_group == "completed")
        return {"done": done, "total": len(kids), "pct": round(100 * done / len(kids))}

    @property
    def estimate_display(self) -> str:
        return estimate_label(self.project.estimate_type, self.estimate)

    @property
    def time_spent_minutes(self) -> int:
        return sum(w.minutes for w in self.worklogs)

    def relations(self):
        """Liste (libellé, ticket lié, relation) dans les deux sens."""
        out = []
        for r in IssueRelation.query.filter_by(issue_id=self.id).all():
            out.append((RELATION_TYPES[r.relation_type][0], r.related, r))
        for r in IssueRelation.query.filter_by(related_id=self.id).all():
            out.append((RELATION_TYPES[r.relation_type][1], r.issue, r))
        return out

    @property
    def blockers(self):
        return [r.related for r in IssueRelation.query.filter_by(issue_id=self.id, relation_type="blocked_by")
                if not r.related.is_closed]

    def is_subscribed(self, user) -> bool:
        return any(u.id == user.id for u in self.subscribers)

    def subscribe(self, user) -> None:
        if user and not self.is_subscribed(user):
            self.subscribers.append(user)


class IssueRelation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    issue_id = db.Column(db.Integer, db.ForeignKey("issue.id", ondelete="CASCADE"), nullable=False)
    related_id = db.Column(db.Integer, db.ForeignKey("issue.id", ondelete="CASCADE"), nullable=False)
    relation_type = db.Column(db.String(20), default="relates_to")
    created_at = db.Column(db.DateTime, default=now)

    issue = db.relationship("Issue", foreign_keys=[issue_id])
    related = db.relationship("Issue", foreign_keys=[related_id])


class Comment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    issue_id = db.Column(db.Integer, db.ForeignKey("issue.id", ondelete="CASCADE"), nullable=False, index=True)
    author_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"))
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=now)
    updated_at = db.Column(db.DateTime)

    issue = db.relationship("Issue", back_populates="comments")
    author = db.relationship("User")
    reactions = db.relationship("Reaction", back_populates="comment", cascade="all, delete-orphan",
                                foreign_keys="Reaction.comment_id")


class Reaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    emoji = db.Column(db.String(16), nullable=False)
    issue_id = db.Column(db.Integer, db.ForeignKey("issue.id", ondelete="CASCADE"))
    comment_id = db.Column(db.Integer, db.ForeignKey("comment.id", ondelete="CASCADE"))
    created_at = db.Column(db.DateTime, default=now)

    user = db.relationship("User")
    issue = db.relationship("Issue", back_populates="reactions", foreign_keys=[issue_id])
    comment = db.relationship("Comment", back_populates="reactions", foreign_keys=[comment_id])


def group_reactions(reactions, user=None):
    """Regroupe les réactions par emoji -> [(emoji, nb, moi?, noms)]."""
    groups = {}
    for r in reactions:
        g = groups.setdefault(r.emoji, {"count": 0, "mine": False, "names": []})
        g["count"] += 1
        g["names"].append(r.user.display_name)
        if user is not None and r.user_id == user.id:
            g["mine"] = True
    return [(e, g["count"], g["mine"], ", ".join(g["names"])) for e, g in groups.items()]


class Activity(db.Model):
    """Historique des modifications (tickets, mais aussi projets, cycles, pages...)."""
    id = db.Column(db.Integer, primary_key=True)
    issue_id = db.Column(db.Integer, db.ForeignKey("issue.id", ondelete="CASCADE"), index=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id", ondelete="CASCADE"), index=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"))
    verb = db.Column(db.String(30), nullable=False)
    field = db.Column(db.String(40))
    old_value = db.Column(db.Text)
    new_value = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=now, index=True)

    issue = db.relationship("Issue", back_populates="activities")
    project = db.relationship("Project")
    actor = db.relationship("User")


class IssueLink(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    issue_id = db.Column(db.Integer, db.ForeignKey("issue.id", ondelete="CASCADE"), nullable=False)
    url = db.Column(db.String(1000), nullable=False)
    title = db.Column(db.String(255), default="")
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"))
    created_at = db.Column(db.DateTime, default=now)

    issue = db.relationship("Issue", back_populates="links")
    created_by = db.relationship("User")


class Attachment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    issue_id = db.Column(db.Integer, db.ForeignKey("issue.id", ondelete="CASCADE"), nullable=False)
    filename = db.Column(db.String(255), nullable=False)
    stored_name = db.Column(db.String(255), nullable=False)
    size = db.Column(db.Integer, default=0)
    mimetype = db.Column(db.String(120), default="application/octet-stream")
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"))
    created_at = db.Column(db.DateTime, default=now)

    issue = db.relationship("Issue", back_populates="attachments")
    uploaded_by = db.relationship("User")

    @property
    def is_image(self):
        return (self.mimetype or "").startswith("image/")


class WorkLog(db.Model):
    """Temps passé sur un ticket."""
    id = db.Column(db.Integer, primary_key=True)
    issue_id = db.Column(db.Integer, db.ForeignKey("issue.id", ondelete="CASCADE"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    minutes = db.Column(db.Integer, nullable=False)
    date = db.Column(db.Date, default=date.today)
    description = db.Column(db.String(255), default="")
    created_at = db.Column(db.DateTime, default=now)

    issue = db.relationship("Issue", back_populates="worklogs")
    user = db.relationship("User")


# ---------------------------------------------------------------------------
# Cycles, modules, vues
# ---------------------------------------------------------------------------

class Cycle(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id", ondelete="CASCADE"), nullable=False)
    name = db.Column(db.String(160), nullable=False)
    description = db.Column(db.Text, default="")
    start_date = db.Column(db.Date)
    end_date = db.Column(db.Date)
    owner_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"))
    created_at = db.Column(db.DateTime, default=now)
    archived_at = db.Column(db.DateTime)

    project = db.relationship("Project", back_populates="cycles")
    owner = db.relationship("User")
    issues = db.relationship("Issue", back_populates="cycle")

    @property
    def status(self) -> str:
        if not self.start_date or not self.end_date:
            return "draft"
        today = date.today()
        if today < self.start_date:
            return "upcoming"
        if today > self.end_date:
            return "completed"
        return "current"

    @property
    def status_label(self):
        return {"draft": "Brouillon", "upcoming": "À venir", "current": "En cours",
                "completed": "Terminé"}[self.status]

    @property
    def active_issues(self):
        return [i for i in self.issues if i.archived_at is None and i.intake_status in (None, "accepted")]

    @property
    def days_left(self):
        if not self.end_date:
            return None
        return (self.end_date - date.today()).days

    def progress(self):
        issues = self.active_issues
        counts = {g: 0 for g, _, _ in STATE_GROUPS}
        for i in issues:
            counts[i.state_group] += 1
        total = len(issues) - counts["cancelled"]
        return {
            "counts": counts,
            "total": len(issues),
            "done": counts["completed"],
            "pct": round(100 * counts["completed"] / total) if total else 0,
        }


class Module(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id", ondelete="CASCADE"), nullable=False)
    name = db.Column(db.String(160), nullable=False)
    description = db.Column(db.Text, default="")
    status = db.Column(db.String(20), default="planned")
    lead_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"))
    start_date = db.Column(db.Date)
    target_date = db.Column(db.Date)
    created_at = db.Column(db.DateTime, default=now)
    archived_at = db.Column(db.DateTime)

    project = db.relationship("Project", back_populates="modules")
    lead = db.relationship("User")
    members = db.relationship("User", secondary=module_members)
    issues = db.relationship("Issue", secondary=issue_modules, back_populates="modules")

    @property
    def status_label(self):
        return MODULE_STATUS_LABELS.get(self.status, self.status)

    @property
    def status_color(self):
        return MODULE_STATUS_COLORS.get(self.status, "#a3a3a3")

    @property
    def active_issues(self):
        return [i for i in self.issues if i.archived_at is None and i.intake_status in (None, "accepted")]

    def progress(self):
        issues = self.active_issues
        counts = {g: 0 for g, _, _ in STATE_GROUPS}
        for i in issues:
            counts[i.state_group] += 1
        total = len(issues) - counts["cancelled"]
        return {
            "counts": counts,
            "total": len(issues),
            "done": counts["completed"],
            "pct": round(100 * counts["completed"] / total) if total else 0,
        }


class SavedView(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id", ondelete="CASCADE"))  # None = vue globale
    name = db.Column(db.String(160), nullable=False)
    description = db.Column(db.String(500), default="")
    query = db.Column(db.Text, default="")  # chaîne de requête (filtres + affichage)
    owner_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"))
    is_public = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=now)

    project = db.relationship("Project", back_populates="views")
    owner = db.relationship("User")


# ---------------------------------------------------------------------------
# Pages (wiki / documentation)
# ---------------------------------------------------------------------------

class Page(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id", ondelete="CASCADE"), nullable=False)
    parent_id = db.Column(db.Integer, db.ForeignKey("page.id", ondelete="SET NULL"))
    title = db.Column(db.String(255), nullable=False, default="Sans titre")
    emoji = db.Column(db.String(16), default="📄")
    content = db.Column(db.Text, default="")
    owner_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"))
    is_public = db.Column(db.Boolean, default=True)
    is_locked = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=now)
    updated_at = db.Column(db.DateTime, default=now)
    updated_by_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"))
    archived_at = db.Column(db.DateTime)

    project = db.relationship("Project", back_populates="pages")
    owner = db.relationship("User", foreign_keys=[owner_id])
    updated_by = db.relationship("User", foreign_keys=[updated_by_id])
    parent = db.relationship("Page", remote_side=[id], backref="children")
    versions = db.relationship("PageVersion", back_populates="page", cascade="all, delete-orphan",
                               order_by="PageVersion.created_at.desc()")

    @property
    def word_count(self):
        return len((self.content or "").split())


class PageVersion(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    page_id = db.Column(db.Integer, db.ForeignKey("page.id", ondelete="CASCADE"), nullable=False)
    title = db.Column(db.String(255))
    content = db.Column(db.Text)
    author_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"))
    created_at = db.Column(db.DateTime, default=now)

    page = db.relationship("Page", back_populates="versions")
    author = db.relationship("User")


# ---------------------------------------------------------------------------
# Notifications, favoris, notes
# ---------------------------------------------------------------------------

class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"))
    issue_id = db.Column(db.Integer, db.ForeignKey("issue.id", ondelete="CASCADE"))
    project_id = db.Column(db.Integer, db.ForeignKey("project.id", ondelete="CASCADE"))
    kind = db.Column(db.String(20), default="update")  # assigned, comment, mention, update, intake...
    message = db.Column(db.String(500), nullable=False)
    url = db.Column(db.String(300))
    created_at = db.Column(db.DateTime, default=now, index=True)
    read_at = db.Column(db.DateTime)
    archived_at = db.Column(db.DateTime)
    snoozed_until = db.Column(db.DateTime)

    user = db.relationship("User", foreign_keys=[user_id])
    actor = db.relationship("User", foreign_keys=[actor_id])
    issue = db.relationship("Issue")
    project = db.relationship("Project")


class Favorite(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    entity_type = db.Column(db.String(20), nullable=False)  # project, cycle, module, view, page
    entity_id = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=now)

    __table_args__ = (db.UniqueConstraint("user_id", "entity_type", "entity_id"),)


class Sticky(db.Model):
    """Pense-bête personnel affiché sur l'accueil."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    content = db.Column(db.Text, default="")
    color = db.Column(db.String(10), default="#fef3c7")
    created_at = db.Column(db.DateTime, default=now)
    updated_at = db.Column(db.DateTime, default=now)


class Absence(db.Model):
    """Indisponibilités des membres (congés, formation...) pour la planification d'équipe."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=False)
    kind = db.Column(db.String(30), default="Congés")
    note = db.Column(db.String(255), default="")
    created_at = db.Column(db.DateTime, default=now)

    user = db.relationship("User")


def json_dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# E-mails
# ---------------------------------------------------------------------------

class MailConfig(db.Model):
    """Configuration des e-mails automatiques (une seule ligne, id = 1)."""
    id = db.Column(db.Integer, primary_key=True)
    enabled = db.Column(db.Boolean, default=False)
    mode = db.Column(db.String(10), default="outbox")  # outbox | smtp
    smtp_host = db.Column(db.String(200), default="")
    smtp_port = db.Column(db.Integer, default=587)
    smtp_security = db.Column(db.String(10), default="starttls")  # starttls | ssl | none
    smtp_user = db.Column(db.String(200), default="")
    smtp_password = db.Column(db.String(300), default="")
    from_name = db.Column(db.String(120), default="Hangar")
    from_address = db.Column(db.String(200), default="")
    base_url = db.Column(db.String(200), default="http://127.0.0.1:5000")
    reminder_hour = db.Column(db.Integer, default=8)
    digest_weekday = db.Column(db.Integer, default=0)  # 0 = lundi
    bilan_weekday = db.Column(db.Integer, default=4)  # 4 = vendredi
    bilan_hour = db.Column(db.Integer, default=17)
    send_notifications = db.Column(db.Boolean, default=True)
    send_reminders = db.Column(db.Boolean, default=True)
    send_digest = db.Column(db.Boolean, default=True)
    send_bilan = db.Column(db.Boolean, default=True)
    last_reminder_date = db.Column(db.Date)
    last_digest_date = db.Column(db.Date)
    last_bilan_date = db.Column(db.Date)

    @staticmethod
    def get() -> "MailConfig":
        cfg = db.session.get(MailConfig, 1)
        if cfg is None:
            cfg = MailConfig(id=1)
            db.session.add(cfg)
            db.session.commit()
        return cfg

    @property
    def sender(self) -> str:
        from email.utils import formataddr
        addr = self.from_address or self.smtp_user or "hangar@localhost"
        return formataddr((self.from_name or "Hangar", addr))


class EmailMessage(db.Model):
    """Boîte d'envoi et historique des e-mails."""
    id = db.Column(db.Integer, primary_key=True)
    to_address = db.Column(db.String(200), nullable=False)
    to_user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"))
    subject = db.Column(db.String(300), nullable=False)
    body_text = db.Column(db.Text, default="")
    body_html = db.Column(db.Text, default="")
    kind = db.Column(db.String(20), default="manual")  # notification | reminder | digest | bilan | manual | test
    status = db.Column(db.String(10), default="queued", index=True)  # queued | sent | failed | outbox
    error = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=now, index=True)
    sent_at = db.Column(db.DateTime)
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"))
    reply_to = db.Column(db.String(200))

    to_user = db.relationship("User", foreign_keys=[to_user_id])
    created_by = db.relationship("User", foreign_keys=[created_by_id])


# ---------------------------------------------------------------------------
# Bilan (rapports d'avancement enregistrés)
# ---------------------------------------------------------------------------

class BilanReport(db.Model):
    """Bilan figé à une date : instantané des chiffres + sections rédigées par l'équipe."""
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id", ondelete="CASCADE"))  # None = tout l'espace
    title = db.Column(db.String(200), nullable=False)
    period_start = db.Column(db.Date)
    period_end = db.Column(db.Date)
    data_json = db.Column(db.Text, default="{}")
    highlights = db.Column(db.Text, default="")    # faits marquants
    difficulties = db.Column(db.Text, default="")  # difficultés / risques
    decisions = db.Column(db.Text, default="")     # décisions prises
    next_steps = db.Column(db.Text, default="")    # prochaines étapes
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"))
    created_at = db.Column(db.DateTime, default=now)
    updated_at = db.Column(db.DateTime, default=now)

    project = db.relationship("Project")
    created_by = db.relationship("User")

    @property
    def data(self) -> dict:
        try:
            return json.loads(self.data_json or "{}")
        except ValueError:
            return {}

"""Modèle de données de Hangar.

Hiérarchie : Espace de travail > Projets > Tickets (work items), avec cycles (sprints),
modules, vues enregistrées, pages (wiki), intake (triage), notifications, temps passé.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import date, datetime, timedelta

from flask_login import UserMixin
from sqlalchemy import func
from sqlalchemy import inspect as sa_inspect
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db
from .texte import cle_tri


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
# Noms lus par les lecteurs d'écran et affichés au survol des pastilles de couleur
COLOR_NAMES = {
    "#3f76ff": "Bleu", "#8b5cf6": "Violet", "#ec4899": "Rose", "#ef4444": "Rouge", "#f97316": "Orange",
    "#eab308": "Jaune", "#22c55e": "Vert", "#14b8a6": "Vert d’eau", "#06b6d4": "Cyan", "#0ea5e9": "Bleu ciel",
    "#6366f1": "Indigo", "#a855f7": "Mauve", "#f43f5e": "Framboise", "#84cc16": "Vert pomme",
    "#64748b": "Gris ardoise", "#d946ef": "Fuchsia",
}


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
    # « Qui es-tu ? » sur la page de connexion pour les visiteurs du réseau local (toujours sur cet ordinateur,
    # jamais derrière un proxy ou un tunnel : voir views/auth.py::_montrer_comptes)
    login_accounts = db.Column(db.Boolean, default=True)
    week_starts_monday = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=now)
    # Jeton des sessions : mis dans chaque session et chaque cookie « se souvenir de moi » ; changé à chaque
    # restauration de sauvegarde, il ferme toutes les sessions ouvertes (un numéro de compte peut désigner
    # quelqu'un d'autre dans la base restaurée). Vide tant qu'aucune restauration n'a eu lieu.
    jeton_sessions = db.Column(db.String(40), default="")

    @staticmethod
    def get() -> "Workspace":
        ws = db.session.get(Workspace, 1)
        if ws is None:
            ws = Workspace(id=1)
            db.session.add(ws)
            db.session.commit()
        return ws

    @staticmethod
    def jeton_actuel() -> str:
        ws = db.session.get(Workspace, 1)
        return (ws.jeton_sessions or "") if ws is not None else ""


MOT_DE_PASSE_MIN = 8  # longueur minimale d'un nouveau mot de passe (les anciens, plus courts, restent valables)


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
    # « Déconnecter mes autres appareils » : +1 rend caduques toutes les sessions ouvertes (voir get_id)
    session_version = db.Column(db.Integer, default=0)

    memberships = db.relationship("ProjectMember", back_populates="user", cascade="all, delete-orphan")

    # Flask-Login
    @property
    def is_active(self):  # type: ignore[override]
        return bool(self.active)

    def get_id(self) -> str:
        """Ce que gardent la session et le cookie « Rester connecté » : « numéro:empreinte ».

        L'empreinte (HMAC avec la clé secrète de data/secret.key) dépend du mot de passe et de
        `session_version` : changer de mot de passe, le faire réinitialiser ou « Déconnecter mes autres
        appareils » rend caducs tous les anciens cookies, et le cookie d'un compte supprimé n'ouvre pas le
        compte créé ensuite avec le même numéro."""
        return f"{self.id}:{self.empreinte_session()}"

    def empreinte_session(self) -> str:
        from flask import current_app
        cle = str(current_app.config["SECRET_KEY"]).encode()
        # Jeton de l'espace : changé à chaque restauration de sauvegarde (voir Workspace.jeton_sessions).
        msg = f"{self.id}|{self.password_hash}|{self.session_version or 0}|{Workspace.jeton_actuel()}".encode()
        return hmac.new(cle, msg, hashlib.sha256).hexdigest()[:32]

    @classmethod
    def depuis_session(cls, valeur) -> "User | None":
        """Compte d'une session ou d'un cookie « Rester connecté » (chargeur de Flask-Login), ou None.

        Refuse sans erreur l'ancien format (numéro seul, écrit avant cette version : il suffit de se
        reconnecter une fois), une empreinte qui ne correspond plus et un compte désactivé. Un cookie
        « Rester connecté » refusé est effacé du navigateur."""
        uid, _, empreinte = str(valeur or "").partition(":")
        user = db.session.get(cls, int(uid)) if uid.isdigit() and empreinte else None
        if (user is not None and user.active
                and hmac.compare_digest(empreinte.encode(), user.empreinte_session().encode())):
            return user
        _oublier_cookie(valeur)
        return None

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


def _oublier_cookie(valeur) -> None:
    """Le cookie « Rester connecté » porte une valeur refusée : Flask-Login l'efface à la réponse."""
    from flask import current_app, has_request_context, request, session
    from flask_login.utils import decode_cookie
    if not has_request_context():
        return
    nom = current_app.config.get("REMEMBER_COOKIE_NAME", "remember_token")
    if nom in request.cookies and decode_cookie(request.cookies[nom]) == valeur:
        session["_remember"] = "clear"


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
                             order_by="Label.name.collate('fr')")
    issues = db.relationship("Issue", back_populates="project", cascade="all, delete-orphan",
                             foreign_keys="Issue.project_id")
    cycles = db.relationship("Cycle", back_populates="project", cascade="all, delete-orphan",
                             order_by="Cycle.start_date")
    modules = db.relationship("Module", back_populates="project", cascade="all, delete-orphan",
                              order_by="Module.name.collate('fr')")
    pages = db.relationship("Page", back_populates="project", cascade="all, delete-orphan")
    views = db.relationship("SavedView", back_populates="project", cascade="all, delete-orphan")

    @property
    def member_users(self):
        return sorted((m.user for m in self.members if m.user.active), key=lambda u: cle_tri(u.display_name))

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
        """Numéro du prochain ticket. L'incrément est fait par la base (UPDATE … = issue_sequence + 1) puis relu
        dans la même transaction : deux créations simultanées dans le projet obtiennent deux numéros
        différents (la seconde attend que la première ait fini d'écrire)."""
        if sa_inspect(self).persistent:
            self.issue_sequence = db.func.coalesce(Project.issue_sequence, 0) + 1
            db.session.flush()  # prend le verrou d'écriture ; l'attribut est relu depuis la base
        else:  # projet pas encore enregistré : personne d'autre ne peut y créer de ticket
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
    assignees = db.relationship("User", secondary=issue_assignees, order_by="User.first_name.collate('fr')")
    labels = db.relationship("Label", secondary=issue_labels, order_by="Label.name.collate('fr')")
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


def blockers_map(issue_ids) -> dict:
    """{id du ticket : bloqueurs encore ouverts}, comme `Issue.blockers`, mais en une requête pour
    tous les tickets affichés (tableau kanban, bilan) au lieu d'une par ticket."""
    from sqlalchemy.orm import selectinload
    ids = sorted(set(issue_ids))
    out = {i: [] for i in ids}
    for k in range(0, len(ids), 500):  # limite de paramètres d'une requête SQLite
        rels = (IssueRelation.query
                .filter(IssueRelation.issue_id.in_(ids[k:k + 500]), IssueRelation.relation_type == "blocked_by")
                .options(selectinload(IssueRelation.related).selectinload(Issue.state),
                         selectinload(IssueRelation.related).selectinload(Issue.project))
                .order_by(IssueRelation.id))
        for r in rels:
            if r.related is not None and not r.related.is_closed:
                out[r.issue_id].append(r.related)
    return out


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

    @property
    def viewable(self):
        """Affichable dans le navigateur (image sûre ou PDF, d'après l'extension) ; sinon téléchargé."""
        from .securite import attachment_type
        return attachment_type(self.filename)[1]


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
    # Les membres (non administrateurs) peuvent-ils écrire à des adresses hors de l'équipe (rédaction, bilan) ?
    # Les messages partent de l'adresse de l'expéditeur configurée (souvent le Gmail d'un administrateur).
    members_external = db.Column(db.Boolean, default=False)
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
    attempts = db.Column(db.Integer, default=0)       # essais d'envoi en échec (nouvel essai si l'erreur est passagère)
    expires_at = db.Column(db.DateTime)               # au-delà, plus de nouvel essai (vide = 24 h après la création)
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


# ---------------------------------------------------------------------------
# Agenda personnel : emploi du temps importé (.ics), tâches, rappels
# ---------------------------------------------------------------------------

RECURRENCES = {
    "": "Ne se répète pas",
    "daily": "Tous les jours",
    "weekdays": "Du lundi au vendredi",
    "weekly": "Toutes les semaines",
    "biweekly": "Toutes les deux semaines",
    "monthly": "Tous les mois",
}

# Délais de rappel proposés (minutes avant le début)
REMINDER_OFFSETS = [
    (0, "À l’heure du début"), (5, "5 min avant"), (10, "10 min avant"), (15, "15 min avant"),
    (30, "30 min avant"), (60, "1 h avant"), (120, "2 h avant"), (180, "3 h avant"),
    (1440, "La veille (24 h avant)"), (2880, "2 jours avant"), (4320, "3 jours avant"), (10080, "1 semaine avant"),
]

TASK_COLORS = ["#3f76ff", "#8b5cf6", "#ec4899", "#ef4444", "#f97316", "#eab308", "#22c55e", "#14b8a6", "#64748b"]


class AgendaEvent(db.Model):
    """Séance de l'emploi du temps, issue d'un import .ics (clé : UID du fichier).

    Un nouvel import met à jour la même ligne (les notes et rappels personnels sont conservés) ;
    une séance absente du dernier fichier passe à l'état « removed » (retirée) au lieu d'être effacée.
    """
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    uid = db.Column(db.String(255), nullable=False)
    title = db.Column(db.String(300), nullable=False)        # matière
    summary = db.Column(db.String(500), default="")          # intitulé brut du fichier
    kind = db.Column(db.String(30), default="")              # CM, TD_TP, CO_ECRIT…
    session_no = db.Column(db.String(10), default="")
    tentative = db.Column(db.Boolean, default=False)         # « (PREVISION) »
    cancelled = db.Column(db.Boolean, default=False)         # annulé dans le fichier
    start = db.Column(db.DateTime, nullable=False, index=True)
    end = db.Column(db.DateTime, nullable=False)
    all_day = db.Column(db.Boolean, default=False)
    location = db.Column(db.String(300), default="")
    groups = db.Column(db.String(500), default="")
    teachers = db.Column(db.String(300), default="")
    description = db.Column(db.Text, default="")
    status = db.Column(db.String(10), default="active", index=True)  # active | removed
    note = db.Column(db.Text, default="")                    # note personnelle (jamais écrasée par un import)
    created_import_id = db.Column(db.Integer)
    changed_import_id = db.Column(db.Integer)
    change_kind = db.Column(db.String(10), default="")       # added | modified | removed | restored
    changes_json = db.Column(db.Text, default="")            # {champ: [avant, après]} du dernier changement
    created_at = db.Column(db.DateTime, default=now)
    updated_at = db.Column(db.DateTime, default=now)

    reminders = db.relationship("AgendaReminder", back_populates="event", cascade="all, delete-orphan",
                                passive_deletes=True)

    __table_args__ = (db.UniqueConstraint("user_id", "uid"),)

    @property
    def is_active(self) -> bool:
        return self.status == "active" and not self.cancelled

    @property
    def last_day(self) -> date:
        """Dernier jour couvert (la fin d'une journée entière est exclusive)."""
        if self.end > self.start:
            return (self.end - timedelta(microseconds=1)).date()
        return self.start.date()

    @property
    def multi_day(self) -> bool:
        return self.all_day or self.last_day > self.start.date()

    @property
    def minutes(self) -> int:
        return max(0, int((self.end - self.start).total_seconds() // 60))

    @property
    def changes(self) -> dict:
        try:
            return json.loads(self.changes_json or "{}")
        except ValueError:
            return {}


class AgendaImport(db.Model):
    """Historique des imports d'emploi du temps (le dernier fait foi)."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    filename = db.Column(db.String(255), default="")
    stored_name = db.Column(db.String(255), default="")
    mode = db.Column(db.String(10), default="weeks")  # weeks | future | merge
    range_start = db.Column(db.Date)
    range_end = db.Column(db.Date)
    total = db.Column(db.Integer, default=0)
    added = db.Column(db.Integer, default=0)
    modified = db.Column(db.Integer, default=0)
    removed = db.Column(db.Integer, default=0)
    restored = db.Column(db.Integer, default=0)
    unchanged = db.Column(db.Integer, default=0)
    details_json = db.Column(db.Text, default="[]")
    warnings = db.Column(db.Text, default="")
    exam_notes = db.Column(db.Text, default="")  # dates d'examen des QCM recalées par cet import (une par ligne)
    exam_quota_sets = db.Column(db.Text, default="")  # sets dont cet import a coupé l'ajustement auto (un par ligne)
    created_at = db.Column(db.DateTime, default=now, index=True)
    undone_at = db.Column(db.DateTime)

    user = db.relationship("User")

    @property
    def details(self) -> list:
        try:
            return json.loads(self.details_json or "[]")
        except ValueError:
            return []

    @property
    def changed(self) -> int:
        return (self.added or 0) + (self.modified or 0) + (self.removed or 0) + (self.restored or 0)


class AgendaTask(db.Model):
    """Tâche personnelle planifiée dans l'agenda (avec rappels éventuels)."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    title = db.Column(db.String(300), nullable=False)
    notes = db.Column(db.Text, default="")
    due_date = db.Column(db.Date, index=True)        # vide = « un jour »
    start_time = db.Column(db.Time)                  # vide = toute la journée
    end_time = db.Column(db.Time)
    priority = db.Column(db.String(10), default="none")
    category = db.Column(db.String(60), default="")
    color = db.Column(db.String(10), default="")
    recurrence = db.Column(db.String(10), default="")
    recurrence_until = db.Column(db.Date)
    recurrence_day = db.Column(db.Integer)           # jour du mois d'origine (« tous les mois » le 31 → 28/29, 30, 31…)
    done_count = db.Column(db.Integer, default=0)    # occurrences terminées (tâches répétées)
    event_id = db.Column(db.Integer, db.ForeignKey("agenda_event.id", ondelete="SET NULL"))
    done_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=now)
    updated_at = db.Column(db.DateTime, default=now)

    event = db.relationship("AgendaEvent")
    reminders = db.relationship("AgendaReminder", back_populates="task", cascade="all, delete-orphan",
                                passive_deletes=True, order_by="AgendaReminder.remind_at")

    @property
    def is_done(self) -> bool:
        return self.done_at is not None

    @property
    def is_overdue(self) -> bool:
        return not self.is_done and self.due_date is not None and self.due_date < date.today()


class AgendaReminder(db.Model):
    """Rappel d'une tâche ou d'une séance : e-mail + notification, ou notification seule."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    task_id = db.Column(db.Integer, db.ForeignKey("agenda_task.id", ondelete="CASCADE"), index=True)
    event_id = db.Column(db.Integer, db.ForeignKey("agenda_event.id", ondelete="CASCADE"), index=True)
    offset_minutes = db.Column(db.Integer)            # minutes avant le début ; vide = date fixe
    remind_at = db.Column(db.DateTime, index=True)
    channel = db.Column(db.String(10), default="email")  # email | app | push (voir agenda.REMINDER_CHANNELS)
    auto = db.Column(db.Boolean, default=False)          # créé automatiquement (contrôles)
    status = db.Column(db.String(10), default="pending", index=True)  # pending | sent | missed | skipped | error
    sent_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=now)
    email_id = db.Column(db.Integer, db.ForeignKey("email_message.id", ondelete="SET NULL"))  # dernier e-mail du rappel

    task = db.relationship("AgendaTask", back_populates="reminders")
    event = db.relationship("AgendaEvent", back_populates="reminders")
    email = db.relationship("EmailMessage")

    @property
    def label(self) -> str:
        if self.offset_minutes is None:
            return f"Le {self.remind_at:%d/%m/%Y à %H:%M}" if self.remind_at else "Date à définir"
        for v, lab in REMINDER_OFFSETS:
            if v == self.offset_minutes:
                return lab
        m = self.offset_minutes
        if m % 1440 == 0:
            return f"{m // 1440} jours avant"
        if m % 60 == 0:
            return f"{m // 60} h avant"
        return f"{m} min avant"


class AgendaSettings(db.Model):
    """Préférences de l'agenda (une ligne par utilisateur)."""
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), primary_key=True)
    day_start_hour = db.Column(db.Integer, default=8)
    day_end_hour = db.Column(db.Integer, default=19)
    show_weekends = db.Column(db.Boolean, default=False)
    show_removed = db.Column(db.Boolean, default=True)
    allday_hour = db.Column(db.Integer, default=8)        # heure de référence des tâches sans heure
    default_reminder = db.Column(db.Integer, default=-1)  # minutes ; -1 = aucun rappel par défaut
    reminder_channel = db.Column(db.String(10), default="email")
    daily_mode = db.Column(db.String(10), default="morning")  # off | morning (résumé du jour) | evening (du lendemain)
    daily_hour = db.Column(db.Integer, default=7)
    last_daily_date = db.Column(db.Date)                  # jour du dernier envoi du résumé
    last_daily_day = db.Column(db.Date)                   # jour couvert par ce résumé (demain en mode soir)
    import_email = db.Column(db.Boolean, default=False)   # e-mail récapitulatif des changements après un import
    exam_reminder = db.Column(db.Integer, default=0)      # rappel automatique des contrôles (minutes, 0 = non)
    weekly_import_nudge = db.Column(db.Boolean, default=True)
    # Abonnement en lecture seule (/agenda/flux/<jeton>.ics) : jeton dédié, régénérable ; vide = coupé
    feed_token = db.Column(db.String(64), index=True)
    # Emploi du temps récupéré tout seul (réglé par un administrateur) : dossier surveillé ou adresse .ics
    source_kind = db.Column(db.String(10), default="")      # "" | folder | url
    source_path = db.Column(db.String(500), default="")     # dossier (folder) ou adresse (url)
    source_fingerprint = db.Column(db.String(64), default="")  # empreinte des séances du dernier fichier lu
    source_seen = db.Column(db.String(300), default="")     # fichier vu (nom, date, taille) : relu seulement s'il change
    source_checked_at = db.Column(db.DateTime)              # dernière lecture réussie de la source
    source_changed_at = db.Column(db.DateTime)              # dernier fichier nouveau (importé ou écarté)
    source_tried_at = db.Column(db.DateTime)                # dernier essai (réussi ou non)
    source_error = db.Column(db.String(500), default="")    # dernier problème, en clair (vide = tout va bien)

    user = db.relationship("User")

    @staticmethod
    def of(user) -> "AgendaSettings":
        s = db.session.get(AgendaSettings, user.id)
        if s is None:
            s = AgendaSettings(user_id=user.id)
            db.session.add(s)
            db.session.commit()
        return s


# ---------------------------------------------------------------------------
# Initiatives (Workgraph) : objectifs transverses qui regroupent projets et modules (épopées)
# ---------------------------------------------------------------------------

INITIATIVE_STATUSES = [
    ("draft", "Brouillon", "#a3a3a3"),
    ("planned", "Planifiée", "#3f76ff"),
    ("active", "Active", "#f59e0b"),
    ("completed", "Terminée", "#16a34a"),
    ("cancelled", "Annulée", "#dc2626"),
]
INITIATIVE_STATUS_LABELS = {k: v for k, v, _ in INITIATIVE_STATUSES}
INITIATIVE_STATUS_COLORS = {k: c for k, _, c in INITIATIVE_STATUSES}

HEALTHS = [
    ("on_track", "Dans les temps", "#16a34a"),
    ("at_risk", "À risque", "#f59e0b"),
    ("off_track", "En retard", "#dc2626"),
]
HEALTH_LABELS = {k: v for k, v, _ in HEALTHS}
HEALTH_COLORS = {k: c for k, _, c in HEALTHS}
HEALTH_RANK = {"on_track": 0, "at_risk": 1, "off_track": 2}

initiative_projects = _assoc("initiative_projects", "initiative", "project")
initiative_modules = _assoc("initiative_modules", "initiative", "module")


class Initiative(db.Model):
    """Initiative : objectif de l'espace de travail suivi à travers plusieurs projets et modules.

    L'avancement et la santé sont calculés (voir hangar/workgraph.py) ; la santé peut être forcée.
    """
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, default="")                 # markdown
    emoji = db.Column(db.String(16), default="💡")
    lead_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"))
    status = db.Column(db.String(12), default="planned")        # voir INITIATIVE_STATUSES
    health_override = db.Column(db.String(12))                   # None = santé calculée
    health_seen = db.Column(db.String(12))                       # dernière santé constatée (résumé quotidien)
    health_since = db.Column(db.DateTime)                        # … et depuis quand
    start_date = db.Column(db.Date)
    target_date = db.Column(db.Date)
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"))
    created_at = db.Column(db.DateTime, default=now)
    updated_at = db.Column(db.DateTime, default=now, onupdate=now)

    lead = db.relationship("User", foreign_keys=[lead_id])
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    projects = db.relationship("Project", secondary=initiative_projects, order_by="Project.name.collate('fr')",
                               backref=db.backref("initiatives", order_by="Initiative.name.collate('fr')"))
    modules = db.relationship("Module", secondary=initiative_modules, order_by="Module.name.collate('fr')",
                              backref=db.backref("initiatives", order_by="Initiative.name.collate('fr')"))
    updates = db.relationship("InitiativeUpdate", back_populates="initiative", cascade="all, delete-orphan",
                              order_by="InitiativeUpdate.created_at.desc()")

    @property
    def status_label(self):
        return INITIATIVE_STATUS_LABELS.get(self.status, self.status)

    @property
    def status_color(self):
        return INITIATIVE_STATUS_COLORS.get(self.status, "#a3a3a3")

    @property
    def is_closed(self) -> bool:
        return self.status in ("completed", "cancelled")


class InitiativeUpdate(db.Model):
    """Point d'avancement daté (« update » de Plane) : santé constatée et note en markdown."""
    id = db.Column(db.Integer, primary_key=True)
    initiative_id = db.Column(db.Integer, db.ForeignKey("initiative.id", ondelete="CASCADE"), nullable=False,
                              index=True)
    author_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"))
    health = db.Column(db.String(12), default="on_track")
    body = db.Column(db.Text, default="")
    progress = db.Column(db.Integer)                             # avancement (%) au moment de la note
    created_at = db.Column(db.DateTime, default=now)

    initiative = db.relationship("Initiative", back_populates="updates")
    author = db.relationship("User")

    @property
    def health_label(self):
        return HEALTH_LABELS.get(self.health, self.health)

    @property
    def health_color(self):
        return HEALTH_COLORS.get(self.health, "#a3a3a3")


class AgendaSubjectLink(db.Model):
    """Matière de l'emploi du temps reliée à une collection du wiki et à une catégorie de QCM (par compte).

    La clé est le titre normalisé de la séance : le lien vaut pour toutes les séances de la matière."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    subject = db.Column(db.String(300), nullable=False)   # titre normalisé
    title = db.Column(db.String(300), default="")         # titre affiché
    wiki_path = db.Column(db.String(500), default="")     # dossier (collection) ou page du coffre
    qcm_category = db.Column(db.String(300), default="")  # catégorie de sets de QCM
    created_at = db.Column(db.DateTime, default=now)
    updated_at = db.Column(db.DateTime, default=now)

    __table_args__ = (db.UniqueConstraint("user_id", "subject"),)

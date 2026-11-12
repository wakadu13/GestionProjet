"""Fonctions utilitaires : permissions, markdown, dates, formatage."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

import markdown as md
import nh3
from flask import abort, request, url_for
from flask_login import current_user
from markupsafe import Markup, escape

from .extensions import db
from .models import (
    ROLE_RANK, Favorite, Issue, Project, ProjectMember, User,
)
from .securite import ExternalImages, media_filter
from .texte import COLLATION_FR, cle_tri, contient_plie, plier  # noqa: F401 (réexportés)

MONTHS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]
MONTHS_FULL = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre",
               "octobre", "novembre", "décembre"]
DAYS = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."]
DAYS_FULL = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------

def project_role(user, project: Project) -> str | None:
    """Rôle effectif : admin / member / guest / viewer (projet public non rejoint) / None."""
    if user is None or not user.is_authenticated:
        return None
    if user.role == "admin":
        return "admin"
    m = ProjectMember.query.filter_by(project_id=project.id, user_id=user.id).first()
    if m:
        if user.role == "guest":
            return "guest"
        return m.role
    if project.network == "public" and user.role != "guest":
        return "viewer"
    return None


def can(user, action: str, project: Project) -> bool:
    role = project_role(user, project)
    if role is None:
        return False
    if action == "view":
        return True
    if action == "comment":
        return role in ("admin", "member", "guest")
    if action == "edit":
        return role in ("admin", "member") and not project.is_archived
    if action == "admin":
        return role == "admin"
    return False


def can_delete_issues(user, project: Project) -> bool:
    """Suppression définitive d'un ticket (fiche, suppression en masse, API) : réservée à l'administrateur
    du projet (ou de l'espace) et au responsable du projet. Les autres membres archivent."""
    if not can(user, "edit", project):
        return False
    return can(user, "admin", project) or (project.lead_id is not None and project.lead_id == user.id)


def favorite_target(user, entity_type: str, entity_id: int):
    """Objet d'un favori (projet, cycle, module, vue, page) si `user` a le droit de le voir, sinon None.
    Sert à l'ajout d'un favori et à la barre latérale : un titre privé ne doit jamais y apparaître."""
    from .models import Cycle, Module, Page, SavedView
    model = {"project": Project, "cycle": Cycle, "module": Module, "view": SavedView, "page": Page}.get(entity_type)
    obj = db.session.get(model, entity_id) if model else None
    if obj is None:
        return None
    project = obj if entity_type == "project" else obj.project
    if project is not None and not can(user, "view", project):
        return None
    if entity_type == "page" and not (obj.is_public or obj.owner_id == user.id):
        return None
    if entity_type == "view" and not (obj.is_public or obj.owner_id == user.id):
        return None
    return obj


def get_project(pid: int, need: str = "view") -> Project:
    project = db.session.get(Project, pid)
    if project is None:
        abort(404)
    if not can(current_user, need, project):
        abort(403)
    return project


def visible_projects(user, include_archived=False):
    q = Project.query
    if not include_archived:
        q = q.filter(Project.archived_at.is_(None))
    projects = q.order_by(tri_fr(Project.name)).all()
    if user.role == "admin":
        return projects
    member_ids = {m.project_id for m in ProjectMember.query.filter_by(user_id=user.id)}
    return [p for p in projects
            if p.id in member_ids or (p.network == "public" and user.role != "guest")]


def joined_projects(user):
    """Projets dont l'utilisateur est membre (pour la barre latérale)."""
    ids = {m.project_id for m in ProjectMember.query.filter_by(user_id=user.id)}
    return [p for p in Project.query.filter(Project.archived_at.is_(None)).order_by(tri_fr(Project.name))
            if p.id in ids]


def require_admin():
    if not current_user.is_authenticated or current_user.role != "admin":
        abort(403)


def role_at_least(role: str | None, minimum: str) -> bool:
    return ROLE_RANK.get(role or "", 0) >= ROLE_RANK[minimum]


def is_favorite(entity_type: str, entity_id: int) -> bool:
    if not current_user.is_authenticated:
        return False
    return Favorite.query.filter_by(user_id=current_user.id, entity_type=entity_type,
                                    entity_id=entity_id).first() is not None


# ---------------------------------------------------------------------------
# Texte et markdown
# ---------------------------------------------------------------------------

def normalize(text: str) -> str:
    """minuscule, sans accents ni ponctuation superflue (« Cœur d’Alène » → « coeur d alene »)."""
    text = plier(str(text or "")).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Recherche et tri à la française (voir texte.py)
# ---------------------------------------------------------------------------

def contient(col, texte: str):
    """Condition SQL « la colonne contient ``texte`` », sans tenir compte de la casse, des accents
    ni de la forme de l'apostrophe : « etablir » trouve « Établir le plan d’actions », « plan
    d'actions » trouve « plan d’actions ». « % » et « _ » sont cherchés tels quels (pas de joker).
    S'appuie sur la fonction SQLite ``contient_plie`` (extensions.py, texte.py) : comme
    LIKE '%…%', elle parcourt la table, et les textes longs déjà pliés sont gardés en mémoire."""
    return db.func.contient_plie(col, plier(texte or ""), type_=db.Boolean)


def tri_fr(col):
    """Colonne triée à la française dans un ORDER BY (« École » avant « Zèbre »)."""
    return col.collate(COLLATION_FR)


def slugify(text: str) -> str:
    return normalize(text).replace(" ", "-") or "x"


# Caractères de contrôle refusés par Excel (openpyxl) et Word (python-docx) : les deux bibliothèques refusent
# exactement ce jeu. La tabulation verticale (saut de ligne Maj+Entrée de PowerPoint) et le saut de page
# deviennent un vrai retour à la ligne ; les autres sont retirés.
_XML_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_LINE_CONTROLS = re.compile(r"[\x00-\x1f\x7f]+")


def xml_safe(value):
    """Texte utilisable dans un fichier Excel ou Word. Les autres valeurs (nombres, dates…) passent telles
    quelles."""
    if not isinstance(value, str):
        return value
    return _XML_ILLEGAL.sub("", value.replace("\x0b", "\n").replace("\x0c", "\n"))


def xml_safe_deep(value):
    """`xml_safe` appliqué à toutes les chaînes d'un dict / d'une liste (données d'un bilan)."""
    if isinstance(value, dict):
        return {k: xml_safe_deep(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [xml_safe_deep(v) for v in value]
    return xml_safe(value)


def single_line(text: str) -> str:
    """Titre sur une ligne : tout caractère de contrôle (tabulation, retour à la ligne collé…) devient une
    espace."""
    return _LINE_CONTROLS.sub(" ", text or "")


# Tableurs (CSV) : une cellule qui commence par l'un de ces caractères serait lue comme une formule.
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value):
    """Valeur de cellule CSV qu'un tableur affiche telle quelle : une apostrophe est ajoutée devant un texte
    qui commence comme une formule (voir `importer.read_table`, qui la retire à l'import)."""
    if isinstance(value, str) and value.startswith(FORMULA_START):
        return "'" + value
    return value


ALLOWED_TAGS = {
    "a", "abbr", "b", "blockquote", "br", "code", "del", "details", "div", "em", "h1", "h2", "h3",
    "h4", "h5", "h6", "hr", "i", "img", "input", "kbd", "li", "mark", "ol", "p", "pre", "s", "span",
    "strong", "sub", "summary", "sup", "table", "tbody", "td", "th", "thead", "tr", "u", "ul",
}
ALLOWED_ATTRS = {
    "a": {"href", "title", "class", "target"},
    "img": {"src", "alt", "title", "width", "height"},
    "input": {"type", "checked", "disabled"},
    "span": {"class"},
    "div": {"class"},
    "code": {"class"},
    "li": {"class"},
    "ul": {"class"},
    "td": {"align"},
    "th": {"align"},
    "h1": {"id"}, "h2": {"id"}, "h3": {"id"}, "h4": {"id"},
}

ISSUE_KEY_RE = re.compile(r"(?<![\w/\-\[])([A-Z][A-Z0-9]{1,9})-(\d+)\b")
MENTION_RE = re.compile(r"(?<![\w@])@([a-zA-Z0-9_.\-]{2,60})")


def _link_refs(text: str) -> str:
    """Transforme @utilisateur et CLE-12 en liens markdown (hors blocs de code)."""
    identifiers = {p.identifier for p in Project.query.with_entities(Project.identifier)}
    usernames = {u.username for u in User.query.with_entities(User.username)}
    parts = re.split(r"(```.*?```|`[^`\n]*`)", text, flags=re.S)
    for i in range(0, len(parts), 2):
        seg = parts[i]
        seg = ISSUE_KEY_RE.sub(
            lambda m: f"[{m.group(0)}](/i/{m.group(0)})" if m.group(1) in identifiers else m.group(0), seg)
        seg = MENTION_RE.sub(
            lambda m: f"[@{m.group(1)}](/team/{m.group(1)})" if m.group(1) in usernames else m.group(0), seg)
        parts[i] = seg
    return "".join(parts)


def render_markdown(text: str | None, link_refs: bool = True) -> Markup:
    if not text:
        return Markup("")
    src = _link_refs(text) if link_refs else text
    html = md.markdown(src, extensions=["extra", "sane_lists", "nl2br", "toc", ExternalImages()], output_format="html")
    # Listes de tâches : - [ ] / - [x]
    html = re.sub(r"<li>\s*\[ \]\s*", '<li class="task"><input type="checkbox" disabled> ', html)
    html = re.sub(r"<li>\s*\[[xX]\]\s*", '<li class="task done"><input type="checkbox" checked disabled> ', html)
    html = re.sub(r"<li>\s*<p>\s*\[ \]\s*", '<li class="task"><p><input type="checkbox" disabled> ', html)
    html = re.sub(r"<li>\s*<p>\s*\[[xX]\]\s*", '<li class="task done"><p><input type="checkbox" checked disabled> ', html)
    # Images seulement depuis Hangar : une image externe (« ![x](https://…) » devenue un lien ci-dessus, ou
    # balise <img> écrite à la main) se chargerait chez chaque lecteur sans qu'il clique.
    clean = nh3.clean(html, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS, attribute_filter=media_filter,
                      url_schemes={"http", "https", "mailto"}, link_rel="noopener noreferrer")
    return Markup(clean)


def extract_mentions(text: str) -> list[User]:
    names = set(MENTION_RE.findall(text or ""))
    if not names:
        return []
    return User.query.filter(User.username.in_(names)).all()


def markdown_toc(text: str) -> list[tuple[int, str, str]]:
    """Table des matières (niveau, titre, ancre) à partir des titres markdown."""
    from markdown.extensions.toc import slugify as _toc_slugify
    toc = []
    used: set[str] = set()
    in_code = False
    for line in (text or "").splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        m = re.match(r"^(#{1,4})\s+(.+?)\s*#*$", line)
        if m:
            # Même algorithme d'ancre que l'extension « toc » de python-markdown
            title = re.sub(r"[*_`]+|\[([^\]]*)\]\([^)]*\)", lambda x: x.group(1) or "", m.group(2)).strip()
            anchor = _toc_slugify(title, "-")
            base, n = anchor, 1
            while anchor in used:
                anchor = f"{base}_{n}"
                n += 1
            used.add(anchor)
            toc.append((len(m.group(1)), title, anchor))
    return toc


def excerpt(text: str | None, length: int = 160) -> str:
    if not text:
        return ""
    t = re.sub(r"[#*_`>\[\]()!|-]+", " ", text)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:length] + ("…" if len(t) > length else "")


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

def parse_date(value) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%Y/%m/%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    try:
        serial = float(s)
        if 20000 < serial < 80000:  # numéro de série Excel
            return date(1899, 12, 30) + timedelta(days=int(serial))
    except ValueError:
        pass
    return None


def fmt_date(d, with_year: bool | None = None, empty: str = "") -> str:
    if not d:
        return empty
    if isinstance(d, datetime):
        d = d.date()
    if with_year is None:
        with_year = d.year != date.today().year
    s = f"{d.day} {MONTHS[d.month - 1]}"
    return f"{s} {d.year}" if with_year else s


def fmt_date_long(d) -> str:
    if not d:
        return ""
    if isinstance(d, datetime):
        d = d.date()
    return f"{DAYS_FULL[d.weekday()]} {d.day} {MONTHS_FULL[d.month - 1]} {d.year}"


def fmt_datetime(dt) -> str:
    if not dt:
        return ""
    return f"{fmt_date(dt)} à {dt:%H:%M}"


def time_ago(dt) -> str:
    if not dt:
        return ""
    if isinstance(dt, date) and not isinstance(dt, datetime):
        dt = datetime.combine(dt, datetime.min.time())
    delta = datetime.now() - dt
    s = int(delta.total_seconds())
    if s < 0:
        return "à l'instant"
    if s < 60:
        return "à l'instant"
    if s < 3600:
        return f"il y a {s // 60} min"
    if s < 86400:
        return f"il y a {s // 3600} h"
    days = s // 86400
    if days == 1:
        return "hier"
    if days < 30:
        return f"il y a {days} j"
    if days < 365:
        return f"il y a {days // 30} mois"
    return f"il y a {days // 365} an{'s' if days >= 730 else ''}"


def relative_due(d) -> str:
    if not d:
        return ""
    diff = (d - date.today()).days
    if diff == 0:
        return "aujourd'hui"
    if diff == 1:
        return "demain"
    if diff == -1:
        return "hier"
    if diff < 0:
        return f"il y a {-diff} j"
    if diff < 7:
        return f"dans {diff} j"
    return fmt_date(d)


def fmt_minutes(minutes: int | None) -> str:
    if not minutes:
        return "0 h"
    h, m = divmod(int(minutes), 60)
    if h and m:
        return f"{h} h {m:02d}"
    if h:
        return f"{h} h"
    return f"{m} min"


def parse_duration(text: str) -> int:
    """'1h30', '90m', '1.5', '2h', '45 min' -> minutes."""
    s = (text or "").strip().lower().replace(",", ".").replace(" ", "")
    if not s:
        return 0
    m = re.fullmatch(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+)(?:m|min)?)?", s)
    if m and (m.group(1) or m.group(2)):
        if m.group(1) is None and "h" not in s and "m" not in s:
            return int(round(float(m.group(2)) * 60))  # nombre seul = heures
        hours = float(m.group(1) or 0)
        mins = int(m.group(2) or 0)
        return int(round(hours * 60)) + mins
    try:
        return int(round(float(s) * 60))
    except ValueError:
        return 0


def week_bounds(d: date | None = None):
    d = d or date.today()
    start = d - timedelta(days=d.weekday())
    return start, start + timedelta(days=6)


def daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def greeting() -> str:
    h = datetime.now().hour
    if h < 5:
        return "Bonne nuit"
    if h < 12:
        return "Bonjour"
    if h < 18:
        return "Bon après-midi"
    return "Bonsoir"


# ---------------------------------------------------------------------------
# Divers
# ---------------------------------------------------------------------------

def is_safe_next(target: str | None) -> bool:
    return bool(target) and target.startswith("/") and not target.startswith("//")


def back_url(default: str) -> str:
    nxt = request.form.get("next") or request.args.get("next")
    if is_safe_next(nxt):
        return nxt
    ref = request.referrer
    if ref and ref.startswith(request.host_url):
        return ref
    return default


def issue_url(issue: Issue, _external=False) -> str:
    return url_for("issues.detail", pid=issue.project_id, seq=issue.sequence_id, _external=_external)


def human_size(n: int) -> str:
    for unit in ("o", "Ko", "Mo", "Go"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "o" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} To"


def contrast_text(hex_color: str) -> str:
    try:
        c = hex_color.lstrip("#")
        r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
        return "#111" if (r * 299 + g * 587 + b * 114) / 1000 > 150 else "#fff"
    except Exception:
        return "#fff"


def nl2br(text: str) -> Markup:
    return Markup("<br>".join(escape(text or "").split("\n")))

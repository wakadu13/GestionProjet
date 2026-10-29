"""Import (Excel, CSV, JSON, Word) et export (CSV, Excel, JSON, Markdown) des données."""
from __future__ import annotations

import csv
import difflib
import io
import json
import random
import re
import secrets
import warnings
import zipfile
from datetime import date, datetime
from xml.etree import ElementTree as ET

from .extensions import db
from .models import (
    PALETTE, PRIORITY_LABELS, STATE_GROUP_LABELS, Comment, Cycle, Issue, IssueRelation, Module, Project,
    ProjectMember, User, now,
)
from .services import add_relation, create_issue, ensure_member, get_or_create_label, get_or_create_module, \
    log_activity, update_issue
from .utils import fmt_minutes, normalize, parse_date

# ---------------------------------------------------------------------------
# Lecture de fichiers tabulaires
# ---------------------------------------------------------------------------

ALIASES = {
    "key": ["identifiant", "cle", "key"],
    "external_id": ["id", "id externe", "external id", "ref", "reference", "n", "numero"],
    "title": ["titre", "tache", "taches", "title", "name", "nom", "intitule", "sujet", "summary", "ticket",
              "work item", "action"],
    "description": ["description", "details", "detail", "contenu"],
    "deliverable": ["livrable attendu", "livrable", "livrables", "deliverable"],
    "state": ["statut", "etat", "state", "status"],
    "priority": ["priorite", "priority", "urgence"],
    "assignees": ["responsable", "responsables", "assigne", "assignes", "assignee", "assignees", "porteur",
                  "owner", "pilote"],
    "support": ["appui", "support", "contributeurs", "contributeur", "aide"],
    "start": ["debut", "date de debut", "start", "start date", "date debut"],
    "target": ["echeance", "date limite", "due", "due date", "target date", "fin", "date de fin", "date fin",
               "deadline"],
    "labels": ["etiquettes", "etiquette", "labels", "label", "tags", "tag", "partie", "categorie"],
    "module": ["module", "modules", "phase", "lot", "epic"],
    "cycle": ["cycle", "sprint", "iteration"],
    "estimate": ["estimation", "duree", "estimate", "points", "effort", "charge"],
    "depends": ["depend de", "dependances", "dependance", "blocked by", "bloque par", "predecesseurs",
                "depends on"],
    "comments": ["commentaires", "commentaire", "comments", "comment", "notes", "remarques"],
    "source": ["source", "sources"],
    "profile": ["profil"],
    "parent": ["parent", "ticket parent"],
}
_ALIAS_INDEX = {alias: field for field, aliases in ALIASES.items() for alias in aliases}
EMPTY = {"", "-", "–", "—", "n/a", "na", "none", "aucun", "aucune", "null"}
GROUP_WORDS = {"ensemble du groupe", "tout le groupe", "tous", "toute l equipe", "equipe", "groupe"}


def match_header(h) -> str | None:
    n = normalize(h)
    if not n:
        return None
    if n in _ALIAS_INDEX:
        return _ALIAS_INDEX[n]
    for alias in sorted(_ALIAS_INDEX, key=len, reverse=True):
        if len(alias) >= 3 and (n.startswith(alias + " ") or n == alias):
            return _ALIAS_INDEX[alias]
    return None


def _rows_from_matrix(matrix):
    """Détecte la ligne d'en-tête puis construit une liste de dictionnaires {champ: valeur}."""
    header_idx, mapping = None, {}
    for idx, row in enumerate(matrix[:30]):
        m = {}
        for col, cell in enumerate(row):
            if cell is None:
                continue
            field = match_header(cell)
            if field and field not in m.values():
                m[col] = field
        if "title" in m.values() and len(m) >= 2:
            header_idx, mapping = idx, m
            break
    if header_idx is None:
        raise ValueError("Impossible de trouver la ligne d’en-tête (il faut au moins une colonne « Titre » ou « Tâche »).")
    out = []
    for row in matrix[header_idx + 1:]:
        d = {}
        for col, field in mapping.items():
            if col < len(row):
                d[field] = row[col]
        if any(v not in (None, "") for v in d.values()):
            out.append(d)
    return out


def read_table(stream, filename: str):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    data = stream.read() if hasattr(stream, "read") else open(stream, "rb").read()
    if ext in ("xlsx", "xlsm"):
        import openpyxl
        warnings.filterwarnings("ignore", module="openpyxl")
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
        best = None
        for ws in wb.worksheets:
            matrix = [list(r) for r in ws.iter_rows(values_only=True)]
            try:
                rows = _rows_from_matrix(matrix)
            except ValueError:
                continue
            if best is None or len(rows) > len(best):
                best = rows
            break  # première feuille exploitable
        if best is None:
            raise ValueError("Aucune feuille exploitable dans le classeur.")
        return best
    if ext in ("csv", "tsv", "txt"):
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
        try:
            dialect = csv.Sniffer().sniff(text[:4000], delimiters=";,\t")
            delim = dialect.delimiter
        except csv.Error:
            delim = ";" if text.count(";") > text.count(",") else ","
        matrix = list(csv.reader(io.StringIO(text), delimiter=delim))
        return _rows_from_matrix(matrix)
    if ext == "json":
        payload = json.loads(data.decode("utf-8"))
        items = payload.get("issues", payload) if isinstance(payload, dict) else payload
        rows = []
        for it in items:
            rows.append({
                "key": it.get("key"), "external_id": it.get("external_id"), "title": it.get("title"),
                "description": it.get("description"),
                "state": (it.get("state") or {}).get("name") if isinstance(it.get("state"), dict) else it.get("state"),
                "priority": it.get("priority"),
                "assignees": ", ".join(a.get("username", a.get("name", "")) if isinstance(a, dict) else str(a)
                                       for a in it.get("assignees") or []),
                "labels": ", ".join(l.get("name", "") if isinstance(l, dict) else str(l) for l in it.get("labels") or []),
                "module": ", ".join(m.get("name", "") if isinstance(m, dict) else str(m) for m in it.get("modules") or []),
                "cycle": (it.get("cycle") or {}).get("name") if isinstance(it.get("cycle"), dict) else it.get("cycle"),
                "start": it.get("start_date"), "target": it.get("target_date"), "estimate": it.get("estimate"),
                "parent": it.get("parent"),
            })
        return rows
    raise ValueError("Format non pris en charge (xlsx, csv ou json attendu).")


# ---------------------------------------------------------------------------
# Import dans un projet
# ---------------------------------------------------------------------------

def _text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v).strip()
    return "" if normalize(s) in EMPTY and len(s) <= 4 else s


def _split(v) -> list[str]:
    s = _text(v)
    if not s:
        return []
    parts = re.split(r"\s*(?:[,;/&+\n]|\bet\b)\s*", s)
    return [p.strip() for p in parts if p.strip()]


class UserMatcher:
    def __init__(self, project, create=False):
        self.project = project
        self.create = create
        self.index = {}
        for u in User.query.filter_by(active=True):
            for k in {u.username, u.first_name, u.last_name, u.display_name,
                      f"{u.last_name} {u.first_name}"}:
                if k and normalize(k):
                    self.index.setdefault(normalize(k), u)
        self.unknown = set()

    def find(self, name: str):
        n = normalize(name)
        if not n or n in GROUP_WORDS:
            return None
        if n in self.index:
            return self.index[n]
        close = difflib.get_close_matches(n, list(self.index), n=1, cutoff=0.82)
        if close:
            return self.index[close[0]]
        if self.create:
            base = re.sub(r"[^a-z0-9]+", ".", n).strip(".")[:40] or "membre"
            username, k = base, 1
            while User.query.filter_by(username=username).first():
                k += 1
                username = f"{base}{k}"
            parts = name.strip().split()
            u = User(username=username, email=User.internal_email(username),
                     first_name=parts[0].capitalize() if len(parts) > 1 else "",
                     last_name=" ".join(parts[1:]).capitalize() if len(parts) > 1 else name.strip().capitalize(),
                     avatar_color=random.choice(PALETTE), must_change_password=True)
            u.set_password(secrets.token_urlsafe(8))
            db.session.add(u)
            db.session.flush()
            self.index[n] = u
            return u
        self.unknown.add(name.strip())
        return None


def _map_state(project, value):
    v = normalize(value)
    if not v:
        return None
    for s in project.states:
        if normalize(s.name) == v:
            return s
    rules = [("cancelled", ("annul", "cancel", "abandon")), ("completed", ("termin", "done", "fait", "clos", "fini", "valid")),
             ("started", ("cours", "progress", "doing", "demarr", "revue", "review")),
             ("backlog", ("backlog", "idee", "plus tard")), ("unstarted", ("faire", "todo", "a venir", "planif", "nouveau"))]
    for group, words in rules:
        if any(w in v for w in words):
            return project.first_state_of(group) if group != "unstarted" else project.default_state()
    return None


def _map_priority(value):
    v = normalize(value)
    if not v:
        return "none"
    if "urgent" in v or "critique" in v or v in ("p0",):
        return "urgent"
    if v.startswith("haut") or "high" in v or "elev" in v or v in ("p1", "1"):
        return "high"
    if v.startswith("moy") or "medium" in v or "normal" in v or v in ("p2", "2"):
        return "medium"
    if v.startswith("bas") or "low" in v or "faible" in v or v in ("p3", "3"):
        return "low"
    for k, lab in PRIORITY_LABELS.items():
        if normalize(lab) == v or k == v:
            return k
    return "none"


def import_rows(project: Project, rows, actor: User, create_members=False, update_existing=False):
    report = {"created": 0, "updated": 0, "skipped": 0, "warnings": []}
    users = UserMatcher(project, create=create_members)
    by_ext: dict[str, Issue] = {}
    pending_deps, pending_parent = [], []
    touched_modules = set()
    for i in Issue.query.filter(Issue.project_id == project.id, Issue.external_id.isnot(None)):
        by_ext[i.external_id] = i
    for row in rows:
        title = _text(row.get("title"))
        if not title:
            report["skipped"] += 1
            continue
        ext = _text(row.get("external_id"))
        existing = None
        if update_existing:
            if ext and ext in by_ext:
                existing = by_ext[ext]
            key = _text(row.get("key"))
            if not existing and key and "-" in key and key.rsplit("-", 1)[1].isdigit():
                existing = Issue.query.filter_by(project_id=project.id,
                                                 sequence_id=int(key.rsplit("-", 1)[1])).first()
        # Description composée
        desc_parts = []
        if _text(row.get("description")):
            desc_parts.append(_text(row.get("description")))
        if _text(row.get("deliverable")):
            desc_parts.append(f"**Livrable attendu :** {_text(row.get('deliverable'))}")
        if _text(row.get("profile")):
            desc_parts.append(f"**Profil :** {_text(row.get('profile'))}")
        if _text(row.get("source")):
            desc_parts.append(f"**Source :** {_text(row.get('source'))}")
        data = {"title": title[:255], "description": "\n\n".join(desc_parts)}
        st = _map_state(project, row.get("state"))
        if st:
            data["state_id"] = st.id
        data["priority"] = _map_priority(row.get("priority"))
        people = []
        for name in _split(row.get("assignees")) + _split(row.get("support")):
            u = users.find(name)
            if u and u not in people:
                ensure_member(project, u)
                people.append(u)
        db.session.flush()
        if people:
            data["assignee_ids"] = [u.id for u in people]
        label_names = _split(row.get("labels")) if row.get("labels") is not None else []
        if re.match(r"^\s*jalon\b", title, re.I):
            label_names.append("Jalon")
        if re.match(r"^\s*marge\b", title, re.I):
            label_names.append("Marge")
        if label_names:
            data["label_ids"] = [get_or_create_label(project, n).id for n in label_names]
        mods = [m for m in re.split(r"\s*[,;]\s*", _text(row.get("module"))) if m]
        if mods:
            ms = [get_or_create_module(project, m) for m in mods]
            db.session.flush()
            data["module_ids"] = [m.id for m in ms]
            touched_modules.update(ms)
        cyc_name = _text(row.get("cycle"))
        if cyc_name:
            cyc = next((c for c in project.cycles if normalize(c.name) == normalize(cyc_name)), None)
            if cyc is None:
                cyc = Cycle(project=project, name=cyc_name[:160], owner=actor)
                db.session.add(cyc)
                db.session.flush()
            data["cycle_id"] = cyc.id
        for field, key in (("start", "start_date"), ("target", "target_date")):
            d = parse_date(row.get(field))
            if d:
                data[key] = d
        est = row.get("estimate")
        try:
            if est not in (None, ""):
                data["estimate"] = int(round(float(str(est).replace(",", "."))))
        except ValueError:
            pass
        db.session.flush()
        if existing:
            update_issue(existing, data, actor)
            issue = existing
            report["updated"] += 1
        else:
            issue = create_issue(project, data, actor)
            issue.external_id = ext[:60] if ext else None
            log_activity(issue, actor, "imported", new=ext or "fichier")
            if st and st.group == "completed":
                # date de clôture réaliste : l'échéance prévue si elle est passée
                tgt = data.get("target_date")
                issue.completed_at = (datetime.combine(tgt, datetime.min.time()).replace(hour=17)
                                      if tgt and tgt <= date.today() else now())
            report["created"] += 1
        if ext:
            by_ext[ext] = issue
        comment = _text(row.get("comments"))
        if comment and not existing:
            author = people[0] if people else actor
            db.session.add(Comment(issue=issue, author=author, body=comment))
        deps = _split(row.get("depends"))
        if deps:
            pending_deps.append((issue, deps))
        if _text(row.get("parent")):
            pending_parent.append((issue, _text(row.get("parent"))))
    db.session.flush()

    def resolve(ref):
        if ref in by_ext:
            return by_ext[ref]
        if "-" in ref and ref.rsplit("-", 1)[1].isdigit():
            return Issue.query.filter_by(project_id=project.id, sequence_id=int(ref.rsplit("-", 1)[1])).first()
        return None

    for issue, deps in pending_deps:
        for ref in deps:
            other = resolve(ref)
            if other is None:
                report["warnings"].append(f"Dépendance introuvable « {ref} » pour « {issue.title[:40]} ».")
            elif not IssueRelation.query.filter_by(issue_id=issue.id, related_id=other.id).first():
                add_relation(issue, other, "blocked_by", actor)
    for issue, ref in pending_parent:
        parent = resolve(ref)
        if parent:
            update_issue(issue, {"parent_id": parent.id}, actor, log=False)
    # Dates et statut des modules à partir de leurs tickets
    for m in touched_modules:
        refresh_module(m)
    if users.unknown:
        report["warnings"].append("Membres non reconnus (ignorés) : " + ", ".join(sorted(users.unknown))
                                  + ". Coche « créer les membres manquants » pour les ajouter.")
    return report


def refresh_module(m: Module):
    issues = m.active_issues
    starts = [i.start_date for i in issues if i.start_date]
    ends = [i.target_date for i in issues if i.target_date]
    if starts and not m.start_date:
        m.start_date = min(starts)
    if ends and not m.target_date:
        m.target_date = max(ends)
    groups = [i.state_group for i in issues]
    if groups and all(g in ("completed", "cancelled") for g in groups):
        m.status = "completed"
    elif any(g in ("started", "completed") for g in groups):
        m.status = "in-progress"
    else:
        m.status = "planned"


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

EXPORT_COLUMNS = ["Identifiant", "Titre", "Description", "État", "Groupe d’état", "Priorité", "Assignés",
                  "Étiquettes", "Modules", "Cycle", "Début", "Échéance", "Estimation", "Parent", "Bloqué par",
                  "Temps passé", "Créé par", "Créé le", "Mis à jour le", "Terminé le", "ID externe"]


def _issue_row(i: Issue):
    blockers = [r.related.key for r in IssueRelation.query.filter_by(issue_id=i.id, relation_type="blocked_by")]
    return [
        i.key, i.title, i.description or "", i.state.name if i.state else "",
        STATE_GROUP_LABELS.get(i.state_group, ""), PRIORITY_LABELS.get(i.priority, ""),
        ", ".join(u.display_name for u in i.assignees), ", ".join(l.name for l in i.labels),
        ", ".join(m.name for m in i.modules), i.cycle.name if i.cycle else "",
        i.start_date, i.target_date, i.estimate, i.parent.key if i.parent else "", ", ".join(blockers),
        fmt_minutes(i.time_spent_minutes) if i.worklogs else "", i.created_by.display_name if i.created_by else "",
        i.created_at, i.updated_at, i.completed_at, i.external_id or "",
    ]


def export_csv(issues) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(EXPORT_COLUMNS)
    for i in issues:
        row = _issue_row(i)
        w.writerow([v.strftime("%d/%m/%Y %H:%M") if isinstance(v, datetime) else
                    v.strftime("%d/%m/%Y") if isinstance(v, date) else ("" if v is None else v) for v in row])
    return "﻿" + buf.getvalue()


def export_xlsx(issues, title="Tickets") -> io.BytesIO:
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Tickets"
    ws.append(EXPORT_COLUMNS)
    head_fill = PatternFill("solid", fgColor="3F76FF")
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = head_fill
        c.alignment = Alignment(vertical="center")
    for i in issues:
        ws.append(_issue_row(i))
    widths = [12, 50, 40, 14, 14, 11, 26, 24, 24, 16, 12, 12, 10, 10, 16, 12, 18, 17, 17, 17, 12]
    for idx, wdt in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = wdt
    for row in ws.iter_rows(min_row=2):
        for c in row:
            if isinstance(c.value, datetime):
                c.number_format = "DD/MM/YYYY HH:MM"
            elif isinstance(c.value, date):
                c.number_format = "DD/MM/YYYY"
        row[2].alignment = Alignment(wrap_text=False)
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = ws.dimensions
    # Synthèse
    syn = wb.create_sheet("Synthèse")
    syn.append([title])
    syn["A1"].font = Font(bold=True, size=14)
    syn.append([f"Export du {date.today():%d/%m/%Y} — {len(issues)} tickets"])
    syn.append([])
    syn.append(["Groupe d’état", "Nombre"])
    for g, lab in STATE_GROUP_LABELS.items():
        syn.append([lab, sum(1 for i in issues if i.state_group == g)])
    syn.append([])
    syn.append(["Membre", "Ouverts", "Terminés", "En retard"])
    people = {}
    for i in issues:
        for u in i.assignees or [None]:
            name = u.display_name if u else "Non assigné"
            d = people.setdefault(name, [0, 0, 0])
            d[0] += 0 if i.is_closed else 1
            d[1] += 1 if i.state_group == "completed" else 0
            d[2] += 1 if i.is_overdue else 0
    for name, d in sorted(people.items()):
        syn.append([name] + d)
    for r in (4, 6 + len(STATE_GROUP_LABELS)):
        for c in syn[r]:
            c.font = Font(bold=True)
    syn.column_dimensions["A"].width = 28
    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    return out


def export_json(issues) -> str:
    from .services import serialize_issue
    return json.dumps({"exported_at": now().isoformat(), "issues": [serialize_issue(i) for i in issues]},
                      ensure_ascii=False, indent=2)


def export_markdown(issues, project) -> str:
    lines = [f"# {project.name} — tickets", "", f"_Export du {date.today():%d/%m/%Y}_", ""]
    by_state = {}
    for i in issues:
        by_state.setdefault(i.state.name if i.state else "Sans état", []).append(i)
    for st in project.states:
        items = by_state.get(st.name, [])
        if not items:
            continue
        lines += [f"## {st.name} ({len(items)})", ""]
        for i in items:
            meta = []
            if i.assignees:
                meta.append(", ".join(u.display_name for u in i.assignees))
            if i.target_date:
                meta.append(f"échéance {i.target_date:%d/%m/%Y}")
            if i.priority != "none":
                meta.append(PRIORITY_LABELS[i.priority].lower())
            check = "x" if i.state_group == "completed" else " "
            lines.append(f"- [{check}] **{i.key}** {i.title}" + (f" — _{' · '.join(meta)}_" if meta else ""))
        lines.append("")
    return "\n".join(lines)


def export_workspace_json() -> str:
    """Export complet lisible (sans mots de passe) pour archivage ou migration."""
    from .models import Activity, Label, Page, State, WorkLog, Workspace

    def rows(model, exclude=()):
        out = []
        for obj in model.query.all():
            d = {}
            for col in model.__table__.columns:
                if col.name in exclude:
                    continue
                v = getattr(obj, col.name)
                d[col.name] = v.isoformat() if isinstance(v, (date, datetime)) else v
            out.append(d)
        return out

    payload = {
        "format": "hangar-export", "version": 1, "exported_at": now().isoformat(),
        "workspace": rows(Workspace), "users": rows(User, exclude=("password_hash",)),
        "projects": rows(Project), "members": rows(ProjectMember), "states": rows(State), "labels": rows(Label),
        "cycles": rows(Cycle), "modules": rows(Module), "issues": rows(Issue), "comments": rows(Comment),
        "relations": rows(IssueRelation), "worklogs": rows(WorkLog), "pages": rows(Page),
        "activities": rows(Activity),
    }
    return json.dumps(payload, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------------------
# Word (.docx) -> Markdown
# ---------------------------------------------------------------------------

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _run_text(r) -> str:
    out = []
    for node in r:
        if node.tag == W + "t":
            out.append(node.text or "")
        elif node.tag == W + "tab":
            out.append("\t")
        elif node.tag in (W + "br", W + "cr"):
            out.append("  \n")
    text = "".join(out)
    if not text.strip():
        return text
    rpr = r.find(W + "rPr")
    bold = rpr is not None and rpr.find(W + "b") is not None and rpr.find(W + "b").get(W + "val") not in ("0", "false")
    italic = rpr is not None and rpr.find(W + "i") is not None and rpr.find(W + "i").get(W + "val") not in ("0", "false")
    lead = text[: len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()):]
    core = text.strip()
    if bold:
        core = f"**{core}**"
    if italic:
        core = f"_{core}_"
    return lead + core + trail


def _para_text(p) -> str:
    parts = []
    for child in p:
        if child.tag == W + "r":
            parts.append(_run_text(child))
        elif child.tag == W + "hyperlink":
            for r in child.findall(W + "r"):
                parts.append(_run_text(r))
    text = "".join(parts)
    return text.replace("****", "").replace("__", "")


def _heading_level(p) -> int:
    ppr = p.find(W + "pPr")
    if ppr is None:
        return 0
    style = ppr.find(W + "pStyle")
    if style is None:
        return 0
    val = (style.get(W + "val") or "").lower()
    if val in ("title", "titre"):
        return 1
    m = re.match(r"(?:heading|titre|titre)(\d)", val)
    if m:
        return min(int(m.group(1)) + 1, 4)
    return 0


def docx_to_markdown(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    body = root.find(W + "body")
    lines = []
    for el in body:
        if el.tag == W + "p":
            text = _para_text(el).rstrip()
            if not text.strip():
                if lines and lines[-1] != "":
                    lines.append("")
                continue
            level = _heading_level(el)
            ppr = el.find(W + "pPr")
            is_list = ppr is not None and ppr.find(W + "numPr") is not None
            if level:
                lines += ["", "#" * level + " " + re.sub(r"\*\*|_", "", text).strip(), ""]
            elif is_list:
                ilvl = ppr.find(W + "numPr").find(W + "ilvl")
                depth = int(ilvl.get(W + "val", "0")) if ilvl is not None else 0
                lines.append("  " * depth + "- " + text.strip())
            else:
                if lines and lines[-1].startswith("- "):
                    lines.append("")
                lines += [text, ""]
        elif el.tag == W + "tbl":
            rows = []
            for tr in el.findall(W + "tr"):
                cells = []
                for tc in tr.findall(W + "tc"):
                    cell = " ".join(_para_text(p).strip() for p in tc.findall(W + "p") if _para_text(p).strip())
                    cells.append(cell.replace("|", "\\|"))
                rows.append(cells)
            if rows:
                width = max(len(r) for r in rows)
                rows = [r + [""] * (width - len(r)) for r in rows]
                lines.append("")
                lines.append("| " + " | ".join(rows[0]) + " |")
                lines.append("|" + "---|" * width)
                for r in rows[1:]:
                    lines.append("| " + " | ".join(r) + " |")
                lines.append("")
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"

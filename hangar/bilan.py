"""Bilan d'avancement : calcul des indicateurs sur une période, export Markdown et Word.

Le résultat de `compute_bilan` est un dictionnaire sérialisable en JSON : il sert à l'affichage,
aux bilans enregistrés (instantané), aux exports et aux e-mails.
"""
from __future__ import annotations

import calendar
import io
from datetime import date, datetime, timedelta

from flask import url_for
from sqlalchemy.orm import selectinload

from .models import (
    PRIORITY_LABELS, PRIORITY_ORDER, Activity, Comment, Issue, Page,
    Project, WorkLog, blockers_map,
)
from .texte import cle_tri
from .utils import MONTHS_FULL, fmt_date, fmt_minutes, week_bounds

PERIODS = [
    ("week", "Cette semaine"),
    ("last_week", "Semaine dernière"),
    ("month", "Ce mois-ci"),
    ("last_month", "Mois dernier"),
    ("since_start", "Depuis le début"),
    ("custom", "Personnalisée"),
]
HEALTH = {
    "green": ("Dans les temps", "#16a34a"),
    "orange": ("Vigilance", "#f59e0b"),
    "red": ("En retard", "#dc2626"),
    "grey": ("Non évalué", "#a3a3a3"),
}


def period_bounds(kind: str, projects, start_s=None, end_s=None):
    """Retourne (début, fin, libellé) pour un type de période."""
    from .utils import parse_date
    today = date.today()
    if kind == "week":
        s, e = week_bounds(today)
    elif kind == "last_week":
        s, e = week_bounds(today - timedelta(days=7))
    elif kind == "month":
        s = today.replace(day=1)
        e = today.replace(day=calendar.monthrange(today.year, today.month)[1])
    elif kind == "last_month":
        e = today.replace(day=1) - timedelta(days=1)
        s = e.replace(day=1)
    elif kind == "custom":
        s = parse_date(start_s) or today - timedelta(days=13)
        e = parse_date(end_s) or today
        if e < s:
            s, e = e, s
    else:  # depuis le début
        kind = "since_start"
        starts = [p.start_date for p in projects if p.start_date]
        if not starts:
            first = (Issue.query.filter(Issue.project_id.in_([p.id for p in projects] or [0]))
                     .order_by(Issue.created_at).first())
            starts = [first.created_at.date()] if first else [today]
        s, e = min(starts), today
    if kind in ("month", "last_month"):
        label = f"{MONTHS_FULL[s.month - 1].capitalize()} {s.year}"
    elif kind == "since_start":
        label = f"Depuis le {fmt_date(s, True)}"
    else:
        label = f"Du {fmt_date(s, True)} au {fmt_date(e, True)}"
    return s, e, label, kind


def _d(v):
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    return v.isoformat()


def _issue(i: Issue, today: date) -> dict:
    return {
        "id": i.id, "key": i.key, "title": i.title,
        "url": url_for("issues.detail", pid=i.project_id, seq=i.sequence_id),
        "state": i.state.name if i.state else "", "state_group": i.state_group,
        "state_color": i.state.color if i.state else None,
        "priority": i.priority, "priority_label": PRIORITY_LABELS.get(i.priority, ""),
        "assignees": [u.display_name for u in i.assignees],
        "module": i.modules[0].name if i.modules else "Sans module",
        "project": i.project.name, "project_emoji": i.project.emoji,
        "start": _d(i.start_date), "target": _d(i.target_date), "completed": _d(i.completed_at),
        "days_late": (today - i.target_date).days if i.target_date and i.target_date < today and not i.is_closed else 0,
        "estimate": i.estimate,
    }


def compute_bilan(projects: list[Project], start: date, end: date, label: str | None = None) -> dict:
    today = date.today()
    pids = [p.id for p in projects] or [0]
    s_dt = datetime.combine(start, datetime.min.time())
    e_dt = datetime.combine(end, datetime.max.time())
    # Relations lues pour chaque ticket (assignés, étiquettes, modules…) chargées d'avance : quelques
    # requêtes en tout au lieu de plusieurs par ticket.
    issues = (Issue.query.filter(Issue.project_id.in_(pids), Issue.is_visible_clause())
              .options(selectinload(Issue.assignees), selectinload(Issue.labels), selectinload(Issue.modules),
                       selectinload(Issue.state), selectinload(Issue.project))
              .all())
    live = [i for i in issues if i.archived_at is None]
    counted = [i for i in issues if i.state_group != "cancelled"]

    done_total = sum(1 for i in counted if i.state_group == "completed")
    done_before = sum(1 for i in counted if i.state_group == "completed" and i.completed_at and i.completed_at < s_dt)
    total_before = sum(1 for i in counted if i.created_at and i.created_at < s_dt) or 0
    completed = sorted([i for i in issues if i.state_group == "completed" and i.completed_at
                        and s_dt <= i.completed_at <= e_dt], key=lambda i: i.completed_at)
    created = [i for i in issues if i.created_at and s_dt <= i.created_at <= e_dt]
    open_now = [i for i in live if not i.is_closed]
    in_progress = [i for i in open_now if i.state_group == "started"]
    overdue = sorted([i for i in open_now if i.is_overdue], key=lambda i: i.target_date)
    horizon = max(today, end) + timedelta(days=14)
    upcoming = sorted([i for i in open_now if i.target_date and max(today, start) <= i.target_date <= horizon],
                      key=lambda i: (i.target_date, PRIORITY_ORDER.get(i.priority, 9)))
    blocked = []
    bloqueurs = blockers_map(i.id for i in open_now)
    for i in open_now:
        bl = bloqueurs[i.id]
        if bl:
            blocked.append({**_issue(i, today), "blockers": [b.key for b in bl]})
    unassigned_hot = [i for i in open_now if not i.assignees and i.priority in ("urgent", "high")]

    # Jalons
    milestones = []
    for i in live:
        if any(l.name.lower() == "jalon" for l in i.labels):
            st = "done" if i.state_group == "completed" else ("late" if i.is_overdue else "upcoming")
            milestones.append({**_issue(i, today), "status": st})
    milestones.sort(key=lambda m: m["target"] or "9999")

    # Modules et cycles
    modules = []
    for p in projects:
        for m in p.modules:
            if m.archived_at:
                continue
            pr = m.progress()
            late = bool(m.target_date and m.target_date < today and pr["pct"] < 100)
            modules.append({"name": m.name, "project": p.name, "status": m.status_label, "status_color": m.status_color,
                            "pct": pr["pct"], "done": pr["done"], "total": pr["total"],
                            "start": _d(m.start_date), "target": _d(m.target_date), "late": late,
                            "url": url_for("modules.detail", pid=p.id, mid=m.id)})
    cycles = []
    for p in projects:
        for c in p.cycles:
            if c.archived_at or not c.start_date or not c.end_date:
                continue
            if c.end_date < start or c.start_date > end:
                continue
            pr = c.progress()
            cycles.append({"name": c.name, "project": p.name, "start": _d(c.start_date), "end": _d(c.end_date),
                           "pct": pr["pct"], "done": pr["done"], "total": pr["total"], "status": c.status_label,
                           "url": url_for("cycles.detail", pid=p.id, cid=c.id)})

    # Temps, commentaires, documents, membres
    logs = (WorkLog.query.join(Issue).filter(Issue.project_id.in_(pids), WorkLog.date >= start, WorkLog.date <= end).all())
    comments = (Comment.query.join(Issue).filter(Issue.project_id.in_(pids), Comment.created_at >= s_dt,
                                                 Comment.created_at <= e_dt).all())
    pages = (Page.query.filter(Page.project_id.in_(pids), Page.archived_at.is_(None), Page.is_public.is_(True),
                               Page.updated_at >= s_dt, Page.updated_at <= e_dt)
             .order_by(Page.updated_at.desc()).all())
    activity_count = Activity.query.filter(Activity.project_id.in_(pids), Activity.created_at >= s_dt,
                                           Activity.created_at <= e_dt).count()
    members = {}
    for p in projects:
        for u in p.member_users:
            members[u.id] = u
    member_rows = []
    for u in sorted(members.values(), key=lambda u: cle_tri(u.display_name)):
        mine_open = [i for i in open_now if any(a.id == u.id for a in i.assignees)]
        member_rows.append({
            "name": u.display_name, "username": u.username, "color": u.avatar_color, "initials": u.initials,
            "url": url_for("team.profile", username=u.username),
            "completed": sum(1 for i in completed if any(a.id == u.id for a in i.assignees)),
            "open": len(mine_open), "overdue": sum(1 for i in mine_open if i.is_overdue),
            "minutes": sum(w.minutes for w in logs if w.user_id == u.id),
            "comments": sum(1 for c in comments if c.author_id == u.id),
        })
    time_by_module = {}
    for w in logs:
        name = w.issue.modules[0].name if w.issue.modules else "Sans module"
        time_by_module[name] = time_by_module.get(name, 0) + w.minutes

    # Indicateurs globaux et météo
    total = len(counted)
    progress = round(100 * done_total / total) if total else 0
    progress_start = round(100 * done_before / total_before) if total_before else 0
    health = _health(projects, progress, len(overdue), len(open_now), today)
    attention = []
    if overdue:
        worst = overdue[0]
        attention.append(f"{len(overdue)} ticket(s) en retard, dont {worst.key} « {worst.title} » "
                         f"({(today - worst.target_date).days} j de retard).")
    if blocked:
        attention.append(f"{len(blocked)} ticket(s) bloqué(s) par des tâches non terminées.")
    if unassigned_hot:
        attention.append(f"{len(unassigned_hot)} ticket(s) prioritaire(s) sans responsable : "
                         + ", ".join(i.key for i in unassigned_hot[:6]) + ("…" if len(unassigned_hot) > 6 else "") + ".")
    late_ms = [m for m in milestones if m["status"] == "late"]
    if late_ms:
        attention.append(f"Jalon en retard : {late_ms[0]['title']}.")
    late_mods = [m for m in modules if m["late"]]
    if late_mods:
        attention.append("Module(s) dépassant leur date cible : " + ", ".join(m["name"] for m in late_mods) + ".")
    idle = [m["name"] for m in member_rows if m["open"] and not m["completed"] and not m["comments"]]
    if idle and len(idle) < len(member_rows):
        attention.append("Sans avancée visible sur la période : " + ", ".join(idle) + ".")
    if not completed and (end - start).days >= 6:
        attention.append("Aucun ticket terminé sur la période.")

    return {
        "version": 1,
        "generated_at": datetime.now().replace(microsecond=0).isoformat(),
        "scope": {"label": projects[0].name if len(projects) == 1 else "Tous les projets",
                  "projects": [{"id": p.id, "name": p.name, "emoji": p.emoji, "identifier": p.identifier}
                               for p in projects]},
        "period": {"start": start.isoformat(), "end": end.isoformat(),
                   "label": label or f"Du {fmt_date(start, True)} au {fmt_date(end, True)}",
                   "days": (end - start).days + 1},
        "kpis": {"total": total, "done_total": done_total, "progress": progress, "progress_start": progress_start,
                 "completed": len(completed), "created": len(created), "open": len(open_now),
                 "in_progress": len(in_progress), "overdue": len(overdue), "blocked": len(blocked),
                 "time_minutes": sum(w.minutes for w in logs), "comments": len(comments), "pages": len(pages),
                 "activity": activity_count,
                 "points_done": sum(i.estimate or 0 for i in completed)},
        "health": health,
        "attention": attention,
        "completed": [_issue(i, today) for i in completed],
        "in_progress": [_issue(i, today) for i in in_progress],
        "overdue": [_issue(i, today) for i in overdue],
        "blocked": blocked,
        "upcoming": [_issue(i, today) for i in upcoming[:25]],
        "unassigned": [_issue(i, today) for i in unassigned_hot],
        "milestones": milestones,
        "modules": modules,
        "cycles": cycles,
        "members": member_rows,
        "documents": [{"title": p.title, "emoji": p.emoji, "url": url_for("pages.view", pid=p.project_id, page_id=p.id),
                       "updated": _d(p.updated_at), "author": p.updated_by.display_name if p.updated_by else ""}
                      for p in pages],
        "time_by_module": sorted(({"name": k, "minutes": v} for k, v in time_by_module.items()),
                                 key=lambda x: -x["minutes"]),
    }


def _health(projects, progress, n_overdue, n_open, today):
    starts = [p.start_date for p in projects if p.start_date]
    ends = [p.target_date for p in projects if p.target_date]
    elapsed = None
    if starts and ends and max(ends) > min(starts):
        s, e = min(starts), max(ends)
        elapsed = max(0, min(100, round(100 * (today - s).days / (e - s).days)))
    overdue_ratio = n_overdue / n_open if n_open else 0
    if elapsed is None:
        level = "grey" if not n_open else ("red" if overdue_ratio > .3 else "orange" if overdue_ratio > .1 else "green")
        detail = f"{n_overdue} ticket(s) en retard sur {n_open} ouverts."
    else:
        gap = elapsed - progress
        if gap <= 10 and overdue_ratio <= .15:
            level = "green"
        elif gap <= 25 and overdue_ratio <= .35:
            level = "orange"
        else:
            level = "red"
        detail = (f"{elapsed} % du temps écoulé pour {progress} % des tickets terminés ; "
                  f"{n_overdue} ticket(s) en retard sur {n_open} ouverts.")
    lab, col = HEALTH[level]
    return {"level": level, "label": lab, "color": col, "elapsed": elapsed, "progress": progress, "detail": detail}


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

MANUAL_SECTIONS = [
    ("highlights", "Faits marquants"),
    ("difficulties", "Difficultés et risques"),
    ("decisions", "Décisions prises"),
    ("next_steps", "Prochaines étapes"),
]


def _fd(iso):
    if not iso:
        return "—"
    return fmt_date(date.fromisoformat(iso[:10]), True)


def _line(i, with_date=None):
    who = f" — {', '.join(i['assignees'])}" if i.get("assignees") else ""
    when = f" ({with_date} {_fd(i[ 'completed' if with_date == 'terminé le' else 'target'])})" if with_date else ""
    return f"- **{i['key']}** {i['title']}{who}{when}"


def bilan_markdown(data: dict, manual: dict | None = None, title: str | None = None) -> str:
    """Bilan au format Markdown (export, e-mail)."""
    manual = manual or {}
    k = data.get("kpis", {})
    h = data.get("health", {})
    out = [f"# {title or 'Bilan — ' + data['scope']['label']}", "",
           f"_{data['period']['label']} · généré le {_fd(data.get('generated_at'))}_", "",
           f"**Météo du projet : {h.get('label', '')}** — {h.get('detail', '')}", "",
           "## Indicateurs", "",
           "| Indicateur | Valeur |", "|---|---|",
           f"| Avancement global | {k.get('progress', 0)} % ({k.get('done_total', 0)}/{k.get('total', 0)} tickets) |",
           f"| Tickets terminés sur la période | {k.get('completed', 0)} |",
           f"| Tickets créés sur la période | {k.get('created', 0)} |",
           f"| En cours | {k.get('in_progress', 0)} |",
           f"| En retard | {k.get('overdue', 0)} |",
           f"| Bloqués | {k.get('blocked', 0)} |",
           f"| Temps saisi | {fmt_minutes(k.get('time_minutes', 0))} |", ""]
    for key, lab in MANUAL_SECTIONS[:1]:
        if manual.get(key):
            out += [f"## {lab}", "", manual[key].strip(), ""]
    if data.get("attention"):
        out += ["## Points d’attention", ""] + [f"- {a}" for a in data["attention"]] + [""]
    if data.get("completed"):
        out += [f"## Réalisé sur la période ({len(data['completed'])})", ""]
        by_mod = {}
        for i in data["completed"]:
            by_mod.setdefault(i["module"], []).append(i)
        for mod, items in by_mod.items():
            out += [f"### {mod}", ""] + [_line(i, "terminé le") for i in items] + [""]
    if data.get("in_progress"):
        out += ["## En cours", ""] + [_line(i, "échéance") for i in data["in_progress"]] + [""]
    if data.get("overdue"):
        out += ["## En retard", ""] + [f"{_line(i, 'échéance')} — {i['days_late']} j de retard" for i in data["overdue"]] + [""]
    if data.get("milestones"):
        st = {"done": "✅ atteint", "late": "⚠️ en retard", "upcoming": "⏳ à venir"}
        out += ["## Jalons", ""] + [f"- {st[m['status']]} — **{m['title']}** ({_fd(m['target'])})" for m in data["milestones"]] + [""]
    if data.get("modules"):
        out += ["## Avancement par module", "", "| Module | Statut | Avancement | Échéance |", "|---|---|---|---|"]
        out += [f"| {m['name']} | {m['status']} | {m['pct']} % ({m['done']}/{m['total']}) | {_fd(m['target'])} |"
                for m in data["modules"]] + [""]
    if data.get("members"):
        out += ["## Contributions de l’équipe", "", "| Membre | Terminés | Ouverts | En retard | Temps | Commentaires |",
                "|---|---|---|---|---|---|"]
        out += [f"| {m['name']} | {m['completed']} | {m['open']} | {m['overdue']} | {fmt_minutes(m['minutes'])} | {m['comments']} |"
                for m in data["members"]] + [""]
    if data.get("documents"):
        out += ["## Documents produits ou mis à jour", ""] + [f"- {d['title']} ({_fd(d['updated'])})" for d in data["documents"]] + [""]
    for key, lab in MANUAL_SECTIONS[1:]:
        if manual.get(key):
            out += [f"## {lab}", "", manual[key].strip(), ""]
    if data.get("upcoming") and not manual.get("next_steps"):
        out += ["## Prochaines échéances", ""] + [_line(i, "échéance") for i in data["upcoming"][:12]] + [""]
    return "\n".join(out)


def bilan_docx(data: dict, manual: dict | None = None, title: str | None = None) -> io.BytesIO:
    """Bilan au format Word (.docx)."""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor

    from .utils import xml_safe, xml_safe_deep

    # Un caractère de contrôle collé depuis PowerPoint (titre de ticket, nom de module…) ferait échouer Word :
    # tout le contenu est nettoyé ici, bilans enregistrés compris.
    data, manual, title = xml_safe_deep(data), xml_safe_deep(manual or {}), xml_safe(title)
    k = data.get("kpis", {})
    h = data.get("health", {})
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(10.5)

    def shade(cell, hex_color):
        tcpr = cell._tc.get_or_add_tcPr()
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"), hex_color)
        tcpr.append(shd)

    def table(headers, rows, widths=None):
        t = doc.add_table(rows=1, cols=len(headers))
        t.style = "Table Grid"
        for idx, hd in enumerate(headers):
            c = t.rows[0].cells[idx]
            c.text = ""
            run = c.paragraphs[0].add_run(hd)
            run.bold = True
            run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
            shade(c, "3F76FF")
        for r in rows:
            cells = t.add_row().cells
            for idx, v in enumerate(r):
                cells[idx].text = str(v)
        doc.add_paragraph()
        return t

    def md_block(text):
        for raw in (text or "").strip().splitlines():
            line = raw.rstrip()
            if not line:
                continue
            if line.lstrip().startswith(("- ", "* ")):
                doc.add_paragraph(line.lstrip()[2:].replace("**", ""), style="List Bullet")
            elif line.startswith("#"):
                doc.add_heading(line.lstrip("#").strip(), level=3)
            else:
                doc.add_paragraph(line.replace("**", ""))

    head = doc.add_heading(title or f"Bilan — {data['scope']['label']}", level=0)
    head.alignment = WD_ALIGN_PARAGRAPH.LEFT
    p = doc.add_paragraph()
    r = p.add_run(f"{data['period']['label']} · généré le {_fd(data.get('generated_at'))}")
    r.italic = True
    p = doc.add_paragraph()
    r = p.add_run(f"Météo du projet : {h.get('label', '')}. ")
    r.bold = True
    col = (h.get("color") or "#000000").lstrip("#")
    r.font.color.rgb = RGBColor(int(col[0:2], 16), int(col[2:4], 16), int(col[4:6], 16))
    p.add_run(h.get("detail", ""))

    doc.add_heading("Indicateurs", level=1)
    table(["Indicateur", "Valeur"], [
        ["Avancement global", f"{k.get('progress', 0)} % ({k.get('done_total', 0)}/{k.get('total', 0)} tickets)"],
        ["Tickets terminés sur la période", k.get("completed", 0)],
        ["Tickets créés sur la période", k.get("created", 0)],
        ["En cours", k.get("in_progress", 0)],
        ["En retard", k.get("overdue", 0)],
        ["Bloqués", k.get("blocked", 0)],
        ["Temps saisi", fmt_minutes(k.get("time_minutes", 0))],
    ])
    if manual.get("highlights"):
        doc.add_heading("Faits marquants", level=1)
        md_block(manual["highlights"])
    if data.get("attention"):
        doc.add_heading("Points d’attention", level=1)
        for a in data["attention"]:
            doc.add_paragraph(a, style="List Bullet")
    if data.get("completed"):
        doc.add_heading(f"Réalisé sur la période ({len(data['completed'])})", level=1)
        table(["Ticket", "Titre", "Module", "Responsables", "Terminé le"],
              [[i["key"], i["title"], i["module"], ", ".join(i["assignees"]) or "—", _fd(i["completed"])]
               for i in data["completed"]])
    if data.get("in_progress"):
        doc.add_heading("En cours", level=1)
        table(["Ticket", "Titre", "Responsables", "Échéance"],
              [[i["key"], i["title"], ", ".join(i["assignees"]) or "—", _fd(i["target"])] for i in data["in_progress"]])
    if data.get("overdue"):
        doc.add_heading("En retard", level=1)
        table(["Ticket", "Titre", "Responsables", "Échéance", "Retard"],
              [[i["key"], i["title"], ", ".join(i["assignees"]) or "—", _fd(i["target"]), f"{i['days_late']} j"]
               for i in data["overdue"]])
    if data.get("milestones"):
        doc.add_heading("Jalons", level=1)
        st = {"done": "Atteint", "late": "En retard", "upcoming": "À venir"}
        table(["Jalon", "Date", "Statut"], [[m["title"], _fd(m["target"]), st[m["status"]]] for m in data["milestones"]])
    if data.get("modules"):
        doc.add_heading("Avancement par module", level=1)
        table(["Module", "Statut", "Avancement", "Échéance"],
              [[m["name"], m["status"], f"{m['pct']} % ({m['done']}/{m['total']})", _fd(m["target"])] for m in data["modules"]])
    if data.get("members"):
        doc.add_heading("Contributions de l’équipe", level=1)
        table(["Membre", "Terminés", "Ouverts", "En retard", "Temps", "Commentaires"],
              [[m["name"], m["completed"], m["open"], m["overdue"], fmt_minutes(m["minutes"]), m["comments"]]
               for m in data["members"]])
    if data.get("documents"):
        doc.add_heading("Documents produits ou mis à jour", level=1)
        for d in data["documents"]:
            doc.add_paragraph(f"{d['title']} ({_fd(d['updated'])})", style="List Bullet")
    for key, lab in MANUAL_SECTIONS[1:]:
        if manual.get(key):
            doc.add_heading(lab, level=1)
            md_block(manual[key])
    if data.get("upcoming") and not manual.get("next_steps"):
        doc.add_heading("Prochaines échéances", level=1)
        table(["Ticket", "Titre", "Responsables", "Échéance"],
              [[i["key"], i["title"], ", ".join(i["assignees"]) or "—", _fd(i["target"])] for i in data["upcoming"][:15]])
    out = io.BytesIO()
    doc.save(out)
    out.seek(0)
    return out


def scope_projects(user, pid=None, sel=None):
    """Projets couverts par un bilan (un projet, ou tous les projets visibles)."""
    from .utils import get_project, visible_projects
    if pid:
        return [get_project(pid)]
    projects = visible_projects(user)
    if sel and str(sel).isdigit():
        projects = [p for p in projects if p.id == int(sel)] or projects
    return projects

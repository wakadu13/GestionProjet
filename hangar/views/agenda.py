"""Agenda personnel : tableau de bord, calendrier, tâches, rappels, imports d'emploi du temps."""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

from flask import (Blueprint, Response, abort, flash, g, jsonify, redirect, render_template, request, send_file, session,
                   url_for)
from flask_login import current_user, login_required

from .. import agenda as A
from .. import ics
from ..extensions import db
from ..models import (PRIORITY_LABELS, RECURRENCES, REMINDER_OFFSETS, TASK_COLORS, AgendaEvent, AgendaImport,
                      AgendaReminder, AgendaTask, now)
from ..services import to_int
from ..utils import back_url, parse_date, plier

bp = Blueprint("agenda", __name__)

# Rappels proposés en cases à cocher dans la fenêtre de tâche
QUICK_OFFSETS = [(0, "À l’heure"), (15, "15 min avant"), (60, "1 h avant"), (1440, "La veille"),
                 (2880, "2 jours avant"), (10080, "1 semaine avant")]


def _fetch() -> bool:
    return request.headers.get("X-Requested-With") == "fetch"


def parse_time(value) -> time | None:
    m = re.fullmatch(r"\s*(\d{1,2})(?:[:hH](\d{2}))?\s*", value or "")
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if h > 23 or mi > 59:
        return None
    return time(h, mi)


def parse_datetime_local(value) -> datetime | None:
    try:
        return datetime.fromisoformat((value or "").strip()) if value else None
    except ValueError:
        return None


@bp.app_context_processor
def _agenda_context():
    def agenda_cfg():
        if "agenda_cfg" in g:
            return g.agenda_cfg
        if not current_user.is_authenticated:
            return {}
        s = A.settings_of(current_user)
        today = date.today()
        due = AgendaTask.query.filter(AgendaTask.user_id == current_user.id, AgendaTask.done_at.is_(None),
                                      AgendaTask.due_date <= today).all()
        g.agenda_cfg = {
            "default_reminder": s.default_reminder if s.default_reminder is not None else -1,
            "channel": s.reminder_channel or "email",
            "categories": A.categories(current_user),
            "has_email": bool(A._mail().valid_email(current_user.contact_email)),
            "due": len(due), "overdue": sum(1 for t in due if t.due_date < today),
        }
        return g.agenda_cfg
    return {"A": A, "agenda_cfg": agenda_cfg, "QUICK_OFFSETS": QUICK_OFFSETS, "REMINDER_OFFSETS": REMINDER_OFFSETS,
            "RECURRENCES": RECURRENCES, "TASK_COLORS": TASK_COLORS, "PRIORITY_LABELS": PRIORITY_LABELS}


def _payloads(events=(), tasks=()):
    recent = A.recent_import_ids(current_user)
    evs = {}
    for e in events:
        if e is not None and e.id not in evs:
            evs[e.id] = A.event_payload(e, recent, url_for)
    tks = {}
    for t in tasks:
        if t is not None and t.id not in tks:
            tks[t.id] = A.task_payload(t, url_for)
    return {"events": evs, "tasks": tks}, recent


def _week_objects(w):
    events, tasks = [], []
    for b in w["bars"]:
        (events if b["type"] == "event" else tasks).append(b["obj"] if b["type"] == "event" else b["obj"].task)
    for c in w["days"]:
        for b in c["blocks"]:
            (events if b.type == "event" else tasks).append(b.obj if b.type == "event" else b.obj.task)
    return events, tasks


def _items_objects(items):
    events = [x["obj"] for x in items if x["type"] == "event"]
    tasks = [x["obj"].task for x in items if x["type"] == "task"]
    return events, tasks


# ---------------------------------------------------------------------------
# Tableau de bord, calendrier
# ---------------------------------------------------------------------------

@bp.route("/agenda")
@bp.route("/")
@login_required
def dashboard():
    ref = parse_date(request.args.get("week"))
    d = A.dashboard(current_user, ref=ref)
    ev1, tk1 = _week_objects(d["week"])
    ev2, tk2 = _items_objects(d["agenda_today"])
    tasks = tk1 + tk2 + [t for grp in d["groups"].values() for t in grp]
    events = ev1 + ev2 + d["exams"] + d["changes"] + d["tomorrow_evs"] + [d["current"], d["next"]]
    data, _ = _payloads(events, tasks)
    return render_template("agenda/dashboard.html", data=data, mail=A.mail_status(current_user),
                           qcm=_qcm_resume(), **d)


def _qcm_resume():
    """Révisions du jour (module QCM) pour le tableau de bord ; None si le module est indisponible."""
    try:
        from ..qcm.services import resume_du_jour
        return resume_du_jour(current_user)
    except Exception:  # le tableau de bord ne doit jamais tomber à cause d'un module
        from flask import current_app
        current_app.logger.exception("Résumé des QCM pour le tableau de bord")
        return None


@bp.route("/agenda/calendar")
@login_required
def calendar():
    view = request.args.get("view") or session.get("agenda_view") or "week"
    if view not in ("week", "month", "list"):
        view = "week"
    session["agenda_view"] = view
    today = date.today()
    ref = parse_date(request.args.get("date")) or today
    s = A.settings_of(current_user)
    if view == "month":
        cal = A.month_view(current_user, ref, s)
        items = [x for wk in cal["weeks"] for day in wk for x in day["items"]]
        events, tasks = _items_objects(items)
    elif view == "list":
        cal = A.list_view(current_user, ref, s)
        items = [x for day in cal["days"] for x in day["items"]]
        events, tasks = _items_objects(items)
    else:
        cal = A.week_view(current_user, ref, s)
        events, tasks = _week_objects(cal)
    data, recent = _payloads(events, tasks)
    return render_template("agenda/calendar.html", view=view, cal=cal, ref=ref, data=data, recent=recent, s=s)


@bp.route("/agenda/api/day")
@login_required
def api_day():
    d = parse_date(request.args.get("date"))
    if d is None:
        return jsonify(ok=True, events=[])
    return jsonify(ok=True, events=A.day_events_payload(current_user, d))


@bp.route("/agenda/export.ics")
@login_required
def export():
    what = request.args.get("what", "all")
    text = A.export_ics(current_user, include_events=what in ("all", "events"), include_tasks=what in ("all", "tasks"))
    return Response(text, mimetype="text/calendar; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename=agenda-{date.today():%Y%m%d}.ics"})


# ---------------------------------------------------------------------------
# Tâches
# ---------------------------------------------------------------------------

def _own_task(tid: int) -> AgendaTask:
    t = db.session.get(AgendaTask, tid)
    if t is None or t.user_id != current_user.id:
        abort(404)
    return t


def _own_event(eid: int) -> AgendaEvent:
    e = db.session.get(AgendaEvent, eid)
    if e is None or e.user_id != current_user.id:
        abort(404)
    return e


@bp.route("/agenda/tasks")
@login_required
def tasks():
    show = request.args.get("show", "open")
    q = plier((request.args.get("q") or "").strip())
    cat = request.args.get("cat") or ""
    today = date.today()

    def keep(t):
        if q and q not in plier(t.title + " " + (t.notes or "") + " " + (t.category or "")):
            return False
        return not cat or (t.category or "") == cat

    if show == "done":
        done = [t for t in AgendaTask.query.filter(AgendaTask.user_id == current_user.id, AgendaTask.done_at.isnot(None))
                .order_by(AgendaTask.done_at.desc()).limit(300) if keep(t)]
        groups = [("done", "Terminées", done)]
    else:
        g_ = A.open_task_groups(current_user, today)
        groups = [(k, lab, [t for t in g_[k] if keep(t)]) for k, lab in A.TASK_GROUP_LABELS]
    all_tasks = [t for _, _, lst in groups for t in lst]
    data, _ = _payloads([t.event for t in all_tasks if t.event], all_tasks)
    counts = {
        "open": AgendaTask.query.filter(AgendaTask.user_id == current_user.id, AgendaTask.done_at.is_(None)).count(),
        "done": AgendaTask.query.filter(AgendaTask.user_id == current_user.id, AgendaTask.done_at.isnot(None)).count(),
    }
    return render_template("agenda/tasks.html", groups=groups, show=show, q=request.args.get("q", ""), cat=cat,
                           cats=A.categories(current_user), data=data, counts=counts,
                           focus=to_int(request.args.get("focus")))


def _apply_task_form(t: AgendaTask, f) -> str | None:
    title = (f.get("title") or "").strip()
    if not title:
        return "Le titre de la tâche est obligatoire."
    t.title = title[:300]
    t.due_date = parse_date(f.get("date"))
    t.start_time = parse_time(f.get("start")) if t.due_date else None
    t.end_time = parse_time(f.get("end")) if t.start_time else None
    if t.end_time and t.start_time and t.end_time <= t.start_time:
        t.end_time = None
    if "notes" in f:
        t.notes = f.get("notes", "")
    if "priority" in f:
        t.priority = f.get("priority") if f.get("priority") in PRIORITY_LABELS else "none"
    if "category" in f:
        t.category = (f.get("category") or "").strip()[:60]
    if "color" in f:
        t.color = f.get("color") if f.get("color") in TASK_COLORS else ""
    if "recurrence" in f:
        t.recurrence = f.get("recurrence") if f.get("recurrence") in RECURRENCES and t.due_date else ""
        t.recurrence_until = parse_date(f.get("until")) if t.recurrence else None
    if "event_id" in f:
        eid = to_int(f.get("event_id"))
        ev = db.session.get(AgendaEvent, eid) if eid else None
        t.event_id = ev.id if ev is not None and ev.user_id == current_user.id else None
    t.updated_at = now()
    return None


def _apply_task_reminders(t: AgendaTask, f, is_new: bool) -> None:
    s = A.settings_of(current_user)
    channel = f.get("reminder_channel") or s.reminder_channel or "email"
    if f.get("reminders_present"):
        offsets = [o for o in (to_int(x) for x in f.getlist("reminders")) if o is not None]
        A.set_task_reminders(t, offsets, channel)
    elif is_new and s.default_reminder is not None and s.default_reminder >= 0 and t.due_date:
        A.set_task_reminders(t, [s.default_reminder], channel)
    for rid in f.getlist("remove_reminder"):
        r = db.session.get(AgendaReminder, to_int(rid) or 0)
        if r is not None and r.task_id == t.id:
            t.reminders.remove(r)
            db.session.delete(r)
    when = parse_datetime_local(f.get("reminder_at"))
    if when:
        A.add_fixed_reminder(current_user.id, when, channel, task=t)


@bp.route("/agenda/tasks", methods=["POST"])
@login_required
def task_create():
    t = AgendaTask(user_id=current_user.id, created_at=now())
    err = _apply_task_form(t, request.form)
    if err:
        if _fetch():
            return jsonify(ok=False, error=err), 400
        flash(err, "error")
        return redirect(back_url(url_for("agenda.tasks")))
    db.session.add(t)
    db.session.flush()
    _apply_task_reminders(t, request.form, is_new=True)
    db.session.commit()
    msg = "Tâche ajoutée" + (f" pour {A.due_label(t.due_date)}" if t.due_date else "") + "."
    if t.reminders and t.reminders[0].channel == "email" and not A._mail().valid_email(current_user.contact_email):
        msg += " Ajoute ton adresse e-mail dans les paramètres de l’agenda pour recevoir les rappels."
    if _fetch():
        return jsonify(ok=True, id=t.id, message=msg)
    flash(msg, "success")
    return redirect(back_url(url_for("agenda.tasks")))


@bp.route("/agenda/tasks/<int:tid>", methods=["POST"])
@login_required
def task_update(tid):
    t = _own_task(tid)
    err = _apply_task_form(t, request.form)
    if err:
        flash(err, "error")
        return redirect(back_url(url_for("agenda.tasks")))
    db.session.flush()
    A.resync_task(t)
    _apply_task_reminders(t, request.form, is_new=False)
    db.session.commit()
    if _fetch():
        return jsonify(ok=True)
    flash("Tâche enregistrée.", "success")
    return redirect(back_url(url_for("agenda.tasks")))


@bp.route("/agenda/tasks/<int:tid>/toggle", methods=["POST"])
@login_required
def task_toggle(tid):
    t = _own_task(tid)
    nxt = A.complete_task(t)
    db.session.commit()
    if nxt:
        msg = f"Bravo ! Prochaine fois : {A.day_long(nxt)}."
    else:
        msg = "Tâche terminée. 🎉" if t.is_done else "Tâche rouverte."
    if _fetch():
        return jsonify(ok=True, done=t.is_done, next=nxt.isoformat() if nxt else None, message=msg)
    flash(msg, "success")
    return redirect(back_url(url_for("agenda.tasks")))


@bp.route("/agenda/tasks/<int:tid>/snooze", methods=["POST"])
@login_required
def task_snooze(tid):
    t = _own_task(tid)
    today = date.today()
    to = request.form.get("to", "tomorrow")
    if to == "today":
        t.due_date = today
    elif to == "tomorrow":
        t.due_date = today + timedelta(days=1)
    elif to == "monday":
        t.due_date = today + timedelta(days=7 - today.weekday())
    elif to == "week":
        t.due_date = max(t.due_date or today, today) + timedelta(days=7)
    elif to == "nodate":
        t.due_date, t.start_time, t.end_time, t.recurrence = None, None, None, ""
    t.updated_at = now()
    A.resync_task(t, reset=True)
    db.session.commit()
    msg = f"Tâche reportée à {A.due_label(t.due_date)}." if t.due_date else "Tâche sans date."
    if _fetch():
        return jsonify(ok=True, message=msg)
    flash(msg, "success")
    return redirect(back_url(url_for("agenda.tasks")))


@bp.route("/agenda/tasks/<int:tid>/delete", methods=["POST"])
@login_required
def task_delete(tid):
    t = _own_task(tid)
    db.session.delete(t)
    db.session.commit()
    if _fetch():
        return jsonify(ok=True)
    flash("Tâche supprimée.", "success")
    return redirect(back_url(url_for("agenda.tasks")))


@bp.route("/agenda/tasks/clear-done", methods=["POST"])
@login_required
def tasks_clear_done():
    n = 0
    for t in AgendaTask.query.filter(AgendaTask.user_id == current_user.id, AgendaTask.done_at.isnot(None)):
        db.session.delete(t)
        n += 1
    db.session.commit()
    flash(f"{n} tâche(s) terminée(s) supprimée(s).", "success")
    return redirect(url_for("agenda.tasks", show="done"))


# ---------------------------------------------------------------------------
# Séances : note personnelle, rappels
# ---------------------------------------------------------------------------

@bp.route("/agenda/events/<int:eid>/note", methods=["POST"])
@login_required
def event_note(eid):
    ev = _own_event(eid)
    ev.note = (request.form.get("note") or "").strip()
    db.session.commit()
    if _fetch():
        return jsonify(ok=True)
    flash("Note enregistrée.", "success")
    return redirect(back_url(url_for("agenda.calendar", date=ev.start.date().isoformat())))


@bp.route("/agenda/events/<int:eid>/reminders", methods=["POST"])
@login_required
def event_reminder(eid):
    ev = _own_event(eid)
    channel = request.form.get("channel") or A.settings_of(current_user).reminder_channel or "email"
    when = parse_datetime_local(request.form.get("at"))
    offset = to_int(request.form.get("offset"))
    if when:
        r = A.add_fixed_reminder(current_user.id, when, channel, event=ev)
    elif offset is not None and offset >= 0:
        if any(x.offset_minutes == offset for x in ev.reminders):
            flash("Ce rappel existe déjà.", "info")
            return redirect(back_url(url_for("agenda.dashboard")))
        r = A.add_event_reminder(ev, offset, channel)
    else:
        flash("Choisis quand te rappeler cette séance.", "error")
        return redirect(back_url(url_for("agenda.dashboard")))
    db.session.commit()
    msg = f"Rappel ajouté : {r.label.lower()}."
    if r.status == "skipped":
        msg = "Ce moment est déjà passé : le rappel ne sera pas envoyé."
    elif r.channel == "email" and not A._mail().valid_email(current_user.contact_email):
        msg += " Renseigne ton adresse e-mail dans les paramètres de l’agenda pour le recevoir par e-mail."
    flash(msg, "success" if r.status == "pending" else "info")
    return redirect(back_url(url_for("agenda.dashboard")))


@bp.route("/agenda/events/<int:eid>/link", methods=["POST"])
@login_required
def subject_link(eid):
    """Relie la matière d'une séance à une collection du wiki et/ou à une catégorie de QCM."""
    ev = _own_event(eid)
    f = request.form
    A.set_subject_link(current_user, ev.title,
                       wiki_path=f.get("wiki_path") if "wiki_path" in f else None,
                       qcm_category=f.get("qcm_category") if "qcm_category" in f else None)
    db.session.commit()
    if _fetch():
        return jsonify(ok=True)
    flash(f"Liens de « {ev.title} » enregistrés (pour toutes ses séances).", "success")
    return redirect(back_url(url_for("agenda.calendar", date=ev.start.date().isoformat())))


@bp.route("/agenda/events/<int:eid>/exam", methods=["POST"])
@login_required
def event_exam(eid):
    """Fixe la date d'examen des QCM liés à la matière au jour de ce contrôle."""
    ev = _own_event(eid)
    n = A.set_exam_objective(current_user, ev)
    if n:
        flash(f"Date d’examen fixée au {ev.start:%d/%m/%Y} pour {n} set(s) de QCM : les révisions s’organisent "
              "pour être prêt à temps.", "success")
    else:
        flash("Relie d’abord cette matière à une catégorie de QCM.", "info")
    return redirect(back_url(url_for("agenda.dashboard")))


@bp.route("/agenda/reminders/<int:rid>/delete", methods=["POST"])
@login_required
def reminder_delete(rid):
    r = db.session.get(AgendaReminder, rid)
    if r is None or r.user_id != current_user.id:
        abort(404)
    db.session.delete(r)
    db.session.commit()
    if _fetch():
        return jsonify(ok=True)
    flash("Rappel supprimé.", "success")
    return redirect(back_url(url_for("agenda.dashboard")))


# ---------------------------------------------------------------------------
# Imports de l'emploi du temps
# ---------------------------------------------------------------------------

def _own_import(iid: int) -> AgendaImport:
    imp = db.session.get(AgendaImport, iid)
    if imp is None or imp.user_id != current_user.id:
        abort(404)
    return imp


@bp.route("/agenda/imports")
@login_required
def imports():
    rows = AgendaImport.query.filter_by(user_id=current_user.id).order_by(AgendaImport.id.desc()).limit(100).all()
    latest = A.latest_import(current_user)
    stats = {
        "events": AgendaEvent.query.filter_by(user_id=current_user.id, status="active").count(),
        "first": db.session.query(db.func.min(AgendaEvent.start)).filter(AgendaEvent.user_id == current_user.id,
                                                                         AgendaEvent.status == "active").scalar(),
        "last": db.session.query(db.func.max(AgendaEvent.start)).filter(AgendaEvent.user_id == current_user.id,
                                                                        AgendaEvent.status == "active").scalar(),
    }
    return render_template("agenda/imports.html", rows=rows, latest=latest, modes=A.IMPORT_MODES, stats=stats)


@bp.route("/agenda/import", methods=["POST"])
@login_required
def import_upload():
    f = request.files.get("file")
    if f is None or not f.filename:
        flash("Choisis le fichier .ics de ton emploi du temps.", "error")
        return redirect(back_url(url_for("agenda.imports")))
    raw = f.read()
    try:
        imp = A.import_calendar(current_user, raw, f.filename, request.form.get("mode", "weeks"))
    except ics.ICSError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return redirect(back_url(url_for("agenda.imports")))
    if imp.changed == imp.added and not imp.modified and not imp.removed and not imp.restored and imp.added:
        msg = f"Emploi du temps importé : {imp.added} séance(s) ajoutée(s)."
    elif imp.changed:
        msg = (f"Emploi du temps mis à jour : {imp.added} ajoutée(s), {imp.modified} modifiée(s), "
               f"{imp.removed} retirée(s){f', {imp.restored} rétablie(s)' if imp.restored else ''}.")
    else:
        msg = "Emploi du temps importé : aucun changement par rapport au précédent."
    flash(msg, "success")
    return redirect(url_for("agenda.import_detail", iid=imp.id))


@bp.route("/agenda/imports/<int:iid>")
@login_required
def import_detail(iid):
    imp = _own_import(iid)
    latest = A.latest_import(current_user)
    details = imp.details
    for d in details:
        snap = d.get("after") or d.get("before") or {}
        d["start_dt"] = datetime.fromisoformat(snap["start"]) if snap.get("start") else None
        d["end_dt"] = datetime.fromisoformat(snap["end"]) if snap.get("end") else None
        d["title"] = snap.get("title", "")
        d["kind"] = snap.get("kind", "")
        d["location"] = snap.get("location", "")
        if d["type"] in ("modified", "restored"):
            d["lines"] = A.change_lines({k: [d["before"][k], d["after"][k]] for k in d.get("fields", [])})
    order = {"modified": 0, "removed": 1, "restored": 2, "added": 3}
    details.sort(key=lambda d: (order.get(d["type"], 9), d["start_dt"] or datetime.min))
    sections = [
        ("modified", "Modifiées", "warning", [d for d in details if d["type"] == "modified"]),
        ("removed", "Retirées du planning", "danger", [d for d in details if d["type"] == "removed"]),
        ("restored", "Rétablies", "accent", [d for d in details if d["type"] == "restored"]),
        ("added", "Ajoutées dans des semaines déjà importées", "accent",
         [d for d in details if d["type"] == "added" and d.get("notable")]),
        ("new", "Nouvelles séances", "", [d for d in details if d["type"] == "added" and not d.get("notable")]),
    ]
    return render_template("agenda/import_detail.html", imp=imp, latest=latest, sections=sections,
                           can_undo=latest is not None and latest.id == imp.id and imp.undone_at is None,
                           file_ok=A.stored_path(imp) is not None, modes=A.IMPORT_MODES)


@bp.route("/agenda/imports/<int:iid>/undo", methods=["POST"])
@login_required
def import_undo(iid):
    imp = _own_import(iid)
    try:
        A.undo_import(imp)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("agenda.import_detail", iid=iid))
    flash("Import annulé : l’emploi du temps est revenu à l’état précédent.", "success")
    return redirect(url_for("agenda.imports"))


@bp.route("/agenda/imports/<int:iid>/file")
@login_required
def import_file(iid):
    imp = _own_import(iid)
    path = A.stored_path(imp)
    if path is None:
        abort(404)
    return send_file(path, mimetype="text/calendar", as_attachment=True, download_name=imp.filename or "planning.ics")


# ---------------------------------------------------------------------------
# Paramètres
# ---------------------------------------------------------------------------

@bp.route("/agenda/settings", methods=["GET", "POST"])
@login_required
def settings():
    s = A.settings_of(current_user)
    if request.method == "POST":
        f = request.form
        mailer = A._mail()
        contact = (f.get("contact_email") or "").strip()
        if contact and not mailer.valid_email(contact):
            flash("Adresse e-mail invalide : elle n’a pas été enregistrée.", "error")
        else:
            current_user.contact_email = contact or None

        def hour(name, default, lo=0, hi=23):
            v = to_int(f.get(name))
            return default if v is None else min(max(v, lo), hi)

        s.day_start_hour = hour("day_start_hour", 8, 0, 22)
        s.day_end_hour = max(hour("day_end_hour", 19, 1, 24), s.day_start_hour + 1)
        s.show_weekends = bool(f.get("show_weekends"))
        s.show_removed = bool(f.get("show_removed"))
        s.allday_hour = hour("allday_hour", 8)
        dr = to_int(f.get("default_reminder"))
        s.default_reminder = dr if dr is not None and dr >= 0 else -1
        s.reminder_channel = f.get("reminder_channel") if f.get("reminder_channel") in ("email", "app") else "email"
        mode = f.get("daily_mode") if f.get("daily_mode") in ("off", "morning", "evening") else "off"
        new_hour = hour("daily_hour", 7)
        if mode != s.daily_mode or new_hour != s.daily_hour:
            # pas d'envoi immédiat à l'activation si l'heure choisie est déjà passée : premier envoi demain
            s.last_daily_date = date.today() if datetime.now().hour >= new_hour else None
        s.daily_mode, s.daily_hour = mode, new_hour
        s.import_email = bool(f.get("import_email"))
        s.weekly_import_nudge = bool(f.get("weekly_import_nudge"))
        er = to_int(f.get("exam_reminder")) or 0
        s.exam_reminder = max(er, 0)
        db.session.flush()
        A.ensure_exam_reminders(current_user)
        db.session.commit()
        flash("Préférences de l’agenda enregistrées.", "success")
        return redirect(url_for("agenda.settings"))
    pending = (AgendaReminder.query.filter(AgendaReminder.user_id == current_user.id, AgendaReminder.status == "pending",
                                           AgendaReminder.remind_at > now())
               .order_by(AgendaReminder.remind_at).limit(30).all())
    history = (AgendaReminder.query.filter(AgendaReminder.user_id == current_user.id,
                                           AgendaReminder.status.in_(("sent", "missed")))
               .order_by(AgendaReminder.remind_at.desc()).limit(15).all())
    return render_template("agenda/settings.html", s=s, mail=A.mail_status(current_user), pending=pending,
                           history=history)


@bp.route("/agenda/summary")
@login_required
def summary_preview():
    """Aperçu du résumé quotidien tel qu'il partira par e-mail."""
    from .. import digest
    at = now()
    s = A.settings_of(current_user)
    which = request.args.get("day")
    if which in ("today", "tomorrow"):
        day = at.date() + timedelta(days=1 if which == "tomorrow" else 0)
    else:
        day = at.date() + timedelta(days=1) if s.daily_mode == "evening" else at.date()
    built = digest.construire(current_user, day, at)
    return render_template("agenda/summary.html", built=built, day=day, s=s, mail=A.mail_status(current_user),
                           sections=digest.sections_enregistrees())


@bp.route("/agenda/settings/test", methods=["POST"])
@login_required
def settings_test():
    mailer = A._mail()
    what = request.form.get("what", "test")
    if not mailer.valid_email(current_user.contact_email):
        flash("Renseigne d’abord ton adresse e-mail.", "error")
        return redirect(url_for("agenda.settings"))
    if what == "program":
        at = now()
        s = A.settings_of(current_user)
        day = at.date() + timedelta(days=1) if s.daily_mode == "evening" or at.hour >= 18 else at.date()
        built = A.build_day_email(current_user, day, at)
        if not built:
            flash(f"Rien à faire le {A.day_long(day)} : aucun résumé à envoyer.", "info")
            return redirect(url_for("agenda.settings"))
        m = mailer.queue_email(current_user.contact_email, built[0], built[1], kind="agenda", to_user=current_user)
        db.session.commit()
        mailer.send_pending(ids=[m.id])
    else:
        m = mailer.send_test(current_user.contact_email, current_user)
    db.session.refresh(m)
    if m.status == "sent":
        flash(f"E-mail envoyé à {m.to_address}.", "success")
    elif m.status == "outbox":
        flash("E-mail enregistré dans la boîte d’envoi locale (data/outbox) : l’envoi réel n’est pas configuré.", "info")
    else:
        flash(f"Échec de l’envoi : {m.error or 'erreur inconnue'}", "error")
    return redirect(url_for("agenda.settings"))

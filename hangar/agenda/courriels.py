"""Agenda : e-mails et notifications des rappels, sections du résumé quotidien, e-mail d'import."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from flask import current_app
from markupsafe import escape

from .. import agenda as A  # le paquet : voir hangar/agenda/__init__.py
from .. import digest
from ..extensions import db
from ..models import AgendaEvent, AgendaImport, AgendaReminder, AgendaSettings, AgendaTask, Notification, User
from ..texte import cle_tri


# ---------------------------------------------------------------------------
# E-mails
# ---------------------------------------------------------------------------

def _mail():
    from .. import mailer
    return mailer


def mail_status(user: User, at: datetime | None = None) -> dict:
    """État de l'envoi des e-mails pour l'agenda (affiché dans l'interface), avec les e-mails de ce compte
    en échec (encore utiles à renvoyer) ou en attente d'un nouvel essai."""
    from ..models import EmailMessage, MailConfig
    m = _mail()
    cfg = MailConfig.get()
    smtp = cfg.mode == "smtp" and bool(cfg.smtp_host)
    at = at or A.now()
    recent = (EmailMessage.query.filter(EmailMessage.to_user_id == user.id,
                                        EmailMessage.status.in_(("failed", "queued")),
                                        EmailMessage.created_at >= at - timedelta(days=30))
              .order_by(EmailMessage.id.desc()).all())
    failed = [x for x in recent if x.status == "failed" and m.deadline(x) > at]
    retrying = [x for x in recent if x.status == "queued" and x.attempts]
    return {"address": user.contact_email if m.valid_email(user.contact_email) else None, "smtp": smtp,
            "host": cfg.smtp_host, "ok": bool(m.valid_email(user.contact_email)) and smtp,
            "failed": len(failed), "retrying": len(retrying),
            "error": m.plain_error((failed or retrying)[0].error) if (failed or retrying) else ""}


def reminder_status_label(r: AgendaReminder) -> str:
    """Libellé de l'état d'un rappel : « Envoyé » seulement si son e-mail est vraiment parti."""
    if r.status == "sent" and r.email is not None:
        st = r.email.status
        if st == "failed":
            return "E-mail en échec"
        if st == "queued":
            return "E-mail en attente d’un nouvel essai" if r.email.attempts else "E-mail en cours d’envoi"
        if st == "outbox":
            return "Envoyé (boîte d’envoi locale)"
    return A.STATUS_LABELS.get(r.status, r.status)


def _row(label: str, value: str) -> str:
    if not value:
        return ""
    return (f'<tr><td style="padding:3px 12px 3px 0;color:#6b7280;white-space:nowrap;vertical-align:top">{escape(label)}</td>'
            f'<td style="padding:3px 0">{value}</td></tr>')


def _md(text: str) -> str:
    from ..utils import render_markdown
    return str(render_markdown(text, link_refs=False)) if text else ""


def send_reminder(r: AgendaReminder, user: User, at: datetime):
    """Notification dans l'application et, selon le moyen choisi, e-mail mis en file (retourné, sinon None)
    ou notification push sur les appareils abonnés."""
    m = _mail()
    cfg = m._cfg()
    if r.task is not None:
        t = r.task
        title, when = t.title, A.task_when(t, A._reminder_occurrence(r))
        rows = _row("Quand", escape(when))
        if t.event is not None:
            rows += _row("Séance liée", escape(f"{t.event.title} ({A.event_when(t.event)})"))
        if t.category:
            rows += _row("Catégorie", escape(t.category))
        body = f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin:4px 0 10px">{rows}</table>'
        if t.notes:
            body += f'<div style="border-left:3px solid #e4e7ec;padding:2px 12px;color:#374151">{_md(t.notes)}</div>'
        path = f"/agenda/tasks?focus={t.id}"
    else:
        ev = r.event
        title, when = ev.title + (f" ({A.kind_label(ev.kind)})" if ev.kind else ""), A.event_when(ev)
        rows = (_row("Quand", escape(when)) + _row("Salle", escape(ev.location)) + _row("Intervenants", escape(ev.teachers))
                + _row("Groupes", escape(ev.groups)))
        body = f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin:4px 0 10px">{rows}</table>'
        if ev.description:
            body += f'<p style="color:#374151;white-space:pre-line">{escape(ev.description)}</p>'
        if ev.note:
            body += (f'<div style="border-left:3px solid #3f76ff;padding:2px 12px;color:#374151"><b>Ma note</b>'
                     f'{_md(ev.note)}</div>')
        path = f"/agenda/calendar?date={ev.start.date().isoformat()}"
    short = when.split(" · ")[-1] if " · " in when else when
    db.session.add(Notification(user_id=user.id, kind="reminder", message=f"⏰ Rappel : {title} — {when}"[:500],
                                url=path, created_at=at))
    if r.channel == "push":
        detail = when
        if r.event is not None and r.event.location:
            detail += f" · {r.event.location}"
        send_push(user, f"⏰ {title}", detail, path, tag=f"agenda-rappel-{r.id}",
                  ttl=_push_ttl(A.reminder_deadline(r), at))
        return None
    if r.channel == "email" and m.accepte_emails(user):  # « Aucun e-mail » : la notification dans l'appli suffit
        html = m.layout(f"⏰ {title}", f"<p>Petit rappel ({escape(r.label.lower())}) :</p>{body}",
                        button=("Ouvrir mon agenda", m.absolute(path, cfg)), cfg=cfg)
        return m.queue_email(user.contact_email, f"⏰ Rappel : {title} — {short}"[:300], html, kind="reminder",
                             to_user=user, expires_at=A.reminder_deadline(r))
    return None


PUSH_TTL_MAX = 24 * 3600


def _push_ttl(deadline: datetime | None, at: datetime) -> int:
    """Durée de validité d'une notification push : jusqu'à la fin de la séance ou de la tâche (au-delà, un
    téléphone resté hors ligne la recevrait pour rien), entre 1 min et 24 h."""
    if deadline is None:
        return 3600
    return int(max(60, min((deadline - at).total_seconds(), PUSH_TTL_MAX)))


def push_devices(user: User) -> list:
    """Appareils de `user` abonnés aux rappels de l'agenda (vide si le module QCM est indisponible)."""
    try:
        from ..qcm.models import AbonnementPush
    except Exception:  # noqa: BLE001
        return []
    return AbonnementPush.query.filter(AbonnementPush.user_id == user.id, AbonnementPush.agenda.is_(True)).all()


def send_push(user: User, title: str, text: str, path: str, tag: str, ttl: int = 3600) -> int:
    """Envoie une notification du système à chaque appareil abonné (service des notifications web des QCM,
    qui n'accepte que les adresses des services connus). Un appareil inconnu du service est oublié ; une panne
    passagère est notée au journal sans bloquer les autres. Retourne le nombre d'appareils atteints."""
    from ..qcm import push
    sent = 0
    for a in push_devices(user):
        try:
            push.envoyer(a, title[:120], text[:300], path, ttl=ttl, tag=tag)
            sent += 1
        except (push.AbonnementExpire, push.AdresseNonAutorisee):
            db.session.delete(a)
        except Exception as exc:  # noqa: BLE001 - réseau, service de notifications : la notification interne reste
            current_app.logger.warning("Notification push d’un rappel impossible : %s", exc)
    return sent


def _event_line(ev: AgendaEvent) -> str:
    color = A.event_color(ev)
    meta = " · ".join(x for x in (A.kind_label(ev.kind), ev.location, ev.teachers) if x)
    when = "Journée" if ev.multi_day else f"{A.hm(ev.start)}–{A.hm(ev.end)}"
    flag = ' <span style="color:#d97706">(prévisionnel)</span>' if ev.tentative else ""
    if ev.cancelled or ev.status != "active":
        flag = ' <span style="color:#dc2626">(retiré / annulé)</span>'
    return (f'<tr><td style="padding:6px 10px 6px 0;white-space:nowrap;font-family:Menlo,Consolas,monospace;'
            f'font-size:12.5px;vertical-align:top">{when}</td><td style="padding:6px 0;border-left:3px solid {color};'
            f'padding-left:10px"><b>{escape(ev.title)}</b>{flag}<div style="color:#6b7280;font-size:12.5px">'
            f'{escape(meta)}</div></td></tr>')


def _task_line(t: AgendaTask, today: date, d: date | None = None) -> str:
    """Ligne d'une tâche dans le résumé ; `d` : date de l'occurrence listée (par défaut l'échéance)."""
    d = d or t.due_date
    when = A.hm(t.start_time) if t.start_time else ""
    late = (f' <span style="color:#dc2626">(prévue {escape(A.due_label(d, today))})</span>'
            if d and d < today else "")
    return (f'<li style="margin:4px 0">{"<b>" + when + "</b> " if when else ""}{escape(t.title)}{late}'
            f'{" <span style=color:#6b7280>· " + escape(t.category) + "</span>" if t.category else ""}</li>')


# ---------------------------------------------------------------------------
# Résumé quotidien : sections de l'agenda (voir hangar/digest.py)
# ---------------------------------------------------------------------------

@digest.section("agenda.cours", ordre=10)
def _digest_cours(ctx):
    a = datetime.combine(ctx.jour, time(0))
    evs = [e for e in A.events_between(ctx.user, a, a + timedelta(days=1), include_removed=False) if not e.cancelled]
    if not evs:
        return None
    total = sum(e.minutes for e in evs if not e.multi_day)
    timed = [e for e in evs if not e.multi_day]
    html = ""
    if timed:
        html += (f'<div style="color:#6b7280;font-size:12.5px;margin-bottom:4px">{A.duration_label(total)} de cours, '
                 f'de {A.hm(timed[0].start)} à {A.hm(max(e.end for e in timed))}</div>')
    html += ('<table role="presentation" cellpadding="0" cellspacing="0" width="100%">'
             + "".join(_event_line(e) for e in evs) + "</table>")
    return digest.Section("agenda.cours", "Emploi du temps", html, compte=len(evs),
                          libelle=digest.pluriel(len(evs), "séance"), couleur="#3f76ff")


@digest.section("agenda.taches", ordre=20)
def _digest_taches(ctx):
    occs = A.task_occurrences(ctx.user, ctx.jour, ctx.jour, include_done=False)
    overdue = (AgendaTask.query.filter(AgendaTask.user_id == ctx.user.id, AgendaTask.done_at.is_(None),
                                       AgendaTask.due_date < ctx.aujourdhui).order_by(AgendaTask.due_date).all())
    listed = {o.task.id for o in occs}  # une tâche répétée déjà listée pour le jour n'est pas relistée en retard
    overdue = [t for t in overdue if t.id not in listed]
    # Résumé du soir (jour résumé = demain) : ce qui reste à faire aujourd'hui n'est ni « demain » ni « en retard »
    rest = []
    if ctx.jour > ctx.aujourdhui:
        rest = [o for o in A.task_occurrences(ctx.user, ctx.aujourdhui, ctx.aujourdhui, include_done=False)
                if o.task.id not in listed]
        rest.sort(key=lambda o: (o.task.start_time or time(23, 59), o.task.title.lower()))
    if not (occs or overdue or rest):
        return None
    html = ""
    if occs:
        occs.sort(key=lambda o: (o.task.start_time or time(23, 59), cle_tri(o.task.title)))
        html += ('<ul style="padding-left:18px;margin:0">'
                 + "".join(_task_line(o.task, ctx.aujourdhui, o.date) for o in occs) + "</ul>")
    if rest:
        html += ('<div style="font-size:12.5px;font-weight:600;color:#d97706;margin:10px 0 2px">'
                 f'Reste à faire aujourd’hui ({len(rest)})</div><ul style="padding-left:18px;margin:0">'
                 + "".join(_task_line(o.task, ctx.aujourdhui, o.date) for o in rest[:12]) + "</ul>")
    if overdue:
        html += ('<div style="font-size:12.5px;font-weight:600;color:#dc2626;margin:10px 0 2px">'
                 f'En retard ({len(overdue)})</div><ul style="padding-left:18px;margin:0">'
                 + "".join(_task_line(t, ctx.aujourdhui) for t in overdue[:12]) + "</ul>")
    n = len(occs)
    if n:
        libelle, extra = digest.pluriel(n, "tâche"), [(rest, "aujourd’hui"), (overdue, "en retard")]
    elif rest:
        libelle, extra = digest.pluriel(len(rest), "tâche") + " aujourd’hui", [(overdue, "en retard")]
    else:
        libelle, extra = digest.pluriel(len(overdue), "en retard", "en retard"), []
    extra = [f"+{len(x)} {lab}" for x, lab in extra if x]
    if extra:
        libelle += f" ({', '.join(extra)})"
    return digest.Section("agenda.taches", "Tâches", html, compte=n or len(rest) or len(overdue), libelle=libelle,
                          couleur="#dc2626" if overdue else "#8b5cf6")


@digest.section("agenda.controles", ordre=40)
def _digest_controles(ctx):
    a = datetime.combine(ctx.jour, time(0))
    exams = [e for e in A.events_between(ctx.user, a + timedelta(days=1), a + timedelta(days=8), include_removed=False)
             if A.is_exam(e) and not e.cancelled]
    if not exams:
        return None
    try:  # statut de préparation (prêt, à commencer, en retard…), calculé sans adresse web
        from .. import controles
        statuts = controles.indicateurs(ctx.user, exams, ctx.at)
    except Exception:  # noqa: BLE001 - la section reste utile sans les statuts
        controles, statuts = None, {}
    items = []
    for e in exams:
        st = statuts.get(e.id)
        etat = ""
        if controles is not None and st and st not in ("passe", "a_relier"):
            couleur = {"pret": "#16a34a", "retard": "#dc2626", "a_commencer": "#d97706"}.get(st, "#3f76ff")
            etat = (f' · <a href="{escape(ctx.lien(f"/agenda/events/{e.id}/preparer"))}" style="color:{couleur};'
                    f'font-weight:600">{escape(controles.libelle(st).lower())}</a>')
        items.append(f"{escape(A.day_short(e.start.date()))} {A.hm(e.start)} — <b>{escape(e.title)}</b>"
                     f"{' · ' + escape(e.location) if e.location else ''}{etat}")
    return digest.Section("agenda.controles", "Contrôles dans les 7 jours", digest.liste_html(items),
                          compte=len(exams), couleur="#dc2626", a_faire=False, tuile=False)


@digest.section("agenda.changements", ordre=45)
def _digest_changements(ctx):
    recent = A.recent_import_ids(ctx.user, ctx.at, days=2)
    if not recent:
        return None
    a = datetime.combine(ctx.aujourdhui, time(0))
    evs = (AgendaEvent.query.filter(AgendaEvent.user_id == ctx.user.id, AgendaEvent.changed_import_id.in_(recent),
                                    AgendaEvent.change_kind.in_(("added", "modified", "removed", "restored")),
                                    AgendaEvent.start >= a, AgendaEvent.start < a + timedelta(days=8))
           .order_by(AgendaEvent.start).all())
    if not evs:
        return None
    labels = {"added": "ajouté", "modified": "modifié", "removed": "retiré", "restored": "rétabli"}
    items = []
    for e in evs[:12]:
        extra = ""
        if e.change_kind in ("modified", "restored") and e.changes:
            extra = f"<br><span style='color:#6b7280'>{escape(' · '.join(A.change_lines(e.changes)))}</span>"
        items.append(f"<b>{labels[e.change_kind]}</b> — {escape(A.day_short(e.start.date()))} {A.hm(e.start)} : "
                     f"{escape(e.title)}{extra}")
    return digest.Section("agenda.changements", "Changements récents du planning", digest.liste_html(items),
                          compte=len(evs), couleur="#d97706", a_faire=False, tuile=False)


def build_day_email(user: User, day: date, at: datetime) -> tuple[str, str] | None:
    """Résumé quotidien (toutes les sections enregistrées). None s'il n'y a rien à faire ce jour-là."""
    built = digest.construire(user, day, at)
    return (built[0], built[1]) if built else None


DAILY_LATEST_HOUR = 18  # après 18 h, plus de résumé « de la journée » : elle est presque finie


def daily_covered(s: AgendaSettings) -> date | None:
    """Dernier jour couvert par un résumé déjà envoyé. Les versions précédentes ne gardaient que le jour de
    l'envoi : en mode soir, le résumé envoyé ce jour-là couvrait le lendemain."""
    if s.last_daily_day is not None:
        return s.last_daily_day
    if s.last_daily_date is None:
        return None
    return s.last_daily_date + timedelta(days=1) if s.daily_mode == "evening" else s.last_daily_date


def daily_target(s: AgendaSettings, at: datetime) -> date | None:
    """Jour à résumer maintenant, ou None.

    - Le matin : le jour même, à partir de l'heure choisie et jusqu'à 18 h (au-delà, la journée est presque
      finie : on n'envoie plus le programme d'une journée passée).
    - La veille au soir : le lendemain, à partir de l'heure choisie. Si Hangar était fermé hier soir, le résumé
      du jour part à la première ouverture, jusqu'à 18 h (rattrapage).
    Un jour déjà couvert par un résumé n'est jamais renvoyé."""
    if s.daily_mode not in ("morning", "evening"):
        return None
    today, hour = at.date(), s.daily_hour or 0
    if s.daily_mode == "morning":
        day = today if hour <= at.hour < max(DAILY_LATEST_HOUR, hour + 1) else None
    elif at.hour >= hour:
        day = today + timedelta(days=1)
    else:
        day = today if at.hour < min(DAILY_LATEST_HOUR, hour) else None
    covered = daily_covered(s)
    if day is None or (covered is not None and covered >= day):
        return None
    return day


def set_daily_schedule(s: AgendaSettings, mode: str, hour: int, at: datetime) -> None:
    """Change le mode ou l'heure du résumé quotidien sans envoi immédiat : le jour que le résumé couvrirait
    tout de suite est noté comme déjà couvert (activé à 10 h pour 7 h : premier résumé demain). Un jour déjà
    couvert le reste (changer l'heure après avoir reçu le résumé du jour ne le renvoie pas)."""
    if mode == s.daily_mode and hour == s.daily_hour:
        return
    covered = daily_covered(s)
    sent_on, s.last_daily_date, s.last_daily_day = s.last_daily_date, None, None
    s.daily_mode, s.daily_hour = mode, hour
    target = daily_target(s, at)
    s.last_daily_date = sent_on
    s.last_daily_day = max((d for d in (covered, target) if d is not None), default=None)


def send_daily_programs(at: datetime | None = None) -> int:
    """Envoie le résumé quotidien aux comptes qui l'ont activé (une fois par jour résumé, voir `daily_target`).

    Rien pour un compte qui a choisi « Aucun e-mail » dans son profil, ni pour un compte qui n'a jamais ouvert
    Hangar (pas encore de préférences d'agenda : le résumé n'est pas activé à son insu)."""
    at = at or A.now()
    m = _mail()
    n = 0
    for user in User.query.filter(User.active.is_(True), User.contact_email.isnot(None)).all():
        if not m.accepte_emails(user):
            continue
        s = db.session.get(AgendaSettings, user.id)
        day = daily_target(s, at) if s is not None else None
        if day is None:
            continue
        built = build_day_email(user, day, at)
        s.last_daily_date, s.last_daily_day = at.date(), day
        if built:  # en cas de panne passagère, nouvel essai jusqu'à la fin de la journée résumée
            m.queue_email(user.contact_email, built[0], built[1], kind="agenda", to_user=user,
                          expires_at=datetime.combine(day + timedelta(days=1), time(0)))
            n += 1
    db.session.commit()
    return n


def digest_enabled(user: User) -> bool:
    """Le résumé quotidien part-il pour ce compte ? (il remplace alors le rappel quotidien des échéances)"""
    s = db.session.get(AgendaSettings, user.id)
    return s is not None and s.daily_mode in ("morning", "evening")


def queue_import_email(user: User, imp: AgendaImport, at: datetime) -> None:
    m = _mail()
    if not m.accepte_emails(user):
        return
    cfg = m._cfg()
    items = []
    for d in imp.details:
        if d["type"] == "added" and not d.get("notable"):
            continue
        snap = d.get("after") or d.get("before")
        start = datetime.fromisoformat(snap["start"])
        if start.date() < at.date():
            continue
        label = {"added": "Ajouté", "modified": "Modifié", "removed": "Retiré", "restored": "Rétabli"}[d["type"]]
        extra = ""
        if d["type"] == "modified":
            extra = "<br><span style='color:#6b7280'>" + escape(" · ".join(
                A.change_lines({f: [d["before"][f], d["after"][f]] for f in d.get("fields", [])}))) + "</span>"
        items.append(f"<li style='margin:5px 0'><b>{label}</b> — {escape(A.day_short(start.date()))} {A.hm(start)} : "
                     f"{escape(snap['title'])}{extra}</li>")
    new_weeks = sum(1 for d in imp.details if d["type"] == "added" and not d.get("notable"))
    if not items and not new_weeks:
        return
    content = (f"<p>Bonjour {escape(user.short_name)},</p><p>Ton emploi du temps a été mis à jour "
               f"(fichier <i>{escape(imp.filename)}</i>).</p>")
    if items:
        content += "<h2 style='font-size:14px;margin:14px 0 6px'>Changements à venir</h2><ul style='padding-left:18px'>" + "".join(items[:40]) + "</ul>"
    if new_weeks:
        content += f"<p>{new_weeks} nouvelle(s) séance(s) ajoutée(s) pour des semaines pas encore importées.</p>"
    html = m.layout("Emploi du temps mis à jour", content,
                    button=("Voir le détail", m.absolute(f"/agenda/imports/{imp.id}", cfg)), cfg=cfg)
    m.queue_email(user.contact_email, f"🔄 Emploi du temps : {imp.modified} modifié(s), {imp.removed} retiré(s), "
                  f"{imp.added} ajouté(s)", html, kind="agenda", to_user=user)



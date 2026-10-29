"""E-mails automatiques : file d'envoi, SMTP ou boîte d'envoi locale, rappels et récapitulatifs planifiés.

- Les requêtes web ne font que *mettre en file* les messages (jamais d'attente SMTP).
- Un fil d'exécution (`start_worker`, lancé par `hangar run`) envoie la file et déclenche les envois planifiés.
- En mode « Boîte d'envoi locale », rien ne quitte la machine : chaque message est écrit en .eml dans data/outbox/.
"""
from __future__ import annotations

import os
import re
import smtplib
import ssl
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from email.message import EmailMessage as MimeMessage
from email.utils import formatdate, make_msgid

from flask import current_app, has_app_context
from markupsafe import escape

from .extensions import db
from .models import (
    PRIORITY_LABELS, EmailMessage, Issue, MailConfig, Notification, Project, ProjectMember, User, now,
)

EMAIL_RE = re.compile(r"^[^@\s<>,;\"']+@[^@\s<>,;\"']+\.[^@\s<>,;\"']+$")
KIND_LABELS = {"notification": "Notification", "reminder": "Rappel", "digest": "Récapitulatif",
               "bilan": "Bilan", "manual": "Message", "test": "Test"}
STATUS_LABELS = {"queued": "En attente", "sent": "Envoyé", "failed": "Échec", "outbox": "Boîte d’envoi locale"}
WEEKDAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]

_send_lock = threading.Lock()
_worker = None


def valid_email(addr: str | None) -> bool:
    return bool(addr) and bool(EMAIL_RE.match(addr.strip()))


def split_addresses(text: str | None) -> list[str]:
    return [a.strip() for a in re.split(r"[,;\s]+", text or "") if a.strip()]


@contextmanager
def _ctx(app=None):
    if app is not None and not has_app_context():
        with app.app_context():
            yield
    else:
        yield


def _cfg() -> MailConfig:
    return MailConfig.get()


def absolute(path: str | None, cfg: MailConfig | None = None) -> str:
    cfg = cfg or _cfg()
    base = (cfg.base_url or "http://127.0.0.1:5000").rstrip("/")
    if not path:
        return base + "/"
    if path.startswith("/"):
        return base + path
    return path


def absolutize_links(html: str, cfg: MailConfig | None = None) -> str:
    cfg = cfg or _cfg()
    base = (cfg.base_url or "http://127.0.0.1:5000").rstrip("/")
    return re.sub(r'(href|src)="/(?!/)', lambda m: f'{m.group(1)}="{base}/', html)


def issue_path(issue: Issue) -> str:
    return f"/projects/{issue.project_id}/issues/{issue.sequence_id}"


# ---------------------------------------------------------------------------
# Mise en forme
# ---------------------------------------------------------------------------

def html_to_text(html: str) -> str:
    t = re.sub(r"<(br|/p|/div|/li|/h[1-6]|/tr)[^>]*>", "\n", html, flags=re.I)
    t = re.sub(r"<li[^>]*>", "- ", t, flags=re.I)
    t = re.sub(r'<a [^>]*href="([^"]+)"[^>]*>(.*?)</a>', r"\2 (\1)", t, flags=re.I | re.S)
    t = re.sub(r"<[^>]+>", "", t)
    t = (t.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
         .replace("&#39;", "'").replace("&quot;", '"'))
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def layout(title: str, content_html: str, button: tuple[str, str] | None = None, footer: str | None = None,
           cfg: MailConfig | None = None) -> str:
    """Gabarit HTML (styles en ligne, compatible avec les clients de messagerie)."""
    cfg = cfg or _cfg()
    btn = ""
    if button:
        label, url = button
        btn = (f'<p style="margin:22px 0 4px"><a href="{escape(url)}" style="display:inline-block;background:#3f76ff;'
               f'color:#ffffff;text-decoration:none;padding:10px 18px;border-radius:7px;font-weight:600;'
               f'font-size:14px">{escape(label)}</a></p>')
    foot = footer or (f'Message envoyé automatiquement par Hangar. Gérer mes préférences : '
                      f'<a href="{escape(absolute("/settings/profile", cfg))}" style="color:#6b7280">mon profil</a>.')
    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#1d2127">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6;padding:24px 12px"><tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:620px">
<tr><td style="padding:0 4px 12px;font-size:15px;font-weight:700;color:#1d2127">
<span style="display:inline-block;background:#3f76ff;color:#fff;border-radius:7px;padding:3px 8px;margin-right:6px">✈</span>Hangar</td></tr>
<tr><td style="background:#ffffff;border:1px solid #e4e7ec;border-radius:10px;padding:24px 26px;font-size:14px;line-height:1.55">
<h1 style="font-size:19px;margin:0 0 14px;line-height:1.3">{escape(title)}</h1>
{content_html}{btn}
</td></tr>
<tr><td style="padding:14px 6px;font-size:12px;color:#6b7280;line-height:1.5">{foot}</td></tr>
</table></td></tr></table></body></html>"""


def issue_card(i: Issue, cfg: MailConfig) -> str:
    meta = [escape(i.state.name) if i.state else ""]
    if i.priority and i.priority != "none":
        meta.append(f"priorité {escape(PRIORITY_LABELS.get(i.priority, i.priority).lower())}")
    if i.target_date:
        color = "#dc2626" if i.is_overdue else "#4a5260"
        meta.append(f'<span style="color:{color}">échéance {i.target_date:%d/%m/%Y}</span>')
    if i.assignees:
        meta.append(escape(", ".join(u.display_name for u in i.assignees)))
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #e4e7ec;'
            f'border-radius:8px;margin:6px 0"><tr><td style="padding:10px 12px">'
            f'<a href="{escape(absolute(issue_path(i), cfg))}" style="color:#1d2127;text-decoration:none;font-weight:600">'
            f'<span style="font-family:Menlo,Consolas,monospace;color:#8a919c;font-weight:400;font-size:12px">{escape(i.key)}</span> '
            f'{escape(i.title)}</a><div style="color:#4a5260;font-size:12.5px;margin-top:3px">'
            f'{" · ".join(m for m in meta if m)}</div></td></tr></table>')


def issue_list(title: str, issues, cfg: MailConfig, color: str = "#1d2127") -> str:
    if not issues:
        return ""
    return (f'<h2 style="font-size:14px;margin:18px 0 6px;color:{color}">{escape(title)} ({len(issues)})</h2>'
            + "".join(issue_card(i, cfg) for i in issues))


# ---------------------------------------------------------------------------
# File d'envoi
# ---------------------------------------------------------------------------

def queue_email(to_address: str, subject: str, html: str, text: str | None = None, kind: str = "manual",
                to_user: User | None = None, created_by: User | None = None, reply_to: str | None = None) -> EmailMessage:
    """Ajoute un message à la file (sans commit : l'appelant valide la transaction)."""
    msg = EmailMessage(to_address=to_address.strip(), subject=subject.strip()[:300], body_html=html,
                       body_text=text or html_to_text(html), kind=kind, status="queued",
                       to_user_id=to_user.id if to_user else None,
                       created_by_id=created_by.id if created_by else None, reply_to=reply_to)
    db.session.add(msg)
    return msg


def build_mime(m: EmailMessage, cfg: MailConfig) -> MimeMessage:
    mime = MimeMessage()
    mime["Subject"] = m.subject
    mime["From"] = cfg.sender
    mime["To"] = m.to_address
    mime["Date"] = formatdate(localtime=True)
    mime["Message-ID"] = make_msgid(domain="hangar.local")
    if m.reply_to:
        mime["Reply-To"] = m.reply_to
    mime["X-Mailer"] = "Hangar"
    mime.set_content(m.body_text or html_to_text(m.body_html or ""))
    if m.body_html:
        mime.add_alternative(m.body_html, subtype="html")
    return mime


def outbox_dir() -> str:
    path = os.path.join(current_app.config["DATA_DIR"], "outbox")
    os.makedirs(path, exist_ok=True)
    return path


def eml_filename(m: EmailMessage) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", m.subject)[:50].strip("-") or "message"
    return f"{(m.created_at or now()):%Y%m%d_%H%M%S}_{m.id}_{slug}.eml"


def _open_smtp(cfg: MailConfig):
    context = ssl.create_default_context()
    port = cfg.smtp_port or (465 if cfg.smtp_security == "ssl" else 587)
    if cfg.smtp_security == "ssl":
        server = smtplib.SMTP_SSL(cfg.smtp_host, port, timeout=20, context=context)
    else:
        server = smtplib.SMTP(cfg.smtp_host, port, timeout=20)
        server.ehlo()
        if cfg.smtp_security == "starttls":
            server.starttls(context=context)
            server.ehlo()
    if cfg.smtp_user:
        server.login(cfg.smtp_user, cfg.smtp_password or "")
    return server


def send_pending(app=None, limit: int = 100, ids: list[int] | None = None) -> dict:
    """Envoie les messages en attente (SMTP ou fichiers .eml). Retourne les compteurs."""
    stats = {"sent": 0, "outbox": 0, "failed": 0}
    with _ctx(app), _send_lock:
        cfg = _cfg()
        q = EmailMessage.query.filter_by(status="queued")
        if ids:
            q = q.filter(EmailMessage.id.in_(ids))
        pending = q.order_by(EmailMessage.id).limit(limit).all()
        if not pending:
            return stats
        use_smtp = cfg.mode == "smtp" and bool(cfg.smtp_host)
        if use_smtp:
            try:
                server = _open_smtp(cfg)
            except Exception as exc:  # connexion impossible : tous en échec, réessayables
                for m in pending:
                    m.status, m.error = "failed", f"Connexion SMTP impossible : {exc}"
                    stats["failed"] += 1
                db.session.commit()
                return stats
            try:
                for m in pending:
                    try:
                        server.send_message(build_mime(m, cfg))
                        m.status, m.sent_at, m.error = "sent", now(), None
                        stats["sent"] += 1
                    except Exception as exc:
                        m.status, m.error = "failed", str(exc)[:1000]
                        stats["failed"] += 1
            finally:
                try:
                    server.quit()
                except Exception:
                    pass
        else:
            folder = outbox_dir()
            for m in pending:
                try:
                    with open(os.path.join(folder, eml_filename(m)), "wb") as fh:
                        fh.write(bytes(build_mime(m, cfg)))
                    m.status, m.sent_at, m.error = "outbox", now(), None
                    stats["outbox"] += 1
                except Exception as exc:
                    m.status, m.error = "failed", str(exc)[:1000]
                    stats["failed"] += 1
        db.session.commit()
    return stats


def send_test(address: str, actor: User | None = None) -> EmailMessage:
    cfg = _cfg()
    mode = ("SMTP via " + cfg.smtp_host) if cfg.mode == "smtp" and cfg.smtp_host else "boîte d’envoi locale"
    html = layout("E-mail de test", f"<p>Bonjour,</p><p>Si tu lis ce message, l’envoi des e-mails de Hangar fonctionne "
                                   f"(mode : <b>{escape(mode)}</b>).</p>",
                  button=("Ouvrir Hangar", absolute("/", cfg)), cfg=cfg)
    m = queue_email(address, "Hangar — e-mail de test", html, kind="test", created_by=actor)
    db.session.commit()
    send_pending(ids=[m.id])
    return m


# ---------------------------------------------------------------------------
# Notifications instantanées (appelé par services.notify)
# ---------------------------------------------------------------------------

def on_notification(user: User, actor: User | None, issue: Issue | None, kind: str, message: str,
                    url: str | None = None) -> None:
    """Met en file un e-mail pour une notification, si l'utilisateur l'a demandé."""
    cfg = db.session.get(MailConfig, 1)  # pas de création ici : on reste dans la transaction de la requête
    if cfg is None or not cfg.enabled or not cfg.send_notifications:
        return
    if not valid_email(user.contact_email) or (user.email_prefs or "instant") != "instant":
        return
    head = message.split(" · ")[0]
    if issue is not None:
        head = head.replace(f" {issue.key}", "").strip()
        subject = f"[{issue.key}] {head} : {issue.title}"
        content = f"<p>{escape(message.split(' · ')[0])}.</p>" + issue_card(issue, cfg)
        button = ("Voir le ticket", absolute(url or issue_path(issue), cfg))
    else:
        subject = head
        content = f"<p>{escape(message)}</p>"
        button = ("Ouvrir Hangar", absolute(url or "/notifications", cfg))
    html = layout(head, content, button=button, cfg=cfg)
    queue_email(user.contact_email, subject[:300], html, kind="notification", to_user=user, created_by=actor)


# ---------------------------------------------------------------------------
# Envois planifiés
# ---------------------------------------------------------------------------

def _recipients(reminders_only=False):
    users = User.query.filter(User.active.is_(True), User.contact_email.isnot(None)).all()
    out = []
    for u in users:
        if not valid_email(u.contact_email) or (u.email_prefs or "instant") == "off":
            continue
        if reminders_only and u.email_reminders is False:
            continue
        out.append(u)
    return out


def _open_issues_of(user: User):
    from . import issue_query as IQ
    from .utils import visible_projects
    pids = [p.id for p in visible_projects(user)] or [0]
    issues = IQ.base_query().filter(Issue.project_id.in_(pids), Issue.assignees.any(User.id == user.id)).all()
    return issues


def send_reminders(cfg: MailConfig, today: date) -> int:
    n = 0
    tomorrow = today + timedelta(days=1)
    for u in _recipients(reminders_only=True):
        open_ = [i for i in _open_issues_of(u) if not i.is_closed and i.target_date and i.target_date <= tomorrow]
        if not open_:
            continue
        overdue = sorted([i for i in open_ if i.target_date < today], key=lambda i: i.target_date)
        due_today = [i for i in open_ if i.target_date == today]
        due_tomorrow = [i for i in open_ if i.target_date == tomorrow]
        content = (f"<p>Bonjour {escape(u.short_name)},</p><p>Voici tes tickets qui demandent ton attention "
                   f"aujourd’hui.</p>"
                   + issue_list("En retard", overdue, cfg, "#dc2626")
                   + issue_list("À rendre aujourd’hui", due_today, cfg, "#d97706")
                   + issue_list("À rendre demain", due_tomorrow, cfg))
        parts = []
        if overdue:
            parts.append(f"{len(overdue)} en retard")
        if due_today:
            parts.append(f"{len(due_today)} aujourd’hui")
        if due_tomorrow:
            parts.append(f"{len(due_tomorrow)} demain")
        html = layout("Rappel de tes échéances", content,
                      button=("Voir mes tickets", absolute("/issues?scope=assigned&order_by=target", cfg)), cfg=cfg)
        queue_email(u.contact_email, f"Hangar — rappel : {', '.join(parts)}", html, kind="reminder", to_user=u)
        n += 1
    return n


def send_digests(cfg: MailConfig, today: date) -> int:
    n = 0
    week_ago = today - timedelta(days=7)
    for u in _recipients():
        mine = _open_issues_of(u)
        open_ = sorted([i for i in mine if not i.is_closed],
                       key=lambda i: (i.target_date or date(9999, 1, 1), i.sequence_id))
        overdue = [i for i in open_ if i.is_overdue]
        week_end = today + timedelta(days=6)
        due_week = [i for i in open_ if i.target_date and today <= i.target_date <= week_end]
        done = [i for i in mine if i.state_group == "completed" and i.completed_at
                and i.completed_at.date() >= week_ago]
        unread = Notification.query.filter(Notification.user_id == u.id, Notification.read_at.is_(None),
                                           Notification.archived_at.is_(None)).order_by(
            Notification.created_at.desc()).all()
        if not (open_ or done or unread):
            continue
        stats = (f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin:6px 0 4px"><tr>'
                 + "".join(f'<td style="padding:8px 14px;border:1px solid #e4e7ec;border-radius:8px;text-align:center">'
                           f'<div style="font-size:20px;font-weight:700;color:{c}">{v}</div>'
                           f'<div style="font-size:11.5px;color:#6b7280">{lab}</div></td><td width="8"></td>'
                           for v, lab, c in ((len(open_), "ouverts", "#3f76ff"), (len(overdue), "en retard", "#dc2626"),
                                             (len(done), "terminés (7 j)", "#16a34a"), (len(unread), "notifications", "#1d2127")))
                 + "</tr></table>")
        notif_html = ""
        if unread and (u.email_prefs or "instant") == "digest":
            notif_html = ('<h2 style="font-size:14px;margin:18px 0 6px">Notifications non lues</h2><ul style="padding-left:18px;margin:0">'
                          + "".join(f'<li style="margin:3px 0">{escape(x.message)}</li>' for x in unread[:15]) + "</ul>")
        content = (f"<p>Bonjour {escape(u.short_name)},</p><p>Voici ton récapitulatif de la semaine.</p>{stats}"
                   + issue_list("En retard", overdue[:10], cfg, "#dc2626")
                   + issue_list("À rendre cette semaine", [i for i in due_week if i not in overdue][:10], cfg)
                   + issue_list("Terminés ces 7 derniers jours", done[:10], cfg, "#16a34a")
                   + notif_html)
        html = layout("Ton récapitulatif hebdomadaire", content,
                      button=("Ouvrir mon accueil", absolute("/", cfg)), cfg=cfg)
        queue_email(u.contact_email, f"Hangar — ta semaine : {len(open_)} tickets ouverts, {len(overdue)} en retard",
                    html, kind="digest", to_user=u)
        n += 1
    return n


def _simple_bilan_md(projects, start: date, end: date) -> str:
    """Résumé de secours si le module bilan n'est pas disponible."""
    from . import issue_query as IQ
    lines = [f"Période du {start:%d/%m/%Y} au {end:%d/%m/%Y}.", ""]
    for p in projects:
        issues = IQ.base_query().filter(Issue.project_id == p.id).all()
        done = [i for i in issues if i.state_group == "completed" and i.completed_at
                and start <= i.completed_at.date() <= end]
        created = [i for i in issues if i.created_at and start <= i.created_at.date() <= end]
        overdue = [i for i in issues if i.is_overdue]
        st = p.stats()
        lines += [f"## {p.emoji} {p.name}", "",
                  f"- Avancement global : **{st['progress']} %** ({st['done']} terminés, {st['open']} ouverts)",
                  f"- Créés sur la période : {len(created)} · terminés : **{len(done)}** · en retard : {len(overdue)}", ""]
        if done:
            lines += ["**Terminés**", ""] + [f"- {i.key} {i.title}" for i in done[:20]] + [""]
        if overdue:
            lines += ["**En retard**", ""] + [f"- {i.key} {i.title} (échéance {i.target_date:%d/%m})"
                                              for i in overdue[:20]] + [""]
    return "\n".join(lines)


def build_bilan_email(user: User, projects, start: date, end: date, cfg: MailConfig) -> tuple[str, str]:
    from .utils import render_markdown
    md = None
    try:
        from .bilan import bilan_markdown, compute_bilan
        md = bilan_markdown(compute_bilan(projects, start, end))
    except Exception:
        md = None
    if not md:
        md = _simple_bilan_md(projects, start, end)
    body = absolutize_links(str(render_markdown(md)), cfg)
    names = ", ".join(p.name for p in projects) if len(projects) <= 2 else f"{len(projects)} projets"
    content = (f"<p>Bonjour {escape(user.short_name)},</p><p>Voici le bilan de la semaine pour <b>{escape(names)}</b>.</p>"
               f'<div style="border-top:1px solid #e4e7ec;margin-top:12px;padding-top:6px">{body}</div>')
    path = f"/projects/{projects[0].id}/bilan" if len(projects) == 1 else "/bilan"
    html = layout(f"Bilan hebdomadaire · semaine du {start:%d/%m}", content,
                  button=("Voir le bilan complet", absolute(path, cfg)), cfg=cfg)
    return f"Hangar — bilan de la semaine du {start:%d/%m/%Y}", html


def send_bilans(cfg: MailConfig, today: date) -> int:
    from .utils import visible_projects
    start, end = today - timedelta(days=6), today
    n = 0
    for u in _recipients():
        if u.is_admin:
            projects = visible_projects(u)
        else:
            led = Project.query.filter(Project.lead_id == u.id, Project.archived_at.is_(None)).all()
            admin_of = [m.project for m in ProjectMember.query.filter_by(user_id=u.id, role="admin")
                        if m.project.archived_at is None]
            projects = list({p.id: p for p in led + admin_of}.values())
        if not projects:
            continue
        subject, html = build_bilan_email(u, projects, start, end, cfg)
        queue_email(u.contact_email, subject, html, kind="bilan", to_user=u)
        n += 1
    return n


def run_scheduled(app=None, at: datetime | None = None, force: set[str] | frozenset = frozenset()) -> dict:
    """Déclenche les envois planifiés dus (une seule fois par jour / semaine). `force` : jobs à lancer quand même."""
    res = {}
    with _ctx(app):
        cfg = _cfg()
        at = at or datetime.now()
        today = at.date()
        auto = cfg.enabled
        if "reminders" in force or (auto and cfg.send_reminders and at.hour >= (cfg.reminder_hour or 0)
                                    and cfg.last_reminder_date != today):
            res["reminders"] = send_reminders(cfg, today)
            cfg.last_reminder_date = today
        if "digest" in force or (auto and cfg.send_digest and today.weekday() == (cfg.digest_weekday or 0)
                                 and at.hour >= (cfg.reminder_hour or 0) and cfg.last_digest_date != today):
            res["digest"] = send_digests(cfg, today)
            cfg.last_digest_date = today
        if "bilan" in force or (auto and cfg.send_bilan and today.weekday() == (cfg.bilan_weekday or 0)
                                and at.hour >= (cfg.bilan_hour or 0) and cfg.last_bilan_date != today):
            res["bilan"] = send_bilans(cfg, today)
            cfg.last_bilan_date = today
        db.session.commit()
    return res


# ---------------------------------------------------------------------------
# Fil d'exécution en arrière-plan
# ---------------------------------------------------------------------------

def start_worker(app, interval: int = 30):
    """Lance (une seule fois) le fil qui envoie la file et les messages planifiés."""
    global _worker
    if _worker is not None and _worker.is_alive():
        return _worker

    def loop():
        while True:
            try:
                with app.app_context():
                    run_scheduled()
                    send_pending()
                    db.session.remove()
            except Exception:  # ne jamais arrêter le serveur pour un e-mail
                app.logger.exception("Erreur dans l’envoi des e-mails")
            time.sleep(interval)

    _worker = threading.Thread(target=loop, name="hangar-mail", daemon=True)
    _worker.start()
    return _worker

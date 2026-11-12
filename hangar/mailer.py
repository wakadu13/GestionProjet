"""E-mails automatiques : file d'envoi, SMTP ou boîte d'envoi locale, rappels et récapitulatifs planifiés.

- Les requêtes web ne font que *mettre en file* les messages (jamais d'attente SMTP).
- Un fil d'exécution (`start_worker`, lancé par `hangar run`) envoie la file et déclenche les envois planifiés.
  Chaque étape du fil est protégée : une étape en panne est notée sur /erreurs/ sans bloquer les suivantes.
- En mode « Boîte d'envoi locale », rien ne quitte la machine : chaque message est écrit en .eml dans data/outbox/.
- Échec passager (pas de réseau au réveil du portable, serveur injoignable, délai dépassé, coupure, code 4xx) :
  le message reste en file et le fil réessaie après 1, 5, 15 puis 60 min, jusqu'à sa date limite (fin de la
  séance ou de la tâche pour un rappel, fin de la journée pour le résumé quotidien, 24 h sinon). Un mot de
  passe refusé ou un destinataire rejeté ne sont pas réessayés : le message passe tout de suite en échec.
"""
from __future__ import annotations

import os
import re
import smtplib
import socket
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
               "bilan": "Bilan", "manual": "Message", "test": "Test", "agenda": "Agenda"}
STATUS_LABELS = {"queued": "En attente", "sent": "Envoyé", "failed": "Échec", "outbox": "Boîte d’envoi locale"}
WEEKDAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]

_send_lock = threading.Lock()
_worker = None

RETRY_DELAYS = (1, 5, 15, 60)            # minutes d'attente après 1, 2, 3, puis 4 échecs ou plus d'affilée
DEFAULT_LIFETIME = timedelta(hours=24)   # date limite d'un message qui n'en a pas (création + 24 h)
# Une panne de connexion touche tous les messages : l'attente est commune et gardée en mémoire (au
# redémarrage de Hangar, un nouvel essai part tout de suite).
_retry = {"failures": 0, "next": None, "error": ""}


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
                to_user: User | None = None, created_by: User | None = None, reply_to: str | None = None,
                expires_at: datetime | None = None) -> EmailMessage:
    """Ajoute un message à la file (sans commit : l'appelant valide la transaction).

    `expires_at` : au-delà, plus de nouvel essai après un échec passager (par défaut 24 h après la création)."""
    subject = " ".join((subject or "").split())  # un retour à la ligne (vieux titre de ticket) est interdit ici
    msg = EmailMessage(to_address=to_address.strip(), subject=subject[:300], body_html=html,
                       body_text=text or html_to_text(html), kind=kind, status="queued",
                       to_user_id=to_user.id if to_user else None,
                       created_by_id=created_by.id if created_by else None, reply_to=reply_to,
                       expires_at=expires_at, attempts=0)
    db.session.add(msg)
    return msg


MAX_EXTERIEURS = 20   # adresses hors de l'équipe par message, pour un membre
PLAFOND_JOUR = 100    # e-mails écrits à la main (rédaction, bilan) par un membre sur 24 h


def refus_envoi(user: User, adresses) -> str | None:
    """Un membre (non administrateur) peut-il envoyer ce message (rédaction ou bilan) à ces adresses ?

    Renvoie None, ou la raison du refus à afficher. Les messages partent de l'adresse configurée par
    l'administrateur (souvent son Gmail) : hors de l'équipe seulement si le réglage le permet, 20 adresses
    extérieures au plus par message, et 100 e-mails au plus par 24 h (rédaction et bilans de ce compte)."""
    if user.is_admin:
        return None
    adresses = {a.strip().lower() for a in adresses if a}
    equipe = {u.contact_email.strip().lower() for u in User.query.filter(User.active.is_(True))
              if valid_email(u.contact_email)}
    dehors = sorted(adresses - equipe)
    if dehors and not MailConfig.get().members_external:
        return ("Tu ne peux écrire qu’aux membres de l’équipe : l’envoi hors de l’équipe ("
                + ", ".join(dehors[:3]) + ("…" if len(dehors) > 3 else "")
                + ") doit être autorisé par un administrateur, dans Réglages › E-mails.")
    if len(dehors) > MAX_EXTERIEURS:
        return (f"{MAX_EXTERIEURS} destinataires hors de l’équipe au plus par message (tu en as mis {len(dehors)}) : "
                "découpe ton envoi ou demande à un administrateur.")
    deja = EmailMessage.query.filter(EmailMessage.created_by_id == user.id,
                                     EmailMessage.kind.in_(("manual", "bilan")),
                                     EmailMessage.created_at >= now() - timedelta(hours=24)).count()
    if deja + len(adresses) > PLAFOND_JOUR:
        return (f"Plafond atteint : {PLAFOND_JOUR} e-mails par 24 heures au plus (déjà {deja} depuis hier à la même "
                f"heure, ce message en ajouterait {len(adresses)}). Réessaie plus tard ou demande à un administrateur.")
    return None


def deadline(m: EmailMessage) -> datetime:
    """Date limite d'envoi d'un message (au-delà, un message en échec n'est plus réessayé ni renvoyé)."""
    return m.expires_at or (m.created_at or now()) + DEFAULT_LIFETIME


def build_mime(m: EmailMessage, cfg: MailConfig) -> MimeMessage:
    mime = MimeMessage()
    mime["Subject"] = " ".join((m.subject or "").split())
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


SMTP_PASSWORD_ENV = "HANGAR_SMTP_PASSWORD"


def smtp_password(cfg: MailConfig) -> str:
    """Mot de passe SMTP : celui enregistré dans les réglages, sinon la variable d'environnement
    HANGAR_SMTP_PASSWORD (il n'est alors ni dans la base ni dans les sauvegardes)."""
    return cfg.smtp_password or os.environ.get(SMTP_PASSWORD_ENV, "")


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
        server.login(cfg.smtp_user, smtp_password(cfg))
    return server


def describe_error(exc: BaseException) -> str:
    """Raison d'un échec d'envoi en français simple, suivie du détail technique entre parenthèses."""
    detail = str(exc) or type(exc).__name__
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        text = ("Identifiant ou mot de passe refusé par le serveur d’envoi (pour Gmail, il faut un "
                "« mot de passe d’application »).")
    elif isinstance(exc, smtplib.SMTPRecipientsRefused):
        text = "Adresse du destinataire refusée par le serveur d’envoi."
    elif isinstance(exc, smtplib.SMTPSenderRefused):
        text = "Adresse d’expédition refusée par le serveur d’envoi."
    elif isinstance(exc, smtplib.SMTPServerDisconnected):
        text = "Connexion avec le serveur d’envoi coupée."
    elif isinstance(exc, smtplib.SMTPNotSupportedError):
        text = "Le serveur d’envoi ne gère pas ce réglage (vérifie la sécurité et l’identifiant)."
    elif isinstance(exc, smtplib.SMTPResponseException) and 400 <= exc.smtp_code < 500:
        text = "Le serveur d’envoi est momentanément indisponible."
    elif isinstance(exc, smtplib.SMTPException):
        text = "Le serveur d’envoi a refusé le message."
    elif isinstance(exc, socket.gaierror):
        text = "Serveur d’envoi introuvable : pas de connexion Internet, ou adresse du serveur mal écrite."
    elif isinstance(exc, TimeoutError):
        text = "Le serveur d’envoi ne répond pas (délai dépassé)."
    elif isinstance(exc, ConnectionRefusedError):
        text = "Le serveur d’envoi refuse la connexion (serveur arrêté, ou port mal réglé)."
    elif isinstance(exc, ssl.SSLError):
        text = "Échec de la connexion sécurisée avec le serveur d’envoi (vérifie la sécurité et le port)."
    elif isinstance(exc, OSError):
        text = "Pas de connexion avec le serveur d’envoi (réseau indisponible ?)."
    else:
        text = "Erreur pendant l’envoi."
    return f"{text}{DETAIL_SEP}{detail})"[:1000]


DETAIL_SEP = " (détail : "


def plain_error(error: str | None) -> str:
    """Raison d'un échec sans le détail technique (pour l'agenda)."""
    return (error or "").split(DETAIL_SEP, 1)[0]


def is_transient(exc: BaseException) -> bool:
    """Erreur passagère : réseau absent, DNS, délai dépassé, connexion coupée ou refusée, code 4xx.
    Un mot de passe refusé, un destinataire rejeté ou un réglage incompatible ne le sont pas."""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return False
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        codes = [code for code, _ in (exc.recipients or {}).values()]
        return bool(codes) and all(400 <= c < 500 for c in codes)
    if isinstance(exc, smtplib.SMTPResponseException):
        return 400 <= exc.smtp_code < 500
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return True
    if isinstance(exc, smtplib.SMTPException):
        return False
    if isinstance(exc, ssl.SSLError):  # certificat, mauvais port : réglage ; fin de flux : coupure réseau
        return isinstance(exc, (ssl.SSLEOFError, ssl.SSLZeroReturnError))
    return isinstance(exc, OSError)


def _connection_lost(exc: BaseException) -> bool:
    """Après cette erreur, la connexion ne sert plus : les messages suivants attendent le prochain essai."""
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return True
    if isinstance(exc, smtplib.SMTPResponseException):
        return exc.smtp_code == 421
    return isinstance(exc, OSError) and not isinstance(exc, smtplib.SMTPException)


def reset_retry() -> None:
    """Oublie les échecs de connexion (réglages modifiés, envoi réussi) : le prochain essai part tout de suite."""
    _retry.update(failures=0, next=None, error="")


def retry_state() -> dict:
    """Prochain essai automatique après une panne (affiché dans Réglages › E-mails)."""
    return dict(_retry)


def _note_failure(at: datetime, error: str) -> None:
    _retry["failures"] += 1
    _retry["next"] = at + timedelta(minutes=RETRY_DELAYS[min(_retry["failures"], len(RETRY_DELAYS)) - 1])
    _retry["error"] = error


def _log_warning(message: str, *args) -> None:
    try:
        current_app.logger.warning(message, *args)
    except Exception:  # noqa: BLE001 - hors application : pas de journal
        pass


def send_pending(app=None, limit: int = 100, ids: list[int] | None = None, at: datetime | None = None,
                 force: bool = False) -> dict:
    """Envoie les messages en attente (SMTP ou fichiers .eml). Retourne les compteurs.

    `ids` : envoi lancé depuis l'interface (test, rédaction, renvoi) : essai immédiat, et un échec est
    immédiat pour pouvoir l'afficher. Sinon (fil d'arrière-plan, « Traiter la file ») : un message en échec
    passager reste en file (« waiting ») ; le prochain essai attend 1, 5, 15 puis 60 min (`force` : sans
    attendre) et le message est abandonné à sa date limite (voir `deadline`)."""
    stats = {"sent": 0, "outbox": 0, "failed": 0, "waiting": 0}
    with _ctx(app), _send_lock:
        at = at or now()
        cfg = _cfg()
        q = EmailMessage.query.filter_by(status="queued")
        if ids:
            q = q.filter(EmailMessage.id.in_(ids))
        pending = q.order_by(EmailMessage.id).limit(limit).all()
        if not pending:
            return stats
        if cfg.mode == "smtp" and bool(cfg.smtp_host):
            _send_smtp(cfg, pending, at, bool(ids), force, stats)
        else:
            folder = outbox_dir()
            for m in pending:
                try:
                    with open(os.path.join(folder, eml_filename(m)), "wb") as fh:
                        fh.write(bytes(build_mime(m, cfg)))
                    m.status, m.sent_at, m.error = "outbox", at, None
                    stats["outbox"] += 1
                except Exception as exc:
                    m.status, m.error = "failed", str(exc)[:1000]
                    stats["failed"] += 1
        db.session.commit()
    return stats


def _send_smtp(cfg: MailConfig, pending: list[EmailMessage], at: datetime, immediate: bool, force: bool,
               stats: dict) -> None:
    if not immediate:
        keep = []
        for m in pending:
            if deadline(m) <= at:  # trop tard : un rappel après la séance, le résumé d'hier… ne servent plus
                m.status = "failed"
                m.error = ("Pas envoyé à temps : " + (m.error or "Hangar n’a pas pu l’envoyer avant sa date limite."))[:1000]
                stats["failed"] += 1
            else:
                keep.append(m)
        pending = keep
        if not pending:
            return
        if not force and _retry["next"] is not None and at < _retry["next"]:
            stats["waiting"] += len(pending)  # panne en cours : on attend le prochain essai
            return
    try:
        server = _open_smtp(cfg)
    except Exception as exc:
        error, transient = describe_error(exc), is_transient(exc)
        for m in pending:
            m.attempts = (m.attempts or 0) + 1
            m.error = error
            if transient and not immediate:
                stats["waiting"] += 1
            else:
                m.status = "failed"
                stats["failed"] += 1
        # Même pour un mot de passe refusé : pas de nouvelle connexion à chaque passage (blocage du compte)
        _note_failure(at, error)
        _log_warning("Envoi des e-mails impossible : %s", error)
        return
    reset_retry()
    try:
        for i, m in enumerate(pending):
            try:
                server.send_message(build_mime(m, cfg))
                m.status, m.sent_at, m.error = "sent", at, None
                stats["sent"] += 1
            except Exception as exc:
                m.attempts = (m.attempts or 0) + 1
                m.error = describe_error(exc)
                if immediate or not is_transient(exc):
                    m.status = "failed"
                    stats["failed"] += 1
                    continue
                stats["waiting"] += 1
                _note_failure(at, m.error)
                _log_warning("E-mail %s : nouvel essai plus tard (%s)", m.id, m.error)
                if _connection_lost(exc):  # les suivants n'ont pas été essayés : ils attendent aussi
                    stats["waiting"] += len(pending) - i - 1
                    break
    finally:
        try:
            server.quit()
        except Exception:
            pass


def retry_failed(user: User | None = None, at: datetime | None = None) -> tuple[list[int], int]:
    """« Renvoyer les échecs » : remet en file les messages en échec encore utiles (date limite pas dépassée),
    tous ou seulement ceux de `user`. Retourne (identifiants remis en file, nombre de messages trop anciens)."""
    at = at or now()
    q = EmailMessage.query.filter_by(status="failed")
    if user is not None:
        q = q.filter(EmailMessage.to_user_id == user.id)
    ids, too_old = [], 0
    for m in q.order_by(EmailMessage.id):
        if deadline(m) <= at:
            too_old += 1
            continue
        m.status, m.error, m.attempts = "queued", None, 0
        ids.append(m.id)
    db.session.commit()
    return ids, too_old


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

def accepte_emails(user: User | None) -> bool:
    """Ce compte accepte-t-il les e-mails automatiques ? Non s'il est désactivé, sans adresse valide ou s'il a
    choisi « Aucun e-mail » dans son profil. Ne s'applique pas aux envois demandés à la main (test, aperçu)."""
    return (user is not None and bool(user.active) and valid_email(user.contact_email)
            and (user.email_prefs or "instant") != "off")


def _recipients(reminders_only=False):
    users = User.query.filter(User.active.is_(True), User.contact_email.isnot(None)).all()
    out = []
    for u in users:
        if not accepte_emails(u):
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
    from .agenda import digest_enabled
    for u in _recipients(reminders_only=True):
        if digest_enabled(u):  # le résumé quotidien contient déjà les tickets à échéance
            continue
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
    from flask import has_request_context

    from .bilan import bilan_markdown, compute_bilan
    md = None
    try:
        if has_request_context():
            md = bilan_markdown(compute_bilan(projects, start, end))
        else:  # fil d'arrière-plan, « hangar mail-send » : les adresses des tickets demandent une requête
            with current_app.test_request_context("/"):
                md = bilan_markdown(compute_bilan(projects, start, end))
    except Exception:  # noqa: BLE001 - l'e-mail part quand même, en version réduite, mais l'erreur est notée
        from .wiki import erreurs
        erreurs.signaler("Bilan hebdomadaire par e-mail : version réduite envoyée", dans_la_transaction=True)
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


def weekly_due(at: datetime, weekday: int | None, hour: int | None, last: date | None) -> bool:
    """Envoi hebdomadaire dû ? Oui dès que le moment prévu de la semaine (jour et heure) est passé et que rien
    n'est parti depuis : si Hangar était fermé ce jour-là, l'envoi part au premier lancement suivant de la même
    semaine (jusqu'au dimanche), une seule fois."""
    monday = at.date() - timedelta(days=at.weekday())
    moment = datetime.combine(monday + timedelta(days=weekday or 0), datetime.min.time()) + timedelta(hours=hour or 0)
    return at >= moment and (last is None or last < moment.date())


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
        if "digest" in force or (auto and cfg.send_digest
                                 and weekly_due(at, cfg.digest_weekday, cfg.reminder_hour, cfg.last_digest_date)):
            res["digest"] = send_digests(cfg, today)
            cfg.last_digest_date = today
        if "bilan" in force or (auto and cfg.send_bilan
                                and weekly_due(at, cfg.bilan_weekday, cfg.bilan_hour, cfg.last_bilan_date)):
            res["bilan"] = send_bilans(cfg, today)
            cfg.last_bilan_date = today
        db.session.commit()
    return res


# ---------------------------------------------------------------------------
# Fil d'exécution en arrière-plan
# ---------------------------------------------------------------------------

def noter_erreur(contexte: str) -> None:
    """Note l'exception en cours sur la page « Erreurs du serveur » (/erreurs/). Ne lève jamais."""
    from .wiki import erreurs
    erreurs.noter(contexte=contexte)  # ne lève jamais


def _etape(app, nom: str, fn):
    """Une étape du fil : en cas d'erreur, on annule ses écritures, on la journalise et on la note sur
    /erreurs/, puis le fil passe à l'étape suivante."""
    try:
        return fn()
    except Exception:  # noqa: BLE001
        db.session.rollback()
        app.logger.exception("Erreur dans le fil d’arrière-plan : %s", nom)
        noter_erreur(f"Fil des e-mails : {nom}")
        return None


def _agenda_jobs():
    from . import agenda  # rappels des tâches et séances, résumé quotidien
    return agenda.run_jobs()


def _qcm_rappels(app):
    from .qcm.rappels import envoyer_rappels_dus  # rappels de révision par notification web
    return envoyer_rappels_dus(journal=app.logger.warning)


def tick(app) -> None:
    """Un tour du fil d'arrière-plan : envois planifiés, agenda, révisions, puis la file d'e-mails."""
    # Un contexte de requête neuf à chaque tour : les adresses des pages (url_for) se construisent comme dans
    # l'appli, et les caches rangés dans `g` pendant le tour sont oubliés au suivant.
    with app.test_request_context("/"):
        try:
            _etape(app, "envois planifiés (rappels d’échéance, récapitulatifs, bilans)", lambda: run_scheduled())
            _etape(app, "rappels de l’agenda et résumé quotidien", _agenda_jobs)
            _etape(app, "rappels de révision (QCM)", lambda: _qcm_rappels(app))
            _etape(app, "envoi de la file d’e-mails", lambda: send_pending())
        finally:
            db.session.remove()


def start_worker(app, interval: int = 30):
    """Lance (une seule fois) le fil qui envoie la file et les messages planifiés."""
    global _worker
    if _worker is not None and _worker.is_alive():
        return _worker

    def loop():
        tours = 0
        while True:
            try:
                tick(app)
            except Exception:  # ne jamais arrêter le serveur pour un e-mail
                app.logger.exception("Erreur dans l’envoi des e-mails")
            tours += 1
            if tours % max(1, 600 // max(interval, 1)) == 0:  # toutes les 10 minutes environ
                from .extensions import vider_journal
                with app.app_context():
                    vider_journal()  # hangar.db à jour même si la fenêtre est fermée brutalement
            time.sleep(interval)

    _worker = threading.Thread(target=loop, name="hangar-mail", daemon=True)
    _worker.start()
    return _worker

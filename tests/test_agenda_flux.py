"""Tests du groupe « agenda-flux » (vague 3a) :

- piste 74 : abonnement en lecture seule par adresse secrète (/agenda/flux/<jeton>.ics), régénérable, 404 si
  le jeton est inconnu ou coupé, adresse affichée utilisable depuis le téléphone ; exemples d'API corrigés ;
- piste 76 : rappels de l'agenda en notifications push (ttl, étiquette propre à chaque rappel), troisième moyen
  « push » partout, abonnement de l'agenda séparé du rappel quotidien des QCM ;
- piste 79 : échéances de mes tickets et fins de sprint dans l'agenda (semaine, mois, liste, tableau de bord,
  export .ics), « Bloquer un créneau » ;
- piste 81 : emploi du temps récupéré tout seul (dossier surveillé, adresse .ics), empreinte (rien de réimporté
  si rien n'a changé), invitations et fichiers étrangers écartés, réglage réservé aux administrateurs.

Aucun appel réseau réel : pywebpush est simulé, l'adresse .ics est servie par un petit serveur local.

Lancer : uv run python -m unittest tests.test_agenda_flux -v
"""
import http.server
import json
import os
import re
import shutil
import tempfile
import threading
import time as _time
import unittest
from datetime import date, datetime, time, timedelta
from unittest import mock

from hangar import agenda as A
from hangar import create_app, ics, mailer
from hangar.extensions import db
from hangar.models import (AgendaEvent, AgendaImport, AgendaReminder, AgendaSettings, AgendaTask, Cycle,
                           EmailMessage, Issue, MailConfig, Notification, Project, ProjectMember, State, User)

ENDPOINT = "https://fcm.googleapis.com/fcm/send/appareil-1"


def monday_after(d: date) -> date:
    return d + timedelta(days=7 - d.weekday())


def make_ics(events, method="PUBLISH", stamp="20261101T000000Z", name=""):
    """events : [(uid, début, fin, intitulé, salle)]."""
    out = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//hacksw/handcal//NONSGML v1.0//EN"]
    if method:
        out.append(f"METHOD:{method}")
    if name:
        out.append(f"X-WR-CALNAME:{name}")
    out.append("X-WR-TIMEZONE:Europe/Paris")
    for uid, st, en, summary, loc in events:
        out += ["BEGIN:VEVENT", f"DTSTART:{st:%Y%m%dT%H%M%S}", f"DTEND:{en:%Y%m%dT%H%M%S}", f"DTSTAMP:{stamp}",
                f"UID:{uid}", f"SUMMARY:{summary}", f"LOCATION:{loc}", "END:VEVENT"]
    out.append("END:VCALENDAR")
    return ("\r\n".join(out) + "\r\n").encode()


def week_file(monday: date, room_math="SALLE A", stamp="20261101T000000Z"):
    """Planning de l'école pour la semaine du lundi `monday` : 4 séances (1001-1004)."""
    def at(day, h, m=0):
        return datetime.combine(monday + timedelta(days=day), time(h, m))
    return make_ics([
        ("1001", at(0, 8), at(0, 10), "Mathématiques 3 CM", room_math),
        ("1002", at(1, 10), at(1, 12), "Anglais  TD_TP", "DLE18"),
        ("1003", at(2, 13, 30), at(2, 17, 30), "Systèmes d'information 12 TD_TP", "SALLE B"),
        ("1004", at(3, 8), at(3, 10), "Droit 2 CM", "AMPHI"),
    ], stamp=stamp)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hangar-v3a-agenda-flux-")
        self.app = create_app(self.tmp, testing=True)
        self.app.config.update(WIKI_VERIF_INTERVALLE=0, QCM_VERIF_INTERVALLE=0)
        self.ctx = self.app.test_request_context("/")
        self.ctx.push()
        self.user = User(username="moi", email="moi@hangar.local", role="member", first_name="Moi",
                         contact_email="moi@exemple.fr")
        self.user.set_password("secret1")
        db.session.add(self.user)
        db.session.commit()
        mailer.reset_retry()

    def tearDown(self):
        mailer.reset_retry()
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def login(self, username="moi"):
        from flask import g
        g.pop("_login_user", None)
        c = self.app.test_client()
        token = re.search(r'name="_csrf" value="([^"]+)"', c.get("/login").get_data(as_text=True)).group(1)
        r = c.post("/login", data={"login": username, "password": "secret1", "_csrf": token})
        self.assertEqual(r.status_code, 302)
        return c, token

    def admin(self):
        u = User(username="chef", email="chef@hangar.local", role="admin", first_name="Chef")
        u.set_password("secret1")
        db.session.add(u)
        db.session.commit()
        return u


# ---------------------------------------------------------------------------
# Piste 74 : abonnement en lecture seule
# ---------------------------------------------------------------------------

class FeedTests(Base):
    def test_feed_by_secret_token(self):
        A.import_calendar(self.user, week_file(monday_after(date.today())), "planning.ics")
        c, csrf = self.login()
        self.assertEqual(c.post("/agenda/flux/regenerer", data={"_csrf": csrf}).status_code, 302)
        token = db.session.get(AgendaSettings, self.user.id).feed_token
        self.assertTrue(token and len(token) >= 40)

        anon = self.app.test_client()  # une appli de calendrier : ni cookie ni en-tête
        r = anon.get(f"/agenda/flux/{token}.ics")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.mimetype.startswith("text/calendar"))
        text = r.get_data(as_text=True)
        for needle in ("METHOD:PUBLISH", "BEGIN:VTIMEZONE", "TZID:Europe/Paris", "UID:1001@hangar",
                       "SUMMARY:Mathématiques (CM)"):
            self.assertIn(needle, text)
        self.assertEqual(len(ics.parse(text).events), 4)  # relu par notre propre lecteur

        # Jeton inconnu, mal formé : 404 (et jamais de redirection vers la connexion)
        self.assertEqual(anon.get("/agenda/flux/" + "x" * 43 + ".ics").status_code, 404)
        self.assertEqual(anon.get("/agenda/flux/court.ics").status_code, 404)

        # Nouvelle adresse : l'ancienne ne marche plus
        c.post("/agenda/flux/regenerer", data={"_csrf": csrf})
        token2 = db.session.get(AgendaSettings, self.user.id).feed_token
        self.assertNotEqual(token, token2)
        self.assertEqual(anon.get(f"/agenda/flux/{token}.ics").status_code, 404)
        self.assertEqual(anon.get(f"/agenda/flux/{token2}.ics").status_code, 200)

        # Compte désactivé : 404 ; abonnement coupé : 404
        self.user.active = False
        db.session.commit()
        self.assertEqual(anon.get(f"/agenda/flux/{token2}.ics").status_code, 404)
        self.user.active = True
        db.session.commit()
        c, csrf = self.login()
        c.post("/agenda/flux/couper", data={"_csrf": csrf})
        self.assertIsNone(db.session.get(AgendaSettings, self.user.id).feed_token)
        self.assertEqual(anon.get(f"/agenda/flux/{token2}.ics").status_code, 404)

    def test_feed_is_read_only(self):
        token = A.regenerate_feed_token(self.user)
        anon = self.app.test_client()
        # Le jeton n'ouvre rien d'autre que le flux (ni l'agenda, ni l'API)
        self.assertEqual(anon.get("/agenda", headers={"X-API-Key": token}).status_code, 302)
        self.assertEqual(anon.get("/api/v1/me", headers={"X-API-Key": token}).status_code, 401)
        self.assertNotEqual(anon.post(f"/agenda/flux/{token}.ics").status_code, 200)  # lecture seulement

    def test_address_shown_for_the_phone(self):
        A.regenerate_feed_token(self.user)
        token = db.session.get(AgendaSettings, self.user.id).feed_token
        c, _ = self.login()
        with mock.patch("hangar.cli._lan_ip", return_value="192.168.1.20"):
            page = c.get("/agenda/imports").get_data(as_text=True)
        # Page ouverte sur le PC (127.0.0.1) : adresse du PC sur le réseau local, et avertissement (écoute locale)
        self.assertIn(f"http://192.168.1.20/agenda/flux/{token}.ics", page)
        self.assertIn("n’accepte que les connexions de cet ordinateur", page)
        # Hangar lancé pour le réseau : plus d'avertissement
        self.app.config["HANGAR_HOST"] = "0.0.0.0"
        with mock.patch("hangar.cli._lan_ip", return_value="192.168.1.20"):
            page = c.get("/agenda/imports").get_data(as_text=True)
        self.assertNotIn("n’accepte que les connexions de cet ordinateur", page)
        # Adresse publique réglée (e-mails automatiques) : c'est elle qui sert
        cfg = MailConfig.get()
        cfg.base_url = "https://hangar.exemple.fr"
        db.session.commit()
        page = c.get("/agenda/imports").get_data(as_text=True)
        self.assertIn(f"https://hangar.exemple.fr/agenda/flux/{token}.ics", page)

    def test_api_examples_use_working_header(self):
        self.user.role = "admin"
        db.session.commit()
        c, _ = self.login()
        page = c.get("/settings/tokens").get_data(as_text=True)
        self.assertNotIn("Authorization: Bearer", page)
        self.assertIn("X-API-Key: hgr_", page)


# ---------------------------------------------------------------------------
# Piste 76 : rappels en notifications push
# ---------------------------------------------------------------------------

class PushTests(Base):
    def subscribe(self, **kw):
        from hangar.qcm.models import AbonnementPush
        a = AbonnementPush(user_id=self.user.id, endpoint=kw.pop("endpoint", ENDPOINT), p256dh="cle", auth="auth",
                           heure=19, minute=0, agenda=kw.pop("agenda", True), **kw)
        db.session.add(a)
        db.session.commit()
        return a

    def test_reminder_sent_as_push_with_tag_and_ttl(self):
        self.subscribe()
        day = date.today() + timedelta(days=2)
        t = AgendaTask(user_id=self.user.id, title="Rendre le rapport", due_date=day, start_time=time(10, 0))
        db.session.add(t)
        db.session.flush()
        A.set_task_reminders(t, [15], "push", at=datetime.combine(date.today(), time(8)))
        db.session.commit()
        r = t.reminders[0]
        self.assertEqual(r.channel, "push")
        at = datetime.combine(day, time(9, 46))
        with mock.patch("pywebpush.webpush") as wp:
            self.assertEqual(A.process_reminders(at), 1)
        self.assertEqual(wp.call_count, 1)
        kw = wp.call_args.kwargs
        data = json.loads(kw["data"])
        self.assertEqual(data["tag"], f"agenda-rappel-{r.id}")
        self.assertIn("Rendre le rapport", data["titre"])
        self.assertTrue(data["url"].startswith("/agenda/tasks"))
        # Valable jusqu'à la fin de la tâche (11 h), pas 12 h
        self.assertLessEqual(kw["ttl"], 75 * 60)
        self.assertGreaterEqual(kw["ttl"], 60)
        # Notification interne toujours créée, pas d'e-mail
        self.assertEqual(Notification.query.filter_by(user_id=self.user.id, kind="reminder").count(), 1)
        self.assertEqual(EmailMessage.query.count(), 0)
        self.assertEqual(db.session.get(AgendaReminder, r.id).status, "sent")

    def test_expired_device_forgotten_and_others_still_served(self):
        from pywebpush import WebPushException
        from hangar.qcm.models import AbonnementPush
        self.subscribe()
        self.subscribe(endpoint="https://updates.push.services.mozilla.com/wpush/v2/autre")
        self.subscribe(endpoint="https://example.org/pas-un-service")  # adresse non autorisée : oubliée sans appel
        ev = AgendaEvent(user_id=self.user.id, uid="x1", title="Maths", start=datetime.now() + timedelta(hours=1),
                         end=datetime.now() + timedelta(hours=3), location="B12")
        db.session.add(ev)
        db.session.flush()
        A.add_event_reminder(ev, 15, "push")
        db.session.commit()

        calls = []

        def fake(**kw):
            calls.append(kw["subscription_info"]["endpoint"])
            if "fcm" in kw["subscription_info"]["endpoint"]:
                raise WebPushException("parti", response=mock.Mock(status_code=410))

        with mock.patch("pywebpush.webpush", side_effect=fake):
            self.assertEqual(A.process_reminders(datetime.now() + timedelta(minutes=50)), 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual([a.endpoint for a in AbonnementPush.query.all()],
                         ["https://updates.push.services.mozilla.com/wpush/v2/autre"])
        note = Notification.query.filter_by(kind="reminder").one()
        self.assertIn("Maths", note.message)

    def test_push_is_a_third_channel_everywhere(self):
        ev = AgendaEvent(user_id=self.user.id, uid="x2", title="Maths", start=datetime.now() + timedelta(days=2),
                         end=datetime.now() + timedelta(days=2, hours=2))
        db.session.add(ev)
        db.session.commit()
        self.assertEqual(A.add_event_reminder(ev, 60, "push").channel, "push")
        self.assertEqual(A.add_event_reminder(ev, 30, "n'importe quoi").channel, "email")  # valeur vérifiée
        db.session.commit()
        c, csrf = self.login()
        c.post("/agenda/settings", data={"_csrf": csrf, "reminder_channel": "push", "daily_mode": "off"})
        self.assertEqual(db.session.get(AgendaSettings, self.user.id).reminder_channel, "push")
        page = c.get("/agenda/settings").get_data(as_text=True)
        self.assertIn('value="push" selected', page)
        self.assertIn("rappel-agenda", page)  # bloc d'abonnement de cet appareil
        self.assertIn("qcm/rappel_agenda.js", page)
        # Formulaire d'une séance : le moyen « push » est accepté tel quel
        r = c.post(f"/agenda/events/{ev.id}/reminders", data={"_csrf": csrf, "offset": "1440", "channel": "push"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(AgendaReminder.query.filter_by(event_id=ev.id, offset_minutes=1440).one().channel, "push")

    def test_agenda_subscription_separate_from_qcm_daily_reminder(self):
        from hangar.qcm import rappels
        from hangar.qcm.models import AbonnementPush
        c, csrf = self.login()
        sub = {"endpoint": ENDPOINT, "keys": {"p256dh": "BPcle", "auth": "auth"}}
        h = {"X-CSRF-Token": csrf, "X-Requested-With": "fetch"}
        r = c.post("/api/push/abonner/", json={"subscription": sub, "quoi": "agenda"}, headers=h)
        self.assertEqual(r.status_code, 200)
        a = AbonnementPush.query.one()
        self.assertEqual((a.agenda, a.quotidien), (True, False))
        # Le rappel quotidien des QCM ne part pas vers un appareil abonné aux seuls rappels de l'agenda
        a.heure, a.minute = 0, 0
        db.session.commit()
        with mock.patch("pywebpush.webpush") as wp:
            rappels.envoyer_rappels_dus(datetime.now().replace(hour=12))
        self.assertEqual(wp.call_count, 0)
        # Activer ensuite le rappel QCM sur le même appareil, puis le couper : l'agenda reste
        c.post("/api/push/abonner/", json={"subscription": sub, "heure": 19, "minute": 0}, headers=h)
        self.assertTrue(AbonnementPush.query.one().quotidien)
        r = c.post("/api/push/desabonner/", json={"endpoint": ENDPOINT}, headers=h)
        self.assertTrue(r.get_json()["garde"])
        a = AbonnementPush.query.one()
        self.assertEqual((a.agenda, a.quotidien), (True, False))
        # Couper aussi l'agenda : l'appareil est oublié
        r = c.post("/api/push/desabonner/", json={"endpoint": ENDPOINT, "quoi": "agenda"}, headers=h)
        self.assertFalse(r.get_json().get("garde"))
        self.assertEqual(AbonnementPush.query.count(), 0)

    def test_qcm_only_device_gets_no_agenda_push(self):
        self.subscribe(agenda=False)  # abonné au seul rappel quotidien des QCM
        ev = AgendaEvent(user_id=self.user.id, uid="x3", title="Maths", start=datetime.now() + timedelta(hours=1),
                         end=datetime.now() + timedelta(hours=3))
        db.session.add(ev)
        db.session.flush()
        A.add_event_reminder(ev, 15, "push")
        db.session.commit()
        with mock.patch("pywebpush.webpush") as wp:
            self.assertEqual(A.process_reminders(datetime.now() + timedelta(minutes=50)), 1)
        self.assertEqual(wp.call_count, 0)
        self.assertEqual(Notification.query.filter_by(kind="reminder").count(), 1)  # la notification interne reste

    def test_push_test_endpoint(self):
        self.subscribe()
        c, csrf = self.login()
        with mock.patch("pywebpush.webpush") as wp:
            r = c.post("/agenda/push/test", json={"endpoint": ENDPOINT},
                       headers={"X-CSRF-Token": csrf, "X-Requested-With": "fetch"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(json.loads(wp.call_args.kwargs["data"])["tag"], "agenda-test")

    def test_sw_uses_tag_from_message(self):
        c, _ = self.login()
        js = c.get("/qcm/sw.js").get_data(as_text=True)
        self.assertIn("tag: d.tag ||", js)


# ---------------------------------------------------------------------------
# Piste 79 : tickets et fins de sprint dans l'agenda
# ---------------------------------------------------------------------------

class TeamTests(Base):
    def setUp(self):
        super().setUp()
        p = Project(name="Projet pluri", identifier="PP", lead_id=self.user.id)
        db.session.add(p)
        db.session.flush()
        db.session.add(ProjectMember(project_id=p.id, user_id=self.user.id, role="member"))
        st = State(project_id=p.id, name="À faire", group="unstarted", color="#3f76ff")
        db.session.add(st)
        db.session.flush()
        self.p, self.st = p, st
        self.today = date.today()
        self.issue = Issue(project_id=p.id, sequence_id=101, title="Rédiger la note DPO", state_id=st.id,
                           target_date=self.today, created_by_id=self.user.id)
        self.issue.assignees.append(self.user)
        db.session.add(self.issue)
        self.cycle = Cycle(project_id=p.id, name="Sprint 4", start_date=self.today - timedelta(days=7),
                           end_date=self.today + timedelta(days=1))
        db.session.add(self.cycle)
        db.session.commit()

    def test_week_view_shows_tickets_and_sprint_end(self):
        s = A.settings_of(self.user)
        w = A.week_view(self.user, self.today, s)
        tickets = [b for b in w["bars"] if b["type"] == "ticket"]
        kinds = sorted(b["obj"].kind for b in tickets)
        self.assertIn("ticket", kinds)
        if A.ics.week_start(self.today + timedelta(days=1)) == A.ics.week_start(self.today):
            self.assertIn("sprint", kinds)

    def test_pages_render_with_tickets(self):
        c, _ = self.login()
        for url in ("/", "/agenda/calendar?view=week", "/agenda/calendar?view=month", "/agenda/calendar?view=list"):
            r = c.get(url)
            self.assertEqual(r.status_code, 200, url)
            page = r.get_data(as_text=True)
            self.assertIn("Rédiger la note DPO", page, url)
            self.assertIn('data-title="PP-101 · Rédiger la note DPO"', page, url)  # « Bloquer un créneau »
        # Fiche du ticket : « Bloquer un créneau »
        page = c.get(f"/projects/{self.p.id}/issues/101").get_data(as_text=True)
        self.assertIn("Bloquer un créneau", page)

    def test_weekend_ticket_shows_weekend(self):
        saturday = A.ics.week_start(self.today) + timedelta(days=5)
        self.issue.target_date = saturday
        db.session.commit()
        w = A.week_view(self.user, saturday, A.settings_of(self.user))
        self.assertEqual(w["ndays"], 7)

    def test_closed_ticket_and_other_people_not_shown(self):
        other = User(username="autre", email="autre@hangar.local", role="member", first_name="Autre")
        other.set_password("secret1")
        db.session.add(other)
        db.session.flush()
        i2 = Issue(project_id=self.p.id, sequence_id=102, title="Ticket d'un autre", state_id=self.st.id,
                   target_date=self.today, created_by_id=self.user.id)
        i2.assignees.append(other)
        done = State(project_id=self.p.id, name="Fait", group="completed", color="#16a34a")
        db.session.add_all([i2, done])
        db.session.flush()
        i3 = Issue(project_id=self.p.id, sequence_id=103, title="Ticket fini", state_id=done.id,
                   target_date=self.today, created_by_id=self.user.id)
        i3.assignees.append(self.user)
        db.session.add(i3)
        db.session.commit()
        titles = [x.title for x in A.team_items(self.user, self.today, self.today)]
        self.assertIn("Rédiger la note DPO", titles)
        self.assertNotIn("Ticket d'un autre", titles)
        self.assertNotIn("Ticket fini", titles)

    def test_export_and_feed_include_tickets(self):
        text = A.export_ics(self.user)
        self.assertIn(f"UID:ticket-{self.issue.id}@hangar", text)
        self.assertIn(f"UID:sprint-{self.cycle.id}@hangar", text)
        self.assertIn("SUMMARY:PP-101 · Rédiger la note DPO", text)
        self.assertNotIn("ticket-", A.export_ics(self.user, include_tasks=False, include_team=False))
        c, _ = self.login()
        self.assertIn("ticket-", c.get("/agenda/export.ics?what=all").get_data(as_text=True))
        self.assertNotIn("ticket-", c.get("/agenda/export.ics?what=events").get_data(as_text=True))
        token = A.regenerate_feed_token(self.user)
        self.assertIn("ticket-", self.app.test_client().get(f"/agenda/flux/{token}.ics").get_data(as_text=True))


# ---------------------------------------------------------------------------
# Piste 81 : emploi du temps récupéré tout seul
# ---------------------------------------------------------------------------

class SourceTests(Base):
    def setUp(self):
        super().setUp()
        self.monday = monday_after(date.today())
        self.at = datetime.combine(self.monday - timedelta(days=2), time(9))  # samedi d'avant
        self.folder = os.path.join(self.tmp, "Telechargements")
        os.makedirs(self.folder)

    def drop(self, name, raw, age=0):
        path = os.path.join(self.folder, name)
        with open(path, "wb") as fh:
            fh.write(raw)
        t = _time.time() - 1000 + age
        os.utime(path, (t, t))
        return path

    def configure(self, kind, where):
        s = A.settings_of(self.user)
        s.source_kind, s.source_path = kind, where
        db.session.commit()

    def active(self):
        return AgendaEvent.query.filter_by(user_id=self.user.id, status="active").count()

    def test_folder_imports_once_and_ignores_unchanged(self):
        self.configure("folder", self.folder)
        self.assertEqual(A.check_source(self.user, self.at), "same")  # dossier vide : rien
        self.drop("planning.ics", week_file(self.monday))
        self.assertEqual(A.check_source(self.user, self.at), "imported")
        self.assertEqual((self.active(), AgendaImport.query.count()), (4, 1))
        # Même fichier : rien n'est relu ni importé
        self.assertEqual(A.check_source(self.user, self.at + timedelta(seconds=30)), "same")
        # Même planning re-téléchargé (« planning (1).ics », autre horodatage) : aucune ligne d'historique de plus
        self.drop("planning (1).ics", week_file(self.monday, stamp="20261115T120000Z"), age=10)
        self.assertEqual(A.check_source(self.user, self.at + timedelta(minutes=1)), "same")
        self.assertEqual(AgendaImport.query.count(), 1)
        # Changement de salle : nouvel import, notification « Emploi du temps mis à jour »
        self.drop("planning (2).ics", week_file(self.monday, room_math="B12"), age=20)
        self.assertEqual(A.check_source(self.user, self.at + timedelta(minutes=2)), "imported")
        imp = A.latest_import(self.user)
        self.assertEqual((imp.modified, imp.removed, AgendaImport.query.count()), (1, 0, 2))
        note = Notification.query.filter_by(user_id=self.user.id, kind="update").order_by(Notification.id.desc()).first()
        self.assertEqual(note.message, "Emploi du temps mis à jour : 1 séance modifiée.")
        s = db.session.get(AgendaSettings, self.user.id)
        self.assertIsNotNone(s.source_checked_at)
        self.assertEqual(s.source_error, "")

    def test_invitation_in_folder_removes_nothing(self):
        self.configure("folder", self.folder)
        self.drop("planning.ics", week_file(self.monday))
        A.check_source(self.user, self.at)
        # Invitation Outlook (une réunion le mardi) tombée dans le dossier
        invite = make_ics([("abc@outlook.com", datetime.combine(self.monday + timedelta(days=1), time(14)),
                            datetime.combine(self.monday + timedelta(days=1), time(15)), "Réunion de promo", "")],
                          method="REQUEST")
        self.drop("invite.ics", invite, age=10)
        self.assertEqual(A.check_source(self.user, self.at + timedelta(minutes=1)), "ignored")
        self.assertEqual(self.active(), 4)
        self.assertEqual(AgendaImport.query.count(), 1)
        self.assertIn("invitation", Notification.query.filter_by(kind="update").order_by(Notification.id.desc())
                      .first().message)
        # Un .ics étranger (billet de train, PUBLISH, rien de connu) : écarté aussi
        train = make_ics([("sncf-1", datetime.combine(self.monday + timedelta(days=2), time(7)),
                           datetime.combine(self.monday + timedelta(days=2), time(9)), "Train Paris - Brest", "")])
        self.drop("billet.ics", train, age=20)
        self.assertEqual(A.check_source(self.user, self.at + timedelta(minutes=2)), "ignored")
        self.assertEqual(self.active(), 4)
        # Le planning de la semaine suivante (UID tous nouveaux, mais types de cours de l'école) passe
        nxt = week_file(self.monday + timedelta(days=7)).replace(b"UID:100", b"UID:200")
        self.drop("planning (1).ics", nxt, age=30)
        self.assertEqual(A.check_source(self.user, self.at + timedelta(minutes=3)), "imported")
        self.assertEqual(self.active(), 8)

    def test_manual_invitation_import_removes_nothing(self):
        A.import_calendar(self.user, week_file(self.monday), "planning.ics", at=self.at)
        invite = make_ics([("abc@outlook.com", datetime.combine(self.monday + timedelta(days=1), time(14)),
                            datetime.combine(self.monday + timedelta(days=1), time(15)), "Réunion de promo", "")],
                          method="REQUEST")
        imp = A.import_calendar(self.user, invite, "invite.ics", "weeks", at=self.at)
        self.assertEqual((imp.removed, imp.added, imp.mode), (0, 1, "merge"))
        self.assertIn("invitation", imp.warnings)
        self.assertEqual(self.active(), 5)

    def test_url_source_with_local_server(self):
        body = {"raw": week_file(self.monday)}

        class H(http.server.BaseHTTPRequestHandler):
            hits = 0

            def do_GET(self):  # noqa: N802
                H.hits += 1
                self.send_response(200)
                self.send_header("Content-Type", "text/calendar")
                self.end_headers()
                self.wfile.write(body["raw"])

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{srv.server_address[1]}/planning.ics"
            self.configure("url", url)
            self.assertEqual(A.check_source(self.user, self.at), "imported")
            self.assertEqual(self.active(), 4)
            # Relue toutes les 3 h seulement
            self.assertEqual(A.check_source(self.user, self.at + timedelta(hours=1)), "wait")
            self.assertEqual(H.hits, 1)
            body["raw"] = week_file(self.monday, room_math="B12", stamp="20261116T000000Z")
            self.assertEqual(A.check_sources(self.at + timedelta(hours=3, minutes=1)), 1)
            self.assertEqual(AgendaImport.query.count(), 2)
            # Serveur injoignable : erreur notée (une seule notification), rien de cassé
            srv.shutdown()
            srv.server_close()
            self.assertEqual(A.check_source(self.user, self.at + timedelta(hours=7), force=True), "error")
            self.assertEqual(A.check_source(self.user, self.at + timedelta(hours=8), force=True), "error")
            self.assertEqual(Notification.query.filter(Notification.message.like("%ne répond pas%")).count(), 1)
            self.assertEqual(self.active(), 4)
        finally:
            srv.server_close()

    def test_settings_reserved_to_admins(self):
        c, csrf = self.login()
        r = c.post("/agenda/source", data={"_csrf": csrf, "source_kind": "folder", "source_path": self.folder})
        self.assertEqual(r.status_code, 403)
        self.assertFalse(db.session.get(AgendaSettings, self.user.id).source_kind)
        self.assertIn("Seul un administrateur", c.get("/agenda/imports").get_data(as_text=True))
        self.admin()
        c, csrf = self.login("chef")
        self.drop("planning.ics", week_file(self.monday))
        r = c.post("/agenda/source", data={"_csrf": csrf, "source_kind": "folder", "source_path": self.folder})
        self.assertEqual(r.status_code, 302)
        chef = User.query.filter_by(username="chef").one()
        s = db.session.get(AgendaSettings, chef.id)
        self.assertEqual((s.source_kind, s.source_path), ("folder", self.folder))
        self.assertEqual(AgendaImport.query.filter_by(user_id=chef.id).count(), 1)  # lu tout de suite
        r = c.post("/agenda/source", data={"_csrf": csrf, "source_kind": "url", "source_path": "file:///etc/passwd"})
        self.assertEqual(db.session.get(AgendaSettings, chef.id).source_kind, "folder")  # refusé

    def test_nudge_follows_the_source(self):
        s = A.settings_of(self.user)
        A.import_calendar(self.user, week_file(self.monday - timedelta(days=21)), "vieux.ics",
                          at=datetime.combine(self.monday - timedelta(days=23), time(9)))
        at = datetime.combine(self.monday - timedelta(days=3), time(9))
        self.assertTrue(A.dashboard(self.user, at)["nudge"])  # import manuel ancien : rappel
        self.configure("folder", self.folder)
        s = db.session.get(AgendaSettings, self.user.id)
        s.source_checked_at, s.source_changed_at = at - timedelta(hours=1), at - timedelta(days=2)
        db.session.commit()
        self.assertFalse(A.dashboard(self.user, at)["nudge"])  # source lue récemment, nouveau fichier récent
        s.source_checked_at = at - timedelta(days=7)
        db.session.commit()
        self.assertTrue(A.dashboard(self.user, at)["nudge"])  # source muette depuis une semaine


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Piste 81 (relecture) : planning complet en METHOD:REQUEST, fichier en cours de téléchargement
# ---------------------------------------------------------------------------

class SourceReviewTests(Base):
    drop, configure, active = SourceTests.drop, SourceTests.configure, SourceTests.active

    def setUp(self):
        Base.setUp(self)
        self.monday = monday_after(date.today())
        self.at = datetime.combine(self.monday - timedelta(days=2), time(9))
        self.folder = os.path.join(self.tmp, "Telechargements")
        os.makedirs(self.folder)

    def full_request_week(self, without=()):
        """Planning complet de la semaine exporté en METHOD:REQUEST (comme certains logiciels de planning)."""
        raw = week_file(self.monday).replace(b"METHOD:PUBLISH", b"METHOD:REQUEST")
        cal = raw.decode()
        for uid in without:
            cal = re.sub(r"BEGIN:VEVENT\r\n(?:(?!END:VEVENT).)*?UID:%s\r\n.*?END:VEVENT\r\n" % uid, "", cal, flags=re.S)
        return cal.encode()

    def test_full_planning_in_request_still_removes(self):
        A.import_calendar(self.user, week_file(self.monday), "planning.ics", at=self.at)
        raw = self.full_request_week(without=("1004",))
        self.assertIn(b"METHOD:REQUEST", raw)
        self.assertNotIn(b"UID:1004", raw)
        imp = A.import_calendar(self.user, raw, "planning.ics", "weeks", at=self.at)
        self.assertEqual((imp.mode, imp.removed), ("weeks", 1))
        self.assertNotIn("invitation", imp.warnings)
        self.assertEqual(self.active(), 3)

    def test_full_planning_in_request_from_folder(self):
        self.configure("folder", self.folder)
        self.drop("planning.ics", week_file(self.monday))
        self.assertEqual(A.check_source(self.user, self.at), "imported")
        self.drop("planning (1).ics", self.full_request_week(without=("1004",)), age=10)
        self.assertEqual(A.check_source(self.user, self.at + timedelta(minutes=1)), "imported")
        self.assertEqual(self.active(), 3)

    def test_real_invitations_still_recognised(self):
        def ev(uid, day, summary):
            d = datetime.combine(self.monday + timedelta(days=day), time(14))
            return (uid, d, d + timedelta(hours=1), summary, "")
        few = ics.parse(make_ics([ev("a", 1, "Réunion de promo")], method="REQUEST"))
        many = ics.parse(make_ics([ev(f"r{i}", i, "Point d’équipe") for i in range(4)], method="REQUEST"))
        two_courses = ics.parse(make_ics([ev("c1", 1, "Droit 2 CM"), ev("c2", 2, "Droit 3 CM")], method="CANCEL"))
        published = ics.parse(make_ics([ev("p", 1, "Réunion de promo")], method="PUBLISH"))
        self.assertEqual([A.is_invitation(c) for c in (few, many, two_courses, published)],
                         [True, True, True, False])

    def test_empty_file_being_downloaded_is_skipped_silently(self):
        self.configure("folder", self.folder)
        path = self.drop("planning.ics", b"")  # Firefox : fichier final vide pendant le téléchargement
        self.assertEqual(A.check_source(self.user, self.at), "busy")
        s = db.session.get(AgendaSettings, self.user.id)
        self.assertEqual((s.source_error, s.source_seen), ("", ""))
        self.assertEqual(Notification.query.filter_by(user_id=self.user.id).count(), 0)
        # Contenu en cours d'écriture (modifié à l'instant) : toujours rien
        with open(path, "wb") as fh:
            fh.write(week_file(self.monday))
        self.assertEqual(A.check_source(self.user, self.at + timedelta(seconds=1)), "busy")
        # « .part » à côté : encore en cours
        t = _time.time() - 100
        os.utime(path, (t, t))
        open(path + ".part", "wb").close()
        self.assertEqual(A.check_source(self.user, self.at + timedelta(seconds=2)), "busy")
        os.remove(path + ".part")
        # Téléchargement fini : importé au passage suivant, sans notification d'erreur
        self.assertEqual(A.check_source(self.user, self.at + timedelta(seconds=30)), "imported")
        self.assertEqual(self.active(), 4)
        msgs = [n.message for n in Notification.query.filter_by(user_id=self.user.id)]
        self.assertFalse([m for m in msgs if "lisible" in m], msgs)

    def test_unreadable_file_message_is_plain(self):
        self.configure("folder", self.folder)
        self.drop("planning.ics", b"ceci n'est pas un calendrier")
        self.assertEqual(A.check_source(self.user, self.at), "error")
        s = db.session.get(AgendaSettings, self.user.id)
        self.assertEqual(s.source_error, "le fichier planning.ics n’est pas un emploi du temps lisible.")
        self.assertNotIn("icalendar", s.source_error.lower())

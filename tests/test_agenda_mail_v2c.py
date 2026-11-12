"""Tests du groupe « agenda-mail » (vague 2c) :

- piste 18 : « Aucun e-mail » du profil arrête le résumé quotidien, les rappels par e-mail et l'e-mail après
  un import ; pas de résumé pour un compte qui n'a jamais ouvert Hangar ; mention dans le profil ;
- piste 26 : le tableau de bord montre « Mes tickets » (en retard, à rendre dans les 7 jours) et la pastille
  de l'icône « Travail » ;
- piste 31 : la date d'examen des QCM suit un contrôle déplacé / retiré (et l'annulation de l'import),
  sans toucher à une date saisie à la main ; préparation automatique ; « niveau 2  CO_ORAL » ;
- piste 39 : résumé du soir rattrapé le lendemain, résumé du matin périmé le soir, « Reste à faire
  aujourd'hui », récapitulatif et bilan hebdomadaires rattrapés dans la semaine ;
- piste 54 : exports .ics conformes (RFC 5545) et relus par le lecteur du projet.

Lancer : uv run python -m unittest tests.test_agenda_mail_v2c -v
"""
import json
import re
import shutil
import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path

from hangar import agenda as A
from hangar import create_app, digest, ics, mailer
from hangar.extensions import db
from hangar.models import (AgendaEvent, AgendaImport, AgendaReminder, AgendaSettings, AgendaTask, Cycle,
                           EmailMessage, Issue, MailConfig, Notification, Project, ProjectMember, State, User)

JEU = date(2026, 11, 12)  # un jeudi


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hangar-v2c-agenda-mail-")
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

    # -- aides --
    def login(self, username="moi"):
        from flask import g
        g.pop("_login_user", None)
        c = self.app.test_client()
        token = re.search(r'name="_csrf" value="([^"]+)"', c.get("/login").get_data(as_text=True)).group(1)
        r = c.post("/login", data={"login": username, "password": "secret1", "_csrf": token})
        self.assertEqual(r.status_code, 302)
        return c, token

    def project(self, user=None):
        user = user or self.user
        p = Project(name="Projet pluri", identifier="PP", lead_id=user.id)
        db.session.add(p)
        db.session.flush()
        db.session.add(ProjectMember(project_id=p.id, user_id=user.id, role="member"))
        st = State(project_id=p.id, name="À faire", group="unstarted", color="#3f76ff")
        db.session.add(st)
        db.session.commit()
        self._seq = 100  # numéros hors de ceux que donne project.next_sequence()
        return p, st

    def issue(self, p, st, title, due, start=None, user=None):
        self._seq += 1
        i = Issue(project_id=p.id, sequence_id=self._seq, title=title, state_id=st.id, target_date=due,
                  start_date=start, created_by_id=self.user.id)
        i.assignees.append(user or self.user)
        db.session.add(i)
        db.session.commit()
        return i

    def settings(self, **kw):
        s = A.settings_of(self.user)
        for k, v in kw.items():
            setattr(s, k, v)
        db.session.commit()
        return s

    def agenda_mails(self):
        return EmailMessage.query.filter_by(kind="agenda").order_by(EmailMessage.id).all()


# ---------------------------------------------------------------------------
# Piste 18 : « Aucun e-mail » respecté partout
# ---------------------------------------------------------------------------

class AucunEmailTests(Base):
    def test_aucun_email_arrete_le_resume_quotidien(self):
        self.settings()  # préférences créées en ouvrant l'agenda : résumé le matin à 7 h
        db.session.add(AgendaTask(user_id=self.user.id, title="Réviser", due_date=JEU))
        self.user.email_prefs = "off"
        db.session.commit()
        self.assertEqual(A.send_daily_programs(datetime(2026, 11, 12, 7, 5)), 0)
        self.assertEqual(self.agenda_mails(), [])
        # Repasser sur « À chaque notification » : le résumé repart
        self.user.email_prefs = "instant"
        db.session.commit()
        self.assertEqual(A.send_daily_programs(datetime(2026, 11, 12, 7, 6)), 1)

    def test_compte_jamais_ouvert_ne_recoit_pas_de_resume(self):
        db.session.add(AgendaTask(user_id=self.user.id, title="Réviser", due_date=JEU))
        db.session.commit()
        self.assertEqual(A.send_daily_programs(datetime(2026, 11, 12, 7, 5)), 0)
        self.assertIsNone(db.session.get(AgendaSettings, self.user.id))  # aucun réglage créé à son insu
        self.assertFalse(A.digest_enabled(self.user))

    def test_aucun_email_arrete_les_rappels_par_email(self):
        self.settings()
        t = AgendaTask(user_id=self.user.id, title="Rendre le rapport", due_date=JEU, start_time=time(14, 0))
        db.session.add(t)
        db.session.flush()
        A.set_task_reminders(t, [60], "email", at=datetime(2026, 11, 12, 8, 0))
        self.user.email_prefs = "off"
        db.session.commit()
        self.assertEqual(A.process_reminders(datetime(2026, 11, 12, 13, 1)), 1)
        self.assertEqual(AgendaReminder.query.one().status, "sent")
        self.assertEqual(Notification.query.filter_by(kind="reminder").count(), 1)  # dans l'appli : oui
        self.assertEqual(EmailMessage.query.count(), 0)                               # par e-mail : non

    def test_aucun_email_arrete_l_email_apres_import(self):
        self.settings(import_email=True)
        cal = ("BEGIN:VCALENDAR\nBEGIN:VEVENT\nDTSTART:20261113T080000\nDTEND:20261113T100000\nUID:a\n"
               "SUMMARY:Cours 1 CM\nEND:VEVENT\nEND:VCALENDAR\n")
        A.import_calendar(self.user, cal, at=datetime(2026, 11, 10, 8, 0))
        before = len(self.agenda_mails())
        cal2 = cal.replace("T080000", "T090000")
        self.user.email_prefs = "off"
        db.session.commit()
        imp = A.import_calendar(self.user, cal2, at=datetime(2026, 11, 10, 9, 0))
        self.assertEqual(imp.modified, 1)
        self.assertEqual(len(self.agenda_mails()), before)
        self.assertFalse(mailer.accepte_emails(self.user))

    def test_profil_signale_le_remplacement_par_le_resume(self):
        c, _ = self.login()
        page = c.get("/settings/profile").get_data(as_text=True)
        self.assertIn("Remplacé par le résumé quotidien", page)       # réglages créés en ouvrant une page
        self.settings(daily_mode="off")
        page = c.get("/settings/profile").get_data(as_text=True)
        self.assertNotIn("Remplacé par le résumé quotidien", page)

    def test_rappel_d_echeance_quand_le_resume_est_coupe(self):
        p, st = self.project()
        self.issue(p, st, "Fiche de cadrage", JEU)
        cfg = MailConfig.get()
        self.settings()
        self.assertEqual(mailer.send_reminders(cfg, JEU), 0)  # le résumé quotidien s'en charge
        self.settings(daily_mode="off")
        self.assertEqual(mailer.send_reminders(cfg, JEU), 1)
        self.user.email_prefs = "off"
        db.session.commit()
        self.assertEqual(mailer.send_reminders(cfg, JEU), 0)


# ---------------------------------------------------------------------------
# Piste 39 : rattrapage et péremption des envois
# ---------------------------------------------------------------------------

class RattrapageTests(Base):
    def setUp(self):
        super().setUp()
        for d in range(0, 6):  # une tâche chaque jour : il y a toujours quelque chose à résumer
            db.session.add(AgendaTask(user_id=self.user.id, title=f"Tâche du {11 + d}", due_date=date(2026, 11, 11 + d)))
        db.session.commit()

    def test_soir_rattrape_le_lendemain_matin(self):
        self.settings(daily_mode="evening", daily_hour=19)
        self.assertEqual(A.send_daily_programs(datetime(2026, 11, 12, 18, 30)), 0)  # trop tôt, rien à rattraper
        # PC éteint à 18 h 30, rouvert le lendemain à 7 h 30 : le résumé du jour part tout de suite
        self.assertEqual(A.send_daily_programs(datetime(2026, 11, 13, 7, 30)), 1)
        self.assertIn("13 nov.", self.agenda_mails()[-1].subject)
        self.assertIn("aujourd’hui", self.agenda_mails()[-1].body_html)
        self.assertEqual(A.send_daily_programs(datetime(2026, 11, 13, 12, 0)), 0)   # une seule fois
        self.assertEqual(A.send_daily_programs(datetime(2026, 11, 13, 19, 0)), 1)   # puis celui du lendemain
        self.assertIn("14 nov.", self.agenda_mails()[-1].subject)
        self.assertEqual(A.send_daily_programs(datetime(2026, 11, 14, 7, 30)), 0)   # déjà couvert hier soir
        # Ouvert seulement après 18 h : pas de rattrapage d'une journée finie, mais le résumé du soir part
        self.assertEqual(A.send_daily_programs(datetime(2026, 11, 15, 18, 10)), 0)
        self.assertEqual(A.send_daily_programs(datetime(2026, 11, 15, 19, 10)), 1)

    def test_matin_perime_le_soir(self):
        self.settings(daily_mode="morning", daily_hour=7)
        self.assertEqual(A.send_daily_programs(datetime(2026, 11, 12, 22, 40)), 0)  # journée finie
        self.assertEqual(A.send_daily_programs(datetime(2026, 11, 13, 6, 59)), 0)
        self.assertEqual(A.send_daily_programs(datetime(2026, 11, 13, 7, 5)), 1)
        self.assertEqual(A.send_daily_programs(datetime(2026, 11, 13, 17, 0)), 0)
        self.assertEqual(A.send_daily_programs(datetime(2026, 11, 14, 17, 30)), 1)  # encore utile à 17 h 30
        self.assertEqual(len(self.agenda_mails()), 2)

    def test_base_existante_sans_doublon(self):
        """Réglage enregistré par la version précédente (seul le jour d'envoi était connu)."""
        self.settings(daily_mode="evening", daily_hour=19, last_daily_date=date(2026, 11, 12))
        self.assertEqual(A.send_daily_programs(datetime(2026, 11, 13, 7, 30)), 0)  # le 13 était déjà couvert
        self.assertEqual(A.send_daily_programs(datetime(2026, 11, 13, 19, 30)), 1)
        s = A.settings_of(self.user)
        self.assertEqual((s.last_daily_date, s.last_daily_day), (date(2026, 11, 13), date(2026, 11, 14)))
        self.settings(daily_mode="morning", daily_hour=7, last_daily_day=None, last_daily_date=date(2026, 11, 14))
        self.assertEqual(A.send_daily_programs(datetime(2026, 11, 14, 9, 0)), 0)

    def test_soir_reste_a_faire_aujourd_hui(self):
        p, st = self.project()
        self.issue(p, st, "Ticket du jour", JEU)
        objet, html, sections = digest.construire(self.user, JEU + timedelta(days=1), datetime(2026, 11, 12, 19, 0))
        self.assertIn("Reste à faire aujourd’hui", html)
        self.assertIn("Tâche du 12", html)          # tâche due aujourd'hui et pas faite
        self.assertIn("Reste à rendre aujourd’hui", html)
        self.assertIn("Ticket du jour", html)
        # Le matin, rien de tout ça (la tâche du jour est dans la liste du jour)
        objet, html, sections = digest.construire(self.user, JEU, datetime(2026, 11, 12, 7, 0))
        self.assertNotIn("Reste à faire aujourd’hui", html)
        self.assertNotIn("Reste à rendre aujourd’hui", html)

    def _post_settings(self, at, mode, hour):
        from unittest import mock
        c, token = self.login()
        with mock.patch("hangar.views.agenda.now", return_value=at):
            r = c.post("/agenda/settings", data={"_csrf": token, "daily_mode": mode, "daily_hour": str(hour),
                                                 "contact_email": "moi@exemple.fr", "default_reminder": "-1",
                                                 "reminder_channel": "email"})
        self.assertEqual(r.status_code, 302)
        db.session.expire_all()

    def test_activation_apres_l_heure_meme_avec_un_historique(self):
        """Réactiver le résumé du matin après l'heure : rien tout de suite, même si un résumé est déjà parti
        avec cette version (last_daily_day rempli)."""
        self.settings(daily_mode="morning", daily_hour=7)
        A.send_daily_programs(datetime(2026, 11, 2, 7, 5))
        self.assertEqual(A.settings_of(self.user).last_daily_day, date(2026, 11, 2))
        self.settings(daily_mode="off")
        self._post_settings(datetime(2026, 11, 12, 9, 0), "morning", 0)
        self.assertEqual(A.send_daily_programs(datetime(2026, 11, 12, 10, 0)), 0)
        self.assertEqual(A.send_daily_programs(datetime(2026, 11, 13, 0, 30)), 1)  # premier envoi le lendemain
        # Mode soir activé à 20 h pour 19 h : rien ce soir, le résumé du surlendemain part demain soir
        self.settings(daily_mode="off")
        self._post_settings(datetime(2026, 11, 13, 20, 0), "evening", 19)
        self.assertEqual(A.send_daily_programs(datetime(2026, 11, 13, 20, 5)), 0)
        self.assertEqual(A.send_daily_programs(datetime(2026, 11, 14, 7, 0)), 0)
        self.assertEqual(A.send_daily_programs(datetime(2026, 11, 14, 19, 5)), 1)

    def test_changer_l_heure_ne_renvoie_pas_le_resume_du_jour(self):
        self.settings(daily_mode="morning", daily_hour=7)
        self.assertEqual(A.send_daily_programs(datetime(2026, 11, 12, 7, 5)), 1)
        self._post_settings(datetime(2026, 11, 12, 7, 30), "morning", 8)  # 8 h pas encore passée
        self.assertEqual(A.send_daily_programs(datetime(2026, 11, 12, 8, 5)), 0)  # déjà reçu aujourd'hui
        self.assertEqual(A.send_daily_programs(datetime(2026, 11, 13, 8, 5)), 1)

    def test_activation_avant_l_heure_envoie_le_jour_meme(self):
        self._post_settings(datetime(2026, 11, 12, 6, 0), "morning", 7)
        self.assertEqual(A.send_daily_programs(datetime(2026, 11, 12, 7, 5)), 1)

    def _cfg(self):
        cfg = MailConfig.get()
        cfg.enabled = True
        db.session.commit()
        return cfg

    def test_recapitulatif_et_bilan_rattrapes_dans_la_semaine(self):
        self._cfg()
        lun, mar = datetime(2026, 11, 16, 7, 0), datetime(2026, 11, 17, 8, 30)
        self.assertNotIn("digest", mailer.run_scheduled(at=lun))           # lundi 7 h : avant 8 h
        self.assertIn("digest", mailer.run_scheduled(at=mar))              # fermé lundi : rattrapé mardi
        self.assertNotIn("digest", mailer.run_scheduled(at=mar + timedelta(days=1)))  # une seule fois
        self.assertNotIn("bilan", mailer.run_scheduled(at=datetime(2026, 11, 20, 16, 0)))  # vendredi 16 h
        self.assertIn("bilan", mailer.run_scheduled(at=datetime(2026, 11, 21, 10, 0)))      # samedi : rattrapé
        self.assertNotIn("bilan", mailer.run_scheduled(at=datetime(2026, 11, 22, 10, 0)))
        # Semaine suivante : rien avant le lundi 8 h, puis de nouveau envoyé
        self.assertNotIn("digest", mailer.run_scheduled(at=datetime(2026, 11, 23, 7, 0)))
        self.assertIn("digest", mailer.run_scheduled(at=datetime(2026, 11, 23, 8, 0)))

    def test_weekly_due(self):
        self.assertFalse(mailer.weekly_due(datetime(2026, 11, 16, 7, 59), 0, 8, None))
        self.assertTrue(mailer.weekly_due(datetime(2026, 11, 16, 8, 0), 0, 8, date(2026, 11, 9)))
        self.assertFalse(mailer.weekly_due(datetime(2026, 11, 18, 8, 0), 0, 8, date(2026, 11, 16)))
        self.assertTrue(mailer.weekly_due(datetime(2026, 11, 22, 23, 0), 4, 17, date(2026, 11, 13)))


# ---------------------------------------------------------------------------
# Piste 26 : tickets sur le tableau de bord
# ---------------------------------------------------------------------------

class TableauDeBordTicketsTests(Base):
    def test_mes_tickets_et_pastille_travail(self):
        today = date.today()
        p, st = self.project()
        self.issue(p, st, "Ticket très en retard", today - timedelta(days=3))
        self.issue(p, st, "Ticket de la semaine", today + timedelta(days=4))
        self.issue(p, st, "Ticket lointain", today + timedelta(days=30))
        c, _ = self.login()
        page = c.get("/").get_data(as_text=True)
        self.assertIn("Mes tickets", page)
        self.assertIn("Ticket très en retard", page)
        self.assertIn("Ticket de la semaine", page)
        self.assertNotIn("Ticket lointain", page)
        self.assertIn("Tâches en retard", page)
        self.assertIn("Aucune tâche dans les 7 prochains jours.", page)
        self.assertNotIn("🎉", page)  # pas de « rien à faire » quand des tickets attendent
        self.assertIn("scope=assigned", page)
        rail = re.search(r'title="Travail">.*?</a>', page, re.S).group(0)
        self.assertIn('rail-badge danger', rail)
        self.assertIn(">1<", rail)
        self.assertEqual(digest.nb_tickets_dus(self.user, today), (1, 1))

    def test_bloc_masque_sans_projet(self):
        c, _ = self.login()
        page = c.get("/").get_data(as_text=True)
        self.assertNotIn("Mes tickets", page)
        self.assertIn("Aucune tâche dans les 7 prochains jours. 🎉", page)
        rail = re.search(r'title="Travail">.*?</a>', page, re.S).group(0)
        self.assertNotIn("rail-badge", rail)

    def test_regle_partagee(self):
        p, st = self.project()
        late = self.issue(p, st, "En retard", JEU - timedelta(days=1))
        today = self.issue(p, st, "Aujourd'hui", JEU)
        soon = self.issue(p, st, "Bientôt", JEU + timedelta(days=2))
        t = digest.tickets_a_echeance(self.user, JEU)
        self.assertEqual((t["retard"], t["du_jour"], t["bientot"], t["aujourdhui"]), ([late], [today], [soon], []))
        t = digest.tickets_a_echeance(self.user, JEU + timedelta(days=1), JEU)  # résumé du soir
        self.assertEqual((t["retard"], t["aujourdhui"]), ([late], [today]))
        autre = User(username="autre", email="autre@hangar.local", role="guest")  # ne voit aucun projet
        autre.set_password("secret1")
        db.session.add(autre)
        db.session.commit()
        self.assertIsNone(digest.tickets_a_echeance(autre, JEU))


# ---------------------------------------------------------------------------
# Piste 31 : date d'examen des QCM qui suit le planning
# ---------------------------------------------------------------------------

def exam_ics(day: date | None, extra: str = "") -> str:
    ev = ""
    if day is not None:
        ev = (f"BEGIN:VEVENT\nDTSTART:{day:%Y%m%d}T101500\nDTEND:{day:%Y%m%d}T121500\nUID:exam1\n"
              "SUMMARY:Techniques numériques 15 CO_ECRIT\nEND:VEVENT\n")
    return ("BEGIN:VCALENDAR\nX-WR-TIMEZONE:Europe/Paris\n" + ev
            + "BEGIN:VEVENT\nDTSTART:20261116T080000\nDTEND:20261116T100000\nUID:cours1\n"
              "SUMMARY:Techniques numériques 14 CM\nEND:VEVENT\n"
            + "BEGIN:VEVENT\nDTSTART:20261120T080000\nDTEND:20261120T100000\nUID:cours2\n"
              "SUMMARY:Anglais 3 TD\nEND:VEVENT\n" + extra + "END:VCALENDAR\n")


class DateExamenTests(Base):
    def setUp(self):
        super().setUp()
        vault = Path(self.app.config["WIKI_DIR"])
        qset = {"titre": "Ohm", "questions": [{"id": "q", "type": "QCM", "question": "U ?", "choix": ["RI", "R/I"],
                                               "reponses": [0]}]}
        for rel in ("QCM/TECHNIQUES_NUM/ohm.json", "QCM/TECHNIQUES_NUM/kirchhoff.json"):
            (vault / rel).parent.mkdir(parents=True, exist_ok=True)
            (vault / rel).write_text(json.dumps(qset), encoding="utf-8")
        self.at = datetime(2026, 11, 10, 8, 0)
        A.import_calendar(self.user, exam_ics(date(2026, 11, 19)), at=self.at)
        A.set_subject_link(self.user, "Techniques numériques", qcm_category="TECHNIQUES_NUM")
        db.session.commit()

    def decks(self):
        from hangar.qcm.models import QuizDeck
        return sorted(QuizDeck.query.all(), key=lambda d: d.set_id)

    def test_controle_avance_la_date_suit(self):
        ev = AgendaEvent.query.filter_by(uid="exam1").one()
        self.assertEqual(A.set_exam_objective(self.user, ev), 2)
        manual = self.decks()[1]
        manual.date_objectif = date(2026, 11, 25)  # date choisie à la main dans QCM › Planning
        db.session.commit()
        imp = A.import_calendar(self.user, exam_ics(date(2026, 11, 16)), at=self.at + timedelta(hours=1))
        self.assertEqual(AgendaEvent.query.filter_by(uid="exam1").one().start.date(), date(2026, 11, 16))
        d0, d1 = self.decks()
        self.assertEqual(d0.date_objectif, date(2026, 11, 16))   # suit le contrôle
        self.assertEqual(d1.date_objectif, date(2026, 11, 25))   # date manuelle intacte
        self.assertIn("contrôle déplacé du 19/11 au 16/11", imp.exam_notes)
        # Annuler l'import remet la date d'avant
        notes = A.undo_import(imp, at=self.at + timedelta(hours=2))
        self.assertEqual(self.decks()[0].date_objectif, date(2026, 11, 19))
        self.assertTrue(any("du 16/11 au 19/11" in n for n in notes))

    def test_controle_retire_la_date_est_effacee(self):
        A.set_exam_objective(self.user, AgendaEvent.query.filter_by(uid="exam1").one())
        for d in self.decks():
            d.auto_quota = True
        db.session.commit()
        imp = A.import_calendar(self.user, exam_ics(None), at=self.at + timedelta(hours=1))
        self.assertEqual(imp.removed, 1)
        for d in self.decks():
            self.assertIsNone(d.date_objectif)
            self.assertFalse(d.auto_quota)
        self.assertIn("date d’examen effacée", imp.exam_notes)

    def test_annuler_l_import_remet_l_ajustement_automatique(self):
        A.set_exam_objective(self.user, AgendaEvent.query.filter_by(uid="exam1").one())
        d0, d1 = self.decks()
        d0.auto_quota, d1.auto_quota = True, False  # un set avec l'ajustement, l'autre sans
        db.session.commit()
        imp = A.import_calendar(self.user, exam_ics(None), at=self.at + timedelta(hours=1))
        self.assertEqual([d.auto_quota for d in self.decks()], [False, False])
        notes = A.undo_import(imp, at=self.at + timedelta(hours=2))
        d0, d1 = self.decks()
        self.assertEqual((d0.date_objectif, d0.auto_quota), (date(2026, 11, 19), True))
        self.assertEqual((d1.date_objectif, d1.auto_quota), (date(2026, 11, 19), False))
        self.assertTrue(any("Ajustement automatique (« Ajuster auto. ») réactivé pour 1 set" in n for n in notes), notes)

    def test_controle_retire_report_au_suivant(self):
        extra = ("BEGIN:VEVENT\nDTSTART:20261126T101500\nDTEND:20261126T121500\nUID:exam2\n"
                 "SUMMARY:Techniques numériques 16 CO_ECRIT\nEND:VEVENT\n")
        A.import_calendar(self.user, exam_ics(date(2026, 11, 19), extra), at=self.at)
        A.set_exam_objective(self.user, AgendaEvent.query.filter_by(uid="exam1").one())
        db.session.commit()
        imp = A.import_calendar(self.user, exam_ics(None, extra), at=self.at + timedelta(hours=1))
        self.assertEqual([d.date_objectif for d in self.decks()], [date(2026, 11, 26)] * 2)
        self.assertNotIn("déplacé", imp.exam_notes)
        self.assertIn("contrôle du 19/11 retiré : date d’examen reportée au prochain contrôle (26/11)",
                      imp.exam_notes)

    def test_nouveau_controle_prepare_sans_clic(self):
        self.assertEqual(self.decks(), [])
        extra = ("BEGIN:VEVENT\nDTSTART:20261126T101500\nDTEND:20261126T121500\nUID:exam2\n"
                 "SUMMARY:Techniques numériques 16 CO_ECRIT\nEND:VEVENT\n")
        imp = A.import_calendar(self.user, exam_ics(date(2026, 11, 19), extra), at=self.at + timedelta(hours=1))
        # Le prochain contrôle (19/11), pas le dernier arrivé (26/11)
        self.assertEqual([d.date_objectif for d in self.decks()], [date(2026, 11, 19)] * 2)
        self.assertIn("fixée au 19/11", imp.exam_notes)
        c, token = self.login()
        page = c.get(f"/agenda/imports/{imp.id}").get_data(as_text=True)
        self.assertIn("Révisions de QCM", page)

    def test_relier_une_matiere_prepare_le_prochain_controle(self):
        from hangar.models import AgendaSubjectLink
        AgendaSubjectLink.query.delete()
        db.session.commit()
        c, token = self.login()
        ev = AgendaEvent.query.filter_by(uid="exam1").one()
        r = c.post(f"/agenda/events/{ev.id}/link", data={"_csrf": token, "qcm_category": "TECHNIQUES_NUM"},
                   follow_redirects=True)
        self.assertIn("Révisions de QCM", r.get_data(as_text=True))
        self.assertEqual([d.date_objectif for d in self.decks()], [date(2026, 11, 19)] * 2)

    def test_numero_de_seance_vide(self):
        self.assertEqual(ics.parse_summary("Eloquence-Argumentation niveau 2  CO_ORAL"),
                         {"title": "Eloquence-Argumentation niveau 2", "session_no": "", "kind": "CO_ORAL", "flag": ""})
        self.assertEqual(ics.parse_summary("Eloquence-Argumentation niveau 2 8 CM")["title"],
                         "Eloquence-Argumentation niveau 2")
        self.assertEqual(A.subject_key(ics.parse_summary("Eloquence-Argumentation niveau 2  CO_ORAL")["title"]),
                         A.subject_key(ics.parse_summary("Eloquence-Argumentation niveau 2 8 CM")["title"]))
        self.assertEqual(ics.parse_summary("Sport  SPORT")["title"], "Sport")
        self.assertEqual(ics.parse_summary("Techniques numériques 15 CO_ECRIT")["session_no"], "15")


# ---------------------------------------------------------------------------
# Piste 54 : exports .ics conformes (RFC 5545)
# ---------------------------------------------------------------------------

def rfc5545_problems(raw: bytes) -> list[str]:
    """Contrôle de conformité des points que les clients de calendrier vérifient."""
    pb = []
    if not raw.endswith(b"\r\n"):
        pb.append("pas de CRLF final")
    physical = raw.split(b"\r\n")[:-1]
    for n, ln in enumerate(physical, 1):
        if len(ln) > 75:
            pb.append(f"ligne {n} : {len(ln)} octets")
        if b"\r" in ln or b"\n" in ln:
            pb.append(f"ligne {n} : retour à la ligne nu")
    text = raw.decode("utf-8")
    lines = ics.unfold(text)
    tzids, vtz, stack, comp = set(), set(), [], None
    for ln in lines:
        name, params, value = ics.parse_line(ln)
        if name == "BEGIN":
            stack.append({"name": value, "props": []})
            continue
        if name == "END":
            comp = stack.pop()
            props = {p[0]: p for p in comp["props"]}
            if comp["name"] == "VEVENT":
                for req in ("UID", "DTSTAMP", "DTSTART"):
                    if req not in props:
                        pb.append(f"VEVENT sans {req}")
                if "DTSTAMP" in props and not props["DTSTAMP"][2].endswith("Z"):
                    pb.append("DTSTAMP pas en UTC")
                if "DTEND" in props:
                    s, e = props["DTSTART"][2], props["DTEND"][2]
                    if e <= s:
                        pb.append(f"DTEND {e} <= DTSTART {s}")
                if "RRULE" in props:
                    until = re.search(r"UNTIL=([0-9TZ]+)", props["RRULE"][2])
                    if until and "TZID" in props["DTSTART"][1] and not until.group(1).endswith("Z"):
                        pb.append(f"UNTIL sans UTC : {props['RRULE'][2]}")
            if comp["name"] == "VALARM":
                for req in ("ACTION", "TRIGGER", "DESCRIPTION"):
                    if req not in props:
                        pb.append(f"VALARM sans {req}")
                if "TRIGGER" in props:
                    p = props["TRIGGER"]
                    if p[1].get("VALUE") == "DATE-TIME":
                        if not re.fullmatch(r"\d{8}T\d{6}Z", p[2]):
                            pb.append(f"TRIGGER absolu invalide : {p[2]}")
                    elif not re.fullmatch(r"-?PT\d+M", p[2]):
                        pb.append(f"TRIGGER invalide : {p[2]}")
            if comp["name"] == "VTIMEZONE":
                vtz.add(props["TZID"][2])
            continue
        if stack:
            stack[-1]["props"].append((name, params, value))
            if "TZID" in params:
                tzids.add(params["TZID"])
            if name in ("SUMMARY", "DESCRIPTION", "LOCATION", "X-WR-CALNAME", "CATEGORIES"):
                if re.search(r"(?<!\\)[,;]", value.replace("\\\\", "")):
                    pb.append(f"{name} : virgule ou point-virgule non échappé : {value!r}")
    for tz in tzids - vtz:
        pb.append(f"TZID {tz} sans VTIMEZONE")
    if stack:
        pb.append("composant non fermé")
    return pb


def components(text: str, name: str) -> list[dict]:
    out, cur = [], None
    for ln in ics.unfold(text):
        n, params, value = ics.parse_line(ln)
        if n == "BEGIN" and value == name:
            cur = {}
        elif n == "END" and value == name:
            out.append(cur)
            cur = None
        elif cur is not None and n not in cur:
            cur[n] = (params, value)
    return out


class ExportICSTests(Base):
    def test_export_agenda_conforme(self):
        self.settings()  # allday_hour = 8
        A.import_calendar(self.user, exam_ics(date(2026, 11, 19)), at=datetime(2026, 11, 10, 8, 0))
        t1 = AgendaTask(user_id=self.user.id, title="Rendre le rapport, version finale; relue", due_date=date(2026, 11, 13),
                        notes="Chapitre 3\r\nChapitre 5 \\ annexes")
        t2 = AgendaTask(user_id=self.user.id, title="Séance de révision", due_date=date(2026, 11, 13),
                        start_time=time(18, 0), end_time=time(19, 0), recurrence="weekly",
                        recurrence_until=date(2026, 12, 4))
        t3 = AgendaTask(user_id=self.user.id, title="Lire le poly", due_date=date(2026, 11, 20))
        db.session.add_all([t1, t2, t3])
        db.session.flush()
        at = datetime(2026, 11, 10, 9, 0)
        A.set_task_reminders(t1, [0, 60, 1440], "app", at=at)
        A.set_task_reminders(t2, [15], "app", at=at)
        A.add_fixed_reminder(self.user.id, datetime(2026, 11, 19, 21, 30), "app", task=t3, at=at)
        db.session.commit()
        # Dans Hangar : 13/11 à 8 h, 7 h et la veille à 8 h ; série jusqu'au 04/12 inclus
        self.assertEqual(sorted(r.remind_at for r in t1.reminders),
                         [datetime(2026, 11, 12, 8), datetime(2026, 11, 13, 7), datetime(2026, 11, 13, 8)])
        text = A.export_ics(self.user, at=at)
        raw = text.encode("utf-8")
        self.assertEqual(rfc5545_problems(raw), [])
        ev = {c["SUMMARY"][1]: c for c in components(text, "VEVENT")}
        self.assertIn("RRULE", ev["✓ Séance de révision"])
        self.assertEqual(ev["✓ Séance de révision"]["RRULE"][1], "FREQ=WEEKLY;UNTIL=20261204T170000Z")
        triggers = sorted(ln for ln in ics.unfold(text) if ln.startswith("TRIGGER"))
        self.assertIn("TRIGGER:PT480M", triggers)    # « à l'heure » : 8 h le jour même
        self.assertIn("TRIGGER:PT420M", triggers)    # « 1 h avant » : 7 h
        self.assertIn("TRIGGER:-PT960M", triggers)   # « la veille » : 8 h la veille
        self.assertIn("TRIGGER:-PT15M", triggers)
        self.assertIn("TRIGGER;VALUE=DATE-TIME:20261119T203000Z", triggers)  # date fixe, en UTC
        self.assertIn("BEGIN:VTIMEZONE", text)
        self.assertNotIn("\r\\n", text)
        # Aller-retour avec le lecteur du projet : tous les événements, titres et notes intacts
        cal = ics.parse(raw)
        self.assertEqual(len(cal.events), 3 + 3)
        titles = {e.summary for e in cal.events}
        self.assertIn("✓ Rendre le rapport, version finale; relue", titles)
        back = next(e for e in cal.events if e.uid == "tache-%d@hangar" % t1.id)
        self.assertEqual(back.description, "Chapitre 3\nChapitre 5 \\ annexes")
        rev = next(e for e in cal.events if e.uid == "tache-%d@hangar" % t2.id)
        self.assertEqual((rev.start, rev.end), (datetime(2026, 11, 13, 18), datetime(2026, 11, 13, 19)))
        # Vérification indépendante du calcul UTC (heure d'hiver et d'été, avec ou sans base des fuseaux)
        self.assertEqual(ics._paris_offset(datetime(2026, 12, 4, 18)), timedelta(hours=1))
        self.assertEqual(ics._paris_offset(datetime(2026, 7, 1, 12)), timedelta(hours=2))
        self.assertEqual(ics._paris_offset(datetime(2026, 3, 29, 1, 59)), timedelta(hours=1))
        self.assertEqual(ics._paris_offset(datetime(2026, 3, 29, 3, 0)), timedelta(hours=2))

    def test_export_tickets_conforme(self):
        from hangar.services import create_issue, update_issue
        p, st = self.project()
        p.cycles_enabled = True
        db.session.add(Cycle(project_id=p.id, name="Sprint 1, phase A", start_date=date(2026, 11, 16),
                             end_date=date(2026, 11, 27)))
        db.session.commit()
        injected = "Ligne 1\nEND:VEVENT\nBEGIN:VEVENT\nUID:faux@x\nSUMMARY:Événement injecté"
        legacy = self.issue(p, st, injected, date(2026, 11, 20))                    # titre d'une base existante
        self.issue(p, st, "Dates inversées", date(2026, 11, 18), start=date(2026, 11, 21))
        self.issue(p, st, "Back\\slash, test; " + "très long titre " * 6, date(2026, 11, 19))
        self.issue(p, st, "Sans échéance", None)
        # À la saisie, les retours à la ligne d'un titre deviennent des espaces
        made = create_issue(p, {"title": "Rédiger le rapport\r\nphase 2 (relecture)"}, self.user)
        db.session.commit()
        self.assertEqual(made.title, "Rédiger le rapport phase 2 (relecture)")
        update_issue(made, {"title": "A\nB", "target_date": "2026-11-25"}, self.user)
        db.session.commit()
        self.assertEqual(made.title, "A B")
        c, _ = self.login()
        r = c.get(f"/projects/{p.id}/calendar.ics")
        self.assertEqual(r.status_code, 200)
        raw = r.get_data()
        self.assertEqual(rfc5545_problems(raw), [])
        cal = ics.parse(raw)
        with_target = Issue.query.filter(Issue.target_date.isnot(None)).count()
        self.assertEqual(len(cal.events), with_target + 1)  # tickets avec échéance + le cycle
        self.assertNotIn("faux@x", {e.uid for e in cal.events})
        self.assertIn(f"{legacy.key} {injected}", {e.summary for e in cal.events})
        inv = next(e for e in cal.events if "Dates inversées" in e.summary)
        self.assertEqual((inv.start.date(), inv.end.date()), (date(2026, 11, 18), date(2026, 11, 19)))
        self.assertIn("Cycle : Sprint 1, phase A", {e.summary for e in cal.events})
        # Les notifications d'un ticket au vieux titre multiligne partent quand même
        m = mailer.queue_email("moi@exemple.fr", f"[{legacy.key}] Modifié : {legacy.title}", "<p>x</p>")
        db.session.commit()
        self.assertNotIn("\n", mailer.build_mime(m, MailConfig.get())["Subject"])


if __name__ == "__main__":
    unittest.main()

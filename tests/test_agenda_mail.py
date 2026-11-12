"""Tests du groupe « agenda-mail » :

- import .ics : semaines couvertes calculées sur de vraies séances d'une journée, garde-fou contre les
  retraits massifs, annulation qui remet les rappels ;
- e-mails : nouvel essai après un échec passager (faux smtplib), date limite selon le type, pas de nouvel
  essai pour un mot de passe refusé, « Renvoyer les échecs », compteur d'échecs dans l'agenda ;
- fil d'arrière-plan : chaque étape protégée et notée sur /erreurs/ ;
- rappels des tâches : tâche répétée non cochée, rappel à date fixe posé après l'échéance, répétition
  mensuelle du 31, résumé sans doublon, annulation d'une coche.

Lancer : uv run python -m unittest tests.test_agenda_mail -v
"""
import re
import shutil
import smtplib
import socket
import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from unittest import mock

from hangar import agenda as A
from hangar import create_app, mailer
from hangar.extensions import db
from hangar.models import AgendaEvent, AgendaReminder, AgendaTask, EmailMessage, MailConfig, User


# ---------------------------------------------------------------------------
# Fichiers .ics de test (format de l'école)
# ---------------------------------------------------------------------------

def vevent(uid, start, end, summary):
    return (f"BEGIN:VEVENT\nDTSTART:{start:%Y%m%dT%H%M%S}\nDTEND:{end:%Y%m%dT%H%M%S}\nUID:{uid}\n"
            f"SUMMARY:{summary}\nEND:VEVENT\n")


def cours(uid, day, hour, minute=0, title="Cours 1 CM", minutes=120):
    start = datetime.combine(day, time(hour, minute))
    return vevent(uid, start, start + timedelta(minutes=minutes), title)


def calendrier(*events):
    return "BEGIN:VCALENDAR\nVERSION:2.0\nX-WR-TIMEZONE:Europe/Paris\n" + "".join(events) + "END:VCALENDAR\n"


# Bloc de formation militaire du lundi 16 au mercredi 25 novembre (à cheval sur S47 et S48)
BLOC_MILT = vevent("MILT", datetime(2026, 11, 16, 8), datetime(2026, 11, 25, 17, 45),
                   "M2.1 : formation militaire complémentaire n°4  MILT")


def semaine(lundi, prefixe, jours=range(5), heures=((8, 0), (13, 30))):
    """Deux séances par jour du lundi au vendredi (UID « prefixe-jour-heure »)."""
    out = []
    for j in jours:
        d = lundi + timedelta(days=j)
        for h, m in heures:
            out.append(cours(f"{prefixe}-{d:%d}-{h:02d}", d, h, m, title=f"Matière {j} 1 CM"))
    return out


S47 = date(2026, 11, 16)
S48 = date(2026, 11, 23)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hangar-agenda-mail-")
        self.app = create_app(self.tmp, testing=True)
        self.ctx = self.app.test_request_context("/")
        self.ctx.push()
        self.user = User(username="moi", email="moi@hangar.local", role="admin", first_name="Moi",
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

    def ev(self, uid):
        return AgendaEvent.query.filter_by(user_id=self.user.id, uid=uid).one()

    def client(self):
        c = self.app.test_client()
        page = c.get("/login").get_data(as_text=True)
        token = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
        c.post("/login", data={"login": "moi", "password": "secret1", "_csrf": token})
        return c, token


# ---------------------------------------------------------------------------
# Piste 5 : import du planning
# ---------------------------------------------------------------------------

class ImportSemainesTests(Base):
    def test_bloc_commence_la_semaine_precedente_ne_retire_pas_les_cours_de_la_semaine(self):
        # Fichier S47 importé le dimanche : bloc MILT + cours du mardi au vendredi
        s47 = calendrier(BLOC_MILT, *semaine(S47, "S47", jours=range(1, 5)))
        A.import_calendar(self.user, s47, "s47.ics", at=datetime(2026, 11, 15, 18, 0))
        # Jeudi midi, fichier S48 (même bloc + deux cours) : les cours de jeudi et vendredi restent
        s48 = calendrier(BLOC_MILT, cours("S48-a", date(2026, 11, 26), 8), cours("S48-b", date(2026, 11, 27), 8))
        imp = A.import_calendar(self.user, s48, "s48.ics", at=datetime(2026, 11, 19, 12, 30))
        self.assertEqual(imp.removed, 0)
        self.assertEqual((imp.range_start, imp.range_end), (S48, date(2026, 11, 29)))
        for uid in ("S47-19-08", "S47-19-13", "S47-20-08", "S47-20-13"):
            self.assertEqual(self.ev(uid).status, "active", uid)
        # Le bandeau « Nouveau planning ? » sait que la semaine S48 est couverte, pas plus
        self.assertEqual(A.dashboard(self.user, at=datetime(2026, 11, 19, 13, 0))["nudge"], False)

    def test_une_prevision_ne_couvre_pas_une_semaine(self):
        from tests.test_agenda import SAMPLE  # fichier S46 de l'école (bloc MILT qui déborde sur S47)
        A.import_calendar(self.user, SAMPLE, "s46.ics", at=datetime(2026, 11, 9, 7, 0))
        A.import_calendar(self.user, calendrier(*semaine(S47, "S47")), "s47.ics", at=datetime(2026, 11, 15, 18, 0))
        # Rectificatif de S46 avec une séance prévisionnelle en S47, importé le lundi 16 au matin
        rect = SAMPLE.replace("END:VCALENDAR\n", cours("PREV", date(2026, 11, 18), 10, title="Anglais 3 CM (PREVISION)")
                              + "END:VCALENDAR\n")
        imp = A.import_calendar(self.user, rect, "s46-v2.ics", at=datetime(2026, 11, 16, 7, 0))
        self.assertEqual(imp.removed, 0)
        self.assertEqual(AgendaEvent.query.filter(AgendaEvent.uid.like("S47-%"), AgendaEvent.status == "active").count(), 10)
        self.assertEqual((imp.range_start, imp.range_end), (date(2026, 11, 9), date(2026, 11, 15)))

    def test_semaine_nouvelle_apres_une_prevision_isolee(self):
        prev = calendrier(*semaine(date(2026, 11, 9), "S46"),
                          cours("PREV", date(2026, 11, 18), 10, title="Anglais 3 CM (PREVISION)"))
        A.import_calendar(self.user, prev, "s46.ics", at=datetime(2026, 11, 9, 7, 0))
        imp = A.import_calendar(self.user, calendrier(*semaine(S47, "S47")), "s47.ics", at=datetime(2026, 11, 15, 18, 0))
        # Première vraie importation de S47 : séances « nouvelles », pas « ajoutées » (pas de badge ni d'alerte)
        self.assertEqual({d.get("notable") for d in imp.details if d["type"] == "added"}, {False})
        self.assertEqual(self.ev("S47-16-08").change_kind, "new")
        # La prévision isolée, absente du vrai fichier de la semaine, est bien retirée
        self.assertEqual(self.ev("PREV").status, "removed")

    def test_garde_fou_contre_un_retrait_massif(self):
        A.import_calendar(self.user, calendrier(*semaine(S47, "S47")), "s47.ics", at=datetime(2026, 11, 15, 18, 0))
        # Rectificatif de S46 qui apporte une seule vraie séance en S47 : S47 perdrait 10 séances sur 10
        s46 = semaine(date(2026, 11, 9), "S46")
        rect = calendrier(*s46, cours("RATT", S47, 10, title="Rattrapage 1 CM"))
        imp = A.import_calendar(self.user, rect, "s46-v2.ics", at=datetime(2026, 11, 15, 20, 0))
        self.assertEqual(imp.removed, 0)
        self.assertEqual(AgendaEvent.query.filter(AgendaEvent.uid.like("S47-%"), AgendaEvent.status == "active").count(), 10)
        self.assertIn("16 nov.", imp.warnings)
        self.assertIn("rien n’a été retiré", imp.warnings)
        self.assertEqual(self.ev("RATT").status, "active")
        # En revanche, le fichier d'une seule semaine fait foi même s'il retire beaucoup de séances
        A.import_calendar(self.user, calendrier(*semaine(S48, "S48")), "s48.ics", at=datetime(2026, 11, 22, 18, 0))
        imp = A.import_calendar(self.user, calendrier(cours("S48-23-08", S48, 8, title="Matière 0 1 CM")),
                                "s48-v2.ics", at=datetime(2026, 11, 22, 19, 0))
        self.assertEqual((imp.removed, imp.warnings), (9, ""))

    def test_annuler_l_import_remet_les_rappels(self):
        s = A.settings_of(self.user)
        s.exam_reminder = 1440
        db.session.commit()
        controle = cours("EXAM", date(2026, 11, 20), 10, 15, title="Techniques numériques 15 CO_ECRIT")
        jeudi = cours("JEU", date(2026, 11, 19), 8, title="Anglais 2 TD_TP")
        autres = semaine(S47, "S47", heures=((14, 0),))
        A.import_calendar(self.user, calendrier(controle, jeudi, *autres), "s47.ics", at=datetime(2026, 11, 15, 18, 0))
        auto = AgendaReminder.query.filter_by(auto=True).one()
        self.assertEqual(auto.remind_at, datetime(2026, 11, 19, 10, 15))
        manuel = A.add_event_reminder(self.ev("JEU"), 1440, at=datetime(2026, 11, 15, 18, 5))
        db.session.commit()
        # Mardi : un fichier (incomplet) retire le contrôle et le cours de jeudi
        imp = A.import_calendar(self.user, calendrier(*autres), "s47-v2.ics", at=datetime(2026, 11, 17, 9, 0))
        self.assertEqual(imp.removed, 2)
        self.assertEqual(AgendaReminder.query.filter_by(auto=True).count(), 0)
        A.process_reminders(datetime(2026, 11, 18, 8, 0, 30))  # rappel du cours retiré : ignoré
        self.assertEqual(db.session.get(AgendaReminder, manuel.id).status, "skipped")
        # Mercredi midi : on annule l'import
        A.undo_import(imp, at=datetime(2026, 11, 18, 12, 0))
        self.assertEqual(self.ev("EXAM").status, "active")
        auto = AgendaReminder.query.filter_by(auto=True).one()
        self.assertEqual((auto.status, auto.remind_at), ("pending", datetime(2026, 11, 19, 10, 15)))
        self.assertEqual(db.session.get(AgendaReminder, manuel.id).status, "pending")
        # Le cours de jeudi n'a pas encore eu lieu : le rappel part (en retard plutôt que jamais)
        self.assertEqual(A.process_reminders(datetime(2026, 11, 18, 12, 0, 30)), 1)

    def test_reimporter_l_ancien_fichier_remet_aussi_les_rappels(self):
        jeudi = cours("JEU", date(2026, 11, 19), 8, title="Anglais 2 TD_TP")
        autres = semaine(S47, "S47", heures=((14, 0),))
        complet = calendrier(jeudi, *autres)
        A.import_calendar(self.user, complet, "s47.ics", at=datetime(2026, 11, 15, 18, 0))
        r = A.add_event_reminder(self.ev("JEU"), 1440, at=datetime(2026, 11, 15, 18, 5))
        db.session.commit()
        A.import_calendar(self.user, calendrier(*autres), "s47-v2.ics", at=datetime(2026, 11, 17, 9, 0))
        A.process_reminders(datetime(2026, 11, 18, 8, 0, 30))
        self.assertEqual(db.session.get(AgendaReminder, r.id).status, "skipped")
        imp = A.import_calendar(self.user, complet, "s47-v3.ics", at=datetime(2026, 11, 18, 12, 0))
        self.assertEqual(imp.restored, 1)
        self.assertEqual(db.session.get(AgendaReminder, r.id).status, "pending")

    def test_alerte_quand_des_cours_du_jour_sont_retires(self):
        c, token = self.client()
        import io
        complet = calendrier(*semaine(date.today() - timedelta(days=date.today().weekday()), "SEM"))
        c.post("/agenda/import", data={"_csrf": token, "file": (io.BytesIO(complet.encode()), "a.ics")},
               content_type="multipart/form-data")
        lundi = date.today() - timedelta(days=date.today().weekday())
        reste = [e for e in semaine(lundi, "SEM") if f"SEM-{date.today():%d}-" not in e
                 and f"SEM-{date.today() + timedelta(days=1):%d}-" not in e]
        if len(reste) == 10:  # week-end : rien aujourd'hui ni demain dans la semaine importée
            self.skipTest("Pas de cours aujourd'hui ni demain dans la semaine de test (week-end).")
        r = c.post("/agenda/import", data={"_csrf": token, "file": (io.BytesIO(calendrier(*reste).encode()), "b.ics")},
                   content_type="multipart/form-data", follow_redirects=True)
        page = r.get_data(as_text=True)
        self.assertIn("séance(s) d’aujourd’hui ou de demain retirée(s)", page)


# ---------------------------------------------------------------------------
# Piste 9 : e-mails en échec passager, faux smtplib
# ---------------------------------------------------------------------------

# Messages sans date limite propre : 24 h après leur création (heure réelle) ; on simule donc les
# passages du fil à partir de maintenant.
T0 = datetime.now().replace(second=0, microsecond=0) + timedelta(minutes=1)


class FauxSMTP:
    """Faux serveur SMTP : on règle ce qui doit échouer, il garde la liste des messages reçus."""
    connexions = 0
    erreur_connexion = None       # exception levée à l'ouverture
    erreur_login = None           # exception levée par login()
    erreurs_envoi = {}            # adresse -> exception levée par send_message()
    recus = []

    def __init__(self, host, port, timeout=None, context=None):
        type(self).connexions += 1
        if self.erreur_connexion is not None:
            raise self.erreur_connexion

    def ehlo(self):
        pass

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        if self.erreur_login is not None:
            raise self.erreur_login

    def send_message(self, mime):
        err = self.erreurs_envoi.get(mime["To"])
        if err is not None:
            raise err
        type(self).recus.append(mime)

    def quit(self):
        pass

    @classmethod
    def remettre(cls):
        cls.connexions, cls.erreur_connexion, cls.erreur_login, cls.erreurs_envoi, cls.recus = 0, None, None, {}, []


class MailBase(Base):
    def setUp(self):
        super().setUp()
        FauxSMTP.remettre()
        cfg = MailConfig.get()
        cfg.mode, cfg.smtp_host, cfg.smtp_port, cfg.smtp_security = "smtp", "smtp.exemple.fr", 587, "starttls"
        cfg.smtp_user, cfg.smtp_password = "moi@exemple.fr", "secret"
        db.session.commit()
        p1 = mock.patch.object(mailer.smtplib, "SMTP", FauxSMTP)
        p2 = mock.patch.object(mailer.smtplib, "SMTP_SSL", FauxSMTP)
        p1.start()
        p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)

    def tache_avec_rappel(self, jour=date(2026, 11, 12), heure=time(10, 0), offset=60):
        t = AgendaTask(user_id=self.user.id, title="Rendre le TP", due_date=jour, start_time=heure)
        db.session.add(t)
        db.session.flush()
        A.set_task_reminders(t, [offset], "email", at=datetime(2026, 11, 10, 9, 0))
        db.session.commit()
        return t

    def statuts(self):
        return sorted((m.kind, m.status) for m in EmailMessage.query.all())


class EmailNouvelEssaiTests(MailBase):
    def test_echec_passager_puis_nouvel_essai(self):
        t = self.tache_avec_rappel()
        s = A.settings_of(self.user)
        s.daily_mode, s.daily_hour = "morning", 7
        db.session.commit()
        at = datetime(2026, 11, 12, 9, 0, 10)
        self.assertEqual(A.run_jobs(at=at), {"reminders": 1, "daily": 1})
        # Réveil du portable : pas encore de Wi-Fi
        FauxSMTP.erreur_connexion = socket.gaierror(-2, "Name or service not known")
        stats = mailer.send_pending(at=at)
        self.assertEqual((stats["sent"], stats["failed"], stats["waiting"]), (0, 0, 2))
        self.assertEqual(self.statuts(), [("agenda", "queued"), ("reminder", "queued")])
        m = EmailMessage.query.filter_by(kind="reminder").one()
        self.assertEqual(m.attempts, 1)
        self.assertIn("connexion Internet", m.error)
        # L'agenda n'affiche pas « Envoyé » pour un e-mail qui n'est pas parti
        r = t.reminders[0]
        self.assertNotEqual(A.reminder_payload(r)["statusLabel"], "Envoyé")
        self.assertEqual(A.mail_status(self.user)["retrying"], 2)
        # Le réseau revient : attente d'une minute avant le nouvel essai (pas de connexion entre-temps)
        FauxSMTP.erreur_connexion = None
        n = FauxSMTP.connexions
        self.assertEqual(mailer.send_pending(at=at + timedelta(seconds=30))["waiting"], 2)
        self.assertEqual(FauxSMTP.connexions, n)
        stats = mailer.send_pending(at=at + timedelta(minutes=1, seconds=5))
        self.assertEqual(stats["sent"], 2)
        self.assertEqual(len(FauxSMTP.recus), 2)
        self.assertEqual(self.statuts(), [("agenda", "sent"), ("reminder", "sent")])
        self.assertEqual(A.reminder_payload(t.reminders[0])["statusLabel"], "Envoyé")

    def test_attente_croissante(self):
        mailer.queue_email("moi@exemple.fr", "Bonjour", "<p>x</p>", kind="manual")
        db.session.commit()
        FauxSMTP.erreur_connexion = TimeoutError("timed out")
        t0 = T0
        tentatives = []
        for minute in range(0, 120):
            avant = FauxSMTP.connexions
            mailer.send_pending(at=t0 + timedelta(minutes=minute, seconds=1))
            if FauxSMTP.connexions > avant:
                tentatives.append(minute)
        self.assertEqual(tentatives, [0, 1, 6, 21, 81])
        self.assertEqual(EmailMessage.query.one().status, "queued")

    def test_mot_de_passe_refuse_pas_de_nouvel_essai(self):
        mailer.queue_email("moi@exemple.fr", "Bonjour", "<p>x</p>", kind="manual")
        db.session.commit()
        FauxSMTP.erreur_login = smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")
        stats = mailer.send_pending(at=T0)
        self.assertEqual(stats["failed"], 1)
        m = EmailMessage.query.one()
        self.assertEqual(m.status, "failed")
        self.assertIn("mot de passe", m.error)
        n = FauxSMTP.connexions
        mailer.send_pending(at=T0 + timedelta(minutes=5))
        self.assertEqual(FauxSMTP.connexions, n)  # rien à renvoyer : pas de nouvel essai de connexion

    def test_destinataire_refuse_et_coupure_en_cours_d_envoi(self):
        for addr in ("a@exemple.fr", "refuse@exemple.fr", "b@exemple.fr", "c@exemple.fr"):
            mailer.queue_email(addr, "Bonjour", "<p>x</p>", kind="manual")
        db.session.commit()
        FauxSMTP.erreurs_envoi = {
            "refuse@exemple.fr": smtplib.SMTPRecipientsRefused({"refuse@exemple.fr": (550, b"no such user")}),
            "b@exemple.fr": smtplib.SMTPServerDisconnected("Connection unexpectedly closed"),
        }
        stats = mailer.send_pending(at=T0)
        self.assertEqual((stats["sent"], stats["failed"], stats["waiting"]), (1, 1, 2))
        par_adresse = {m.to_address: m.status for m in EmailMessage.query}
        self.assertEqual(par_adresse, {"a@exemple.fr": "sent", "refuse@exemple.fr": "failed",
                                       "b@exemple.fr": "queued", "c@exemple.fr": "queued"})
        FauxSMTP.erreurs_envoi = {}
        mailer.send_pending(at=T0 + timedelta(minutes=2))
        self.assertEqual({m.to_address: m.status for m in EmailMessage.query},
                         {"a@exemple.fr": "sent", "refuse@exemple.fr": "failed",
                          "b@exemple.fr": "sent", "c@exemple.fr": "sent"})

    def test_code_4xx_reessaye(self):
        mailer.queue_email("a@exemple.fr", "Bonjour", "<p>x</p>", kind="manual")
        db.session.commit()
        FauxSMTP.erreurs_envoi = {"a@exemple.fr": smtplib.SMTPDataError(451, b"Try again later")}
        self.assertEqual(mailer.send_pending(at=T0)["waiting"], 1)
        self.assertEqual(EmailMessage.query.one().status, "queued")

    def test_date_limite_d_un_rappel_et_du_resume(self):
        self.tache_avec_rappel(heure=time(10, 0), offset=60)  # rappel à 9 h, tâche jusqu'à 11 h
        s = A.settings_of(self.user)
        s.daily_mode, s.daily_hour = "morning", 7
        db.session.commit()
        A.run_jobs(at=datetime(2026, 11, 12, 9, 0, 10))
        FauxSMTP.erreur_connexion = ConnectionRefusedError(111, "Connection refused")
        for minute in range(0, 130, 1):
            mailer.send_pending(at=datetime(2026, 11, 12, 9, 0, 10) + timedelta(minutes=minute))
        # 11 h 10 : la tâche est finie, le rappel est abandonné ; le résumé du jour attend encore
        self.assertEqual(self.statuts(), [("agenda", "queued"), ("reminder", "failed")])
        rappel = EmailMessage.query.filter_by(kind="reminder").one()
        self.assertIn("Pas envoyé à temps", rappel.error)
        self.assertEqual(rappel.expires_at, datetime(2026, 11, 12, 11, 0))
        # Lendemain : le résumé d'hier n'est plus envoyé
        FauxSMTP.erreur_connexion = None
        mailer.send_pending(at=datetime(2026, 11, 13, 0, 5))
        self.assertEqual(self.statuts(), [("agenda", "failed"), ("reminder", "failed")])
        self.assertEqual(FauxSMTP.recus, [])

    def test_envoi_depuis_l_interface_echoue_tout_de_suite(self):
        FauxSMTP.erreur_connexion = ConnectionRefusedError(111, "Connection refused")
        m = mailer.send_test("moi@exemple.fr")
        db.session.refresh(m)
        self.assertEqual(m.status, "failed")
        self.assertTrue(m.error)

    def test_un_essai_depuis_l_interface_qui_reussit_relance_la_file(self):
        mailer.queue_email("a@exemple.fr", "Bonjour", "<p>x</p>", kind="manual")
        db.session.commit()
        FauxSMTP.erreur_connexion = OSError(101, "Network is unreachable")
        mailer.send_pending(at=T0)
        FauxSMTP.erreur_connexion = None
        self.assertEqual(mailer.send_pending(at=T0 + timedelta(seconds=20))["waiting"], 1)
        # « Traiter la file » (bouton des réglages) n'attend pas
        self.assertEqual(mailer.send_pending(at=T0 + timedelta(seconds=30), force=True)["sent"], 1)


class RenvoyerLesEchecsTests(MailBase):
    def test_bouton_des_reglages_e_mails(self):
        FauxSMTP.erreur_login = smtplib.SMTPAuthenticationError(535, b"bad credentials")
        mailer.queue_email("a@exemple.fr", "Un", "<p>x</p>", kind="manual")
        mailer.queue_email("b@exemple.fr", "Deux", "<p>x</p>", kind="manual")
        db.session.commit()
        mailer.send_pending()
        self.assertEqual({m.status for m in EmailMessage.query}, {"failed"})
        FauxSMTP.erreur_login = None  # mot de passe corrigé
        c, token = self.client()
        page = c.get("/settings/mail").get_data(as_text=True)
        self.assertIn("Renvoyer les échecs", page)
        r = c.post("/settings/mail", data={"_csrf": token, "action": "retry_failed"}, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual({m.status for m in EmailMessage.query}, {"sent"})
        self.assertEqual(len(FauxSMTP.recus), 2)

    def test_compteur_et_bouton_dans_l_agenda(self):
        FauxSMTP.erreur_login = smtplib.SMTPAuthenticationError(535, b"bad credentials")
        t = self.tache_avec_rappel(jour=date.today() + timedelta(days=2))
        A.process_reminders(datetime.combine(date.today() + timedelta(days=2), time(9, 0, 30)))
        mailer.queue_email("autre@exemple.fr", "Pas pour moi", "<p>x</p>", kind="manual")
        db.session.commit()
        mailer.send_pending()
        self.assertEqual(A.mail_status(self.user)["failed"], 1)  # seulement ses propres e-mails
        self.assertEqual(A.reminder_payload(t.reminders[0])["statusLabel"], "E-mail en échec")
        c, token = self.client()
        page = c.get("/agenda/settings").get_data(as_text=True)
        self.assertIn("1 e-mail n’a pas pu partir", page)
        self.assertIn("Renvoyer les échecs", page)
        self.assertIn("1 e-mail n’a pas pu partir", c.get("/agenda").get_data(as_text=True))
        FauxSMTP.erreur_login = None
        c.post("/agenda/mail/retry", data={"_csrf": token})
        self.assertEqual(EmailMessage.query.filter_by(to_address="moi@exemple.fr").one().status, "sent")
        self.assertEqual(EmailMessage.query.filter_by(to_address="autre@exemple.fr").one().status, "failed")
        self.assertEqual(A.mail_status(self.user)["failed"], 0)


# ---------------------------------------------------------------------------
# Piste 9 : fil d'arrière-plan
# ---------------------------------------------------------------------------

class FilArrierePlanTests(Base):
    def test_une_etape_en_panne_ne_bloque_pas_les_suivantes(self):
        from hangar.wiki.models import ErreurServeur
        appels = []
        with mock.patch.object(mailer, "run_scheduled", side_effect=RuntimeError("récap en panne")), \
                mock.patch("hangar.agenda.run_jobs", side_effect=lambda: appels.append("agenda")), \
                mock.patch("hangar.qcm.rappels.envoyer_rappels_dus", side_effect=lambda **kw: appels.append("qcm")), \
                mock.patch.object(mailer, "send_pending", side_effect=lambda: appels.append("file")):
            mailer.tick(self.app)
        self.assertEqual(appels, ["agenda", "qcm", "file"])
        erreurs = ErreurServeur.query.all()
        self.assertEqual(len(erreurs), 1)
        self.assertIn("récap en panne", erreurs[0].message)
        self.assertIn("Fil des e-mails", erreurs[0].chemin)

    def test_un_rappel_en_panne_n_empeche_pas_les_autres_ni_le_resume(self):
        from hangar.wiki.models import ErreurServeur
        t1 = AgendaTask(user_id=self.user.id, title="Panne", due_date=date(2026, 11, 12), start_time=time(10, 0))
        t2 = AgendaTask(user_id=self.user.id, title="Normal", due_date=date(2026, 11, 12), start_time=time(10, 30))
        db.session.add_all([t1, t2])
        db.session.flush()
        for t in (t1, t2):
            A.set_task_reminders(t, [60], "app", at=datetime(2026, 11, 10, 9, 0))
        s = A.settings_of(self.user)
        s.daily_mode, s.daily_hour = "morning", 7
        db.session.commit()
        vrai = A.send_reminder

        def envoyer(r, user, at):
            if r.task.title == "Panne":
                raise ValueError("gabarit cassé")
            return vrai(r, user, at)

        with mock.patch.object(A, "send_reminder", side_effect=envoyer):
            res = A.run_jobs(at=datetime(2026, 11, 12, 9, 45))
        self.assertEqual(res, {"reminders": 1, "daily": 1})
        self.assertEqual(t1.reminders[0].status, "error")
        self.assertEqual(t2.reminders[0].status, "sent")
        self.assertEqual(ErreurServeur.query.count(), 1)


# ---------------------------------------------------------------------------
# Piste 10 : rappels des tâches
# ---------------------------------------------------------------------------

class RappelsTachesTests(Base):
    def test_tache_repetee_non_cochee_garde_ses_rappels(self):
        t = AgendaTask(user_id=self.user.id, title="Réviser 20 min", due_date=date(2026, 11, 9),
                       start_time=time(18, 0), recurrence="daily")
        db.session.add(t)
        db.session.flush()
        A.set_task_reminders(t, [0], "email", at=datetime(2026, 11, 9, 8, 0))
        db.session.commit()
        for jour in (9, 10, 11, 12):
            self.assertEqual(A.process_reminders(datetime(2026, 11, jour, 18, 0, 30)), 1, jour)
        self.assertEqual(EmailMessage.query.filter_by(kind="reminder").count(), 4)
        r = t.reminders[0]
        self.assertEqual((r.status, r.remind_at), ("pending", datetime(2026, 11, 13, 18, 0)))
        self.assertEqual(t.due_date, date(2026, 11, 9))  # l'échéance n'avance pas : la tâche reste en retard
        # Modifier la tâche ne remet pas le rappel sur l'occurrence en retard
        A.resync_task(t, at=datetime(2026, 11, 12, 18, 2))
        self.assertEqual((r.status, r.remind_at), ("pending", datetime(2026, 11, 13, 18, 0)))
        # Application fermée deux jours : le rappel manqué est reprogrammé pour la prochaine occurrence
        self.assertEqual(A.process_reminders(datetime(2026, 11, 15, 10, 0)), 0)
        self.assertEqual((r.status, r.remind_at), ("pending", datetime(2026, 11, 15, 18, 0)))
        # La série s'arrête : plus de rappel après la dernière occurrence
        t.recurrence_until = date(2026, 11, 15)
        db.session.commit()
        self.assertEqual(A.process_reminders(datetime(2026, 11, 15, 18, 0, 30)), 1)
        self.assertEqual(r.status, "sent")

    def test_rappel_a_date_fixe_pose_apres_l_echeance(self):
        t = AgendaTask(user_id=self.user.id, title="Rendre le rapport", due_date=date(2026, 11, 10))
        db.session.add(t)
        db.session.flush()
        r = A.add_fixed_reminder(self.user.id, datetime(2026, 11, 13, 18, 0), task=t, at=datetime(2026, 11, 12, 9, 0))
        db.session.commit()
        self.assertEqual(r.status, "pending")
        self.assertEqual(A.process_reminders(datetime(2026, 11, 13, 18, 0, 30)), 1)
        self.assertEqual(r.status, "sent")
        mail = EmailMessage.query.filter_by(kind="reminder").one()
        self.assertEqual(mail.expires_at, datetime(2026, 11, 14, 18, 0))
        # Plus de 24 h de retard (application fermée) : abandonné
        r2 = A.add_fixed_reminder(self.user.id, datetime(2026, 11, 14, 8, 0), task=t, at=datetime(2026, 11, 12, 9, 0))
        db.session.commit()
        self.assertEqual(A.process_reminders(datetime(2026, 11, 15, 9, 0)), 0)
        self.assertEqual(r2.status, "missed")
        # Tâche terminée : le rappel n'est pas envoyé
        r3 = A.add_fixed_reminder(self.user.id, datetime(2026, 11, 16, 8, 0), task=t, at=datetime(2026, 11, 12, 9, 0))
        A.complete_task(t, at=datetime(2026, 11, 15, 10, 0))
        db.session.commit()
        self.assertEqual(A.process_reminders(datetime(2026, 11, 16, 8, 0, 30)), 0)
        self.assertEqual(r3.status, "skipped")

    def test_rappel_fixe_le_soir_d_une_tache_du_matin(self):
        t = AgendaTask(user_id=self.user.id, title="Sport", due_date=date(2026, 11, 20), start_time=time(9, 0))
        db.session.add(t)
        db.session.flush()
        A.add_fixed_reminder(self.user.id, datetime(2026, 11, 20, 19, 0), task=t, at=datetime(2026, 11, 12, 9, 0))
        db.session.commit()
        self.assertEqual(A.process_reminders(datetime(2026, 11, 20, 19, 0, 30)), 1)

    def test_repetition_mensuelle_garde_le_jour_d_origine(self):
        t = AgendaTask(user_id=self.user.id, title="Loyer", due_date=date(2027, 1, 31), recurrence="monthly",
                       recurrence_day=31)
        db.session.add(t)
        db.session.commit()
        occ = A.task_occurrences(self.user, date(2027, 1, 1), date(2027, 5, 31))
        self.assertEqual([o.date for o in occ], [date(2027, 1, 31), date(2027, 2, 28), date(2027, 3, 31),
                                                date(2027, 4, 30), date(2027, 5, 31)])
        dates = [A.complete_task(t, at=datetime(2027, 1, 31, 9, 0)) for _ in range(3)]
        self.assertEqual(dates, [date(2027, 2, 28), date(2027, 3, 31), date(2027, 4, 30)])

    def test_formulaire_memorise_le_jour_de_la_repetition_mensuelle(self):
        c, token = self.client()
        c.post("/agenda/tasks", data={"_csrf": token, "title": "Loyer", "date": "2027-01-31", "recurrence": "monthly"})
        t = AgendaTask.query.one()
        self.assertEqual(t.recurrence_day, 31)
        A.complete_task(t, at=datetime(2027, 1, 31, 9, 0))
        db.session.commit()
        self.assertEqual(t.due_date, date(2027, 2, 28))
        # Enregistrer la fiche sans changer la date ne fait pas oublier le 31
        c.post(f"/agenda/tasks/{t.id}", data={"_csrf": token, "title": "Loyer", "date": "2027-02-28",
                                              "recurrence": "monthly"})
        db.session.refresh(t)
        self.assertEqual(t.recurrence_day, 31)
        self.assertEqual(A.complete_task(t, at=datetime(2027, 2, 28, 9, 0)), date(2027, 3, 31))
        # Ancienne tâche (créée avant cette version) : on garde le jour de sa date
        vieille = AgendaTask(user_id=self.user.id, title="Vieille", due_date=date(2027, 1, 30), recurrence="monthly")
        db.session.add(vieille)
        db.session.commit()
        self.assertEqual(A.complete_task(vieille, at=datetime(2027, 1, 30, 9, 0)), date(2027, 2, 28))

    def test_resume_sans_doublon_pour_une_tache_repetee_en_retard(self):
        from hangar import digest
        t = AgendaTask(user_id=self.user.id, title="Réviser", due_date=date(2026, 11, 9), start_time=time(18, 0),
                       recurrence="daily")
        db.session.add(t)
        db.session.commit()
        built = digest.construire(self.user, date(2026, 11, 12), datetime(2026, 11, 12, 7, 0))
        html = built[1]
        self.assertEqual(html.count("Réviser"), 1)
        self.assertNotIn("prévue il y a", html)
        self.assertNotIn("En retard", html)

    def test_annuler_une_coche_donnee_par_erreur(self):
        t = AgendaTask(user_id=self.user.id, title="Footing", due_date=date.today(), recurrence="weekly")
        db.session.add(t)
        db.session.commit()
        c, token = self.client()
        r = c.post(f"/agenda/tasks/{t.id}/toggle", data={"_csrf": token}, headers={"X-Requested-With": "fetch"})
        data = r.get_json()
        self.assertEqual(data["next"], (date.today() + timedelta(days=7)).isoformat())
        self.assertTrue(data["undo"])
        r = c.post(data["undo"], data={"_csrf": token, "prev": data["prev"]}, headers={"X-Requested-With": "fetch"})
        self.assertTrue(r.get_json()["ok"])
        db.session.refresh(t)
        self.assertEqual((t.due_date, t.done_count, t.done_at), (date.today(), 0, None))


if __name__ == "__main__":
    unittest.main()

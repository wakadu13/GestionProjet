"""Tests de l'agenda : lecture .ics, imports (le dernier fait foi), tâches, rappels.

Lancer : uv run python -m unittest discover -s tests -v
"""
import re
import shutil
import tempfile
import unittest
from datetime import date, datetime, time, timedelta

from hangar import agenda as A
from hangar import create_app, ics
from hangar.extensions import db
from hangar.models import AgendaEvent, AgendaReminder, AgendaTask, EmailMessage, Notification, User

# Fichier au format de l'export de l'école (lignes repliées, \r isolés dans les descriptions, PREVISION…)
SAMPLE = (
    "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//hacksw/handcal//NONSGML v1.0//EN\nMETHOD:PUBLISH\n"
    "X-WR-TIMEZONE:Europe/Paris\n"
    "BEGIN:VEVENT\nDTSTART:20261116T080000\nDTEND:20261125T174500\nDTSTAMP:20261125T174500\nUID:1001\nSEQUENCE:1\n"
    "SUMMARY:M2.1 : formation militaire complémentaire n°4  MILT \\n\nDESCRIPTION:\n"
    " - DESCRIPTION : M2.1\r\\nTIR FA\r\\nNRBC\\n\n - Apprenants : CL_25 / CM_25_SI\\n\n - Intervenants : \\n\nEND:VEVENT\n"
    "BEGIN:VEVENT\nDTSTART:20261109T080000\nDTEND:20261109T100000\nUID:1002\n"
    "SUMMARY:Grands enjeux contemporains 7 CM \\n\nLOCATION:SALLE A\\n\nDESCRIPTION:\n"
    " - DESCRIPTION : \\n\n - Apprenants : CL_25\\n\n - Intervenants : DUPONT\\n\nEND:VEVENT\n"
    "BEGIN:VEVENT\nDTSTART:20261110T133000\nDTEND:20261110T173000\nUID:1003\n"
    "SUMMARY:Systèmes d'information géographique 12 TD_TP \\n\nLOCATION:SALLE B\\n\nDESCRIPTION:\n"
    " - DESCRIPTION : \\n\n - Apprenants : CL_25_NED\\n\n - Intervenants : MARTIN\\n\nEND:VEVENT\n"
    "BEGIN:VEVENT\nDTSTART:20261112T080000\nDTEND:20261112T100000\nUID:1004\n"
    "SUMMARY:Anglais, Anglais bilangue et LV2  TD_TP \\n\nLOCATION:DLE18\\n\nEND:VEVENT\n"
    "BEGIN:VEVENT\nDTSTART:20261112T101500\nDTEND:20261112T121500\nUID:1005\n"
    "SUMMARY:Techniques numériques 15 CO_ECRIT \\n\nLOCATION:LABO\\n\nEND:VEVENT\n"
    "BEGIN:VEVENT\nDTSTART:20261113T101500\nDTEND:20261113T121500\nUID:1006\n"
    "SUMMARY:Eloquence-Argumentation niveau 2 8 CM (PREVISION)\\n\nEND:VEVENT\n"
    "END:VCALENDAR\n"
)


class ParserTests(unittest.TestCase):
    def test_school_format(self):
        cal = ics.parse(SAMPLE.encode())
        self.assertEqual(len(cal.events), 6)
        ev = {e.uid: e for e in cal.events}
        m = ev["1001"]
        self.assertEqual(m.title, "M2.1 : formation militaire complémentaire n°4")
        self.assertEqual(m.kind, "MILT")
        self.assertEqual(m.groups, "CL_25 / CM_25_SI")
        self.assertEqual(m.description, "M2.1\nTIR FA\nNRBC")
        self.assertEqual((m.start, m.end), (datetime(2026, 11, 16, 8), datetime(2026, 11, 25, 17, 45)))
        g = ev["1003"]
        self.assertEqual((g.title, g.session_no, g.kind, g.location, g.teachers),
                         ("Systèmes d'information géographique", "12", "TD_TP", "SALLE B", "MARTIN"))
        self.assertEqual(ev["1004"].title, "Anglais, Anglais bilangue et LV2")
        self.assertTrue(ev["1006"].tentative)
        self.assertEqual((ev["1006"].title, ev["1006"].session_no), ("Eloquence-Argumentation niveau 2", "8"))
        self.assertTrue(ics.is_exam(ev["1005"].kind, ev["1005"].title))

    def test_generic_summary_is_not_split(self):
        self.assertEqual(ics.parse_summary("Réunion avec IBM")["title"], "Réunion avec IBM")
        self.assertEqual(ics.parse_summary("Sport  SPORT")["kind"], "SPORT")

    def test_standard_ics(self):
        raw = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:a\r\nDTSTART:20260704T120000Z\r\nDTEND:20260704T130000Z\r\n"
               "SUMMARY:Déjeuner\\, équipe\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:b\r\nDTSTART;VALUE=DATE:20260705\r\n"
               "DTEND;VALUE=DATE:20260706\r\nSUMMARY:Congé\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:c\r\n"
               "DTSTART;TZID=Europe/Paris:20260706T090000\r\nDURATION:PT90M\r\nSUMMARY:Cours\r\nSTATUS:CANCELLED\r\n"
               "END:VEVENT\r\nEND:VCALENDAR\r\n")
        ev = {e.uid: e for e in ics.parse(raw).events}
        self.assertEqual(ev["a"].title, "Déjeuner, équipe")
        if ics._zone("Europe/Paris") is not None:  # base des fuseaux disponible
            self.assertEqual(ev["a"].start, datetime(2026, 7, 4, 14, 0))  # UTC -> heure de Paris (été)
        self.assertTrue(ev["b"].all_day)
        self.assertEqual(ev["c"].end - ev["c"].start, timedelta(minutes=90))
        self.assertTrue(ev["c"].cancelled)

    def test_not_a_calendar(self):
        with self.assertRaises(ics.ICSError):
            ics.parse(b"hello")


class AgendaTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hangar-test-")
        self.app = create_app(self.tmp, testing=True)
        self.ctx = self.app.test_request_context("/")
        self.ctx.push()
        self.user = User(username="moi", email="moi@hangar.local", role="admin", first_name="Moi",
                         contact_email="moi@exemple.fr")
        self.user.set_password("secret1")
        db.session.add(self.user)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ev(self, uid):
        return AgendaEvent.query.filter_by(user_id=self.user.id, uid=uid).one()


class ImportTests(AgendaTestCase):
    at = datetime(2026, 11, 9, 7, 0)

    def test_latest_file_wins(self):
        imp1 = A.import_calendar(self.user, SAMPLE, "s46.ics", at=self.at)
        self.assertEqual((imp1.added, imp1.modified, imp1.removed), (6, 0, 0))
        # Semaines couvertes : celles où le fichier apporte de vraies séances d'une journée (le bloc MILT qui
        # commence le 16 et la séance « (PREVISION) » ne font pas couvrir la semaine suivante)
        self.assertEqual((imp1.range_start, imp1.range_end), (date(2026, 11, 9), date(2026, 11, 15)))
        self.ev("1005").note = "calculatrice"
        db.session.commit()
        # Nouvel envoi : SIG déplacé et changé de salle, Anglais retiré, une séance ajoutée,
        # et le contrôle annulé dans le fichier.
        v2 = (SAMPLE.replace("DTSTART:20261110T133000", "DTSTART:20261110T140000")
              .replace("LOCATION:SALLE B", "LOCATION:SALLE C")
              .replace("UID:1004", "UID:2004"))
        imp2 = A.import_calendar(self.user, v2, "s46-v2.ics", at=self.at + timedelta(hours=1))
        self.assertEqual((imp2.added, imp2.modified, imp2.removed, imp2.unchanged), (1, 1, 1, 4))
        sig = self.ev("1003")
        self.assertEqual((sig.start, sig.location, sig.change_kind), (datetime(2026, 11, 10, 14), "SALLE C", "modified"))
        self.assertEqual(set(sig.changes), {"start", "location"})
        self.assertEqual(self.ev("1004").status, "removed")
        self.assertEqual(self.ev("2004").change_kind, "added")  # semaine déjà importée : à signaler
        self.assertEqual(self.ev("1005").note, "calculatrice")  # la note survit à l'import
        # Retour de la séance retirée
        imp3 = A.import_calendar(self.user, SAMPLE, "s46-v3.ics", at=self.at + timedelta(hours=2))
        self.assertEqual(imp3.restored, 1)
        self.assertEqual(self.ev("1004").status, "active")
        # Annuler le dernier import revient à l'état de l'import 2
        A.undo_import(imp3)
        self.assertEqual(self.ev("1004").status, "removed")
        self.assertEqual(self.ev("1003").location, "SALLE C")
        with self.assertRaises(ValueError):
            A.undo_import(imp1)  # pas le dernier

    def test_past_is_never_removed(self):
        A.import_calendar(self.user, SAMPLE, at=self.at)
        # Nouveau fichier (même période) sans les séances du 10 au 13, importé le jeudi 12
        v2 = re.sub(r"BEGIN:VEVENT\nDTSTART:2026111[0-3].*?END:VEVENT\n", "", SAMPLE, flags=re.S)
        imp = A.import_calendar(self.user, v2, at=datetime(2026, 11, 12, 9, 0))
        self.assertEqual(self.ev("1002").status, "active")   # toujours dans le fichier
        self.assertEqual(self.ev("1003").status, "active")   # mardi 10 : passé, jamais retiré
        self.assertEqual([self.ev(u).status for u in ("1004", "1005", "1006")], ["removed"] * 3)
        self.assertEqual(imp.removed, 3)
        # Un fichier qui ne couvre que la semaine suivante ne touche pas à cette semaine
        only_next = re.sub(r"BEGIN:VEVENT\nDTSTART:2026110.*?END:VEVENT\n", "", v2, flags=re.S)
        imp = A.import_calendar(self.user, only_next, at=datetime(2026, 11, 12, 9, 30))
        self.assertEqual((imp.range_start, imp.removed), (date(2026, 11, 16), 0))

    def test_merge_mode_removes_nothing(self):
        A.import_calendar(self.user, SAMPLE, at=self.at)
        imp = A.import_calendar(self.user, SAMPLE.replace("UID:1004", "UID:9999"), mode="merge", at=self.at)
        self.assertEqual((imp.added, imp.removed), (1, 0))
        self.assertEqual(self.ev("1004").status, "active")

    def test_week_view_layout(self):
        A.import_calendar(self.user, SAMPLE, at=self.at)
        s = A.settings_of(self.user)
        w = A.week_view(self.user, date(2026, 11, 11), s, at=self.at)
        self.assertEqual(w["ndays"], 5)
        thursday = w["days"][3]["blocks"]
        self.assertEqual([b.obj.uid for b in thursday], ["1004", "1005"])
        self.assertEqual(w["total_minutes"], 2 * 60 + 4 * 60 + 2 * 60 + 2 * 60 + 2 * 60)
        w2 = A.week_view(self.user, date(2026, 11, 18), s, at=self.at)
        self.assertEqual([(b["c0"], b["c1"], b["cont_right"]) for b in w2["bars"]], [(0, 4, True)])


class TaskAndReminderTests(AgendaTestCase):
    def test_recurring_task_moves_to_next_occurrence(self):
        t = AgendaTask(user_id=self.user.id, title="Footing", due_date=date(2026, 11, 11), recurrence="weekly")
        db.session.add(t)
        db.session.commit()
        nxt = A.complete_task(t, at=datetime(2026, 11, 11, 19, 0))
        self.assertEqual(nxt, date(2026, 11, 18))
        self.assertIsNone(t.done_at)
        occ = A.task_occurrences(self.user, date(2026, 11, 16), date(2026, 11, 30))
        self.assertEqual([(o.date, o.ghost) for o in occ],
                         [(date(2026, 11, 18), False), (date(2026, 11, 25), True)])
        self.assertEqual(A.next_date(date(2026, 1, 31), "monthly"), date(2026, 2, 28))
        self.assertEqual(A.next_date(date(2026, 11, 13), "weekdays"), date(2026, 11, 16))

    def test_task_reminder_is_emailed_once(self):
        t = AgendaTask(user_id=self.user.id, title="Rendre le TP", due_date=date(2026, 11, 12),
                       start_time=time(10, 0))
        db.session.add(t)
        db.session.flush()
        A.set_task_reminders(t, [60, 1440], "email", at=datetime(2026, 11, 10, 9, 0))
        db.session.commit()
        self.assertEqual(sorted(r.remind_at for r in t.reminders),
                         [datetime(2026, 11, 11, 10, 0), datetime(2026, 11, 12, 9, 0)])
        self.assertEqual(A.process_reminders(datetime(2026, 11, 11, 10, 0, 30)), 1)
        self.assertEqual(A.process_reminders(datetime(2026, 11, 11, 10, 1)), 0)
        mails = EmailMessage.query.filter_by(kind="reminder").all()
        self.assertEqual(len(mails), 1)
        self.assertIn("Rendre le TP", mails[0].subject)
        self.assertEqual(Notification.query.filter_by(kind="reminder").count(), 1)
        # Application fermée jusqu'après la tâche : le second rappel n'est plus envoyé
        self.assertEqual(A.process_reminders(datetime(2026, 11, 12, 12, 0)), 0)
        self.assertEqual({r.status for r in t.reminders}, {"sent", "missed"})

    def test_moved_event_reschedules_its_reminder(self):
        A.import_calendar(self.user, SAMPLE, at=datetime(2026, 11, 9, 7, 0))
        ev = self.ev("1003")
        r = A.add_event_reminder(ev, 30, at=datetime(2026, 11, 9, 7, 0))
        db.session.commit()
        self.assertEqual(r.remind_at, datetime(2026, 11, 10, 13, 0))
        A.import_calendar(self.user, SAMPLE.replace("DTSTART:20261110T133000", "DTSTART:20261110T150000"),
                          at=datetime(2026, 11, 9, 8, 0))
        self.assertEqual(db.session.get(AgendaReminder, r.id).remind_at, datetime(2026, 11, 10, 14, 30))

    def test_exam_auto_reminders(self):
        s = A.settings_of(self.user)
        s.exam_reminder = 1440
        db.session.commit()
        A.import_calendar(self.user, SAMPLE, at=datetime(2026, 11, 9, 7, 0))
        autos = AgendaReminder.query.filter_by(auto=True).all()
        self.assertEqual([(a.event.uid, a.remind_at) for a in autos], [("1005", datetime(2026, 11, 11, 10, 15))])
        s.exam_reminder = 0
        A.ensure_exam_reminders(self.user, at=datetime(2026, 11, 9, 8, 0))
        db.session.commit()
        self.assertEqual(AgendaReminder.query.filter_by(auto=True).count(), 0)

    def test_daily_program_email(self):
        A.import_calendar(self.user, SAMPLE, at=datetime(2026, 11, 9, 7, 0))
        s = A.settings_of(self.user)
        s.daily_mode, s.daily_hour = "evening", 20
        db.session.commit()
        self.assertEqual(A.send_daily_programs(datetime(2026, 11, 11, 19, 0)), 0)  # trop tôt
        self.assertEqual(A.send_daily_programs(datetime(2026, 11, 11, 20, 5)), 1)
        self.assertEqual(A.send_daily_programs(datetime(2026, 11, 11, 21, 0)), 0)  # une fois par jour
        mail = EmailMessage.query.filter_by(kind="agenda").one()
        self.assertIn("jeu. 12 nov.", mail.subject)
        self.assertIn("Techniques numériques", mail.body_html)

    def test_export_round_trip(self):
        A.import_calendar(self.user, SAMPLE, at=datetime(2026, 11, 9, 7, 0))
        out = ics.parse(A.export_ics(self.user))
        self.assertEqual(len(out.events), 6)


class RouteTests(AgendaTestCase):
    def test_pages_and_upload(self):
        c = self.app.test_client()
        page = c.get("/login").get_data(as_text=True)
        token = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
        r = c.post("/login", data={"login": "moi", "password": "secret1", "_csrf": token})
        self.assertEqual(r.headers["Location"], "/")
        import io
        r = c.post("/agenda/import", data={"_csrf": token, "file": (io.BytesIO(SAMPLE.encode()), "planning.ics")},
                   content_type="multipart/form-data")
        self.assertEqual(r.status_code, 302)
        for url in ("/", "/agenda/calendar?view=week&date=2026-11-12", "/agenda/calendar?view=month&date=2026-11-12",
                    "/agenda/calendar?view=list&date=2026-11-09", "/agenda/tasks", "/agenda/imports",
                    "/agenda/imports/1", "/agenda/settings", "/agenda/export.ics", "/home"):
            self.assertEqual(c.get(url).status_code, 200, url)
        r = c.post("/agenda/tasks", data={"_csrf": token, "title": "Réviser", "date": "2026-11-11", "start": "14:00",
                                          "reminders_present": "1", "reminders": ["60"]})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(AgendaTask.query.one().reminders[0].offset_minutes, 60)


if __name__ == "__main__":
    unittest.main()

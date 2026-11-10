"""Tests du résumé quotidien unifié (hangar/digest.py)."""
import shutil
import tempfile
import unittest
from datetime import date, datetime, time

from hangar import agenda as A
from hangar import create_app, digest, mailer
from hangar.extensions import db
from hangar.models import AgendaTask, EmailMessage, Issue, MailConfig, Project, ProjectMember, State, User


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hangar-digest-")
        self.app = create_app(self.tmp, testing=True)
        self.ctx = self.app.test_request_context("/")
        self.ctx.push()
        self.user = User(username="moi", email="moi@hangar.local", role="member", first_name="Moi",
                         contact_email="moi@exemple.fr")
        self.user.set_password("secret1")
        db.session.add(self.user)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _project_with_issue(self, due: date) -> Issue:
        p = Project(name="Projet pluri", identifier="PP", lead_id=self.user.id)
        db.session.add(p)
        db.session.flush()
        db.session.add(ProjectMember(project_id=p.id, user_id=self.user.id, role="member"))
        st = State(project_id=p.id, name="À faire", group="unstarted", color="#3f76ff")
        db.session.add(st)
        db.session.flush()
        i = Issue(project_id=p.id, sequence_id=1, title="Rendre la fiche de cadrage", state_id=st.id,
                  target_date=due, created_by_id=self.user.id)
        i.assignees.append(self.user)
        db.session.add(i)
        db.session.commit()
        return i

    def test_nothing_to_do_sends_nothing(self):
        self.assertIsNone(digest.construire(self.user, date(2026, 11, 12), datetime(2026, 11, 12, 7, 0)))
        self.assertEqual(A.send_daily_programs(datetime(2026, 11, 12, 7, 5)), 0)

    def test_tasks_and_tickets_in_one_email(self):
        self._project_with_issue(date(2026, 11, 12))
        db.session.add(AgendaTask(user_id=self.user.id, title="Réviser le contrôle", due_date=date(2026, 11, 12),
                                  start_time=time(18, 0)))
        db.session.add(AgendaTask(user_id=self.user.id, title="Payer la cotisation", due_date=date(2026, 11, 8)))
        db.session.commit()
        objet, html, sections = digest.construire(self.user, date(2026, 11, 12), datetime(2026, 11, 12, 7, 0))
        cles = [s.cle for s in sections]
        self.assertIn("agenda.taches", cles)
        self.assertIn("projets.tickets", cles)
        self.assertIn("Réviser le contrôle", html)
        self.assertIn("Payer la cotisation", html)          # en retard
        self.assertIn("Rendre la fiche de cadrage", html)   # ticket du jour
        self.assertIn("jeu. 12 nov.", objet)
        # Envoi à 7 h (réglage par défaut : le matin), une seule fois par jour
        self.assertEqual(A.send_daily_programs(datetime(2026, 11, 12, 6, 59)), 0)
        self.assertEqual(A.send_daily_programs(datetime(2026, 11, 12, 7, 1)), 1)
        self.assertEqual(A.send_daily_programs(datetime(2026, 11, 12, 9, 0)), 0)
        self.assertEqual(EmailMessage.query.filter_by(kind="agenda").count(), 1)

    def test_no_duplicate_ticket_reminder(self):
        self._project_with_issue(date(2026, 11, 12))
        cfg = MailConfig.get()
        self.assertEqual(mailer.send_reminders(cfg, date(2026, 11, 12)), 0)  # le résumé s'en charge
        s = A.settings_of(self.user)
        s.daily_mode = "off"
        db.session.commit()
        self.assertEqual(mailer.send_reminders(cfg, date(2026, 11, 12)), 1)

    def test_custom_section_and_failure_isolation(self):
        @digest.section("test.ok", ordre=99)
        def _ok(ctx):
            return digest.Section("test.ok", "Section de test", "<p>contenu</p>", compte=2, libelle="2 essais")

        @digest.section("test.ko", ordre=98)
        def _ko(ctx):
            raise RuntimeError("boum")

        try:
            objet, html, sections = digest.construire(self.user, date(2026, 11, 12), datetime(2026, 11, 12, 7, 0))
            self.assertIn("2 essais", objet)
            self.assertIn("Section de test", html)
        finally:
            digest._SECTIONS[:] = [s for s in digest._SECTIONS if not s[1].startswith("test.")]


if __name__ == "__main__":
    unittest.main()

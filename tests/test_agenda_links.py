"""Matières de l'emploi du temps reliées au wiki et aux QCM (suggestions, liens, date d'examen)."""
import json
import shutil
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from hangar import agenda as A
from hangar import create_app
from hangar.extensions import db
from hangar.models import AgendaEvent, AgendaSubjectLink, User

ICS = ("BEGIN:VCALENDAR\nX-WR-TIMEZONE:Europe/Paris\n"
       "BEGIN:VEVENT\nDTSTART:20261112T101500\nDTEND:20261112T121500\nUID:exam1\n"
       "SUMMARY:Techniques numériques 15 CO_ECRIT \\n\nDESCRIPTION:\n - Intervenants : Scipioni\\n\nEND:VEVENT\n"
       "BEGIN:VEVENT\nDTSTART:20261113T080000\nDTEND:20261113T100000\nUID:tsn1\n"
       "SUMMARY:Traitement du signal numériques et télécommunications numériques 13 TD_TP \\n\nDESCRIPTION:\n"
       " - Intervenants : SANTORI\\n\nEND:VEVENT\nEND:VCALENDAR\n")


class SubjectLinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hangar-links-")
        self.app = create_app(self.tmp, testing=True)
        self.app.config.update(WIKI_VERIF_INTERVALLE=0, QCM_VERIF_INTERVALLE=0)
        vault = Path(self.app.config["WIKI_DIR"])
        for rel, text in [("Cours/TechniquesNumeriques/00 - Sommaire TN.md", "# TN"),
                          ("Cours/TechniquesNumeriques/01 - Ohmmètre.md", "# Ohm"),
                          ("Cours/TSN-CLEAE2-NED/00 - Sommaire TSN.md", "# TSN"),
                          ("Cours/BDD/1.Introduction.md", "# BDD")]:
            (vault / rel).parent.mkdir(parents=True, exist_ok=True)
            (vault / rel).write_text(text, encoding="utf-8")
        qset = {"titre": "Ohm", "questions": [{"id": "q", "type": "QCM", "question": "U ?", "choix": ["RI", "R/I"],
                                               "reponses": [0]}]}
        for rel in ("QCM/TECHNIQUES_NUM/ohm.json", "QCM/TELECOM_SANTORI/tsn.json", "QCM/Linux/bash.json"):
            (vault / rel).parent.mkdir(parents=True, exist_ok=True)
            (vault / rel).write_text(json.dumps(qset), encoding="utf-8")
        self.ctx = self.app.test_request_context("/")
        self.ctx.push()
        self.user = User(username="moi", email="moi@hangar.local", role="member")
        self.user.set_password("secret1")
        db.session.add(self.user)
        db.session.commit()
        A.import_calendar(self.user, ICS, at=datetime(2026, 11, 10, 8, 0))

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ev(self, uid):
        return AgendaEvent.query.filter_by(uid=uid).one()

    def test_suggestions(self):
        self.assertEqual(A._similarity("Techniques numériques", "TechniquesNumeriques"), 100)
        tn = A.subject_links(self.user, self.ev("exam1"))
        self.assertEqual(tn["wikiSuggest"][0], "Cours/TechniquesNumeriques")
        self.assertEqual(tn["qcmSuggest"][0], "TECHNIQUES_NUM")
        tsn = A.subject_links(self.user, self.ev("tsn1"))
        self.assertIn("Cours/TSN-CLEAE2-NED", tsn["wikiSuggest"])           # sigle TSN
        self.assertEqual(tsn["qcmSuggest"][0], "TELECOM_SANTORI")          # nom de l'intervenant
        self.assertIn("Cours/BDD", tsn["wikiAll"])

    def test_link_and_exam_objective(self):
        from hangar.qcm.models import QuizDeck
        ev = self.ev("exam1")
        A.set_subject_link(self.user, ev.title, wiki_path="Cours/TechniquesNumeriques", qcm_category="TECHNIQUES_NUM")
        db.session.commit()
        links = A.subject_links(self.user, ev)
        self.assertEqual(links["wiki"]["label"], "TechniquesNumeriques")
        self.assertIn("Sommaire", links["wiki"]["url"])                     # page sommaire de la collection
        self.assertEqual(links["qcm"]["label"], "TECHNIQUES_NUM")
        self.assertEqual(A.set_exam_objective(self.user, ev), 1)
        self.assertEqual(QuizDeck.query.one().date_objectif, date(2026, 11, 12))
        self.assertEqual(AgendaSubjectLink.query.count(), 1)

    def test_routes(self):
        import re
        from flask import g
        g.pop("_login_user", None)
        c = self.app.test_client()
        token = re.search(r'name="_csrf" value="([^"]+)"', c.get("/login").get_data(as_text=True)).group(1)
        c.post("/login", data={"login": "moi", "password": "secret1", "_csrf": token})
        ev = self.ev("exam1")
        r = c.post(f"/agenda/events/{ev.id}/link", data={"_csrf": token, "qcm_category": "TECHNIQUES_NUM"})
        self.assertEqual(r.status_code, 302)
        r = c.post(f"/agenda/events/{ev.id}/exam", data={"_csrf": token})
        self.assertEqual(r.status_code, 302)
        page = c.get("/agenda/calendar?view=week&date=2026-11-12").get_data(as_text=True)
        self.assertIn("TECHNIQUES_NUM", page)


if __name__ == "__main__":
    unittest.main()

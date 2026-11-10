"""Parcours de toutes les pages sur les données de démonstration (projets, tickets, cycles, modules,
bilans, réglages…) : aucune ne doit renvoyer d'erreur 500. Et les commandes en ligne écrivent en UTF-8
même quand leur sortie est redirigée dans un encodage limité (cp1252 sous Windows)."""
import io
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout

from hangar import cli, create_app
from hangar.extensions import db
from hangar.models import AgendaTask, Cycle, Issue, Module, Page, Project, SavedView, User


class PagesSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="hangar-smoke-")
        cls.app = create_app(cls.tmp, testing=True)
        with cls.app.test_request_context("/"):
            from hangar.seed import seed_demo
            seed_demo(None, verbose=False)
            p = Project.query.filter_by(identifier="EAE").first() or Project.query.first()
            cls.values = {
                "pid": p.id,
                "seq": Issue.query.filter_by(project_id=p.id).order_by(Issue.sequence_id).first().sequence_id,
                "cid": Cycle.query.filter_by(project_id=p.id).first().id,
                "mid": Module.query.filter_by(project_id=p.id).first().id,
                "page_id": Page.query.filter_by(project_id=p.id).first().id,
                "vid": db.session.query(SavedView).filter_by(project_id=p.id).first().id,
                "username": "wacogne",
                "key": Issue.query.filter_by(project_id=p.id).first().key,
                "fmt": "csv",
            }
            u = User.query.filter_by(username="wacogne").one()
            db.session.add(AgendaTask(user_id=u.id, title="Tâche de test"))
            db.session.commit()

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            db.session.remove()
            db.engine.dispose()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _client(self):
        c = self.app.test_client()
        token = re.search(r'name="_csrf" value="([^"]+)"', c.get("/login").get_data(as_text=True)).group(1)
        r = c.post("/login", data={"login": "wacogne", "password": "hangar2026", "_csrf": token})
        self.assertEqual(r.status_code, 302)
        return c

    def test_every_get_page(self):
        """Chaque route GET dont les paramètres sont connus : jamais de 500."""
        c = self._client()
        urls = set()
        with self.app.test_request_context("/"):
            from flask import url_for
            for rule in self.app.url_map.iter_rules():
                if "GET" not in rule.methods or rule.endpoint == "static" or rule.endpoint.startswith("auth."):
                    continue
                if rule.endpoint in ("ai.chat_stream",) or "logout" in rule.endpoint:
                    continue
                if not set(rule.arguments) <= set(self.values):
                    continue
                try:
                    urls.add(url_for(rule.endpoint, **{a: self.values[a] for a in rule.arguments}))
                except Exception:  # noqa: BLE001 - convertisseur incompatible (ex. type de paramètre)
                    continue
        pid = self.values["pid"]
        for layout in ("list", "board", "spreadsheet", "calendar", "gantt"):
            urls.add(f"/projects/{pid}/issues?layout={layout}")
            urls.add(f"/issues?layout={layout}")
        for view in ("week", "month", "list"):
            urls.add(f"/agenda/calendar?view={view}")
        self.assertGreater(len(urls), 80)
        errors = []
        for url in sorted(urls):
            r = c.get(url)
            if r.status_code >= 500:
                errors.append(f"{r.status_code} {url}")
        self.assertEqual(errors, [], "pages en erreur")

    def test_ticket_workflow(self):
        """Créer un ticket, le commenter, changer son état : les vues principales restent en état de marche."""
        c = self._client()
        token = re.search(r'name="_csrf" value="([^"]+)"', c.get("/home").get_data(as_text=True)).group(1)
        pid = self.values["pid"]
        r = c.post("/api/issues/quick", json={"project_id": pid, "title": "Ticket du test de parcours"},
                   headers={"X-CSRF-Token": token})
        self.assertLess(r.status_code, 400, r.get_data(as_text=True)[:300])
        with self.app.app_context():
            issue = Issue.query.filter_by(title="Ticket du test de parcours").one()
            seq, iid = issue.sequence_id, issue.id
        r = c.post(f"/projects/{pid}/issues/{seq}/comments", data={"_csrf": token, "body": "Commentaire **markdown**"})
        self.assertLess(r.status_code, 400)
        r = c.post(f"/api/issues/{iid}", json={"priority": "high"}, headers={"X-CSRF-Token": token})
        self.assertLess(r.status_code, 400, r.get_data(as_text=True)[:300])
        for url in (f"/projects/{pid}/issues/{seq}", f"/projects/{pid}/issues", "/home", "/bilan", "/analytics"):
            self.assertEqual(c.get(url).status_code, 200, url)


class CliEncodingTests(unittest.TestCase):
    def test_redirected_output_in_cp1252(self):
        tmp = tempfile.mkdtemp(prefix="hangar-cli-")
        try:
            ics = f"{tmp}/planning.ics"
            with open(ics, "w", encoding="utf-8") as fh:
                fh.write("BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:1\nDTSTART:20261112T080000\nDTEND:20261112T100000\n"
                         "SUMMARY:Cours  CM \\n\nEND:VEVENT\nEND:VCALENDAR\n")
            app = create_app(f"{tmp}/data", testing=True)
            with app.app_context():
                u = User(username="moi", email="moi@hangar.local", role="admin")
                u.set_password("secret1")
                db.session.add(u)
                db.session.commit()
                db.session.remove()
                db.engine.dispose()
            raw = io.BytesIO()
            stream = io.TextIOWrapper(raw, encoding="cp1252")  # sortie redirigée sous Windows
            with redirect_stdout(stream):
                cli.main(["--data", f"{tmp}/data", "agenda-import", ics])  # affiche « → »
                stream.flush()
            self.assertIn("→", raw.getvalue().decode("utf-8"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

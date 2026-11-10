"""La protection CSRF ne se lève que pour une requête portant un jeton d'appareil valide, sur les adresses
prévues pour l'application, ou pour la connexion de l'application (qui n'ouvre pas de session navigateur)."""
import json
import re
import shutil
import tempfile
import unittest

from hangar import create_app
from hangar.extensions import db
from hangar.models import User


class CsrfTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hangar-csrf-")
        self.app = create_app(self.tmp, testing=True)
        with self.app.app_context():
            u = User(username="eleve", email="eleve@hangar.local", role="member")
            u.set_password("secret1")
            db.session.add(u)
            db.session.commit()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _session_client(self):
        c = self.app.test_client()
        token = re.search(r'name="_csrf" value="([^"]+)"', c.get("/login").get_data(as_text=True)).group(1)
        self.assertEqual(c.post("/login", data={"login": "eleve", "password": "secret1", "_csrf": token}).status_code, 302)
        return c

    def _device_token(self):
        r = self.app.test_client().post("/api/appareil/connexion/",
                                        data=json.dumps({"username": "eleve", "password": "secret1"}),
                                        content_type="application/json")
        self.assertEqual(r.status_code, 200)
        return r.get_json()["jeton"]

    def test_cookie_session_still_needs_csrf(self):
        c = self._session_client()
        self.assertEqual(c.post("/agenda/tasks", data={"title": "x"}).status_code, 400)
        self.assertEqual(c.post("/qcm/hors-ligne/synchro/", data="{}", content_type="application/json").status_code, 400)

    def test_device_token_exempts_only_allowed_paths(self):
        jeton = self._device_token()
        c = self.app.test_client()
        auth = {"Authorization": f"Bearer {jeton}"}
        r = c.post("/qcm/hors-ligne/synchro/", data=json.dumps({"reponses": []}), content_type="application/json",
                   headers=auth)
        self.assertNotEqual(r.status_code, 400)
        self.assertLess(r.status_code, 500)
        self.assertEqual(c.post("/agenda/tasks", data={"title": "x"}, headers=auth).status_code, 403)
        self.assertEqual(c.post("/qcm/hors-ligne/synchro/", data="{}", content_type="application/json",
                                headers={"Authorization": "Bearer faux"}).status_code, 401)
        self.assertNotIn("Set-Cookie", r.headers)  # une requête de l'appli ne crée pas de session


if __name__ == "__main__":
    unittest.main()

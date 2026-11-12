"""Emploi du temps récupéré tout seul (source automatique) : corrections de la revue v3.

- une panne pendant l'import ne marque plus le fichier comme lu : il est réimporté au passage suivant, et
  l'erreur est visible dans « source_error » ; « Lire maintenant » (force=True) ignore l'empreinte ;
- la lecture du fil d'arrière-plan ne vaut que pour un compte encore administrateur (un compte rétrogradé
  peut couper sa source) ; la lecture d'une adresse a une durée totale bornée ;
- un chemin Windows collé entre guillemets, ou écrit avec %USERPROFILE% / ~, est accepté comme pour le
  coffre du wiki.

Lancer : uv run python -m unittest tests.test_agenda_source -v
"""
import http.server
import os
import threading
import time as _time
import unittest
from datetime import datetime, time, timedelta
from unittest import mock

from sqlalchemy.exc import OperationalError

from hangar import agenda as A
from hangar import heure as horloge
from hangar.agenda import source as S
from hangar.extensions import db
from hangar.models import AgendaEvent, AgendaImport, AgendaSettings
from test_agenda_flux import Base, monday_after, week_file


class SourceV3Tests(Base):
    def setUp(self):
        super().setUp()
        self.monday = monday_after(horloge.today())
        self.at = datetime.combine(self.monday - timedelta(days=2), time(9))
        self.folder = os.path.join(self.tmp, "Telechargements")
        os.makedirs(self.folder)

    def drop(self, name, raw, age=0):
        path = os.path.join(self.folder, name)
        with open(path, "wb") as fh:
            fh.write(raw)
        t = _time.time() - 1000 + age
        os.utime(path, (t, t))
        return path

    def configure(self, kind, where, user=None):
        s = A.settings_of(user or self.user)
        s.source_kind, s.source_path = kind, where
        db.session.commit()

    def promote(self):
        self.user.role = "admin"
        db.session.commit()

    def active(self):
        return AgendaEvent.query.filter_by(user_id=self.user.id, status="active").count()

    # -- Constat 0 : empreinte validée seulement après un import réussi -----------------------------------

    def test_panne_pendant_import_le_fichier_est_relu(self):
        self.promote()
        self.configure("folder", self.folder)
        self.drop("planning.ics", week_file(self.monday))
        panne = OperationalError("UPDATE", {}, Exception("database is locked"))
        with mock.patch.object(A, "ensure_exam_reminders", side_effect=panne):
            self.assertEqual(A.check_sources(self.at), 0)
        self.assertEqual((AgendaImport.query.count(), self.active()), (0, 0))
        s = db.session.get(AgendaSettings, self.user.id)
        self.assertEqual((s.source_fingerprint, s.source_seen), ("", ""))
        self.assertIn("import", s.source_error)  # erreur visible sur la page
        # Passage suivant, sans panne : le fichier est importé
        self.assertEqual(A.check_source(self.user, self.at + timedelta(seconds=30)), "imported")
        self.assertEqual((AgendaImport.query.count(), self.active()), (1, 4))
        self.assertEqual(db.session.get(AgendaSettings, self.user.id).source_error, "")

    def test_panne_pendant_import_depuis_la_page(self):
        self.promote()
        self.configure("folder", self.folder)
        self.drop("planning.ics", week_file(self.monday))
        c, csrf = self.login()
        with mock.patch.object(A, "ensure_exam_reminders", side_effect=RuntimeError("boum")):
            r = c.post("/agenda/source/check", data={"_csrf": csrf})
        self.assertEqual(r.status_code, 302)  # pas d'erreur 500 : le message est sur la page
        page = c.get("/agenda/imports").get_data(as_text=True)
        self.assertIn("Lecture impossible", page)
        self.assertNotIn("rien de nouveau", page)
        r = c.post("/agenda/source/check", data={"_csrf": csrf})
        self.assertEqual(AgendaImport.query.count(), 1)

    def test_lire_maintenant_ignore_l_empreinte(self):
        self.promote()
        self.configure("folder", self.folder)
        self.drop("planning.ics", week_file(self.monday))
        self.assertEqual(A.check_source(self.user, self.at), "imported")
        A.undo_import(A.latest_import(self.user), self.at + timedelta(minutes=1))
        self.assertEqual(self.active(), 0)
        # Passage automatique : même fichier, rien de nouveau
        self.assertEqual(A.check_source(self.user, self.at + timedelta(minutes=2)), "same")
        # « Lire maintenant » : le fichier est réimporté malgré l'empreinte
        self.assertEqual(A.check_source(self.user, self.at + timedelta(minutes=3), force=True), "imported")
        self.assertEqual(self.active(), 4)

    # -- Constat 12 : droit d'administrateur à la lecture, durée totale bornée ---------------------------

    def test_lecture_reservee_aux_administrateurs(self):
        self.promote()
        self.configure("folder", self.folder)
        self.user.role = "member"  # administrateur rétrogradé
        db.session.commit()
        self.drop("planning.ics", week_file(self.monday))
        with mock.patch.object(S, "_fetch_url") as fetch:
            self.assertEqual(A.check_sources(self.at), 0)
        fetch.assert_not_called()
        self.assertEqual(AgendaImport.query.count(), 0)
        self.user.role = "admin"
        db.session.commit()
        self.assertEqual(A.check_sources(self.at + timedelta(minutes=1)), 1)

    def test_compte_retrograde_peut_couper_sa_source(self):
        self.configure("url", "http://192.168.1.1/planning.ics")
        c, csrf = self.login()
        r = c.post("/agenda/source", data={"_csrf": csrf, "source_kind": "url",
                                           "source_path": "http://192.168.1.2/planning.ics"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(db.session.get(AgendaSettings, self.user.id).source_path, "http://192.168.1.1/planning.ics")
        page = c.get("/agenda/imports").get_data(as_text=True)
        self.assertIn("Couper la récupération automatique", page)
        r = c.post("/agenda/source", data={"_csrf": csrf, "source_kind": "", "source_path": ""})
        self.assertEqual(r.status_code, 302)
        s = db.session.get(AgendaSettings, self.user.id)
        self.assertEqual((s.source_kind, s.source_path), ("", ""))

    def test_lecture_d_une_adresse_a_une_duree_totale_bornee(self):
        class Lent(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "text/calendar")
                self.send_header("Content-Length", "100")
                self.end_headers()
                try:
                    for _ in range(100):  # un octet toutes les 0,1 s : 10 s en tout
                        self.wfile.write(b"x")
                        self.wfile.flush()
                        _time.sleep(0.1)
                except OSError:
                    pass

            def log_message(self, *a):
                pass

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Lent)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{srv.server_address[1]}/planning.ics"
            with mock.patch.object(S, "SOURCE_DEADLINE", 1), mock.patch.object(S, "SOURCE_TIMEOUT", 2), \
                    mock.patch.dict(os.environ, {"no_proxy": "*", "NO_PROXY": "*"}):
                t0 = _time.monotonic()
                with self.assertRaises(TimeoutError):
                    S._fetch_url(url)
                self.assertLess(_time.monotonic() - t0, 5)
        finally:
            srv.shutdown()
            srv.server_close()

    # -- Constat 21 : chemin Windows collé entre guillemets, variables d'environnement ------------------

    def save_folder(self, path):
        self.promote()
        c, csrf = self.login()
        r = c.post("/agenda/source", data={"_csrf": csrf, "source_kind": "folder", "source_path": path})
        self.assertEqual(r.status_code, 302)
        return c, db.session.get(AgendaSettings, self.user.id)

    def test_dossier_colle_entre_guillemets(self):
        c, s = self.save_folder(f'  "{self.folder}"  ')
        self.assertEqual(s.source_kind, "folder")
        self.assertEqual(os.path.normcase(s.source_path), os.path.normcase(os.path.realpath(self.folder)))
        self.assertNotIn("introuvable", c.get("/agenda/imports").get_data(as_text=True))

    def test_dossier_avec_variable_d_environnement(self):
        var = "%HANGAR_TEST_DL%" if os.name == "nt" else "$HANGAR_TEST_DL"
        with mock.patch.dict(os.environ, {"HANGAR_TEST_DL": self.tmp}):
            c, s = self.save_folder(os.path.join(var, "Telechargements"))
        self.assertEqual(s.source_kind, "folder")
        self.assertEqual(os.path.normcase(s.source_path), os.path.normcase(os.path.realpath(self.folder)))

    def test_dossier_avec_tilde(self):
        home = {"USERPROFILE": self.tmp, "HOME": self.tmp}
        with mock.patch.dict(os.environ, home):
            c, s = self.save_folder(os.path.join("~", "Telechargements"))
        self.assertEqual(os.path.normcase(s.source_path), os.path.normcase(os.path.realpath(self.folder)))

    def test_guillemets_seuls_ne_designent_pas_le_dossier_courant(self):
        c, s = self.save_folder('""')
        self.assertEqual((s.source_kind, s.source_path), ("", ""))

    def test_dossier_introuvable_toujours_refuse(self):
        c, s = self.save_folder(f'"{os.path.join(self.tmp, "absent")}"')
        self.assertEqual((s.source_kind, s.source_path), ("", ""))
        self.assertIn("introuvable", c.get("/agenda/imports").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()

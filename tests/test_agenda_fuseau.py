"""Vague 3b, groupe « agenda-fuseau » : pistes 70 (socle de create_app, paquet agenda) et 71 (fuseau horaire)."""
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent


def _python(code: str, env: dict | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
    """Lance `code` dans un interpréteur neuf (aucun module de Hangar déjà importé par un autre test)."""
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code)], cwd=RACINE, env=e, capture_output=True,
                          text=True, timeout=timeout)


# ---------------------------------------------------------------------------
# Piste 70 : create_app ne dépend plus d'un import caché du module QCM
# ---------------------------------------------------------------------------

class SocleCreateAppTests(unittest.TestCase):
    def test_base_neuve_dans_un_processus_neuf_a_les_tables_de_connexion(self):
        """Sur une base neuve, les tables du blocage des connexions et des jetons d'appareil existent dès le
        premier create_app, sans compter sur un autre test (ou un autre module) pour les importer."""
        with tempfile.TemporaryDirectory() as d:
            r = _python("""
                from sqlalchemy import inspect
                from hangar import create_app
                from hangar.extensions import db
                app = create_app(%r, testing=True)
                with app.app_context():
                    tables = set(inspect(db.engine).get_table_names())
                print("connexion_echec" in tables, "jeton_appareil" in tables, "appareils" in app.blueprints)
                # le crochet du jeton d'appareil passe avant la vérification CSRF du socle
                noms = [f.__name__ for f in app.before_request_funcs[None]]
                print(noms.index("_avant") < noms.index("_setup_and_csrf"))
            """ % d)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.split(), ["True", "True", "True", "True"])

    def test_create_app_importe_lui_meme_appareils_et_blocage(self):
        """Ni le blueprint « appareils » ni ses tables ne passent plus par le module QCM : retirer un jour
        l'import du module QCM ne casse plus la connexion sur une base neuve."""
        import re
        qcm_models = (RACINE / "hangar" / "qcm" / "models.py").read_text(encoding="utf-8")
        qcm_views = (RACINE / "hangar" / "qcm" / "views.py").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"^from \.\. import .*(appareils|blocage_connexion)", qcm_models, re.M))
        self.assertFalse("register_blueprint" in qcm_views)
        with tempfile.TemporaryDirectory() as d:
            # On empêche le module QCM d'importer quoi que ce soit d'autre : les tables viennent de create_app.
            r = _python("""
                import builtins
                vrai = builtins.__import__
                def garde(name, globals=None, locals=None, fromlist=(), level=0):
                    appelant = (globals or {}).get("__name__", "")
                    if appelant.startswith("hangar.qcm") and fromlist and (
                            "appareils" in fromlist or "blocage_connexion" in fromlist):
                        raise ImportError("import interdit depuis le module QCM")
                    return vrai(name, globals, locals, fromlist, level)
                builtins.__import__ = garde
                from sqlalchemy import inspect
                from hangar import create_app
                from hangar.extensions import db
                app = create_app(%r, testing=True)
                with app.app_context():
                    tables = set(inspect(db.engine).get_table_names())
                print("connexion_echec" in tables, "jeton_appareil" in tables)
            """ % d)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.split(), ["True", "True"])

    def test_gabarit_inutilise_supprime(self):
        self.assertFalse((RACINE / "hangar" / "templates" / "ai" / "placeholder.html").exists())

    def test_variables_jinja_declarees_une_seule_fois(self):
        """Les variables communes des gabarits sont des globales Jinja (visibles aussi des macros importées
        sans contexte) ; le context_processor n'ajoute que ce qui dépend de la requête."""
        import shutil
        from hangar import create_app
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        app = create_app(d, testing=True)
        g = app.jinja_env.globals
        for nom in ("icon", "csrf_token", "M", "IQ", "version", "can", "greeting", "markdown_toc"):
            self.assertIn(nom, g)
        with app.test_request_context("/"):
            ctx = {}
            for fn in app.template_context_processors[None]:
                ctx.update(fn())
        for nom in ("icon", "csrf_token", "M", "IQ", "version", "can", "greeting", "markdown_toc", "charts"):
            self.assertNotIn(nom, ctx)
        for nom in ("today", "qs", "can_delete_issues"):
            self.assertIn(nom, ctx)


# ---------------------------------------------------------------------------
# Piste 70 : hangar/agenda devient un paquet, mêmes noms qu'avant
# ---------------------------------------------------------------------------

class PaquetAgendaTests(unittest.TestCase):
    def test_paquet_et_noms_reexportes(self):
        from hangar import agenda as A
        self.assertTrue(hasattr(A, "__path__"), "hangar.agenda doit être un paquet")
        for nom in ("import_calendar", "undo_import", "week_view", "month_view", "list_view", "dashboard",
                    "run_jobs", "export_ics", "process_reminders", "send_reminder", "send_daily_programs",
                    "check_source", "check_sources", "subject_links", "_subject_link", "_mail", "now",
                    "IMPORT_MODES", "RECURRENCES", "REMINDER_OFFSETS", "TASK_GROUP_LABELS", "SOURCE_KINDS",
                    "DAYS", "AgendaTask", "ics", "is_exam", "hm", "feed_user", "regenerate_feed_token"):
            self.assertTrue(hasattr(A, nom), nom)
        # le nom d'une fonction n'est jamais masqué par celui d'un module du paquet
        self.assertTrue(callable(A.export_ics))

    def test_remplacer_agenda_now_agit_dans_les_modules(self):
        """`mock.patch.object(agenda, "now")` agit encore sur tout l'agenda, comme avant le découpage."""
        import shutil
        from datetime import date, datetime, time
        from unittest import mock

        from hangar import agenda as A
        from hangar import create_app
        from hangar.extensions import db
        from hangar.models import AgendaImport, AgendaTask, User
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        app = create_app(d, testing=True)
        with app.test_request_context("/"):
            u = User(username="moi", email="moi@hangar.local", role="admin")
            u.set_password("secret1")
            db.session.add(u)
            db.session.flush()
            imp = AgendaImport(user_id=u.id, filename="a.ics", created_at=datetime(2020, 1, 1, 9))
            t = AgendaTask(user_id=u.id, title="Rendu", due_date=date(2020, 1, 3), start_time=time(10))
            db.session.add_all([imp, t])
            db.session.commit()
            self.assertEqual(A.recent_import_ids(u), set())  # vraie horloge : import trop ancien
            with mock.patch.object(A, "now", return_value=datetime(2020, 1, 2, 9)):
                self.assertEqual(A.recent_import_ids(u), {imp.id})  # imports.py
                A.set_task_reminders(t, [60], "app")  # rappels.py : rappel à 9 h, pas encore passé
                self.assertEqual([r.remind_at for r in t.reminders], [datetime(2020, 1, 3, 9)])
                self.assertEqual(A.dashboard(u)["today"], date(2020, 1, 2))  # vues.py
            db.session.remove()
            db.engine.dispose()


if __name__ == "__main__":
    unittest.main()

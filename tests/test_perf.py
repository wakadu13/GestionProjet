"""Performances : simulation du planning des QCM, accueil et calendrier de l'agenda, pages QCM,
tableau kanban et bilan (pistes 28 et 57 de l'audit).

Les tests comptent des appels et des requêtes SQL, jamais des durées.
Lancer : uv run python -m unittest tests.test_perf -v
"""
import json
import random
import re
import shutil
import tempfile
import unittest
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

from sqlalchemy import event

from hangar import create_app
from hangar.extensions import db
from hangar.models import User
from hangar.qcm import fsrs, planning, services


@contextmanager
def requetes_sql(app):
    """Liste des requêtes SQL exécutées dans le bloc."""
    vues = []

    def compter(conn, cursor, statement, parameters, context, executemany):
        vues.append(statement)
    with app.app_context():
        moteur = db.engine
    event.listen(moteur, "before_cursor_execute", compter)
    try:
        yield vues
    finally:
        event.remove(moteur, "before_cursor_execute", compter)


def sur_table(requetes, table):
    return [r for r in requetes if re.search(rf"\bFROM {table}\b", r)]


def connexion(app, login, mot_de_passe):
    c = app.test_client()
    token = re.search(r'name="_csrf" value="([^"]+)"', c.get("/login").get_data(as_text=True)).group(1)
    r = c.post("/login", data={"login": login, "password": mot_de_passe, "_csrf": token})
    assert r.status_code == 302, r.status_code
    return c, token


# ---------------------------------------------------------------------------
# Piste 28 : simulation du planning (arrêt anticipé, mêmes résultats)
# ---------------------------------------------------------------------------
def _sans_arret():
    """La simulation complète, sans arrêt anticipé (référence)."""
    return mock.patch.object(planning, "PLATEAU_JOURS", planning.HORIZON_JOURS + 1)


class SimulationTests(unittest.TestCase):
    def _compter_revisions(self, *args):
        with mock.patch.object(fsrs, "suivante", wraps=fsrs.suivante) as espion:
            res = planning.simuler(*args)
        return res, espion.call_count

    def test_precision_faible_arret_anticipe(self):
        """Précision de 60 % : les questions ne se stabilisent jamais. Avant, les 365 jours étaient
        simulés à chaque affichage ; la simulation s'arrête maintenant bien plus tôt, même réponse."""
        cartes = [(None, 0, 0)] * 60
        for quotas in ((60, 60), (5, 60)):
            res, revisions = self._compter_revisions(cartes, *quotas, 0.6)
            with _sans_arret():
                res_complet, revisions_complet = self._compter_revisions(cartes, *quotas, 0.6)
            self.assertIsNone(res_complet)
            self.assertEqual(res, res_complet)
            self.assertLess(revisions * 3, revisions_complet, quotas)

    def test_quota_de_revisions_atteint_pas_d_arret(self):
        """Quand le quota de révisions coupe la file, les reports peuvent encore faire progresser :
        pas d'arrêt anticipé. Ici, 85 jours de suite sans progrès, puis le set est prêt au bout de
        337 jours : un simple « 60 jours sans progrès » aurait répondu « impossible »."""
        cartes = [(None, 0, 0)] * 40
        with _sans_arret():
            self.assertEqual(planning.simuler(cartes, 5, 5, 0.6), (337, 1714))
        self.assertEqual(planning.simuler(cartes, 5, 5, 0.6), (337, 1714))

    def test_cas_limites_meme_resultat(self):
        """Séries de réussites rares, ou coupure tardive de la file : la question finit par passer
        après un long plateau."""
        e = fsrs.Etat
        cas = [
            ([(None, 0, 0)] * 8 + [(e(2.4107104692493264, 9.540903739485554), 1, 17.415115073771034), (None, 0, 0)],
             30, 10, 0.7556464773463157, (0, 0), (101, 282)),
            ([(e(5.379445671145284, 4.659454164756079), 5, 40.28336654608051),
              (e(18.20262212829123, 6.82670462674777), 20, 83.85495256195784),
              (e(52.03978903046562, 5.597832333836343), 0, 58.26223820991451),
              (e(0.2512849874595795, 8.616236025464294), 5, 84.38909694002035),
              (e(2.4519180306139434, 9.488681539385915), 1, 12.092931896309999),
              (e(8.59235303454718, 1.1059801478424562), 0, 46.70560581907942),
              (e(6.146653936911784, 7.048030620738992), 2, 4.511931651197715),
              (e(8.765091553001227, 1.9835456063590526), 1, 50.19048762268279),
              (e(4.963498417418111, 7.020296311542694), 0, 7.493844039969134),
              (e(5.261589355377518, 6.430611810340095), 0, 89.44560380715939)],
             2, 3, 0.7613866992594712, (5, 0), (90, 112)),
            # Quota d'une révision par jour : la file n'est pas coupée pendant plus de 60 jours,
            # puis une coupure tardive reporte une révision et la question finit par passer.
            ([(e(0.5, 6.719303045407995), 3, 10.444612730180175), (None, 0, 0),
              (e(3, 6.458617774599349), 1, 11.588412328209541)],
             3, 1, 0.72, (0, 0), (325, 215)),
            ([(e(0.5, 6.719303045407995), 3, 10.444612730180175), (None, 0, 0),
              (e(3, 6.458617774599349), 1, 11.588412328209541)],
             3, 1, 0.72, (1, 2), (325, 215)),
        ]
        for cartes, qn, qr, p, deja, attendu in cas:
            with _sans_arret():
                self.assertEqual(planning.simuler(cartes, qn, qr, p, deja), attendu)
            self.assertEqual(planning.simuler(cartes, qn, qr, p, deja), attendu)

    def test_tirages_meme_resultat(self):
        """Tirages au hasard (états FSRS, échéances, quotas, précision) : même résultat qu'avec la
        simulation complète (vérifié hors des tests sur 23 000 tirages et une base d'un an)."""
        rng = random.Random(28)
        for k in range(150):
            taille = rng.choice([1, 4, 12, 30])
            part_neuves = rng.random()
            cartes = []
            for _ in range(taille):
                if rng.random() < part_neuves:
                    cartes.append((None, 0, 0))
                else:
                    st = rng.choice([0.3, 1.0, 2.0, 3.5, 6.9, 7.0, 20.0]) * rng.uniform(0.7, 1.3)
                    cartes.append((fsrs.Etat(st, rng.uniform(1, 10)), rng.choice([0, 0, 1, 3, 15]), rng.uniform(0, 60)))
            p = rng.uniform(0.6, 0.97)
            qn, qr = rng.choice([taille, 1, 3, 10]), rng.choice([taille, taille, 3, 10])
            deja = (rng.choice([0, 0, 2]), rng.choice([0, 0, 5]))
            with _sans_arret():
                attendu = planning.simuler(cartes, qn, qr, p, deja)
            self.assertEqual(planning.simuler(cartes, qn, qr, p, deja), attendu, (k, taille, qn, qr, p, deja))


# ---------------------------------------------------------------------------
# Piste 28 : accueil, calendrier et pages QCM
# ---------------------------------------------------------------------------
SET = {"titre": "Ohm", "questions": [
    {"id": f"q{i}", "type": "QCM", "question": f"Question {i}", "choix": ["a", "b"], "reponses": [0]} for i in range(12)]}


class PagesQcmAgendaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hangar-perf-")
        self.app = create_app(self.tmp, testing=True)
        self.app.config.update(WIKI_VERIF_INTERVALLE=0, QCM_VERIF_INTERVALLE=0)
        vault = Path(self.app.config["WIKI_DIR"])
        for rel in ("Cours/TechniquesNumeriques/00 - Sommaire TN.md", "Cours/BDD/1.Introduction.md"):
            (vault / rel).parent.mkdir(parents=True, exist_ok=True)
            (vault / rel).write_text("# Cours", encoding="utf-8")
        self.sets = ["QCM/TECHNIQUES_NUM/ohm1", "QCM/TECHNIQUES_NUM/ohm2", "QCM/Linux/bash"]
        for sid in self.sets:
            (vault / (sid + ".json")).parent.mkdir(parents=True, exist_ok=True)
            (vault / (sid + ".json")).write_text(json.dumps(SET), encoding="utf-8")
        # Séances de la semaine en cours (tableau de bord et vue semaine), dont un contrôle
        lundi = date.today() - timedelta(days=date.today().weekday())
        lignes = ["BEGIN:VCALENDAR", "X-WR-TIMEZONE:Europe/Paris"]
        for j in range(5):
            jour = lundi + timedelta(days=j)
            for h, titre in ((8, "Techniques numériques 15 CO_ECRIT"), (10, "Systèmes Linux 3 TD"), (14, "Anglais 2 TD")):
                lignes += ["BEGIN:VEVENT", f"DTSTART:{jour:%Y%m%d}T{h:02d}0000", f"DTEND:{jour:%Y%m%d}T{h + 2:02d}0000",
                           f"UID:{jour:%Y%m%d}-{h}", f"SUMMARY:{titre}", "END:VEVENT"]
        lignes.append("END:VCALENDAR")
        from hangar import agenda as A
        from hangar.models import AgendaReminder, AgendaTask
        from hangar.qcm.models import QuizDeck, QuizProgress
        with self.app.test_request_context("/"):
            u = User(username="moi", email="moi@hangar.local", role="member")
            u.set_password("secret1")
            db.session.add(u)
            db.session.commit()
            self.uid = u.id
            A.import_calendar(u, "\n".join(lignes) + "\n", at=datetime.now() - timedelta(days=1))
            from hangar.models import AgendaEvent
            evs = AgendaEvent.query.filter_by(user_id=u.id).all()
            for ev in evs:  # une tâche liée et un rappel par séance
                db.session.add(AgendaTask(user_id=u.id, title=f"Préparer {ev.title}", due_date=ev.start.date(),
                                          event_id=ev.id))
                db.session.add(AgendaReminder(user_id=u.id, event_id=ev.id, offset_minutes=60,
                                              remind_at=ev.start - timedelta(hours=1), channel="email"))
            tn = next(ev for ev in evs if ev.title.startswith("Techniques"))
            A.set_subject_link(u, tn.title, qcm_category="TECHNIQUES_NUM")
            # Progression : quelques questions dues, examen dans 10 jours sur deux sets
            hier = datetime.now() - timedelta(days=1)
            for sid in self.sets:
                for i in range(6):
                    p = QuizProgress(user_id=u.id, set_id=sid, question_id=f"q{i}")
                    p.record(i % 2 == 0, quand=hier - timedelta(days=3))
                    p.echeance = hier
                    db.session.add(p)
            for sid in self.sets[:2]:
                db.session.add(QuizDeck(user_id=u.id, set_id=sid, nouvelles_jour=2,
                                        date_objectif=date.today() + timedelta(days=10)))
            db.session.commit()
            self.nb_seances = len(evs)
            self.rappels = {ev.id: AgendaReminder.query.filter_by(event_id=ev.id).count() for ev in evs}
        self.c, self.token = connexion(self.app, "moi", "secret1")

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _get(self, url):
        with mock.patch.object(planning, "estimer", wraps=planning.estimer) as estimer, \
                mock.patch.object(services, "etat", wraps=services.etat) as etat, \
                requetes_sql(self.app) as sql:
            r = self.c.get(url)
        self.assertEqual(r.status_code, 200, url)
        return r.get_data(as_text=True), estimer.call_count, etat.call_count, sql

    @staticmethod
    def _agenda(html):
        return json.loads(re.search(r"window\.AGENDA = (\{.*?\});</script>", html, re.S).group(1))

    def test_accueil_resume_des_qcm_calcule_une_fois(self):
        """Avant : l'état des QCM et l'estimation des examens étaient calculés deux fois (résumé du
        tableau de bord, puis cartes dues des liens des séances)."""
        html, estimer, etat, sql = self._get("/")
        self.assertEqual(etat, 1)
        self.assertEqual(estimer, 2)  # une estimation par set daté, pas deux
        data = self._agenda(html)
        lie = next(e for e in data["events"].values() if e["links"].get("qcm"))
        self.assertEqual(lie["links"]["qcm"]["label"], "TECHNIQUES_NUM")
        with self.app.test_request_context("/"):
            u = db.session.get(User, self.uid)
            attendu = sum(s["cartes"] for s in services.resume_du_jour(u)["par_set"] if s["categorie"] == "TECHNIQUES_NUM")
        self.assertGreater(attendu, 0)
        self.assertEqual(lie["links"]["qcm"]["due"], attendu)

    def test_calendrier_sans_estimation_des_examens(self):
        """Le calendrier n'affiche que les cartes dues d'une catégorie reliée : plus d'estimation
        des examens (avant : tout le résumé du jour, simulation comprise)."""
        _, estimer, etat, _ = self._get("/agenda/calendar?view=week")
        self.assertEqual(estimer, 0)
        self.assertLessEqual(etat, 1)

    def test_calendrier_requetes_groupees(self):
        """Rappels, tâches liées et liens de matière : quelques requêtes pour toute la semaine, et non
        trois par séance (15 séances ici)."""
        html, _, _, sql = self._get("/agenda/calendar?view=week")
        data = self._agenda(html)
        self.assertEqual(len(data["events"]), self.nb_seances)
        for table in ("agenda_reminder", "agenda_subject_link"):
            self.assertLessEqual(len(sur_table(sql, table)), 2, table)
        self.assertLessEqual(len(sur_table(sql, "agenda_task")), 4)
        for eid, ev in data["events"].items():
            self.assertEqual(len(ev["reminders"]), self.rappels[int(eid)])
            self.assertEqual([t["title"] for t in ev["tasks"]], [f"Préparer {ev['title']}"])

    def test_listes_des_liens_une_seule_fois(self):
        """Toutes les collections du wiki et catégories de QCM : à la racine des données de la page,
        plus recopiées dans chaque séance."""
        data = self._agenda(self._get("/agenda/calendar?view=week")[0])
        self.assertEqual(data["wikiAll"], ["Cours", "Cours/BDD", "Cours/TechniquesNumeriques"])
        self.assertEqual(data["qcmAll"], ["Linux", "TECHNIQUES_NUM"])
        for ev in data["events"].values():
            self.assertNotIn("wikiAll", ev["links"])
            self.assertNotIn("qcmAll", ev["links"])
            self.assertIn("subject", ev["links"])
        linux = next(e for e in data["events"].values() if e["title"].startswith("Systèmes Linux"))
        self.assertEqual(linux["links"]["qcmSuggest"], ["Linux"])

    def test_planning_une_estimation_par_set_et_une_lecture(self):
        """/qcm/planning/ : chaque set daté était simulé deux fois et la progression lue deux fois."""
        _, estimer, etat, sql = self._get("/qcm/planning/")
        self.assertEqual(estimer, 2)
        self.assertEqual(etat, 1)
        self.assertEqual(len(sur_table(sql, "qcm_progress")), 1)

    def test_aujourdhui_et_stats_une_lecture_de_la_progression(self):
        """Les prévisions réutilisent la progression déjà lue (avant : relue par l'ORM)."""
        for url in ("/qcm/aujourdhui/", "/qcm/stats/"):
            _, _, _, sql = self._get(url)
            self.assertEqual(len(sur_table(sql, "qcm_progress")), 1, url)
        with self.app.test_request_context("/"):
            e = services.etat(self.uid)
            now = datetime.now()
            self.assertEqual(services.previsions(self.uid, e["library"], now, tous=e["tous"]),
                             services.previsions(self.uid, e["library"], now))
            exams = services.objectifs(self.uid, e["library"], e["decks"], now, tous=e["tous"])
            self.assertEqual([x["e"] for x in exams],
                             [x["e"] for x in services.objectifs(self.uid, e["library"], e["decks"], now)])


# ---------------------------------------------------------------------------
# Piste 57 : tableau kanban et bilan
# ---------------------------------------------------------------------------
class TicketsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="hangar-perf-tickets-")
        cls.app = create_app(cls.tmp, testing=True)
        with cls.app.test_request_context("/"):
            from hangar.models import Project
            from hangar.seed import seed_demo
            seed_demo(None, verbose=False)
            cls.pid = (Project.query.filter_by(identifier="EAE").first() or Project.query.first()).id

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            db.session.remove()
            db.engine.dispose()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.c, _ = connexion(self.app, "wacogne", "hangar2026")

    def _requetes(self, url):
        self.c.get(url)  # premier affichage (caches)
        with requetes_sql(self.app) as sql:
            r = self.c.get(url)
        self.assertEqual(r.status_code, 200, url)
        return sql, r.get_data(as_text=True)

    def _ajouter_tickets(self, n=20):
        """n tickets ouverts de plus, avec assigné, étiquette, module et un bloqueur ouvert chacun."""
        from hangar.models import Issue, IssueRelation, Project
        from hangar.services import create_issue
        with self.app.test_request_context("/"):
            p = db.session.get(Project, self.pid)
            moi = User.query.filter_by(username="wacogne").one()
            bloqueur = next(i for i in Issue.query.filter_by(project_id=p.id) if not i.is_closed)
            for k in range(n):
                i = create_issue(p, {"title": f"Ticket de charge {k}", "priority": "high", "assignee_ids": [moi.id],
                                     "label_ids": [p.labels[0].id], "module_ids": [p.modules[0].id]}, moi)
                db.session.add(IssueRelation(issue_id=i.id, related_id=bloqueur.id, relation_type="blocked_by"))
            db.session.commit()

    def test_blockers_map_comme_blockers(self):
        from hangar.models import Issue, blockers_map
        with self.app.test_request_context("/"):
            issues = Issue.query.all()
            m = blockers_map(i.id for i in issues)
            for i in issues:
                self.assertEqual([b.id for b in m[i.id]], [b.id for b in i.blockers], i.key)
            self.assertTrue(any(m.values()))
            self.assertEqual(blockers_map([]), {})

    def test_kanban_requetes_independantes_du_nombre_de_tickets(self):
        """Avant : une requête par carte pour son cadenas (deux pour une carte bloquée), soit 164 sur
        le projet de démo ; maintenant une seule pour tout le tableau."""
        urls = (f"/projects/{self.pid}/issues?layout=board", "/issues?layout=board&scope=all")
        avant = {}
        for url in urls:
            sql, html = self._requetes(url)
            self.assertLessEqual(len(sur_table(sql, "issue_relation")), 1, url)
            self.assertIn('class="blocked" title="Bloqué par', html)
            avant[url] = len(sql)
        self._ajouter_tickets()
        for url in urls:
            sql, html = self._requetes(url)
            self.assertIn("Ticket de charge 19", html)
            self.assertLessEqual(len(sql), avant[url] + 2, url)

    def test_bilan_requetes_independantes_du_nombre_de_tickets(self):
        """Avant : 502 requêtes pour 124 tickets (assignés, étiquettes, modules et bloqueurs lus ticket
        par ticket) ; le nombre de requêtes ne dépend plus du nombre de tickets."""
        sql, _ = self._requetes("/bilan")
        self.assertLessEqual(len(sur_table(sql, "issue_relation")), 1)
        avant = len(sql)
        self._ajouter_tickets()
        sql, html = self._requetes("/bilan")
        self.assertLessEqual(len(sql), avant + 2)
        self.assertIn("Ticket de charge", html)
        with self.app.test_request_context("/"):
            from hangar.bilan import compute_bilan
            from hangar.models import Project
            b = compute_bilan([db.session.get(Project, self.pid)], date.today() - timedelta(days=30), date.today())
        self.assertTrue(b["blocked"])
        self.assertTrue(all(x["blockers"] for x in b["blocked"]))
        self.assertTrue(any(x["title"] == "Ticket de charge 0" for x in b["blocked"]))


if __name__ == "__main__":
    unittest.main()

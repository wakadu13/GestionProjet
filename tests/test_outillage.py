"""Outillage (vague 3, pistes 68 et 69) : tests qui manquaient et garde-fous de la CI.

- Section Travail : services (glisser-déposer pour chaque regroupement, duplication, déplacement entre
  projets, relations), filtres et regroupements de issue_query, formulaires (/issues/new, actions en masse,
  pièces jointes, relations, sous-tickets, temps passé) et API à jeton /api/v1 (valide, expiré, compte désactivé).
- Premier lancement (/setup, inscription, déconnexion) et mise à jour depuis une base d'une version plus ancienne
  (colonnes et tables absentes, remplies avec leur valeur par défaut).
- Test des QCM qui dépendait de l'heure : il passe aussi à 23 h 55.
- « hangar run » démarre vraiment le serveur sur un port libre et répond ; une seconde commande voit qu'il tourne.
- Numéro de version écrit à un seul endroit, dépendances importées toutes déclarées, ruff et Dependabot en place.

Aucun contexte de requête n'est gardé ouvert : chaque accès à la base se fait dans un ``with app.app_context()``
court, et chaque client de test a sa propre session (pas de compte « hérité » d'un autre client).

Lancer : uv run python -m unittest tests.test_outillage -v
"""
import http.client
import importlib.metadata
import io
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

from werkzeug.datastructures import MultiDict

import hangar
from hangar import cli, create_app, issue_query
from hangar.extensions import db
from hangar.models import (
    APIToken, Attachment, Cycle, Issue, IssueRelation, Label, Module, Project, User, WorkLog, Workspace,
)
from hangar.services import add_relation, create_issue, create_project, duplicate_issue, move_issue, \
    move_issue_to_project

RACINE = Path(__file__).resolve().parent.parent
MOT_DE_PASSE = "mot-de-passe-1"
JETON = "jeton-csrf-des-tests"


def _client(app, username=None):
    """Client avec son propre jeton CSRF, connecté à `username` s'il est donné."""
    c = app.test_client()
    with c.session_transaction() as s:
        s["_csrf"] = JETON
    if username:
        r = c.post("/login", data={"login": username, "password": MOT_DE_PASSE, "_csrf": JETON})
        assert r.status_code == 302 and "/login" not in r.headers["Location"], r.get_data(as_text=True)[:300]
    return c


class BaseCase(unittest.TestCase):
    """Deux projets : ALP (chef administrateur, membre et invité) et BET (chef seul)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hangar-outillage-")
        self.app = create_app(self.tmp, testing=True)
        with self.app.test_request_context("/"):
            ids = {}
            for username, role in (("chef", "admin"), ("membre", "member"), ("invite", "guest")):
                u = User(username=username, email=f"{username}@exemple.fr", role=role, first_name=username.title())
                u.set_password(MOT_DE_PASSE)
                db.session.add(u)
                db.session.flush()
                ids[username] = u.id
            chef = db.session.get(User, ids["chef"])
            alp = create_project("Alpha", "ALP", chef, member_ids=[ids["membre"], ids["invite"]])
            bet = create_project("Beta", "BET", chef, network="private")
            for p in (alp, bet):
                db.session.add_all([Label(project=p, name="Urgent"), Label(project=p, name="Rapport")])
            db.session.add_all([Cycle(project=alp, name="Cycle 1", start_date=date.today(),
                                      end_date=date.today() + timedelta(days=14)),
                                Module(project=alp, name="Terrain"), Module(project=alp, name="Synthèse")])
            db.session.commit()
            self.ids = ids
            self.alp, self.bet = alp.id, bet.id

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ticket(self, titre="Ticket", projet=None, **data):
        """Crée un ticket (comme le formulaire) et renvoie son id."""
        with self.app.test_request_context("/"):
            p = db.session.get(Project, projet or self.alp)
            i = create_issue(p, {"title": titre, **data}, db.session.get(User, self.ids["chef"]))
            db.session.commit()
            return i.id


# ---------------------------------------------------------------------------
# Section Travail : services
# ---------------------------------------------------------------------------

class ServicesTravailTests(BaseCase):
    def test_glisser_deposer_pour_chaque_regroupement(self):
        a, b, c = self.ticket("A"), self.ticket("B"), self.ticket("C")
        with self.app.test_request_context("/"):
            chef = db.session.get(User, self.ids["chef"])
            p = db.session.get(Project, self.alp)
            issue = db.session.get(Issue, a)
            en_cours = p.first_state_of("started")
            move_issue(issue, "state", issue.state_id, en_cours.id, None, None, chef)
            self.assertEqual(issue.state_id, en_cours.id)
            move_issue(issue, "state_group", "started", "completed", None, None, chef)
            self.assertEqual(issue.state.group, "completed")
            move_issue(issue, "priority", "none", "urgent", None, None, chef)
            self.assertEqual(issue.priority, "urgent")
            cycle = Cycle.query.filter_by(project_id=p.id).one()
            move_issue(issue, "cycle", "none", cycle.id, None, None, chef)
            self.assertEqual(issue.cycle, cycle)
            db.session.flush()  # comme entre deux requêtes : cycle_id est relu en base
            move_issue(issue, "cycle", cycle.id, "none", None, None, chef)
            self.assertIsNone(issue.cycle)
            membre = db.session.get(User, self.ids["membre"])
            move_issue(issue, "assignee", "none", str(membre.id), None, None, chef)
            self.assertEqual([u.id for u in issue.assignees], [membre.id])
            move_issue(issue, "assignee", str(membre.id), str(chef.id), None, None, chef)
            self.assertEqual([u.id for u in issue.assignees], [chef.id])  # retiré de l'ancien groupe
            urgent = Label.query.filter_by(project_id=p.id, name="Urgent").one()
            move_issue(issue, "label", "none", str(urgent.id), None, None, chef)
            self.assertEqual([lb.name for lb in issue.labels], ["Urgent"])
            terrain = Module.query.filter_by(project_id=p.id, name="Terrain").one()
            move_issue(issue, "module", "none", str(terrain.id), None, None, chef)
            self.assertEqual([m.name for m in issue.modules], ["Terrain"])
            move_issue(issue, "module", str(terrain.id), "none", None, None, chef)
            self.assertEqual(issue.modules, [])
            # Ordre manuel : déposé entre B et C
            ib, ic = db.session.get(Issue, b), db.session.get(Issue, c)
            move_issue(issue, "priority", "urgent", "urgent", ib.id, ic.id, chef)
            self.assertTrue(ib.sort_order < issue.sort_order < ic.sort_order)
            db.session.commit()

    def test_duplication(self):
        with self.app.test_request_context("/"):
            p = db.session.get(Project, self.alp)
            ids = [lb.id for lb in p.labels if lb.name == "Urgent"]
            mid = Module.query.filter_by(project_id=p.id, name="Terrain").one().id
        src = self.ticket("Rapport final", priority="high", label_ids=ids, module_ids=[mid],
                          assignee_ids=[self.ids["membre"]], target_date="2026-12-01")
        with self.app.test_request_context("/"):
            issue = db.session.get(Issue, src)
            copie = duplicate_issue(issue, db.session.get(User, self.ids["chef"]))
            db.session.commit()
            self.assertEqual(copie.title, "Rapport final (copie)")
            self.assertNotEqual(copie.sequence_id, issue.sequence_id)
            self.assertEqual((copie.priority, copie.target_date), ("high", date(2026, 12, 1)))
            self.assertEqual([lb.name for lb in copie.labels], ["Urgent"])
            self.assertEqual([m.name for m in copie.modules], ["Terrain"])
            self.assertEqual([u.username for u in copie.assignees], ["membre"])

    def test_deplacement_vers_un_autre_projet(self):
        with self.app.test_request_context("/"):
            ids = [lb.id for lb in db.session.get(Project, self.alp).labels]
        a = self.ticket("À déplacer", label_ids=ids, assignee_ids=[self.ids["membre"], self.ids["chef"]])
        b = self.ticket("Lié")
        with self.app.test_request_context("/"):
            chef = db.session.get(User, self.ids["chef"])
            ia, ib = db.session.get(Issue, a), db.session.get(Issue, b)
            add_relation(ia, ib, "blocked_by", chef)
            db.session.commit()
            move_issue_to_project(ia, db.session.get(Project, self.bet), chef)
            db.session.commit()
            self.assertEqual(ia.key, "BET-1")
            self.assertEqual(sorted(lb.project_id for lb in ia.labels), [self.bet, self.bet])  # même nom, projet cible
            self.assertEqual([u.username for u in ia.assignees], ["chef"])  # membre absent de BET : retiré
            self.assertEqual(IssueRelation.query.count(), 0)
            self.assertEqual(ia.state.project_id, self.bet)

    def test_relations(self):
        a, b = self.ticket("A"), self.ticket("B")
        with self.app.test_request_context("/"):
            chef = db.session.get(User, self.ids["chef"])
            ia, ib = db.session.get(Issue, a), db.session.get(Issue, b)
            self.assertIsNone(add_relation(ia, ia, "relates_to", chef))  # pas vers lui-même
            self.assertIsNone(add_relation(ia, ib, "inconnu", chef))
            self.assertIsNotNone(add_relation(ia, ib, "blocked_by", chef))
            db.session.commit()
            self.assertIsNone(add_relation(ib, ia, "relates_to", chef))  # déjà liés, dans un sens ou l'autre
            self.assertEqual(IssueRelation.query.count(), 1)


# ---------------------------------------------------------------------------
# Section Travail : filtres et regroupements
# ---------------------------------------------------------------------------

class IssueQueryTests(BaseCase):
    def setUp(self):
        super().setUp()
        with self.app.app_context():
            p = db.session.get(Project, self.alp)
            urgent = [lb.id for lb in p.labels if lb.name == "Urgent"]
            terrain = Module.query.filter_by(name="Terrain").one().id
            cycle = Cycle.query.filter_by(project_id=p.id).one().id
        self.a = self.ticket("Entretiens", priority="high", assignee_ids=[self.ids["membre"]], label_ids=urgent,
                             module_ids=[terrain], cycle_id=cycle, target_date=date.today().isoformat())
        self.b = self.ticket("Sondage", priority="low")
        self.c = self.ticket("Synthèse écrite", priority="high", assignee_ids=[self.ids["chef"]])

    def _filtrer(self, **args):
        with self.app.test_request_context("/"):
            chef = db.session.get(User, self.ids["chef"])
            f = issue_query.parse_filters(MultiDict(args))
            q = issue_query.apply_filters(issue_query.base_query().filter(Issue.project_id == self.alp), f, chef)
            return f, sorted(i.title for i in q.all())

    def test_parse_filters(self):
        f = issue_query.parse_filters(MultiDict([("priority", "high,low"), ("priority", "urgent"), ("q", "  mot ")]))
        self.assertEqual(f["priority"], ["high", "low", "urgent"])
        self.assertEqual(f["q"], "mot")
        self.assertEqual(issue_query.parse_filters(MultiDict()), {})

    def test_apply_filters(self):
        self.assertEqual(self._filtrer(priority="high")[1], ["Entretiens", "Synthèse écrite"])
        self.assertEqual(self._filtrer(assignee="me")[1], ["Synthèse écrite"])  # « moi » = le chef
        self.assertEqual(self._filtrer(q="sondage")[1], ["Sondage"])
        self.assertEqual(self._filtrer(priority="high", q="entretien")[1], ["Entretiens"])

    def test_group_issues_pour_chaque_regroupement(self):
        with self.app.test_request_context("/"):
            p = db.session.get(Project, self.alp)
            issues = Issue.query.filter_by(project_id=self.alp).all()
            for group_by in ("none", "state", "state_group", "priority", "assignee", "label", "cycle", "module",
                             "created_by", "project"):
                groupes = issue_query.group_issues(issues, group_by, [p])
                vus = {i.id for g in groupes for i in g["issues"]}
                self.assertEqual(vus, {self.a, self.b, self.c}, group_by)  # aucun ticket perdu
                self.assertEqual(len({g["key"] for g in groupes}), len(groupes), group_by)
            prio = {g["key"]: [i.title for i in g["issues"]] for g in issue_query.group_issues(issues, "priority", [p])}
            self.assertEqual(sorted(prio["high"]), ["Entretiens", "Synthèse écrite"])
            par_assigne = issue_query.group_issues(issues, "assignee", [p])
            sans = [g for g in par_assigne if g["key"] == "none"]
            self.assertEqual([i.title for i in sans[0]["issues"]], ["Sondage"])

    def test_calendrier_du_mois(self):
        with self.app.test_request_context("/"):
            issues = Issue.query.filter_by(project_id=self.alp).all()
            semaines = issue_query.calendar_weeks(date.today().strftime("%Y-%m"), issues)
            self.assertTrue(semaines)
            places = {i.id: jour["date"] for semaine in semaines["weeks"] for jour in semaine for i in jour["issues"]}
            self.assertEqual(places, {self.a: date.today()})  # seul ticket daté : le jour de son échéance
            self.assertEqual(semaines["undated"], 2)
            self.assertTrue(issue_query.calendar_weeks("n'importe quoi", issues))  # mois invalide : mois courant


# ---------------------------------------------------------------------------
# Section Travail : formulaires et actions en masse
# ---------------------------------------------------------------------------

class FormulairesTravailTests(BaseCase):
    def test_creation_par_formulaire(self):
        c = _client(self.app, "membre")
        with self.app.app_context():
            p = db.session.get(Project, self.alp)
            label = [lb.id for lb in p.labels if lb.name == "Rapport"][0]
            state = p.first_state_of("started").id
        r = c.post("/issues/new", data={"_csrf": JETON, "project_id": self.alp, "title": "Préparer l'entretien",
                                        "priority": "medium", "label_ids": [label], "state_id": state,
                                        "assignee_ids": [self.ids["membre"]], "target_date": "2026-12-15"})
        self.assertEqual(r.status_code, 302)
        with self.app.app_context():
            i = Issue.query.filter_by(title="Préparer l'entretien").one()
            self.assertIn(f"/projects/{self.alp}/issues/{i.sequence_id}", r.headers["Location"])
            self.assertEqual((i.priority, i.state_id, i.target_date), ("medium", state, date(2026, 12, 15)))
            self.assertEqual([lb.name for lb in i.labels], ["Rapport"])
            self.assertEqual([u.username for u in i.assignees], ["membre"])
        # Titre vide : rien n'est créé ; un invité ne crée pas de ticket ; projet hors de portée : 404
        n = self._nombre()
        c.post("/issues/new", data={"_csrf": JETON, "project_id": self.alp, "title": "  "})
        self.assertEqual(self._nombre(), n)
        r = _client(self.app, "invite").post("/issues/new", data={"_csrf": JETON, "project_id": self.alp,
                                                                  "title": "Ticket d'invité"})
        self.assertEqual(r.status_code, 403)
        r = c.post("/issues/new", data={"_csrf": JETON, "project_id": self.bet, "title": "Ailleurs"})
        self.assertIn(r.status_code, (403, 404))
        self.assertEqual(self._nombre(), n)

    def _nombre(self):
        with self.app.app_context():
            return Issue.query.count()

    def test_actions_en_masse(self):
        ids = [self.ticket(f"T{n}") for n in range(3)]
        with self.app.app_context():
            p = db.session.get(Project, self.alp)
            label = [lb.id for lb in p.labels if lb.name == "Urgent"][0]
            module = Module.query.filter_by(name="Terrain").one().id
            cycle = Cycle.query.filter_by(project_id=p.id).one().id
        c = _client(self.app, "membre")

        def masse(action, value="", lire=None, tickets=ids, client=c):
            """Action en masse, puis `lire(ticket)` pour chaque ticket (None s'il a été supprimé)."""
            r = client.post("/issues/bulk", data={"_csrf": JETON, "ids": tickets, "bulk_action": action,
                                                  "value": value})
            self.assertEqual(r.status_code, 302, action)
            with self.app.app_context():
                issues = [db.session.get(Issue, i) for i in tickets]
                return [None if i is None else (lire(i) if lire else i.id) for i in issues]

        self.assertEqual(masse("state", "completed", lambda i: i.state.group), ["completed"] * 3)
        self.assertEqual(masse("priority", "urgent", lambda i: i.priority), ["urgent"] * 3)
        noms = lambda i: [u.username for u in i.assignees]  # noqa: E731
        self.assertEqual(masse("assignee", str(self.ids["membre"]), noms), [["membre"]] * 3)
        self.assertEqual(masse("unassign", "", noms), [[]] * 3)
        self.assertEqual(masse("label", str(label), lambda i: [lb.name for lb in i.labels]), [["Urgent"]] * 3)
        self.assertEqual(masse("module", str(module), lambda i: [m.name for m in i.modules]), [["Terrain"]] * 3)
        self.assertEqual(masse("cycle", str(cycle), lambda i: i.cycle_id), [cycle] * 3)
        self.assertEqual(masse("target_date", "2026-12-24", lambda i: i.target_date), [date(2026, 12, 24)] * 3)
        self.assertTrue(masse("archive", "", lambda i: i.archived_at is not None, tickets=ids[:1])[0])
        # Suppression : refusée à un simple membre, faite par l'administrateur du projet
        self.assertEqual(masse("delete", tickets=ids[1:]), ids[1:])
        self.assertEqual(masse("delete", tickets=ids[1:], client=_client(self.app, "chef")), [None, None])
        # Un invité ne modifie rien
        self.assertEqual(masse("priority", "low", lambda i: i.priority, tickets=ids[:1],
                               client=_client(self.app, "invite")), ["urgent"])

    def test_pieces_jointes_relations_sous_tickets_temps(self):
        a, b = self.ticket("Principal"), self.ticket("Autre")
        with self.app.app_context():
            ia, ib = db.session.get(Issue, a), db.session.get(Issue, b)
            seq, cle_b = ia.sequence_id, ib.key
        c = _client(self.app, "membre")
        base = f"/projects/{self.alp}/issues/{seq}"
        r = c.post(f"{base}/attachments", data={"_csrf": JETON, "files": (io.BytesIO(b"contenu"), "notes.txt")},
                   content_type="multipart/form-data")
        self.assertEqual(r.status_code, 302)
        with self.app.app_context():
            att = Attachment.query.one()
            self.assertEqual((att.filename, att.size), ("notes.txt", 7))
            aid = att.id
        r = c.get(f"/attachments/{aid}")
        self.assertEqual((r.status_code, r.data), (200, b"contenu"))
        r.close()
        self.assertEqual(_client(self.app).get(f"/attachments/{aid}").status_code, 302)  # sans session
        c.post(f"/attachments/{aid}/delete", data={"_csrf": JETON})
        with self.app.app_context():
            self.assertEqual(Attachment.query.count(), 0)
        # Relation par la clé du ticket
        c.post(f"{base}/relations", data={"_csrf": JETON, "ref": cle_b, "relation_type": "blocked_by"})
        with self.app.app_context():
            rel = IssueRelation.query.one()
            self.assertEqual((rel.issue_id, rel.related_id, rel.relation_type), (a, b, "blocked_by"))
        # Sous-ticket créé depuis la fiche
        c.post(f"{base}/subissues", data={"_csrf": JETON, "title": "Étape 1"})
        with self.app.app_context():
            self.assertEqual(Issue.query.filter_by(title="Étape 1").one().parent_id, a)
        # Temps passé : « 1h30 » = 90 minutes ; durée invalide refusée
        c.post(f"{base}/worklogs", data={"_csrf": JETON, "duration": "1h30", "description": "Lecture"})
        c.post(f"{base}/worklogs", data={"_csrf": JETON, "duration": "beaucoup"})
        with self.app.app_context():
            self.assertEqual([w.minutes for w in WorkLog.query.all()], [90])
        self.assertEqual(c.get(base).status_code, 200)


# ---------------------------------------------------------------------------
# Section Travail : API à jeton
# ---------------------------------------------------------------------------

class ApiV1Tests(BaseCase):
    def _jeton(self, username="membre", jours=None, expire=False):
        with self.app.app_context():
            t, brut = APIToken.generate(db.session.get(User, self.ids[username]), "Script", days=jours)
            if expire:
                t.expires_at = datetime.now() - timedelta(minutes=1)
            db.session.add(t)
            db.session.commit()
            return brut

    def test_jeton_valide(self):
        brut = self._jeton(jours=30)
        c = self.app.test_client()
        h = {"Authorization": f"Bearer {brut}"}
        r = c.get("/api/v1/me", headers=h)
        self.assertEqual((r.status_code, r.json["user"]["username"]), (200, "membre"))
        self.assertEqual(c.get("/api/v1/me", headers={"X-API-Key": brut}).status_code, 200)
        projets = c.get("/api/v1/projects", headers=h).json["projects"]
        self.assertEqual([p["identifier"] for p in projets], ["ALP"])  # BET n'est pas visible
        self.assertEqual(c.get(f"/api/v1/projects/{self.bet}", headers=h).status_code, 403)
        r = c.post(f"/api/v1/projects/{self.alp}/issues", json={"title": "Par l'API", "priority": "high"}, headers=h)
        self.assertEqual(r.status_code, 201)
        iid = r.json["issue"]["id"]
        self.assertEqual(c.post(f"/api/v1/projects/{self.alp}/issues", json={}, headers=h).status_code, 400)
        r = c.patch(f"/api/v1/issues/{iid}", json={"priority": "low"}, headers=h)
        self.assertEqual((r.status_code, r.json["issue"]["priority"]), (200, "low"))
        r = c.post(f"/api/v1/issues/{iid}/comments", json={"body": "Depuis un script"}, headers=h)
        self.assertLess(r.status_code, 300)
        self.assertEqual(len(c.get(f"/api/v1/issues/{iid}/comments", headers=h).json["comments"]), 1)
        titres = [i["title"] for i in c.get(f"/api/v1/projects/{self.alp}/issues", headers=h).json["issues"]]
        self.assertEqual(titres, ["Par l'API"])
        self.assertEqual(c.delete(f"/api/v1/issues/{iid}", headers=h).status_code, 403)  # simple membre
        with self.app.app_context():
            self.assertIsNotNone(APIToken.query.one().last_used_at)

    def test_jeton_refuse(self):
        c = self.app.test_client()
        self.assertEqual(c.get("/api/v1/me").status_code, 401)
        self.assertEqual(c.get("/api/v1/me", headers={"Authorization": "Bearer hgr_faux"}).status_code, 401)
        expire = self._jeton(expire=True)
        self.assertEqual(c.get("/api/v1/me", headers={"Authorization": f"Bearer {expire}"}).status_code, 401)
        brut = self._jeton()
        with self.app.app_context():
            db.session.get(User, self.ids["membre"]).active = False
            db.session.commit()
        self.assertEqual(c.get("/api/v1/me", headers={"Authorization": f"Bearer {brut}"}).status_code, 401)
        # Une session de navigateur ne remplace pas le jeton
        self.assertEqual(_client(self.app, "chef").get("/api/v1/projects").status_code, 401)


# ---------------------------------------------------------------------------
# Premier lancement et mise à jour de version
# ---------------------------------------------------------------------------

class PremierLancementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hangar-premier-")
        self.app = create_app(self.tmp, testing=True)

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_installation_neuve(self):
        c = _client(self.app)
        for url in ("/", "/home", "/login", "/agenda", "/wiki/", "/qcm/"):
            r = c.get(url)
            self.assertEqual((r.status_code, r.headers.get("Location")), (302, "/setup"), url)
        self.assertEqual(c.get("/setup").status_code, 200)
        # Formulaire refusé (mot de passe trop court) : rien n'est créé
        r = c.post("/setup", data={"_csrf": JETON, "username": "moi", "password": "court"})
        self.assertEqual(r.status_code, 200)
        with self.app.app_context():
            self.assertEqual(User.query.count(), 0)
        r = c.post("/setup", data={"_csrf": JETON, "username": "Moi", "password": MOT_DE_PASSE,
                                   "first_name": "Jean", "workspace": "Ma promo"})
        self.assertEqual((r.status_code, r.headers["Location"]), (302, "/agenda/imports"))
        self.assertEqual(c.get("/agenda/imports").status_code, 200)  # session ouverte
        with self.app.app_context():
            u = User.query.one()
            self.assertEqual((u.username, u.role, u.first_name), ("moi", "admin", "Jean"))
            self.assertEqual(Workspace.get().name, "Ma promo")
        # Une fois le compte créé, /setup ne sert plus ; un autre visiteur va à la connexion
        self.assertEqual(c.get("/setup").status_code, 302)
        autre = _client(self.app)
        self.assertEqual(autre.post("/setup", data={"_csrf": JETON, "username": "pirate",
                                                    "password": MOT_DE_PASSE}).status_code, 302)
        with self.app.app_context():
            self.assertEqual(User.query.count(), 1)
        r = autre.get("/home")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].startswith("/login"))
        # Déconnexion : en POST seulement
        self.assertEqual(c.get("/logout").status_code, 302)
        self.assertEqual(c.get("/agenda/imports").status_code, 200)
        c.post("/logout", data={"_csrf": JETON})
        self.assertTrue(c.get("/agenda/imports").headers["Location"].startswith("/login"))

    def test_inscription(self):
        c = _client(self.app)
        c.post("/setup", data={"_csrf": JETON, "username": "admin", "password": MOT_DE_PASSE})
        visiteur = _client(self.app)
        r = visiteur.get("/signup")  # fermée par défaut
        self.assertEqual((r.status_code, r.headers["Location"]), (302, "/login"))
        with self.app.app_context():
            Workspace.get().allow_signup = True
            db.session.commit()
        self.assertEqual(visiteur.get("/signup").status_code, 200)
        r = visiteur.post("/signup", data={"_csrf": JETON, "username": "eleve", "password": MOT_DE_PASSE})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(visiteur.get("/agenda").status_code, 200)
        with self.app.app_context():
            self.assertEqual(User.query.filter_by(username="eleve").one().role, "member")


class MiseAJourTests(unittest.TestCase):
    """Base créée par une version plus ancienne : colonnes et tables absentes, ajoutées au démarrage sans action
    de l'utilisateur, et remplies avec leur valeur par défaut."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hangar-maj-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_base_d_une_ancienne_version(self):
        app = create_app(self.tmp, testing=True)
        with app.test_request_context("/"):
            from hangar.wiki.models import WikiPdfText
            u = User(username="chef", email="chef@exemple.fr", role="admin")
            u.set_password(MOT_DE_PASSE)
            db.session.add(u)
            db.session.flush()
            p = create_project("Alpha", "ALP", u)
            create_issue(p, {"title": "Ancien ticket", "priority": "high"}, u)
            db.session.add(WikiPdfText(chemin="Cours/a.pdf", pages=["p1"]))
            db.session.commit()
            db.session.remove()
            db.engine.dispose()
        # Ce que n'avait pas une version plus ancienne : trois colonnes (date calculée, liste JSON, texte à valeur
        # par défaut) et deux tables plus récentes.
        con = sqlite3.connect(os.path.join(self.tmp, "hangar.db"))
        try:
            con.execute("ALTER TABLE issue DROP COLUMN created_at")
            con.execute("ALTER TABLE issue DROP COLUMN external_id")
            con.execute("ALTER TABLE wiki_pdf_text DROP COLUMN pages")
            con.execute("ALTER TABLE project DROP COLUMN description")
            con.execute("DROP TABLE ai_message")
            con.execute("DROP TABLE ai_conversation")
            con.commit()
        finally:
            con.close()

        app = create_app(self.tmp, testing=True)  # rien d'autre à faire : la base est mise à niveau au démarrage
        try:
            with app.app_context():
                from sqlalchemy import inspect, text

                from hangar.ai.models import AIConversation
                from hangar.wiki.models import WikiPdfText
                insp = inspect(db.engine)
                self.assertIn("created_at", {c["name"] for c in insp.get_columns("issue")})
                self.assertTrue(insp.has_table("ai_conversation"))
                issue = Issue.query.one()
                self.assertEqual((issue.title, issue.priority), ("Ancien ticket", "high"))  # rien de perdu
                self.assertIsInstance(issue.created_at, datetime)  # rempli, pas vide
                self.assertIsNone(issue.external_id)  # sans valeur par défaut : vide
                self.assertEqual(WikiPdfText.query.one().pages, [])
                self.assertEqual(db.session.get(Project, 1).description, "")
                self.assertEqual(db.session.execute(text("SELECT count(*) FROM issue WHERE created_at IS NULL"))
                                 .scalar(), 0)
                self.assertEqual(AIConversation.query.count(), 0)
            c = _client(app, "chef")
            for url in ("/home", "/projects/1/issues", "/projects/1/issues/1", "/projects/1/issues?layout=gantt",
                        "/analytics", "/bilan", "/ia/"):
                r = c.get(url)
                self.assertEqual(r.status_code, 200, url)
            # Une seconde ouverture ne change plus rien
            with app.app_context():
                self.assertEqual(hangar.migrer_schema(), [])
        finally:
            with app.app_context():
                db.session.remove()
                db.engine.dispose()


# ---------------------------------------------------------------------------
# Test des QCM qui dépendait de l'heure
# ---------------------------------------------------------------------------

class HeureDesTestsQcmTests(unittest.TestCase):
    def test_file_du_jour_passe_aussi_juste_avant_minuit(self):
        """tests/test_qcm.py ReponsesTests échouait entre 23 h 49 et minuit (« + 11 minutes » = le lendemain)."""
        from hangar.qcm import quiz
        try:
            import test_qcm  # suite lancée par « unittest discover -s tests »
        except ImportError:
            from tests import test_qcm
        tard = datetime.combine(date.today(), datetime.min.time()).replace(hour=23, minute=55)
        suite = unittest.TestSuite([test_qcm.ReponsesTests("test_reponses_enregistrees_et_file_du_jour")])
        resultat = unittest.TestResult()
        with mock.patch.object(quiz, "maintenant", lambda: tard):
            suite.run(resultat)
        self.assertEqual((resultat.failures, resultat.errors), ([], []))
        self.assertEqual(resultat.testsRun, 1)


# ---------------------------------------------------------------------------
# « hangar run » : vrai serveur sur un port libre
# ---------------------------------------------------------------------------

def _port_libre() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(port, chemin):
    con = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        con.request("GET", chemin)
        r = con.getresponse()
        return r.status, r.getheader("Location"), r.read()
    finally:
        con.close()


class LancementServeurTests(unittest.TestCase):
    def test_hangar_run_demarre_et_repond(self):
        tmp = tempfile.mkdtemp(prefix="hangar-run-")
        data = os.path.join(tmp, "données")  # accent dans le chemin, comme un dossier Windows
        port = _port_libre()
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("HANGAR_") and k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
        sortie = open(os.path.join(tmp, "sortie.log"), "wb")
        proc = subprocess.Popen([sys.executable, "-m", "hangar", "--data", data, "run", "--no-browser",
                                 "--port", str(port)], stdout=sortie, stderr=subprocess.STDOUT, env=env,
                                cwd=str(RACINE))
        try:
            fin = time.monotonic() + 60
            reponse = None
            while time.monotonic() < fin and proc.poll() is None:
                try:
                    reponse = _get(port, "/login")
                    break
                except OSError:
                    time.sleep(0.3)
            self.assertIsNone(proc.poll(), "le serveur s'est arrêté au démarrage")
            self.assertIsNotNone(reponse, "le serveur ne répond pas")
            self.assertEqual(reponse[:2], (302, "/setup"))  # base neuve : création du premier compte
            self.assertEqual(_get(port, "/setup")[0], 200)
            self.assertEqual(_get(port, "/static/js/app.js")[0], 200)
            self.assertTrue(cli._port_busy(port))
            self.assertTrue(cli._is_hangar(port))
            # Second lancement (double-clic sur lancer.bat) : il voit que Hangar tourne déjà et s'arrête là
            texte = io.StringIO()
            with redirect_stdout(texte), mock.patch("waitress.serve") as serve:
                cli.main(["--data", data, "run", "--no-browser", "--port", str(port)])
            serve.assert_not_called()
            self.assertIn("tourne déjà", texte.getvalue())
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=20)
            sortie.close()
            journal = Path(tmp, "sortie.log").read_bytes().decode("utf-8", errors="replace")
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertIn(f"http://127.0.0.1:{port}", journal)  # bannière écrite, même en cp1252 sous Windows
        self.assertNotIn("Traceback", journal)

    def test_port_occupe_par_un_autre_programme(self):
        """Port pris par autre chose que Hangar : le suivant est choisi, sans erreur."""
        tmp = tempfile.mkdtemp(prefix="hangar-run-")
        occupant = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        occupant.bind(("127.0.0.1", 0))
        occupant.listen(1)
        port = occupant.getsockname()[1]
        try:
            texte = io.StringIO()
            with redirect_stdout(texte), mock.patch("waitress.serve") as serve, \
                    mock.patch.object(cli, "_is_hangar", return_value=False), \
                    mock.patch("hangar.mailer.start_worker"), \
                    mock.patch("hangar.lancement.ouvrir_journal", return_value=Path(tmp, "hangar.log")):
                # (journal non branché : il resterait ouvert sur le dossier temporaire pour les tests suivants)
                cli.main(["--data", os.path.join(tmp, "data"), "run", "--no-browser", "--port", str(port)])
            serve.assert_called_once()
            self.assertNotEqual(serve.call_args.kwargs["port"], port)
            self.assertIn("utilise le port", texte.getvalue())
        finally:
            occupant.close()
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Version, dépendances, vérification du code
# ---------------------------------------------------------------------------

class OutillageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pyproject = tomllib.loads((RACINE / "pyproject.toml").read_text(encoding="utf-8"))

    def test_version_ecrite_a_un_seul_endroit(self):
        projet = self.pyproject["project"]
        self.assertNotIn("version", projet, "la version ne s'écrit que dans hangar/__init__.py")
        self.assertIn("version", projet.get("dynamic", []))
        self.assertEqual(self.pyproject["tool"]["hatch"]["version"]["path"], "hangar/__init__.py")
        self.assertRegex(hangar.__version__, r"^\d+\.\d+\.\d+$")
        self.assertEqual(importlib.metadata.version("hangar"), hangar.__version__)

    def test_dependances_importees_toutes_declarees(self):
        """Chaque bibliothèque importée par hangar/ est déclarée dans pyproject.toml, pas seulement tirée par une
        autre (sauf le socle de Flask et de Flask-SQLAlchemy, qui ne peut pas disparaître)."""
        import ast
        declarees = {re.split(r"[<>=!~;\[ ]", d, maxsplit=1)[0].lower().replace("_", "-")
                     for d in self.pyproject["project"]["dependencies"]}
        socle = {"werkzeug", "markupsafe", "sqlalchemy", "jinja2"}
        distributions = importlib.metadata.packages_distributions()
        manquantes = set()
        for f in (RACINE / "hangar").rglob("*.py"):
            for n in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
                if isinstance(n, ast.Import):
                    noms = [a.name for a in n.names]
                elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                    noms = [n.module]
                else:
                    continue
                for nom in noms:
                    racine = nom.split(".")[0]
                    if racine in sys.stdlib_module_names or racine == "hangar":
                        continue
                    for dist in distributions.get(racine, [racine]):
                        dist = dist.lower().replace("_", "-")
                        if dist not in declarees and dist not in socle:
                            manquantes.add(f"{dist} ({f.relative_to(RACINE)})")
        self.assertEqual(sorted(manquantes), [])

    def test_ci_verifie_le_code_et_dependabot_suit_les_dependances(self):
        ci = (RACINE / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        self.assertRegex(ci, r"ruff@[\d.]+ check --no-cache --select F,E9")
        self.assertEqual(self.pyproject["tool"]["ruff"]["lint"]["select"], ["F", "E9"])
        dependabot = (RACINE / ".github" / "dependabot.yml").read_text(encoding="utf-8")
        for ecosysteme in ("uv", "github-actions"):
            self.assertIn(f'package-ecosystem: "{ecosysteme}"', dependabot)


if __name__ == "__main__":
    unittest.main()

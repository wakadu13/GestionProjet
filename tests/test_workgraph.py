"""Tests des initiatives et du Workgraph : avancement, santé, graphe (nœuds, liens, dépendances), pages, droits.

Lancer : uv run python -m unittest discover -s tests -v
"""
import json
import re
import shutil
import tempfile
import unittest
from datetime import date, datetime, timedelta

from hangar import create_app, digest
from hangar import workgraph as W
from hangar.extensions import db
from hangar.models import Initiative, InitiativeUpdate, Label, Project, User
from hangar.services import add_relation, create_issue, create_project

TODAY = date(2026, 11, 10)


def st(done=0, total=0, cancelled=0, open_=None, overdue=0):
    """Statistiques synthétiques au format de `stats_of`."""
    active = total - cancelled
    return {"counts": {"backlog": 0, "unstarted": 0, "started": 0, "completed": done, "cancelled": cancelled},
            "total": total, "done": done, "open": active - done if open_ is None else open_, "overdue": overdue,
            "pct": round(100 * done / active) if active else 0}


class HealthTests(unittest.TestCase):
    """Règles de santé (fonctions pures)."""

    def test_no_dates_is_on_track(self):
        self.assertEqual(W.compute_health(st(1, 10), None, None, TODAY), ("on_track", []))

    def test_past_target_is_off_track(self):
        h, why = W.compute_health(st(5, 10), date(2026, 10, 1), date(2026, 11, 5), TODAY)
        self.assertEqual(h, "off_track")
        self.assertIn("Échéance dépassée depuis 5 jours", why[0])

    def test_past_target_but_finished_is_on_track(self):
        self.assertEqual(W.compute_health(st(10, 10), date(2026, 10, 1), date(2026, 11, 5), TODAY)[0], "on_track")

    def test_progress_versus_elapsed_time(self):
        start, target = date(2026, 10, 11), date(2026, 12, 10)  # 30 j écoulés sur 60 : 50 % attendu
        self.assertEqual(W.compute_health(st(4, 10), start, target, TODAY)[0], "on_track")   # écart 10
        self.assertEqual(W.compute_health(st(3, 10), start, target, TODAY)[0], "at_risk")    # écart 20
        h, why = W.compute_health(st(1, 10), start, target, TODAY)                           # écart 40
        self.assertEqual(h, "off_track")
        self.assertEqual(why, ["10 % terminé pour 50 % du temps écoulé"])

    def test_close_target_with_low_progress(self):
        h, why = W.compute_health(st(5, 10), None, TODAY + timedelta(days=5), TODAY)
        self.assertEqual(h, "at_risk")
        self.assertIn("Échéance dans 5 jours", why[0])

    def test_overdue_tickets(self):
        self.assertEqual(W.compute_health(st(0, 10, overdue=2), None, None, TODAY)[0], "at_risk")
        self.assertEqual(W.compute_health(st(0, 10, overdue=6), None, None, TODAY)[0], "off_track")
        self.assertEqual(W.compute_health(st(0, 20, overdue=1), None, None, TODAY)[0], "on_track")

    def test_closed_is_on_track(self):
        self.assertEqual(W.compute_health(st(0, 10, overdue=9), None, date(2026, 1, 1), TODAY, closed=True),
                         ("on_track", []))

    def test_stats_of(self):
        class I:  # ticket minimal
            def __init__(self, g, d=None):
                self.state_group, self.target_date = g, d
        s = W.stats_of([I("completed"), I("cancelled"), I("started", date(2026, 11, 1)), I("backlog")], TODAY)
        self.assertEqual((s["total"], s["done"], s["open"], s["overdue"], s["pct"]), (4, 1, 2, 1, 33))


class WorkgraphCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hangar-test-")
        self.app = create_app(self.tmp, testing=True)
        self.ctx = self.app.test_request_context("/")
        self.ctx.push()
        self.admin = self._user("chef", "admin", "Anne", "Chef")
        self.member = self._user("membre", "member", "Marc", "Membre")
        self.guest = self._user("invite", "guest", "Ines", "Invitée")
        self.outsider = self._user("autre", "member", "Olga", "Autre")
        ids = [self.admin.id, self.member.id, self.guest.id]
        self.pa = create_project("Projet A", "AAA", self.admin, member_ids=ids, lead=self.member,
                                 start_date=date(2026, 10, 1), target_date=date(2026, 12, 31))
        self.pb = create_project("Projet B", "BBB", self.admin, member_ids=ids, network="private")
        self.pc = create_project("Projet C", "CCC", self.admin, member_ids=ids)
        from hangar.models import Module
        self.ma1 = Module(project=self.pa, name="Module A1", status="in-progress", target_date=date(2026, 11, 20))
        self.ma2 = Module(project=self.pa, name="Module A2", status="planned")
        self.mb1 = Module(project=self.pb, name="Module B1", status="planned")
        db.session.add_all([self.ma1, self.ma2, self.mb1])
        db.session.flush()
        # Projet A : 4 tickets (A1 : 1 terminé + 1 ouvert ; A2 : 1 ouvert ; 1 annulé sans module)
        self.a1_done = self._issue(self.pa, "A1 fait", [self.ma1], "completed")
        self.a1_open = self._issue(self.pa, "A1 ouvert", [self.ma1])
        self.a2_open = self._issue(self.pa, "A2 ouvert", [self.ma2])
        self.a_cancel = self._issue(self.pa, "Annulé", [], "cancelled")
        # Projet B : 2 tickets dans B1 (1 terminé), 1 hors module
        self.b1_done = self._issue(self.pb, "B1 fait", [self.mb1], "completed")
        self.b1_open = self._issue(self.pb, "B1 ouvert", [self.mb1])
        self.b_other = self._issue(self.pb, "B hors module", [])
        # Dépendances : A2 bloqué par A1 (inter-modules) ; B1 bloqué par A1 (inter-projets)
        add_relation(self.a2_open, self.a1_open, "blocked_by", self.admin)
        add_relation(self.b1_open, self.a1_open, "blocked_by", self.admin)
        self.ini = Initiative(name="Objectif commun", emoji="🎯", status="active", lead=self.member,
                              created_by=self.admin, start_date=date(2026, 10, 1), target_date=date(2026, 12, 31))
        self.ini.projects = [self.pa]
        self.ini.modules = [self.mb1]
        db.session.add(self.ini)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _user(self, username, role, first, last):
        u = User(username=username, email=User.internal_email(username), role=role, first_name=first, last_name=last)
        u.set_password("secret1")
        db.session.add(u)
        db.session.flush()
        return u

    def _issue(self, project, title, modules, group=None, target=None):
        i = create_issue(project, {"title": title, "module_ids": [m.id for m in modules], "target_date": target},
                         self.admin)
        if group:
            i.state = project.first_state_of(group)
        db.session.flush()
        return i

    def client(self, username="chef"):
        from flask import g
        for k in ("_login_user", "sidebar", "picker"):  # contexte d'application partagé avec le test
            g.pop(k, None)
        c = self.app.test_client()
        page = c.get("/login").get_data(as_text=True)
        token = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
        r = c.post("/login", data={"login": username, "password": "secret1", "_csrf": token})
        self.assertEqual(r.status_code, 302)
        return c, token


class ProgressTests(WorkgraphCase):
    def test_progress_union_of_projects_and_modules(self):
        s = W.Snapshot(TODAY).initiative(self.ini)
        # Projet A (4 tickets dont 1 annulé) + module B1 (2 tickets) = 6 tickets, 2 terminés sur 5 actifs
        self.assertEqual(s["stats"]["total"], 6)
        self.assertEqual(s["stats"]["done"], 2)
        self.assertEqual(s["pct"], 40)
        self.assertNotIn(self.b_other.id, s["issue_ids"])

    def test_module_linked_to_its_own_project_is_not_counted_twice(self):
        self.ini.modules.append(self.ma1)
        db.session.commit()
        self.assertEqual(W.Snapshot(TODAY).initiative(self.ini)["stats"]["total"], 6)

    def test_health_override_and_cancelled(self):
        self.ini.health_override = "off_track"
        s = W.Snapshot(TODAY).initiative(self.ini)
        self.assertEqual((s["health"], s["forced"]), ("off_track", True))
        self.ini.status = "cancelled"
        s = W.Snapshot(TODAY).initiative(self.ini)
        self.assertIsNone(s["health"])
        self.assertFalse(s["forced"])

    def test_effective_dates_fall_back_on_linked_items(self):
        self.ini.start_date = self.ini.target_date = None
        start, target = W.effective_dates(self.ini)
        self.assertEqual((start, target), (date(2026, 10, 1), date(2026, 12, 31)))

    def test_blockers(self):
        s = W.Snapshot(TODAY).initiative(self.ini)
        pairs = {(b["blocked"].id, b["blocker"].id, b["external"]) for b in s["blockers"]}
        self.assertEqual(pairs, {(self.a2_open.id, self.a1_open.id, False), (self.b1_open.id, self.a1_open.id, False)})
        self.assertEqual(s["blocked_open"], 2)

    def test_milestones(self):
        jalon = Label(project=self.pa, name="Jalon", color="#f00")
        db.session.add(jalon)
        self.a1_open.labels.append(jalon)
        self.a1_open.target_date = date(2026, 11, 15)
        db.session.commit()
        snap = W.Snapshot(TODAY)
        ms = W.milestones(self.ini, snap)
        titles = [m["title"] for m in ms]
        self.assertEqual(titles[0], "Début de l’initiative")
        self.assertIn("A1 ouvert", titles)
        self.assertIn("Module « Module A1 »", titles)
        self.assertEqual([m["date"] for m in ms], sorted(m["date"] for m in ms))

    def test_refresh_health(self):
        at = datetime(2026, 11, 10, 7)
        self.assertTrue(W.refresh_health(self.ini, "at_risk", at))
        self.assertFalse(W.refresh_health(self.ini, "at_risk", at + timedelta(days=1)))
        self.assertEqual(self.ini.health_since, at)


class GraphTests(WorkgraphCase):
    def graph(self, user=None, **kw):
        return W.build_graph(user or self.admin, today=TODAY, **kw)

    def test_nodes_and_links(self):
        g = self.graph()
        ids = {n["id"] for n in g["nodes"]}
        i = f"i:{self.ini.id}"
        self.assertTrue({"ws", i, "i:none", f"p:{self.pa.id}", f"p:{self.pb.id}", f"p:{self.pc.id}",
                         f"m:{self.ma1.id}", f"m:{self.mb1.id}"} <= ids)
        edges = {(e["s"], e["t"], e["kind"]) for e in g["edges"]}
        self.assertIn(("ws", i, "tree"), edges)
        self.assertIn((i, f"p:{self.pa.id}", "tree"), edges)
        self.assertIn((f"p:{self.pa.id}", f"m:{self.ma1.id}", "tree"), edges)
        self.assertIn((i, f"m:{self.mb1.id}", "direct"), edges)          # module lié seul
        self.assertIn(("i:none", f"p:{self.pb.id}", "tree"), edges)       # projet sans initiative
        self.assertIn(("i:none", f"p:{self.pc.id}", "tree"), edges)
        node = {n["id"]: n for n in g["nodes"]}[i]
        self.assertEqual((node["pct"], node["kind"], node["col"]), (40, "initiative", 1))

    def test_dependencies(self):
        g = self.graph()
        deps = {(d["s"], d["t"]): d for d in g["deps"]}
        mod = deps[(f"m:{self.ma1.id}", f"m:{self.ma2.id}")]
        self.assertEqual((mod["n"], mod["open"]), (1, 1))
        self.assertEqual(mod["items"][0]["blocker"], self.a1_open.key)
        self.assertIn((f"p:{self.pa.id}", f"p:{self.pb.id}"), deps)
        self.assertIn((f"m:{self.ma1.id}", f"m:{self.mb1.id}"), deps)
        nodes = {n["id"]: n for n in g["nodes"]}
        self.assertEqual(nodes[f"m:{self.ma1.id}"]["blocks"], 2)
        self.assertEqual(nodes[f"m:{self.ma2.id}"]["blocked_by"], 1)
        # le ticket bloquant est terminé : la dépendance reste mais n'est plus ouverte
        self.a1_open.state = self.pa.first_state_of("completed")
        db.session.commit()
        deps = {(d["s"], d["t"]): d for d in self.graph()["deps"]}
        self.assertEqual(deps[(f"m:{self.ma1.id}", f"m:{self.ma2.id}")]["open"], 0)

    def test_initiative_dependencies(self):
        other = Initiative(name="Autre", created_by=self.admin, status="planned")
        other.projects = [self.pc]
        db.session.add(other)
        c1 = self._issue(self.pc, "C bloqué", [])
        add_relation(c1, self.a2_open, "blocked_by", self.admin)
        db.session.commit()
        deps = {(d["s"], d["t"]) for d in self.graph()["deps"]}
        self.assertIn((f"i:{self.ini.id}", f"i:{other.id}"), deps)

    def test_private_project_hidden(self):
        g = self.graph(self.outsider)
        ids = {n["id"] for n in g["nodes"]}
        self.assertNotIn(f"p:{self.pb.id}", ids)
        self.assertNotIn(f"m:{self.mb1.id}", ids)
        self.assertIn(f"p:{self.pa.id}", ids)

    def test_filters(self):
        g = self.graph(filters={"status": ["planned"]})
        ids = {n["id"] for n in g["nodes"]}
        self.assertNotIn(f"i:{self.ini.id}", ids)
        self.assertNotIn(f"p:{self.pa.id}", ids)  # lié à une initiative masquée : pas dans « Sans initiative »
        self.assertIn(f"p:{self.pb.id}", ids)
        g = self.graph(filters={"mine": True})
        self.assertNotIn(f"i:{self.ini.id}", {n["id"] for n in g["nodes"]})
        g = self.graph(self.member, filters={"mine": True})
        self.assertIn(f"i:{self.ini.id}", {n["id"] for n in g["nodes"]})

    def test_project_and_team_tabs(self):
        g = self.graph(tab="project")
        self.assertFalse([n for n in g["nodes"] if n["kind"] in ("initiative", "group")])
        self.assertEqual({n["col"] for n in g["nodes"] if n["kind"] == "project"}, {1})
        g = self.graph(tab="team")
        edges = {(e["s"], e["t"]) for e in g["edges"]}
        self.assertIn((f"u:{self.member.id}", f"p:{self.pa.id}"), edges)    # responsable du projet A
        g = self.graph(tab="team", group="members")
        edges = {(e["s"], e["t"]) for e in g["edges"]}
        self.assertIn((f"u:{self.guest.id}", f"p:{self.pa.id}"), edges)

    def test_table_rows(self):
        rows = W.table_rows(self.graph())
        first = rows[0]
        self.assertEqual((first["node"]["id"], first["depth"]), (f"i:{self.ini.id}", 0))
        paths = [r["path"] for r in rows]
        self.assertIn(f"ws/i:{self.ini.id}/p:{self.pa.id}/m:{self.ma1.id}", paths)
        self.assertIn(f"ws/i:{self.ini.id}/m:{self.mb1.id}", paths)
        self.assertTrue(all(r["parent"] == r["path"].rsplit("/", 1)[0] for r in rows))

    def test_json_serializable(self):
        json.dumps(self.graph())


class IntegrationTests(WorkgraphCase):
    def test_ai_context(self):
        ctx = W.ai_context(f"/initiatives/{self.ini.id}", {}, self.admin)
        self.assertEqual(ctx["type"], "initiative")
        self.assertIn("Objectif commun", ctx["text"])
        self.assertIn("Dépendances bloquantes", ctx["text"])
        ctx = W.ai_context("/workgraph", {}, self.admin)
        self.assertEqual(ctx["type"], "workgraph")
        self.assertIn("Objectif commun", ctx["text"])
        self.assertIn("Projets sans initiative", ctx["text"])
        self.assertIsNone(W.ai_context("/agenda/", {}, self.admin))
        self.assertIsNone(W.ai_context("/initiatives/999", {}, self.admin))
        from hangar import ai
        self.assertIn(W.ai_context, ai._CONTEXT_RESOLVERS)

    def test_digest_section(self):
        self.assertIn("travail.initiatives", digest.sections_enregistrees())
        self.ini.health_override = "at_risk"
        db.session.commit()
        at = datetime.combine(TODAY, datetime.min.time()).replace(hour=7)
        s = W._digest_initiatives(digest.Contexte(user=self.member, jour=TODAY, at=at))
        self.assertIsNotNone(s)
        self.assertFalse(s.a_faire)
        self.assertIn("Objectif commun", s.html)
        self.assertEqual(self.ini.health_seen, "at_risk")
        # le lendemain, la santé n'a pas changé : rien de nouveau à signaler
        self.assertIsNone(W._digest_initiatives(digest.Contexte(user=self.member, jour=TODAY, at=at + timedelta(days=2))))
        # pas d'initiative pour ce compte
        self.assertIsNone(W._digest_initiatives(digest.Contexte(user=self.guest, jour=TODAY, at=at)))


class RouteTests(WorkgraphCase):
    def test_pages(self):
        c, _ = self.client()
        for url in ("/initiatives", "/initiatives?layout=cards&sort=progress", "/initiatives?status=active&health=on_track",
                    f"/initiatives/{self.ini.id}", "/workgraph", "/workgraph?view=table", "/workgraph?tab=project",
                    "/workgraph?tab=team&group=members", f"/workgraph?sel=i:{self.ini.id}&status=active"):
            r = c.get(url)
            self.assertEqual(r.status_code, 200, url)
        html = c.get("/workgraph").get_data(as_text=True)
        self.assertIn('id="wg-data"', html)
        data = c.get("/workgraph/data.json").get_json()
        self.assertTrue(any(n["id"] == f"i:{self.ini.id}" for n in data["nodes"]))
        self.assertIn("Objectif commun", c.get("/initiatives").get_data(as_text=True))
        self.assertEqual(c.get("/initiatives/999").status_code, 404)

    def test_create_edit_link_update_delete(self):
        c, tok = self.client("membre")
        r = c.post("/initiatives/new", data={"_csrf": tok, "name": "Nouvelle", "emoji": "🚀", "status": "draft",
                                             "lead_id": self.member.id, "start_date": "2026-11-01",
                                             "target_date": "2026-12-01", "project_ids": [self.pa.id, self.pc.id]})
        self.assertEqual(r.status_code, 302)
        ini = Initiative.query.filter_by(name="Nouvelle").one()
        self.assertEqual({p.id for p in ini.projects}, {self.pa.id, self.pc.id})
        self.assertEqual((ini.created_by_id, ini.status), (self.member.id, "draft"))
        # propriété seule
        c.post(f"/initiatives/{ini.id}/edit", data={"_csrf": tok, "field": "status", "value": "active"})
        c.post(f"/initiatives/{ini.id}/edit", data={"_csrf": tok, "field": "health", "value": "at_risk"})
        db.session.expire_all()
        self.assertEqual((ini.status, ini.health_override), ("active", "at_risk"))
        # dates incohérentes refusées
        c.post(f"/initiatives/{ini.id}/edit", data={"_csrf": tok, "field": "target_date", "value": "2026-10-01"})
        db.session.expire_all()
        self.assertEqual(ini.target_date, date(2026, 12, 1))
        # formulaire complet
        c.post(f"/initiatives/{ini.id}/edit", data={"_csrf": tok, "name": "Renommée", "description": "**But**",
                                                    "status": "planned", "lead_id": "", "health_override": ""})
        db.session.expire_all()
        self.assertEqual((ini.name, ini.lead_id, ini.health_override), ("Renommée", None, None))
        # lier / délier
        c.post(f"/initiatives/{ini.id}/link", data={"_csrf": tok, "project_ids": [self.pb.id],
                                                    "module_ids": [self.mb1.id]})
        db.session.expire_all()
        self.assertIn(self.pb, ini.projects)
        self.assertIn(self.mb1, ini.modules)
        c.post(f"/initiatives/{ini.id}/unlink", data={"_csrf": tok, "kind": "project", "id": self.pc.id})
        db.session.expire_all()
        self.assertNotIn(self.pc, ini.projects)
        # mise à jour d'avancement
        c.post(f"/initiatives/{ini.id}/updates", data={"_csrf": tok, "health": "off_track", "body": "Ça coince."})
        up = InitiativeUpdate.query.filter_by(initiative_id=ini.id).one()
        self.assertEqual((up.health, up.author_id), ("off_track", self.member.id))
        self.assertIsNotNone(up.progress)
        self.assertIn("Ça coince.", c.get(f"/initiatives/{ini.id}").get_data(as_text=True))
        # suppression (l'auteur peut supprimer ; les projets restent)
        r = c.post(f"/initiatives/{ini.id}/delete", data={"_csrf": tok})
        self.assertEqual(r.status_code, 302)
        self.assertIsNone(db.session.get(Initiative, ini.id))
        self.assertEqual(InitiativeUpdate.query.count(), 0)
        self.assertIsNotNone(db.session.get(Project, self.pb.id))

    def test_name_required(self):
        c, tok = self.client()
        c.post("/initiatives/new", data={"_csrf": tok, "name": "  "})
        self.assertEqual(Initiative.query.count(), 1)

    def test_guest_cannot_modify(self):
        c, tok = self.client("invite")
        self.assertEqual(c.get("/initiatives").status_code, 200)
        self.assertEqual(c.get(f"/initiatives/{self.ini.id}").status_code, 200)
        page = c.get("/workgraph").get_data(as_text=True)
        self.assertNotIn("new-initiative-modal", page)
        self.assertEqual(c.post("/initiatives/new", data={"_csrf": tok, "name": "X"}).status_code, 403)
        for path, data in ((f"/initiatives/{self.ini.id}/edit", {"field": "status", "value": "draft"}),
                           (f"/initiatives/{self.ini.id}/link", {"project_ids": self.pc.id}),
                           (f"/initiatives/{self.ini.id}/unlink", {"kind": "project", "id": self.pa.id}),
                           (f"/initiatives/{self.ini.id}/updates", {"body": "x"}),
                           (f"/initiatives/{self.ini.id}/delete", {})):
            self.assertEqual(c.post(path, data={"_csrf": tok, **data}).status_code, 403, path)
        db.session.expire_all()
        self.assertEqual((self.ini.status, len(self.ini.projects)), ("active", 1))

    def test_member_cannot_delete_others_initiative(self):
        other = Initiative(name="Pas à moi", created_by=self.admin, lead=self.admin)
        db.session.add(other)
        db.session.commit()
        c, tok = self.client("autre")
        self.assertEqual(c.post(f"/initiatives/{other.id}/delete", data={"_csrf": tok}).status_code, 403)
        c, tok = self.client("chef")
        self.assertEqual(c.post(f"/initiatives/{other.id}/delete", data={"_csrf": tok}).status_code, 302)

    def test_csrf_required(self):
        c, _ = self.client()
        self.assertEqual(c.post("/initiatives/new", data={"name": "Sans jeton"}).status_code, 400)

    def test_cannot_link_invisible_project(self):
        c, tok = self.client("autre")
        c.post(f"/initiatives/{self.ini.id}/link", data={"_csrf": tok, "project_ids": [self.pb.id]})
        db.session.expire_all()
        self.assertNotIn(self.pb, self.ini.projects)


if __name__ == "__main__":
    unittest.main()

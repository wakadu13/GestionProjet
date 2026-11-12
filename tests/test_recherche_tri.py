"""Recherche et tri à la française (pistes 25 et 60).

- Recherche insensible à la casse, aux accents, à la forme de l'apostrophe (’ ' ʼ) et aux ligatures (œ, æ) :
  palette Ctrl+K, liste des tickets, liaison de tickets, pages, membres, assistant IA, mentions « @ »,
  wiki, QCM ; « % » et « _ » tapés ne sont plus des jokers.
- Tri à la française : « anglais, Droit, Économie, Tactique, Zèbre », nombres dans l'ordre.

Lancer : uv run python -m unittest tests.test_recherche_tri -v
"""
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from flask import url_for

from hangar import create_app
from hangar.extensions import db
from hangar.models import AgendaEvent, AgendaSubjectLink, AgendaTask, Comment, Cycle, Label, Module, Page, Project, User
from hangar.services import create_issue, create_project, ensure_member
from hangar.texte import cle_tri, comparer, contient_sql, plier

ORDRE_FR = ["anglais", "Droit", "Économie", "Tactique", "Zèbre"]


class PlierTests(unittest.TestCase):
    def test_accents_casse_apostrophes_ligatures(self):
        self.assertEqual(plier("Établir le plan d’actions"), "etablir le plan d'actions")
        self.assertEqual(plier("ÉTAT DES LIEUX"), "etat des lieux")
        self.assertEqual(plier("NŒUD"), "noeud")
        self.assertEqual(plier("Cœur"), plier("coeur"))
        self.assertEqual(plier("Æther"), "aether")
        self.assertEqual(plier("l\u02bcétat"), plier("l'etat"))
        self.assertEqual(plier("10\u00a0h"), "10 h")  # espace insécable
        self.assertEqual(plier("« Oui »"), '" oui "')
        self.assertIsNone(plier(None))

    def test_plier_est_stable(self):
        """Plier deux fois ne change rien : la recherche plie la requête, la base plie la colonne."""
        for code in range(0x20, 0x3000):
            if 0xD800 <= code <= 0xDFFF:
                continue
            c = chr(code)
            self.assertEqual(plier(plier(c)), plier(c), hex(code))

    def test_fonction_sql_sans_jokers(self):
        self.assertTrue(contient_sql("Établir le plan d’actions", plier("plan d'actions")))
        self.assertTrue(contient_sql("Taux 50% atteint", "50%"))
        self.assertFalse(contient_sql("Lire 50 pages", "50%"))
        self.assertFalse(contient_sql("Groupe CL-25", "cl_25"))
        self.assertIsNone(contient_sql(None, "x"))

    def test_cle_tri(self):
        self.assertEqual(sorted(["Zèbre", "Tactique", "École", "anglais", "Droit", "Économie"], key=cle_tri),
                         ["anglais", "Droit", "École", "Économie", "Tactique", "Zèbre"])
        self.assertEqual(sorted(["Linux 10", "Linux 2", "linux 1"], key=cle_tri), ["linux 1", "Linux 2", "Linux 10"])
        # À égalité, la forme sans majuscule puis sans accent d'abord (comme localeCompare("fr")).
        self.assertEqual(sorted(["Élodie", "Elodie", "elodie"], key=cle_tri), ["elodie", "Elodie", "Élodie"])
        self.assertLess(comparer("École", "Zèbre"), 0)
        self.assertEqual(comparer("a", "a"), 0)


class BaseTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hangar-recherche-")
        self.app = create_app(self.tmp, testing=True)
        self.ctx = self.app.test_request_context("/")
        self.ctx.push()
        self.moi = self._user("moi", "admin", "Élodie", "Martin")
        self.autre = self._user("rlaz", "member", "Raphaël", "Lazare")
        db.session.commit()
        self.p = create_project("Optimisation des procédures d’intégration", "OPT", self.moi)
        ensure_member(self.p, self.autre)
        db.session.commit()
        self.i_etablir = self._issue("Établir le plan d’actions PI-EAE", "Lots et responsables.")
        self.i_evaluer = self._issue("Évaluer la solution (prise en main)", "")
        self.i_etudier = self._issue("Étudier le croisement intradef", "")
        self.i_noeud = self._issue("Nœud d’accès réseau", "Relier le cœur du réseau.")
        self.i_taux = self._issue("Taux 50% atteint", "")
        self.i_pages = self._issue("Lire 50 pages", "")
        self.i_cl = self._issue("Groupe CL_25 à rattraper", "")
        self.i_cl2 = self._issue("Groupe CL-25 cours", "")
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _user(self, name, role, first, last):
        u = User(username=name, email=f"{name}@hangar.local", role=role, first_name=first, last_name=last)
        u.set_password("secret1")
        db.session.add(u)
        return u

    def _issue(self, title, description):
        return create_issue(self.p, {"title": title, "description": description}, self.moi)

    def client(self):
        c = self.app.test_client()
        token = re.search(r'name="_csrf" value="([^"]+)"', c.get("/login").get_data(as_text=True)).group(1)
        r = c.post("/login", data={"login": "moi", "password": "secret1", "_csrf": token})
        self.assertEqual(r.status_code, 302)
        return c


# ---------------------------------------------------------------------------
# Piste 25 : recherche
# ---------------------------------------------------------------------------

class RechercheTests(BaseTestCase):
    def titres(self, r, cle="issues"):
        self.assertEqual(r.status_code, 200)
        return [x["title"] for x in r.get_json()[cle]]

    def test_palette_sans_accents_ni_majuscule(self):
        c = self.client()
        for q in ("Établir", "établir", "etablir", "ETABLIR"):
            self.assertIn("Établir le plan d’actions PI-EAE", self.titres(c.get("/api/search", query_string={"q": q})), q)

    def test_palette_apostrophe_du_clavier(self):
        c = self.client()
        self.assertIn("Établir le plan d’actions PI-EAE",
                      self.titres(c.get("/api/search", query_string={"q": "plan d'actions"})))

    def test_palette_ligature(self):
        c = self.client()
        self.assertIn("Nœud d’accès réseau", self.titres(c.get("/api/search", query_string={"q": "noeud d'acces"})))

    def test_palette_membres_projets_cycles_pages(self):
        db.session.add_all([Cycle(project_id=self.p.id, name="Itération d’été"),
                            Module(project_id=self.p.id, name="Sécurité"),
                            Page(project_id=self.p.id, title="État des lieux", content="Synthèse", owner_id=self.moi.id)])
        db.session.commit()
        c = self.client()
        for q in ("raphael", "RAPHAËL", "raphaël lazare"):
            self.assertEqual(self.titres(c.get("/api/search", query_string={"q": q}), "members"), ["Raphaël Lazare"], q)
        self.assertEqual(self.titres(c.get("/api/search", query_string={"q": "procedures"}), "projects"),
                         ["Optimisation des procédures d’intégration"])
        self.assertEqual(self.titres(c.get("/api/search", query_string={"q": "iteration d'ete"}), "cycles"),
                         ["Itération d’été"])
        self.assertEqual(self.titres(c.get("/api/search", query_string={"q": "securite"}), "modules"), ["Sécurité"])
        self.assertIn("État des lieux", self.titres(c.get("/api/search", query_string={"q": "etat"}), "pages"))
        page = c.get("/search", query_string={"q": "etablir"}).get_data(as_text=True)
        self.assertIn("Établir le plan", page)

    def test_jokers_cherches_tels_quels(self):
        c = self.client()
        self.assertEqual(self.titres(c.get("/api/search", query_string={"q": "50%"})), ["Taux 50% atteint"])
        self.assertEqual(self.titres(c.get("/api/search", query_string={"q": "CL_25"})), ["Groupe CL_25 à rattraper"])
        self.assertEqual(self.titres(c.get("/api/issues/lookup", query_string={"q": "_"})), ["Groupe CL_25 à rattraper"])

    def test_liaison_de_tickets(self):
        c = self.client()
        for q in ("etablir", "établir"):
            self.assertEqual(self.titres(c.get("/api/issues/lookup", query_string={"q": q})),
                             ["Établir le plan d’actions PI-EAE"], q)

    def test_liste_des_tickets_du_projet(self):
        c = self.client()
        page = c.get(url_for("issues.index", pid=self.p.id), query_string={"q": "etablir"}).get_data(as_text=True)
        self.assertIn("Établir le plan", page)
        self.assertNotIn("Nœud d’accès", page)
        page = c.get(url_for("issues.index", pid=self.p.id), query_string={"q": "coeur"}).get_data(as_text=True)
        self.assertIn("Nœud d’accès", page)  # trouvé par sa description (« cœur »)

    def test_liste_des_pages_et_des_projets(self):
        trouvee = Page(project_id=self.p.id, title="Évaluation finale", content="", owner_id=self.moi.id)
        autre = Page(project_id=self.p.id, title="Zoulou", content="", owner_id=self.moi.id)
        db.session.add_all([trouvee, autre])
        # Projets dont « moi » n'est pas membre : absents de la barre latérale, seule la liste les montre.
        create_project("Intégration réseau", "INR", self.autre)
        create_project("Zoulou", "ZOU", self.autre)
        db.session.commit()
        c = self.client()
        page = c.get(url_for("pages.index", pid=self.p.id), query_string={"q": "evaluation"}).get_data(as_text=True)
        self.assertIn(url_for("pages.view", pid=self.p.id, page_id=trouvee.id), page)
        self.assertNotIn(url_for("pages.view", pid=self.p.id, page_id=autre.id), page)
        page = c.get(url_for("projects.index"), query_string={"q": "integration"}).get_data(as_text=True)
        self.assertIn('<span class="key">INR</span>', page)  # carte du projet « Intégration réseau »
        self.assertNotIn('<span class="key">ZOU</span>', page)

    def test_taches_de_l_agenda(self):
        db.session.add(AgendaTask(user_id=self.moi.id, title="Réviser l’épreuve d’économie"))
        db.session.commit()
        c = self.client()
        page = c.get(url_for("agenda.tasks"), query_string={"q": "epreuve d'economie"}).get_data(as_text=True)
        self.assertIn("Réviser l’épreuve", page)


class AssistantTests(BaseTestCase):
    def test_chercher_tickets(self):
        from hangar.ai.tools import ToolBox

        tb = ToolBox(self.moi)
        for texte in ("évaluer solution", "evaluer solution", "EVALUER la SOLUTION"):
            self.assertIn("Évaluer la solution", tb.t_chercher_tickets(texte=texte, etat="tous"), texte)
        self.assertIn("Établir le plan", tb.t_chercher_tickets(texte="plan d'actions", etat="tous"))

    def test_chercher_tickets_repli_sur_un_des_mots(self):
        """« évaluation » n'est pas dans « Évaluer la solution » : les tickets qui ont au moins un des mots."""
        from hangar.ai.tools import ToolBox

        r = ToolBox(self.moi).t_chercher_tickets(texte="évaluation solution", etat="tous")
        self.assertTrue(r.startswith("Aucun ticket ne contient tous ces mots"), r)
        self.assertIn("Évaluer la solution", r)
        r = ToolBox(self.moi).t_chercher_tickets(texte="zzz", etat="tous")
        self.assertIn("ignore déjà la casse et les accents", r)

    def test_projet_et_pages(self):
        from hangar.ai.tools import ToolBox, _find_project

        self.assertEqual(_find_project("procedures", self.moi), self.p)
        self.assertEqual(_find_project("opt", self.moi), self.p)
        db.session.add(Page(project_id=self.p.id, title="Compte rendu", owner_id=self.moi.id,
                            content="Introduction.\n\n" + "Bla. " * 200 + "\n\nL’État-major décide."))
        db.session.commit()
        r = ToolBox(self.moi).t_chercher_pages("etat-major")
        self.assertIn("Compte rendu", r)
        self.assertIn("L’État-major décide", r)  # l'extrait est centré sur le mot trouvé

    def test_mentions(self):
        from hangar.ai.context import search_mentions

        for q in ("étudier", "etudier", "Étudier"):
            titres = [m["title"] for m in search_mentions(q, self.moi) if m["type"] == "ticket"]
            self.assertEqual(titres, [f"{self.i_etudier.key} · Étudier le croisement intradef"], q)
        projets = [m["title"] for m in search_mentions("integration", self.moi) if m["type"] == "projet"]
        self.assertEqual(projets, ["Optimisation des procédures d’intégration"])


class WikiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hangar-wiki-recherche-"))
        (self.tmp / "Réseaux").mkdir()
        (self.tmp / "Réseaux" / "Nœud d’accès.md").write_text(
            "# Nœud d’accès\n\nUn nœud d’accès relie le cœur du réseau à l’utilisateur.\n", encoding="utf-8")
        (self.tmp / "Coexister.md").write_text("Coexister avec les autres réseaux.\n", encoding="utf-8")
        for d in ("anglais", "Droit", "Économie", "Tactique", "Zèbre"):
            (self.tmp / d).mkdir()
            (self.tmp / d / "Cours.md").write_text("x", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def vault(self):
        from hangar.wiki.vault import Vault, scan

        return Vault(self.tmp, scan(self.tmp))

    def test_recherche_ligatures_et_apostrophes(self):
        v = self.vault()
        for q in ("nœud", "noeud", "NOEUD d'acces", "coeur", "l'utilisateur"):
            self.assertEqual([n.title for n, _ in v.search(q)], ["Nœud d’accès"], q)
        _, extrait = v.search("noeud")[0]
        self.assertIn("<mark>nœud</mark>", extrait)  # l'extrait surligne le mot tel qu'il est écrit
        self.assertEqual([n.title for n, _ in v.search("coexister")], ["Coexister"])

    def test_demander_au_wiki(self):
        from hangar.wiki import demander

        v = self.vault()
        notes = demander.pages_pertinentes(v, demander.mots_cles("Qu'est-ce qu'un noeud ?"))
        self.assertEqual([n.title for n in notes], ["Nœud d’accès"])

    def test_arbre_trie_a_la_francaise(self):
        v = self.vault()
        dossiers = [f["name"] for f in v.tree["folders"]]
        self.assertEqual([d for d in dossiers if d in ORDRE_FR], ORDRE_FR)
        self.assertEqual([d for d in v.folders() if d in ORDRE_FR], ORDRE_FR)


class QcmTests(unittest.TestCase):
    def test_reponse_tapee_avec_ou_sans_ligature(self):
        from hangar.qcm.quiz import Question, normaliser_reponse

        self.assertEqual(normaliser_reponse("noeud"), normaliser_reponse("Nœud."))
        self.assertEqual(normaliser_reponse("l'etat"), normaliser_reponse("L’État"))
        q = Question(id="t", type="TROU", enonce="Le ___ du réseau.", reponses_texte=["cœur"])
        self.assertTrue(q.est_correcte_texte("coeur"))

    def test_recherche_et_tri_des_sets(self):
        tmp = tempfile.mkdtemp(prefix="hangar-qcm-tri-")
        try:
            app = create_app(tmp, testing=True)
            app.config["QCM_VERIF_INTERVALLE"] = 0
            vault = Path(app.config["WIKI_DIR"])
            for cat in ORDRE_FR[::-1]:
                p = vault / "QCM" / cat / "set.json"
                p.parent.mkdir(parents=True, exist_ok=True)
                questions = [{"id": "q", "type": "QCM", "question": "Le plan d’actions ?", "choix": ["a", "b"],
                              "reponses": [0]}]
                p.write_text(json.dumps({"titre": f"Set {cat}", "questions": questions}, ensure_ascii=False),
                             encoding="utf-8")
            with app.test_request_context("/"):
                from hangar.qcm import quiz

                lib = quiz.get_library().pour(None)
                self.assertEqual(list(lib.categories()), ORDRE_FR)
                self.assertTrue(lib.get("QCM/Économie/set").correspond("economie plan d'actions"))
                db.session.remove()
                db.engine.dispose()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipUnless(shutil.which("node"), "Node.js absent")
class NavigateurTests(unittest.TestCase):
    """Le pliage écrit en JavaScript (palette, filtres, correction des QCM) donne le même résultat qu'en Python."""

    def test_meme_pliage_qu_en_python(self):
        racine = Path(__file__).resolve().parent.parent
        essais = ["Établir le plan d’actions", "NŒUD d’accès", "Æther", "10\u00a0h", "« Oui »", "Raphaël LAZARO",
                  "Saint\u2011Cyr", "l\u02bcétat", "Ça ira"]
        script = """
const fs = require("fs");
const [racine, essais] = [process.argv[1], JSON.parse(process.argv[2])];
const out = {};
for (const f of ["hangar/static/js/app.js", "hangar/static/wiki/palette.js", "hangar/static/wiki/graphe.js",
                 "hangar/static/qcm/qcm_index.js", "hangar/static/qcm/quiz.js", "hangar/static/qcm/langues_hors_ligne.js"]) {
  const m = fs.readFileSync(racine + "/" + f, "utf-8").match(/const (?:plier|normaliser|replier) = \\(s\\) => (String\\(s \\|\\| ''\\)[\\s\\S]*?);\\n/);
  out[f] = m ? essais.map(new Function("s", "return " + m[1])) : null;
}
console.log(JSON.stringify(out));
"""
        r = subprocess.run(["node", "-e", script, str(racine), json.dumps(essais)], capture_output=True, text=True,
                           encoding="utf-8", timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        attendu = [plier(e) for e in essais]
        for fichier, obtenu in json.loads(r.stdout).items():
            self.assertEqual(obtenu, attendu, fichier)


# ---------------------------------------------------------------------------
# Piste 60 : tri à la française
# ---------------------------------------------------------------------------

class TriTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        for nom, ident in (("Zèbre", "ZEB"), ("atelier perso", "ATP"), ("École de guerre", "EDG")):
            create_project(nom, ident, self.moi)
        db.session.commit()

    def test_projets(self):
        from hangar.utils import joined_projects, visible_projects

        attendu = ["atelier perso", "École de guerre", "Optimisation des procédures d’intégration", "Zèbre"]
        self.assertEqual([p.name for p in visible_projects(self.moi)], attendu)
        self.assertEqual([p.name for p in joined_projects(self.moi)], attendu)
        page = self.client().get("/").get_data(as_text=True)  # barre latérale
        positions = [page.find(nom) for nom in ("atelier perso", "École de guerre", "Zèbre")]
        self.assertTrue(all(x >= 0 for x in positions) and positions == sorted(positions), positions)

    def test_tickets_par_titre(self):
        from hangar import issue_query as IQ
        from hangar.models import Issue

        titres = [i.title for i in IQ.sort_issues(Issue.query.filter_by(project_id=self.p.id).all(), "title")]
        self.assertEqual(titres, ["Établir le plan d’actions PI-EAE", "Étudier le croisement intradef",
                                  "Évaluer la solution (prise en main)", "Groupe CL-25 cours", "Groupe CL_25 à rattraper",
                                  "Lire 50 pages", "Nœud d’accès réseau", "Taux 50% atteint"])

    def test_relations_triees_en_base(self):
        for nom in ("Zèbre", "économie", "Droit", "anglais", "Tactique"):
            db.session.add(Label(project_id=self.p.id, name=nom))
            db.session.add(Module(project_id=self.p.id, name=nom.capitalize()))
        db.session.commit()
        i = self.i_noeud
        i.labels = Label.query.filter_by(project_id=self.p.id).all()
        db.session.commit()
        db.session.expire_all()
        self.assertEqual([l.name for l in self.p.labels], ["anglais", "Droit", "économie", "Tactique", "Zèbre"])
        self.assertEqual([l.name for l in i.labels], ["anglais", "Droit", "économie", "Tactique", "Zèbre"])
        self.assertEqual([m.name for m in self.p.modules], ["Anglais", "Droit", "Économie", "Tactique", "Zèbre"])

    def test_pages_par_titre_et_membres(self):
        for t in ("Zoulou final", "État des lieux initial", "Analyse des écarts"):
            db.session.add(Page(project_id=self.p.id, title=t, content="", owner_id=self.moi.id))
        self._user("zoe", "member", "Zoé", "Durand")
        self._user("aline", "member", "aline", "Petit")
        db.session.commit()
        c = self.client()
        page = c.get(url_for("pages.index", pid=self.p.id), query_string={"sort": "title"}).get_data(as_text=True)
        positions = [page.find(t) for t in ("Analyse des écarts", "État des lieux initial", "Zoulou final")]
        self.assertTrue(all(x >= 0 for x in positions) and positions == sorted(positions), positions)
        noms = [m["name"] for m in c.get("/api/members").get_json()["members"]]
        self.assertEqual(noms, ["aline Petit", "Élodie Martin", "Raphaël Lazare", "Zoé Durand"])


class CompatibiliteTests(BaseTestCase):
    def test_normalize_ecrit_les_ligatures(self):
        from hangar.utils import normalize, slugify

        self.assertEqual(normalize("Cœur de réseau"), "coeur de reseau")
        self.assertEqual(normalize("Droit de l’homme"), normalize("Droit de l'homme"))
        self.assertEqual(slugify("Œuvre d’art"), "oeuvre-d-art")

    def test_lien_de_matiere_enregistre_avant(self):
        """Un lien de matière enregistré avec l'ancienne clé (« manuvre » pour « Manœuvre ») est retrouvé,
        puis réenregistré avec la nouvelle ; la couleur de la séance ne change pas."""
        import hashlib
        from datetime import datetime

        from hangar import agenda as A

        db.session.add(AgendaSubjectLink(user_id=self.moi.id, subject="manuvre", title="Manœuvre",
                                         wiki_path="Tactique"))
        ev = AgendaEvent(user_id=self.moi.id, uid="seance-1", title="Manœuvre", start=datetime(2026, 11, 12, 8),
                         end=datetime(2026, 11, 12, 10))
        db.session.add(ev)
        db.session.commit()
        self.assertEqual(A._subject_link(self.moi, ev.title).wiki_path, "Tactique")
        A.set_subject_link(self.moi, "Manœuvre", qcm_category="Tactique")
        db.session.commit()
        lien = AgendaSubjectLink.query.filter_by(user_id=self.moi.id).one()
        self.assertEqual((lien.subject, lien.wiki_path, lien.qcm_category), ("manoeuvre", "Tactique", "Tactique"))
        ancienne = int(hashlib.md5(b"manuvre").hexdigest()[:8], 16)
        self.assertEqual(A.event_color(ev), A.SUBJECT_COLORS[ancienne % len(A.SUBJECT_COLORS)])

    def test_lien_qcm_de_la_seance_ouvre_la_categorie(self):
        """Le lien « QCM » d'une séance vise l'ancre posée par la page QCM, même avec « œ » ou « ’ »."""
        from datetime import datetime

        from flask import render_template_string

        from hangar import agenda as A

        gabarit = (Path(self.app.root_path) / "templates" / "qcm" / "_categorie.html").read_text(encoding="utf-8")
        self.assertIn('id="cat-{{ cat.nom|qcm_slug }}"', gabarit)
        for i, categorie in enumerate(["Œuvre d’art", "Cœur de réseau", "Droit de l'homme", "Tactique"]):
            ev = AgendaEvent(user_id=self.moi.id, uid=f"seance-qcm-{i}", title=f"Matière {i}",
                             start=datetime(2026, 11, 12, 8), end=datetime(2026, 11, 12, 10))
            db.session.add(ev)
            A.set_subject_link(self.moi, ev.title, qcm_category=categorie)
            db.session.commit()
            url = A.subject_links(self.moi, ev)["qcm"]["url"]
            attendu = render_template_string("cat-{{ n|qcm_slug }}", n=categorie)
            self.assertEqual(url.split("#", 1)[1], attendu, categorie)


if __name__ == "__main__":
    unittest.main()

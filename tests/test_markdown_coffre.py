"""Coffre et Markdown (piste 72) : les QCM lisent le coffre par le wiki (un parcours, un cache, une liste
d'extensions, un délai), sont rendus par le moteur du wiki sans moteur de secours, et tous les rendus
Markdown (tickets, pages, IA, wiki, QCM) suivent la même règle : médias locaux seulement.

Lancer : uv run python -m unittest tests.test_markdown_coffre -v
"""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hangar import create_app
from hangar.extensions import db


class CoffreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hangar-md-coffre-")
        self.app = create_app(self.tmp, testing=True)
        self.vault = Path(self.app.config["WIKI_DIR"])
        self.vault.mkdir(parents=True, exist_ok=True)
        self.ctx = self.app.test_request_context("/")
        self.ctx.push()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ecrire(self, rel, contenu=b"x"):
        p = self.vault / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(contenu if isinstance(contenu, bytes) else contenu.encode("utf-8"))
        return p


class CoffreCommunTests(CoffreTestCase):
    def test_memes_regles_que_le_wiki(self):
        from hangar.qcm import coffre
        from hangar.wiki import render, vault
        for nom in ("IGNORED_DIRS", "INVALID_NAME_RE", "clean_key", "scan", "invalider"):
            self.assertIs(getattr(coffre, nom), getattr(vault, nom), nom)
        # Une seule liste d'extensions : celles que le wiki sait afficher (.3gp, .mkv compris).
        self.assertTrue(render.IMAGE_EXTS | render.AUDIO_EXTS | render.VIDEO_EXTS <= coffre.MEDIA_EXTS)
        self.assertIn(".mkv", coffre.MEDIA_EXTS)
        self.assertIn(".3gp", coffre.MEDIA_EXTS)

    def test_video_mkv_et_son_3gp_resolus_et_servis(self):
        from hangar.qcm import coffre
        self.app.config["WIKI_VERIF_INTERVALLE"] = 0
        self.ecrire("Médias/film.mkv")
        self.ecrire("son.3gp")
        self.assertEqual(str(coffre.resoudre("film.mkv")), "Médias/film.mkv")
        self.assertEqual(str(coffre.resoudre("son.3gp")), "son.3gp")
        from hangar.models import User
        u = User(username="eleve", email="eleve@hangar.local", role="admin")
        u.set_password("secret1")
        db.session.add(u)
        db.session.commit()
        c = self.app.test_client()
        import re
        token = re.search(r'name="_csrf" value="([^"]+)"', c.get("/login").get_data(as_text=True)).group(1)
        c.post("/login", data={"login": "eleve", "password": "secret1", "_csrf": token})
        self.assertEqual(c.get("/qcm/fichiers/M%C3%A9dias/film.mkv").status_code, 200)

    def test_un_seul_delai_de_verification(self):
        """Régler le délai du wiki suffit : les QCM voient un fichier ajouté sans attendre."""
        from hangar.qcm import coffre
        from hangar.wiki import vault
        self.app.config["WIKI_VERIF_INTERVALLE"] = 0
        self.app.config.pop("QCM_VERIF_INTERVALLE", None)
        self.ecrire("a.png")
        self.assertIsNotNone(coffre.resoudre("a.png"))
        self.ecrire("Dossier/b.png")  # déposé à la main : pas d'invalider()
        self.assertEqual(str(coffre.resoudre("b.png")), "Dossier/b.png")
        self.assertIn("Dossier/b.png", [f[0] for f in coffre.signature(coffre.racine())])
        self.assertIn("dossier/b.png", vault.get_vault().attachments)

    def test_ancien_reglage_qcm_garde_comme_alias(self):
        from hangar.wiki import vault
        self.app.config.update(WIKI_VERIF_INTERVALLE=2.0, QCM_VERIF_INTERVALLE=0)
        self.assertEqual(vault.intervalle_verif(), 0)
        self.app.config.pop("QCM_VERIF_INTERVALLE")
        self.assertEqual(vault.intervalle_verif(), 2.0)

    def test_une_ecriture_du_wiki_est_vue_par_les_qcm(self):
        from hangar.qcm import coffre
        from hangar.wiki import vault
        self.app.config.update(WIKI_VERIF_INTERVALLE=60)
        self.ecrire("a.png")
        vault.invalider()
        self.assertIsNotNone(coffre.resoudre("a.png"))
        self.ecrire("neuve.png")
        vault.invalider()  # écriture du wiki ou de l'IA
        self.assertIsNotNone(coffre.resoudre("neuve.png"))

    def test_images_et_lien_court(self):
        from hangar.qcm import coffre
        self.app.config["WIKI_VERIF_INTERVALLE"] = 0
        self.ecrire("A/schema.png")
        self.ecrire("B/schema.png")
        self.ecrire("seule.jpg")
        self.ecrire("note.md", "# Note")
        imgs = coffre.images()
        self.assertEqual([str(p) for p in imgs], ["A/schema.png", "B/schema.png", "seule.jpg"])
        self.assertEqual(coffre.lien_court(imgs[2]), "seule.jpg")
        self.assertEqual(coffre.lien_court(imgs[0]), "A/schema.png")
        self.assertEqual(str(coffre.resoudre("B/schema.png")), "B/schema.png")
        self.assertIsNone(coffre.resoudre("note.md"))  # une note n'est pas un média
        self.assertIsNone(coffre.resoudre("absente.png"))


class RenduQcmTests(CoffreTestCase):
    def test_plus_de_moteur_de_secours(self):
        from hangar.qcm import rendu
        self.assertFalse(hasattr(rendu, "rendu_local"))
        self.assertFalse(hasattr(rendu, "_moteur_wiki"))

    def test_erreur_du_moteur_notee(self):
        from hangar.qcm import rendu
        from hangar.wiki.render import Renderer
        with mock.patch.object(Renderer, "convert", side_effect=RuntimeError("panne")), \
                mock.patch("hangar.wiki.erreurs.noter") as noter, \
                self.assertLogs(self.app.logger, "WARNING"):
            out = rendu.rendre("Texte <b>gras</b> & co", True)
        noter.assert_called_once()
        self.assertIn("Texte &lt;b&gt;gras&lt;/b&gt; &amp; co", out)
        self.assertNotIn("<b>", out)

    def test_erreur_du_moteur_notee_une_seule_fois(self):
        """Un énoncé que le moteur ne sait pas rendre : texte brut gardé en cache, une seule écriture en base."""
        from hangar.qcm import rendu
        from hangar.wiki.models import ErreurServeur
        from hangar.wiki.render import Renderer
        with mock.patch.object(Renderer, "convert", side_effect=RuntimeError("panne 72")) as convert, \
                self.assertLogs(self.app.logger, "WARNING"):
            sorties = [rendu.rendre("Énoncé en panne 72 <i>", True) for _ in range(5)]
        self.assertEqual(convert.call_count, 1)
        self.assertEqual(len(set(sorties)), 1)
        self.assertIn("&lt;i&gt;", sorties[0])
        db.session.remove()
        erreurs = [e for e in ErreurServeur.query.all() if e.message == "panne 72"]
        self.assertEqual(len(erreurs), 1)
        self.assertEqual(erreurs[0].nombre, 1)
        # Le coffre change : on retente le rendu (le moteur peut aller mieux)
        self.ecrire("nouveau.png")
        from hangar.wiki import vault
        vault.invalider()
        self.assertNotIn("&lt;i&gt;", rendu.rendre("Énoncé en panne 72 <i>", True))

    def test_cache_garde_apres_un_nouveau_parcours_sans_changement(self):
        from hangar.qcm import rendu
        from hangar.wiki.render import Renderer
        self.app.config["WIKI_VERIF_INTERVALLE"] = 0  # le coffre est reparcouru à chaque appel
        self.ecrire("a.png")
        vrai = Renderer.convert
        with mock.patch.object(Renderer, "convert", autospec=True, side_effect=vrai) as convert:
            premier = rendu.rendre("Énoncé **unique** 72", True)
            second = rendu.rendre("Énoncé **unique** 72", True)
        self.assertEqual(premier, second)
        self.assertEqual(convert.call_count, 1)
        # Un vrai changement du coffre vide bien le cache
        self.ecrire("b.png")
        with mock.patch.object(Renderer, "convert", autospec=True, side_effect=vrai) as convert:
            rendu.rendre("Énoncé **unique** 72", True)
        self.assertEqual(convert.call_count, 1)

    def test_images_des_qcm_servies_par_le_wiki(self):
        from hangar.qcm import rendu
        self.app.config["WIKI_VERIF_INTERVALLE"] = 0
        self.ecrire("Images/schema.png")
        out = rendu.rendre("![[schema.png|100]]", True)
        self.assertRegex(out, r'src="/fichiers/Images/schema\.png"')


PIEGES = '''[x](data:text/html;base64,PHNjcmlwdD4=) <img src="https://exemple.org/a.png" alt="Logo"> \
<input type="text" value="z"> <video src="https://e.org/v.mp4"></video> <iframe src="/fichiers/a.png"></iframe> \
<img src="data:image/png;base64,AAAA"> <img src="/fichiers/ok.png">'''


class PolitiqueCommuneTests(CoffreTestCase):
    def rendus(self):
        from hangar.ai.rendering import render
        from hangar.qcm import rendu
        from hangar.utils import render_markdown
        from hangar.wiki.render import sanitize
        self.app.config["WIKI_VERIF_INTERVALLE"] = 0
        return {"ticket": str(render_markdown(PIEGES, link_refs=False)), "ia": str(render(PIEGES)),
                "qcm (moteur du wiki)": rendu.rendre(PIEGES, True), "wiki (HTML seul)": sanitize(PIEGES)}

    def test_une_seule_politique(self):
        from hangar import nettoyage_html
        from hangar.wiki import render as wiki_render
        self.assertIs(wiki_render.images_externes_en_liens, nettoyage_html.images_externes_en_liens)
        for lieu, out in self.rendus().items():
            with self.subTest(lieu=lieu):
                if "HTML seul" not in lieu:  # le lien Markdown n'est pas lu par le nettoyage seul
                    self.assertNotIn("data:", out)
                self.assertNotIn('src="data:', out)
                self.assertNotRegex(out, r"<(img|video|source|audio)[^>]+(exemple\.org|e\.org)")
                self.assertNotIn('type="text"', out)
                self.assertIn('src="/fichiers/ok.png"', out)
                # Une image externe écrite en HTML devient un lien visible, pas une balise vide
                self.assertIn("Image externe", out)
                self.assertIn('href="https://exemple.org/a.png"', out)

    def test_cadre_seulement_dans_le_wiki(self):
        from hangar import nettoyage_html
        out = nettoyage_html.nettoyer('<iframe src="/fichiers/a.png"></iframe>', {"iframe"}, {"iframe": {"src"}})
        self.assertNotIn("src=", out)
        out = nettoyage_html.nettoyer('<iframe src="/fichiers/a.png"></iframe>', {"iframe"}, {"iframe": {"src"}},
                                      prefixes_cadre=("/fichiers/",))
        self.assertIn('src="/fichiers/a.png"', out)


if __name__ == "__main__":
    unittest.main()

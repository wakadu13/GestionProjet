# Guide pour les IA — créer des pages et des QCM dans le wiki de Hangar

Ce document s'adresse à un agent IA chargé d'**ajouter du contenu** au wiki
(pages de cours, fiches, QCM). Suis-le à la lettre : le site est généré
automatiquement à partir des fichiers, il n'y a ni base de données de pages ni
étape de build.

---

## 1. Principe en 30 secondes

- Hangar affiche un **coffre Obsidian** : le dossier `vault/` des données de Hangar (`data/vault/` par
  défaut, ou celui donné par la variable `HANGAR_VAULT`). Dans ce guide, `vault/` désigne ce dossier.
- **1 fichier `.md` dans `vault/` = 1 page**, servie sur `/wiki/<chemin sans .md>`.
  Ex. `vault/Cours/BDD/5.Jointure.md` → `/wiki/Cours/BDD/5.Jointure`.
- **1 fichier `.json` dans `vault/` contenant une clé `questions` = 1 set de QCM**,
  visible sur `/qcm/`. Le **nom du dossier parent** devient la catégorie.
- Le wiki se réindexe tout seul (au plus 2 secondes après) si un fichier a changé :
  **aucun redémarrage, aucune migration, aucun commit n'est nécessaire** pour
  qu'une page apparaisse.
- Tu ne dois **modifier que des fichiers dans `vault/`**. Ne touche pas au code
  Python (`hangar/`), ni à `hangar.db`, ni à `secret.key`, ni à `vault/.obsidian/`,
  ni à `vault/.trash/`.

| Quoi | Chemin |
|---|---|
| Données de Hangar | `data/` à la racine du projet, ou le dossier donné par `--data` / `HANGAR_DATA` |
| Coffre (pages) | `data/vault/`, ou le dossier donné par `HANGAR_VAULT` (un fichier écrit est en ligne au plus tard 2 secondes après) |
| Copier un coffre existant | `uv run hangar wiki-import <dossier ou adresse git>` |

---

## 2. Arborescence et conventions de nommage

```
vault/
├── Accueil.md                 ← page d'accueil (ne pas renommer)
├── Guide/                     ← documentation du wiki (Syntaxe Obsidian.md, QCM.md)
├── Cours/<Matière>/           ← cours de l'école (BDD, TSN-CLEAE2-NED, …)
├── Hack/<Module>/             ← modules cybersécurité (Nmap, Linux, Metasploit, …)
├── QCM/<Catégorie>/*.json     ← sets de questions
├── image/  images/  Fichiers/ ← pièces jointes (png, svg, pdf…)
└── .obsidian/  .trash/        ← ignorés par le wiki, NE PAS TOUCHER
```

Règles pour un **nouveau module / cours** `vault/<Section>/<NomModule>/` :

1. Un fichier sommaire : **`00 - Sommaire <Nom lisible>.md`** (ex. `00 - Sommaire Nmap.md`).
2. Les chapitres numérotés sur 2 chiffres : **`01 - Titre du chapitre.md`**,
   `02 - …`. La barre latérale trie par nom de fichier : le préfixe numérique
   garantit l'ordre. TD/annexes : `TD1 - Numérisation.md`, `Formulaire - À retenir.md`.
3. Nom de dossier sans espace ni accent (`IntroductionToNetworking`, `BDD`).
   Les noms de fichier, eux, peuvent contenir espaces et accents.
4. Caractères **interdits** dans les noms : `\ : * ? " < > | # ^ [ ]`, et pas
   de nom commençant par `.` (fichier ignoré).
5. Évite les noms de fichier déjà utilisés ailleurs (`01 - Introduction.md`
   existe dans plusieurs dossiers) : un lien `[[01 - Introduction]]` résout
   d'abord vers le dossier de la note courante, mais un nom unique est plus sûr.
6. Encodage **UTF-8**, fins de ligne `\n`.

Pour qu'un module soit trouvable, ajoute un lien vers son sommaire depuis une
page existante pertinente (ex. `Accueil.md` ou le sommaire de la section).

---

## 3. Gabarits

### 3.1 Page sommaire (`00 - Sommaire <Nom>.md`)

```markdown
---
tags: [nmap, MOC]
---

# Nmap — Sommaire

> [!abstract] Présentation
> Une ou deux phrases sur le module, prérequis éventuels.

1. [[01 - Énumération|Énumération]]
2. [[02 - Introduction à Nmap|Introduction à Nmap]]
3. [[03 - Découverte d'Hôtes|Découverte d'Hôtes]]

## Révision
- QCM du module : [/qcm/](/qcm/) (catégorie « Nmap »)
```

### 3.2 Page chapitre (`NN - Titre.md`)

~~~~markdown
---
tags: [nmap, scan]
---
# Découverte d'Hôtes
Retour : [[00 - Sommaire Nmap]] · Précédent : [[02 - Introduction à Nmap]] · Suite : [[04 - Scan d'hôtes et de ports]]

Introduction courte du chapitre.

## Première notion
Texte explicatif en **français**, termes techniques en `code` ou avec la
traduction anglaise entre parenthèses (*ICMP echo request*).

```bash
sudo nmap 10.129.2.0/24 -sn -oA tnet
```

| Option | Description |
|---|---|
| `-sn` | Désactive le scan de ports. |

> [!tip] À retenir
> La phrase clé du chapitre.

> [!question]- Exercice : que fait `-Pn` ?
> Réponse repliée : désactive la découverte d'hôtes (ping).
~~~~

La ligne de navigation « Retour / Précédent / Suite » est la convention des
cours : mets-la sur chaque chapitre (sans « Précédent » sur le premier, sans
« Suite » sur le dernier).

### 3.3 Propriétés YAML (front-matter)

Optionnel, mais toujours en **tout début de fichier**, entre deux lignes `---`.

| Clé | Effet |
|---|---|
| `tags` | liste de tags → pages `/tags/<tag>` ; `a/b` crée une hiérarchie |
| `title` | remplace le nom du fichier comme titre affiché |
| `aliases` | autres noms utilisables dans `[[…]]` |
| toute autre clé (`cours`, `enseignant`, `annee`, `ordre`…) | affichée comme propriété en haut de la page (n'influence **pas** le tri) |

Si tu écris un titre `# …` dans le corps, ne mets pas `title:` (doublon).

---

## 4. Syntaxe prise en charge

Référence complète et exemple rendu : [Syntaxe Obsidian](/documentation/syntaxe/).

| Besoin | Syntaxe |
|---|---|
| Lien interne | `[[Nom de la note]]`, `[[Note\|texte affiché]]`, `[[Note#Titre]]`, `[[#Titre de cette page]]`, `[[Note#^id-bloc]]` |
| Lien externe | `[texte](https://…)` |
| Image | `![[schema.png]]`, largeur : `![[schema.png\|400]]` |
| PDF / audio / vidéo intégrés | `![[support.pdf]]` (lecteur PDF dans la page), `![[support.pdf#page=3]]` (ouvert à la page 3), `![[son.mp3]]`, `![[demo.mp4]]` |
| Inclure une autre note / section | `![[Note]]`, `![[Note#Titre]]` |
| Callout | `> [!note] Titre` ; types : `note`, `tip`, `info`, `warning`, `danger`, `example`, `question`, `success`, `failure`, `quote`, `abstract`, `important`… |
| Callout replié / déplié | `> [!question]- Titre` / `> [!example]+ Titre` |
| Maths | `$x^2$` en ligne, `$$ … $$` en bloc (KaTeX) |
| Diagramme | bloc ` ```mermaid ` |
| Mise en forme | `**gras**`, `*italique*`, `==surligné==`, `~~barré~~`, `` `code` `` |
| Tâches | `- [ ] à faire`, `- [x] fait` |
| Note de bas de page | `texte[^1]` puis `[^1]: source` |
| Tag en ligne | `#tag` (hors blocs de code) |
| Commentaire invisible | `%% non affiché %%` |

Pièges fréquents :

- **HTML brut** : il est nettoyé (anti-XSS). Pas de `<script>`, `<style>`,
  `onclick`, `style=` : n'en mets pas, écris en Markdown.
- **Images externes** : `<img src="https://…">` et `![](https://…)` ne sont
  jamais chargées ; le wiki affiche à leur place un lien « Image externe :
  légende ↗ ». Télécharge l'image dans `vault/image/` et intègre-la avec
  `![[nom.png]]` (`hangar wiki-import --images`, ou le bouton de la page
  Importer, le fait pour tout le coffre).
- **`#` en début de mot = tag** : écris `n°3` ou `` `#3` `` plutôt que `#3`
  dans le texte, et mets les commandes shell/commentaires dans des blocs de code.
- **`$` = formule** : pour un prix ou une variable shell hors code, échappe
  `\$` ou utilise `` `$HOME` ``.
- Une **liste ou un tableau** doit être précédé d'une ligne (texte ou vide) ;
  laisse une ligne vide avant un titre `##`.
- **Un lien vers une page inexistante** s'affiche en pointillés : vérifie
  l'orthographe exacte (accents, apostrophe `'` droite) du nom de fichier.
- Un fichier qui commence par un BOM ou du texte avant `---` n'aura pas ses
  propriétés reconnues.

---

## 5. Créer un set de QCM

Fichier : `vault/QCM/<Catégorie>/<nom>.json` (ex. `vault/QCM/Nmap/nmap_partie2.json`).
Utilise le **même nom de catégorie** que le dossier du cours associé.
Doc utilisateur : [Guide des QCM](/documentation/qcm/).

```json
{
  "titre": "Nmap — partie 2",
  "description": "NSE, performances, évasion de pare-feu.",
  "questions": [
    {"id": "nmap-nse-default", "type": "QCM", "question": "Quelle option lance les scripts NSE par défaut ?", "choix": ["-sV", "-sC", "-O", "-A"], "reponses": [1], "explication": "`-sC` = `--script=default`."},
    {"id": "nmap-timing", "type": "QCR", "question": "Quelles options influencent la vitesse du scan ?", "choix": ["-T4", "--min-rate", "-oA", "--max-retries"], "reponses": [0, 1, 3]},
    {"id": "nmap-all-ports", "type": "TROU", "question": "Option pour scanner les 65535 ports : ___", "reponses_texte": ["-p-", "-p 1-65535"]},
    {"id": "en-dog", "type": "QRC", "consigne": "Traduire en anglais", "question": "chien", "reponses_texte": ["dog"], "inverse": true, "consigne_inverse": "Traduire en français"},
    {"id": "nmap-decoy", "type": "FLASHCARD", "question": "Option `-D RND:5`", "verso": "Génère 5 adresses IP leurres (*decoys*) pour masquer la vraie source."}
  ]
}
```

| Type | Champs obligatoires | Règle |
|---|---|---|
| `QCM` | `question`, `choix`, `reponses` | **exactement 1** indice dans `reponses` |
| `QCR` | `question`, `choix`, `reponses` | ≥ 1 indice |
| `TROU` | `question`, `reponses_texte` | réponses acceptées ; casse, accents et espaces ignorés |
| `QRC` | `question`, `reponses_texte` | réponse courte tapée (traduction, synonyme) ; `consigne` optionnelle ; `inverse: true` (+ `consigne_inverse`) génère la question dans l'autre sens, id `<id>~inverse` |
| `FLASHCARD` | `question`, `verso` | carte recto/verso |

- `reponses` = **indices à partir de 0** dans `choix`.
- `explication` (optionnelle mais recommandée) s'affiche après la réponse.
- `id` : **stable et unique dans le fichier** (préfixe du module + mot-clé).
  La progression des élèves est liée à l'`id` : **ne renomme jamais l'`id`
  d'une question existante**, et n'en réutilise pas un pour une autre question.
  Sans `id`, c'est la position (`q1`, `q2`…) qui sert : insérer une question au
  milieu décalerait alors toute la progression.
- Markdown et LaTeX acceptés dans `question`, `choix`, `verso`, `explication`.
  **Dans le JSON, double les backslash** : `"$\\frac{a}{b}$"`, `"$\\pi$"`.
- 10 à 25 questions par set ; au-delà, crée `<nom>_partie2.json`.
- Varie la position de la bonne réponse et fais des distracteurs plausibles.
- Un set invalide n'apparaît pas et son erreur est affichée en haut de `/qcm/`.

### 5.1 Fichier de langue (module Langues)

Pour du vocabulaire, de la conjugaison ou de la grammaire de langue, préfère un
**fichier de langue** : il se reconnaît à sa clé `"langue"` (et l'absence de
`"questions"`). Fichier : `vault/QCM/langues/<nom>.json`. Plusieurs fichiers
d'une même langue sont regroupés. Exemples complets :
`vault/QCM/langues/langues_anglais.json`, `vault/QCM/langues/espagnol_debutant.json`.

```json
{
  "langue": "Anglais",
  "titre": "Anglais — vocabulaire du travail",
  "particules_optionnelles": ["to", "a", "an", "the"],
  "caracteres_speciaux": [],
  "vocabulaire": [
    {"id": "voc-001", "mot": "to achieve", "traductions": ["réussir", "accomplir"],
     "exemple": "She finally [achieved] her goal.", "exemple_traduction": "Elle a enfin atteint son objectif.",
     "note": "achievement (n.) = une réussite"}
  ],
  "conjugaison": [
    {"id": "irr-go", "verbe": "to go", "traduction": "aller", "temps": "verbe irrégulier",
     "formes": {"prétérit": "went", "participe passé": "gone"}}
  ],
  "grammaire": [
    {"id": "g1", "type": "TROU", "question": "She ___ (go) to school every day.", "reponses_texte": ["goes"]},
    {"id": "g2", "type": "ORDRE", "phrase": "She has never been to London.", "traduction": "Elle n'est jamais allée à Londres."},
    {"id": "g3", "type": "CORRECTION", "phrase": "He don't like coffee.", "corrections": ["He doesn't like coffee."]}
  ]
}
```

| Champ | Détail |
|---|---|
| `langue` | **obligatoire** ; regroupe les fichiers d'une même langue |
| `vocabulaire[]` | `mot`, et `traductions` ou `definition` (au moins l'un des deux) ; `definition` : définition courte **dans la langue étudiée**, sans le mot (cartes anglais → anglais) ; `exemple` avec le mot **entre crochets** (crée l'exercice en contexte), `exemple_traduction`, `note`, `prononciation`, `variantes` facultatifs |
| `conjugaison[]` | `verbe`, `temps`, `formes` (personne → forme, ou liste de formes acceptées) |
| `grammaire[]` | `TROU`, `QCM`, `QCR`, `FLASHCARD` (format des sets), `ORDRE` (`phrase`, `variantes`, `traduction`) ou `CORRECTION` (`phrase` fautive, `corrections`) |
| `particules_optionnelles` | mots facultatifs en tête de réponse (« achieve » accepté pour « to achieve ») |
| `caracteres_speciaux` | boutons d'insertion sous le champ (ex. `["á", "ñ", "¿"]`) |
| `nouveaux_par_jour` | quota de nouveautés par jour (défaut 20) |
| `exercices` | types de cartes de vocabulaire, parmi `decouverte` (carte FR → langue), `production` (traduction à taper FR → langue), `choix` (QCM : le bon mot parmi ceux du fichier), `contexte` (phrase à compléter), `contexte_choix` (QCM : le mot qui manque dans la phrase), `dictee` (le mot est lu par la synthèse vocale, on l'écrit), `ecoute_choix` (QCM : le mot est lu, choisir son sens), `reconnaissance` (carte langue → FR, ou → définition sans traduction), `traduction` (traduction à taper langue → FR), `choix_traduction` (QCM langue → FR), `definition` (carte définition → mot), `definition_saisie` (définition, mot à taper avec un indice), `definition_choix` (QCM définition → mot), `prononciation` (dire le mot, vérifié au micro). Les cartes de définition demandent `definition`, les cartes FR demandent `traductions`. La première de la liste, dans cet ordre, fait découvrir le mot. Absent : `["reconnaissance", "production", "contexte"]` |
| `code_langue` | code BCP 47 de la voix de synthèse (ex. `"en-US"`) ; sinon déduit du nom de la langue (`Anglais` → `en-GB`) |

Les `id` servent au suivi : garde-les **stables** et uniques. Avec une IA configurée dans Hangar, la page des QCM
génère aussi ces fichiers (« Générer avec l'IA »).

---

## 6. Qualité du contenu

- Langue : **français**, ton de cours clair ; garder les termes techniques
  anglais usuels (entre parenthèses ou en `code`).
- Un chapitre = une notion ; titres `##` / `###` (le `#` unique est le titre de
  la page ; la table des matières est générée depuis les titres).
- Mets en avant l'essentiel avec `> [!tip] À retenir`, les pièges avec
  `> [!warning]`, les exercices en `> [!question]-` (correction repliée).
- Commandes et sorties dans des blocs de code avec langage (`bash`, `python`, `sql`…).
- Ne recopie pas de données personnelles, identifiants ou mots de passe réels.
- Pas de page vide : si tu crées un lien vers une page future, crée aussi la page.

---

## 7. Vérifier son travail (obligatoire)

Depuis la racine du projet Hangar, lance :

```bash
uv run python -c "
from hangar import create_app
from hangar.wiki.vault import get_vault
from hangar.qcm.quiz import get_library
app = create_app()
with app.test_request_context('/'):
    v = get_vault()
    for n in v.sorted_notes(): v.render(n)
    print(len(v.notes), 'notes rendues')
    for t, src in sorted(v.unresolved.items()):
        if '://' not in t: print('LIEN CASSÉ:', t, '<-', sorted(s.key for s in src))
    lib = get_library()
    print(len(lib.sets), 'sets QCM')
    for e in lib.errors: print('ERREUR QCM:', e)
"
```

(ajoute `--data <dossier>` à `create_app("<dossier>")` si les données ne sont pas dans `data/`).
Puis vérifie les QCM et fichiers de langue (avertissements compris) :

```bash
uv run hangar qcm-verifier
```

Attendu : aucune ligne `ERREUR QCM` ni `✗`, et aucun `LIEN CASSÉ` provenant de **tes**
fichiers (des liens cassés préexistants peuvent exister, ne les « corrige » pas
en masse sans qu'on te le demande). Si tu as touché au code :
`uv run python -m unittest discover -s tests`.

---

## 8. Checklist finale

- [ ] Fichiers uniquement dans `vault/`, en UTF-8, noms sans caractère interdit.
- [ ] Nouveau module : `00 - Sommaire …` + chapitres `01 - …` numérotés.
- [ ] Front-matter avec `tags`, titre `#` unique, navigation Retour/Précédent/Suite.
- [ ] Tous les `[[liens]]` pointent vers des fichiers existants.
- [ ] Images copiées dans `vault/image/` et intégrées avec `![[…]]`.
- [ ] Sommaire relié depuis une page existante.
- [ ] QCM : `id` uniques, indices `reponses` corrects, backslash LaTeX doublés.
- [ ] Script de vérification (§7) lancé sans erreur.
- [ ] Ne pas faire de `git commit`/`push` sauf demande explicite.

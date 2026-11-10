# ✈️ Hangar — agenda, cours, QCM, assistant IA et gestion de projet, 100 % locaux

Application web en **Python** (Flask + SQLite) organisée comme [Plane](https://plane.so) : une barre d'icônes à gauche donne accès à six sections.

| Section | Contenu |
|---|---|
| **Agenda** | tableau de bord, emploi du temps importé chaque semaine (`.ics`), calendrier, tâches, rappels et **résumé quotidien par e-mail** |
| **Travail** | gestion de projet d'équipe : projets, tickets, cycles, modules, **initiatives**, **Workgraph**, planning, analytique, bilans |
| **Wiki** | le coffre Obsidian des cours (port complet de [Revision](https://github.com/wakadu13/Revision)) : rendu Obsidian, formules, éditeur, historique, PDF, graphe |
| **QCM** | révision espacée (FSRS) des sets de QCM et des langues, planning d'examens, statistiques |
| **IA** | assistant façon « Plane AI » branché sur **Claude** ou sur des **IA open source** (Ollama, LM Studio, vLLM…), avec les fonctions d'IA de Revision |
| **Réglages** | profil, e-mails, fournisseurs d'IA, sauvegardes |

Aucun service en ligne n'est nécessaire : les données restent sur ta machine et l'interface fonctionne hors connexion (KaTeX, Mermaid, CodeMirror et pdf.js sont embarqués, aucun CDN). Seul l'assistant IA contacte le fournisseur que tu choisis (et rien du tout avec un modèle local).

## Agenda : emploi du temps, tâches et rappels

Le **tableau de bord** (page d'accueil) montre la séance en cours ou la prochaine (avec compte à rebours), le programme du jour, la semaine en grille horaire, les tâches à faire, les contrôles à venir et les changements du dernier planning importé.

**Importer l'emploi du temps chaque semaine** (menu *Emploi du temps*, ou le bandeau du tableau de bord)
- Dépose le fichier `.ics` exporté de ton planning. **Le dernier fichier importé fait foi** :
  - chaque séance est reconnue par son identifiant (`UID`) : si l'horaire, la salle ou l'intervenant changent, elle est **mise à jour** et marquée « modifiée » (avant → après) ;
  - une séance absente du nouveau fichier est **retirée** (barrée dans le calendrier), seulement pour les semaines couvertes par le fichier et à partir d'aujourd'hui : le passé n'est jamais effacé ;
  - tes **notes**, **rappels** et **tâches liées** restent attachés à la séance d'un import à l'autre ;
  - chaque import a sa page de détail (ajouts, modifications, retraits) et le **dernier import peut être annulé**. Le fichier d'origine est conservé.
- Format de l'école reconnu : `Réseaux avancés 3 CM` → matière, n° de séance, type (CM, TD/TP, contrôle écrit, sport, militaire, conférence…) ; `(PREVISION)` → séance prévisionnelle (hachurée) ; rubriques `Apprenants` et `Intervenants` de la description. Les autres fichiers `.ics` (Google Agenda, Outlook…) fonctionnent aussi.
- En ligne de commande : `uv run hangar agenda-import planning.ics` (option `--mode future` pour remplacer tout le planning à venir, `--mode merge` pour ne rien retirer).

**Calendrier** : vues semaine (grille horaire, ligne « maintenant »), mois et liste ; clic sur une séance pour ses détails, une note personnelle, un rappel ou une tâche liée ; clic sur un créneau libre pour créer une tâche à cette heure ; export `.ics` (cours et/ou tâches, avec les rappels) vers Google Agenda / Outlook / téléphone ; impression.

**Tâches** : date, heure de début et de fin (ou journée entière), notes, priorité, catégorie, couleur, séance liée, **répétition** (tous les jours, du lundi au vendredi, chaque semaine, toutes les deux semaines, chaque mois — cocher l'occurrence passe à la suivante), report rapide (demain, lundi, +1 semaine). Raccourci clavier `T`.

**Rappels et e-mails** (menu *Rappels et e-mails*)
- Rappels par tâche ou par séance : à l'heure, 15 min, 1 h, la veille, 2 jours, 1 semaine avant… ou à une date précise ; par **e-mail + notification** dans l'application, ou notification seule.
- **Résumé quotidien** par e-mail (activé par défaut à 7 h) : un seul message avec tout ce qu'il y a à faire — cours du jour (horaires, salles), tâches du jour et en retard, tickets de projet à échéance, cartes de QCM à réviser et examens proches, contrôles de la semaine, changements récents du planning, initiatives à risque. Le matin pour la journée, ou la veille au soir pour le lendemain ; rien n'est envoyé les jours vides ; aperçu dans *Agenda › Résumé quotidien*.
- **Rappel automatique des contrôles** (la veille, 2 jours, 1 semaine avant), e-mail récapitulatif des **changements après chaque import**.
- Pour recevoir réellement les e-mails : renseigne ton adresse dans *Rappels et e-mails*, puis (administrateur) configure l'envoi dans *menu de l'espace › E-mails automatiques* — mode **SMTP**, préréglage Gmail avec un [mot de passe d'application](https://myaccount.google.com/apppasswords). Sinon, les messages sont seulement enregistrés en `.eml` dans `data/outbox/`.
- Les rappels partent tant que Hangar est lancé (`uv run hangar`). Si l'application était fermée à l'heure d'un rappel, il est envoyé à la réouverture, sauf si la séance ou la tâche est déjà terminée.

**Matières reliées au wiki et aux QCM** : dans la fenêtre d'une séance, relie la matière à sa collection du wiki et à sa catégorie de QCM (suggestions automatiques d'après le nom, le sigle ou l'intervenant). Pour un contrôle, « Préparer ce contrôle » fixe la date d'examen des QCM liés au jour du contrôle : les révisions s'organisent pour être prêt à temps.

## Wiki : les cours (coffre Obsidian)

Port complet du wiki de [Revision](https://github.com/wakadu13/Revision) : chaque fichier `.md` du coffre est une page.
- **Récupérer tes cours** : `uv run hangar wiki-import https://github.com/wakadu13/Revision` (ou le chemin d'un dossier Obsidian ; `--remplacer` pour écraser). Ou travaille directement dans un coffre existant : `HANGAR_VAULT=/chemin/du/coffre uv run hangar`.
- Syntaxe Obsidian : liens `[[…]]` et alias, intégrations `![[…]]` (images, PDF, notes, sections), encadrés `> [!note]`, propriétés YAML, tags, formules `$…$` / `$$…$$`, diagrammes Mermaid, surlignage, notes de bas de page…
- Menu façon Plane : collections (les dossiers), favoris, pages récentes, sommaire, tags, graphe, recherche (pages **et texte des PDF**).
- Éditeur CodeMirror (aperçu en direct, côte à côte, Ctrl+S, collage d'images, liens suggérés, brouillon de secours), historique des versions avec différences et restauration, import de fichiers (.md, PDF, images, LaTeX), lecteur PDF avec annotations, conversion LaTeX → page.

## QCM et révision espacée

Section séparée du wiki. Tout fichier `.json` du coffre contenant une clé `questions` est un set (catégorie = dossier, `QCM/` par défaut, comme dans Revision).
- Types QCM, QCR, texte à trous, réponse rédigée, flashcards, images masquées ; langues (vocabulaire, conjugaison, grammaire).
- Algorithme **FSRS** : chaque carte revient juste avant d'être oubliée ; « Réviser tout ce qui est dû » ; quotas par set, **date d'examen** avec quotas ajustés automatiquement.
- Statistiques (calendrier d'activité, série de jours, temps par matière), espace personnel, import/export JSON, CSV et Anki (`.apkg`), signalements.
- Les cartes dues apparaissent sur le tableau de bord et dans le résumé quotidien. `uv run hangar qcm-verifier` contrôle les fichiers.

## Assistant IA (Claude ou IA open source)

Bouton **Assistant IA** en haut à droite (ou Ctrl+J) : un panneau comme « Plane AI », qui connaît la page ouverte (cours du wiki, ticket, tableau de bord, set de QCM, workgraph…).
- **Fournisseurs** (*Réglages › Fournisseurs d'IA*, préréglages en un clic) : **Claude** via l'API Anthropic (SDK officiel, `claude-opus-5-5` par défaut, modèle rapide pour les petites tâches), ou toute API **compatible OpenAI** : **Ollama** (`ollama pull qwen2.5`, adresse `http://localhost:11434/v1`), LM Studio, vLLM, llama.cpp, OpenRouter, Mistral, Groq, Together, ou une adresse personnalisée. Test de connexion, liste des modèles, plafond de dépense mensuel, suivi de l'usage.
- Réponses en direct, « @ » pour ajouter du contexte, « / » pour les commandes ; l'assistant consulte tickets, tâches, agenda et wiki, et **propose des actions** que tu valides d'un clic : créer la tâche, créer le ticket, ajouter à la page, ajouter au cycle, changer une échéance.
- Fonctions d'IA de Revision : expliquer une réponse de QCM, corriger une réponse rédigée, générer un QCM ou des fiches de vocabulaire depuis un cours, « Demander au wiki » (réponses avec sources), tuteur d'une page, améliorer la rédaction dans l'éditeur, transformer un PDF ou du LaTeX en page, corriger une copie photographiée.
- En ligne de commande : `hangar ia-ajouter ollama`, `ia-tester`, `ia-demander "…"`, `ia-usage`.

## Travail : initiatives et Workgraph

En plus des projets, tickets, cycles et modules :
- **Initiatives** : objectifs qui regroupent des projets et des modules, avec avancement, **santé** calculée (dans les temps, à risque, en retard) et notes d'avancement datées.
- **Workgraph** : graphe Espace → Initiatives → Projets → Modules, avec les flèches de dépendances entre tickets (bloque / bloqué par) ; vue Tableau ; regroupements par initiative, projet ou équipe.

## Démarrage rapide

Prérequis : [uv](https://docs.astral.sh/uv/) (il installe Python et les dépendances tout seul).

```bash
cd "Projet Pluri/hangar"
uv run hangar
```

Le navigateur s'ouvre sur <http://127.0.0.1:5000>. Raccourcis : `./lancer.sh` (Linux/macOS) ou `lancer.bat` (Windows).

- **Première ouverture sur une base vide** : un écran d'installation crée l'espace de travail et le compte administrateur.
- **Données du projet EAE** : `uv run hangar demo` crée l'équipe (9 membres), le projet « Optimisation des procédures d'intégration EAE », 7 sprints de deux semaines et **123 tickets rédigés d'après les documents du dossier** (fiche de cadrage, comptes rendus, matrices RGPD, benchmark, plan d'actions, mails prêts à envoyer, démonstrateur…), avec les documents sources joints aux tickets et 12 pages de comptes rendus. Le planning Excel prévisionnel n'est pas utilisé (il reste importable : `uv run hangar demo --planning ../Planning/Plan_taches_gouvernance_donnees_v5.xlsx`).

Connexion par **identifiant et mot de passe** (pas d'e-mail) ; l'écran de connexion affiche les membres : clique sur ton nom puis tape le mot de passe.

| Identifiant | Mot de passe | Rôle |
|---|---|---|
| `wacogne` | `hangar2026` | Administrateur |
| `lazaro`, `brancourt`, `gali`, `claeys`, `niord`, `flohic`, `loubatier`, `alquarni` | `hangar2026` (à changer à la 1ʳᵉ connexion) | Membres |

> Prénoms retrouvés dans les documents : Raphaël Lazaro, Mathieu Brancourt, Vincent Flohic, Benoit Loubatier. Les autres se complètent depuis **Planning (Gantt)**
- Page **Planning** par projet et pour tout l'espace : couloirs par module (ou par assigné / cycle), barre de synthèse avec avancement, bandes des sprints, ligne « aujourd'hui », jalons en losanges, **flèches de dépendances** (en rouge quand les dates se contredisent).
- Zoom jour / semaine / mois / trimestre, glisser pour déplacer, tirer les bords pour changer les dates, filtres, impression / PDF.
- Onglet **Tendances** de l'analytique : flux cumulé (CFD), burn-up, vélocité par cycle, délais de réalisation, âge des tickets ouverts, date de fin estimée.

**Bilan**
- Bilan d'avancement par projet ou pour tout l'espace, sur la semaine, le mois, depuis le début ou une période libre : météo du projet (temps écoulé vs travail terminé), indicateurs, points d'attention générés automatiquement, réalisations par module, en cours, retards, blocages, jalons, avancement des modules et des cycles, contributions de chaque membre, documents produits, prochaines échéances.
- **Bilans enregistrés** (chiffres figés) avec sections rédigées : faits marquants, difficultés, décisions, prochaines étapes.
- Export **Word (.docx)**, Markdown, impression / PDF, et **envoi par e-mail**.

**E-mails automatiques** (menu de l'espace › E-mails automatiques)
- Notifications instantanées (assignation, mention, commentaire…), rappel quotidien des échéances, récapitulatif hebdomadaire, bilan hebdomadaire envoyé aux responsables, e-mails manuels avec modèles (relance, convocation, compte rendu, demande de documents).
- Mode **boîte d'envoi locale** par défaut : rien ne sort de la machine, les messages sont enregistrés en `.eml` dans `data/outbox/` (ouvrables avec Thunderbird/Outlook). Mode **SMTP** pour un envoi réel (préréglages Gmail avec mot de passe d'application, Outlook, Orange, Free).
- Chaque membre renseigne son adresse de notification et ses préférences dans **Mon profil** (l'adresse ne sert pas à la connexion).

**Équipe** ou **Mon profil**.

## Travailler à plusieurs (réseau local)

```bash
uv run hangar run --host 0.0.0.0
```

L'adresse à partager (ex. `http://192.168.1.20:5000`) s'affiche dans le terminal. Chaque membre se connecte avec son compte depuis son navigateur. Le serveur (waitress, 8 fils) gère sans souci une équipe de cette taille.

## Fonctionnalités

**Tickets (work items)**
- États personnalisables par groupe (Backlog, Non commencé, En cours, Terminé, Annulé), priorités, assignés multiples, étiquettes (avec parent), dates de début et d'échéance, estimations (points, Fibonacci, tailles, heures, jours-homme), cycle, modules.
- Sous-tickets avec progression, relations *bloque / bloqué par / lié à / doublon de*, liens, pièces jointes (glisser-déposer), duplication, déplacement vers un autre projet, archivage.
- Commentaires markdown avec réactions, **@mentions** et liens automatiques vers les tickets (`EAE-12`), historique complet de chaque modification, suivi du temps passé.

**Vues et organisation**
- 5 dispositions : **liste**, **tableau kanban** (glisser-déposer entre colonnes), **tableur** (modifications en masse), **calendrier** (glisser une échéance), **Gantt** (déplacer / redimensionner les barres).
- Filtres (état, priorité, assigné, étiquette, cycle, module, dates, créateur, abonnement, texte), regroupement, tri, propriétés affichées, **vues enregistrées** partagées, favoris.
- Modification en ligne de toutes les propriétés, ajout rapide dans chaque groupe.

**Pilotage**
- **Cycles** (sprints) : avancement, burndown, répartition par assigné / étiquette, transfert des tickets non terminés.
- **Modules** (phases, lots) avec statut, responsable, membres, dates, progression.
- **Intake** : boîte de réception des demandes (accepter, refuser, reporter, marquer comme doublon).
- **Pages** : wiki markdown avec modèles (compte rendu de réunion, d'entretien, note de décision, rétrospective, spécification), sommaire, historique des versions avec différences, verrouillage, pages privées, sauvegarde automatique, **import de documents Word (.docx)**.
- **Analytique** : indicateurs, répartition par état et priorité, tendance créés/terminés, analyse personnalisée (axe X × segment × métrique, export CSV), statistiques par membre.

**Équipe**
- Rôles d'espace (administrateur, membre, invité) et de projet, invitations avec mot de passe provisoire, désactivation, réinitialisation.
- Profils (statistiques, carte d'activité, tickets, temps passé), **charge de travail hebdomadaire**, capacité, **absences**.
- Notifications (assignation, mention, commentaire, changement d'état), mise en sommeil, archivage.

**Confort**
- Palette de commandes `Ctrl+K`, raccourcis clavier (`C` créer, `/` rechercher, `G` puis `H/I/P/N/A/T`, `?` aide), thème clair / sombre, pense-bêtes sur l'accueil, interface responsive.

**Données**
- Import Excel / CSV / JSON (détection automatique des colonnes : Titre/Tâche, Statut, Priorité, Responsable, Appui, Début, Échéance, Phase, Partie, Dépend de…), export Excel (avec feuille de synthèse), CSV, JSON, Markdown, calendrier iCalendar (.ics).
- Sauvegardes de la base en un clic (et restauration), export JSON complet.
- **API REST** `/api/v1` avec jetons personnels (voir *Paramètres › Jetons d'API*).

## Commandes

```bash
uv run hangar                       # lance le serveur (http://127.0.0.1:5000)
uv run hangar run --port 8080       # autre port ; --host 0.0.0.0 pour le réseau local ; --no-browser
uv run hangar demo                  # données de démonstration du projet EAE
uv run hangar demo --force          # efface puis recrée la démonstration
uv run hangar rebuild-docs          # recrée les tickets du projet EAE d'après les documents (sauvegarde avant)
uv run hangar agenda-import planning.ics  # importe l'emploi du temps (.ics) ; --user identifiant, --mode weeks|future|merge
uv run hangar mail-send             # envoie la file d'e-mails, les rappels de l'agenda et les envois programmés maintenant
uv run hangar wiki-import <dossier|url git> [--remplacer]   # importe un coffre Obsidian (cours) dans le wiki
uv run hangar wiki-indexer-pdf      # lit le texte de tous les PDF du wiki (recherche)
uv run hangar qcm-verifier          # vérifie les fichiers de QCM et de langue
uv run hangar ia-ajouter ollama     # ajoute un fournisseur d'IA (anthropic, ollama, lmstudio, vllm, openrouter, mistral…)
uv run hangar ia-tester             # teste la connexion aux fournisseurs d'IA
uv run hangar ia-usage              # dépense et jetons de l'IA ce mois-ci
uv run hangar mail-test adresse@ex.fr  # e-mail de test
uv run hangar backup                # sauvegarde dans data/backups/
uv run hangar reset                 # efface toutes les données (sauvegarde de sécurité avant)
uv run hangar create-admin          # crée ou répare un compte administrateur
uv run hangar --data /chemin run    # utiliser un autre dossier de données
uv run python -m unittest discover -s tests   # tests (lancés aussi par GitHub Actions sous Linux et Windows)
```

## Où sont les données ?

Tout est dans `hangar/data/` :

- `hangar.db` : la base SQLite (projets, tickets, pages, comptes…)
- `uploads/` : les pièces jointes
- `backups/` : les sauvegardes
- `outbox/` : les e-mails en mode « boîte d'envoi locale »
- `agenda/` : les fichiers `.ics` importés (un dossier par utilisateur)
- `vault/` : le coffre du wiki (pages `.md`, images, PDF, sets de QCM `.json`) — ou le dossier indiqué par `HANGAR_VAULT`
- `wiki_versions/` : l'historique des pages du wiki
- `qcm_perso/` : les sets de l'espace personnel de chaque compte
- `copies/` : les photos des copies corrigées par l'IA
- `secret.key` : la clé de session (générée automatiquement)

Pour transférer Hangar sur une autre machine, copie le dossier `hangar/` entier (données comprises).

## Structure du code

```
hangar/
├── pyproject.toml          dépendances (uv)
├── hangar/
│   ├── __init__.py         application Flask, sécurité (CSRF), aides de gabarits
│   ├── cli.py              ligne de commande
│   ├── models.py           modèle de données (SQLAlchemy)
│   ├── services.py         logique métier : tickets, historique, notifications
│   ├── issue_query.py      filtres, tris, regroupements, calendrier, Gantt
│   ├── ics.py              lecture des fichiers .ics (format de l'école et iCalendar standard)
│   ├── digest.py           résumé quotidien par e-mail (chaque module y ajoute sa section)
│   ├── workgraph.py        initiatives : avancement, santé, graphe
│   ├── wiki/               wiki (port de Revision : vault, render, latex, historique, texte_pdf…)
│   ├── qcm/                QCM et révision espacée (port de Revision : quiz, fsrs, langues, planning…)
│   ├── ai/                 assistant IA : fournisseurs (Claude, compatibles OpenAI), panneau, outils, fonctions
│   ├── agenda.py           agenda : imports (le dernier fait foi), tâches, rappels, vues semaine / mois
│   ├── importer.py         import/export Excel, CSV, JSON, Word
│   ├── charts.py           graphiques SVG générés en Python
│   ├── seed.py             données de démonstration
│   ├── views/              routes (une par domaine : tickets, cycles, pages, équipe, API…)
│   ├── templates/          gabarits Jinja
│   └── static/             CSS et JavaScript (sans dépendance)
├── tests/                  tests de l'agenda (uv run python -m unittest discover -s tests)
└── data/                   base et fichiers (créé au premier lancement)
```

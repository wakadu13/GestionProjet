# ✈️ Hangar — gestion de projet et d'équipe, 100 % locale

Application web en **Python** (Flask + SQLite) inspirée de [Plane](https://plane.so), pensée pour piloter une équipe projet sans aucun service en ligne : les données restent sur ta machine, l'interface fonctionne hors connexion (aucune ressource externe, aucun CDN).

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
uv run hangar mail-send             # envoie la file d'e-mails et les envois programmés maintenant
uv run hangar mail-test adresse@ex.fr  # e-mail de test
uv run hangar backup                # sauvegarde dans data/backups/
uv run hangar reset                 # efface toutes les données (sauvegarde de sécurité avant)
uv run hangar create-admin          # crée ou répare un compte administrateur
uv run hangar --data /chemin run    # utiliser un autre dossier de données
```

## Où sont les données ?

Tout est dans `hangar/data/` :

- `hangar.db` : la base SQLite (projets, tickets, pages, comptes…)
- `uploads/` : les pièces jointes
- `backups/` : les sauvegardes
- `outbox/` : les e-mails en mode « boîte d'envoi locale »
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
│   ├── importer.py         import/export Excel, CSV, JSON, Word
│   ├── charts.py           graphiques SVG générés en Python
│   ├── seed.py             données de démonstration
│   ├── views/              routes (une par domaine : tickets, cycles, pages, équipe, API…)
│   ├── templates/          gabarits Jinja
│   └── static/             CSS et JavaScript (sans dépendance)
└── data/                   base et fichiers (créé au premier lancement)
```

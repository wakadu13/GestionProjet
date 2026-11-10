"""QCM et révision espacée (port de github.com/wakadu13/Revision).

Section « QCM » de Hangar (blueprint `qcm`, adresses `/qcm/…`). Les sets sont des fichiers JSON du
coffre du wiki (`WIKI_DIR`, dossier `QCM/` par défaut ; la catégorie est le dossier parent) ou de
l'espace personnel de chaque compte (`DATA_DIR/qcm_perso/<user_id>/`).

Modules :
- `quiz`, `fsrs`, `langues`, `planning`, `paquets`, `anki`, `verifier`, `masque` : logique de Revision,
  gardée telle quelle (seuls les réglages Django et le parcours du coffre changent) ;
- `coffre` (fichiers du coffre), `rendu` (Markdown + LaTeX, moteur du wiki s'il est là) ;
- `models` (progression FSRS, réglages, activité, temps, objectifs, signalements, explications de l'IA) ;
- `services` : `resume_du_jour(user)`, section « qcm.revisions » du résumé quotidien, contexte du panneau IA ;
- `views`, `gestion`, `langues_views`, `masque_views` : les pages ; `cli` : `hangar qcm-verifier`.

Pour l'agent IA : `services.bibliotheque(user_id).get(set_id)` donne un set (ses `questions`),
`rendu.rendre(texte)` le HTML d'une explication, `models.Explication` le cache des explications,
`paquets.lire_liste(form, fichier)` / `paquets.ecrire_paquet_langue()` / `paquets.ecrire_set_qcm()`
l'écriture des paquets et des QCM générés.
"""

"""Planificateur de révisions FSRS (Free Spaced Repetition Scheduler, v4.5).

Porté depuis l'application « revision_qcm » (src/fsrs.rs).

FSRS modélise la mémoire de chaque élément par deux variables :
- la **stabilité** S (en jours) : délai au bout duquel la probabilité de rappel
  tombe à 90 % ;
- la **difficulté** D (de 1 à 10) : vitesse à laquelle S augmente.

La probabilité de rappel (« retrievabilité ») après t jours vaut
R(t, S) = (1 + 19/81 · t/S)^(-0,5). Chaque révision met à jour S et D selon la
note donnée et le moment où elle a eu lieu : une révision réussie alors que le
souvenir était presque oublié (R faible) fait davantage progresser S.
L'élément est reprogrammé quand R atteint la rétention visée (90 %).

Pour une même rétention, FSRS demande nettement moins de révisions que les
boîtes de Leitner ou SM-2 ; les paramètres ci-dessous sont les valeurs par
défaut publiées, optimisées sur des centaines de millions de révisions réelles.
"""

import math
from dataclasses import dataclass

# Paramètres par défaut de FSRS-4.5.
W = (
    0.4072, 1.1829, 3.1262, 15.4722, 7.2102, 0.5316, 1.0651, 0.0234, 1.616, 0.1544, 1.0824, 1.9813,
    0.0953, 0.2975, 2.2042, 0.2407, 2.9466,
)
DECAY = -0.5
FACTOR = 19 / 81
RETENTION = 0.9   # probabilité de rappel visée au moment de la révision
S_MIN = 0.01      # stabilité minimale (évite les divisions par zéro)

# Notes d'une révision (même valeur que dans FSRS).
A_REVOIR, DIFFICILE, BIEN, FACILE = 1, 2, 3, 4
NOTES = (A_REVOIR, DIFFICILE, BIEN, FACILE)


@dataclass(frozen=True)
class Etat:
    stabilite: float
    difficulte: float


def retrievabilite(jours: float, stabilite: float) -> float:
    """Probabilité de se souvenir de l'élément `jours` après la dernière révision."""
    return (1 + FACTOR * max(jours, 0.0) / max(stabilite, S_MIN)) ** DECAY


def _clamp(x, bas, haut):
    return min(haut, max(bas, x))


def _difficulte_initiale(note: int) -> float:
    return _clamp(W[4] - (note - 3) * W[5], 1.0, 10.0)


def premiere(note: int) -> Etat:
    """État après la toute première révision."""
    return Etat(W[note - 1], _difficulte_initiale(note))


def suivante(etat: Etat, jours: float, note: int) -> Etat:
    """État après une révision faite `jours` après la précédente."""
    s, d = max(etat.stabilite, S_MIN), etat.difficulte
    r = retrievabilite(jours, s)

    # Difficulté : ajustée selon la note, avec retour progressif vers la moyenne.
    d_note = d - W[6] * (note - 3)
    d2 = _clamp(W[7] * _difficulte_initiale(FACILE) + (1 - W[7]) * d_note, 1.0, 10.0)

    if note == A_REVOIR:
        # Oubli : la stabilité repart d'une valeur basse (jamais plus haute qu'avant).
        s_oubli = W[11] * d ** -W[12] * ((s + 1) ** W[13] - 1) * math.exp(W[14] * (1 - r))
        s2 = min(s_oubli, s)
    else:
        penalite = W[15] if note == DIFFICILE else 1.0
        bonus = W[16] if note == FACILE else 1.0
        s2 = s * (1 + math.exp(W[8]) * (11 - d) * s ** -W[9]
                  * (math.exp(W[10] * (1 - r)) - 1) * penalite * bonus)
    return Etat(max(s2, S_MIN), d2)


def intervalle_jours(stabilite: float, retention: float = RETENTION) -> float:
    """Intervalle (en jours) avant que la probabilité de rappel retombe à `retention`
    (90 % par défaut ; plus haut à l'approche d'un examen : révisions plus serrées)."""
    return stabilite / FACTOR * (retention ** (1 / DECAY) - 1)

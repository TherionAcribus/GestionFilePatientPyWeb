"""Diagnostic de démarrage affiché sur l'écran « Borne hors ligne ».

Une borne dont la configuration est valide mais fausse (secret refusé par le
serveur, URL qui ne mène pas au serveur Flowelis, réseau coupé) restait
indéfiniment sur « Borne hors ligne » sans dire pourquoi. Ce module traduit la
dernière panne de la boucle d'initialisation en une ligne destinée au
technicien, ajoutée discrètement sous le message patient.

Deux sortes de pannes :

- **certainement de configuration** (secret refusé, page introuvable) :
  signalées dès le premier échec ;
- **possiblement passagères** (réseau, serveur en redémarrage) : signalées
  seulement si elles durent ``DIAGNOSIS_DELAY_S``, pour ne pas inquiéter à
  chaque démarrage où le réseau arrive quelques secondes après la borne.

Logique pure (aucune dépendance à Qt ni au réseau) : testable seule.
"""

from dataclasses import dataclass

from errors import (
    SecretRejectedError,
    ServerResponseError,
    SessionUnavailableError,
    TokenUnavailableError,
)
from maintenance_pin import MAINTENANCE_KEY

#: Délai avant d'afficher une panne qui peut n'être que passagère.
DIAGNOSIS_DELAY_S = 120


@dataclass(frozen=True)
class Diagnosis:
    """Panne à signaler au technicien."""

    code: str
    message: str
    #: Vrai si la panne est certainement de configuration : affichée sans
    #: attendre ``DIAGNOSIS_DELAY_S``.
    immediate: bool

    def screen_text(self) -> str:
        """Texte complet de la ligne technicien (non échappé)."""
        return (f"{self.message} Touche {MAINTENANCE_KEY} (clavier) : "
                "configuration de la borne.")


def diagnose(error, base_url: str) -> Diagnosis | None:
    """Diagnostic correspondant à une panne d'initialisation, ou None si la
    panne n'a rien d'utile à dire au technicien."""
    if isinstance(error, SecretRejectedError):
        return Diagnosis(
            "secret_refused",
            f"Le serveur refuse le secret d'application de cette borne (HTTP "
            f"{error.status_code}). Vérifiez le secret saisi sur la borne et "
            "celui configuré sur le serveur.",
            immediate=True)
    if isinstance(error, ServerResponseError):
        if error.status_code in (404, 405):
            return Diagnosis(
                "not_flowelis",
                f"L'adresse {base_url} répond, mais ce n'est pas un serveur "
                f"Flowelis (HTTP {error.status_code}). Vérifiez l'URL du "
                "serveur.",
                immediate=True)
        return Diagnosis(
            "server_error",
            f"Le serveur {base_url} répond anormalement ({error}).",
            immediate=False)
    if isinstance(error, TokenUnavailableError):
        return Diagnosis(
            "unreachable",
            f"Aucune réponse du serveur {base_url} depuis plus de "
            f"{DIAGNOSIS_DELAY_S // 60} minutes. Vérifiez le réseau, que le "
            "serveur est démarré et l'URL configurée.",
            immediate=False)
    if isinstance(error, SessionUnavailableError):
        return Diagnosis(
            "session_refused",
            f"Le serveur accepte le secret mais ne délivre pas de session "
            f"borne ({error}).",
            immediate=False)
    return None


class DiagnosisTracker:
    """Suit la panne en cours et décide quand l'afficher.

    Le délai court depuis le DÉBUT de la panne (pas depuis le dernier
    échec) ; il repart à zéro quand la nature de la panne change ou après
    un succès."""

    def __init__(self, delay_s: float = DIAGNOSIS_DELAY_S):
        self._delay_s = delay_s
        self._current = None
        self._since = None

    def record_failure(self, diagnosis: Diagnosis | None, now: float) -> None:
        if diagnosis is None:
            return
        if self._current is None or self._current.code != diagnosis.code:
            self._since = now
        self._current = diagnosis

    def record_success(self) -> None:
        self._current = None
        self._since = None

    def visible(self, now: float) -> Diagnosis | None:
        """Diagnostic à afficher maintenant, ou None."""
        if self._current is None:
            return None
        if self._current.immediate or now - self._since >= self._delay_s:
            return self._current
        return None

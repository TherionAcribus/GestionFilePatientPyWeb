# errors.py
"""Exceptions métier de la borne.

Pourquoi
--------
Le code levait des ``Exception`` génériques (« Impossible d'obtenir le token »,
« imprimante sans token ») et les rattrapait avec des ``except Exception`` tout
aussi génériques. Conséquences : impossible de distinguer une panne ATTENDUE
(serveur injoignable, imprimante absente — la borne doit réessayer) d'un BOGUE
(faute de frappe, attribut manquant — qui doit remonter dans les logs avec sa
trace), et un ``except Exception`` avalait silencieusement les deux.

Convention
----------
- Tout ce qui hérite de :class:`BorneError` est une panne **attendue et gérée** :
  on la rattrape par son type, on journalise un message court, on réessaie.
- Tout le reste est **inattendu** : les rares ``except Exception`` restants sont
  des *frontières* (boucle de thread qui ne doit pas mourir, pont JavaScript qui
  doit toujours renvoyer un dictionnaire, chemin de fermeture). Ils sont
  documentés comme tels et journalisent avec ``logger.exception`` pour conserver
  la trace complète.
"""


class BorneError(Exception):
    """Base de toutes les pannes attendues de la borne."""


class TokenUnavailableError(BorneError):
    """Le jeton d'application n'a pas pu être obtenu auprès du serveur.

    Cas normal d'une borne démarrée hors ligne ou pendant une coupure : la
    boucle d'initialisation réessaie avec un backoff, l'écran « Borne hors
    ligne » reste affiché."""


class PrinterNotReadyError(BorneError):
    """Initialisation de l'imprimante demandée alors que la borne n'a pas encore
    de jeton d'application (les statuts imprimante ne pourraient pas être
    authentifiés)."""


class SessionUnavailableError(BorneError):
    """Le serveur n'a pas délivré de ticket de session borne.

    Panne attendue (serveur injoignable, jeton refusé, réponse inattendue) : la
    boucle d'initialisation réessaie avec backoff, comme pour le token."""


class PrintPayloadError(BorneError, ValueError):
    """Charge d'impression refusée par la validation (base64 invalide, ticket
    trop long, commande ESC/POS non autorisée...).

    Hérite aussi de :class:`ValueError` : c'était le type levé historiquement
    par ``decode_and_validate_print_payload``, et les appelants qui filtrent sur
    ``ValueError`` continuent donc de fonctionner. Le message ne contient JAMAIS
    le contenu du ticket."""

# editor_logic.py
"""Logique pure de l'éditeur de configuration de la borne (``config-editor.py``).

Extraite du module d'interface (tkinter, non importable — son nom contient un
tiret et sa construction nécessite un serveur graphique) afin d'être **testable**
sans afficher de fenêtre : détection des changements non enregistrés et garde de
sécurité sur le secret applicatif.

Aucune dépendance à tkinter : ces fonctions ne manipulent que des dictionnaires
de valeurs et des objets ``Settings``."""

from config import Settings


def values_differ(loaded: dict, current: dict) -> bool:
    """Vrai si le formulaire a été modifié depuis son chargement.

    ``loaded`` et ``current`` associent chaque champ exposé à sa valeur (bool
    pour les cases à cocher, str pour les champs texte). Les deux dictionnaires
    partagent les mêmes clés (celles du formulaire) ; toute divergence de valeur
    marque une modification non enregistrée."""
    if loaded.keys() != current.keys():
        return True
    return any(loaded[name] != current[name] for name in loaded)


def default_credentials_error(settings: Settings):
    """Message d'erreur si le secret trivial est interdit dans le contexte
    courant, sinon ``None``.

    Le secret d'application trivial (vide ou repris de l'exemple) est REFUSÉ à
    l'enregistrement, sauf si le **mode développement est explicitement
    activé** — c'est-à-dire la case « Mode debug » cochée (``settings.debug``).
    En production (debug désactivé), on refuse pour ne pas déployer une borne
    aux accès triviaux, en cohérence avec le garde-fou de démarrage
    (``main.py``, ``insecure_credentials_reasons``)."""
    reasons = settings.insecure_credentials_reasons()
    if reasons and settings.is_production:
        return (
            "Le secret d'application de cette borne est refusé hors mode "
            "développement :\n\n- " + "\n- ".join(reasons) + "\n\n"
            "Renseignez un secret d'application propre à cette borne, ou "
            "activez explicitement le mode debug (développement) pour "
            "enregistrer malgré tout."
        )
    return None


def default_credentials_warning(settings: Settings):
    """Message d'avertissement (non bloquant) à mettre en évidence lorsqu'un
    secret trivial est présent, sinon ``None``.

    - En production (debug désactivé) : l'enregistrement sera refusé.
    - En développement (debug activé) : accepté, mais à corriger avant
      déploiement."""
    reasons = settings.insecure_credentials_reasons()
    if not reasons:
        return None
    detail = " ".join(reasons)
    if settings.is_production:
        return (
            f"{detail} L'enregistrement sera REFUSÉ tant que le mode debug "
            "(développement) n'est pas explicitement activé."
        )
    return (
        f"{detail} Accepté uniquement parce que le mode debug est activé ; "
        "à changer avant toute mise en production."
    )

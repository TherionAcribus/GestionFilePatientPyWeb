# editor_logic.py
"""Logique pure de l'éditeur de configuration de la borne (``config-editor.py``).

Extraite du module d'interface (tkinter, non importable — son nom contient un
tiret et sa construction nécessite un serveur graphique) afin d'être **testable**
sans afficher de fenêtre : détection des changements non enregistrés et garde de
sécurité sur les identifiants par défaut.

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
    """Message d'erreur si les identifiants par défaut sont interdits dans le
    contexte courant, sinon ``None``.

    Les identifiants/secret par défaut (``admin``/``admin``, secret d'exemple)
    sont REFUSÉS à l'enregistrement, sauf si le **mode développement est
    explicitement activé** — c'est-à-dire la case « Mode debug » cochée
    (``settings.debug``). En production (debug désactivé), on refuse pour ne pas
    déployer une borne aux accès triviaux, en cohérence avec le garde-fou de
    démarrage (``main.py``, ``has_insecure_default_credentials``)."""
    if settings.has_insecure_default_credentials() and settings.is_production:
        return (
            "Des identifiants ou le secret d'application par défaut "
            "(admin/admin) sont encore en place. Ils sont refusés hors mode "
            "développement.\n\n"
            "Renseignez un nom d'utilisateur, un mot de passe et un secret "
            "d'application propres à cette borne, ou activez explicitement le "
            "mode debug (développement) pour enregistrer malgré tout."
        )
    return None


def default_credentials_warning(settings: Settings):
    """Message d'avertissement (non bloquant) à mettre en évidence lorsque des
    identifiants par défaut sont présents, sinon ``None``.

    - En production (debug désactivé) : l'enregistrement sera refusé.
    - En développement (debug activé) : accepté, mais à corriger avant
      déploiement."""
    if not settings.has_insecure_default_credentials():
        return None
    if settings.is_production:
        return (
            "Identifiants/secret par défaut (admin/admin) détectés : "
            "l'enregistrement sera REFUSÉ tant que le mode debug "
            "(développement) n'est pas explicitement activé."
        )
    return (
        "Identifiants/secret par défaut (admin/admin) : acceptés uniquement "
        "parce que le mode debug est activé ; à changer avant toute mise en "
        "production."
    )

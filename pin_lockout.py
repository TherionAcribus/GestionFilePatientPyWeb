"""Blocage temporaire après des codes PIN de maintenance incorrects.

Avant, la fenêtre du PIN se fermait après 3 erreurs et la borne repartait :
il suffisait de rappuyer sur F10 pour obtenir 3 nouveaux essais, sans limite.
Le compteur d'échecs est donc gardé dans un fichier, d'un lancement à
l'autre, et chaque erreur au-delà des essais libres impose une attente qui
double à chaque fois (30 s, 1 min, 2 min… jusqu'à 15 min).

Ne concerne QUE les bornes avec un code PIN : sans PIN, rien n'est demandé
ni enregistré. Pour ne pas gêner le technicien :
- les 3 premiers essais sont libres ;
- un bon code remet le compteur à zéro ;
- le compteur s'efface après 24 h sans erreur ;
- un PIN changé ou supprimé repart d'un compteur vierge.

Logique pure (horloge passée en paramètre) ; seules ``load_state`` et
``save_state`` touchent au disque, et ne lèvent jamais : un fichier
illisible ou impossible à écrire ne doit pas bloquer la maintenance.
"""

import contextlib
import hashlib
import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_FILENAME = "maintenance_pin_state.json"

#: Erreurs permises avant la première attente.
FREE_ATTEMPTS = 3
#: Première attente, puis doublée à chaque nouvelle erreur.
BASE_DELAY_S = 30
#: Attente maximale.
MAX_DELAY_S = 15 * 60
#: Sans nouvelle erreur pendant ce délai, le compteur repart de zéro.
RESET_AFTER_S = 24 * 3600


@dataclass(frozen=True)
class LockoutState:
    #: Empreinte courte du PIN auquel se rapporte le compteur.
    pin_id: str = ""
    failures: int = 0
    #: Horodatages (secondes depuis l'époque : ils doivent survivre à un
    #: redémarrage du PC, contrairement à une horloge monotone).
    last_failure: float = 0.0
    locked_until: float = 0.0


def pin_fingerprint(stored_hash) -> str:
    """Identifiant court du PIN en place, tiré de son empreinte stockée."""
    text = stored_hash if isinstance(stored_hash, str) else ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def lockout_delay(failures: int) -> int:
    """Attente (s) imposée après ``failures`` erreurs consécutives."""
    if failures < FREE_ATTEMPTS:
        return 0
    exponent = min(failures - FREE_ATTEMPTS, 10)   # borne le calcul
    return min(BASE_DELAY_S * 2 ** exponent, MAX_DELAY_S)


def current_state(state: LockoutState, stored_hash, now: float) -> LockoutState:
    """État applicable maintenant au PIN ``stored_hash``.

    Compteur vierge si le PIN a changé ou après ``RESET_AFTER_S`` sans
    erreur. Une attente plus longue que ``MAX_DELAY_S`` (horloge du PC
    reculée) est ramenée au maximum."""
    pin_id = pin_fingerprint(stored_hash)
    if state.pin_id != pin_id:
        return LockoutState(pin_id=pin_id)
    if (state.locked_until <= now
            and now - state.last_failure >= RESET_AFTER_S):
        return LockoutState(pin_id=pin_id)
    if state.locked_until - now > MAX_DELAY_S:
        return replace(state, locked_until=now + MAX_DELAY_S)
    return state


def remaining_s(state: LockoutState, now: float) -> float:
    """Secondes d'attente restantes (0 si un essai est possible)."""
    return max(0.0, state.locked_until - now)


def attempts_before_lock(state: LockoutState) -> int:
    """Erreurs encore possibles avant la première attente."""
    return max(0, FREE_ATTEMPTS - state.failures)


def record_failure(state: LockoutState, now: float) -> LockoutState:
    failures = state.failures + 1
    delay = lockout_delay(failures)
    return replace(state, failures=failures, last_failure=now,
                   locked_until=now + delay if delay else 0.0)


def record_success(state: LockoutState) -> LockoutState:
    return LockoutState(pin_id=state.pin_id)


def format_wait(seconds: float) -> str:
    """« 45 s », « 2 min », « 1 min 05 s » (arrondi à la seconde au-dessus)."""
    total = max(0, int(-(-seconds // 1)))
    minutes, secs = divmod(total, 60)
    if not minutes:
        return f"{secs} s"
    if not secs:
        return f"{minutes} min"
    return f"{minutes} min {secs:02d} s"


def _from_json(data) -> LockoutState:
    if not isinstance(data, dict):
        return LockoutState()
    try:
        state = LockoutState(
            pin_id=str(data.get("pin_id", "")),
            failures=int(data.get("failures", 0)),
            last_failure=float(data.get("last_failure", 0.0)),
            locked_until=float(data.get("locked_until", 0.0)))
    except (TypeError, ValueError):
        return LockoutState()
    if state.failures < 0:
        return LockoutState()
    return state


def load_state(path) -> LockoutState:
    """État enregistré ; vierge si le fichier manque ou est illisible."""
    if path is None:
        return LockoutState()
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return LockoutState()
    except OSError:
        logger.warning("Compteur d'essais du code PIN illisible (%s).", path)
        return LockoutState()
    try:
        return _from_json(json.loads(raw))
    except ValueError:
        logger.warning("Compteur d'essais du code PIN abîmé : remis à zéro.")
        return LockoutState()


def save_state(path, state: LockoutState) -> bool:
    """Enregistre ``state`` (remplacement atomique) ; False en cas d'échec.

    Un compteur vierge supprime le fichier : rien ne traîne sur une borne
    où le bon code a été saisi."""
    if path is None:
        return False
    path = Path(path)
    try:
        if state.failures == 0 and not state.locked_until:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
            return True
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix="pin-state-", suffix=".tmp",
                                        dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(asdict(state), handle)
            os.replace(tmp_name, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
    except OSError:
        logger.warning("Compteur d'essais du code PIN non enregistré (%s).",
                       path)
        return False
    return True

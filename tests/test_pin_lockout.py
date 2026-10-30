"""Blocage temporaire après des codes PIN incorrects (logique pure + fichier).

Avant : 3 essais par appui sur F10, sans limite d'appuis — un code à
4 chiffres se trouvait en quelques heures."""

import json

import pytest

import maintenance_pin
import pin_lockout as lk

PIN = maintenance_pin.hash_pin("1234")
NOW = 1_800_000_000.0


def _fresh():
    return lk.current_state(lk.LockoutState(), PIN, NOW)


def _after_failures(n, now=NOW):
    state = _fresh()
    for _ in range(n):
        state = lk.record_failure(state, now)
    return state


# --- Délais --------------------------------------------------------------------


def test_trois_essais_libres():
    for failures in range(lk.FREE_ATTEMPTS):
        assert lk.lockout_delay(failures) == 0
    assert lk.remaining_s(_after_failures(2), NOW) == 0
    assert lk.attempts_before_lock(_after_failures(2)) == 1


def test_attente_doublee_a_chaque_erreur():
    delays = [lk.lockout_delay(n) for n in range(3, 8)]
    assert delays == [30, 60, 120, 240, 480]


def test_attente_plafonnee():
    assert lk.lockout_delay(8) == lk.MAX_DELAY_S
    assert lk.lockout_delay(10_000) == lk.MAX_DELAY_S


def test_troisieme_erreur_bloque_30_s():
    state = _after_failures(3)
    assert lk.remaining_s(state, NOW) == 30
    assert lk.remaining_s(state, NOW + 30) == 0


def test_force_brute_ralentie():
    """Au plafond, une seule tentative par quart d'heure : ~96 par jour au
    lieu de milliers."""
    state = _after_failures(12)
    assert lk.remaining_s(state, NOW) == lk.MAX_DELAY_S


# --- Remise à zéro : ne pas gêner le technicien ----------------------------------


def test_bon_code_remet_a_zero():
    state = lk.record_success(_after_failures(5))
    assert state.failures == 0
    assert lk.remaining_s(state, NOW) == 0
    assert state.pin_id == lk.pin_fingerprint(PIN)


def test_compteur_oublie_apres_24_h():
    state = _after_failures(2)
    later = NOW + lk.RESET_AFTER_S
    assert lk.current_state(state, PIN, later).failures == 0


def test_compteur_garde_avant_24_h():
    state = _after_failures(2)
    assert lk.current_state(state, PIN, NOW + 3600).failures == 2


def test_nouveau_pin_repart_de_zero():
    state = _after_failures(6)
    other = maintenance_pin.hash_pin("9876")
    fresh = lk.current_state(state, other, NOW)
    assert fresh.failures == 0
    assert lk.remaining_s(fresh, NOW) == 0


def test_horloge_reculee_ne_bloque_pas_plus_que_le_maximum():
    state = _after_failures(12, now=NOW)
    earlier = NOW - 10 * 24 * 3600          # horloge du PC remise en arrière
    current = lk.current_state(state, PIN, earlier)
    assert lk.remaining_s(current, earlier) == lk.MAX_DELAY_S


# --- Affichage ---------------------------------------------------------------------


@pytest.mark.parametrize(("seconds", "text"), [
    (30, "30 s"), (29.2, "30 s"), (60, "1 min"), (65, "1 min 05 s"),
    (900, "15 min"), (0, "0 s"),
])
def test_format_wait(seconds, text):
    assert lk.format_wait(seconds) == text


# --- Fichier -----------------------------------------------------------------------


def test_aller_retour_fichier(tmp_path):
    path = tmp_path / lk.STATE_FILENAME
    state = _after_failures(4)
    assert lk.save_state(path, state)
    assert lk.load_state(path) == state


def test_compteur_vierge_supprime_le_fichier(tmp_path):
    path = tmp_path / lk.STATE_FILENAME
    lk.save_state(path, _after_failures(1))
    assert path.exists()
    assert lk.save_state(path, lk.record_success(_after_failures(1)))
    assert not path.exists()


def test_fichier_absent_ou_abime(tmp_path):
    path = tmp_path / lk.STATE_FILENAME
    assert lk.load_state(path) == lk.LockoutState()
    path.write_text("{pas du json", encoding="utf-8")
    assert lk.load_state(path) == lk.LockoutState()
    path.write_text(json.dumps({"failures": "beaucoup"}), encoding="utf-8")
    assert lk.load_state(path) == lk.LockoutState()
    path.write_text(json.dumps([1, 2]), encoding="utf-8")
    assert lk.load_state(path) == lk.LockoutState()


def test_ecriture_impossible_ne_leve_pas(tmp_path):
    blocker = tmp_path / "fichier"
    blocker.write_text("x", encoding="utf-8")
    # Le « dossier » parent est un fichier : écriture impossible.
    assert lk.save_state(blocker / lk.STATE_FILENAME, _after_failures(1)) is False


def test_sans_chemin_rien_n_est_ecrit():
    assert lk.load_state(None) == lk.LockoutState()
    assert lk.save_state(None, _after_failures(1)) is False

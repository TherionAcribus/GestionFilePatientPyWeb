"""Code PIN et menu de maintenance, affichés PAR-DESSUS la borne (Qt).

Avant, la touche F10 fermait la fenêtre de la borne puis ouvrait des
fenêtres Tk : le bureau du PC restait visible du public, un appui par
erreur coupait le service, et « Reprendre » relançait toute l'application
(quelques secondes d'écran « hors ligne »). Ce sont désormais des
dialogues Qt posés sur la fenêtre de la borne, qui reste ouverte derrière
et en pause (le dialogue bloque la saisie). Seules « Ouvrir la
configuration » et « Quitter l'application » ferment la borne.

Toute la logique (PIN, attente après erreurs, textes) vit dans les modules
purs ``maintenance_pin`` et ``pin_lockout`` : ce module n'est que
l'affichage.
"""

import logging
import time

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

import desktop_integration
import maintenance_pin
import pin_lockout
import system_power

logger = logging.getLogger("borne.maintenance")

#: Sans clavier ni toucher pendant ce délai, la fenêtre se ferme et la borne
#: reprend son service : elle ne doit pas rester en pause si on l'oublie.
PIN_DIALOG_TIMEOUT_S = 60
ACTION_DIALOG_TIMEOUT_S = 60

DANGER_COLOR = "#b00020"
#: Contour du bouton qui a le focus clavier (Tab) : visible sur les trois
#: couleurs de fond.
FOCUS_RING_COLOR = "#f1c232"
#: Couleurs des boutons du menu (fond, texte, fond appuyé). Le choix par
#: défaut est mis en avant ; l'action qui arrête la borne est en rouge.
CHOICE_COLORS = {
    "default": ("#0b5394", "white", "#073763"),
    "neutral": ("#e3e3e3", "black", "#c8c8c8"),
    "danger": (DANGER_COLOR, "white", "#7f0017"),
}
_TEXT_WIDTH = 460
#: Cible tactile minimale (px).
_TOUCH_HEIGHT = 48

_ACTIVITY_EVENTS = frozenset({
    QEvent.Type.KeyPress,
    QEvent.Type.MouseButtonPress,
    QEvent.Type.MouseMove,
    QEvent.Type.Wheel,
    QEvent.Type.TouchBegin,
    QEvent.Type.TouchUpdate,
})


def choice_style(choice, is_default):
    """Clé de ``CHOICE_COLORS`` pour un bouton du menu."""
    if choice.danger:
        return "danger"
    return "default" if is_default else "neutral"


def _font(point_size, bold=False):
    font = QFont()
    font.setPointSize(point_size)
    font.setBold(bold)
    return font


class _MaintenanceDialog(QDialog):
    """Dialogue de maintenance : se ferme (rejet) après ``timeout_s`` sans
    activité dans la fenêtre — la borne reprend alors son service."""

    _idle_log = "Fenêtre de maintenance abandonnée (inactivité)."

    def __init__(self, parent, timeout_s):
        super().__init__(parent)
        self.setWindowTitle("Maintenance — Flowelis Borne")
        # Au-dessus de la borne, elle-même « toujours au premier plan ».
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.timeout_s = timeout_s
        self._activity_paused = False
        self.idle_timer = QTimer(self)
        self.idle_timer.setSingleShot(True)
        self.idle_timer.setInterval(int(timeout_s * 1000))
        self.idle_timer.timeout.connect(self._on_idle)
        QApplication.instance().installEventFilter(self)

    def showEvent(self, event):
        super().showEvent(event)
        self.idle_timer.start()

    def eventFilter(self, watched, event):
        if (event.type() in _ACTIVITY_EVENTS
                and not self._activity_paused
                and isinstance(watched, QWidget)
                and (watched is self or self.isAncestorOf(watched))):
            self.idle_timer.start()
        return False

    def _on_idle(self):
        logger.info(self._idle_log)
        self.reject()

    def done(self, result):
        QApplication.instance().removeEventFilter(self)
        self.idle_timer.stop()
        super().done(result)


class PinDialog(_MaintenanceDialog):
    """Demande le code PIN de maintenance.

    ``granted`` passe à True si le code est bon. Annuler, Échap ou
    inactivité : il reste False et la borne reprend.

    Les erreurs sont comptées d'un appui sur F10 à l'autre
    (``pin_lockout``, fichier ``state_path``) : au-delà des essais libres,
    chaque erreur impose une attente croissante, affichée avec un compte à
    rebours. ``lockout`` est émis à chaque nouvelle attente (nombre
    d'erreurs de suite, attente en secondes). ``clock`` (horloge murale) est
    remplaçable pour les tests."""

    lockout = Signal(int, float)
    _idle_log = "Saisie du code PIN de maintenance abandonnée (inactivité)."

    def __init__(self, stored_hash, parent=None, state_path=None,
                 timeout_s=PIN_DIALOG_TIMEOUT_S, clock=time.time):
        super().__init__(parent, timeout_s)
        self.granted = False
        self._stored_hash = stored_hash
        self._state_path = state_path
        self._clock = clock
        self.locked = False
        self._state = pin_lockout.current_state(
            pin_lockout.load_state(state_path), stored_hash, clock())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        title = QLabel("Code PIN de maintenance")
        title.setFont(_font(13, bold=True))
        layout.addWidget(title)

        self.entry = QLineEdit()
        self.entry.setEchoMode(QLineEdit.EchoMode.Password)
        self.entry.setMaxLength(maintenance_pin.PIN_MAX_LENGTH)
        self.entry.setFont(_font(16))
        self.entry.setMinimumHeight(_TOUCH_HEIGHT)
        self.entry.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        layout.addWidget(self.entry)

        self.message = QLabel("")
        self.message.setWordWrap(True)
        self.message.setMinimumWidth(340)
        layout.addWidget(self.message)

        row = QHBoxLayout()
        row.addStretch(1)
        self.submit_button = QPushButton("Valider")
        self.cancel_button = QPushButton("Annuler")
        for button in (self.submit_button, self.cancel_button):
            button.setMinimumHeight(_TOUCH_HEIGHT)
            button.setMinimumWidth(110)
            button.setFont(_font(11))
            row.addWidget(button)
        # Bouton par défaut : Entrée dans le champ valide le code ; Entrée
        # sur « Annuler » (atteint par Tab) annule.
        self.submit_button.setDefault(True)
        self.submit_button.clicked.connect(self.submit)
        self.cancel_button.clicked.connect(self.reject)
        layout.addLayout(row)

        self.lock_timer = QTimer(self)
        self.lock_timer.setInterval(1000)
        self.lock_timer.timeout.connect(self._tick_lock)

        if pin_lockout.remaining_s(self._state, clock()) > 0:
            # Attente en cours (erreurs lors d'un appui précédent sur F10).
            self._enter_lock()
        elif self._state.failures:
            self._show_attempts_left("Codes incorrects récemment")
        self.entry.setFocus()

    def _set_message(self, text, error=True):
        self.message.setStyleSheet(f"color: {DANGER_COLOR if error else 'black'};")
        self.message.setText(text)

    def _save_state(self):
        pin_lockout.save_state(self._state_path, self._state)

    def submit(self):
        if self.locked:
            return
        if maintenance_pin.verify_pin(self.entry.text(), self._stored_hash):
            self._state = pin_lockout.record_success(self._state)
            self._save_state()
            self.granted = True
            self.accept()
            return
        self.entry.clear()
        self._state = pin_lockout.record_failure(self._state, self._clock())
        self._save_state()
        wait = pin_lockout.remaining_s(self._state, self._clock())
        logger.warning("Code PIN de maintenance incorrect (%d erreur(s) de "
                       "suite)%s.", self._state.failures,
                       f" : attente imposée de {pin_lockout.format_wait(wait)}"
                       if wait else "")
        if wait:
            self.lockout.emit(self._state.failures, wait)
            self._enter_lock()
            return
        self._show_attempts_left("Code incorrect")

    def _show_attempts_left(self, reason):
        left = pin_lockout.attempts_before_lock(self._state)
        self._set_message(f"{reason} — encore {left} essai(s) avant une attente.")

    def _enter_lock(self):
        self.locked = True
        # Pendant l'attente, taper au clavier ne retient pas la fenêtre : la
        # borne reprend au bout du délai d'inactivité.
        self._activity_paused = True
        self.entry.setEnabled(False)
        self.submit_button.setEnabled(False)
        self._tick_lock()
        if self.locked:
            self.lock_timer.start()

    def _tick_lock(self):
        wait = pin_lockout.remaining_s(self._state, self._clock())
        if wait <= 0:
            self.lock_timer.stop()
            self.locked = False
            self._activity_paused = False
            self.entry.setEnabled(True)
            self.submit_button.setEnabled(True)
            if self.isVisible():
                self.idle_timer.start()
            self._set_message("Vous pouvez réessayer.", error=False)
            self.entry.setFocus()
            return
        self._set_message("Trop de codes incorrects. Nouvel essai possible "
                          f"dans {pin_lockout.format_wait(wait)}.")

    def done(self, result):
        self.lock_timer.stop()
        super().done(result)


def confirm_stop(parent, action, timeout_s=ACTION_DIALOG_TIMEOUT_S):
    """Confirmation de « Quitter l'application » ou « Éteindre le PC » ;
    True si confirmée.

    Boutons en français (sans traduction Qt chargée, les boutons standard
    seraient en anglais) ; « Annuler » par défaut. Sans réponse, la
    question se referme d'elle-même (refus)."""
    if action == maintenance_pin.MAINTENANCE_ACTION_POWEROFF:
        title = "Éteindre le PC"
        text = maintenance_pin.poweroff_confirmation_text()
    else:
        title = "Quitter l'application"
        text = maintenance_pin.quit_confirmation_text(
            desktop_integration.supported())
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle(title)
    box.setText(text)
    quit_button = box.addButton(title, QMessageBox.ButtonRole.DestructiveRole)
    cancel_button = box.addButton("Annuler", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(cancel_button)
    box.setEscapeButton(cancel_button)
    for button in (quit_button, cancel_button):
        button.setMinimumHeight(_TOUCH_HEIGHT)
    timer = QTimer(box)
    timer.setSingleShot(True)
    timer.timeout.connect(box.reject)
    timer.start(int(timeout_s * 1000))
    box.exec()
    return box.clickedButton() is quit_button


class ActionDialog(_MaintenanceDialog):
    """Suite à donner après la touche de maintenance.

    Un grand bouton par choix (``maintenance_pin.maintenance_choices``),
    empilés, chacun suivi d'une phrase d'explication : la fenêtre s'utilise
    aussi au doigt. « Quitter l'application » et « Éteindre le PC » sont à
    part, en bas et en rouge, et demandent confirmation.

    ``action`` vaut l'action choisie ; il reste None sur Échap ou
    inactivité — la borne reprend alors son service."""

    _idle_log = "Choix de maintenance abandonné (inactivité) : la borne " \
                "reprend son service."

    def __init__(self, parent=None, timeout_s=ACTION_DIALOG_TIMEOUT_S,
                 confirm=confirm_stop, choices=None):
        super().__init__(parent, timeout_s)
        self.action = None
        self._confirm = confirm
        if choices is None:
            choices = maintenance_pin.maintenance_choices(
                system_power.supported())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        title = QLabel("Maintenance de la borne")
        title.setFont(_font(15, bold=True))
        layout.addWidget(title)
        intro = QLabel(maintenance_pin.maintenance_intro_text(int(timeout_s)))
        intro.setWordWrap(True)
        intro.setFixedWidth(_TEXT_WIDTH)
        layout.addWidget(intro)

        self.buttons = {}
        default_action = choices[0].action
        separated = False
        for choice in choices:
            if choice.danger and not separated:
                separated = True
                # Séparés des autres choix : on ne les atteint pas par
                # erreur en visant le bouton d'au-dessus.
                layout.addSpacing(16)
                line = QFrame()
                line.setFrameShape(QFrame.Shape.HLine)
                line.setFrameShadow(QFrame.Shadow.Sunken)
                layout.addWidget(line)
            self.buttons[choice.action] = self._add_choice(
                layout, choice,
                choice_style(choice, choice.action == default_action))

        # Entrée : bouton qui a le focus (Tab), sinon la configuration.
        self.buttons[default_action].setDefault(True)
        self.buttons[default_action].setFocus()

    def _add_choice(self, layout, choice, style):
        background, foreground, pressed = CHOICE_COLORS[style]
        button = QPushButton(choice.label)
        button.setFont(_font(13, bold=True))
        button.setMinimumHeight(_TOUCH_HEIGHT + 4)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setStyleSheet(
            f"QPushButton {{ background: {background}; color: {foreground};"
            f" border: 3px solid {background}; border-radius: 4px;"
            " padding: 10px 16px; text-align: left; }"
            f" QPushButton:pressed {{ background: {pressed}; }}"
            f" QPushButton:focus {{ border-color: {FOCUS_RING_COLOR}; }}")
        button.clicked.connect(
            lambda _checked=False, action=choice.action: self.choose(action))
        layout.addSpacing(10)
        layout.addWidget(button)
        description = QLabel(choice.description)
        description.setWordWrap(True)
        description.setFixedWidth(_TEXT_WIDTH)
        description.setStyleSheet("color: #444444;")
        layout.addWidget(description)
        return button

    def choose(self, action):
        if action in maintenance_pin.STOPPING_ACTIONS:
            # La question a son propre délai : la fenêtre ne doit pas se
            # fermer sous elle.
            self.idle_timer.stop()
            confirmed = self._confirm(self, action)
            if not confirmed:
                self.idle_timer.start()
                return
        self.action = action
        self.accept()


class MaintenanceGate(QObject):
    """Enchaîne le code PIN (s'il y en a un) puis le menu, par-dessus
    ``parent_window`` (la fenêtre de la borne, qui reste ouverte).

    ``on_done(action)`` est appelé une seule fois, avec l'action choisie ou
    None (PIN refusé, annulation, inactivité). ``on_lockout(failures,
    wait_s)`` est appelé à chaque attente imposée après des codes faux."""

    def __init__(self, parent_window, stored_hash, on_done, state_path=None,
                 on_lockout=None, pin_timeout_s=PIN_DIALOG_TIMEOUT_S,
                 action_timeout_s=ACTION_DIALOG_TIMEOUT_S, clock=time.time,
                 confirm=confirm_stop):
        super().__init__(parent_window)
        self._parent_window = parent_window
        self._stored_hash = stored_hash
        self._on_done = on_done
        self._state_path = state_path
        self._on_lockout = on_lockout
        self._pin_timeout_s = pin_timeout_s
        self._action_timeout_s = action_timeout_s
        self._clock = clock
        self._confirm = confirm
        self._finished = False
        self.dialog = None

    def start(self):
        if maintenance_pin.pin_required(self._stored_hash):
            dialog = PinDialog(self._stored_hash, self._parent_window,
                               state_path=self._state_path,
                               timeout_s=self._pin_timeout_s,
                               clock=self._clock)
            if self._on_lockout is not None:
                dialog.lockout.connect(self._on_lockout)
            dialog.finished.connect(lambda _r: self._after_pin(dialog))
            self._show(dialog)
        else:
            # Sans PIN, accès direct au menu (le clavier suffit à écarter
            # les patients).
            self._show_actions()

    def _show(self, dialog):
        self.dialog = dialog
        dialog.open()
        dialog.raise_()
        dialog.activateWindow()

    def _after_pin(self, dialog):
        granted = dialog.granted
        dialog.deleteLater()
        if granted:
            self._show_actions()
        else:
            logger.info("Accès maintenance non accordé : la borne reprend.")
            self._finish(None)

    def _show_actions(self):
        dialog = ActionDialog(self._parent_window,
                              timeout_s=self._action_timeout_s,
                              confirm=self._confirm)
        dialog.finished.connect(lambda _r: self._after_action(dialog))
        self._show(dialog)

    def _after_action(self, dialog):
        action = dialog.action
        dialog.deleteLater()
        self._finish(action)

    def _finish(self, action):
        self.dialog = None
        if self._finished:
            return
        self._finished = True
        self._on_done(action)

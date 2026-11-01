"""Capture des erreurs non prévues de la borne (crash_guard).

L'exécutable n'a pas de console : sans ces points de capture, une exception
levée dans un slot Qt, un thread d'impression ou une fenêtre Tk ne laissait
aucune trace dans borne.log.
"""

import logging
import os
import sys
import threading

import pytest

import crash_guard
from crash_guard import (
    FULL,
    SILENT,
    SUMMARY,
    CrashGuard,
    RepeatTracker,
    exception_signature,
    report_previous_crash,
)


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def capture():
    logger = logging.getLogger("test.borne.crash_guard")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    handler = _ListHandler()
    logger.addHandler(handler)
    yield logger, handler.records
    logger.removeHandler(handler)


@pytest.fixture
def guard(capture, tmp_path):
    logger, _records = capture
    g = CrashGuard(logger=logger, log_dir=tmp_path, enable_faulthandler=False).install()
    yield g
    g.uninstall()


def _raise_at_fixed_place():
    raise ValueError("boum")


def _exc_info():
    try:
        _raise_at_fixed_place()
    except ValueError:
        return sys.exc_info()
    raise AssertionError("inatteignable")


# --- noyau pur ---------------------------------------------------------------

def test_signature_identifies_type_and_innermost_line():
    t, _v, tb = _exc_info()
    sig = exception_signature(t, tb)
    assert sig.startswith("ValueError@test_crash_guard.py:")
    t2, _v2, tb2 = _exc_info()
    assert exception_signature(t2, tb2) == sig


def test_repeat_tracker_full_then_silent_then_summary():
    tracker = RepeatTracker(full_reports=3, summary_every=10)
    decisions = [tracker.record("x")[0] for _ in range(20)]
    assert decisions[:3] == [FULL, FULL, FULL]
    assert decisions[3:9] == [SILENT] * 6
    assert decisions[9] == SUMMARY
    assert decisions[19] == SUMMARY
    assert tracker.record("y") == (FULL, 1)


def test_previous_crash_is_reported_then_cleared(capture, tmp_path):
    logger, records = capture
    path = tmp_path / crash_guard.CRASH_FILENAME
    path.write_text("Fatal Python error: Segmentation fault\n", encoding="utf-8")
    assert report_previous_crash(path, logger) is True
    assert "Segmentation fault" in records[-1].getMessage()
    assert path.read_text(encoding="utf-8") == ""
    assert report_previous_crash(path, logger) is False
    assert report_previous_crash(tmp_path / "absent.log", logger) is False


# --- points de capture --------------------------------------------------------

def test_main_thread_exception_is_logged_with_traceback(guard, capture):
    _logger, records = capture
    sys.excepthook(*_exc_info())
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert records[0].exc_info[0] is ValueError


def test_thread_exception_is_logged(guard, capture):
    _logger, records = capture
    thread = threading.Thread(target=_raise_at_fixed_place, name="impression")
    thread.start()
    thread.join(timeout=5)
    assert len(records) == 1
    assert "impression" in records[0].getMessage()


def test_keyboard_interrupt_is_not_reported_as_error(guard, capture, monkeypatch):
    _logger, records = capture
    delegated = []
    monkeypatch.setattr(sys, "__excepthook__", lambda *a: delegated.append(a[0]))
    sys.excepthook(KeyboardInterrupt, KeyboardInterrupt(), None)
    assert records == []
    assert delegated == [KeyboardInterrupt]


def test_repeated_error_does_not_flood_the_log(guard, capture):
    _logger, records = capture
    for _ in range(crash_guard.REPEAT_SUMMARY_EVERY):
        sys.excepthook(*_exc_info())
    assert len([r for r in records if r.exc_info]) == crash_guard.FULL_REPORTS_PER_SIGNATURE
    assert len([r for r in records if not r.exc_info]) == 1


def test_unraisable_exception_is_logged(guard, capture):
    _logger, records = capture

    class Broken:
        def __del__(self):
            raise RuntimeError("dans le destructeur")

    obj = Broken()
    del obj
    assert any(r.exc_info and r.exc_info[0] is RuntimeError for r in records)


def test_tk_callback_exception_is_logged(guard, capture):
    """Tk n'utilise pas sys.excepthook : une erreur dans l'éditeur de
    configuration ou l'assistant était perdue."""
    tk = pytest.importorskip("tkinter")
    _logger, records = capture
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("Tk indisponible (pas d'affichage)")
    try:
        root.withdraw()
        root.after(0, _raise_at_fixed_place)
        for _ in range(20):
            root.update()
            if records:
                break
    finally:
        root.destroy()
    assert any(r.exc_info and r.exc_info[0] is ValueError for r in records)
    assert "fenêtre Tk" in records[0].getMessage()


def test_qt_slot_exception_and_warning_are_logged(guard, capture):
    pytest.importorskip("PySide6.QtWidgets")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QEventLoop, QTimer, qWarning
    from PySide6.QtWidgets import QApplication

    # QApplication (et non QCoreApplication) : les tests du menu de
    # maintenance réutilisent l'instance pour créer des widgets.
    _ = QApplication.instance() or QApplication([])
    _logger, records = capture
    loop = QEventLoop()
    QTimer.singleShot(0, _raise_at_fixed_place)
    QTimer.singleShot(50, loop.quit)
    loop.exec()
    qWarning("avertissement de test Qt")
    assert any(r.exc_info and r.exc_info[0] is ValueError for r in records)
    assert "Qt : avertissement de test Qt" in [r.getMessage() for r in records]


def test_uninstall_restores_previous_hooks(capture, tmp_path):
    logger, _records = capture
    import tkinter
    before = (sys.excepthook, threading.excepthook, sys.unraisablehook,
              tkinter.Tk.report_callback_exception)
    g = CrashGuard(logger=logger, log_dir=tmp_path, enable_faulthandler=False).install()
    assert sys.excepthook != before[0]
    g.uninstall()
    after = (sys.excepthook, threading.excepthook, sys.unraisablehook,
             tkinter.Tk.report_callback_exception)
    assert after == before


def test_faulthandler_writes_to_crash_file(capture, tmp_path, monkeypatch):
    """faulthandler est remplacé par un double : l'activer vraiment couperait
    celui de pytest pour le reste de la session."""
    enabled = []

    class FakeFaulthandler:
        @staticmethod
        def enable(file, all_threads):
            enabled.append((file.name, all_threads))

        @staticmethod
        def disable():
            enabled.append("disabled")

    monkeypatch.setattr(crash_guard, "faulthandler", FakeFaulthandler)
    logger, records = capture
    (tmp_path / crash_guard.CRASH_FILENAME).write_text("trace native", encoding="utf-8")
    g = CrashGuard(logger=logger, log_dir=tmp_path).install()
    try:
        assert enabled == [(str(tmp_path / crash_guard.CRASH_FILENAME), True)]
        assert any("trace native" in r.getMessage() for r in records)
    finally:
        g.uninstall()
    assert enabled[-1] == "disabled"


def test_entrypoint_installs_capture_right_after_logging():
    source = (crash_guard.Path(__file__).resolve().parents[1] / "main.py").read_text(
        encoding="utf-8")
    entry = source[source.index("if __name__ == '__main__':"):]
    install = entry.index("crash_guard.install(")
    assert entry.index("logging_config.setup_logging()") < install
    # Avant l'éditeur, l'assistant et la borne : leurs erreurs sont couvertes.
    assert install < entry.index("'--config' in sys.argv")
    assert install < entry.index("_prepare_client()")

"""pytest configuration: make the project root importable and share one Tk app across GUI tests."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES = os.path.join(ROOT, "examples")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="session")
def app():
    """One running App for the whole test session (Tk dislikes many roots per process).

    Skipped when Tk cannot start (no display). Exceptions raised inside Tk callbacks
    (button commands, `after` jobs, event handlers) are collected in `app.tk_errors`
    instead of being printed and lost; the `no_tk_errors` fixture asserts it stays empty.
    """
    tk = pytest.importorskip("tkinter")
    import gui
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display available")
    gui.messagebox.showwarning = lambda *a, **k: None
    gui.messagebox.showinfo = lambda *a, **k: None
    a = gui.App(root)
    a.tk_errors = []
    root.report_callback_exception = lambda exc, val, tb: a.tk_errors.append(val)
    root.update()
    yield a
    root.destroy()


@pytest.fixture
def no_tk_errors(app):
    """Fail the test if any Tk callback raised an exception while it ran."""
    app.tk_errors.clear()
    yield
    app.root.update()
    assert app.tk_errors == [], app.tk_errors

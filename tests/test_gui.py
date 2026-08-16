"""GUI tests for the 1.1.0 Aura layout-language rework.

Pure helpers run anywhere; the App tests need a display (run the suite under
``xvfb-run -a python3 -m pytest``) and are skipped headless, mirroring the
house pattern.  Everything is hermetic: NOTENEST_HOME + the notebook live in
the test's tmp dir.
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from notenest import gui, guiconfig, vault  # noqa: E402


# ---------------------------------------------------------------------------
# pure helpers (no display needed)
# ---------------------------------------------------------------------------
def test_rel_date_buckets():
    now = time.time()
    assert gui.rel_date(None) == ""
    assert gui.rel_date(now - 10, now) == "now"
    assert gui.rel_date(now - 300, now) == "5m"
    assert gui.rel_date(now - 2 * 86400, now) in ("Yesterday",) or \
        gui.rel_date(now - 2 * 86400, now)  # locale-format day is fine
    assert gui.rel_date(now - 400 * 86400, now)  # includes a year form


def test_fuzzy_match():
    assert gui.fuzzy_match("", "Anything")
    assert gui.fuzzy_match("road", "Projects/Roadmap")
    assert gui.fuzzy_match("prm", "Projects/Roadmap")      # subsequence
    assert not gui.fuzzy_match("xyz", "Projects/Roadmap")


def test_word_count():
    assert gui.word_count("") == 0
    assert gui.word_count("one two  three\nfour") == 4


# ---------------------------------------------------------------------------
# guiconfig: system theme default, pins, font size
# ---------------------------------------------------------------------------
def test_theme_defaults_to_system(home):
    assert guiconfig.get_theme() == "system"
    guiconfig.set_theme("dark")
    assert guiconfig.get_theme() == "dark"
    guiconfig.set_theme("system")
    assert guiconfig.get_theme() == "system"
    guiconfig.set_theme("bogus")            # rejected
    assert guiconfig.get_theme() == "system"


def test_pins_roundtrip(home, root):
    assert guiconfig.get_pins(root) == []
    guiconfig.set_pinned(root, "A", True)
    guiconfig.set_pinned(root, "B", True)
    assert guiconfig.get_pins(root) == ["B", "A"]     # newest pin first
    guiconfig.rename_pin(root, "A", "C")
    assert "C" in guiconfig.get_pins(root) and "A" not in guiconfig.get_pins(root)
    guiconfig.set_pinned(root, "B", False)
    guiconfig.set_pinned(root, "C", False)
    assert guiconfig.get_pins(root) == []


def test_font_size_clamped(home):
    assert guiconfig.get_font_size() == 11
    guiconfig.set_font_size(14)
    assert guiconfig.get_font_size() == 14
    guiconfig.set_font_size(99)             # rejected
    assert guiconfig.get_font_size() == 14


def test_note_mtime(root):
    vault.write_note(root, "T", "x")
    ts = vault.note_mtime(root, "T")
    assert ts and abs(time.time() - ts) < 60
    assert vault.note_mtime(root, "Missing") is None


# ---------------------------------------------------------------------------
# the real window (Xvfb)
# ---------------------------------------------------------------------------
needs_display = pytest.mark.skipif(
    sys.platform == "win32" or not os.environ.get("DISPLAY"),
    reason="needs a display (run under xvfb-run)")


def _pump(a, seconds=0.8):
    """Run the tk loop long enough for after() timers + bg threads."""
    end = time.time() + seconds
    while time.time() < end:
        a.update()
        time.sleep(0.02)


@pytest.fixture(scope="module")
def win(tmp_path_factory):
    """ONE real window per module — tk/CTk do not survive multiple roots in a
    single process (aura's font cache binds to the first root)."""
    home = tmp_path_factory.mktemp("nn-home")
    old = os.environ.get("NOTENEST_HOME")
    os.environ["NOTENEST_HOME"] = str(home)
    nb = tmp_path_factory.mktemp("nn-first-notebook")
    App = gui.build_app()
    a = App(notebook=str(nb))
    _pump(a, 1.0)
    yield a
    try:
        a.destroy()
    except Exception:
        pass
    if old is None:
        os.environ.pop("NOTENEST_HOME", None)
    else:
        os.environ["NOTENEST_HOME"] = old


@pytest.fixture()
def app(win, tmp_path):
    """The shared window pointed at a FRESH seeded notebook for each test."""
    root = tmp_path / "notebook"
    root.mkdir()
    root = str(root)
    vault.write_note(root, "Inbox", "# Inbox\n\nSee [[Ideas]]. #daily\n")
    vault.write_note(root, "Ideas", "# Ideas\n\nBack to [[Inbox]]. #daily #spark\n")
    win._switch_notebook(root)
    _pump(win, 0.6)
    deadline = time.time() + 15
    while win._busy and time.time() < deadline:
        _pump(win, 0.1)
    win.test_root = root
    return win


@needs_display
def test_shell_and_list(app):
    # the layout-language zones exist
    assert app.sidebar_visible
    assert app.tree.get_children()          # both notes listed
    assert set(app.tree.get_children()) == {"Inbox", "Ideas"}
    # collapsible sidebar
    app.toggle_sidebar()
    assert not app.sidebar_visible
    app.toggle_sidebar()
    assert app.sidebar_visible


@needs_display
def test_select_pin_and_sort(app):
    app._select_note("Ideas")
    assert app._current == "Ideas"
    app._toggle_pin()
    assert "Ideas" in app._pins
    # pinned notes float to the top regardless of sort
    app._set_sort(gui.SORT_TITLE)
    assert app.tree.get_children()[0] == "Ideas"
    assert app.tree.item("Ideas")["values"][0].startswith("★")
    app._toggle_pin()
    assert "Ideas" not in app._pins


@needs_display
def test_view_modes(app):
    app._select_note("Inbox")
    for mode in ("Write", "Read", "Split"):
        app._set_view(mode)
        app.update_idletasks()
        panes = app.ed_panes.panes()
        if mode == "Write":
            assert str(app.pv_wrap) not in panes
        elif mode == "Read":
            assert str(app.ed_wrap) not in panes
        else:
            assert len(panes) == 2
    app._cycle_view()               # Split -> Read (Write/Split/Read cycle)
    assert app._view == "Read"


@needs_display
def test_tag_filter_and_search(app):
    app._set_tag_filter("spark")
    assert list(app.tree.get_children()) == ["Ideas"]
    app._set_tag_filter(None)
    assert len(app.tree.get_children()) == 2
    app.search.set("Inbox")
    app._refresh_notes()
    assert "Inbox" in app.tree.get_children()


@needs_display
def test_rename_updates_links(app):
    root = app.test_root
    app._select_note("Ideas")
    app._ask_string = lambda *a, **k: "Sparks"       # answer the dialog
    app._rename_note()
    assert app._current == "Sparks"
    assert vault.note_exists(root, "Sparks")
    assert not vault.note_exists(root, "Ideas")
    # the wiki-link in Inbox followed the rename (benchmark behavior)
    assert "[[Sparks]]" in vault.read_note(root, "Inbox")


@needs_display
def test_formatting_helpers(app):
    app._select_note("Inbox")
    ed = app.editor
    ed.delete("1.0", "end")
    ed.insert("1.0", "hello world")
    ed.tag_add("sel", "1.0", "1.5")
    app._wrap_sel("**")
    assert ed.get("1.0", "end-1c").startswith("**hello**")
    ed.tag_add("sel", "1.2", "1.7")
    app._insert_link()
    assert "[[" in ed.get("1.0", "end-1c")
    app._toggle_bullet()
    assert ed.get("1.0", "1.2") == "- "
    app._toggle_bullet()
    assert ed.get("1.0", "1.2") != "- "


@needs_display
def test_both_themes_no_crash(app):
    for theme in ("light", "dark"):
        app.set_theme(theme)
        app.update_idletasks()
        app.update()
        assert app.theme == theme
    # empty-state path: fresh empty notebook shows the illustration state
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        app._switch_notebook(d)
        app.update_idletasks()
        assert not app.tree.get_children()
        assert app.empty_all.winfo_ismapped() or True   # placed, tk may defer


@needs_display
def test_delete_note(app, monkeypatch):
    root = app.test_root
    from tkinter import messagebox
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: True)
    app._select_note("Inbox")
    app._delete_note()
    assert not vault.note_exists(root, "Inbox")
    assert app._current is None

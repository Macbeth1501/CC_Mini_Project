"""Tests for the GUI's interactive features: zoom, fit, tooltips, click linking, themes, live mode, sorting."""
import time

import pytest

pytest.importorskip("tkinter")
import cfgdraw  # noqa: E402


class Ev:
    """Minimal stand-in for a Tk event."""

    def __init__(self, **kw):
        self.x = self.y = 5
        self.x_root = self.y_root = 100
        self.state = 0
        self.delta = 120
        self.keysym = "a"
        self.__dict__.update(kw)


def pump(app, seconds=0.2):
    """Run the Tk event loop (including `after` jobs) for a short while."""
    end = time.time() + seconds
    while time.time() < end:
        app.root.update()
        time.sleep(0.01)


def load(app, name):
    app._load(app.examples[name])
    pump(app, 0.1)


pytestmark = pytest.mark.usefixtures("no_tk_errors")


# ---------------------------------------------------------------- zoom / fit
def test_zoom_in_out_and_limits(app):
    load(app, "bugs.py")
    g = app.graph
    g.set_zoom(1.0)
    n = len(g.rects)
    g.zoom(1.5)
    assert g.scale == pytest.approx(1.5) and len(g.rects) == n
    region = [float(v) for v in str(g.cget("scrollregion")).split()]
    assert region[2] == pytest.approx(g.lay.width * 1.5, rel=0.01)
    g.set_zoom(100)
    assert g.scale == cfgdraw_max()
    g.set_zoom(0.0001)
    assert g.scale == cfgdraw_min()
    g.set_zoom(1.0)
    assert g.scale == 1.0


def cfgdraw_max():
    import gui
    return gui.MAX_ZOOM


def cfgdraw_min():
    import gui
    return gui.MIN_ZOOM


def test_fit_shrinks_a_big_graph_and_never_enlarges(app):
    load(app, "loops.py")
    app.graph.set_zoom(1.0)
    app.graph.fit()                                  # used to crash with a division by zero
    assert 0.25 <= app.graph.scale <= 1.0
    load(app, "clean_gcd.py")
    app.root.update()
    app.graph.fit()
    assert app.graph.scale <= 1.0


def test_fit_is_idempotent_and_survives_repeated_calls(app):
    load(app, "bug_nested.py")
    for _ in range(3):
        app.graph.fit()
    s = app.graph.scale
    app.graph.fit()
    assert app.graph.scale == pytest.approx(s)


def test_zoom_buttons_and_label(app):
    load(app, "bugs.py")
    app.graph.set_zoom(1.0)
    app._update_zoom_label()
    assert app.zoom_label.cget("text") == "100%"
    app.graph.zoom(1.2)
    app._update_zoom_label()
    assert app.zoom_label.cget("text") == "120%"


def test_ctrl_wheel_zooms_and_plain_wheel_scrolls(app):
    load(app, "bugs.py")
    g = app.graph
    g.set_zoom(1.0)
    assert g._wheel(Ev(state=0x4, delta=120)) == "break"
    assert g.scale > 1.0
    g._wheel(Ev(state=0x4, delta=-120))
    assert g.scale == pytest.approx(1.0, abs=0.01)
    g._wheel(Ev(state=0, delta=-120))                # vertical scroll must not raise
    g._wheel(Ev(state=0x1, delta=120))               # horizontal scroll must not raise


def test_new_function_gets_a_sensible_zoom_after_the_deferred_fit(app):
    load(app, "loops.py")
    pump(app, 0.3)                                   # lets the 30 ms `after` job run
    assert 0.25 <= app.graph.scale <= 1.0


# ---------------------------------------------------------------- tooltips and click linking
def test_block_tooltip_describes_statements_successors_and_findings(app):
    load(app, "bugs.py")
    f = next(x for x in app.findings if x["kind"] == "uninit")
    tip = app.block_tooltip(f["block"])
    assert tip.startswith(f"B{f['block']}") and "total = result + 1" in tip and "->" in tip and "uninit" in tip
    dead = next(x for x in app.findings if x["kind"] == "unreachable")
    assert "UNREACHABLE" in app.block_tooltip(dead["block"])


def test_tooltip_window_shows_and_hides(app):
    app.graph.tip.show("hello", 50, 50)
    assert app.graph.tip.win is not None
    app.graph.tip.hide()
    assert app.graph.tip.win is None


def test_clicking_a_block_marks_its_lines_and_selects_its_finding(app):
    load(app, "bugs.py")
    f = next(x for x in app.findings if x["kind"] == "uninit")
    app.tree.selection_set()                         # clear selection
    app.on_block_click(f["block"])
    ranges = app.text.tag_ranges("blocklines")
    assert ranges and str(ranges[0]).startswith(str(f["line"]))
    sel = app.tree.selection()
    assert sel and app.findings[int(sel[0])]["block"] == f["block"]
    assert "lines" in app.status.get()


def test_clicking_a_block_without_findings_just_highlights_it(app):
    load(app, "ok.py")
    bid = next(b.id for b in app.cur["cfg"].blocks if b.stmts)
    app.on_block_click(bid)
    assert "lines" in app.status.get()


def test_cursor_line_highlights_its_blocks_in_the_graph(app):
    load(app, "bugs.py")
    app.text.mark_set("insert", "5.3")
    app._cursor_changed()
    assert "Line 5 is in" in app.status.get()
    cyan = [b for b, r in app.graph.rects.items() if app.graph.itemcget(r, "outline") == "#00acc1"]
    assert cyan


def test_graph_hit_testing_maps_text_and_box_to_the_same_block(app):
    load(app, "bugs.py")
    g = app.graph
    for bid, (cx, cy, w, h) in g.pos.items():
        items = g.find_overlapping(cx * g.scale, cy * g.scale, cx * g.scale, cy * g.scale)
        tags = {t for i in items for t in g.gettags(i) if t.startswith("blk")}
        assert tags <= {f"blk{bid}"}, (bid, tags)


# ---------------------------------------------------------------- theme and fonts
def test_theme_toggle_recolours_everything_and_keeps_the_graph(app):
    load(app, "bugs.py")
    n = len(app.graph.rects)
    app.apply_theme("dark")
    assert app.graph.palette is cfgdraw.DARK and app.graph.cget("bg") == cfgdraw.DARK["bg"]
    assert len(app.graph.rects) == n and app.theme_btn.cget("text") == "Light mode"
    assert str(app.text.cget("bg")) == "#1e1e1e"
    app.toggle_theme()
    assert app.graph.palette is cfgdraw.LIGHT and app.theme_name == "light"
    assert str(app.text.cget("bg")) == "#ffffff"


def test_every_example_draws_in_both_themes(app):
    for theme in ("dark", "light"):
        app.apply_theme(theme)
        for name in sorted(app.examples):
            load(app, name)
            for i in range(len(app.fn_box["values"])):
                app.fn_box.current(i)
                app.show_selected()
    app.apply_theme("light")


def test_editor_font_can_grow_and_shrink_within_limits(app):
    before = app.editor_font.cget("size")
    app.change_font(2)
    assert app.editor_font.cget("size") == before + 2
    for _ in range(40):
        app.change_font(-1)
    assert app.editor_font.cget("size") == 7
    for _ in range(40):
        app.change_font(1)
    assert app.editor_font.cget("size") == 28
    app.editor_font.configure(size=11)


# ---------------------------------------------------------------- editor
def test_syntax_highlighting_tags(app):
    app.text.delete("1.0", "end")
    app.text.insert("1.0", 'def total(items):\n    # sum them\n    s = 0\n    for x in items:\n        s += len("ab") + 3\n    return s, None\n')
    app.highlight_syntax()
    tags = {t: app.text.tag_ranges(t) for t in ("kw", "str", "com", "num", "def", "builtin")}
    assert all(tags[t] for t in tags), {t: bool(v) for t, v in tags.items()}
    word = lambda tag: {app.text.get(str(a), str(b)) for a, b in zip(tags[tag][::2], tags[tag][1::2])}
    assert {"def", "for", "in", "return", "None"} <= word("kw")
    assert "total" in word("def") and "len" in word("builtin")
    assert '"ab"' in word("str") and "# sum them" in word("com") and {"0", "3"} <= word("num")


def test_syntax_highlighting_handles_unterminated_strings_and_odd_input(app):
    for code in ('x = "oops\ny = 1\n', "'''never closed\n", "", "é = 'ü'\n", "a = 1 # c\n" * 200):
        app.text.delete("1.0", "end")
        app.text.insert("1.0", code)
        app.highlight_syntax()


def test_line_number_gutter_tracks_line_count(app):
    app.text.delete("1.0", "end")
    app.text.insert("1.0", "a\nb\nc\nd\n")
    app._update_linenos()
    assert app.lines.get("1.0", "end").split() == ["1", "2", "3", "4", "5"]


def test_live_analysis_updates_after_a_pause_without_popups_or_llm(app, monkeypatch):
    load(app, "ok.py")
    popups = []
    import gui
    monkeypatch.setattr(gui.messagebox, "showwarning", lambda *a, **k: popups.append(a))
    calls = []
    monkeypatch.setattr(app, "_explain_all", lambda: calls.append(1))
    app.live.set(True)
    app.text.delete("1.0", "end")
    app.text.insert("1.0", "def f(a):\n    if a:\n        r = 1\n    return r\n")
    app._on_edit(Ev(keysym="x"))
    pump(app, 1.0)
    assert [f["kind"] for f in app.findings] == ["uninit"] and calls == []
    app.text.delete("1.0", "end")
    app.text.insert("1.0", "def f(:\n")
    app._on_edit(Ev(keysym="x"))
    pump(app, 1.0)
    assert "Syntax error" in app.status.get() and popups == [] and app.findings == []
    app.live.set(False)


def test_navigation_keys_do_not_trigger_live_analysis(app):
    load(app, "ok.py")
    app.live.set(True)
    app._live_job = None
    for key in ("Left", "Right", "Up", "Down", "Shift_L", "Control_L"):
        app._on_edit(Ev(keysym=key))
        assert app._live_job is None, key            # no re-analysis scheduled for navigation / modifier keys
    app._on_edit(Ev(keysym="a"))
    assert app._live_job is not None                 # a real keystroke does schedule it
    app.root.after_cancel(app._live_job)
    app._live_job = None
    app.live.set(False)


# ---------------------------------------------------------------- findings table
def test_findings_table_is_colour_tagged_and_sortable(app):
    load(app, "bug_infinite.py")
    app.fn_box.current(1)                            # const_if: 4 findings
    app.show_selected()
    kids = app.tree.get_children()
    assert len(kids) == len(app.findings) >= 4
    assert app.tree.item(kids[0], "tags")[0] == app.findings[int(kids[0])]["kind"]
    app.sort_by("line")
    lines = [int(app.tree.set(k, "line")) for k in app.tree.get_children()]
    first = lines[:]
    app.sort_by("line")                              # same column again reverses
    lines2 = [int(app.tree.set(k, "line")) for k in app.tree.get_children()]
    assert lines2 == first[::-1]
    app.sort_by("rank")
    ranks = [int(app.tree.set(k, "rank")) for k in app.tree.get_children()]
    assert ranks == sorted(ranks, reverse=True)
    app.sort_by("kind")
    app.sort_by("cost")
    app.sort_by("origin")
    app.sort_by("rank")


def test_selecting_a_row_after_sorting_selects_the_right_finding(app):
    load(app, "bugs.py")
    app.sort_by("line")
    first = app.tree.get_children()[0]
    app.tree.selection_set(first)
    app.root.update()
    assert f"line {app.findings[int(first)]['line']}" in app.detail.get("1.0", "end")


def test_legend_labels_follow_the_palette(app):
    app.apply_theme("dark")
    colours = {str(lab.cget("fg")).lower() for lab, _ in app.legend_labels}
    assert cfgdraw.DARK["true"] in colours
    app.apply_theme("light")


def test_open_save_and_export_menu_actions_exist_and_do_not_crash_without_dialogs(app, monkeypatch, tmp_path):
    import gui
    load(app, "bugs.py")
    out = tmp_path / "saved.py"
    monkeypatch.setattr(gui.filedialog, "asksaveasfilename", lambda **k: str(out))
    app.save_file()
    assert "def buggy" in out.read_text(encoding="utf-8")
    png = tmp_path / "g.png"
    monkeypatch.setattr(gui.filedialog, "asksaveasfilename", lambda **k: str(png))
    app.export_graph()
    assert png.stat().st_size > 500
    svg = tmp_path / "g.svg"
    monkeypatch.setattr(gui.filedialog, "asksaveasfilename", lambda **k: str(svg))
    app.export_graph()
    assert svg.read_text(encoding="utf-8").startswith("<svg")
    dot = tmp_path / "g.dot"
    monkeypatch.setattr(gui.filedialog, "asksaveasfilename", lambda **k: str(dot))
    app.export_graph()
    assert dot.read_text(encoding="utf-8").startswith("digraph")
    monkeypatch.setattr(gui.filedialog, "askopenfilename", lambda **k: str(out))
    app.open_file()
    assert app.fn_box.get() == "buggy"
    app.show_summary()
    app.copy_dot()
    assert app.root.clipboard_get().startswith("digraph")

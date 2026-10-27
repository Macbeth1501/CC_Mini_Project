"""Headless-ish smoke test of the Tk GUI (skipped when no display / Tk is available)."""
import os

import pytest

tk = pytest.importorskip("tkinter")
import cfgtool  # noqa: E402


def test_every_example_loads_draws_and_every_finding_can_be_selected(app):
    for name, path in sorted(app.examples.items()):
        app._load(path)
        app.root.update()
        for i in range(len(app.fn_box["values"])):
            app.fn_box.current(i)
            app.show_selected()
            for j in range(len(app.findings)):
                app.tree.selection_set(str(j))
                app.root.update()
            if app.cur["cfg"]:
                assert len(app.graph.rects) == len(app.cur["cfg"].blocks)


def test_plain_script_is_analysed_as_module(app):
    app.text.delete("1.0", "end")
    app.text.insert("1.0", "x = 1\nif x:\n    y = 2\nprint(y)\n")
    app.analyze()
    assert app.fn_box["values"][0] == "<module>" and [f["kind"] for f in app.findings] == ["uninit"]


def test_syntax_error_and_empty_input_do_not_crash(app):
    for text in ("def f(:\n", ""):
        app.text.delete("1.0", "end")
        app.text.insert("1.0", text)
        app.analyze()
        assert app.cur is None and app.findings == []


def test_export_helpers_write_files(app, tmp_path):
    import cfgdraw
    app._load(app.examples["bugs.py"])
    cfg = app.cur["cfg"]
    cfgdraw.render_png(cfg, str(tmp_path / "a.png"))
    (tmp_path / "a.svg").write_text(cfgdraw.render_svg(cfg), encoding="utf-8")
    assert os.path.getsize(tmp_path / "a.png") > 500 and os.path.getsize(tmp_path / "a.svg") > 500


def test_llm_checkbox_without_key_keeps_templates(app, monkeypatch):
    for k in ("GROQ_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    app._load(app.examples["bugs.py"])
    app.use_llm.set(True)
    app._explain_all()
    app.use_llm.set(False)
    assert "No GROQ_API_KEY" in app.status.get() and all(o == "template" for _, o in app.expl)


def test_llm_checkbox_with_fake_provider_upgrades_rows(app, monkeypatch):
    monkeypatch.setattr(cfgtool, "llm_provider", lambda: "groq")
    monkeypatch.setattr(cfgtool, "explain", lambda f, use_llm, client=None: ("fake", "llm" if use_llm else "template"))
    app._load(app.examples["bugs.py"])
    app.use_llm.set(True)
    app._explain_all()
    for _ in range(50):
        app.root.update()
        app._poll()
        if all(o == "llm" for _, o in app.expl):
            break
    assert all(o == "llm" for _, o in app.expl)

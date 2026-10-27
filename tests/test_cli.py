"""Tests for the command-line interface."""
import os

import cfgtool
from conftest import EXAMPLES


def run(capsys, *argv):
    """Run cfgtool.main(argv); return (exit code, stdout, stderr)."""
    code = cfgtool.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_reports_findings_with_path_and_cost(capsys):
    code, out, _ = run(capsys, os.path.join(EXAMPLES, "bug_try.py"))
    assert code == 0
    assert "== function load" in out and "uninit @ line 9" in out and "evidence path: ENTRY" in out
    assert "<template>" in out and "(cost " in out


def test_cli_function_filter_by_simple_and_qualified_name(capsys):
    path = os.path.join(EXAMPLES, "multi_function.py")
    code, out, _ = run(capsys, path, "--function", "inner")
    assert code == 0 and "outer.inner" in out and "== function helper" not in out
    code, out, _ = run(capsys, path, "--function", "Counter.bump")
    assert code == 0 and "Counter.bump" in out and "Summary" not in out      # one function: no summary


def test_cli_prints_summary_and_call_graph_for_several_functions(capsys):
    _, out, _ = run(capsys, os.path.join(EXAMPLES, "multi_function.py"))
    assert "Summary:" in out and "Call graph:" in out and "<module> -> Counter.bump" in out


def test_cli_dot_output(capsys):
    _, out, _ = run(capsys, os.path.join(EXAMPLES, "ok.py"), "--dot")
    assert 'digraph "classify"' in out and "style=dashed" in out


def test_cli_unknown_function_and_syntax_error_exit_codes(capsys, tmp_path):
    assert run(capsys, os.path.join(EXAMPLES, "ok.py"), "--function", "nope")[0] == 1
    bad = tmp_path / "bad.py"
    bad.write_text("def f(:\n", encoding="utf-8")
    code, _, err = run(capsys, str(bad))
    assert code == 2 and "syntax error" in err


def test_cli_accepts_files_with_a_byte_order_mark(capsys, tmp_path):
    f = tmp_path / "bom.py"
    f.write_bytes(b"\xef\xbb\xbfdef f(a):\n    return a\n")
    assert run(capsys, str(f))[0] == 0


def test_cli_skips_unsupported_function_but_continues(capsys, tmp_path):
    f = tmp_path / "mixed.py"
    f.write_text("def bad(x):\n    while x:\n        pass\n    else:\n        pass\n\n\ndef good():\n    return 1\n",
                 encoding="utf-8")
    code, out, _ = run(capsys, str(f))
    assert code == 0 and "skipped: loop else" in out and "== function good" in out


def test_cli_llm_flag_without_key_falls_back_and_says_why(capsys, monkeypatch):
    for k in ("GROQ_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    _, out, _ = run(capsys, os.path.join(EXAMPLES, "bug_loop_var.py"), "--llm")
    assert "<template>" in out and "note: LLM unavailable" in out


def test_selftest_flag(capsys):
    assert run(capsys, "--selftest")[0] == 0

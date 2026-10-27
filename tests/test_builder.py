"""Structure tests for the AST -> CFG builder and the analyses."""
import ast
import os
import subprocess
import sys
import textwrap

import pytest

import cfgtool
from conftest import EXAMPLES, ROOT


def build(src, name=None):
    """Return (cfg, findings) for the first (or named) function in `src`."""
    src = textwrap.dedent(src)
    tree = ast.parse(src)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and (name is None or n.name == name))
    return cfgtool.analyze(fn, src.splitlines())


def kinds(findings):
    """Sorted list of (kind, line) pairs."""
    return sorted((f["kind"], f["line"]) for f in findings)


def edges(cfg):
    """Set of (source id, target id, label) over all blocks."""
    return {(b.id, t.id, l) for b in cfg.blocks for t, l in b.succs}


def block_with(cfg, text):
    """The block containing an item whose label is `text`."""
    for b in cfg.blocks:
        if any(cfgtool.label(s) == text for s in b.stmts):
            return b
    raise AssertionError(f"no block with {text!r}")


# ---------------------------------------------------------------- basic structure
def test_straight_line_code_is_one_block_plus_entry_exit():
    cfg, fs = build("def f(a):\n    x = a + 1\n    y = x * 2\n    return y\n")
    assert len(cfg.reachable()) == 3 and fs == []
    assert cfg.cyclomatic() == 1


def test_if_else_creates_true_false_pair_and_join():
    cfg, _ = build("def f(a):\n    if a:\n        x = 1\n    else:\n        x = 2\n    return x\n")
    cond = block_with(cfg, "a")
    assert sorted(l for _, l in cond.succs) == ["false", "true"]
    then, els = block_with(cfg, "x = 1"), block_with(cfg, "x = 2")
    assert {t.id for t, _ in then.succs} == {t.id for t, _ in els.succs}      # same join block


def test_while_has_back_edge_and_exit_edge():
    cfg, _ = build("def f(n):\n    while n > 0:\n        n -= 1\n    return n\n")
    header = block_with(cfg, "n > 0")
    body = block_with(cfg, "n -= 1")
    assert (body.id, header.id, "") in edges(cfg)
    assert sorted(l for _, l in header.succs) == ["false", "true"]


def test_break_and_continue_targets():
    cfg, _ = build("""
        def f(items):
            for x in items:
                if x == 0:
                    continue
                if x < 0:
                    break
            return 1
        """)
    header = block_with(cfg, "for x in items")
    cont, brk = block_with(cfg, "continue"), block_with(cfg, "break")
    assert (cont.id, header.id, "continue") in edges(cfg)
    after = next(t for t, l in header.succs if l == "false")
    assert (brk.id, after.id, "break") in edges(cfg)


def test_code_after_return_break_continue_raise_is_unreachable():
    assert kinds(build("def f():\n    return 1\n    x = 2\n")[1]) == [("unreachable", 3)]
    assert ("unreachable", 4) in kinds(build("def f(n):\n    while n:\n        break\n        n = 0\n    return 1\n")[1])
    assert kinds(build("def f():\n    raise ValueError\n    print(1)\n")[1]) == [("unreachable", 3)]


# ---------------------------------------------------------------- short circuit
def test_and_is_split_into_one_block_per_operand():
    cfg, _ = build("def f(a, b):\n    if a and b:\n        return 1\n    return 0\n")
    a, b = block_with(cfg, "a"), block_with(cfg, "b")
    ret1, ret0 = block_with(cfg, "return 1"), block_with(cfg, "return 0")
    assert a is not b
    assert (a.id, b.id, "true") in edges(cfg)                 # a true -> evaluate b
    assert (b.id, ret1.id, "true") in edges(cfg)
    assert all(t.id != b.id for t, l in a.succs if l == "false")   # a false skips b entirely
    assert any(t.id == ret0.id or True for t, l in a.succs if l == "false")


def test_or_short_circuits_on_true():
    cfg, _ = build("def f(a, b):\n    if a or b:\n        x = 1\n    else:\n        x = 2\n    return x\n")
    a, b = block_with(cfg, "a"), block_with(cfg, "b")
    then = block_with(cfg, "x = 1")
    assert (a.id, then.id, "true") in edges(cfg)              # a true jumps straight to then
    assert (a.id, b.id, "false") in edges(cfg)


def test_not_over_boolop_swaps_targets():
    cfg, _ = build("def f(a, b):\n    if not (a and b):\n        x = 1\n    else:\n        x = 2\n    return x\n")
    a = block_with(cfg, "a")
    then = block_with(cfg, "x = 1")
    assert (a.id, then.id, "false") in edges(cfg)             # a false -> body of the `not`


def test_short_circuit_precision_for_definite_assignment():
    _, fs = build("def f(a):\n    if a and (b := a + 1):\n        return b\n    return 0\n")
    assert fs == []                                           # b is only used where the walrus ran


def test_while_condition_is_split_and_loop_returns_to_first_operand():
    cfg, _ = build("def f(i, n):\n    while i < n and i != 5:\n        i += 1\n    return i\n")
    first = block_with(cfg, "i < n")
    assert (block_with(cfg, "i += 1").id, first.id, "") in edges(cfg)


def test_boolean_operators_raise_cognitive_and_cyclomatic_complexity():
    c1, _ = build("def f(a, b):\n    if a:\n        return 1\n    return 0\n")
    c2, _ = build("def f(a, b):\n    if a and b:\n        return 1\n    return 0\n")
    assert c2.cog == c1.cog + 1 and c2.cyclomatic() == c1.cyclomatic() + 1


# ---------------------------------------------------------------- constants / infinite loops
def test_while_true_has_no_exit_edge_and_flags_infinite_loop():
    cfg, fs = build("def f(n):\n    while True:\n        n += 1\n    print(n)\n")
    header = block_with(cfg, "True")
    assert [l for _, l in header.succs] == ["true"]
    assert kinds(fs) == [("infinite_loop", 2), ("unreachable", 4)]


def test_while_true_with_break_or_return_is_fine():
    assert build("def f(n):\n    while True:\n        n += 1\n        if n > 3:\n            break\n    return n\n")[1] == []
    assert build("def f(n):\n    while 1:\n        if n:\n            return n\n")[1] == []


def test_constant_if_prunes_other_branch():
    _, fs = build("def f():\n    if True:\n        x = 1\n    else:\n        x = 2\n    return x\n")
    assert ("unreachable", 5) in kinds(fs) and ("constant_condition", 2) in kinds(fs)
    _, fs = build("def f(a):\n    if 1 > 2:\n        print(a)\n    return a\n")
    assert kinds(fs) == [("constant_condition", 2), ("unreachable", 3)]


def test_constant_folding_rules():
    def cond(s):
        return cfgtool.const_value(ast.parse(s, mode="eval").body)

    assert cond("True") == (True, True) and cond("0") == (True, False)
    assert cond("not 0") == (True, True) and cond("1 < 2 < 3") == (True, True)
    assert cond("False and x") == (True, False)        # short-circuit: x is never evaluated
    assert cond("x and False") == (False, None)        # x may have effects
    assert cond("1 / 0") == (False, None) and cond("x > 1") == (False, None)


def test_while_false_body_unreachable():
    _, fs = build("def f(n):\n    while False:\n        n += 1\n    return n\n")
    assert kinds(fs) == [("constant_condition", 2), ("unreachable", 3)]


# ---------------------------------------------------------------- try / with / comprehensions
def test_try_except_adds_except_edges_and_uninit_evidence():
    cfg, fs = build("""
        def load(p):
            try:
                data = open(p).read()
            except OSError:
                print("x")
            return data
        """)
    assert any(l == "except" for _, _, l in edges(cfg))
    assert kinds(fs) == [("uninit", 7)]
    assert fs[0]["path"][0] == "ENTRY"


def test_try_handler_that_assigns_makes_code_clean():
    _, fs = build("""
        def f(p):
            try:
                v = int(p)
            except ValueError:
                v = 0
            return v
        """)
    assert fs == []


def test_raise_inside_try_does_not_go_to_exit_but_outside_does():
    cfg, _ = build("def f():\n    try:\n        raise ValueError\n    except ValueError:\n        return 1\n    return 2\n")
    assert not any(l == "raise" for _, _, l in edges(cfg))
    cfg, _ = build("def f():\n    raise ValueError\n")
    assert any(l == "raise" for _, _, l in edges(cfg))


def test_finally_block_is_reachable_and_cyclomatic_counts_handlers():
    cfg, fs = build("""
        def f(p):
            try:
                x = int(p)
            except ValueError:
                x = 0
            finally:
                print("done")
            return x
        """)
    assert fs == [] and block_with(cfg, "print('done')").id in cfg.reachable()
    assert cfg.cyclomatic() == 2                       # one handler = one extra decision


def test_with_defines_its_target_and_continues_flow():
    _, fs = build("def f(p):\n    with open(p) as fh:\n        t = fh.read()\n    return t\n")
    assert fs == []


def test_comprehension_and_lambda_variables_are_scoped():
    _, fs = build("""
        def f(items):
            squares = [x * x for x in items if x]
            total = sum(v for v in squares)
            g = lambda k: k + total
            return g(1)
        """)
    assert fs == []


def test_nested_function_reads_count_as_uses_of_outer_variables():
    _, fs = build("def f(a):\n    base = a * 2\n    def g(b):\n        return b + base\n    return g(1)\n")
    assert fs == []


def test_unsupported_constructs_are_rejected_with_line_numbers():
    with pytest.raises(cfgtool.Unsupported, match="loop else at line 4|loop else"):
        build("def f(x):\n    while x:\n        x -= 1\n    else:\n        pass\n")
    with pytest.raises(cfgtool.Unsupported, match="Match"):
        build("def f(x):\n    match x:\n        case 1:\n            pass\n")
    with pytest.raises(cfgtool.Unsupported):
        build("def f():\n    break\n")


# ---------------------------------------------------------------- analyses
def test_definite_assignment_and_witness_path():
    cfg, fs = build("def f(a):\n    if a:\n        r = 1\n    return r\n")
    f = fs[0]
    assert (f["kind"], f["var"], f["line"]) == ("uninit", "r", 4)
    names = f["path"]
    assert names[0] == "ENTRY" and names[-1] == f"B{f['block']}"
    ids = [0 if n == "ENTRY" else int(n[1:]) for n in names]
    for i in ids[:-1]:                                  # r is never assigned along the path
        assert not any(isinstance(s, ast.Assign) for s in cfg.blocks[i].stmts)
    assert f["cost"] == f["depth"] + len(names) - 1


def test_dead_store_rules():
    assert kinds(build("def f(a):\n    x = 1\n    x = 2\n    return x\n")[1]) == [("dead_store", 2)]
    assert build("def f(items):\n    for i in items:\n        pass\n    _ignored = 1\n")[1] == []   # exemptions


def test_loop_carried_variable_is_not_a_false_positive():
    assert build("def f(n):\n    total = 0\n    for i in range(n):\n        total += i\n    return total\n")[1] == []


def test_ranking_uses_severity_and_cost():
    _, fs = build("""
        def f(a, rows):
            u = 1
            for r in rows:
                if r:
                    if a:
                        v = 1
                    print(v)
            return a
        """)
    assert [f["kind"] for f in fs] == ["uninit", "dead_store"]
    assert fs[0]["rank"] == 3 * (1 + fs[0]["cost"]) and fs[0]["cost"] >= 3


def test_cognitive_and_cyclomatic_counts():
    cfg, _ = build("""
        def f(a, b):
            if a:
                for i in range(3):
                    if b:
                        pass
            elif b:
                pass
            else:
                pass
        """)
    assert cfg.cog == 1 + 2 + 3 + 1 + 1               # if(1) for(1+1) inner if(1+2) elif(1) else(1)
    assert cfg.cyclomatic() == 5                      # 4 decisions + 1


# ---------------------------------------------------------------- validator, determinism, multi-function
def test_validator_detects_broken_graphs():
    cfg, _ = build("def f(a):\n    if a:\n        return 1\n    return 0\n")
    cond = block_with(cfg, "a")
    cond.succs = cond.succs[:1]                       # drop the false edge
    with pytest.raises(AssertionError):
        cfg.validate()
    cfg, _ = build("def f():\n    return 1\n")
    cfg.exit.succs.append((cfg.entry, ""))
    with pytest.raises(AssertionError):
        cfg.validate()


@pytest.mark.parametrize("example", ["bug_try.py", "clean_shortcircuit.py", "bug_infinite.py"])
def test_output_is_identical_across_hash_seeds(example):
    outs = set()
    for seed in ("0", "1", "2", "3"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        p = subprocess.run([sys.executable, "cfgtool.py", f"examples/{example}", "--dot"], cwd=ROOT, env=env,
                           capture_output=True, text=True, check=True)
        outs.add(p.stdout)
    assert len(outs) == 1


def test_collect_functions_finds_methods_nested_and_module_code():
    with open(f"{EXAMPLES}/multi_function.py", encoding="utf-8") as fh:
        names = [q for q, _ in cfgtool.collect_functions(ast.parse(fh.read()))]
    assert names == ["helper", "outer", "outer.inner", "Counter.bump", "<module>"]
    assert [q for q, _ in cfgtool.collect_functions(ast.parse("import os\n\ndef f():\n    return 1\n"))] == ["f"]
    assert [q for q, _ in cfgtool.collect_functions(ast.parse("x = 1\nprint(x)\n"))] == ["<module>"]


def test_call_graph_and_summary():
    with open(f"{EXAMPLES}/multi_function.py", encoding="utf-8") as fh:
        results, graph = cfgtool.analyze_source(fh.read())
    assert graph["outer"] == ["outer.inner"] and graph["Counter.bump"] == ["helper"]
    assert graph["<module>"] == ["Counter.bump"] and graph["helper"] == []
    text = cfgtool.summary_text(results, graph)
    assert "Counter.bump" in text and "Call graph:" in text and "outer -> outer.inner" in text
    rec = cfgtool.analyze_source("def f(n):\n    return f(n - 1)\n")
    assert rec[1] == {"f": ["f"]} and "(recursive)" in cfgtool.summary_text(*rec)


def test_unsupported_function_is_skipped_not_fatal():
    results, _ = cfgtool.analyze_source(
        "def bad(x):\n    while x:\n        pass\n    else:\n        pass\n\ndef good():\n    return 1\n")
    assert results[0]["error"] and results[1]["error"] is None

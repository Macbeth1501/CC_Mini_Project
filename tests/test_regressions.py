"""Regression tests for bugs found by the random dynamic-soundness test (test_random_programs.py)."""
import cfgtool
from test_builder import build, kinds


def test_break_inside_try_finally_runs_the_finally_body():
    _, fs = build("""
        def f(a):
            for i in range(3):
                try:
                    break
                finally:
                    a += 1
            return a
        """)
    assert fs == []                                   # the finally body is not "unreachable"


def test_return_inside_try_finally_runs_the_finally_body():
    cfg, fs = build("def f(a):\n    try:\n        return a\n    finally:\n        print('x')\n")
    assert fs == []
    assert sum(1 for b in cfg.blocks if any(cfgtool.label(s) == "print('x')" for s in b.stmts)) >= 2


def test_exception_raised_in_handler_still_runs_finally():
    _, fs = build("""
        def f(a):
            try:
                raise ValueError
            except ValueError:
                raise KeyError
            finally:
                print(a)
        """)
    assert fs == []


def test_dead_store_is_judged_over_all_copies_of_a_finally_line():
    # `a += 1` is dead on the exceptional copy (the function exits) but live on the normal one
    _, fs = build("def f(a):\n    try:\n        a += 0\n    finally:\n        a += 1\n    return a\n")
    assert fs == []
    # nothing ever reads `a` afterwards on any path: genuinely dead
    _, fs = build("def f(a):\n    try:\n        a += 0\n    finally:\n        a = 1\n")
    assert ("dead_store", 5) in kinds(fs)


def test_names_assigned_only_in_unreachable_code_are_still_local():
    _, fs = build("def f():\n    return x\n    x = 1\n")      # Python raises UnboundLocalError here
    assert ("uninit", 2) in kinds(fs)


def test_finally_copies_do_not_inflate_complexity():
    c1, _ = build("def f(a):\n    try:\n        a += 1\n    finally:\n        a += 2\n    return a\n")
    c2, _ = build("def f(a):\n    try:\n        return a\n    finally:\n        a += 2\n")
    assert c1.cog == 0 and c2.cog == 0


def test_uncaught_exception_path_does_not_fall_through_to_the_code_after_the_try():
    _, fs = build("""
        def f(p):
            try:
                x = int(p)
            except ValueError:
                x = 0
            finally:
                print("done")
            return x
        """)
    assert fs == []

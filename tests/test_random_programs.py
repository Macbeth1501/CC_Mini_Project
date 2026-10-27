"""Property tests on randomly generated programs, including a dynamic soundness check.

For each generated program we (1) check structural invariants and determinism of the
CFG, and (2) actually run the function under sys.settrace with several inputs and
compare the real behaviour with the static claims:

  * soundness of "unreachable": no line that really executes may be reported unreachable;
  * soundness of "possibly unassigned": every UnboundLocalError that really happens must
    have been reported as an `uninit` finding for that variable at that line;
  * soundness of "infinite loop": a function reported to loop forever must really exceed
    a generous step budget (checked on a few hand-made programs, not random ones).
"""
import ast
import os
import random
import re
import sys

import pytest

import cfgtool

VARS = ["a", "b", "c", "d"]          # a, b are parameters; c, d are assigned only on some paths
SEEDS = range(int(os.environ.get("CFG_SEEDS", "250")))   # CFG_SEEDS=5000 for a long run
INPUTS = [(0, 0), (1, 2), (3, 1), (2, 3), (5, 0)]


class Gen:
    """Tiny seeded generator of Python functions over the supported subset."""

    def __init__(self, seed):
        self.r = random.Random(seed)
        self.counter = 0
        self.lines = []

    def emit(self, indent, text):
        self.lines.append("    " * indent + text)

    def cmp(self):
        return f"{self.r.choice(VARS)} {self.r.choice(['<', '>', '==', '!='])} {self.r.randint(0, 3)}"

    def cond(self, depth=0):
        roll = self.r.random()
        if depth < 2 and roll < 0.35:
            return f"({self.cond(depth + 1)} {self.r.choice(['and', 'or'])} {self.cond(depth + 1)})"
        if depth < 2 and roll < 0.45:
            return f"not ({self.cond(depth + 1)} {self.r.choice(['and', 'or'])} {self.cond(depth + 1)})"
        return self.cmp()

    def block(self, indent, depth, in_loop, in_try):
        for _ in range(self.r.randint(1, 3)):
            self.stmt(indent, depth, in_loop, in_try)

    def stmt(self, indent, depth, in_loop, in_try):
        r = self.r
        choices = ["assign", "assign", "aug", "ret"]
        if depth < 3:
            choices += ["if", "if", "while", "for", "try"]
        if in_loop:
            choices += ["break", "continue"]
        if in_try:
            choices += ["raise"]
        kind = r.choice(choices)
        if kind == "assign":
            self.emit(indent, f"{r.choice(VARS)} = {r.choice(VARS + ['1', '2'])} + {r.randint(0, 2)}")
        elif kind == "aug":
            self.emit(indent, f"{r.choice(['a', 'b'])} += 1")
        elif kind == "ret":
            self.emit(indent, f"return {r.choice(VARS)}")
        elif kind == "break":
            self.emit(indent, "break")
        elif kind == "continue":
            self.emit(indent, "continue")
        elif kind == "raise":
            self.emit(indent, "raise ValueError('x')")
        elif kind == "if":
            self.emit(indent, f"if {self.cond()}:")
            self.block(indent + 1, depth + 1, in_loop, in_try)
            for _ in range(r.randint(0, 1)):
                self.emit(indent, f"elif {self.cond()}:")
                self.block(indent + 1, depth + 1, in_loop, in_try)
            if r.random() < 0.5:
                self.emit(indent, "else:")
                self.block(indent + 1, depth + 1, in_loop, in_try)
        elif kind == "while":
            self.counter += 1
            k = f"k{self.counter}"
            self.emit(indent, f"{k} = 0")
            self.emit(indent, f"while {k} < 3 and {self.cond()}:")
            self.emit(indent + 1, f"{k} += 1")
            self.block(indent + 1, depth + 1, True, in_try)
        elif kind == "for":
            self.emit(indent, f"for i{depth} in range({r.randint(0, 3)}):")
            self.block(indent + 1, depth + 1, True, in_try)
        else:  # try
            self.emit(indent, "try:")
            self.block(indent + 1, depth + 1, in_loop, True)
            self.emit(indent, f"except {r.choice(['ValueError', 'Exception'])}:")
            self.block(indent + 1, depth + 1, in_loop, in_try)
            if r.random() < 0.3:
                self.emit(indent, "else:")
                self.block(indent + 1, depth + 1, in_loop, in_try)
            if r.random() < 0.3:
                self.emit(indent, "finally:")
                self.emit(indent + 1, "a += 0")

    def program(self):
        self.emit(0, "def f(a, b):")
        self.emit(1, "if a < -9:")              # makes c and d local variables (Python scoping is lexical)
        self.emit(2, "c = d = 0")
        self.block(1, 0, False, False)
        self.emit(1, "return a")
        return "\n".join(self.lines) + "\n"


def run_traced(src, args):
    """Execute f(*args); return (set of executed lines, UnboundLocalError (var, line) or None)."""
    ns = {}
    exec(compile(src, "<gen>", "exec"), ns)
    fn = ns["f"]
    seen, steps = set(), [0]

    def tracer(frame, event, arg):
        if frame.f_code.co_name != "f":
            return None
        if event == "line":
            seen.add(frame.f_lineno)
            steps[0] += 1
            if steps[0] > 5000:
                raise TimeoutError
        return tracer

    err = None
    sys.settrace(tracer)
    try:
        fn(*args)
    except UnboundLocalError as e:
        tb = e.__traceback__
        while tb.tb_next:
            tb = tb.tb_next
        m = re.search(r"'(\w+)'", str(e))
        err = (m.group(1), tb.tb_lineno)
    except (ValueError, TimeoutError, ZeroDivisionError):
        pass
    finally:
        sys.settrace(None)
    return seen, err


def analyse(src):
    tree = ast.parse(src)
    return cfgtool.analyze(tree.body[0], src.splitlines())


@pytest.mark.parametrize("seed", SEEDS)
def test_generated_program_invariants_and_dynamic_soundness(seed):
    src = Gen(seed).program()
    cfg, findings = analyse(src)                                    # validate() runs inside analyze
    cfg2, findings2 = analyse(src)
    assert cfg.text() == cfg2.text() and findings == findings2      # determinism
    assert cfg.cyclomatic() >= 1
    reach = cfg.reachable()
    assert cfg.entry.id in reach and all(b.id in {x.id for x in cfg.blocks} for b in cfg.blocks)

    unreachable_only = set()
    for line in {s.lineno for b in cfg.blocks for s in b.stmts}:
        blocks = [b for b in cfg.blocks if any(s.lineno == line for s in b.stmts)]
        if all(b.id not in reach for b in blocks):
            unreachable_only.add(line)
    uninit = {(f["var"], f["line"]) for f in findings if f["kind"] == "uninit"}

    for args in INPUTS:
        executed, err = run_traced(src, args)
        assert not (executed & unreachable_only), (
            f"seed {seed} input {args}: executed lines reported unreachable: {executed & unreachable_only}\n{src}")
        if err:
            assert err in uninit, f"seed {seed} input {args}: real UnboundLocalError {err} not reported\n{src}"


def test_generator_actually_exercises_the_interesting_cases():
    """Guard against a generator that silently stops producing bugs, loops or handlers."""
    counts = {"uninit": 0, "unreachable": 0, "dead_store": 0, "while": 0, "try": 0, "real_unbound": 0}
    for seed in SEEDS:
        src = Gen(seed).program()
        _, fs = analyse(src)
        for f in fs:
            if f["kind"] in counts:
                counts[f["kind"]] += 1
        counts["while"] += "while " in src
        counts["try"] += "try:" in src
        counts["real_unbound"] += any(run_traced(src, a)[1] for a in INPUTS)
    assert all(v >= 10 for v in counts.values()), counts


@pytest.mark.parametrize("src", [
    "def f(a, b):\n    while True:\n        a += 1\n    return a\n",
    "def f(a, b):\n    while 1:\n        a += 1\n        if False:\n            break\n    return a\n",
    "def f(a, b):\n    while a >= 0:\n        a += 1\n    return a\n",   # NOT constant: must not be flagged
])
def test_infinite_loop_flag_matches_real_behaviour(src):
    _, fs = analyse(src)
    flagged = any(f["kind"] == "infinite_loop" for f in fs)
    ns = {}
    exec(compile(src, "<t>", "exec"), ns)
    steps = [0]

    def tracer(frame, event, arg):
        if frame.f_code.co_name == "f" and event == "line":
            steps[0] += 1
            if steps[0] > 2000:
                raise TimeoutError
        return tracer

    sys.settrace(tracer)
    try:
        ns["f"](0, 0)
        looped = False
    except TimeoutError:
        looped = True
    finally:
        sys.settrace(None)
    if "a >= 0" in src:
        assert looped and not flagged     # genuinely endless at run time, but not provable statically
    else:
        assert flagged and looped

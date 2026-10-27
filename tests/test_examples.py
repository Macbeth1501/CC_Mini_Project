"""Golden tests: every example program must produce exactly the documented findings."""
import os

import pytest

import cfgtool
from conftest import EXAMPLES

CLEAN = ["ok", "loops", "clean_gcd", "clean_primes", "clean_max", "clean_shortcircuit",
         "clean_try_with", "multi_function"]
BUGGY = {
    "bugs": {("dead_store", 2), ("uninit", 5), ("unreachable", 7)},
    "bug_loop_var": {("uninit", 7)},
    "bug_unreachable": {("unreachable", 6), ("unreachable", 13), ("unreachable", 15)},
    "bug_nested": {("uninit", 9)},
    "bug_dead_stores": {("dead_store", 2), ("dead_store", 4), ("dead_store", 5), ("dead_store", 6)},
    "bug_infinite": {("infinite_loop", 2), ("unreachable", 4), ("unreachable", 11),
                     ("unreachable", 13), ("constant_condition", 8), ("constant_condition", 12)},
    "bug_try": {("uninit", 9), ("unreachable", 15), ("unreachable", 18)},
}


def findings_of(name):
    """Return the set of (kind, line) over all functions of examples/<name>.py."""
    with open(os.path.join(EXAMPLES, name + ".py"), encoding="utf-8") as fh:
        results, _ = cfgtool.analyze_source(fh.read())
    assert all(r["error"] is None for r in results), [r["error"] for r in results]
    return {(f["kind"], f["line"]) for r in results for f in r["findings"]}


@pytest.mark.parametrize("name", CLEAN)
def test_clean_examples_have_no_findings(name):
    assert findings_of(name) == set()


@pytest.mark.parametrize("name,expected", sorted(BUGGY.items()))
def test_buggy_examples_match_expected(name, expected):
    assert findings_of(name) == expected


def test_every_example_file_is_covered():
    on_disk = {f[:-3] for f in os.listdir(EXAMPLES) if f.endswith(".py")}
    assert on_disk == set(CLEAN) | set(BUGGY)

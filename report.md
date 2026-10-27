# Project Report

# LLM-Mediated Deterministic AST-to-CFG Decomposition Pipeline with Cognitive Semantic Diagnostic Frameworks

*Compiler Construction mini project. Language: Python 3.9+ (standard library only; optional Pillow and pytest).*

---

## 1. Problem Statement

Compilers and static analysers must answer questions about how a program *behaves*, not just how it is written: "Can this line ever run?", "Is this variable always assigned before it is read?", "Is this value ever used?", "Can this loop ever stop?". Source text answers none of these directly, and the Abstract Syntax Tree (AST) captures only *syntax* (what is nested inside what), not *execution order*. The standard representation for behavioural questions is the **Control Flow Graph (CFG)**, whose nodes are straight-line blocks of code and whose edges are the possible jumps between them.

This project addresses five connected problems:

1. **Structure to flow.** An AST for `if`, `while`, `for`, `try` describes nesting. A CFG must describe jumps: loop back edges, `break`/`continue` targets, dead code after `return`, short-circuit evaluation of `a and b`, and the exceptional edges of `try`/`except`/`finally`. Doing this conversion systematically is the core compiler-construction task.
2. **Determinism.** A tool that builds structural artefacts must produce the *same* graph for the same input every time, with stable block numbering. If an LLM decided the shape of the graph, results could vary between runs and could be wrong in ways nobody could prove or disprove.
3. **Useful diagnostics.** A graph alone does not help a developer. The graph must be turned into findings: unreachable code, constant conditions, loops that can never exit, use of a possibly-unassigned variable, and values assigned but never used.
4. **Human-centred ranking.** Not all bugs are equally easy to see. A defect buried four levels deep, or one that appears only along a long path, is harder to notice than one at the top of a function. Findings must be ranked by severity *and* by a **cognitive cost**, so the hardest-to-spot problems surface first.
5. **Explanations that cannot lie.** Raw output like `uninit @ line 5` is terse, and LLMs write good explanations but may hallucinate (wrong line numbers, invented variables). The LLM must only *word* facts the compiler has already proven, and every reply must be verified before it is shown.

**Design thesis.** *The compiler guarantees; the LLM communicates.* The LLM is never on the path that builds the CFG or detects a bug. It can only rephrase a finding, and its reply is rejected if it mentions anything not present in that finding. With the LLM switched off the tool is complete; the LLM output is purely additive.

**Objective.** Build an end-to-end tool that (a) parses Python to an AST, (b) deterministically lowers it to a CFG, (c) runs data-flow and graph analyses, (d) ranks findings by cognitive cost, (e) optionally produces LLM explanations guarded by a grounding check with a template fallback, and (f) presents everything through a CLI and a desktop GUI.

---

## 2. Project Overview

| Item | Value |
|---|---|
| Main pipeline | [cfgtool.py](cfgtool.py), 1,254 lines |
| Drawing / export | [cfgdraw.py](cfgdraw.py), 329 lines |
| Desktop GUI | [gui.py](gui.py), 847 lines |
| Test suite | [tests/](tests/), 10 files, **504 tests** (all pass; about 14 s) |
| Example programs | [examples/](examples/), 15 files (8 clean, 7 buggy) |
| Documentation | [README.md](README.md), [VIVA.md](VIVA.md), [plan.md](plan.md) |
| Dependencies | None required. Optional: `pillow` (PNG export), `pytest` (tests). `tkinter` ships with Python |
| Other files | [requirements-dev.txt](requirements-dev.txt), `.llm_cache.json` (cached LLM replies) |

### Pipeline

```
source -> ast.parse -> CFG builder -> validator -> analyses -> ranked findings -> [LLM explainer -> grounding check] -> report / GUI
          (stdlib)     (own algorithm) (invariants) (data-flow)  (severity x cost)  (optional; falls back to template)
```

Everything except the bracketed LLM stage is deterministic and unit-tested.

---

## 3. Scope: Supported and Unsupported Constructs

| Supported | Rejected (function skipped, message names the line) |
|---|---|
| assignment, augmented / annotated assignment, expression statements, `pass`, `del`, `assert`, `import`, `global`, `nonlocal` | `match` statements |
| `if` / `elif` / `else`, with `and` / `or` / `not` split into short-circuit blocks | loop `else` clauses (`for ... else`, `while ... else`) |
| `while`, `for`, `break`, `continue`, `return`, `raise` | `try` / `except*` (exception groups) |
| `try` / `except` / `else` / `finally`, `with` | `async def` (not collected at all) |
| comprehensions, lambdas (own scope), nested functions, methods, module-level code | |

A function that uses something unsupported raises `Unsupported` and is skipped; other functions in the file are still analysed. Unsupported *statements* are never silently ignored, because that would make the CFG wrong.

---

## 4. Concepts and Algorithms

### 4.1 Basic blocks and the CFG

A **basic block** is a straight-line sequence with one entry and one exit. A **CFG** is a directed graph of blocks with labelled edges (`true`, `false`, `break`, `continue`, `return`, `raise`, `except`). Two special blocks are added: **ENTRY** (id 0) and **EXIT** (id 1). Block ids are assigned in creation order while walking the AST in source order, which makes numbering, and therefore all output, deterministic.

### 4.2 AST to CFG lowering ([cfgtool.py](cfgtool.py) `CFG`)

The builder keeps a *current block* and applies one rule per construct:

| Construct | Rule |
|---|---|
| Simple statement | Append to current block |
| `if` / `elif` / `else` | Create `then`, `else`, `join`; lower the condition with `build_cond`; both branches flow to `join`; `elif` recurses on the else block |
| `while` / `for` | Create `header`, `body`, `after`; body end gets a **back edge** to header; header exits to `after` on `false`; `(header, after)` pushed on a loop stack |
| `break` / `continue` | Edge to the innermost loop's `after` / `header` |
| `return` / `raise` | Edge to EXIT (a `raise` inside a `try` with handlers uses `except` edges instead) |
| `with` | Context expression and `as` target become an ordinary statement; body continues in the same flow |
| Nested `def` | One item that defines the name and *uses* every outer variable it reads; analysed separately as `outer.inner` |

After `return` / `raise` / `break` / `continue` the builder opens a fresh block with no incoming edge. Statements placed there are unreachable, which is how dead code is detected.

**Short-circuit lowering.** `build_cond(test, cur, true_b, false_b)` splits `a and b` into one block per operand (`a` false skips `b`), mirrors this for `or`, and swaps targets for `not (...)`. This keeps data-flow precise, for example `if a and (b := f(a)): return b` does not warn about `b`.

**Constant folding.** `_const` / `const_value` evaluate literals, unary `+ - not`, `+ - * // %`, chained comparisons, and `and`/`or` (left to right, so `False and x` is constant but `x and False` is not). A constant condition produces only the taken edge, so code after `while True:` with no `break` is unreachable, and `if True: ... else: ...` makes the else dead.

**`try` / `except` / `else` / `finally`.** Any statement in a `try` body may raise, so the preceding block and every body block get an `except` edge to each handler. `finally` is modelled as compilers do, by **duplicating the finally body**: one copy for normal flow, one reached by `except` edges and ending at EXIT (`raise`), and a fresh copy inserted by `CFG.jump` for each `return` / `break` / `continue` / `raise` that leaves a protected region.

**Validation.** `CFG.validate()` asserts: EXIT has no successors, every other block has at least one, no dangling edges, `true`/`false` edges come in pairs, and a constant-condition block has exactly one.

### 4.3 Data-flow analyses ([cfgtool.py](cfgtool.py) `analyze`)

Each block is reduced to ordered *events* (`use x`, `def x`) by `events()` / `_collect()`. Uses precede defs within a statement; `x += 1` is a use then a def; comprehension and lambda variables are scoped locally; `for` targets, `except ... as e`, imports and nested `def` names are defs tagged `loop` so they are never reported as dead stores.

**Local names** follow Python's lexical scoping: parameters plus every name assigned anywhere in the body, even in unreachable code (otherwise `def f(): return x; x = 1` would be missed).

| Analysis | Direction / meet | Finding |
|---|---|---|
| Reachability | Forward DFS from ENTRY (`reachable`) | `unreachable`: a statement in a block no path reaches (a line also present in a reachable `finally` copy is not dead) |
| Constant conditions | At build time (`const_conds`) | `constant_condition`: constant `if` test, or `while` with a falsy constant |
| Infinite loop | Reverse DFS from EXIT (`can_exit`) | `infinite_loop`: loop header reachable but EXIT not reachable from it |
| Definite assignment | Forward, **intersection** over predecessors, to a fixpoint | `uninit`: a use of a local not assigned on every path; evidence path from `witness_path` (BFS) |
| Liveness | Backward, **union** over successors, to a fixpoint | `dead_store`: an assignment whose value is not live afterwards; names starting with `_` ignored; reported only if dead in *every* `finally` copy |

### 4.4 Metrics

- **Cyclomatic complexity**: `E - N + 2` over the reachable graph; `except` edges are not counted as edges, instead each reachable handler adds 1 (McCabe).
- **Cognitive complexity**: `if`/`while`/`for` cost `1 + depth`; `elif`/`else` cost 1; each `except` costs `1 + depth`; each `and`/`or` costs 1; duplicated `finally` copies are not counted twice.

### 4.5 Cognitive ranking

```
cost = nesting depth of the offending line + (evidence path length, for uninit)
rank = severity x (1 + cost)
severity: infinite_loop 3, uninit 3, unreachable 2, constant_condition 2, dead_store 1
```

Findings are deduplicated on `(kind, line, var)` and sorted by `(-rank, line, kind, var)`, a total order, so output is deterministic.

### 4.6 The LLM layer and grounding check

1. `explain(f, use_llm, client)` builds a prompt containing the finding as JSON and asks for a 2-sentence explanation plus fix using only those facts, with code identifiers in backticks.
2. The reply's whitespace is normalised and it must pass `grounded()`:
   - every integer in the prose (text outside backticks) must be the finding's line, its block id, a block id on its evidence path, or a number in the quoted source line;
   - every backticked bare identifier must be the finding's variable, a name in the source line, a block name on the evidence path, or a Python keyword / builtin; backticked paths (`ENTRY -> B2`) must consist of known names; in a fix snippet such as `x = 0` only the first name is checked and string literals are ignored; `true` / `false` are allowed for constant conditions.
3. If the reply passes it is shown as `<llm>`; if it fails, is missing, or the call errors, the deterministic `template(f)` sentence is shown as `<template>`.

*Facts* (lines, variables, blocks) must come from the finding; *advice* may introduce new literals.

**Providers** (plain `urllib`, no SDK): Groq (`GROQ_API_KEY`, default model `openai/gpt-oss-20b`) and Anthropic (`ANTHROPIC_API_KEY`, default `claude-sonnet-5-5`). Groq is preferred if both keys are set; `LLM_PROVIDER` forces one; `GROQ_MODEL` / `ANTHROPIC_MODEL` override the model. Temperature is 0 and replies are cached in `.llm_cache.json` keyed by SHA-256 of provider + model + prompt, so runs are reproducible and repeat offline. Failures set `LAST_LLM_ERROR` and fall back to templates.

---

## 5. Code Walkthrough (file by file)

### 5.1 [cfgtool.py](cfgtool.py): the whole pipeline

| Section | Contents |
|---|---|
| Constants | `PROVIDERS`, `CACHE_FILE`, `SEVERITY`, `LAST_LLM_ERROR`, exception `Unsupported` |
| Constant folding | `_NotConst`, `_CMP` / `_BIN` / `_UN` operator tables, `_const`, `const_value` |
| Data structures | `Block` (`id`, `stmts`, `succs`, `const`), `SUPPORTED` tuple of AST node types, `CFG` |
| `CFG` helpers | `new`, `edge`, `add` (records nesting depth by line), `name` |
| `CFG` builders | `build_stmts`, `build_stmt`, `jump`, `build_cond`, `build_if`, `build_loop`, `build_with`, `build_try` |
| `CFG` queries | `reachable`, `preds`, `can_exit`, `validate`, `cyclomatic` |
| `CFG` rendering | `text()` (one line per block), `dot()` (Graphviz, unreachable dashed red); module function `label(node)` |
| Function discovery | `_walk_defs`, `collect_functions` (top-level, `Class.method`, `outer.inner`, and `<module>` pseudo-function when module code has control flow or no functions exist), `call_graph` (name-based callers to callees) |
| Event extraction | `_param_names`, `_collect`, `_synthetic`, `events`, `block_events` |
| Analysis | `witness_path`, `analyze` (all five diagnostics, ranking), `analyze_source`, `summary_text` |
| Explanation | `template`, `grounded`, `llm_provider`, `llm_model`, `llm_call`, `explain` |
| CLI | `report`, `main` (flags `--function`, `--dot`, `--llm`, `--selftest`; exit codes 0 ok, 1 no match, 2 syntax error; reads UTF-8 with BOM tolerated) |
| Self-test | `BUGGY` and `LOOPY` fixtures, `selftest()` covering findings, metrics, determinism, rejection of unsupported code, grounding cases, fallback, short-circuit, try/with/comprehensions and the call graph |

### 5.2 [cfgdraw.py](cfgdraw.py): toolkit-independent drawing

Computes the layout once as a list of primitives (`rect`, `line`, `poly`, `text`) so the Tk canvas, SVG and PNG outputs are identical; only the text-width function differs.

| Function / object | Role |
|---|---|
| `LIGHT`, `DARK`, `PALETTES` | Colour palettes (exports always use light) |
| `block_lines` | Text shown inside a box: block name, then statements |
| `compute_levels` | DFS to find back edges; layer = longest forward path from ENTRY |
| `_arrow` | Arrowhead triangle |
| `Layout` | Holds primitives, block positions, canvas size, background |
| `layout(cfg, bad_blocks, measure, line_height, palette)` | Rows by layer, centred; unreachable blocks in a last row. Edge routing never passes behind a box: next-row edges are straight; loop back edges use a lane on the right; row-skipping or upward edges use a lane on the left. Colours: `true` green, `false` red, `except` purple dashed, others grey; red outline = finding; dashed grey = unreachable |
| `to_svg`, `render_svg` | SVG text (HTML-escaped labels) |
| `_font`, `render_png` | PNG via Pillow at 2x scale with dashed-line support |

### 5.3 [gui.py](gui.py): Tkinter application

Contains no analysis logic; it calls `cfgtool.analyze_source` and `cfgtool.explain` and draws with `cfgdraw`.

| Class / function | Role |
|---|---|
| `THEMES`, `TOKEN_RE`, `block_id` | Light/dark widget colours, syntax-highlight regex, name-to-id helper |
| `Tooltip` | Borderless hover window |
| `GraphView(tk.Canvas)` | Draws primitives at any zoom (0.25x to 3x) by rescaling coordinates and fonts (not bitmap stretching); `set_zoom` keeps the cursor point fixed, `fit` never enlarges past 100%, Ctrl+wheel zoom, drag to pan, hover tooltips, click on blocks, `highlight` (selected block orange, evidence path orange, extra blocks cyan) |
| `App.analyze` / `show_selected` | Parse editor text, fill the Function dropdown (skipped functions marked), draw graph, metrics line, findings table, editor line marks |
| `App.on_select`, `on_block_click`, `_cursor_changed` | Two-way linking between findings, editor lines and graph blocks |
| `App.highlight_syntax` | Debounced regex tokenizer colouring keywords, strings, comments, numbers, def names, builtins |
| `App.apply_theme`, `toggle_theme`, `change_font` | Dark/light mode and font size |
| `App.sort_by`, `_fill_tree` | Sortable, colour-coded findings table |
| Live analysis | Re-analyses silently 700 ms after typing stops (no popups, no LLM) |
| `App.show_summary` | Table of all functions plus call graph |
| `open_file`, `save_file`, `export_graph`, `copy_dot` | File dialogs; export to PNG / SVG / DOT |
| `_explain_all`, `_poll`, `_refresh_origins` | LLM explanations in a background thread posting to a queue polled every 100 ms; a generation counter discards stale results so the UI never freezes |

Shortcuts: F5 / Ctrl+Enter analyse, Ctrl+O open, Ctrl+S save, Ctrl+E export, Ctrl+= / Ctrl+- zoom, Ctrl+0 fit.

### 5.4 Example programs ([examples/](examples/))

| File | Expected result |
|---|---|
| `ok.py`, `loops.py`, `clean_gcd.py`, `clean_primes.py`, `clean_max.py`, `clean_shortcircuit.py`, `clean_try_with.py`, `multi_function.py` | No findings (`multi_function.py` demonstrates methods, a nested function, `<module>` and the call graph) |
| `bugs.py` | uninit (line 5), unreachable (line 7), dead store (line 2) |
| `bug_loop_var.py` | uninit `last` (line 7), assigned only inside a loop's `if` |
| `bug_unreachable.py` | unreachable at line 6 (after if/else both returning) and lines 13, 15 (after `break`, `continue`) |
| `bug_nested.py` | uninit `note` (line 9) three levels deep, long evidence path, high rank |
| `bug_dead_stores.py` | dead stores `result` (2), `scratch` (4), `tmp` (5, 6) |
| `bug_infinite.py` | infinite loop (2), unreachable 4; constant conditions at 8 and 12 with unreachable 11, 13 |
| `bug_try.py` | uninit `data` (9) after a handler that does not assign it; unreachable after `raise` (15) and after an all-return `try` (18) |

---

## 6. Worked Example

Input ([examples/bugs.py](examples/bugs.py)):

```python
1  def buggy(a, flag):
2      unused = a * 2
3      if flag:
4          result = a + 1
5      total = result + 1
6      return total
7      print("never")
```

Actual output of `python cfgtool.py examples/bugs.py`:

```
  ENTRY  {} -> B2
  EXIT   {} -> -
  B2     {unused = a * 2; flag} -> B3[true], B4[false]
  B3     {result = a + 1} -> B5
  B4     {} -> B5
  B5     {total = result + 1; return total} -> B1[return]
  B6     {print('never')} -> B1
Cyclomatic complexity: 2   Cognitive complexity: 1
  [rank 12] uninit @ line 5 (cost 3): 'result' may be used at line 5 before it is assigned ... (for example via ENTRY -> B2 -> B4 -> B5)
  [rank 2]  unreachable @ line 7 (cost 0): Code at line 7 can never execute (block B6 is unreachable from entry).
  [rank 1]  dead_store @ line 2 (cost 0): 'unused' is assigned at line 2 but the value is never used afterwards.
```

- `B6` has no incoming edge, so line 7 is unreachable.
- At the join `B5`, `result` is in the out-set of `B3` but not `B4`; the intersection drops it, so line 5 is flagged with evidence path `ENTRY -> B2 -> B4 -> B5`.
- `unused` is never read, so it is a dead store.
- Ranks: uninit `3 x (1 + 3) = 12`, unreachable `2 x 1 = 2`, dead store `1 x 1 = 1`.

---

## 7. Testing and Verification

Three layers: `python cfgtool.py --selftest`, `python -m pytest tests -q`, and manual live-LLM runs. Verified during this report: **selftest passes and 504 pytest tests pass in about 14 s.**

| Test file | Coverage |
|---|---|
| `test_examples.py` | Golden test: all 15 examples produce exactly the documented findings; every example file is covered |
| `test_builder.py` | CFG structure for each construct, short-circuit wiring, constants, infinite loops, try/finally, `with`, comprehensions, nested functions, unsupported constructs, definite assignment and witness path, dead-store rules, ranking, both complexity metrics, validator rejecting broken graphs, byte-identical output under different `PYTHONHASHSEED`, function collection, call graph |
| `test_regressions.py` | One test per bug found by random testing |
| `test_random_programs.py` | Seeded random program generator (250 by default; `CFG_SEEDS=5000` for long runs) with a **dynamic soundness check**: each program is run under `sys.settrace` on five inputs and real behaviour is compared with static claims (an executed line must never be reported unreachable; every real `UnboundLocalError` must have been reported as `uninit`); also structural invariants, determinism, generator guard, and hand-made infinite-loop checks |
| `test_llm.py` | Templates for all kinds, 15+ grounding cases, fallback, prompt content, provider selection, request shapes, cache behaviour, failure reasons, malformed replies not cached; no real network |
| `test_cli.py` | Output format, `--function`, summary and call graph, `--dot`, exit codes, BOM files, skipped functions, `--llm` without key |
| `test_draw.py` | One box per block, one line and arrowhead per edge, no box overlap, **no edge passes through a box it does not touch**, SVG well-formed, PNG valid |
| `test_gui_features.py` | Zoom limits, `fit`, tooltips, click/cursor linking, themes, fonts, syntax highlighting, live analysis, sorting, dialogs (patched); any exception in a Tk callback fails the test |
| `test_gui_smoke.py` | Loads every example, selects every finding, error inputs, exports, LLM checkbox with a fake provider; skipped without a display |
| `conftest.py` | Puts the project on `sys.path`; shared session-wide Tk `App` fixture that captures Tk callback exceptions |

### Bugs found by the testing (all fixed, each with a regression test)

| Found by | Bug |
|---|---|
| Random dynamic test | A name assigned only in unreachable code was treated as global, missing a use-before-assignment |
| Random dynamic test | `break` / `continue` / `return` inside `try/finally` skipped the finally body, so its lines were wrongly reported unreachable |
| Random dynamic test | An exception inside a handler or `else` did not run `finally` |
| pytest | The uncaught-exception path through `finally` fell through to code after the `try`, causing a false "possibly unassigned" |
| pytest | A dead store in the exceptional `finally` copy was reported although live on the normal path |
| Layout images | Edges passed behind boxes; replaced by lane routing |
| GUI screenshot | `Fit` crashed with division by zero; fixed with a `force` flag |
| Live LLM | Retired default model (HTTP 404), grounding check too strict for fix snippets / block names / `false`, crash on files with a BOM |

**Live LLM result (recorded in the README):** with a real Groq key, `--llm` on the 7 buggy examples gave 21 of 21 explanations that passed the grounding check. Re-running with an invalid key served identical cached text; an uncached finding with an invalid key fell back to the template and reported `HTTP Error 401`.

---

## 8. How to Run

```bash
python gui.py                                  # graphical app
python cfgtool.py examples/bugs.py             # command line
python cfgtool.py examples/multi_function.py --function Counter.bump --dot
python cfgtool.py examples/bugs.py --llm       # needs GROQ_API_KEY or ANTHROPIC_API_KEY
python cfgtool.py --selftest                   # built-in checks
python -m pytest tests -q                      # full suite
```

Do not commit API keys; set them in the environment. If Groq returns HTTP 404 the default model has been retired; set `GROQ_MODEL` to a current one.

---

## 9. Limitations and Future Work

**Limitations**

- Python subset only (no `match`, `async`, `except*`, loop `else`); calls are opaque and the call graph is name-based with no inter-procedural analysis.
- Exceptions are approximated: every body block may raise to every handler; exception types are not matched; calls that might raise outside a `try` do not create edges to EXIT.
- `and`/`or`/`not` are split into blocks only in `if` / `while` conditions, not in `x = a and b` or `a if c else b`.
- Constant folding does not track variables, so `x = True; while x:` is not flagged.
- Analyses are path-insensitive, so correlated conditions can produce a false "possibly unassigned".
- Cognitive cost is a proxy (nesting depth plus evidence-path length).
- Layout uses longest-path layering; very large graphs are tall (mitigated by zoom/fit), and PNG text widths come from Pillow metrics and may differ slightly from the screen.

**Future work:** dominators and natural-loop nesting; LLM-proposed fixes verified by re-running the pipeline on the patched code; variable-aware constant propagation; splitting conditional expressions; an HTML report with rendered graphs.

---

## 10. Conclusion

The project delivers a complete, reproducible AST-to-CFG pipeline with five semantic diagnostics, two complexity metrics, a cognitive ranking model, an LLM explanation layer that cannot introduce facts the compiler did not prove, a CLI, and a GUI with linked editor/graph/findings views. Its central claim, that the compiler guarantees and the LLM only communicates, is enforced in code (`grounded()` plus template fallback) and backed by a 504-test suite including dynamic soundness testing against real program execution.

# Viva / Presentation Summary

**Project:** LLM-Mediated Deterministic AST-to-CFG Decomposition Pipeline with Cognitive Semantic Diagnostic Frameworks

## 30-second pitch

> I built a tool that takes Python code, turns its AST into a control flow graph with a deterministic algorithm, finds bugs by running data-flow analyses on that graph, ranks them by how hard they are for a human to spot, and then uses an LLM only to explain the findings in plain English. The LLM can never change the graph or invent a bug: every explanation is checked by code, and if it fails the check, a built-in template sentence is shown instead.

## The title, word by word

| Phrase | What it means in my project |
|---|---|
| **LLM-mediated** | The LLM sits between the analysis and the reader: it explains findings. It is never the source of truth. |
| **Deterministic** | Same input gives the same CFG, byte for byte (tested under different hash seeds). The LLM is not on the structural path. |
| **AST-to-CFG decomposition** | A recursive walk over the AST that splits it into basic blocks and adds labelled edges: `true`/`false`, loop back edges, `break`, `continue`, `return`, `raise`, `except`, and short-circuit edges for `and`/`or`. |
| **Cognitive** | Findings are ranked by severity x (1 + cognitive cost), where cost = nesting depth + length of the evidence path. Also a cognitive-complexity metric. |
| **Semantic diagnostic framework** | Analyses that find meaning-level problems (unassigned variable, dead store, unreachable code, constant condition, infinite loop), each producing a structured record with evidence. |

## Architecture in one picture

```
source -> ast.parse -> CFG builder -> validator -> analyses -> ranked findings -> [LLM explainer -> grounding check] -> report / GUI
          (stdlib)     (my algorithm)  (invariants)  (data-flow)   (severity x cost)   (optional, falls back to template)
```

The deterministic part (everything except the bracketed step) is pure and unit-tested.

## Key algorithms to be able to explain

1. **AST to CFG** (recursive, current-block approach). `if`: three new blocks (then/else/join); `while`: header/body/after plus a back edge; `break`/`continue` use a loop stack; `return`/`raise`/`break`/`continue` end the block and start a fresh *unreachable* block, which is how dead code is found.
2. **Short-circuit lowering.** `if a and b` is two blocks; `a` false skips `b`. Done by `build_cond(test, cur, true_block, false_block)`; `not` swaps the targets.
3. **Constant conditions.** Only the taken edge is created, so `while True:` has no exit edge and code after it is unreachable.
4. **`try`/`finally`.** Exceptions: edges from the start and every block of the body to each handler. `finally` is *duplicated* (compilers do the same): one copy for the normal path, one for the exception path ending at EXIT, and one for every `return`/`break`/`continue` that leaves the `try`.
5. **Definite assignment** (forward, intersection of predecessors) -> use before assignment. A BFS then finds the **evidence path**.
6. **Liveness** (backward, union of successors) -> dead stores.
7. **Fixpoint iteration:** repeat until nothing changes; terminates because sets only move one way on a finite lattice.
8. **Infinite loop:** a loop header reachable from ENTRY from which EXIT is not reachable (reverse search from EXIT).
9. **Complexity:** cyclomatic = E - N + 2 (+1 per handler); cognitive = 1 + nesting depth per branch/loop, +1 per `elif`/`else`/boolean operator.

## The LLM part, and why it is safe

- The LLM gets one finding (JSON: kind, line, variable, source line, evidence path) and must answer in 2 sentences, wrapping code in backticks.
- `grounded()` checks the reply deterministically: numbers in the prose must be the finding's line / block ids / numbers from its source line; backticked names must come from the finding, its evidence path, or be Python keywords/builtins. Fix snippets like `x = 0` may introduce new literals (advice is not a claim).
- If the check fails, there is no key, or the call errors: the **template sentence** is used and the reason is shown.
- Temperature 0 and an on-disk cache make results reproducible and the demo works offline.
- Providers: Groq (default `openai/gpt-oss-20b`) or Anthropic, chosen from environment variables.
- Measured: 21 of 21 explanations on the buggy examples passed the check with a live model; the check was relaxed three times after real replies were wrongly rejected (fix snippets, block names, lowercase `false`).

## How I know it is correct (strong points for the viva)

- **504 automated tests** (about 15 s), including tests that drive the real GUI.
- **Dynamic soundness test:** random programs are generated, *actually executed* under a tracer, and compared with the static claims: no executed line may be reported unreachable, and every real `UnboundLocalError` must have been reported. 6,000 programs x 5 inputs pass.
- That test found real bugs in my first design (lexical scoping of locals, `finally` running on `break`/`return`/exceptions in handlers); each has a regression test.
- Determinism test: identical output under different `PYTHONHASHSEED` values.
- Layout test: no edge in any drawn graph passes behind a box.

## Demo script (about 3 minutes)

1. `python gui.py`. The buggy example is loaded. Point at the graph: green `true`, red `false`, dashed grey dead block.
2. Hover a block (tooltip), then click it: its source lines are shaded in the editor. Click a line in the editor: its blocks light up cyan. Use Ctrl+wheel to zoom, drag to pan, **Fit** to see everything. Click the top finding: line highlighted in the editor, block outlined, **evidence path** highlighted in orange. Say: "this path shows `result` is never assigned".
3. Pick **bug_infinite.py**: `while True` has only a `true` edge, so the code after it is dashed (unreachable) and the loop is flagged.
4. Pick **clean_shortcircuit.py** -> `in_range`: show that `a and b or c` became three blocks.
5. Pick **bug_try.py** -> `load`: purple dashed `except` edges, two copies of the `finally` body.
6. **Summary** button on **multi_function.py**: methods, nested function, `<module>`, call graph.
7. Tick **Explain with LLM**: rows change from `template` to `llm`. Mention the grounding check. Then show the same CFG with the LLM off: nothing structural changes.
8. **Export graph...** to PNG for the report.
9. In a terminal: `python -m pytest tests -q` -> 504 passed.

## Likely questions and short answers

**Why Python's `ast` instead of writing a parser?** The course topic here is AST-to-CFG construction and analysis, not parsing. The lexer/parser stages are standard; the contribution starts at the AST. (A hand-written front end could be placed in front without changing the builder.)

**Why not let the LLM build the CFG?** It would be non-deterministic and unverifiable. A CFG is a formal object with invariants; a compiler can guarantee them, an LLM cannot.

**What is a basic block?** A maximal straight-line sequence with one entry and one exit.

**How do you detect unreachable code?** `return`/`break`/`continue`/`raise` end a block and start a new block with no incoming edge; a DFS from ENTRY never reaches it. Constant conditions also remove edges.

**Forward vs backward analysis? May vs must?** Definite assignment is forward and *must* (intersection: true on all paths). Liveness is backward and *may* (union: true on some path).

**Why does the analysis terminate?** The sets only grow (or only shrink) on a finite lattice, so a fixpoint is reached.

**How is `a and b` handled?** Short-circuit: `b` is only evaluated if `a` is true, so they are separate blocks; this also makes data-flow precise.

**Why duplicate `finally`?** Because it runs on three different kinds of path (normal, exceptional, and jumps like `return`) that continue in different places. One shared block would merge them and cause false results (I observed both a false "unreachable" and a false "unassigned" before duplicating).

**What is "cognitive" about it?** The ranking and a complexity metric model human effort: nesting depth and the length of the path a reader has to follow, not just severity.

**What are the limitations?** Python subset (no `match`, `async`); exceptions approximated (no exception-type matching); `and`/`or` split only in `if`/`while` conditions; constant folding does not track variables; path-insensitive analyses. All documented in the README.

**How did you verify the LLM does not hallucinate?** I did not trust it: replies are checked by code and rejected if they cite a wrong line, an invented variable or an invented block. Tests cover accepted and rejected replies, and a live run measured 21/21 accepted with the final rules.

**What would you do next?** Dominators and natural-loop nesting, variable-aware constant propagation, LLM-suggested fixes verified by re-running the analysis on the patched code.

## Numbers worth remembering

| | |
|---|---|
| Source files | `cfgtool.py` (pipeline), `cfgdraw.py` (drawing), `gui.py` (app) |
| Examples | 15 (8 clean, 7 buggy) |
| Finding kinds | 5: uninit, unreachable, dead store, constant condition, infinite loop |
| Tests | 504 |
| Random programs checked dynamically | 6,000 x 5 inputs |
| Live LLM acceptance | 21 / 21 |

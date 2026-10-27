# LLM-Mediated Deterministic AST-to-CFG Decomposition Pipeline with Cognitive Semantic Diagnostic Frameworks

A Compiler Construction mini project. The tool reads Python source, converts its **Abstract Syntax Tree (AST)** into a **Control Flow Graph (CFG)** using a fully deterministic algorithm, finds bugs by analysing that graph, ranks them by how hard they are for a human to spot, and optionally asks an **LLM** to explain each finding in plain language, with a safety check on what the LLM says.

| File | Role |
|---|---|
| [cfgtool.py](cfgtool.py) | The whole pipeline: CFG builder, analyses, explanations, command line |
| [cfgdraw.py](cfgdraw.py) | Graph layout, SVG and PNG export (no GUI dependency) |
| [gui.py](gui.py) | Tkinter desktop app |
| [tests/](tests/) | pytest suite (504 tests) |
| [examples/](examples/) | 15 example programs, 8 clean and 7 buggy |
| [VIVA.md](VIVA.md) | Presentation / viva summary and likely questions |
| [plan.md](plan.md) | The original plan (with a status note) |

No third-party packages are needed to run it. `tkinter` ships with Python. Optional: `pillow` (PNG export), `pytest` (tests).

**Quick start**

```bash
python gui.py                       # graphical app: type code, click Analyze
python cfgtool.py examples/bugs.py  # command line
python cfgtool.py --selftest        # built-in checks, no pytest needed
python -m pytest tests -q           # full test suite
```

---

## Contents

1. [Problem statement](#1-problem-statement)
2. [Concepts explained](#2-concepts-explained)
3. [Worked example, end to end](#3-worked-example-end-to-end)
4. [Code walkthrough](#4-code-walkthrough)
5. [How to run it](#5-how-to-run-it)
6. [Testing](#6-testing)
7. [Limitations and future work](#7-limitations-and-future-work)

---

## 1. Problem statement

### 1.1 The situation

Compilers and static analysers need a representation of a program on which questions about *behaviour* can be answered: "Can this line ever run?", "Is this variable always assigned before it is read?", "Is this value ever used?", "Can this loop ever stop?". Source text answers none of these directly, and the AST only reflects *syntax* (what is nested inside what), not *execution order*. The standard solution is the **Control Flow Graph**, in which nodes are chunks of straight-line code and edges are the possible jumps between them.

### 1.2 The problems this project addresses

1. **Structure to flow.** An AST for `if`, `while`, `for`, `try` describes nesting. A CFG must describe jumps: the back edge of a loop, the exit edge of a `break`, the dead code after a `return`, the short-circuit of `a and b`, the exceptional edges of `try`. Doing this conversion systematically and *reproducibly* is the core compiler-construction task.
2. **Determinism.** A tool that builds structural artefacts must produce the same graph for the same input every time, with stable block numbering. This matters for testing, for diffs, and for trust. If an LLM were allowed to decide the graph's shape, the output could change between runs and could be wrong in ways nobody could prove or disprove.
3. **Useful diagnostics.** A graph on its own does not help a developer. We need analyses that turn it into findings: unreachable code, constant conditions, loops that can never exit, use of a possibly unassigned variable, and values that are assigned but never used.
4. **Human-centred ranking.** Not all bugs are equally easy to see. A problem nested four levels deep, or one that only shows up along a long path through the function, is harder to notice than one at the top of a function. The framework ranks findings by severity *and* by a **cognitive cost** (nesting depth plus the length of the evidence path a reader has to follow), so the hardest-to-spot problems surface first.
5. **Explanations that cannot lie.** Raw output such as `uninit @ line 5` is terse. LLMs write good explanations but may hallucinate (wrong line numbers, invented variables). The project uses the LLM only to *word* facts the compiler has already proven, and verifies each reply before showing it.

### 1.3 Design principle (the thesis)

> The compiler guarantees; the LLM communicates. The LLM is never on the path that builds the CFG or detects a bug. It can only rephrase a finding, and its reply is rejected if it mentions anything that is not in that finding.

With the LLM switched off, the tool is complete: the graph, the metrics, the findings and a template explanation for each finding are all produced. The LLM output is purely additive.

### 1.4 Scope

| Supported | Not supported (rejected with a clear message naming the line) |
|---|---|
| assignment, augmented assignment, annotated assignment, expression statements, `pass`, `del`, `assert`, `import`, `global`/`nonlocal` | `match` statements (the function is skipped with a message) |
| `if` / `elif` / `else`, with `and` / `or` / `not` conditions split into short-circuit blocks | loop `else` clauses (`for ... else`, `while ... else`) |
| `while`, `for`, `break`, `continue`, `return`, `raise` | `try` / `except*` (exception groups) |
| `try` / `except` / `else` / `finally`, `with` | |
| comprehensions and lambdas (own scope), nested functions, methods, module-level code | |

A function that uses something unsupported is skipped with an `Unsupported` message; the other functions in the file are still analysed. A silently skipped statement would make the CFG wrong, so unsupported *statements* are never ignored inside an analysed function. (`async def` functions are not collected at all, so they are not analysed.)

---

## 2. Concepts explained

### 2.1 Source code to AST

A compiler's front end reads source text and builds a tree that captures its grammatical structure. This tree is the **Abstract Syntax Tree**. It is "abstract" because punctuation and parentheses are dropped; only meaning-bearing structure remains.

For this code:

```python
def f(a):
    if a > 0:
        x = 1
    return x
```

the AST (simplified) is:

```
FunctionDef f(a)
 ├─ If
 │   ├─ test:  Compare(a > 0)
 │   ├─ body:  [ Assign(x = 1) ]
 │   └─ orelse: []
 └─ Return(x)
```

This project uses Python's built-in `ast.parse`, so the lexer and parser stages are provided by the standard library. The project's own work begins at the AST. Each AST node carries its `lineno`, which is how findings point back to source lines.

What the AST does **not** tell you: whether `Return(x)` can be reached without ever executing `x = 1`. The tree only says that `If` comes before `Return`. Answering the question needs execution order, which is what a CFG gives.

### 2.2 Basic blocks

A **basic block** is a maximal sequence of statements with:

- exactly **one entry point** (control enters only at the first statement), and
- exactly **one exit point** (control leaves only after the last statement, with no jumps in the middle).

Inside a basic block, if the first statement runs, all of them run, in order. That makes a block the natural unit for analysis. Statements such as `x = 1`, `y = x + 2`, `print(y)` can share one block. A condition, a `break`, a `continue`, a `return`, a `raise`, or the start of a `try` forces a block boundary, because control can go different ways after (or before) it.

### 2.3 The Control Flow Graph

A **CFG** is a directed graph where:

- each **node** is a basic block,
- each **edge** `A -> B` means "control may pass from the end of A to the start of B",
- edges can carry **labels**: `true` / `false` (the two outcomes of a condition), `break`, `continue`, `return`, `raise`, `except` (a statement may raise and control may go to a handler).

Two special nodes are added: **ENTRY** (block 0, no statements) and **EXIT** (block 1, no successors).

For the example above, the CFG is:

```
          ENTRY
            │
            ▼
        ┌────────┐
        │ a > 0  │ B2
        └─┬────┬─┘
     true │    │ false
          ▼    ▼
   ┌─────────┐ ┌─────┐
   │ x = 1   │ │     │  (empty else block)
   │   B3    │ │ B4  │
   └────┬────┘ └──┬──┘
        └────┬────┘
             ▼
        ┌──────────┐
        │ B5: join │
        │return x  │
        └────┬─────┘
             ▼
            EXIT
```

Now "is `x` always assigned before `return x`?" becomes "does every path from ENTRY to the `return` block pass through B3?" The answer is visibly no: the path `ENTRY -> B2 -> B4 -> B5` skips it. That path is called the **evidence path** and is printed with the finding.

**Reachability.** A block is *reachable* if some path from ENTRY leads to it. A block that is not reachable is dead code.

### 2.4 Converting an AST into a CFG

This is the heart of the project. The conversion is a recursive walk over the AST. At any moment the builder holds a **current block**, the block into which the next simple statement is appended. Each statement type has a rule:

| AST construct | Rule |
|---|---|
| **Simple statement** (`x = 1`, `f(x)`, `x += 1`, `assert`, `import`) | Append to the current block. The current block does not change. |
| **`if cond: A else: B`** | Create `then`, `else`, `join` blocks. Lower the condition (see *short-circuit* below) so that it ends in `true` / `false` edges to `then` / `else`. Build `A` in `then` and `B` in `else`; each branch's end gets an edge to `join`. Continue in `join`. |
| **`elif`** | Python stores `elif` as an `if` inside `orelse`; the builder recurses with the else block as current block at the same nesting depth. |
| **`while cond: body`** | Create `header`, `body`, `after`. `current -> header`. Lower the condition into `header` with `true -> body` and `false -> after`. Build `body`; its end gets a **back edge** to `header`. Continue in `after`. |
| **`for x in it: body`** | Same shape. The header holds the whole `for` item ("fetch the next element, bind `x`, test whether there is one"). |
| **`break` / `continue`** | Edge to the innermost loop's `after` / `header`, found on a **loop stack** of `(header, after)` pairs. |
| **`return`** | Edge to EXIT. |
| **`raise`** | Edge to EXIT labelled `raise`, unless inside a `try` with handlers (then the `except` edges, below, are the successors). |
| **`with ctx as v: body`** | The context expression and the `as` target become an ordinary assignment in the current block; the body continues in the same flow. |
| **comprehension, lambda** | Part of an expression. Their own variables are scoped to them and are neither uses nor definitions of the enclosing function. |
| **nested `def`** | One item in the block: it defines the function's name and counts as a *use* of every outer variable the nested body reads. The nested function is analysed separately as `outer.inner`. |
| **End of function** | The final block gets an edge to EXIT. |

**Short-circuit lowering of `and` / `or` / `not`.** In `if a and b:` Python evaluates `b` only when `a` is true. A faithful CFG therefore has one block per operand:

```
 a  --true-->  b  --true-->  [then]
 |             |
 +--false-->   +--false-->  [else]      (a false skips b entirely)
```

`a or b` mirrors this (`a` true jumps straight to `then`). `not (a and b)` swaps the two targets. The recursion is `build_cond(test, cur, true_block, false_block)`. This makes data-flow precise: in `if a and (b := f(a)): return b` the use of `b` is only reachable where the walrus ran, so no false warning. Each boolean operator adds 1 to cognitive complexity and 1 to cyclomatic complexity.

**Constant conditions.** If a condition folds to a compile-time constant (`True`, `1`, `not 0`, `1 > 2`, `False and x`), only the taken edge is created. `while True:` therefore has no exit edge, so code after it is unreachable unless a `break` (or `return`) exists; `if True: ... else: ...` makes the `else` dead; `while False:` makes its body dead. `x and False` is **not** folded, because evaluating `x` may have effects.

**`try` / `except` / `else` / `finally`.** Any statement in a `try` body may raise, so the block before the `try` and every block created for its body get an `except` edge to each handler. A handler is a block starting with an `except T as e` item. Normal completion goes to `else`, then to `finally`. Real `finally` semantics are modelled the way compilers do it, by **duplicating the finally body**:

- the normal path runs one copy and continues after the `try`;
- an exception that escapes from the body, a handler or `else` runs a second copy (reached by `except` edges) and then leaves through EXIT with label `raise`;
- a `return` / `break` / `continue` / `raise` inside the `try` runs a fresh copy of every enclosing `finally` body before jumping (`CFG.jump`).

Without these copies, a `finally` line could be wrongly reported as unreachable or a variable wrongly reported as unassigned (both bugs were found and fixed by the random testing in section 6).

**How unreachable code is found.** After a `return`, `raise`, `break` or `continue`, the builder starts a **fresh block with no incoming edge** and keeps building there. A later reachability search flags any statement placed in it. A line is only reported if it appears in no reachable block (so one copy of a `finally` line being unreachable does not matter).

**Determinism.** Block ids are assigned in creation order (ENTRY = 0, EXIT = 1, then blocks as the walk meets them), and the walk visits the AST in source order. The structural path uses no iteration over unordered sets. The same source always yields byte-identical output, which is tested across different `PYTHONHASHSEED` values.

**Validation.** After building, `CFG.validate()` asserts invariants: EXIT has no successors, every other block has at least one, no edge points to a missing block, `true`/`false` edges come as a pair, and a constant-condition block has exactly one of them.

### 2.5 Data-flow analysis

Once the CFG exists, we can ask questions about *values flowing along edges*. A **data-flow analysis** assigns each block a set of facts and iterates until nothing changes (a **fixpoint**).

Each block's code is first reduced to an ordered list of **events**: `use x` (variable read) and `def x` (variable written). Within a single statement, uses come before defs (`x = x + 1` reads `x`, then writes it). `x += 1` is a use followed by a def. Implicit bindings (`for` targets, `except ... as e`, imports, nested `def` names) are defs that are never reported as dead stores.

**Which names are local?** Exactly as in Python: parameters plus every name assigned *anywhere* in the function body, *including unreachable code*. (`def f(): return x; x = 1` raises `UnboundLocalError`, so the analysis must treat `x` as local. This was found by the random testing.) Names never assigned (`print`, `len`) are globals and are ignored.

#### Definite assignment (forward, "must" analysis) -> *use before assignment*

Question: at each read of `x`, is `x` assigned on **every** path that reaches it?

- Fact set: variables definitely assigned. Parameters are assigned at ENTRY.
- At the start of a block: the **intersection** of the sets coming out of all its predecessors.
- A `def x` adds `x`; a `use x` with `x` not in the set is reported.
- Only blocks reachable from ENTRY take part (dead blocks would pollute the intersection).
- **Evidence path:** for each finding, a breadth-first search finds the shortest path ENTRY -> use block along which `x` is never assigned (`witness_path`). It is printed with the finding, highlighted in the GUI, and used in the cognitive cost.

#### Liveness (backward, "may" analysis) -> *dead stores*

Question: after this assignment, could the assigned value ever be read?

- A variable is **live** at a point if some path from there reads it before overwriting it.
- At the end of a block: the **union** of the live-in sets of its successors. Walking the events **backwards**: a use makes the variable live; a def kills it.
- A `def x` where `x` is not live is a **dead store**. Because one source line can appear in several blocks (copies of a `finally` body), a store is reported only if it is dead in *every* copy.
- Names beginning with `_` are never reported.

Both analyses use the same shape of loop: repeat until a full pass changes nothing. They terminate because the sets only move in one direction on a finite lattice.

#### Infinite loops and constant conditions

- A loop **can never exit** if its header is reachable from ENTRY but EXIT is not reachable from it. `can_exit` does a reverse search from EXIT over the reachable blocks; any loop header outside that set is reported as `infinite_loop`. `while True:` with a `break` or `return` is fine.
- `constant_condition`: an `if` whose test is constant, or a `while` whose test is constant false. (`while True:` is an idiom and is not reported by itself.)

### 2.6 Complexity metrics

**Cyclomatic complexity** (McCabe): `E - N + 2` over the reachable graph. It counts independent paths, roughly *decision points + 1*. Every `if`, loop, `and`/`or` operand and exception handler adds 1. The many `except` edges are *not* counted as edges; instead each reachable handler adds 1, which matches McCabe's definition (one extra decision per `except`).

**Cognitive complexity** (in the spirit of SonarSource's metric): how hard code is to *read*.

- `if`, `while`, `for`: `1 + nesting depth` (nested structures cost more).
- `elif`, `else`: `1` (no nesting penalty).
- each `except` handler: `1 + depth`.
- each boolean operator (`and` / `or`): `1`.
- finally copies made for jumps are not counted twice.

Two functions can have the same cyclomatic complexity but very different cognitive complexity if one nests its branches deeply.

### 2.7 The LLM's role and why it is "mediated"

The LLM sits **between the analysis and the human reader**:

```
 Findings (proven by the compiler)
        │
        ▼
 LLM writes a 2-sentence explanation + fix hint
        │
        ▼
 Grounding check (deterministic code)
   ├─ passes -> show the LLM's text   <llm>
   └─ fails / no key / error -> show template text   <template>
```

**Grounding check** (`grounded()`): the reply is accepted only if
- every integer in its prose (text outside backticks, so a suggested fix like `x = 0` is allowed) is the finding's line number, its block id, a block id on its evidence path, or a number that appears in the quoted source line, and
- every `backticked` bare identifier is the finding's variable, appears in the quoted source line, is a block name from the finding's evidence path (`ENTRY`, `B4`), or is a Python keyword/builtin (such as `None` or `print`). A backticked path such as `` `ENTRY -> B2 -> B5` `` must consist of known names only. Inside a backticked fix snippet like `` `last = None` `` only the first name (the variable being assigned) is checked, and string literals are ignored. For constant conditions `true`/`false` are allowed.

The prompt asks the model to backtick code identifiers precisely so this check can be applied mechanically. A reply such as "`ghost` is unset at line 42" fails and is replaced by the template sentence, while "initialise it with `result = 0` before line 5" passes. The rule is deliberate: *facts* about the program (line numbers, variable and block names) must come from the finding, but *advice* (a suggested fix) may introduce new literals. The first, stricter versions of the check rejected good replies (for suggesting `None`, for naming blocks of the evidence path, for writing `` `false` ``); each was found by testing against a live model and then relaxed in a controlled way.

**Reproducibility:** temperature 0 and an on-disk cache keyed by `hash(provider + model + prompt)`, so re-running the tool returns identical text and does not call the API again. If a call fails (bad key, no network, unknown model) the template is used and the reason is shown (`note: LLM unavailable ... HTTP Error 401`).

### 2.8 What "cognitive semantic diagnostic" means here

- **Semantic:** the diagnostics concern *meaning* (is the variable assigned? is the value used? can the code run? can the loop stop?), not formatting or syntax.
- **Cognitive:** each finding is ranked with a proxy for how hard it is for a person to notice:

  ```
  cost = nesting depth of the offending line + (length of the evidence path, for use-before-assignment)
  rank = severity x (1 + cost)
  severity: infinite loop 3, uninitialised use 3, unreachable code 2, constant condition 2, dead store 1
  ```

  A problem buried inside several nested blocks, or one that only appears after following a long path through the function, ranks higher than the same problem in plain sight.
- **Diagnostic framework:** each finding is a structured record (`kind`, `line`, `var`, `block`, `depth`, `cost`, `rank`, `source`, `path`, `value`) that feeds the ranking, the explanation stage, and the GUI.

---

## 3. Worked example, end to end

Input, [examples/bugs.py](examples/bugs.py):

```python
1  def buggy(a, flag):
2      unused = a * 2
3      if flag:
4          result = a + 1
5      total = result + 1
6      return total
7      print("never")
```

Run: `python cfgtool.py examples/bugs.py`

### Step 1. AST
`ast.parse` produces a `FunctionDef` whose body holds: an `Assign`, an `If` (one `Assign` in its body, empty `orelse`), an `Assign`, a `Return`, and an `Expr` (the `print` call).

### Step 2. CFG
Actual output:

```
  ENTRY  {} -> B2
  EXIT   {} -> -
  B2     {unused = a * 2; flag} -> B3[true], B4[false]
  B3     {result = a + 1} -> B5
  B4     {} -> B5
  B5     {total = result + 1; return total} -> B1[return]
  B6     {print('never')} -> B1
```

How it was built:
- `unused = a * 2` is simple, so it goes into B2. The `if` puts its test `flag` into B2 as well, then creates B3 (then), B4 (else, empty), B5 (join).
- `result = a + 1` goes into B3. B3 and B4 both flow to B5.
- `total = result + 1` and `return total` continue in B5. The `return` adds the edge `B5 -> EXIT` and starts a fresh block B6.
- `print('never')` lands in B6. Nothing points to B6, so it is unreachable. It still gets an edge to EXIT because every block must end somewhere.

### Step 3. Analyses
- **Reachability:** B6 is not reachable and holds a statement -> *unreachable code at line 7*.
- **Definite assignment:** the set coming out of B3 contains `result`; B4's does not. At the join B5 the intersection drops `result`, so the read at line 5 is flagged. The evidence path is `ENTRY -> B2 -> B4 -> B5`.
- **Liveness:** `unused` is assigned in B2 but never read -> *dead store at line 2*. (`total` is live because the `return` reads it.)
- **Metrics:** cyclomatic complexity 2 (one `if`), cognitive complexity 1.

### Step 4. Ranking

| Finding | Severity | Cost | Rank |
|---|---|---|---|
| uninit `result`, line 5 | 3 | depth 0 + path length 3 = 3 | 3 x 4 = **12** |
| unreachable, line 7 | 2 | 0 | 2 |
| dead store `unused`, line 2 | 1 | 0 | 1 |

### Step 5. Explanation
Without `--llm` (real output):

```
[rank 12] uninit @ line 5 (cost 3): 'result' may be used at line 5 before it is assigned: some path reaches it without assigning 'result' (for example via ENTRY -> B2 -> B4 -> B5).  <template>
    evidence path: ENTRY -> B2 -> B4 -> B5
```

With `--llm` and a key, the model's sentence replaces it if it passes the grounding check, and the line ends with `<llm>`.

### A `try` example
[examples/bug_try.py](examples/bug_try.py) `load()`: `data` is assigned in the `try` body, the handler only prints, and `data` is returned. The CFG has `except` edges from the start and from the body block to the handler, plus two copies of the `finally` body:

```
  B2  {}                                   -> B3, B6[except], B7[except]
  B3  {f = open(path); data = f.read()}    -> B5, B6[except], B7[except]
  B4  {return data}                        -> B1[return]
  B5  {print('done')}                      -> B4              (finally, normal path)
  B6  {except OSError as e; print(...)}    -> B5, B7[except]
  B7  {print('done')}                      -> B1[raise]       (finally, exception path)
```

The finding is `uninit data @ line 9`, evidence path `ENTRY -> B2 -> B6 -> B5 -> B4` (cost 4, rank 15): the exception is caught, the handler does not assign `data`, `finally` runs, and `return data` fails.

---

## 4. Code walkthrough

### 4.1 [cfgtool.py](cfgtool.py)

**Constants and errors.** `PROVIDERS` (LLM backends), `SEVERITY` (finding kind -> weight), `CACHE_FILE`, and `Unsupported` (raised for constructs outside the subset).

**Constant folding.** `_const(node)` evaluates literals, unary `+ - not`, `+ - * // %`, comparisons (chained too) and `and`/`or` (left to right, so `False and x` is constant but `x and False` is not); `const_value(node)` wraps it as `(known, truthiness)`.

**`Block`** and **`CFG`**:

| Member | Purpose |
|---|---|
| `Block.stmts`, `.succs`, `.const` | AST items, `(target, label)` edges, and whether the block ends in a constant condition (then it has one edge, not two). |
| `CFG.__init__(fn)` | Creates ENTRY (0), EXIT (1), a first block; builds the body; initialises the loop stack, `finals` stack (enclosing `finally` bodies), loop headers, constant conditions, handler ids, depth map, cognitive counter. |
| `new`, `edge`, `add`, `name` | Allocate a block, add an edge, append an item (recording its nesting depth by line), display name (`ENTRY`/`EXIT`/`B<n>`). |
| `build_stmts`, `build_stmt` | Walk statement lists; dispatch on statement type; reject unsupported nodes; handle simple statements and `return`/`raise`/`break`/`continue`. |
| `jump(cur, target, label, level)` | Adds the edge for a leaving jump after routing it through a copy of each enclosing `finally` body (innermost first). |
| `build_cond(test, cur, true_b, false_b, depth)` | Short-circuit lowering of `and`/`or`/`not`, constant pruning, cognitive +1 per boolean operator. |
| `build_if`, `build_loop` | `if`/`elif`/`else` and `while`/`for`; cognitive complexity; loop stack; records constant conditions and loop headers. |
| `build_with`, `build_try` | `with` as ordinary statements; `try` with handlers, `else`, the two `finally` copies and the `except` edges. |
| `reachable()`, `can_exit()`, `preds()` | Forward DFS from ENTRY, reverse DFS from EXIT, predecessor lists. |
| `validate()` | Asserts the structural invariants. |
| `cyclomatic()` | `E - N + 2` over reachable blocks, without `except` edges, plus one per reachable handler. |
| `text()`, `dot()` | Plain-text and Graphviz renderings. |

`label(node)` gives the display text of a block item (a `for` header without its body, `except T as e`, `def f(args)`).

**Functions in a file.** `collect_functions(tree)` returns `(qualified name, FunctionDef)` for top-level functions, methods (`Class.method`), nested functions (`outer.inner`), and module-level code as a pseudo-function `<module>` (when there are no functions, or when the module has control flow such as `if __name__ == "__main__":`). `call_graph(funcs)` maps each function to the analysed functions it calls (matched by simple name; calls inside nested functions belong to the nested function). `analyze_source(src)` analyses everything and returns `(results, call graph)`; `summary_text` formats the table and the graph.

**Analyses.**
- `_collect`, `events`, `block_events`: turn AST items into ordered use/def events, with scoping for comprehensions and lambdas and special handling of compound-statement headers.
- `witness_path`: BFS for the evidence path of a use-before-assignment.
- `analyze(fn, src_lines)`: builds the CFG, validates it, computes the local-name set (lexical scoping), then runs: unreachable lines, constant conditions, infinite loops (headers outside `can_exit`), definite assignment, liveness, each with a fixpoint phase and a reporting phase. It deduplicates findings and sorts them by `(-rank, line, kind, var)`, a total order, so the output is deterministic. Finding: `{kind, line, var, block, depth, cost, rank, source, path, value}`.

**Explanation layer.**
- `template(f)`: deterministic sentence for each of the five kinds.
- `grounded(reply, f)`: the hallucination guard (section 2.7).
- `llm_provider()`, `llm_model(name)`, `llm_call(prompt)`: provider-agnostic access via `urllib` (no SDK). **Groq** (OpenAI-compatible chat completions, default model `openai/gpt-oss-20b`) and **Anthropic** (Messages API, default `claude-sonnet-5-5`). Groq is chosen if `GROQ_API_KEY` is set, else Anthropic if `ANTHROPIC_API_KEY` is set; `LLM_PROVIDER=groq|anthropic` forces a choice; `GROQ_MODEL` / `ANTHROPIC_MODEL` override the model. Any failure returns `None` and records the reason in `LAST_LLM_ERROR`. Temperature 0; replies cached by SHA-256 of provider + model + prompt.
- `explain(f, use_llm, client)`: builds the prompt (the finding as JSON, including the evidence path), calls the client, collapses whitespace (models emit non-breaking spaces), and returns `(text, "llm")` if the reply passes `grounded`, otherwise `(template(f), "template")`. The `client` parameter lets tests inject a fake LLM.

**CLI and self-test.** `report`, `main` (reads the file as `utf-8-sig` so a byte-order mark is fine; exit code 0 ok, 1 no matching function, 2 syntax error), `selftest`.

### 4.2 [cfgdraw.py](cfgdraw.py) (drawing, no GUI dependency)

| Function | What it does |
|---|---|
| `compute_levels(cfg)` | DFS finds back edges; layer = longest forward path from ENTRY. |
| `layout(cfg, bad_blocks, measure, line_height, palette)` | Places blocks in centred rows (unreachable blocks in a last row) and emits drawing primitives. **Edge routing never passes behind a box:** an edge to the next row is a straight arrow; a loop back edge leaves the bottom of its source, runs through the gap between rows to a lane on the right, climbs to the gap above the target row and enters it from the top; edges that skip rows (or point upward, e.g. dead code to EXIT) use a lane on the left. Colours: `true` green, `false` red, `except` purple dashed, others grey; red outline = block with a finding; dashed grey box = unreachable. `palette` is `cfgdraw.LIGHT` (default, also used by exports) or `cfgdraw.DARK`. |
| `to_svg`, `render_svg(cfg, bad)` | SVG text from the primitives. |
| `render_png(cfg, path, bad)` | PNG via Pillow (2x scale). |

The same primitives are drawn on the Tk canvas, in SVG and in PNG, so all three look identical. Only the text-width function differs.

### 4.3 [gui.py](gui.py)

`gui.py` adds no analysis logic: it calls `cfgtool.analyze_source` and `cfgtool.explain` and draws with `cfgdraw`.

| Part | What it does |
|---|---|
| `GraphView` | Tk canvas that draws the layout primitives at any zoom (coordinates and font sizes are scaled and redrawn, never bitmap-stretched). `set_zoom` keeps the point under the cursor fixed (`force` redraws even at an unchanged scale), `fit` fits the whole graph (never above 100%), Ctrl+wheel zooms, wheel / Shift+wheel scroll, dragging pans. Every block is tagged `blk<id>` (box and text) for hover tooltips and clicks. `highlight(bid, path_ids, extra)` outlines the selected block (thick orange), the evidence path (orange) and extra blocks (cyan). |
| `Tooltip` | Small borderless window that follows the mouse over a block. |
| `App.analyze(silent)` | Parses the editor text, analyses every function, fills the **Function** dropdown (functions, methods, nested functions, `<module>`; unsupported ones are marked "skipped"). Errors give a status message, a message in the detail box and, unless `silent` (live mode), a popup. |
| `App.show_selected()` | Draws the chosen function, updates the metrics line, the findings table and the editor highlights; fits the view when the graph is larger than the window. |
| `App.on_select()` | Shows the explanation, evidence path and cognitive cost; highlights the source line (yellow), the block and the path. |
| `App.on_block_click(bid)` / `App.block_tooltip(bid)` | Click a block: its source lines are shaded in the editor and its finding (if any) is selected. Hover: statements, successors, UNREACHABLE flag and findings. |
| `App._cursor_changed()` | The reverse link: clicking or moving in the editor outlines (cyan) every block that contains the cursor's line. |
| `App.highlight_syntax()` | Regex tokenizer colours keywords, strings, comments, numbers, definition names and builtins (debounced 120 ms; robust against unterminated strings). |
| `App.apply_theme(name)` / `toggle_theme` | Light and dark schemes for every widget (ttk "clam" theme, editor, gutter, findings table, legend) and the graph palette. |
| `App.sort_by(col)` | Sort the findings table by any column; the same column again reverses. Rows are colour-coded by finding kind. |
| Live analysis | With "Live analysis" ticked, a pause of 700 ms after typing re-analyses silently (no popups, no LLM calls); navigation keys do not trigger it. |
| `App.show_summary()` | Window with the per-function table and the call graph. |
| `App.export_graph()` / `save_file()` | Save dialogs: PNG, SVG or DOT by extension (PNG needs Pillow; otherwise a hint to use SVG); save the editor text. |
| `_explain_all()` / `_poll()` | With "Explain with LLM" ticked, a background thread requests grounded explanations and posts results to a queue that the Tk thread polls every 100 ms, so the window never freezes. A generation counter discards results from an outdated analysis. |

---

## 5. How to run it

Requirements: Python 3.9+ (uses `ast.unparse`), with `tkinter` for the GUI (included in the standard Windows/macOS installers). Optional: `pip install pillow pytest`.

### Graphical app

```bash
python gui.py
```

1. Type or paste code in the left editor (syntax-highlighted, with line numbers; **A-** / **A+** or Ctrl+wheel change the font size), or pick a file from the **Example** dropdown, or use **Open...**. **Save...** writes the editor text to a file.
2. Click **Analyze** (F5 / Ctrl+Enter). Code with no `def` is analysed as `<module>`; with several functions, choose one in the **Function** dropdown. **Summary** shows all functions at once with the call graph. Tick **Live analysis** to re-analyse automatically 0.7 s after you stop typing.
3. The right side shows the CFG, a metrics line (reachable blocks, cyclomatic, cognitive complexity) and the ranked findings (columns: rank, kind, line, cognitive cost, origin; click a heading to sort). Click a finding to highlight its line, its block and its evidence path.
4. **Explore the graph:** the `-` / `+` / `100%` / **Fit** buttons (or Ctrl+wheel, Ctrl+`=` / Ctrl+`-` / Ctrl+0) zoom; drag to pan; hover a block for its statements, successors and findings; **click a block** to shade its source lines in the editor; **click a line in the editor** to outline its blocks (cyan) in the graph.
5. **Dark mode / Light mode** switches the whole window. **Export graph...** (Ctrl+E) saves PNG / SVG / DOT (exports are always light); **Copy DOT** copies Graphviz text.
6. Optional: tick **Explain with LLM** (needs `GROQ_API_KEY` or `ANTHROPIC_API_KEY`). The "origin" column shows which explanation each row uses.

Graph key (also shown under the graph): green arrow `true`, red `false`, purple dashed `except`, grey other; red-outlined box = finding; dashed grey box = unreachable; blue box = ENTRY / EXIT; orange = selected block / evidence path; cyan = blocks of the editor's current line.

Shortcuts: F5 / Ctrl+Enter analyze, Ctrl+O open, Ctrl+S save, Ctrl+E export, Ctrl+`=` / Ctrl+`-` zoom, Ctrl+0 fit.

### Command line

```bash
# Analyse every function in a file (a summary and call graph follow if there are several)
python cfgtool.py examples/multi_function.py

# One function only (simple or qualified name), plus Graphviz output
python cfgtool.py examples/multi_function.py --function Counter.bump --dot

# Explanations by the LLM (falls back to templates without a key)
set GROQ_API_KEY=your-key             # Windows cmd (no quotes); or ANTHROPIC_API_KEY
$env:GROQ_API_KEY="your-key"          # PowerShell (with quotes)
# optional: $env:GROQ_MODEL="another-model-id"   $env:LLM_PROVIDER="groq"
python cfgtool.py examples/bugs.py --llm

# Built-in checks and the full test suite
python cfgtool.py --selftest
python -m pytest tests -q
```

Exit codes: 0 success, 1 no matching function, 2 syntax error.

**Reading the output.** Each finding looks like:

```
[rank 12] uninit @ line 5 (cost 3): <explanation>  <template|llm>
    evidence path: ENTRY -> B2 -> B4 -> B5
```

`<template>` means the deterministic sentence was used; `<llm>` means the model's reply passed the grounding check. If `--llm` was requested but a call failed, a `note: LLM unavailable ...` line gives the reason.

**Setting a key permanently (Windows).** `setx GROQ_API_KEY your-key` in cmd (open a new terminal afterwards). Never put keys in source files or commit them. Groq retires model names over time; if you get `HTTP Error 404`, the model is gone, so list current ones at `https://api.groq.com/openai/v1/models` and set `GROQ_MODEL`.

### Example programs ([examples/](examples/))

| File | Expected findings |
|---|---|
| `ok.py` | none (if / elif / else) |
| `loops.py` | none (for, while, break, continue) |
| `clean_gcd.py` | none (while loop, swap via temp variable) |
| `clean_primes.py` | none (two functions, early returns, loop with a call) |
| `clean_max.py` | none (for loop with if / elif) |
| `clean_shortcircuit.py` | none (`and` / `or` conditions, `while i < n and ...`, `while True` with `break`) |
| `clean_try_with.py` | none (`try`/`except` that assigns, `with`, comprehensions, lambda) |
| `multi_function.py` | none; shows methods, a nested function, `<module>` and the call graph |
| `bugs.py` | uninitialised use (line 5), unreachable code (line 7), dead store (line 2) |
| `bug_loop_var.py` | uninitialised use of `last` (line 7): it is only assigned inside the loop's `if` |
| `bug_unreachable.py` | unreachable code at line 6 (after if/else that both return), and at lines 13 and 15 (after `break` and `continue`) |
| `bug_nested.py` | uninitialised use of `note` at line 9, nested three levels deep, so a long evidence path and a high rank |
| `bug_dead_stores.py` | dead stores: `result` (line 2, overwritten), `scratch` (line 4), `tmp` (lines 5 and 6) |
| `bug_infinite.py` | infinite loop (line 2) with unreachable line 4; constant conditions at lines 8 and 12 with unreachable lines 11 and 13 |
| `bug_try.py` | uninitialised `data` after a handler that does not assign it (line 9); unreachable code after a `raise` (line 15) and after a `try` whose paths all return (line 18) |

---

## 6. Testing

Three layers: `python cfgtool.py --selftest` (no dependencies, covers the main features), `python -m pytest tests -q` (**504 tests**, about 15 seconds), and manual live tests against a real LLM.

### The pytest suite ([tests/](tests/))

| File | What it checks |
|---|---|
| `test_examples.py` | Golden test: all 15 examples produce exactly the documented findings; every example file is covered. |
| `test_builder.py` | Structure of the CFG for each construct (blocks, edge labels, join blocks, back edges, break/continue targets, short-circuit wiring for `and`/`or`/`not`, constants, infinite loops, try/except/finally, `with`, comprehensions, nested functions); unsupported constructs; definite assignment and the witness path; dead-store rules; ranking and cost; cognitive and cyclomatic counts; the validator rejecting broken graphs; **byte-identical output under different `PYTHONHASHSEED` values**; function collection and the call graph. |
| `test_regressions.py` | One test per bug found by the random testing (below). |
| `test_random_programs.py` | **Property and dynamic soundness tests** on seeded random programs (250 by default; `CFG_SEEDS=5000` for a long run). |
| `test_llm.py` | Templates for every finding kind; 15+ grounding cases (wrong line, invented identifier, allowed fix snippets, block names, invented blocks, true/false); fallback; prompt content; provider selection; Groq and Anthropic request shapes; cache (a second call needs no network, even with a bad key); failure reasons; malformed replies are not cached. No real network access. |
| `test_cli.py` | Output format, `--function`, summary and call graph, `--dot`, exit codes, BOM files, skipped functions, `--llm` without a key. |
| `test_draw.py` | For every function of every example: one box per block, one line and one arrowhead per edge, boxes never overlap and stay on the canvas, **no edge passes through a box it does not touch**, forward edges go to deeper layers; SVG is well-formed XML and escapes special characters; PNG is a valid image. |
| `test_gui_features.py` | The interactive GUI features on the real Tk app: zoom limits and scrollregion scaling, `fit` (idempotent, never enlarges), Ctrl+wheel, block tooltips, click-to-lines and cursor-to-blocks linking, theme toggling (all examples drawn in both themes), font limits, syntax-highlight tags and odd input, live analysis (debounce, silent errors, no LLM, navigation keys ignored), sorting, save / export / open dialogs (patched). Any exception inside a Tk callback fails the test. |
| `test_gui_smoke.py` | Starts the real Tk app: loads every example, selects every finding of every function, plain-script input, syntax errors and empty input, exports, the LLM checkbox (no key; fake provider via the background thread and queue). Skipped automatically without a display. |

### Dynamic soundness test (the strongest check)

`test_random_programs.py` generates random programs (nested `if`/`elif`/`else` with `and`/`or`/`not`, bounded `while`, `for`, `break`, `continue`, `return`, `try`/`except`/`else`/`finally`, `raise`), then **actually runs** each one under `sys.settrace` on five inputs and compares real behaviour with the static claims:

- every line that really executes must not have been reported **unreachable**;
- every `UnboundLocalError` that really happens must have been reported as a **use before assignment** for that variable and line;
- structural invariants and determinism hold for every program;
- a guard test ensures the generator keeps producing bugs, loops and handlers;
- hand-made programs check that the **infinite loop** flag matches real behaviour (and that a loop that is endless at run time but not provable statically, `while a >= 0: a += 1`, is *not* flagged).

Runs of 6,000 programs x 5 inputs pass.

### Bugs this testing found (all fixed, each with a regression test)

| Found by | Bug |
|---|---|
| random dynamic test | A name assigned only in unreachable code was treated as a global, so a use-before-assignment was missed (Python scoping is lexical). |
| random dynamic test | `break` / `continue` / `return` inside a `try` with `finally` skipped the `finally` body, so its lines were wrongly reported unreachable. Fixed with per-jump copies of the finally body. |
| random dynamic test | An exception raised inside an `except` handler or `else` did not run `finally`; fixed with an exceptional copy reached from every block of the statement. |
| pytest | The uncaught-exception path through `finally` fell through to the code after the `try`, causing a false "possibly unassigned"; the exceptional copy now ends at EXIT. |
| pytest | A dead store in the exceptional finally copy was reported although it is live on the normal path; stores are now judged across all copies of a line. |
| layout images | Edges passed behind boxes and labels overlapped; replaced by lane routing (test: no edge passes through a box). |
| GUI screenshot | `Fit` crashed with a division by zero (the zoom was temporarily set to 0 to force a redraw); fixed with an explicit `force` flag. It slipped past the first GUI tests, so the fixture now turns any exception inside a Tk callback into a test failure. |
| live LLM | Retired default model (HTTP 404); grounding check too strict for fix snippets, for evidence-path block names and for `false`; a crash on files with a byte-order mark. |

### Manual live-LLM test

With a real Groq key (`openai/gpt-oss-20b`), `--llm` on all 7 buggy examples gave **21 of 21** explanations that passed the grounding check and were shown as `llm`. In the GUI, ticking the checkbox changed rows from `template` to `llm` through the background thread. Re-running with an invalid key served identical text from the cache; an invalid key on an uncached finding fell back to the template and reported `HTTP Error 401`.

---

## 7. Limitations and future work

- **Python subset.** No `match`, `async`, `except*`, or loop `else`. Calls to other functions are opaque (the call graph is name-based and there is no inter-procedural analysis).
- **Exceptions are approximated.** Any statement in a `try` body may raise, so handlers are reachable from every body block; exception *types* are not matched against handler types, and calls to other functions that might raise outside a `try` do not create edges to EXIT.
- **Expression-level control flow.** `and`/`or`/`not` are split into blocks only in `if` and `while` conditions. In `x = a and b` or `a if c else b` the expression stays one item. Conditional expressions and comprehension conditions are not split.
- **Constant folding is intentionally small.** Literals, arithmetic, comparisons, boolean operators. It does not track variables, so `x = True; while x:` is not recognised as endless.
- **Path-insensitive analyses.** "Possibly unassigned" can flag a use that is safe because two conditions are correlated.
- **Cognitive cost is still a proxy.** Nesting depth plus evidence path length; a richer model of human difficulty is future work.
- **GUI layout.** Longest-path layering with blocks ordered by id; large graphs get tall and there is no zoom. PNG text widths use Pillow's font metrics, so very long lines may differ slightly from the on-screen drawing.

Ideas for extension: dominators and natural-loop nesting, LLM-proposed fixes verified by re-running the pipeline on the patched code, variable-aware constant propagation, conditional-expression splitting, and an HTML report with the rendered graphs.
"# CC_Mini_Project" 

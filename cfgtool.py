"""LLM-mediated deterministic AST-to-CFG pipeline with semantic diagnostics.

Usage:
    python cfgtool.py FILE.py [--function NAME] [--dot] [--llm]
    python cfgtool.py --selftest

Stages: ast.parse -> CFG builder (deterministic) -> validator -> analyses
(reachability, constant conditions, infinite loops, definite assignment, liveness,
complexity) -> ranked findings -> optional LLM explanation with a grounding check and
template fallback.

The CFG builder understands: assignment, if/elif/else, while, for, break, continue,
return, raise, assert, import, with, try/except/else/finally, nested function
definitions, comprehensions and lambdas (as scoped expressions). `and` / `or` /
`not` inside if/while conditions are lowered with short-circuit evaluation (one basic
block per operand). Constant conditions remove the impossible edge, so code after
`while True:` without a break is unreachable.

The LLM never influences the CFG or the findings; it only rewords them. Providers
(chosen from the environment, see llm_provider): Groq via GROQ_API_KEY, or Anthropic
via ANTHROPIC_API_KEY. LLM_PROVIDER forces one; GROQ_MODEL / ANTHROPIC_MODEL override
the model. Without a key the tool still works and uses template explanations.
"""
import argparse
import ast
import builtins
import hashlib
import json
import keyword
import operator
import os
import re
import sys
import urllib.request
from collections import deque

PROVIDERS = {
    "groq": {"key_env": "GROQ_API_KEY", "model_env": "GROQ_MODEL", "model": "openai/gpt-oss-20b",
             "url": "https://api.groq.com/openai/v1/chat/completions"},
    "anthropic": {"key_env": "ANTHROPIC_API_KEY", "model_env": "ANTHROPIC_MODEL", "model": "claude-sonnet-5-5",
                  "url": "https://api.anthropic.com/v1/messages"},
}
LAST_LLM_ERROR = ""
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".llm_cache.json")
SEVERITY = {"infinite_loop": 3, "uninit": 3, "unreachable": 2, "constant_condition": 2, "dead_store": 1}


class Unsupported(Exception):
    """Raised when the input uses a Python construct outside the supported subset."""


# --------------------------------------------------------------------------
# Constant folding (used to prune impossible edges)
# --------------------------------------------------------------------------
class _NotConst(Exception):
    """Internal: the expression is not a compile-time constant."""


_CMP = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le,
        ast.Gt: operator.gt, ast.GtE: operator.ge, ast.Is: operator.is_, ast.IsNot: operator.is_not}
_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod}
_UN = {ast.USub: operator.neg, ast.UAdd: operator.pos, ast.Not: operator.not_}


def _const(n):
    """Evaluate a constant expression or raise _NotConst.

    Supports literals, unary +/-/not, + - * // %, comparisons, and `and` / `or`
    (evaluated left to right, so `False and x` is constant but `x and False` is not).
    """
    if isinstance(n, ast.Constant):
        return n.value
    try:
        if isinstance(n, ast.UnaryOp) and type(n.op) in _UN:
            return _UN[type(n.op)](_const(n.operand))
        if isinstance(n, ast.BinOp) and type(n.op) in _BIN:
            return _BIN[type(n.op)](_const(n.left), _const(n.right))
        if isinstance(n, ast.Compare) and all(type(o) in _CMP for o in n.ops):
            left = _const(n.left)
            for op, right in zip(n.ops, n.comparators):
                right = _const(right)
                if not _CMP[type(op)](left, right):
                    return False
                left = right
            return True
        if isinstance(n, ast.BoolOp):
            val = None
            for v in n.values:
                val = _const(v)
                if isinstance(n.op, ast.And) and not val:
                    return val
                if isinstance(n.op, ast.Or) and val:
                    return val
            return val
    except _NotConst:
        raise
    except Exception:          # e.g. division by zero, type errors: treat as not constant
        raise _NotConst()
    raise _NotConst()


def const_value(node):
    """Return (True, truthiness) if `node` is a compile-time constant condition, else (False, None)."""
    try:
        return True, bool(_const(node))
    except _NotConst:
        return False, None


# --------------------------------------------------------------------------
# CFG data structures and AST -> CFG construction
# --------------------------------------------------------------------------
class Block:
    """A basic block: a straight-line run of AST items with labelled outgoing edges."""

    def __init__(self, bid):
        """Create an empty block with the given integer id.

        Args:
            bid: Unique block number, assigned in creation order (deterministic).
        """
        self.id = bid
        self.stmts = []     # AST items (statements, condition expressions, handlers)
        self.succs = []     # (Block, label), label in "", true, false, break, continue, return, raise, except
        self.const = False  # True if this block ends in a constant condition (only one edge)


SUPPORTED = (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Expr, ast.Pass, ast.If, ast.While,
             ast.For, ast.Break, ast.Continue, ast.Return, ast.Raise, ast.Assert, ast.Delete,
             ast.Import, ast.ImportFrom, ast.With, ast.Try, ast.FunctionDef, ast.Global, ast.Nonlocal)


class CFG:
    """Control flow graph of one function, built deterministically from its AST."""

    def __init__(self, fn):
        """Build the CFG for a function.

        Creates ENTRY (id 0) and EXIT (id 1), then lowers the body recursively.

        Args:
            fn: An ast.FunctionDef node (or a synthetic one for module-level code).

        Raises:
            Unsupported: If the body uses an unsupported construct.
        """
        self.fn = fn
        self.blocks = []
        self.depth = {}           # line number -> nesting depth
        self.cog = 0              # cognitive complexity
        self.loops = []           # stack of (header, after)
        self.loop_headers = {}    # header block id -> line of the loop statement
        self.const_conds = []     # (line, value) for constant `if` tests and `while <falsy>`
        self.handler_ids = set()  # ids of except-handler entry blocks
        self.catch = 0            # >0 while building a try body that has handlers
        self.finals = []          # stack of (finally body, depth, loop level) for enclosing try/finally
        self.entry = self.new()
        self.exit = self.new()
        first = self.new()
        self.edge(self.entry, first)
        last = self.build_stmts(fn.body, first, 0)
        self.edge(last, self.exit)

    # -- helpers
    def new(self):
        """Allocate, register and return a fresh empty block."""
        b = Block(len(self.blocks))
        self.blocks.append(b)
        return b

    @staticmethod
    def edge(a, b, label=""):
        """Add a control-flow edge a -> b with an optional label ("true", "false", "break", ...)."""
        a.succs.append((b, label))

    def add(self, block, node, depth):
        """Append an AST item to a block and record its nesting depth by line number."""
        block.stmts.append(node)
        self.depth[node.lineno] = depth

    def name(self, b):
        """Display name of block `b`: ENTRY, EXIT or B<id>."""
        return "ENTRY" if b is self.entry else "EXIT" if b is self.exit else f"B{b.id}"

    # -- construction
    def build_stmts(self, stmts, cur, depth):
        """Lower a statement list starting in block `cur`; return the block where control ends up."""
        for s in stmts:
            cur = self.build_stmt(s, cur, depth)
        return cur

    def build_stmt(self, s, cur, depth):
        """Lower one statement.

        Simple statements are appended to `cur`. `return`, `raise`, `break` and
        `continue` add their edge and hand back a fresh block, so any code after them
        lands in an unreachable block. Compound statements delegate to their builders.

        Returns:
            The block in which the next statement should be placed.

        Raises:
            Unsupported: For unsupported statements or break/continue outside a loop.
        """
        if not isinstance(s, SUPPORTED):
            raise Unsupported(f"unsupported construct {type(s).__name__} at line {s.lineno}")
        if isinstance(s, (ast.Pass, ast.Global, ast.Nonlocal)):
            return cur
        if isinstance(s, ast.If):
            return self.build_if(s, cur, depth, False)
        if isinstance(s, (ast.While, ast.For)):
            return self.build_loop(s, cur, depth)
        if isinstance(s, ast.Try):
            return self.build_try(s, cur, depth)
        if isinstance(s, ast.With):
            return self.build_with(s, cur, depth)
        self.add(cur, s, depth)
        if isinstance(s, ast.Return):
            self.jump(cur, self.exit, "return", 0)
            return self.new()          # following code is unreachable
        if isinstance(s, ast.Raise):
            if not self.catch:         # inside a try-with-handlers the "except" edges are added later
                self.jump(cur, self.exit, "raise", 0)
            return self.new()
        if isinstance(s, (ast.Break, ast.Continue)):
            if not self.loops:
                raise Unsupported(f"break/continue outside loop at line {s.lineno}")
            header, after = self.loops[-1]
            if isinstance(s, ast.Break):
                self.jump(cur, after, "break", len(self.loops))
            else:
                self.jump(cur, header, "continue", len(self.loops))
            return self.new()
        return cur

    def jump(self, cur, target, label, level):
        """Add the edge for return/raise/break/continue, running enclosing `finally` bodies first.

        Each try/finally that the jump leaves (those entered at loop level >= `level`;
        level 0 means all of them) contributes a fresh copy of its finally body, innermost
        first, between `cur` and `target`, as real `finally` semantics require.
        Cognitive complexity is not counted for these copies.
        """
        saved_finals, cog = self.finals, self.cog
        src = cur
        for i in reversed([i for i, e in enumerate(saved_finals) if e[2] >= level]):
            body, depth, _ = saved_finals[i]
            blk = self.new()
            self.edge(src, blk)
            self.finals = saved_finals[:i]       # only the finally blocks outside this one are active inside it
            src = self.build_stmts(body, blk, depth)
        self.finals, self.cog = saved_finals, cog
        self.edge(src, target, label)

    def build_cond(self, test, cur, true_b, false_b, depth):
        """Lower a condition with short-circuit evaluation.

        `a and b` becomes a block testing `a` (false goes straight to the false
        target, true goes on to the block testing `b`); `a or b` mirrors that.
        `not (a and b)` swaps the targets. A leaf condition gets two edges
        ("true"/"false"), or only the taken edge if it is a constant.
        Each boolean operator adds 1 to cognitive complexity.

        Args:
            test: The condition expression.
            cur: Block that receives the first operand.
            true_b: Block to continue in if the condition holds.
            false_b: Block to continue in otherwise.
            depth: Current nesting depth.
        """
        if isinstance(test, ast.BoolOp):
            self.cog += 1
            is_and = isinstance(test.op, ast.And)
            for v in test.values[:-1]:
                nxt = self.new()
                if is_and:
                    self.build_cond(v, cur, nxt, false_b, depth)
                else:
                    self.build_cond(v, cur, true_b, nxt, depth)
                cur = nxt
            self.build_cond(test.values[-1], cur, true_b, false_b, depth)
            return
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not) and isinstance(test.operand, ast.BoolOp):
            self.build_cond(test.operand, cur, false_b, true_b, depth)
            return
        self.add(cur, test, depth)
        known, val = const_value(test)
        if known:
            cur.const = True
            self.edge(cur, true_b if val else false_b, "true" if val else "false")
        else:
            self.edge(cur, true_b, "true")
            self.edge(cur, false_b, "false")

    def build_if(self, s, cur, depth, is_elif):
        """Lower an if/elif/else.

        The condition is lowered by build_cond into then/else blocks that rejoin at a
        join block. Updates cognitive complexity (an elif adds 1 without a nesting
        penalty; an else adds 1) and records a constant test for diagnostics.

        Args:
            s: The ast.If node.
            cur: Block receiving the condition.
            depth: Current nesting depth.
            is_elif: True when `s` is the elif of an enclosing if.

        Returns:
            The join block.
        """
        self.cog += 1 if is_elif else 1 + depth
        then, els, join = self.new(), self.new(), self.new()
        known, val = const_value(s.test)
        if known:
            self.const_conds.append((s.test.lineno, val))
        self.build_cond(s.test, cur, then, els, depth)
        self.edge(self.build_stmts(s.body, then, depth + 1), join)
        if len(s.orelse) == 1 and isinstance(s.orelse[0], ast.If):
            e_end = self.build_if(s.orelse[0], els, depth, True)
        else:
            if s.orelse:
                self.cog += 1
            e_end = self.build_stmts(s.orelse, els, depth + 1)
        self.edge(e_end, join)
        return join

    def build_loop(self, s, cur, depth):
        """Lower a while or for loop.

        Creates header (test / iterator), body and after blocks; the body loops back
        to the header, which exits to `after` on "false". For `while`, the test is
        lowered by build_cond (so `while a and b` gets one block per operand and a
        constant-true test has no exit edge). The (header, after) pair is pushed on the
        loop stack so break/continue can find their targets.

        Returns:
            The `after` block.

        Raises:
            Unsupported: If the loop has an `else` clause.
        """
        if s.orelse:
            raise Unsupported(f"loop else at line {s.lineno}")
        header, body, after = self.new(), self.new(), self.new()
        self.edge(cur, header)
        self.cog += 1 + depth
        self.loop_headers[header.id] = s.lineno
        if isinstance(s, ast.While):
            known, val = const_value(s.test)
            if known and not val:
                self.const_conds.append((s.test.lineno, False))
            self.build_cond(s.test, header, body, after, depth)
        else:
            self.add(header, s, depth)
            self.edge(header, body, "true")
            self.edge(header, after, "false")
        self.loops.append((header, after))
        self.edge(self.build_stmts(s.body, body, depth + 1), header)
        self.loops.pop()
        return after

    def build_with(self, s, cur, depth):
        """Lower `with`: each context expression (and its `as` target) is an ordinary
        statement in the current block, then the body continues in the same flow."""
        for item in s.items:
            if item.optional_vars is not None:
                node = ast.Assign(targets=[item.optional_vars], value=item.context_expr)
            else:
                node = ast.Expr(value=item.context_expr)
            ast.copy_location(node, s)
            node.lineno = getattr(item.context_expr, "lineno", s.lineno)
            self.add(cur, node, depth)
        return self.build_stmts(s.body, cur, depth)

    def build_try(self, s, cur, depth):
        """Lower try/except/else/finally.

        The body starts in a fresh block. Every block created for the body (and the
        block before the try) gets an "except" edge to each handler, since any
        statement may raise. Normal completion goes to `else` (if any), then to
        `finally` (if any) or the join block; handlers end the same way. With a
        `finally`, a second copy of its body is reached by "except" edges from every
        block of the statement (body, handlers, else: an exception may escape from any
        of them) and then leaves through EXIT. A `return`/`break`/`continue`/`raise`
        inside the try, its handlers or its `else` also runs a copy of the `finally`
        body first (see jump()).

        Returns:
            The block after the whole statement.
        """
        start = cur
        body = self.new()
        self.edge(cur, body)
        first_id = body.id
        if s.finalbody:
            self.finals.append((s.finalbody, depth, len(self.loops)))
        if s.handlers:
            self.catch += 1
        body_end = self.build_stmts(s.body, body, depth + 1)
        if s.handlers:
            self.catch -= 1
        sources = [start] + self.blocks[first_id:]
        after = self.new()
        fin = self.new() if s.finalbody else None
        target = fin if fin else after
        if s.orelse:
            els = self.new()
            self.edge(body_end, els)
            self.edge(self.build_stmts(s.orelse, els, depth + 1), target)
        else:
            self.edge(body_end, target)
        for h in s.handlers:
            hb = self.new()
            self.handler_ids.add(hb.id)
            for src in sources:
                self.edge(src, hb, "except")
            self.cog += 1 + depth
            self.add(hb, h, depth + 1)
            self.edge(self.build_stmts(h.body, hb, depth + 1), target)
        if s.finalbody:
            self.finals.pop()                    # jumps inside the finally body itself do not re-run it
        if fin:
            # Every block of the whole statement (body, handlers, else) may raise an
            # exception that is not handled there; it still runs `finally` and then keeps
            # propagating. Model that with a second copy of the finally body ending at
            # EXIT (compilers duplicate `finally` the same way).
            protected = [start] + [b for b in self.blocks[first_id:] if b is not after and b is not fin]
            self.edge(self.build_stmts(s.finalbody, fin, depth), after)
            cog = self.cog
            fin_exc = self.new()
            for src in protected:
                self.edge(src, fin_exc, "except")
            self.edge(self.build_stmts(s.finalbody, fin_exc, depth), self.exit, "raise")
            self.cog = cog                       # the copy must not count twice
        return after

    # -- queries
    def reachable(self):
        """Return the set of block ids reachable from ENTRY (iterative DFS)."""
        seen, stack = set(), [self.entry]
        while stack:
            b = stack.pop()
            if b.id in seen:
                continue
            seen.add(b.id)
            stack.extend(t for t, _ in b.succs)
        return seen

    def preds(self, ids):
        """Map each id in `ids` to the list of predecessor ids, considering only blocks in `ids`."""
        p = {i: [] for i in ids}
        for i in ids:
            for t, _ in self.blocks[i].succs:
                if t.id in p:
                    p[t.id].append(i)
        return p

    def can_exit(self, reach):
        """Return the set of reachable block ids from which EXIT can be reached (reverse DFS)."""
        radj = {}
        for i in reach:
            for t, _ in self.blocks[i].succs:
                radj.setdefault(t.id, []).append(i)
        seen, stack = {self.exit.id}, [self.exit.id]
        while stack:
            for p in radj.get(stack.pop(), []):
                if p not in seen:
                    seen.add(p)
                    stack.append(p)
        return seen & reach

    def validate(self):
        """Check structural invariants of the CFG.

        EXIT has no successors, every other block has at least one, no edge dangles,
        "true"/"false" edges come in pairs, and a constant-condition block has exactly
        one of them.

        Raises:
            AssertionError: If an invariant is violated.
        """
        ids = {b.id for b in self.blocks}
        assert not self.exit.succs, "EXIT must have no successors"
        for b in self.blocks:
            if b is not self.exit:
                assert b.succs, f"B{b.id} has no successor"
            assert all(t.id in ids for t, _ in b.succs), f"B{b.id} has a dangling edge"
            labels = sorted(l for _, l in b.succs if l in ("true", "false"))
            if b.const:
                assert labels in (["true"], ["false"]), f"B{b.id} constant condition needs exactly one edge"
            else:
                assert labels in ([], ["false", "true"]), f"B{b.id} has unpaired true/false edges"

    def cyclomatic(self):
        """Return cyclomatic complexity over the reachable graph.

        E - N + 2, where "except" edges are not counted as edges and each reachable
        exception handler adds 1 (each handler is one extra decision, as in McCabe's
        definition) instead of one edge per statement that may raise.
        """
        r = self.reachable()
        e = sum(1 for i in r for t, l in self.blocks[i].succs if l != "except")
        return e - len(r) + 2 + len(self.handler_ids & r)

    # -- rendering
    def text(self):
        """Render the CFG as plain text, one line per block with its statements and successors."""
        out = []
        for b in self.blocks:
            succ = ", ".join(f"B{t.id}" + (f"[{l}]" if l else "") for t, l in b.succs)
            body = "; ".join(label(s) for s in b.stmts)
            out.append(f"  {self.name(b):<6} {{{body}}} -> {succ or '-'}")
        return "\n".join(out)

    def dot(self):
        """Render the CFG as Graphviz DOT text; unreachable blocks are dashed and red."""
        lines = [f'digraph "{self.fn.name}" {{', "  node [shape=box, fontname=monospace];"]
        r = self.reachable()
        for b in self.blocks:
            txt = "\\l".join([self.name(b)] + [label(s) for s in b.stmts]) + "\\l"
            txt = txt.replace('"', '\\"')
            style = "" if b.id in r else ", style=dashed, color=red"
            lines.append(f'  b{b.id} [label="{txt}"{style}];')
        for b in self.blocks:
            for t, l in b.succs:
                lab = f' [label="{l}"]' if l else ""
                lines.append(f"  b{b.id} -> b{t.id}{lab};")
        lines.append("}")
        return "\n".join(lines)


def label(node):
    """Return a one-line source-like label for a CFG item.

    A `for` header is shown without its body, an except handler as its clause line,
    and a nested function as its signature.
    """
    if isinstance(node, ast.For):
        return f"for {ast.unparse(node.target)} in {ast.unparse(node.iter)}"
    if isinstance(node, ast.ExceptHandler):
        typ = f" {ast.unparse(node.type)}" if node.type else ""
        return f"except{typ}" + (f" as {node.name}" if node.name else "")
    if isinstance(node, ast.FunctionDef):
        return f"def {node.name}({ast.unparse(node.args)})"
    return ast.unparse(node).replace("\n", " ")


# --------------------------------------------------------------------------
# Functions in a source file, call graph
# --------------------------------------------------------------------------
def _walk_defs(stmts, prefix, out):
    """Collect (qualified name, FunctionDef) pairs from `stmts`, recursing into classes,
    function bodies and compound statements."""
    for s in stmts:
        if isinstance(s, ast.FunctionDef):
            q = prefix + s.name
            out.append((q, s))
            _walk_defs(s.body, q + ".", out)
        elif isinstance(s, ast.ClassDef):
            _walk_defs(s.body, prefix + s.name + ".", out)
        else:
            for field in ("body", "orelse", "finalbody"):
                _walk_defs(getattr(s, field, None) or [], prefix, out)
            for h in getattr(s, "handlers", []) or []:
                _walk_defs(h.body, prefix, out)


def collect_functions(tree):
    """Return every analysable function in a module as [(qualified name, FunctionDef)].

    Includes top-level functions, methods (`Class.method`) and nested functions
    (`outer.inner`). Module-level code is added as a pseudo-function `<module>` when
    the file has no functions at all, or when it has control flow (an `if
    __name__ == "__main__":` block, loops, try).
    """
    out = []
    _walk_defs(tree.body, "", out)
    rest = [s for s in tree.body if not isinstance(s, (ast.FunctionDef, ast.ClassDef))]
    flow = any(isinstance(s, (ast.If, ast.While, ast.For, ast.Try, ast.With)) for s in rest)
    if rest and (not out or flow):
        mod = ast.FunctionDef(
            name="<module>", args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[],
                                                kw_defaults=[], defaults=[]),
            body=rest, decorator_list=[], lineno=1, col_offset=0)
        out.append(("<module>", mod))
    return out


def call_graph(funcs):
    """Return {caller: sorted callees} among the analysed functions (matched by simple name).

    A call `f(...)` or `obj.f(...)` is an edge to every analysed function whose own
    name is `f`. Calls inside nested functions are attributed to the nested function.
    """
    by_simple = {}
    for q, fn in funcs:
        by_simple.setdefault(fn.name, []).append(q)
    graph = {}
    for q, fn in funcs:
        called, stack = set(), list(fn.body)
        while stack:
            n = stack.pop()
            if isinstance(n, ast.FunctionDef):
                continue
            if isinstance(n, ast.Call):
                f = n.func
                simple = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
                called.update(by_simple.get(simple, []))
            stack.extend(ast.iter_child_nodes(n))
        graph[q] = sorted(called)
    return graph


# --------------------------------------------------------------------------
# Analyses
# --------------------------------------------------------------------------
COMP = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def _param_names(args):
    """Return the set of parameter names declared by an ast.arguments node."""
    names = {a.arg for a in args.posonlyargs + args.args + args.kwonlyargs}
    names |= {a.arg for a in (args.vararg, args.kwarg) if a}
    return names


def _collect(n, bound, uses, defs):
    """Collect Name nodes under `n` into `uses` (Load/Del) and `defs` (Store).

    Names in `bound` are ignored. Comprehension targets and lambda parameters are
    scoped to the comprehension / lambda, so they are neither uses nor defs of the
    enclosing function. The first iterable of a comprehension is evaluated in the
    enclosing scope.
    """
    if isinstance(n, COMP):
        names = {x.id for g in n.generators for x in ast.walk(g.target) if isinstance(x, ast.Name)}
        inner = bound | names
        for i, g in enumerate(n.generators):
            _collect(g.iter, bound if i == 0 else inner, uses, defs)
            for c in g.ifs:
                _collect(c, inner, uses, defs)
        for part in ((n.key, n.value) if isinstance(n, ast.DictComp) else (n.elt,)):
            _collect(part, inner, uses, defs)
        return
    if isinstance(n, ast.Lambda):
        a = n.args
        for d in a.defaults + [k for k in a.kw_defaults if k is not None]:
            _collect(d, bound, uses, defs)
        _collect(n.body, bound | _param_names(a), uses, defs)
        return
    if isinstance(n, ast.Name):
        if n.id not in bound:
            (defs if isinstance(n.ctx, ast.Store) else uses).append(n)
        return
    for c in ast.iter_child_nodes(n):
        _collect(c, bound, uses, defs)


def _synthetic(name, node, ctx):
    """Create a Name node located at `node`, used for implicit uses/defs."""
    return ast.copy_location(ast.Name(id=name, ctx=ctx), node)


def events(node):
    """Return ordered (kind, name, line, tag) events for one CFG item: uses first, then defs.

    kind is "use" or "def". tag is "assign" for ordinary definitions and "loop" for
    implicit bindings that are never reported as dead stores (for-loop targets,
    `except ... as e`, imports, nested `def` names). An augmented assignment such as
    `x += 1` counts as a use of `x` followed by a def. Compound-statement headers
    (`for`, except handlers, nested functions) only contribute their own header, not
    their body. A nested function counts as a use of every outer name it reads.
    """
    uses, defs, tag = [], [], "assign"
    if isinstance(node, ast.For):
        _collect(node.iter, set(), uses, defs)
        _collect(node.target, set(), uses, defs)
        tag = "loop"
    elif isinstance(node, ast.ExceptHandler):
        if node.type is not None:
            _collect(node.type, set(), uses, defs)
        if node.name:
            defs.append(_synthetic(node.name, node, ast.Store()))
        tag = "loop"
    elif isinstance(node, ast.FunctionDef):
        for d in node.args.defaults + [k for k in node.args.kw_defaults if k is not None] + node.decorator_list:
            _collect(d, set(), uses, defs)
        inner_local = _param_names(node.args) | {
            n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        free = sorted({n.id for n in ast.walk(node) if isinstance(n, ast.Name)
                       and isinstance(n.ctx, ast.Load) and n.id not in inner_local})
        uses += [_synthetic(n, node, ast.Load()) for n in free]
        defs.append(_synthetic(node.name, node, ast.Store()))
        tag = "loop"
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        for a in node.names:
            defs.append(_synthetic((a.asname or a.name).split(".")[0], node, ast.Store()))
        tag = "loop"
    elif isinstance(node, ast.AnnAssign):
        if node.value is not None:
            _collect(node.value, set(), uses, defs)
            _collect(node.target, set(), uses, defs)
    else:
        _collect(node, set(), uses, defs)
        if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            uses.append(node.target)
    return ([("use", n.id, n.lineno, "") for n in uses] +
            [("def", n.id, n.lineno, tag) for n in defs])


def block_events(b):
    """Return the concatenated use/def events of all items in block `b`, in order."""
    return [e for s in b.stmts for e in events(s)]


def witness_path(cfg, ev, var, target):
    """Return the shortest block path ENTRY -> `target` along which `var` is never assigned.

    Used as evidence for an "assigned on some paths only" finding: following this
    path, the variable is still unset when the block `target` is reached.

    Args:
        cfg: The CFG.
        ev: Dict block id -> events, for reachable blocks only.
        var: The variable name.
        target: Block id where the unassigned use occurs.

    Returns:
        List of block ids from ENTRY to `target`, or [] if none is found.
    """
    prev, queue = {cfg.entry.id: None}, deque([cfg.entry.id])
    while queue:
        u = queue.popleft()
        if u == target:
            break
        if any(k == "def" and n == var for k, n, _, _ in ev[u]):
            continue                      # var is assigned in u: this path no longer witnesses the bug
        for t, _ in cfg.blocks[u].succs:
            if t.id in ev and t.id not in prev:
                prev[t.id] = u
                queue.append(t.id)
    if target not in prev:
        return []
    path, x = [], target
    while x is not None:
        path.append(x)
        x = prev[x]
    return path[::-1]


def analyze(fn, src_lines):
    """Build the CFG for `fn` and run all diagnostics on it.

    Runs reachability, constant-condition and infinite-loop checks, definite
    assignment (forward, intersection) and liveness (backward, union), each to a
    fixpoint. Findings are deduplicated and ranked by severity x (1 + cognitive cost),
    where cognitive cost is the nesting depth of the offending line plus, for
    "assigned on some paths only" findings, the length of the witness path a reader
    has to follow. Ties are broken by line, kind and variable.

    Args:
        fn: An ast.FunctionDef node.
        src_lines: Source text split into lines, used to quote the offending line.

    Returns:
        (cfg, findings) where findings is a ranked list of dicts with keys kind,
        line, var, block, depth, cost, rank, source, path (block names, for uninit)
        and value (for constant_condition).

    Raises:
        Unsupported: If the function uses an unsupported construct.
    """
    cfg = CFG(fn)
    cfg.validate()
    reach = cfg.reachable()
    order = sorted(reach)
    preds = cfg.preds(order)
    ev = {i: block_events(cfg.blocks[i]) for i in order}

    params = _param_names(fn.args)
    # Python scoping is lexical: a name assigned anywhere in the function body (even in
    # unreachable code) is local, so reading it before assignment is an error.
    local = set(params) | {n for b in cfg.blocks for k, n, _, _ in block_events(b) if k == "def"}

    findings = []

    def add(kind, line, var, bid, path=None, value=None):
        """Record one finding, computing its depth, cognitive cost and rank."""
        depth = cfg.depth.get(line, 0)
        cost = depth + (len(path) - 1 if path else 0)
        findings.append({
            "kind": kind, "line": line, "var": var, "block": bid, "depth": depth, "cost": cost,
            "rank": SEVERITY[kind] * (1 + cost),
            "source": src_lines[line - 1].strip() if 0 < line <= len(src_lines) else "",
            "path": [cfg.name(cfg.blocks[p]) for p in path] if path else [],
            "value": value,
        })

    # 1. unreachable code (a line also present in a reachable block, e.g. one of the
    #    copies of a finally body, is not dead)
    live_lines = {s.lineno for i in order for s in cfg.blocks[i].stmts}
    for b in cfg.blocks:
        if b.id not in reach and b.stmts and b.stmts[0].lineno not in live_lines:
            add("unreachable", b.stmts[0].lineno, None, b.id)

    # 2. constant conditions and loops that can never exit
    for line, val in cfg.const_conds:
        add("constant_condition", line, None, 0, value=val)
    trapped = reach - cfg.can_exit(reach)
    for hid, line in cfg.loop_headers.items():
        if hid in trapped:
            add("infinite_loop", line, None, hid)

    # 3. definite assignment (forward, intersection)
    out = {i: set(local) for i in order}
    out[cfg.entry.id] = set(params)

    def forward(i, report):
        """Transfer function of block `i` for definite assignment.

        Returns the set of variables definitely assigned at block exit. When `report`
        is true, also records a finding (with its witness path) for each use that is
        not definitely assigned.
        """
        cur = set(params) if i == cfg.entry.id else (
            set.intersection(*[out[p] for p in preds[i]]) if preds[i] else set())
        for kind, name, line, _ in ev[i]:
            if kind == "use" and name in local and name not in cur and report:
                add("uninit", line, name, i, path=witness_path(cfg, ev, name, i))
            elif kind == "def":
                cur.add(name)
        return cur

    changed = True
    while changed:
        changed = False
        for i in order:
            new = forward(i, False)
            if new != out[i]:
                out[i], changed = new, True
    for i in order:
        forward(i, True)

    # 4. liveness (backward, union). A source line can appear in several blocks (copies of
    #    a finally body), so a store is reported dead only if it is dead in every copy.
    live_in = {i: set() for i in order}
    live_defs, dead_defs = set(), {}

    def live_out(i):
        """Return the variables live at the exit of block `i` (union over successors' live-in)."""
        s = set()
        for t, _ in cfg.blocks[i].succs:
            s |= live_in[t.id]
        return s

    def backward(i, report):
        """Transfer function of block `i` for liveness.

        Returns the set of variables live at block entry. When `report` is true, also
        records a dead-store finding for each ordinary assignment whose value is not
        live afterwards (names starting with "_" are ignored).
        """
        live = live_out(i)
        for kind, name, line, tag in reversed(ev[i]):
            if kind == "def":
                if report and tag == "assign" and not name.startswith("_"):
                    if name in live:
                        live_defs.add((line, name))
                    else:
                        dead_defs.setdefault((line, name), i)
                live.discard(name)
            else:
                live.add(name)
        return live

    changed = True
    while changed:
        changed = False
        for i in reversed(order):
            new = backward(i, False)
            if new != live_in[i]:
                live_in[i], changed = new, True
    for i in order:
        backward(i, True)
    for (line, name), bid in dead_defs.items():
        if (line, name) not in live_defs:
            add("dead_store", line, name, bid)

    # dedupe + deterministic ranking
    uniq = {(f["kind"], f["line"], f["var"]): f for f in findings}
    ranked = sorted(uniq.values(), key=lambda f: (-f["rank"], f["line"], f["kind"], f["var"] or ""))
    return cfg, ranked


def analyze_source(src):
    """Analyse every function in a source text.

    Returns:
        (results, graph) where results is a list of dicts with keys name, fn, cfg,
        findings, error (None, or the Unsupported message if the function was
        skipped), and graph is the call graph from call_graph().

    Raises:
        SyntaxError: If `src` does not parse.
    """
    tree = ast.parse(src)
    lines = src.splitlines()
    funcs = collect_functions(tree)
    results = []
    for q, fn in funcs:
        try:
            cfg, findings = analyze(fn, lines)
            results.append({"name": q, "fn": fn, "cfg": cfg, "findings": findings, "error": None})
        except Unsupported as e:
            results.append({"name": q, "fn": fn, "cfg": None, "findings": [], "error": str(e)})
    return results, call_graph(funcs)


def summary_text(results, graph):
    """Return a plain-text table of all analysed functions plus the call graph."""
    rows = [f"  {'function':<28}{'blocks':>7}{'cyclo':>7}{'cogn':>6}{'findings':>9}"]
    for r in results:
        if r["cfg"]:
            c = r["cfg"]
            rows.append(f"  {r['name']:<28}{len(c.reachable()):>7}{c.cyclomatic():>7}{c.cog:>6}{len(r['findings']):>9}")
        else:
            rows.append(f"  {r['name']:<28}  skipped: {r['error']}")
    edges = [f"  {a} -> {b}" + ("  (recursive)" if a == b else "") for a in graph for b in graph[a]]
    return "Summary:\n" + "\n".join(rows) + "\nCall graph:\n" + ("\n".join(edges) if edges else "  (no calls between analysed functions)")


# --------------------------------------------------------------------------
# Explanations: template, LLM, grounding check
# --------------------------------------------------------------------------
def template(f):
    """Return the deterministic fallback explanation sentence for a finding."""
    v, ln, kind = f["var"], f["line"], f["kind"]
    if kind == "uninit":
        via = f" (for example via {' -> '.join(f['path'])})" if f.get("path") else ""
        return f"'{v}' may be used at line {ln} before it is assigned: some path reaches it without assigning '{v}'{via}."
    if kind == "unreachable":
        return f"Code at line {ln} can never execute (block B{f['block']} is unreachable from entry)."
    if kind == "infinite_loop":
        return f"The loop at line {ln} can never exit: no path from it reaches the end of the function (no reachable break or return)."
    if kind == "constant_condition":
        return f"The condition at line {ln} is always {'true' if f.get('value') else 'false'}, so one branch can never run."
    return f"'{v}' is assigned at line {ln} but the value is never used afterwards."


def grounded(reply, f):
    """Deterministic grounding check for an LLM reply.

    Every integer in the reply's prose (text outside backticks) must be the finding's
    line, its block id, a block id on its evidence path, or a number in the quoted source line. Every `backticked` bare
    identifier must be the finding's variable, appear in the source line, be a block name from
    its evidence path (ENTRY, B4, ...) or be a Python keyword/builtin; a backticked path such as
    `ENTRY -> B2` must consist of known names only; in a backticked fix snippet such as
    `x = 0` only the first name is checked.

    Returns:
        True if the reply only cites facts present in the finding, else False.
    """
    ok_ints = ({f["line"], f["block"]} | {int(x) for x in re.findall(r"\b\d+\b", f["source"])}
               | {int(x) for p in f.get("path", []) for x in re.findall(r"\d+", p)})
    prose = re.sub(r"`[^`]*`", " ", reply)   # numbers inside suggested code (e.g. `x = 0`) are not claims
    if any(int(x) not in ok_ints for x in re.findall(r"\b\d+\b", prose)):
        return False
    ok_names = (set(re.findall(r"[A-Za-z_]\w*", f["source"])) | ({f["var"]} if f["var"] else set())
                | set(keyword.kwlist) | set(dir(builtins)) | set(f.get("path", [])) | {f"B{f['block']}"}
                | ({"true", "false"} if f.get("value") is not None else set()))
    for tok in re.findall(r"`([^`]+)`", reply):
        names = re.findall(r"[A-Za-z_]\w*", re.sub(r"(['\"]).*?\1", "", tok))   # drop string literals
        if re.fullmatch(r"\s*[A-Za-z_]\w*\s*", tok) or "->" in tok or "→" in tok:
            check = names                    # bare identifier or a path `A -> B`: every name must be known
        else:
            check = names[:1]                # fix snippet like `x = 0`: only its first name is a claim
        if any(n not in ok_names for n in check):
            return False
    return True


def llm_provider():
    """Pick which LLM provider to use from the environment.

    LLM_PROVIDER ("groq" or "anthropic") forces a choice; otherwise Groq is used if
    GROQ_API_KEY is set, else Anthropic if ANTHROPIC_API_KEY is set.

    Returns:
        "groq", "anthropic", or None if no usable API key is configured.
    """
    forced = os.environ.get("LLM_PROVIDER", "").lower()
    names = [forced] if forced in PROVIDERS else list(PROVIDERS)
    for name in names:
        if os.environ.get(PROVIDERS[name]["key_env"]):
            return name
    return None


def llm_model(name):
    """Return the model id for provider `name` (overridable via GROQ_MODEL / ANTHROPIC_MODEL)."""
    cfg = PROVIDERS[name]
    return os.environ.get(cfg["model_env"], cfg["model"])


def llm_call(prompt):
    """Send a prompt to the configured LLM provider (temperature 0) and return the reply text.

    Supports Groq (OpenAI-compatible chat completions) and the Anthropic Messages API.
    Replies are cached on disk keyed by a hash of provider + model + prompt, so
    repeated runs are identical and offline-reproducible. On failure the reason is
    stored in LAST_LLM_ERROR.

    Returns:
        The reply text, or None if no API key is set or the request fails.
    """
    global LAST_LLM_ERROR
    LAST_LLM_ERROR = ""
    name = llm_provider()
    if not name:
        LAST_LLM_ERROR = "no API key set (GROQ_API_KEY or ANTHROPIC_API_KEY)"
        return None
    cfg, model = PROVIDERS[name], llm_model(name)
    key = os.environ[cfg["key_env"]]
    h = hashlib.sha256(f"{name}|{model}|{prompt}".encode()).hexdigest()
    try:
        with open(CACHE_FILE) as fh:
            cache = json.load(fh)
    except (OSError, ValueError):
        cache = {}
    if h in cache:
        return cache[h]
    body = {"model": model, "max_tokens": 800, "temperature": 0,
            "messages": [{"role": "user", "content": prompt}]}
    headers = {"content-type": "application/json", "user-agent": "cfgtool/1.0"}
    if name == "groq":
        headers["authorization"] = "Bearer " + key
    else:
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
    req = urllib.request.Request(cfg["url"], data=json.dumps(body).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
        text = (data["choices"][0]["message"]["content"] if name == "groq"
                else data["content"][0]["text"]).strip()
    except Exception as e:
        LAST_LLM_ERROR = f"{name} request failed: {e}"
        return None
    cache[h] = text
    try:
        with open(CACHE_FILE, "w") as fh:
            json.dump(cache, fh, indent=1, sort_keys=True)
    except OSError:
        pass
    return text


def explain(f, use_llm, client=llm_call):
    """Explain a finding in plain language.

    The LLM reply has its whitespace normalised to single spaces and must pass
    grounded(); a missing, failed or ungrounded reply falls back to template().

    Args:
        f: A finding dict from analyze().
        use_llm: Whether to try the LLM first.
        client: Callable prompt -> reply text or None (injectable for tests).

    Returns:
        (text, origin) where origin is "llm" if the reply passed the grounding check,
        otherwise "template" with the deterministic fallback sentence.
    """
    if use_llm:
        prompt = ("A compiler analysis produced this finding about a Python function:\n"
                  + json.dumps({k: f[k] for k in ("kind", "line", "var", "source", "path", "value") if f.get(k) not in (None, [])})
                  + "\nExplain it in 2 sentences and suggest a fix. Use ONLY facts from the finding. "
                    "Wrap any code identifier in backticks. Mention only the line number and block names given.")
        reply = client(prompt)
        reply = " ".join(reply.split()) if reply else reply   # normalise odd spaces/newlines
        if reply and grounded(reply, f):
            return reply, "llm"
    return template(f), "template"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def report(r, use_llm, show_dot):
    """Print one function's CFG, metrics, ranked findings (with explanations) and optional DOT.

    Args:
        r: A result dict from analyze_source() (name, fn, cfg, findings, error).
        use_llm: Whether to ask the LLM to explain findings.
        show_dot: Whether to also print Graphviz DOT.
    """
    fn = r["fn"]
    if r["error"]:
        print(f"== function {r['name']} == skipped: {r['error']}\n")
        return
    cfg, findings = r["cfg"], r["findings"]
    print(f"== function {r['name']} (line {fn.lineno}) ==")
    print("CFG:")
    print(cfg.text())
    print(f"Cyclomatic complexity: {cfg.cyclomatic()}   Cognitive complexity: {cfg.cog}")
    print(f"Findings ({len(findings)}), ranked by severity x (1 + cognitive cost):")
    for f in findings:
        text, origin = explain(f, use_llm)
        print(f"  [rank {f['rank']}] {f['kind']} @ line {f['line']} (cost {f['cost']}): {text}  <{origin}>")
        if f["path"]:
            print(f"      evidence path: {' -> '.join(f['path'])}")
    if use_llm and LAST_LLM_ERROR:
        print(f"  note: LLM unavailable, templates used ({LAST_LLM_ERROR})")
    if show_dot:
        print(cfg.dot())
    print()


def main(argv=None):
    """Command-line entry point.

    Reads the file as UTF-8 (a leading byte-order mark is accepted) and analyses every
    function in it (top-level, methods, nested, plus module-level code), or only those
    matching --function (qualified or simple name). A function that uses an unsupported
    construct is skipped with a message. With more than one function a summary table and
    call graph are printed at the end.

    Args:
        argv: Argument list (defaults to sys.argv[1:]).

    Returns:
        Process exit code: 0 on success, 1 if no matching function was found, 2 if the
        file has a syntax error.
    """
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("file", nargs="?")
    ap.add_argument("--function", help="analyse only this function (qualified or simple name)")
    ap.add_argument("--dot", action="store_true", help="also print Graphviz DOT")
    ap.add_argument("--llm", action="store_true", help="explain findings with an LLM (needs GROQ_API_KEY or ANTHROPIC_API_KEY)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if not a.file:
        ap.error("FILE required")
    with open(a.file, encoding="utf-8-sig") as fh:
        src = fh.read()
    try:
        results, graph = analyze_source(src)
    except SyntaxError as e:
        print(f"syntax error in {a.file}: {e.msg} (line {e.lineno})", file=sys.stderr)
        return 2
    chosen = [r for r in results if not a.function or a.function in (r["name"], r["fn"].name)]
    if not chosen:
        print("no matching function found", file=sys.stderr)
        return 1
    for r in chosen:
        report(r, a.llm, a.dot)
    if len(chosen) > 1:
        print(summary_text(chosen, graph))
    return 0


# --------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------
BUGGY = '''def buggy(a, flag):
    unused = a * 2
    if flag:
        result = a + 1
    total = result + 1
    return total
    print("never")
'''
LOOPY = '''def f(items):
    count = 0
    for x in items:
        if x == 0:
            continue
        if x < 0:
            break
        count += 1
    i = 0
    while i < 10:
        i += 1
    return count
'''


def selftest():
    """Run built-in checks: expected findings, metrics, determinism, rejection of
    unsupported code, and the LLM grounding check (wrong lines, invented names, allowed
    fix snippets) and template fallback. No network access is used.

    Returns:
        0 if all checks pass.

    Raises:
        AssertionError: On the first failing check.
    """
    def run(src):
        """Parse `src` (one function) and return analyze()'s (cfg, findings)."""
        fn = ast.parse(src).body[0]
        return analyze(fn, src.splitlines())

    cfg, fs = run(BUGGY)
    got = {(f["kind"], f["line"]) for f in fs}
    assert got == {("dead_store", 2), ("uninit", 5), ("unreachable", 7)}, got
    assert [f["kind"] for f in fs][0] == "uninit", "uninit should rank first"

    cfg, fs = run(LOOPY)
    assert fs == [], fs
    assert cfg.cyclomatic() == 5, cfg.cyclomatic()      # 4 decision points (for, if, if, while) + 1
    assert cfg.cog == 1 + (1 + 1) + (1 + 1) + 1, cfg.cog

    for src in (BUGGY, LOOPY):                           # determinism
        assert run(src)[0].text() == run(src)[0].text()
        assert run(src)[0].dot() == run(src)[0].dot()

    try:                                                 # unsupported constructs rejected
        run("def g(x):\n    while x:\n        x -= 1\n    else:\n        pass\n")
        raise AssertionError("expected Unsupported")
    except Unsupported:
        pass

    f = [x for x in run(BUGGY)[1] if x["kind"] == "uninit"][0]
    assert not grounded("`result` is used at line 99.", f)           # wrong line
    assert not grounded("`ghost` is used at line 5.", f)             # invented identifier
    assert grounded("`result` is used at line 5; assign it first.", f)
    assert grounded("Initialise it, e.g. `result = 0`, before line 5.", f)   # fix-snippet literals are fine
    assert not grounded("Initialise `result = 0` before line 12.", f)        # wrong line in prose still rejected
    assert grounded("Set `result = None` first.", f)                         # keywords allowed in fix snippets
    assert grounded('Use `result = "x"` first.', f)                          # string literals ignored
    assert not grounded("Set `ghost = 0` first.", f)                         # invented variable still rejected
    text, origin = explain(f, True, client=lambda p: "`ghost` at line 42")
    assert origin == "template", origin                              # hallucination -> fallback
    text, origin = explain(f, True, client=lambda p: "`result` at line 5 may be unset.")
    assert origin == "llm", origin
    # constant conditions, infinite loops, short-circuit, try/with/comprehensions, multi-function
    _, fs = run("def f(n):\n    while True:\n        n += 1\n    print(n)\n")
    assert {(x["kind"], x["line"]) for x in fs} == {("infinite_loop", 2), ("unreachable", 4)}, fs
    cfg, fs = run("def f(a, b):\n    if a and b:\n        return 1\n    return 0\n")
    assert sum(1 for blk in cfg.blocks if blk.const is False and len(blk.succs) == 2) == 2   # one block per operand
    _, fs = run("def f(p):\n    try:\n        d = open(p).read()\n    except OSError:\n        pass\n    return d\n")
    assert [(x["kind"], x["line"]) for x in fs] == [("uninit", 6)] and fs[0]["path"][0] == "ENTRY"
    _, fs = run("def f(items):\n    sq = [x * x for x in items]\n    with open('p') as fh:\n        t = fh.read()\n    return sq, t\n")
    assert fs == []
    results, graph = analyze_source("def a():\n    return b()\n\ndef b():\n    return 1\n")
    assert graph == {"a": ["b"], "b": []} and [r["name"] for r in results] == ["a", "b"]
    print("selftest: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

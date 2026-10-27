"""Tkinter GUI for cfgtool: type or load Python code, see its CFG drawn, and browse ranked findings.

Usage:
    python gui.py

Editor (left): syntax highlighting, line numbers, adjustable font, optional live
re-analysis while you type. Graph (right): zoom (Ctrl+wheel, +/-, Fit), pan (drag),
tooltips on blocks, click a block to see its source lines, click an editor line to see
its blocks, dark/light theme. Findings table: colour-coded by kind, sortable; selecting
a finding highlights its source line, its block and (for "possibly unassigned") the
evidence path. The Function dropdown lists every function, method, nested function and
module-level code; Summary shows all of them with the call graph; Export saves PNG, SVG
or DOT. Tick "Explain with LLM" (needs GROQ_API_KEY or ANTHROPIC_API_KEY) to replace
template explanations with grounding-checked LLM ones.

Shortcuts: F5 / Ctrl+Enter analyze, Ctrl+O open, Ctrl+S save, Ctrl+E export,
Ctrl+= / Ctrl+- zoom, Ctrl+0 fit, Ctrl+wheel zoom the graph.
"""
import builtins
import glob
import keyword
import os
import queue
import re
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

import cfgdraw
import cfgtool

sys.setrecursionlimit(5000)
HERE = os.path.dirname(os.path.abspath(__file__))

# Per-theme colours for the Tk widgets (the graph uses cfgdraw.PALETTES).
THEMES = {
    "light": {"bg": "#f3f3f3", "fg": "#1e1e1e", "field": "#ffffff", "editor": "#ffffff", "gutter": "#ececec",
              "gutter_fg": "#8a8a8a", "select": "#cfe3ff", "accent": "#0b63ce",
              "finding_bg": "#ffe3e3", "selected_bg": "#fff2a8", "blocklines_bg": "#d6ecff",
              "kw": "#0000ff", "str": "#a31515", "com": "#008000", "num": "#098658", "def": "#795e26",
              "builtin": "#267f99",
              "kinds": {"infinite_loop": "#b71c1c", "uninit": "#c62828", "unreachable": "#e65100",
                        "constant_condition": "#6a1b9a", "dead_store": "#455a64"}},
    "dark": {"bg": "#252526", "fg": "#d4d4d4", "field": "#1e1e1e", "editor": "#1e1e1e", "gutter": "#252526",
             "gutter_fg": "#858585", "select": "#264f78", "accent": "#4fc1ff",
             "finding_bg": "#4a2222", "selected_bg": "#5c5200", "blocklines_bg": "#1f3a5f",
             "kw": "#569cd6", "str": "#ce9178", "com": "#6a9955", "num": "#b5cea8", "def": "#dcdcaa",
             "builtin": "#4ec9b0",
             "kinds": {"infinite_loop": "#ff8a80", "uninit": "#ff6b6b", "unreachable": "#ffb74d",
                       "constant_condition": "#ce93d8", "dead_store": "#90a4ae"}},
}
MIN_ZOOM, MAX_ZOOM = 0.25, 3.0
TOKEN_RE = re.compile(
    r"(?P<com>#[^\n]*)"
    r"|(?P<str>'''.*?'''|\"\"\".*?\"\"\"|'(?:\\.|[^'\\\n])*'|\"(?:\\.|[^\"\\\n])*\")"
    r"|(?P<num>\b\d+(?:\.\d+)?\b)"
    r"|(?P<defn>\b(?:def|class)\s+[A-Za-z_]\w*)"
    r"|(?P<word>\b[A-Za-z_]\w*\b)", re.S)


def block_id(name):
    """Convert a block display name (ENTRY, EXIT, B<n>) back to its integer id."""
    return 0 if name == "ENTRY" else 1 if name == "EXIT" else int(name[1:])


class Tooltip:
    """A small borderless window that follows the mouse and shows text."""

    def __init__(self, widget):
        """Create a tooltip for `widget` (hidden until show())."""
        self.widget = widget
        self.win = None

    def show(self, text, x, y):
        """Show `text` near screen position (x, y), creating the window on first use."""
        if self.win is None:
            self.win = tk.Toplevel(self.widget)
            self.win.wm_overrideredirect(True)
            self.label = tk.Label(self.win, justify="left", background="#ffffe1", foreground="#000000",
                                  relief="solid", borderwidth=1, font=("Consolas", 9), padx=6, pady=3)
            self.label.pack()
        self.label.configure(text=text)
        self.win.wm_geometry(f"+{x + 14}+{y + 12}")

    def hide(self):
        """Hide the tooltip."""
        if self.win is not None:
            self.win.destroy()
            self.win = None


class GraphView(tk.Canvas):
    """Canvas that draws the primitives produced by cfgdraw.layout, with zoom, pan and clicks."""

    def __init__(self, master, on_block_click=None, tip_text=None):
        """Create the canvas.

        Args:
            master: Parent widget.
            on_block_click: Callback(block id) when a block is clicked.
            tip_text: Function block id -> tooltip text.
        """
        super().__init__(master, bg="white", highlightthickness=0)
        self.base_size = 9
        self.font = tkfont.Font(family="Consolas", size=self.base_size)
        self.label_font = tkfont.Font(family="Segoe UI", size=8)
        self.rects = {}
        self.pos = {}
        self.lay = None
        self.cfg = None
        self.bad = set()
        self.scale = 1.0
        self.palette = cfgdraw.LIGHT
        self.base_style = {}
        self.on_block_click = on_block_click
        self.tip_text = tip_text
        self.tip = Tooltip(self)
        self._moved = False
        self.tag_bind("block", "<ButtonRelease-1>", self._click)
        self.tag_bind("block", "<Enter>", self._hover)
        self.tag_bind("block", "<Motion>", self._hover)
        self.tag_bind("block", "<Leave>", lambda e: self.tip.hide())
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<MouseWheel>", self._wheel)
        self.bind("<Button-4>", lambda e: self._wheel(e, 120))
        self.bind("<Button-5>", lambda e: self._wheel(e, -120))

    # ---- drawing
    def draw(self, cfg, bad_blocks, palette=None, keep_view=False):
        """Lay out and draw `cfg` (at the current zoom).

        Args:
            cfg: A cfgtool.CFG.
            bad_blocks: Set of block ids that have findings (drawn with a red outline).
            palette: cfgdraw palette; keeps the current one if omitted.
            keep_view: Keep the scroll position (used when only the theme changes).
        """
        self.cfg, self.bad = cfg, set(bad_blocks)
        if palette is not None:
            self.palette = palette
        self.lay = cfgdraw.layout(cfg, self.bad, self.font.measure, self.font.metrics("linespace"), self.palette)
        self.pos = self.lay.pos
        self.render(keep_view)

    def render(self, keep_view=False):
        """(Re)draw the stored layout at the current zoom level."""
        view = (self.xview()[0], self.yview()[0])
        self.delete("all")
        self.configure(bg=self.palette["bg"])
        s = self.scale
        mono = tkfont.Font(family="Consolas", size=max(5, round(self.base_size * s)))
        small = tkfont.Font(family="Segoe UI", size=max(5, round(8 * s)))
        self._fonts = (mono, small)                  # keep references alive
        self.rects = {}
        for p in self.lay.prims:
            t = p["t"]
            if t == "line":
                self.create_line(*[v * s for v in p["pts"]], fill=p["color"], dash=(4, 3) if p["dash"] else None)
            elif t == "poly":
                self.create_polygon(*[v * s for v in p["pts"]], fill=p["fill"], outline=p["fill"])
            elif t == "rect":
                bid = p["bid"]
                self.rects[bid] = self.create_rectangle(
                    p["x0"] * s, p["y0"] * s, p["x1"] * s, p["y1"] * s, fill=p["fill"], outline=p["outline"],
                    dash=(4, 3) if p["dash"] else None, width=p["width"], tags=("block", f"blk{bid}"))
            else:
                tags = ()
                if p["mono"]:
                    tags = ("block", f"blk{self._bid_for_text(p)}")
                self.create_text(p["x"] * s, p["y"] * s, text=p["text"], fill=p["color"], anchor=p["anchor"],
                                 font=mono if p["mono"] else small, tags=tags)
        self.base_style = {bid: (self.itemcget(r, "outline"), self.itemcget(r, "width"))
                           for bid, r in self.rects.items()}
        self.configure(scrollregion=(0, 0, self.lay.width * s, self.lay.height * s))
        if keep_view:
            self.xview_moveto(view[0])
            self.yview_moveto(view[1])

    def _bid_for_text(self, p):
        """Block id of a block-text primitive: the block whose box contains its top-left corner."""
        for bid, (cx, cy, w, h) in self.pos.items():
            if cx - w / 2 <= p["x"] <= cx + w / 2 and cy - h / 2 <= p["y"] <= cy + h / 2:
                return bid
        return -1

    # ---- zoom / pan
    def set_zoom(self, scale, focus=None, force=False):
        """Set the zoom factor (clamped) and redraw, keeping the point under `focus` (x, y in widget px) fixed.

        Does nothing if the factor is unchanged, unless `force` is true.
        """
        scale = max(MIN_ZOOM, min(MAX_ZOOM, scale))
        if self.lay is None or (not force and abs(scale - self.scale) < 1e-6):
            return
        fx, fy = focus if focus else (self.winfo_width() / 2, self.winfo_height() / 2)
        cx, cy = self.canvasx(fx), self.canvasy(fy)
        ratio = scale / self.scale
        self.scale = scale
        self.render()
        w, h = self.lay.width * scale, self.lay.height * scale
        self.xview_moveto(max(0, (cx * ratio - fx) / w))
        self.yview_moveto(max(0, (cy * ratio - fy) / h))

    def zoom(self, factor, focus=None):
        """Multiply the zoom by `factor`."""
        self.set_zoom(self.scale * factor, focus)

    def fit(self):
        """Zoom so the whole graph fits in the window (never above 100%)."""
        if self.lay is None:
            return
        w, h = max(self.winfo_width(), 200), max(self.winfo_height(), 200)
        target = min(w / self.lay.width, h / self.lay.height, 1.0)
        self.set_zoom(max(MIN_ZOOM, target), force=True)
        self.xview_moveto(0)
        self.yview_moveto(0)

    def _wheel(self, e, delta=None):
        """Mouse wheel: Ctrl zooms, Shift scrolls sideways, otherwise scrolls vertically."""
        d = delta if delta is not None else e.delta
        if e.state & 0x4:                              # Ctrl
            self.zoom(1.1 if d > 0 else 1 / 1.1, (e.x, e.y))
        elif e.state & 0x1:                            # Shift
            self.xview_scroll(-1 if d > 0 else 1, "units")
        else:
            self.yview_scroll(-1 if d > 0 else 1, "units")
        return "break"

    def _press(self, e):
        """Start a drag-pan."""
        self.scan_mark(e.x, e.y)
        self._moved = False
        self.focus_set()

    def _drag(self, e):
        """Pan while the left button is held."""
        self._moved = True
        self.scan_dragto(e.x, e.y, gain=1)

    # ---- block interaction
    def _bid_at_current(self):
        """Block id of the canvas item under the mouse, or None."""
        for t in self.gettags("current"):
            if t.startswith("blk"):
                return int(t[3:])
        return None

    def _click(self, _e):
        """Report a click (not a drag) on a block."""
        bid = self._bid_at_current()
        if bid is not None and bid >= 0 and not self._moved and self.on_block_click:
            self.on_block_click(bid)

    def _hover(self, e):
        """Show the tooltip for the block under the mouse."""
        bid = self._bid_at_current()
        if bid is not None and bid >= 0 and self.tip_text:
            self.tip.show(self.tip_text(bid), e.x_root, e.y_root)

    def highlight(self, bid=None, path_ids=(), extra=()):
        """Emphasise blocks: `bid` thick orange, evidence `path_ids` orange, `extra` cyan.

        Also scrolls `bid` into view.
        """
        for b, r in self.rects.items():
            outline, width = self.base_style[b]
            self.itemconfigure(r, outline=outline, width=width)
        for p in extra:
            if p in self.rects:
                self.itemconfigure(self.rects[p], outline="#00acc1", width=3)
        for p in path_ids:
            if p in self.rects:
                self.itemconfigure(self.rects[p], outline="#fb8c00", width=2)
        if bid in self.rects:
            self.itemconfigure(self.rects[bid], outline="#ef6c00", width=3)
            region = [float(v) for v in str(self.cget("scrollregion")).split()]
            cx, cy, _, _ = self.pos[bid]
            self.xview_moveto(max(0, (cx * self.scale - self.winfo_width() / 2) / max(region[2], 1)))
            self.yview_moveto(max(0, (cy * self.scale - self.winfo_height() / 2) / max(region[3], 1)))


class App:
    """Main window: editor, controls, graph view and findings table."""

    def __init__(self, root):
        """Build all widgets and analyse the sample program."""
        self.root = root
        root.title("CFG Tool: AST to CFG with semantic diagnostics")
        root.geometry("1380x840")
        self.theme_name = "light"
        self.cur = None           # current result dict
        self.results = []
        self.graph_calls = {}
        self.findings = []
        self.expl = []
        self.gen = 0
        self.q = queue.Queue()
        self._hl_job = self._live_job = None
        self._sort = ("rank", True)
        self.editor_font = tkfont.Font(family="Consolas", size=11)
        self._build_ui()
        self.apply_theme("light")
        self.text.insert("1.0", cfgtool.BUGGY)
        self._on_edit()
        self.analyze()
        root.after(100, self._poll)

    # ---- UI construction
    def _build_ui(self):
        """Create and lay out every widget."""
        r = self.root
        self.style = ttk.Style(r)
        self.style.theme_use("clam")
        top = ttk.Frame(r, padding=(8, 6))
        top.pack(fill="x")
        ttk.Button(top, text="Analyze (F5)", command=self.analyze).pack(side="left")
        ttk.Button(top, text="Open...", command=self.open_file).pack(side="left", padx=(6, 0))
        ttk.Button(top, text="Save...", command=self.save_file).pack(side="left", padx=(6, 0))
        ttk.Label(top, text="Example:").pack(side="left", padx=(14, 2))
        self.examples = {os.path.basename(p): p for p in sorted(glob.glob(os.path.join(HERE, "examples", "*.py")))}
        self.ex_box = ttk.Combobox(top, values=list(self.examples), state="readonly", width=22)
        self.ex_box.pack(side="left")
        self.ex_box.bind("<<ComboboxSelected>>", self.load_example)
        ttk.Label(top, text="Function:").pack(side="left", padx=(14, 2))
        self.fn_box = ttk.Combobox(top, state="readonly", width=24)
        self.fn_box.pack(side="left")
        self.fn_box.bind("<<ComboboxSelected>>", lambda e: self.show_selected())
        ttk.Button(top, text="Summary", command=self.show_summary).pack(side="left", padx=(8, 0))
        self.use_llm = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Explain with LLM", variable=self.use_llm,
                        command=self._explain_all).pack(side="left", padx=(14, 0))
        self.theme_btn = ttk.Button(top, text="Dark mode", command=self.toggle_theme)
        self.theme_btn.pack(side="right")
        ttk.Button(top, text="Export graph...", command=self.export_graph).pack(side="right", padx=(0, 6))
        ttk.Button(top, text="Copy DOT", command=self.copy_dot).pack(side="right", padx=(0, 6))

        self.status = tk.StringVar()
        self.statusbar = ttk.Label(r, textvariable=self.status, padding=(8, 3), anchor="w")
        self.statusbar.pack(fill="x", side="bottom")

        main = ttk.PanedWindow(r, orient="horizontal")
        main.pack(fill="both", expand=True, padx=6, pady=(0, 4))

        # -- editor side
        left = ttk.Frame(main)
        main.add(left, weight=2)
        ebar = ttk.Frame(left)
        ebar.pack(fill="x")
        ttk.Label(ebar, text="Code", font=("Segoe UI", 10, "bold")).pack(side="left")
        ttk.Button(ebar, text="A+", width=3, command=lambda: self.change_font(1)).pack(side="right")
        ttk.Button(ebar, text="A-", width=3, command=lambda: self.change_font(-1)).pack(side="right", padx=(0, 4))
        self.live = tk.BooleanVar(value=False)
        ttk.Checkbutton(ebar, text="Live analysis", variable=self.live).pack(side="right", padx=(0, 10))
        body = ttk.Frame(left)
        body.pack(fill="both", expand=True)
        self.lines = tk.Text(body, width=4, state="disabled", bd=0, font=self.editor_font, padx=4, takefocus=0,
                             cursor="arrow")
        self.lines.pack(side="left", fill="y")
        sb = ttk.Scrollbar(body, orient="vertical")
        sb.pack(side="right", fill="y")

        def yscroll(*a):
            """Keep the scrollbar and the line-number gutter in sync with the editor."""
            sb.set(*a)
            self.lines.yview_moveto(a[0])

        self.text = tk.Text(body, wrap="none", undo=True, font=self.editor_font, yscrollcommand=yscroll,
                            insertwidth=2)
        self.text.pack(side="left", fill="both", expand=True)
        sb.configure(command=self.text.yview)
        self.text.bind("<KeyRelease>", self._on_edit)
        self.text.bind("<ButtonRelease-1>", lambda e: self.root.after(10, self._cursor_changed))
        self.text.bind("<Control-MouseWheel>", lambda e: (self.change_font(1 if e.delta > 0 else -1), "break")[1])

        # -- right side
        right = ttk.PanedWindow(main, orient="vertical")
        main.add(right, weight=3)
        gframe = ttk.Frame(right)
        right.add(gframe, weight=3)
        gbar = ttk.Frame(gframe)
        gbar.pack(fill="x")
        self.metrics = tk.StringVar()
        ttk.Label(gbar, textvariable=self.metrics, padding=(4, 2), font=("Segoe UI", 10, "bold")).pack(side="left")
        ttk.Button(gbar, text="Fit", width=4, command=lambda: self.graph.fit()).pack(side="right")
        ttk.Button(gbar, text="100%", width=5, command=lambda: self.graph.set_zoom(1.0)).pack(side="right", padx=(0, 4))
        ttk.Button(gbar, text="+", width=3, command=lambda: self.graph.zoom(1.2)).pack(side="right", padx=(0, 4))
        self.zoom_label = ttk.Label(gbar, text="100%", width=5, anchor="center")
        self.zoom_label.pack(side="right")
        ttk.Button(gbar, text="-", width=3, command=lambda: self.graph.zoom(1 / 1.2)).pack(side="right", padx=(0, 4))
        gwrap = ttk.Frame(gframe)
        gwrap.pack(fill="both", expand=True)
        self.graph = GraphView(gwrap, self.on_block_click, self.block_tooltip)
        hs = ttk.Scrollbar(gwrap, orient="horizontal", command=self.graph.xview)
        vs = ttk.Scrollbar(gwrap, orient="vertical", command=self.graph.yview)
        self.graph.configure(xscrollcommand=hs.set, yscrollcommand=vs.set)
        hs.pack(side="bottom", fill="x")
        vs.pack(side="right", fill="y")
        self.graph.pack(fill="both", expand=True)
        self.legend = tk.Frame(gframe)
        self.legend.pack(fill="x")
        self.legend_labels = []
        for key, text in (("true", "true"), ("false", "false"), ("except", "except"), ("other", "other edge"),
                          ("bad_line", "block with finding"), ("dead_line", "unreachable")):
            lab = tk.Label(self.legend, text=("■ " if key.endswith("_line") else "→ ") + text,
                           font=("Segoe UI", 8))
            lab.pack(side="left", padx=6)
            self.legend_labels.append((lab, key))

        fframe = ttk.Frame(right)
        right.add(fframe, weight=2)
        cols = ("rank", "kind", "line", "cost", "origin")
        self.tree = ttk.Treeview(fframe, columns=cols, show="headings", height=5, selectmode="browse")
        for c, w in zip(cols, (60, 140, 60, 60, 80)):
            self.tree.heading(c, text=c, command=lambda c=c: self.sort_by(c))
            self.tree.column(c, width=w, anchor="w", stretch=False)
        self.tree.pack(fill="x")
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        self.detail = tk.Text(fframe, height=6, wrap="word", font=("Segoe UI", 10), state="disabled", bd=0, padx=6, pady=4)
        self.detail.pack(fill="both", expand=True, pady=(4, 0))

        r.bind("<F5>", lambda e: self.analyze())
        r.bind("<Control-Return>", lambda e: self.analyze())
        r.bind("<Control-o>", lambda e: self.open_file())
        r.bind("<Control-s>", lambda e: self.save_file())
        r.bind("<Control-e>", lambda e: self.export_graph())
        r.bind("<Control-equal>", lambda e: self.graph.zoom(1.2))
        r.bind("<Control-plus>", lambda e: self.graph.zoom(1.2))
        r.bind("<Control-minus>", lambda e: self.graph.zoom(1 / 1.2))
        r.bind("<Control-0>", lambda e: self.graph.fit())

    # ---- theme and fonts
    def apply_theme(self, name):
        """Apply the light or dark colour scheme to every widget and redraw the graph."""
        self.theme_name = name
        t = THEMES[name]
        s = self.style
        s.configure(".", background=t["bg"], foreground=t["fg"], fieldbackground=t["field"])
        for w in ("TFrame", "TLabel", "TPanedwindow"):
            s.configure(w, background=t["bg"], foreground=t["fg"])
        s.configure("TButton", background=t["gutter"], foreground=t["fg"], bordercolor=t["gutter_fg"], padding=3)
        s.map("TButton", background=[("active", t["select"])])
        s.configure("TCheckbutton", background=t["bg"], foreground=t["fg"])
        s.map("TCheckbutton", background=[("active", t["bg"])])
        s.configure("TCombobox", fieldbackground=t["field"], foreground=t["fg"], background=t["gutter"],
                    arrowcolor=t["fg"], selectbackground=t["field"], selectforeground=t["fg"])
        s.map("TCombobox", fieldbackground=[("readonly", t["field"])], foreground=[("readonly", t["fg"])])
        s.configure("Treeview", background=t["field"], fieldbackground=t["field"], foreground=t["fg"], rowheight=22)
        s.configure("Treeview.Heading", background=t["gutter"], foreground=t["fg"])
        s.map("Treeview", background=[("selected", t["select"])], foreground=[("selected", t["fg"])])
        s.configure("TScrollbar", background=t["gutter"], troughcolor=t["bg"], arrowcolor=t["fg"])
        self.root.configure(bg=t["bg"])
        self.root.option_add("*TCombobox*Listbox.background", t["field"])
        self.root.option_add("*TCombobox*Listbox.foreground", t["fg"])
        self.text.configure(bg=t["editor"], fg=t["fg"], insertbackground=t["fg"], selectbackground=t["select"])
        self.lines.configure(bg=t["gutter"], fg=t["gutter_fg"])
        self.detail.configure(bg=t["field"], fg=t["fg"])
        self.text.tag_configure("finding", background=t["finding_bg"])
        self.text.tag_configure("blocklines", background=t["blocklines_bg"])
        self.text.tag_configure("selected", background=t["selected_bg"])
        for tag in ("kw", "str", "com", "num", "def", "builtin"):
            self.text.tag_configure(tag, foreground=t[tag])
        for kind, color in t["kinds"].items():
            self.tree.tag_configure(kind, foreground=color)
        self.legend.configure(bg=t["bg"])
        pal = cfgdraw.PALETTES[name]
        for lab, key in self.legend_labels:
            lab.configure(bg=t["bg"], fg=pal[key])
        self.theme_btn.configure(text="Light mode" if name == "dark" else "Dark mode")
        self.text.tag_raise("sel")
        if self.graph.cfg is not None:
            self.graph.draw(self.graph.cfg, self.graph.bad, pal, keep_view=True)
        else:
            self.graph.palette = pal
            self.graph.configure(bg=pal["bg"])
        self.on_select()

    def toggle_theme(self):
        """Switch between the light and dark theme."""
        self.apply_theme("dark" if self.theme_name == "light" else "light")

    def change_font(self, delta):
        """Grow or shrink the editor font (and line-number gutter) by `delta` points."""
        size = max(7, min(28, self.editor_font.cget("size") + delta))
        self.editor_font.configure(size=size)
        self._update_linenos()

    # ---- editor behaviour
    def _on_edit(self, _event=None):
        """After any edit: refresh line numbers, debounce syntax highlighting and live analysis."""
        self._update_linenos()
        if self._hl_job:
            self.root.after_cancel(self._hl_job)
        self._hl_job = self.root.after(120, self.highlight_syntax)
        if _event is not None and self.live.get() and _event.keysym not in (
                "Up", "Down", "Left", "Right", "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L"):
            if self._live_job:
                self.root.after_cancel(self._live_job)
            self._live_job = self.root.after(700, lambda: self.analyze(silent=True))
        elif _event is not None:
            self._cursor_changed()

    def highlight_syntax(self):
        """Colour keywords, strings, comments, numbers, definitions and builtins in the editor."""
        self._hl_job = None
        txt = self.text
        for tag in ("kw", "str", "com", "num", "def", "builtin"):
            txt.tag_remove(tag, "1.0", "end")
        src = txt.get("1.0", "end-1c")
        # Tk text indexes count characters, so use "1.0+Nc" offsets from the start
        for m in TOKEN_RE.finditer(src):
            kind = m.lastgroup
            a, b = f"1.0+{m.start()}c", f"1.0+{m.end()}c"
            if kind == "word":
                w = m.group()
                if keyword.iskeyword(w) or w in ("True", "False", "None"):
                    txt.tag_add("kw", a, b)
                elif hasattr(builtins, w):
                    txt.tag_add("builtin", a, b)
            elif kind == "defn":
                head = m.group().split()[0]
                txt.tag_add("kw", a, f"1.0+{m.start() + len(head)}c")
                txt.tag_add("def", f"1.0+{m.end() - len(m.group().split(None, 1)[1])}c", b)
            else:
                txt.tag_add(kind, a, b)

    def _update_linenos(self):
        """Refresh the line-number gutter to match the editor's line count."""
        n = int(self.text.index("end-1c").split(".")[0])
        self.lines.configure(state="normal", width=max(3, len(str(n)) + 1))
        self.lines.delete("1.0", "end")
        self.lines.insert("1.0", "\n".join(str(i) for i in range(1, n + 1)))
        self.lines.configure(state="disabled")
        self.lines.yview_moveto(self.text.yview()[0])

    def _cursor_changed(self):
        """Highlight, in the graph, every block that contains the editor cursor's line."""
        if not (self.cur and self.cur["cfg"]):
            return
        line = int(self.text.index("insert").split(".")[0])
        blocks = [b.id for b in self.cur["cfg"].blocks if any(s.lineno == line for s in b.stmts)]
        self.graph.highlight(None, (), blocks)
        if blocks:
            self.status.set(f"Line {line} is in " + ", ".join(self.cur["cfg"].name(self.cur["cfg"].blocks[b]) for b in blocks))

    # ---- actions
    def open_file(self):
        """Ask for a .py file and load it into the editor."""
        path = filedialog.askopenfilename(filetypes=[("Python", "*.py"), ("All files", "*.*")])
        if path:
            self._load(path)

    def save_file(self):
        """Save the editor contents to a .py file chosen by the user."""
        path = filedialog.asksaveasfilename(defaultextension=".py", filetypes=[("Python", "*.py")])
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self.text.get("1.0", "end-1c"))
            self.status.set(f"Saved {path}")

    def load_example(self, _event=None):
        """Load the example selected in the dropdown."""
        self._load(self.examples[self.ex_box.get()])

    def _load(self, path):
        """Replace the editor contents with a file's text (UTF-8, BOM accepted) and analyze it."""
        with open(path, encoding="utf-8-sig") as fh:
            src = fh.read()
        self.text.delete("1.0", "end")
        self.text.insert("1.0", src)
        self._on_edit()
        self.fn_box.set("")
        self.analyze()

    def copy_dot(self):
        """Copy the current CFG as Graphviz DOT text to the clipboard."""
        if self.cur and self.cur["cfg"]:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.cur["cfg"].dot())
            self.status.set("DOT copied to clipboard.")

    def export_graph(self):
        """Save the current CFG as PNG, SVG or DOT (chosen by the file extension)."""
        if not (self.cur and self.cur["cfg"]):
            messagebox.showinfo("CFG Tool", "Nothing to export: analyze a function first.")
            return
        name = self.cur["name"].replace("<", "").replace(">", "").replace(".", "_")
        path = filedialog.asksaveasfilename(
            defaultextension=".png", initialfile=f"cfg_{name}",
            filetypes=[("PNG image", "*.png"), ("SVG image", "*.svg"), ("Graphviz DOT", "*.dot")])
        if not path:
            return
        cfg, bad = self.cur["cfg"], {f["block"] for f in self.findings}
        try:
            if path.lower().endswith(".svg"):
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(cfgdraw.render_svg(cfg, bad))
            elif path.lower().endswith(".dot"):
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(cfg.dot())
            else:
                cfgdraw.render_png(cfg, path, bad)
        except ImportError:
            messagebox.showwarning("CFG Tool", "PNG export needs the Pillow package (pip install pillow). "
                                               "Save as SVG or DOT instead.")
            return
        self.status.set(f"Exported {path}")

    def show_summary(self):
        """Open a window with the per-function summary table and the call graph."""
        if not self.results:
            return
        t = THEMES[self.theme_name]
        win = tk.Toplevel(self.root)
        win.title("Summary of all functions")
        win.geometry("660x420")
        box = tk.Text(win, font=("Consolas", 10), wrap="none", bg=t["field"], fg=t["fg"])
        box.insert("1.0", cfgtool.summary_text(self.results, self.graph_calls))
        box.configure(state="disabled")
        box.pack(fill="both", expand=True)

    def _fail(self, msg, silent=False):
        """Clear the results and show an error message (status bar, details box and, unless silent, a popup)."""
        self.cur, self.results, self.findings, self.expl = None, [], [], []
        self.graph.delete("all")
        self.graph.cfg = None
        self.tree.delete(*self.tree.get_children())
        self.metrics.set("")
        self.fn_box["values"] = []
        self.text.tag_remove("finding", "1.0", "end")
        self.status.set(msg)
        self._set_detail("Could not analyze:\n\n" + msg)
        if not silent:
            messagebox.showwarning("CFG Tool", msg)

    def analyze(self, silent=False):
        """Parse the editor text, analyse every function, and show the selected one.

        Syntax errors and empty input give a status-bar message, a message in the detail
        box and (unless `silent`, used by live analysis) a popup. Functions that use
        unsupported constructs appear in the dropdown marked as skipped. Live analysis
        never calls the LLM.
        """
        src = self.text.get("1.0", "end-1c")
        try:
            self.results, self.graph_calls = cfgtool.analyze_source(src)
        except SyntaxError as e:
            return self._fail(f"Syntax error: {e.msg} (line {e.lineno})", silent)
        if not self.results:
            return self._fail("Nothing to analyze: the editor is empty.", silent)
        names = [r["name"] for r in self.results]
        self.fn_box["values"] = [n if r["cfg"] else f"{n} (skipped)" for n, r in zip(names, self.results)]
        want = self.fn_box.get().replace(" (skipped)", "")
        self.fn_box.current(names.index(want) if want in names else 0)
        self.show_selected(use_llm=not silent)

    def show_selected(self, use_llm=True):
        """Display the function chosen in the dropdown: graph, metrics, findings, editor marks."""
        idx = self.fn_box.current()
        if idx < 0 or idx >= len(self.results):
            return
        same = self.cur is not None and self.cur["name"] == self.results[idx]["name"]
        self.cur = r = self.results[idx]
        for tag in ("finding", "selected", "blocklines"):
            self.text.tag_remove(tag, "1.0", "end")
        if r["error"]:
            self.findings, self.expl = [], []
            self.graph.delete("all")
            self.graph.cfg = None
            self.tree.delete(*self.tree.get_children())
            self.metrics.set(f"{r['name']}: skipped")
            self._set_detail(f"This function was skipped:\n\n{r['error']}")
            self.status.set(f"Skipped {r['name']}: {r['error']}")
            return
        cfg = r["cfg"]
        self.findings = r["findings"]
        self.expl = [cfgtool.explain(f, False) for f in self.findings]
        self.graph.draw(cfg, {f["block"] for f in self.findings}, cfgdraw.PALETTES[self.theme_name], keep_view=same)
        if not same:
            self.root.after(30, self._fit_if_large)
        self._update_zoom_label()
        self.metrics.set(f"{r['name']}: {len(cfg.reachable())}/{len(cfg.blocks)} blocks reachable   "
                         f"cyclomatic {cfg.cyclomatic()}   cognitive {cfg.cog}   findings {len(self.findings)}")
        self._fill_tree()
        for f in self.findings:
            self.text.tag_add("finding", f"{f['line']}.0", f"{f['line']}.end+1c")
        self.status.set(f"Analyzed {len(self.results)} function(s)." if self.findings
                        else f"Analyzed {len(self.results)} function(s); none for this one has findings.")
        if use_llm:
            self._explain_all()

    def _fit_if_large(self):
        """After a new function is drawn, shrink the view to fit when the graph is larger than the window."""
        lay = self.graph.lay
        if lay and (lay.width > self.graph.winfo_width() or lay.height > self.graph.winfo_height()) and self.graph.winfo_width() > 50:
            self.graph.fit()
        else:
            self.graph.set_zoom(1.0)
        self._update_zoom_label()

    def _update_zoom_label(self):
        """Show the current zoom percentage next to the zoom buttons."""
        self.zoom_label.configure(text=f"{round(self.graph.scale * 100)}%")

    # ---- findings table
    def _fill_tree(self):
        """Repopulate the findings table (sorted as chosen) from self.findings / self.expl."""
        self.tree.delete(*self.tree.get_children())
        order = list(range(len(self.findings)))
        col, desc = self._sort
        key = {"rank": lambda i: self.findings[i]["rank"], "kind": lambda i: self.findings[i]["kind"],
               "line": lambda i: self.findings[i]["line"], "cost": lambda i: self.findings[i]["cost"],
               "origin": lambda i: self.expl[i][1]}[col]
        order.sort(key=key, reverse=desc)
        for i in order:
            f = self.findings[i]
            self.tree.insert("", "end", iid=str(i), tags=(f["kind"],),
                             values=(f["rank"], f["kind"], f["line"], f["cost"], self.expl[i][1]))
        if self.findings:
            self.tree.selection_set(str(order[0]))
        else:
            self._set_detail("No findings for this function.")

    def sort_by(self, col):
        """Sort the findings table by a column; clicking the same column again reverses the order."""
        self._sort = (col, not self._sort[1] if self._sort[0] == col else col in ("rank", "cost"))
        self._fill_tree()

    def _set_detail(self, text):
        """Show `text` in the read-only explanation box."""
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", text)
        self.detail.configure(state="disabled")

    def on_select(self, _event=None):
        """Show the selected finding's explanation and highlight its source line, block and evidence path."""
        sel = self.tree.selection()
        if not sel or not self.findings:
            return
        i = int(sel[0])
        f = self.findings[i]
        text, origin = self.expl[i]
        evidence = f"\n\nEvidence path: {' -> '.join(f['path'])}" if f["path"] else ""
        self._set_detail(f"[{f['kind']}] line {f['line']}: {f['source']}\n\n{text}{evidence}\n\n"
                         f"(cognitive cost {f['cost']}; explanation source: {origin})")
        self.text.tag_remove("selected", "1.0", "end")
        self.text.tag_add("selected", f"{f['line']}.0", f"{f['line']}.end+1c")
        self.text.see(f"{f['line']}.0")
        self.graph.highlight(f["block"], [block_id(p) for p in f["path"]])

    # ---- graph <-> editor linking
    def block_tooltip(self, bid):
        """Tooltip text for a block: its statements, successors and findings."""
        cfg = self.cur["cfg"]
        b = cfg.blocks[bid]
        lines = [cfg.name(b)] + ["  " + cfgtool.label(s) for s in b.stmts[:8]]
        if len(b.stmts) > 8:
            lines.append(f"  ... {len(b.stmts) - 8} more")
        lines.append("-> " + (", ".join(f"{cfg.name(t)}" + (f" [{l}]" if l else "") for t, l in b.succs[:6]) or "(none)"))
        if bid not in cfg.reachable():
            lines.append("UNREACHABLE")
        lines += [f"! {f['kind']} at line {f['line']}" for f in self.findings if f["block"] == bid]
        return "\n".join(lines)

    def on_block_click(self, bid):
        """A block was clicked in the graph: mark its source lines in the editor and select its finding."""
        cfg = self.cur["cfg"]
        b = cfg.blocks[bid]
        self.text.tag_remove("blocklines", "1.0", "end")
        lines = sorted({s.lineno for s in b.stmts})
        for ln in lines:
            self.text.tag_add("blocklines", f"{ln}.0", f"{ln}.end+1c")
        if lines:
            self.text.see(f"{lines[0]}.0")
        for i, f in enumerate(self.findings):
            if f["block"] == bid and self.tree.exists(str(i)):
                self.tree.selection_set(str(i))
                break
        else:
            self.graph.highlight(bid)
        self.status.set(f"{cfg.name(b)}: lines " + (", ".join(map(str, lines)) if lines else "(no statements)"))

    # ---- LLM explanations (background thread)
    def _explain_all(self):
        """If the LLM checkbox is on, request grounded explanations in a background thread.

        Shows a status message instead when no API key is configured. Turning the
        checkbox off (or re-analysing) restores template explanations and invalidates
        any in-flight request via the generation counter.
        """
        self.gen += 1
        if not self.use_llm.get() or not self.findings:
            if self.findings:
                self.expl = [cfgtool.explain(f, False) for f in self.findings]
                self._refresh_origins()
            return
        if not cfgtool.llm_provider():
            self.status.set("No GROQ_API_KEY / ANTHROPIC_API_KEY set: using template explanations.")
            return
        self.status.set("Asking the LLM for explanations...")
        gen, findings = self.gen, list(self.findings)

        def work():
            """Worker: explain each finding via the LLM and queue the results."""
            for i, f in enumerate(findings):
                self.q.put((gen, i, cfgtool.explain(f, True)))
            self.q.put((gen, -1, None))

        threading.Thread(target=work, daemon=True).start()

    def _poll(self):
        """Apply queued LLM results on the Tk thread (stale generations are ignored)."""
        try:
            while True:
                gen, i, res = self.q.get_nowait()
                if gen != self.gen:
                    continue
                if i == -1:
                    err = f" Last error: {cfgtool.LAST_LLM_ERROR}" if cfgtool.LAST_LLM_ERROR else ""
                    self.status.set("LLM explanations done (rows marked 'template' fell back)." + err)
                else:
                    self.expl[i] = res
                    self._refresh_origins()
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _refresh_origins(self):
        """Update the origin column and detail box after explanations change."""
        for i in range(len(self.findings)):
            if self.tree.exists(str(i)):
                self.tree.set(str(i), "origin", self.expl[i][1])
        self.on_select()


def main():
    """Create the Tk root and run the GUI main loop."""
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()

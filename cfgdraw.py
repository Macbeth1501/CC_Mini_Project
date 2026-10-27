"""Toolkit-independent drawing of a CFG: layout, SVG and PNG export.

The layout is computed once as a list of drawing primitives (rectangles, lines,
arrowhead polygons, text). gui.py draws them on a Tk canvas; render_svg and
render_png draw the same primitives to files, so the picture is identical.
Only the text-width function differs per target, so it is passed in.

Primitive dicts (all coordinates in pixels):
    {"t": "rect", "x0", "y0", "x1", "y1", "fill", "outline", "dash", "width", "bid"}
    {"t": "line", "pts": [x0, y0, x1, y1, ...], "color", "dash"}
    {"t": "poly", "pts": [...], "fill"}
    {"t": "text", "x", "y", "text", "color", "anchor" ("nw", "w" or "e"), "mono", "lh"}
"""
import html
import math

COL_TRUE, COL_FALSE, COL_EXCEPT, COL_OTHER = "#2e7d32", "#c62828", "#8e24aa", "#555555"
LINE_H = 16          # line height used by the SVG/PNG renderers

# Colour palettes for layout(). Exports (SVG/PNG) always use LIGHT; the GUI picks per theme.
LIGHT = {"bg": "#ffffff", "box": "#fafafa", "box_line": "#333333", "text": "#000000",
         "entry": "#dbeafe", "bad_fill": "#ffe9e9", "bad_line": "#c62828",
         "dead_fill": "#eeeeee", "dead_line": "#999999",
         "true": COL_TRUE, "false": COL_FALSE, "except": COL_EXCEPT, "other": COL_OTHER}
DARK = {"bg": "#1e1e1e", "box": "#2d2d30", "box_line": "#9a9a9a", "text": "#e6e6e6",
        "entry": "#1f3a5f", "bad_fill": "#4a2222", "bad_line": "#ff6b6b",
        "dead_fill": "#2a2a2a", "dead_line": "#666666",
        "true": "#66bb6a", "false": "#ef5350", "except": "#ce93d8", "other": "#b0b0b0"}
PALETTES = {"light": LIGHT, "dark": DARK}


def block_lines(cfg, b):
    """Return the text lines shown inside a block's box: its name, then its statements."""
    import cfgtool
    return [cfg.name(b)] + [cfgtool.label(s) for s in b.stmts]


def compute_levels(cfg):
    """Assign each reachable block a layer (longest forward path from ENTRY) and find back edges.

    A depth-first search marks an edge as a back edge when it targets a block still on
    the DFS stack. Layers are then computed over the remaining (acyclic) edges in
    reverse post-order, so every forward edge points to a deeper layer.

    Returns:
        (level, back): level maps block id -> layer number; back is a set of
        (source id, target id) back edges.
    """
    state, back, post = {}, set(), []

    def dfs(u):
        """Depth-first visit that records back edges and post-order."""
        state[u.id] = 1
        for t, _ in u.succs:
            if t.id not in state:
                dfs(t)
            elif state[t.id] == 1:
                back.add((u.id, t.id))
        state[u.id] = 2
        post.append(u.id)

    dfs(cfg.entry)
    level = {cfg.entry.id: 0}
    for uid in reversed(post):
        for t, _ in cfg.blocks[uid].succs:
            if (uid, t.id) not in back:
                level[t.id] = max(level.get(t.id, 0), level[uid] + 1)
    return level, back


def _arrow(x0, y0, x1, y1, size=9):
    """Return the triangle points of an arrowhead at (x1, y1) for a segment coming from (x0, y0)."""
    dx, dy = x1 - x0, y1 - y0
    n = math.hypot(dx, dy) or 1.0
    ux, uy = dx / n, dy / n
    px, py = -uy, ux
    return [x1, y1, x1 - size * ux + 4 * px, y1 - size * uy + 4 * py,
            x1 - size * ux - 4 * px, y1 - size * uy - 4 * py]


class Layout:
    """Result of layout(): primitives plus canvas size and block positions."""

    def __init__(self):
        """Create an empty layout."""
        self.prims = []
        self.pos = {}       # block id -> (centre x, centre y, width, height)
        self.width = 0
        self.height = 0
        self.bg = "#ffffff"


def layout(cfg, bad_blocks, measure, line_height, palette=None):
    """Lay out `cfg` as boxes and arrows.

    Blocks are placed in rows by layer (see compute_levels) and centred; unreachable
    blocks go in a final row. Edge routing never passes behind a box:
      * an edge to the next row is a straight arrow;
      * a loop back edge leaves the bottom of its source, runs through the gap below
        that row to a lane on the right, climbs to the gap above the target row and
        enters the target from the top;
      * an edge that skips rows (or points upward, e.g. from dead code to EXIT) uses
        the same idea with a lane on the left.
    Edge colours: true green, false red, except purple (dashed), others grey; edges
    leaving unreachable blocks are dashed.

    Args:
        cfg: A cfgtool.CFG.
        bad_blocks: Set of block ids that have findings (red outline).
        measure: Function text -> width in pixels for the block font.
        line_height: Height in pixels of one text line.
        palette: Colour dict (LIGHT or DARK); defaults to LIGHT.

    Returns:
        A Layout.
    """
    pal = palette or LIGHT
    lay = Layout()
    reach = cfg.reachable()
    level, back = compute_levels(cfg)
    rows = {}
    for b in cfg.blocks:
        if b.id in reach:
            rows.setdefault(level[b.id], []).append(b)
    dead = [b for b in cfg.blocks if b.id not in reach]
    if dead:
        rows[max(rows) + 1] = dead

    size = {}
    for b in cfg.blocks:
        lines = block_lines(cfg, b)
        size[b.id] = (max(measure(x) for x in lines) + 18, len(lines) * line_height + 10)
    gap_x, gap_y, margin, lane = 40, 55, 30, 18
    row_w = {lv: sum(size[b.id][0] for b in bs) + gap_x * (len(bs) - 1) for lv, bs in rows.items()}
    inner_w = max(row_w.values()) + 2 * margin

    row_of, row_top, row_bot = {}, {}, {}
    y = margin
    for ri, lv in enumerate(sorted(rows)):
        row_h = max(size[b.id][1] for b in rows[lv])
        row_top[ri], row_bot[ri] = y, y + row_h
        x = margin + (inner_w - 2 * margin - row_w[lv]) / 2
        for b in rows[lv]:
            w, h = size[b.id]
            lay.pos[b.id] = (x + w / 2, y + row_h / 2, w, h)
            row_of[b.id] = ri
            x += w + gap_x
        y += row_h + gap_y

    def kind(b, t):
        """Classify an edge as back, adjacent (straight), long (skips rows downward) or long_up."""
        if (b.id, t.id) in back:
            return "back"
        d = row_of[t.id] - row_of[b.id]
        if d == 1 or d == -1:
            return "adj"
        return "long" if d > 1 else "long_up"

    kinds = {(b.id, i): kind(b, t) for b in cfg.blocks for i, (t, _) in enumerate(b.succs)}
    n_left = sum(1 for v in kinds.values() if v in ("long", "long_up"))
    n_back = sum(1 for v in kinds.values() if v == "back")
    shift = lane * n_left
    lay.pos = {bid: (cx + shift, cy, w, h) for bid, (cx, cy, w, h) in lay.pos.items()}
    left_min = min(cx - w / 2 for cx, _, w, _ in lay.pos.values())
    right = max(cx + w / 2 for cx, _, w, _ in lay.pos.values())

    kl = kr = 0
    for b in cfg.blocks:
        ux, uy, uw, uh = lay.pos[b.id]
        for i, (t, lab) in enumerate(b.succs):
            tx, ty, tw, th = lay.pos[t.id]
            color = pal.get(lab if lab in ("true", "false", "except") else "other")
            dash = b.id not in reach or lab == "except"
            k = kinds[(b.id, i)]
            ru, rt = row_of[b.id], row_of[t.id]
            label_at = None
            if k == "back":
                kr += 1
                xo = right + lane * kr
                ys, yt = row_bot[ru] + gap_y / 2, row_top[rt] - gap_y / 2
                pts = [ux, uy + uh / 2, ux, ys, xo, ys, xo, yt, tx, yt, tx, ty - th / 2]
                label_at = (xo + 4, (ys + yt) / 2, "w")
            elif k in ("long", "long_up"):
                kl += 1
                xl = left_min - lane * kl
                if k == "long":
                    ys, yt = row_bot[ru] + gap_y / 2, row_top[rt] - gap_y / 2
                    pts = [ux, uy + uh / 2, ux, ys, xl, ys, xl, yt, tx, yt, tx, ty - th / 2]
                    label_at = (ux - 6, uy + uh / 2 + 12, "e")
                else:
                    ys, yt = row_top[ru] - gap_y / 2, row_bot[rt] + gap_y / 2
                    pts = [ux, uy - uh / 2, ux, ys, xl, ys, xl, yt, tx, yt, tx, ty + th / 2]
                    label_at = (ux - 6, uy - uh / 2 - 12, "e")
            elif ty > uy:
                pts = [ux, uy + uh / 2, tx, ty - th / 2]
                right_going = tx >= ux
                label_at = (ux + (tx - ux) * 0.3 + (6 if right_going else -6), uy + uh / 2 + 14,
                            "w" if right_going else "e")
            else:
                pts = [ux, uy - uh / 2, tx, ty + th / 2]
                right_going = tx >= ux
                label_at = (ux + (tx - ux) * 0.3 + (6 if right_going else -6), uy - uh / 2 - 12,
                            "w" if right_going else "e")
            lay.prims.append({"t": "line", "pts": pts, "color": color, "dash": dash})
            lay.prims.append({"t": "poly", "pts": _arrow(pts[-4], pts[-3], pts[-2], pts[-1]), "fill": color})
            if lab and lab != "except":
                lay.prims.append({"t": "text", "x": label_at[0], "y": label_at[1], "text": lab, "color": color,
                                  "anchor": label_at[2], "mono": False, "lh": line_height})

    for b in cfg.blocks:
        cx, cy, w, h = lay.pos[b.id]
        fill, outline, dash = pal["box"], pal["box_line"], False
        if b is cfg.entry or b is cfg.exit:
            fill = pal["entry"]
        if b.id not in reach:
            fill, outline, dash = pal["dead_fill"], pal["dead_line"], True
        if b.id in bad_blocks:
            fill, outline = pal["bad_fill"], pal["bad_line"]
        lay.prims.append({"t": "rect", "x0": cx - w / 2, "y0": cy - h / 2, "x1": cx + w / 2, "y1": cy + h / 2,
                          "fill": fill, "outline": outline, "dash": dash, "width": 1, "bid": b.id})
        lay.prims.append({"t": "text", "x": cx - w / 2 + 9, "y": cy - h / 2 + 5, "text": "\n".join(block_lines(cfg, b)),
                          "color": pal["text"], "anchor": "nw", "mono": True, "lh": line_height})
    lay.bg = pal["bg"]
    lay.width = right + lane * n_back + margin + 10
    lay.height = y - gap_y + margin + 10
    return lay


def to_svg(lay):
    """Return an SVG document (string) for a Layout."""
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{lay.width:.0f}" height="{lay.height:.0f}" '
           f'viewBox="0 0 {lay.width:.0f} {lay.height:.0f}">',
           f'<rect width="100%" height="100%" fill="white"/>']
    for p in lay.prims:
        t = p["t"]
        if t == "line":
            pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(p["pts"][::2], p["pts"][1::2]))
            dash = ' stroke-dasharray="4 3"' if p["dash"] else ""
            out.append(f'<polyline points="{pts}" fill="none" stroke="{p["color"]}"{dash}/>')
        elif t == "poly":
            pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(p["pts"][::2], p["pts"][1::2]))
            out.append(f'<polygon points="{pts}" fill="{p["fill"]}"/>')
        elif t == "rect":
            dash = ' stroke-dasharray="4 3"' if p["dash"] else ""
            out.append(f'<rect x="{p["x0"]:.1f}" y="{p["y0"]:.1f}" width="{p["x1"] - p["x0"]:.1f}" '
                       f'height="{p["y1"] - p["y0"]:.1f}" fill="{p["fill"]}" stroke="{p["outline"]}" '
                       f'stroke-width="{p["width"]}"{dash}/>')
        else:
            family = "Consolas, 'DejaVu Sans Mono', monospace" if p["mono"] else "'Segoe UI', sans-serif"
            size = 12 if p["mono"] else 11
            lines = p["text"].split("\n")
            if p["anchor"] in ("w", "e"):
                end = ' text-anchor="end"' if p["anchor"] == "e" else ""
                out.append(f'<text x="{p["x"]:.1f}" y="{p["y"]:.1f}" dominant-baseline="middle"{end} font-family="{family}" '
                           f'font-size="{size}" fill="{p["color"]}">{html.escape(lines[0])}</text>')
            else:
                spans = "".join(f'<tspan x="{p["x"]:.1f}" y="{p["y"] + (i + 0.8) * p["lh"]:.1f}">{html.escape(s)}</tspan>'
                                for i, s in enumerate(lines))
                out.append(f'<text font-family="{family}" font-size="{size}" fill="{p["color"]}" '
                           f'xml:space="preserve">{spans}</text>')
    out.append("</svg>")
    return "\n".join(out)


def render_svg(cfg, bad_blocks=()):
    """Lay out `cfg` with a monospace width estimate and return it as SVG text."""
    return to_svg(layout(cfg, set(bad_blocks), lambda s: 7.2 * len(s), LINE_H))


def _font(px):
    """Return a monospace PIL font of the given pixel size (falls back to PIL's default)."""
    from PIL import ImageFont
    for name in ("consola.ttf", "DejaVuSansMono.ttf", "cour.ttf", "LiberationMono-Regular.ttf"):
        try:
            return ImageFont.truetype(name, px)
        except OSError:
            continue
    return ImageFont.load_default()


def render_png(cfg, path, bad_blocks=(), scale=2):
    """Render `cfg` to a PNG file using Pillow (raises ImportError if Pillow is missing).

    Args:
        cfg: A cfgtool.CFG.
        path: Output file path.
        bad_blocks: Block ids to outline in red.
        scale: Pixel multiplier for a sharper image.
    """
    from PIL import Image, ImageDraw
    base = _font(12)
    lay = layout(cfg, set(bad_blocks), base.getlength, LINE_H)
    big = _font(12 * scale)
    img = Image.new("RGB", (int(lay.width * scale), int(lay.height * scale)), "white")
    d = ImageDraw.Draw(img)

    def dashed(pts, color):
        """Draw a dashed polyline."""
        for (x0, y0), (x1, y1) in zip(pts[:-1], pts[1:]):
            n = max(1, int(math.hypot(x1 - x0, y1 - y0) / (4 * scale)))
            for i in range(0, n, 2):
                a, b = i / n, min(1.0, (i + 1) / n)
                d.line([(x0 + (x1 - x0) * a, y0 + (y1 - y0) * a), (x0 + (x1 - x0) * b, y0 + (y1 - y0) * b)],
                       fill=color, width=scale)

    for p in lay.prims:
        t = p["t"]
        if t == "line":
            pts = [(x * scale, y * scale) for x, y in zip(p["pts"][::2], p["pts"][1::2])]
            if p["dash"]:
                dashed(pts, p["color"])
            else:
                d.line(pts, fill=p["color"], width=scale)
        elif t == "poly":
            d.polygon([(x * scale, y * scale) for x, y in zip(p["pts"][::2], p["pts"][1::2])], fill=p["fill"])
        elif t == "rect":
            x0, y0, x1, y1 = (p[k] * scale for k in ("x0", "y0", "x1", "y1"))
            d.rectangle([x0, y0, x1, y1], fill=p["fill"])
            if p["dash"]:
                dashed([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)], p["outline"])
            else:
                d.rectangle([x0, y0, x1, y1], outline=p["outline"], width=scale * p["width"])
        elif p["anchor"] in ("w", "e"):
            d.text((p["x"] * scale, p["y"] * scale), p["text"], fill=p["color"], font=big,
                   anchor="lm" if p["anchor"] == "w" else "rm")
        else:
            for i, line in enumerate(p["text"].split("\n")):
                d.text((p["x"] * scale, (p["y"] + i * p["lh"]) * scale), line, fill=p["color"], font=big)
    img.save(path)

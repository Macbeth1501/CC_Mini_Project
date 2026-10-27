"""Tests for the layout / SVG / PNG code (no Tk window needed)."""
import ast
import os
import xml.dom.minidom

import pytest

import cfgdraw
import cfgtool
from conftest import EXAMPLES


def all_cfgs():
    """Every analysed CFG of every example, as (label, cfg, blocks with findings)."""
    for name in sorted(os.listdir(EXAMPLES)):
        with open(os.path.join(EXAMPLES, name), encoding="utf-8") as fh:
            results, _ = cfgtool.analyze_source(fh.read())
        for r in results:
            yield f"{name}:{r['name']}", r["cfg"], {f["block"] for f in r["findings"]}


CASES = list(all_cfgs())
IDS = [c[0] for c in CASES]


def measure(s):
    """Fake text measurer: 7 px per character."""
    return 7.0 * len(s)


@pytest.mark.parametrize("label,cfg,bad", CASES, ids=IDS)
def test_layout_has_one_box_per_block_and_one_line_per_edge(label, cfg, bad):
    lay = cfgdraw.layout(cfg, bad, measure, 16)
    rects = [p for p in lay.prims if p["t"] == "rect"]
    lines = [p for p in lay.prims if p["t"] == "line"]
    assert len(rects) == len(cfg.blocks) and {p["bid"] for p in rects} == {b.id for b in cfg.blocks}
    assert len(lines) == sum(len(b.succs) for b in cfg.blocks)
    assert len([p for p in lay.prims if p["t"] == "poly"]) == len(lines)          # an arrowhead per edge


@pytest.mark.parametrize("label,cfg,bad", CASES, ids=IDS)
def test_boxes_never_overlap_and_stay_on_the_canvas(label, cfg, bad):
    lay = cfgdraw.layout(cfg, bad, measure, 16)
    boxes = [(p["x0"], p["y0"], p["x1"], p["y1"]) for p in lay.prims if p["t"] == "rect"]
    for i, a in enumerate(boxes):
        assert a[0] >= 0 and a[1] >= 0 and a[2] <= lay.width and a[3] <= lay.height
        for b in boxes[i + 1:]:
            assert a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1], (a, b)


@pytest.mark.parametrize("label,cfg,bad", CASES, ids=IDS)
def test_no_edge_segment_passes_through_a_box_it_does_not_touch(label, cfg, bad):
    lay = cfgdraw.layout(cfg, bad, measure, 16)
    boxes = {p["bid"]: p for p in lay.prims if p["t"] == "rect"}
    lines = [p for p in lay.prims if p["t"] == "line"]
    k = 0
    for b in cfg.blocks:
        for t, _ in b.succs:
            pts = lines[k]["pts"]
            k += 1
            segs = list(zip(zip(pts[::2], pts[1::2]), zip(pts[2::2], pts[3::2])))
            for bid, r in boxes.items():
                if bid in (b.id, t.id):
                    continue
                for (x0, y0), (x1, y1) in segs:
                    if x0 == x1:                       # vertical segment
                        hit = r["x0"] < x0 < r["x1"] and min(y0, y1) < r["y1"] and max(y0, y1) > r["y0"]
                    elif y0 == y1:                     # horizontal segment
                        hit = r["y0"] < y0 < r["y1"] and min(x0, x1) < r["x1"] and max(x0, x1) > r["x0"]
                    else:                              # diagonal: sample points along it
                        hit = any(r["x0"] < x0 + (x1 - x0) * s / 20 < r["x1"]
                                  and r["y0"] < y0 + (y1 - y0) * s / 20 < r["y1"] for s in range(1, 20))
                    assert not hit, f"{label}: edge B{b.id}->B{t.id} passes through box B{bid}"


@pytest.mark.parametrize("label,cfg,bad", CASES, ids=IDS)
def test_forward_edges_always_point_to_a_deeper_layer(label, cfg, bad):
    level, back = cfgdraw.compute_levels(cfg)
    for b in cfg.blocks:
        if b.id not in level:
            continue
        for t, _ in b.succs:
            if (b.id, t.id) not in back:
                assert level[t.id] > level[b.id]


def test_back_edges_are_found_for_loops_only():
    src = "def f(n):\n    while n:\n        n -= 1\n    return n\n"
    cfg = cfgtool.analyze(ast.parse(src).body[0], src.splitlines())[0]
    assert len(cfgdraw.compute_levels(cfg)[1]) == 1
    src = "def f(n):\n    if n:\n        n -= 1\n    return n\n"
    cfg = cfgtool.analyze(ast.parse(src).body[0], src.splitlines())[0]
    assert cfgdraw.compute_levels(cfg)[1] == set()


@pytest.mark.parametrize("label,cfg,bad", CASES[:12], ids=IDS[:12])
def test_svg_is_well_formed_and_contains_every_block(label, cfg, bad):
    svg = cfgdraw.render_svg(cfg, bad)
    doc = xml.dom.minidom.parseString(svg)
    assert doc.documentElement.tagName == "svg"
    assert len(doc.getElementsByTagName("rect")) == len(cfg.blocks) + 1           # + white background
    assert "ENTRY" in svg and "EXIT" in svg


def test_svg_escapes_special_characters():
    src = 'def f(a):\n    s = "<&>"\n    return s\n'
    cfg = cfgtool.analyze(ast.parse(src).body[0], src.splitlines())[0]
    xml.dom.minidom.parseString(cfgdraw.render_svg(cfg))                           # raises if unescaped


def test_png_export_writes_a_valid_image(tmp_path):
    pytest.importorskip("PIL")
    from PIL import Image
    cfg = next(c for l, c, b in CASES if l.startswith("loops.py"))
    out = tmp_path / "g.png"
    cfgdraw.render_png(cfg, str(out), {2})
    with Image.open(out) as im:
        assert im.format == "PNG" and im.width > 100 and im.height > 100

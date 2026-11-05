"""Generate the isometric line-art illustrations for the autopilot site into site/art/.

Same drawing rules as the honghao.me site (its scripts/gen_icons.py): 30-degree isometric, 1px ink lines,
three face tones, at most one meaningful accent per drawing, no text inside a drawing.

The object that stands for autopilot is a pilot's checklist on a clipboard: a pilot does not fly from
memory, every phase starts by reading the list. Every autopilot pass starts the same way.

Run: python3 site/gen_art.py
"""
import math
import pathlib

C30, S30 = math.cos(math.pi / 6), 0.5
INK, GROUND, TOP, LEFT, RIGHT = "#171717", "#fafafa", "#ffffff", "#f1f1f1", "#e6e6e6"
GRAY = "#8f8f8f"
# Accents (Okabe-Ito, as on honghao.me): one per drawing, only where it carries meaning.
GREEN, VERMILLION, AMBER = "#009e73", "#d55e00", "#e69f00"
DASH = 'stroke-dasharray="2 3"'
SVG_NS = "http://www.w3.org/2000/svg"


class Iso:
    def __init__(self, s, ox=0, oy=0, sw=1):
        self.s, self.ox, self.oy, self.sw, self.out, self.seen = s, ox, oy, sw, [], []

    def p(self, x, y, z):
        q = (self.ox + (x - y) * C30 * self.s, self.oy + (x + y) * S30 * self.s - z * self.s)
        self.seen.append(q)
        return q

    def poly(self, pts, fill=GROUND, stroke=INK, sw=None, extra=""):
        d = " ".join(f"{a:.1f},{b:.1f}" for a, b in (self.p(*q) for q in pts))
        self.out.append(f'<polygon points="{d}" fill="{fill}" stroke="{stroke}" stroke-width="{sw or self.sw}" {extra}/>')

    def polyline(self, pts, stroke=INK, sw=None, extra=""):
        d = " ".join(f"{a:.1f},{b:.1f}" for a, b in (self.p(*q) for q in pts))
        self.out.append(f'<polyline points="{d}" fill="none" stroke="{stroke}" stroke-width="{sw or self.sw}" {extra}/>')

    def line(self, a, b, stroke=INK, sw=None, extra=""):
        (x1, y1), (x2, y2) = self.p(*a), self.p(*b)
        self.out.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{stroke}" '
                        f'stroke-width="{sw or self.sw}" {extra}/>')

    def box(self, x, y, z, w, d, h, top=TOP, left=LEFT, right=RIGHT, extra=""):
        # visible faces: +y face (lower-left), +x face (lower-right), top
        self.poly([(x, y + d, z), (x + w, y + d, z), (x + w, y + d, z + h), (x, y + d, z + h)], left, extra=extra)
        self.poly([(x + w, y, z), (x + w, y + d, z), (x + w, y + d, z + h), (x + w, y, z + h)], right, extra=extra)
        self.poly([(x, y, z + h), (x + w, y, z + h), (x + w, y + d, z + h), (x, y + d, z + h)], top, extra=extra)

    def circle(self, c, r, plane, fill=GROUND, n=36, sw=None, stroke=INK):
        cx, cy, cz = c
        pts = []
        for i in range(n):
            t = 2 * math.pi * i / n
            u, v = r * math.cos(t), r * math.sin(t)
            pts.append({"xz": (cx + u, cy, cz + v), "yz": (cx, cy + u, cz + v), "xy": (cx + u, cy + v, cz)}[plane])
        self.poly(pts, fill, sw=sw, stroke=stroke)

    def hull(self, pts3, fill):
        pts = convex_hull([self.p(*q) for q in pts3])
        d = " ".join(f"{a:.1f},{b:.1f}" for a, b in pts)
        self.out.append(f'<polygon points="{d}" fill="{fill}" stroke="{INK}" stroke-width="{self.sw}"/>')

    def cylinder_y(self, x, z, r, y0, y1, fill=LEFT, cap=TOP):
        # a cylinder whose axis runs along y (a disc facing the viewer's lower left); silhouette = hull of both caps
        ring = [(math.cos(2 * math.pi * i / 48), math.sin(2 * math.pi * i / 48)) for i in range(48)]
        self.hull([(x + r * u, y, z + r * v) for y in (y0, y1) for u, v in ring], fill)
        self.circle((x, y1, z), r, "xz", fill=cap)

    def cylinder_z(self, x, y, z, r, h, fill=LEFT, cap=TOP):
        # an upright cylinder; silhouette = hull of the bottom and top circles
        ring = [(math.cos(2 * math.pi * i / 48), math.sin(2 * math.pi * i / 48)) for i in range(48)]
        self.hull([(x + r * u, y + r * v, zz) for zz in (z, z + h) for u, v in ring], fill)
        self.circle((x, y, z + h), r, "xy", fill=cap)

    def dome(self, x, y, z, r, fill=TOP):
        # a half sphere resting on z; convex, so its outline is the hull of points on its surface
        pts = []
        for i in range(10):
            phi = math.pi / 2 * i / 9
            for j in range(36):
                t = 2 * math.pi * j / 36
                pts.append((x + r * math.sin(phi) * math.cos(t), y + r * math.sin(phi) * math.sin(t), z + r * math.cos(phi)))
        self.hull(pts, fill)

    def check(self, c, size, color=INK, sw=1.4):
        # a screen-aligned check mark (strokes along the iso axes would read as slanted)
        x, y = self.p(*c)
        a = size
        self.out.append(f'<path d="M{x - a:.1f},{y:.1f} L{x - a * 0.3:.1f},{y + a * 0.7:.1f} L{x + a:.1f},{y - a * 0.75:.1f}" '
                        f'fill="none" stroke="{color}" stroke-width="{sw}"/>')

    def rays(self, c, r0, r1, angles, color=AMBER):
        # short dashed accent strokes fanning out on screen from a 3D point (a ring, a glow); angles in degrees,
        # 0 = right, 90 = up
        x, y = self.p(*c)
        for a in angles:
            t = math.radians(a)
            self.seen.append((x + r1 * math.cos(t), y - r1 * math.sin(t)))
            self.out.append(f'<line x1="{x + r0 * math.cos(t):.1f}" y1="{y - r0 * math.sin(t):.1f}" '
                            f'x2="{x + r1 * math.cos(t):.1f}" y2="{y - r1 * math.sin(t):.1f}" stroke="{color}" '
                            f'stroke-width="{self.sw}" stroke-dasharray="2 2"/>')

    def arrowhead(self, a, b, color=INK, size=5, sw=1.2):
        # a screen-aligned chevron at b, pointing from a to b
        (x1, y1), (x2, y2) = self.p(*a), self.p(*b)
        ang = math.atan2(y2 - y1, x2 - x1)
        pts = [(x2 - size * math.cos(ang - k), y2 - size * math.sin(ang - k)) for k in (0.5, -0.5)]
        self.out.append(f'<path d="M{pts[0][0]:.1f},{pts[0][1]:.1f} L{x2:.1f},{y2:.1f} L{pts[1][0]:.1f},{pts[1][1]:.1f}" '
                        f'fill="none" stroke="{color}" stroke-width="{sw}"/>')

    def svg(self, w, h, label):
        # fit the drawing into a w x h box, centered, with a little padding
        xs, ys = [q[0] for q in self.seen], [q[1] for q in self.seen]
        pad = 4
        bw, bh = max(xs) - min(xs) + 2 * pad, max(ys) - min(ys) + 2 * pad
        k = max(bw / w, bh / h)
        vx, vy = (min(xs) + max(xs) - w * k) / 2, (min(ys) + max(ys) - h * k) / 2
        body = "\n    ".join(self.out)
        return (f'<svg xmlns="{SVG_NS}" viewBox="{vx:.1f} {vy:.1f} {w*k:.1f} {h*k:.1f}" width="{w}" '
                f'height="{h}" role="img" aria-label="{label}" stroke-linejoin="round" stroke-linecap="round">'
                f'\n    {body}\n  </svg>')


def convex_hull(pts):
    pts = sorted(set((round(a, 2), round(b, 2)) for a, b in pts))

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lo, hi = [], []
    for q in pts:
        while len(lo) >= 2 and cross(lo[-2], lo[-1], q) <= 0:
            lo.pop()
        lo.append(q)
    for q in reversed(pts):
        while len(hi) >= 2 and cross(hi[-2], hi[-1], q) <= 0:
            hi.pop()
        hi.append(q)
    return lo[:-1] + hi[:-1]


# ---------------------------------------------------------------------------------------------------------
# Objects


def clipboard(g, x, y, z, w=6.0, h=8.0, rows=6, current=3):
    """A pilot's checklist on a clipboard, standing upright with its face toward the viewer's lower left.
    Steps above `current` are ticked; the amber box is the step being worked on now."""
    d = 0.3
    g.box(x, y, z, w, d, h)                                     # the board
    px, pz, pw, ph = x + 0.4, z + 0.4, w - 0.8, h - 1.3
    g.box(px, y + d, pz, pw, 0.05, ph)                          # the sheet
    fy = y + d + 0.05

    def f(u, v):
        return (px + u, fy, pz + v)
    g.line(f(0.5, ph - 0.85), f(pw * 0.6, ph - 0.85), sw=1.6)  # title
    lengths = (3.2, 2.5, 3.0, 2.7, 2.1, 2.9)
    step = min(0.95, (ph - 2.4) / max(rows - 1, 1))
    for i in range(rows):
        v = ph - 1.8 - i * step
        box = [f(0.5, v - 0.24), f(0.98, v - 0.24), f(0.98, v + 0.24), f(0.5, v + 0.24)]
        g.poly(box, fill=AMBER if i == current else TOP)
        ln = min(lengths[i % len(lengths)], pw - 1.8) * (pw / 5.2 if pw < 5.2 else 1)
        g.line(f(1.35, v), f(1.35 + ln, v), stroke=INK if i <= current else GRAY)
        if i < current:
            g.check(f(0.74, v), size=0.2 * g.s, sw=max(1.2, g.sw * 1.3))
    # the clip: a metal plate over the sheet's top edge, a lever, and the hanging loop
    cx = x + w / 2
    g.box(cx - 1.35, y + d, z + h - 1.45, 2.7, 0.35, 1.0)
    g.box(cx - 0.95, y + d + 0.05, z + h - 0.45, 1.9, 0.25, 0.4)
    outer = [(cx + 0.62 * math.cos(t), y + d + 0.18, z + h - 0.05 + 0.62 * math.sin(t))
             for t in (math.pi * i / 18 for i in range(19))]
    inner = [(cx + 0.4 * math.cos(t), y + d + 0.18, z + h - 0.05 + 0.4 * math.sin(t))
             for t in (math.pi * (18 - i) / 18 for i in range(19))]
    g.poly(outer + inner, fill=LEFT)


def alarm_clock(g, x, y, z, r=2.0, ring=True):
    """A twin-bell alarm clock: it wakes the loop. Its face looks toward the viewer's lower left."""
    cz = z + r + 0.45
    yb, yf = y, y + 1.1
    for k in (-1, 1):                                           # feet
        g.box(x + k * 0.55 * r - 0.2, y + 0.35, z, 0.4, 0.4, 0.6)
    g.line((x - 0.55 * r, y + 0.55, cz + 0.95 * r), (x + 0.55 * r, y + 0.55, cz + 0.95 * r), sw=1.4)  # bell bar
    for k in (-1, 1):                                           # bells, behind the body's top
        g.dome(x + k * 0.62 * r, y + 0.55, cz + 0.62 * r, 0.62 * r * 0.75)
    g.cylinder_z(x, y + 0.55, cz + r * 0.98, 0.14, 0.35)          # the hammer post
    g.cylinder_y(x, cz, r, yb, yf)                               # the body
    g.circle((x, yf, cz), r * 0.86, "xz", fill=TOP)              # the bezel
    for i in range(12):                                          # hour marks
        t = math.pi / 2 - 2 * math.pi * i / 12
        r0 = r * (0.64 if i % 3 == 0 else 0.72)
        g.line((x + r0 * math.cos(t), yf, cz + r0 * math.sin(t)),
               (x + r * 0.78 * math.cos(t), yf, cz + r * 0.78 * math.sin(t)), sw=1.2 if i % 3 == 0 else 1)
    for t, ln, sw in ((math.radians(150), 0.4, 1.6), (math.radians(90), 0.6, 1.2)):  # hour and minute hands
        g.line((x, yf, cz), (x + r * ln * math.cos(t), yf, cz + r * ln * math.sin(t)), sw=sw)
    g.circle((x, yf, cz), 0.09 * r, "xz", fill=INK)
    if ring:                                                     # it rings: dashed accent strokes above the bells
        for k, angles in ((-1, (100, 130, 160)), (1, (20, 50, 80))):
            g.rays((x + k * 0.62 * r, y + 0.55, cz + 0.62 * r + 0.3 * r), 0.75 * r * g.s, 1.1 * r * g.s, angles)


def folders(g, x, y, z, n=3):
    """A desk file sorter with one folder per worker: each task gets its own copy of the repo to work in."""
    w, depth = 5.0, 0.9 * n + 0.6
    g.box(x, y, z, w + 0.3, depth, 0.2)                          # base
    g.box(x, y, z + 0.2, 0.15, depth, 1.5)                       # left wall (its inner face shows)
    for k in range(n):                                           # back to front
        fy = y + 0.35 + 0.9 * k
        g.box(x + 0.3, fy, z + 0.2, w - 0.4, 0.15, 3.2)
        tx = x + 0.6 + k * 1.45
        g.box(tx, fy, z + 3.4, 1.3, 0.15, 0.45)
        for r in range(2):
            v = z + 2.6 - r * 0.45
            g.line((x + 0.8, fy + 0.15, v), (x + 0.8 + (2.6 if r == 0 else 1.8), fy + 0.15, v), stroke=GRAY)
    g.box(x + w + 0.15, y, z + 0.2, 0.15, depth, 1.5)            # right wall
    g.box(x, y + depth - 0.15, z + 0.2, w + 0.3, 0.15, 1.5)      # front wall


def tray(g, x, y, z, waiting=True):
    """An in-tray of cards: the page puts what waits on you first, each card with its answer buttons."""
    w, d = 6.4, 4.6
    g.box(x, y, z, w, d, 0.25)                                   # base
    g.box(x, y, z + 0.25, w, 0.15, 0.9)                          # back wall
    g.box(x, y, z + 0.25, 0.15, d, 0.9)                          # left wall
    offs = [(0.55, 0.62), (0.4, 0.5), (0.62, 0.42)]
    zz = z + 0.25
    for ox, oy in offs:                                          # older cards underneath
        g.box(x + ox, y + oy, zz, 5.2, 3.4, 0.1)
        zz += 0.1
    cx, cy = x + 0.5, y + 0.55
    g.box(cx, cy, zz, 5.2, 3.4, 0.1)                             # the top card
    top = zz + 0.1

    def f(u, v):
        return (cx + u, cy + v, top)
    if waiting:
        g.circle(f(0.6, 0.6), 0.24, "xy", fill=VERMILLION)       # waiting on you
    g.line(f(1.1, 0.6), f(3.8, 0.6), sw=1.4)
    g.line(f(0.4, 1.25), f(4.6, 1.25), stroke=GRAY)
    g.line(f(0.4, 1.7), f(3.6, 1.7), stroke=GRAY)
    for u0, u1 in ((0.4, 2.3), (2.6, 4.5)):                     # two answer buttons
        g.poly([f(u0, 2.2), f(u1, 2.2), f(u1, 2.9), f(u0, 2.9)], fill=TOP)
    g.box(x + w - 0.15, y, z + 0.25, 0.15, d, 0.9)               # right wall
    g.box(x, y + d - 0.15, z + 0.25, w, 0.15, 0.9)               # front wall


def sheet(g, x, y, z, w, d, rules=None, lines=3, h=0.1):
    """A sheet of paper lying flat. `rules` colors the first row (the standing rules), None leaves it out."""
    g.box(x, y, z, w, d, h)
    t = z + h
    v = 0.45
    if rules:
        g.poly([(x + 0.35, y + v - 0.12, t), (x + w - 0.6, y + v - 0.12, t), (x + w - 0.6, y + v + 0.12, t),
                (x + 0.35, y + v + 0.12, t)], fill=rules, stroke=rules)
        v += 0.5
    for r in range(lines):
        g.line((x + 0.35, y + v + r * 0.42, t), (x + w - 0.5 - (r % 2) * 0.7, y + v + r * 0.42, t), stroke=GRAY)


def padlock(g, x, y, z):
    """A padlock on a sheet of rules: the passes cannot edit them."""
    sheet(g, x - 1.2, y - 1.4, z, 5.6, 4.6, lines=7)
    zt = z + 0.1
    bx, by, bw, bd, bh = x, y, 2.6, 1.0, 2.1
    cy = by + bd / 2
    # shackle (behind the body's top): a U of two arcs joined, standing in the body's mid-plane
    cxs, base = bx + bw / 2, zt + bh
    ro, ri = 0.95, 0.6
    outer = [(cxs - ro, cy, base)] + [(cxs + ro * math.cos(t), cy, base + 0.7 + ro * math.sin(t))
                                      for t in (math.pi - math.pi * i / 24 for i in range(25))] + [(cxs + ro, cy, base)]
    inner = [(cxs + ri, cy, base)] + [(cxs + ri * math.cos(t), cy, base + 0.7 + ri * math.sin(t))
                                      for t in (math.pi * i / 24 for i in range(25))] + [(cxs - ri, cy, base)]
    g.poly(outer + inner, fill=LEFT)
    g.box(bx, by, zt, bw, bd, bh)                                # body
    fy = by + bd
    g.circle((cxs, fy, zt + 1.25), 0.2, "xz", fill=INK)          # keyhole
    g.poly([(cxs - 0.08, fy, zt + 1.2), (cxs + 0.08, fy, zt + 1.2), (cxs + 0.08, fy, zt + 0.7), (cxs - 0.08, fy, zt + 0.7)],
           fill=INK)


def stamp(g, x, y, z):
    """A rubber stamp beside a sheet it has approved: merging waits for the checks to pass."""
    sx, sy = x - 0.2, y + 1.0
    sheet(g, sx, sy, z, 5.2, 4.0, lines=0)
    t = z + 0.1

    def f(u, v):
        return (sx + u, sy + v, t)
    for r in range(3):
        g.line(f(0.4, 0.5 + r * 0.42), f(4.4 - (r % 2) * 0.8, 0.5 + r * 0.42), stroke=GRAY)
    g.poly([f(1.4, 1.8), f(3.4, 1.8), f(3.4, 3.6), f(1.4, 3.6)], fill="none", stroke=GREEN, sw=1.4)  # the imprint
    g.check(f(2.4, 2.7), size=0.42 * g.s, color=GREEN, sw=1.8)
    # the stamp, standing behind and to the right of the sheet
    kx, ky = x + 5.4, y - 0.6
    g.box(kx - 1.2, ky - 0.9, z, 2.4, 1.8, 0.35)                    # rubber pad
    g.box(kx - 1.2, ky - 0.9, z + 0.35, 2.4, 1.8, 0.55)             # wooden block
    g.cylinder_z(kx, ky, z + 0.9, 0.32, 1.3)                      # neck
    g.cylinder_z(kx, ky, z + 2.2, 0.8, 0.35)                      # knob
    g.dome(kx, ky, z + 2.55, 0.8)


def gauge(g, x, y, z, value=0.6):
    """A dial gauge on a stand: no new work starts once usage reaches the vermillion zone."""
    r = 2.4
    cz = z + r + 0.7
    g.box(x - 1.6, y - 0.2, z, 3.2, 1.6, 0.35)                   # stand base
    g.box(x - 0.35, y + 0.2, z + 0.35, 0.7, 0.5, 0.7)            # post
    g.cylinder_y(x, cz, r, y, y + 0.7)
    fy = y + 0.7
    g.circle((x, fy, cz), r * 0.88, "xz", fill=TOP)

    def at(v, rr):
        t = math.radians(225 - 270 * v)
        return (x + rr * math.cos(t), fy, cz + rr * math.sin(t))
    for i in range(11):                                          # scale
        g.line(at(i / 10, r * (0.62 if i % 5 == 0 else 0.68)), at(i / 10, r * 0.76), sw=1.2 if i % 5 == 0 else 1)
    g.polyline([at(0.85 + 0.15 * i / 12, r * 0.72) for i in range(13)], stroke=VERMILLION, sw=2.6)  # 85% to full
    g.line((x, fy, cz), at(value, r * 0.6), sw=1.6)               # needle
    g.circle((x, fy, cz), 0.16, "xz", fill=INK)


def service_bell(g, x, y, z):
    """A front-desk bell: nobody waits at the desk, so you ring when you want to talk."""
    g.cylinder_z(x, y, z, 2.0, 0.45)                             # base
    g.dome(x, y, z + 0.45, 1.55)                                 # the bell
    g.cylinder_z(x, y, z + 1.95, 0.1, 0.75)                      # plunger
    g.cylinder_z(x, y, z + 2.7, 0.34, 0.12)                      # button
    g.rays((x, y, z + 1.0), 2.0 * g.s, 2.6 * g.s, (25, 50, 130, 155))  # it rings


# ---------------------------------------------------------------------------------------------------------
# Figures


def hero():
    g = Iso(20)
    clipboard(g, 0, 0, 0)
    return g.svg(420, 300, "A pilot's checklist on a clipboard: the first steps ticked, the current step marked in amber")


def single():
    """One long session: a growing pile of sheets; the rules sit on the bottom sheet, faded."""
    g = Iso(13)
    jitter = [(0.0, 0.0), (0.25, -0.15), (-0.1, 0.2), (0.3, 0.1), (-0.2, -0.1), (0.15, 0.25), (-0.25, 0.05),
              (0.2, -0.2), (0.0, 0.15), (-0.15, -0.2), (0.25, 0.2), (-0.05, -0.05), (0.1, 0.1), (-0.2, 0.15)]
    # the first sheet, your rules on it, sticking out at the front of the pile and faded
    g.box(-0.2, 0.0, 0, 6.0, 5.6, 0.1)
    g.poly([(0.2, 4.75, 0.1), (5.2, 4.75, 0.1), (5.2, 5.15, 0.1), (0.2, 5.15, 0.1)], fill=AMBER + "33", stroke=AMBER + "33")
    z = 0.1
    for k in range(3):
        for dx, dy in jitter:
            last = k == 2 and (dx, dy) == jitter[-1]
            sheet(g, 0.2 + dx, -0.2 + dy, z, 5.6, 4.2, lines=6 if last else 0, h=0.1)
            z += 0.1
    return g.svg(360, 260, "One long session: a tall pile of pages, with the rules on the bottom page, faded")


def fresh():
    """A new session for every pass: separate short sheets, each starting with the rules at full strength."""
    g = Iso(13)
    for k in range(4):
        sheet(g, k * 3.2, -k * 3.2, 0, 3.6, 3.0, rules=AMBER, lines=3)
    return g.svg(360, 260, "A new session for every pass: four separate short pages, each starting with the rules in full color")


def cycle():
    """The loop: the alarm clock wakes a check, a pass reads its checklist, workers take the tasks."""
    g = Iso(13)
    alarm_clock(g, 0, 0, 0, r=1.9, ring=False)
    clipboard(g, 5.2, -6.2, 0, w=4.2, h=5.6, rows=4, current=2)
    folders(g, 12.6, -12.2, 0)
    # leads between the parts, with screen-aligned arrowheads; on screen, (+1, -1, 0) runs straight right
    leads = [((2.6, -1.4, 2.6), (5.2, -4.0, 2.6)), ((10.2, -6.8, 2.6), (12.6, -9.2, 2.6))]
    for a, b in leads:
        g.line(a, b, stroke=AMBER, extra=DASH)
        g.arrowhead(a, b, color=AMBER)
    # each worker kicks the check when it is done: back along the floor, in front of everything
    back = [(15.0, -8.2, 0), (3.1, 3.7, 0), (3.1, 3.7, 2.0)]
    g.polyline(back, stroke=AMBER, extra=DASH)
    g.arrowhead(back[-2], back[-1], color=AMBER)
    return g.svg(640, 260, "The loop: an alarm clock, a clipboard checklist and a file sorter of three folders, "
                           "joined by dashed arrows that run in a circle")


def f_clock():
    g = Iso(13)
    alarm_clock(g, 0, 0, 0)
    return g.svg(400, 250, "A twin-bell alarm clock, ringing")


def f_folders():
    g = Iso(13)
    folders(g, 0, 0, 0)
    return g.svg(400, 250, "A desk file sorter holding three folders, one per worker")


def f_tray():
    g = Iso(13)
    tray(g, 0, 0, 0)
    return g.svg(400, 250, "An in-tray of cards; the top card has a vermillion dot and two answer buttons")


def f_lock():
    g = Iso(13)
    padlock(g, 0, 0, 0)
    return g.svg(400, 250, "A padlock standing on a sheet of rules")


def f_stamp():
    g = Iso(13)
    stamp(g, 0, 0, 0)
    return g.svg(400, 250, "A rubber stamp beside a sheet with a green approval mark")


def f_gauge():
    g = Iso(13)
    gauge(g, 0, 0, 0)
    return g.svg(400, 250, "A dial gauge; the last part of its scale is vermillion and the needle sits below it")


def f_bell():
    g = Iso(13)
    service_bell(g, 0, 0, 0)
    return g.svg(400, 250, "A front-desk service bell, ringing")


def favicon():
    # The clipboard reduced to what reads at 16-32 px: board, sheet, clip and the amber current step.
    g = Iso(2.6, sw=1.1)
    w, h, d = 6.0, 8.0, 0.4
    g.box(0, 0, 0, w, d, h)
    g.box(0.6, d, 0.6, w - 1.2, 0.05, h - 1.9)
    fy = d + 0.05
    for i, z in enumerate((4.6, 3.1, 1.6)):
        fill = AMBER if i == 2 else TOP
        g.poly([(1.2, fy, z - 0.45), (2.1, fy, z - 0.45), (2.1, fy, z + 0.45), (1.2, fy, z + 0.45)], fill=fill)
        g.line((2.7, fy, z), (4.8, fy, z))
    g.box(1.6, d, h - 1.9, 2.8, 0.4, 1.5)
    return g.svg(32, 32, "autopilot")


if __name__ == "__main__":
    here = pathlib.Path(__file__).resolve().parent
    out = here / "art"
    out.mkdir(parents=True, exist_ok=True)
    figures = {"hero": hero, "single": single, "fresh": fresh, "cycle": cycle, "clock": f_clock,
               "folders": f_folders, "tray": f_tray, "lock": f_lock, "stamp": f_stamp, "gauge": f_gauge,
               "bell": f_bell}
    for name, fn in figures.items():
        (out / f"{name}.svg").write_text(fn() + "\n")
        print(f"wrote site/art/{name}.svg")
    (here / "favicon.svg").write_text(favicon() + "\n")
    print("wrote site/favicon.svg")

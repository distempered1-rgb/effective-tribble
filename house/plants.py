"""
plants.py - geometric (Nanite-friendly) plants for build_house.py.

Every leaf, needle and petal is real geometry: opaque, no alpha cut-outs, and
double-sided so it looks right from any angle in any engine. That is exactly
what Unreal's Nanite handles best - it streams millions of triangles cheaply,
while masked "leaf card" materials are slow under Nanite.

Each generator returns a PlantMesh; build_house.py turns it into an object.
Pivots sit at ground level.
"""

import math
import random

import bpy
from mathutils import Matrix, Vector, noise

UP = Vector((0, 0, 1))
PALETTE_COLS = 8


class PlantMesh:
    """Fast mesh accumulator with explicit per-corner UVs and per-face smoothing."""

    def __init__(self):
        self.verts = []
        self.faces = []
        self.uvs = []
        self.face_mat = []
        self.face_smooth = []
        self.mat_names = []

    def mat(self, name):
        if name not in self.mat_names:
            self.mat_names.append(name)
        return self.mat_names.index(name)

    def vert(self, co):
        self.verts.append(tuple(co))
        return len(self.verts) - 1

    def face(self, idx, uvs, mat, smooth):
        self.faces.append(tuple(idx))
        self.uvs.extend(uvs)
        self.face_mat.append(mat)
        self.face_smooth.append(smooth)

    @property
    def tris(self):
        return sum(len(f) - 2 for f in self.faces)

    def to_mesh(self, name, materials):
        me = bpy.data.meshes.new(name)
        me.from_pydata(self.verts, [], self.faces)
        uv = me.uv_layers.new(name="UVMap")
        flat = [c for pair in self.uvs for c in pair]
        uv.data.foreach_set("uv", flat)
        me.polygons.foreach_set("material_index", self.face_mat)
        me.polygons.foreach_set("use_smooth", self.face_smooth)
        for m in self.mat_names:
            me.materials.append(materials[m])
        me.validate(clean_customdata=False)
        me.update()
        return me


# --------------------------------------------------------------------------- #
# Primitive pieces
# --------------------------------------------------------------------------- #

def perpendicular(v):
    ref = Vector((1, 0, 0)) if abs(v.x) < 0.9 else Vector((0, 1, 0))
    return v.cross(ref).normalized()


def tube(pm, pts, radii, mat, tile=0.5, sides=None, cap=True):
    """Tapered tube along a polyline, parallel-transport frames (no twisting)."""
    m = pm.mat(mat)
    n = sides or max(3, min(12, int(6 + radii[0] * 60)))
    rings = []
    d0 = (pts[1] - pts[0]).normalized()
    u = perpendicular(d0)
    dist = [0.0]
    for i in range(1, len(pts)):
        dist.append(dist[-1] + (pts[i] - pts[i - 1]).length)
    for i, p in enumerate(pts):
        d = (pts[min(i + 1, len(pts) - 1)] - pts[max(i - 1, 0)]).normalized()
        u = (u - d * u.dot(d)).normalized()
        w = d.cross(u)
        rings.append([pm.vert(p + (u * math.cos(2 * math.pi * k / n) + w * math.sin(2 * math.pi * k / n)) * radii[i])
                      for k in range(n)])
    circ = 2 * math.pi * radii[0]
    for i in range(len(rings) - 1):
        v0, v1 = dist[i] / tile, dist[i + 1] / tile
        for k in range(n):
            j = (k + 1) % n
            u0, u1 = k / n * circ / tile, (k + 1) / n * circ / tile
            pm.face((rings[i][k], rings[i][j], rings[i + 1][j], rings[i + 1][k]),
                    [(u0, v0), (u1, v0), (u1, v1), (u0, v1)], m, True)
    if cap:
        tip = pm.vert(pts[-1] + (pts[-1] - pts[-2]).normalized() * radii[-1])
        vt = dist[-1] / tile
        for k in range(n):
            j = (k + 1) % n
            pm.face((rings[-1][k], rings[-1][j], tip),
                    [(k / n, vt), ((k + 1) / n, vt), ((k + 0.5) / n, vt + 0.05)], m, True)


def double_face(pm, pts, uvs, mat, normal, eps=0.0004):
    """A flat polygon visible from both sides (front + offset reversed back)."""
    m = pm.mat(mat)
    front = [pm.vert(p + normal * eps) for p in pts]
    back = [pm.vert(p - normal * eps) for p in pts]
    pm.face(front, uvs, m, False)
    pm.face(list(reversed(back)), list(reversed(uvs)), m, False)


def leaf(pm, base, axis, normal, length, width, mat, col, rng, curl=0.18):
    """Diamond/lanceolate leaf with a slight fold along the midrib."""
    axis = axis.normalized()
    side = normal.cross(axis).normalized()
    normal = axis.cross(side).normalized()
    widest = rng.uniform(0.3, 0.45)
    droop = normal * (-curl * length)                   # tip curls down a little
    p_tip = base + axis * length + droop
    p_l = base + axis * (length * widest) + side * (width / 2) - normal * (width * 0.12)
    p_r = base + axis * (length * widest) - side * (width / 2) - normal * (width * 0.12)
    u = (col + 0.5) / PALETTE_COLS
    double_face(pm, [base, p_l, p_tip, p_r],
                [(u, 0.05), (u - 0.02, 0.45), (u, 0.95), (u + 0.02, 0.45)], mat, normal)


def needle_tuft(pm, base, axis, length, count, mat, col, rng):
    """Pine needle fascicle bundle: thin triangles fanned around the shoot."""
    axis = axis.normalized()
    u = (col + 0.5) / PALETTE_COLS
    p = perpendicular(axis)
    for i in range(count):
        a = 2 * math.pi * i / count + rng.uniform(-0.3, 0.3)
        spread = rng.uniform(0.35, 0.7)
        d = (axis + (p * math.cos(a) + axis.cross(p) * math.sin(a)) * spread).normalized()
        side = d.cross(axis if abs(d.dot(axis)) < 0.99 else p).normalized() * 0.011
        tip = base + d * length * rng.uniform(0.8, 1.1)
        n = d.cross(side).normalized()
        double_face(pm, [base - side, base + side, tip], [(u - 0.01, 0.1), (u + 0.01, 0.1), (u, 0.95)], mat, n)


def flower(pm, centre, normal, radius, mat, col, rng, petals=5):
    normal = normal.normalized()
    t = perpendicular(normal)
    u = (col + 0.5) / PALETTE_COLS
    for i in range(petals):
        a = 2 * math.pi * i / petals + rng.uniform(-0.1, 0.1)
        d = (t * math.cos(a) + normal.cross(t) * math.sin(a))
        s = normal.cross(d)
        cup = normal * radius * 0.25
        pts = [centre, centre + d * radius * 0.45 + s * radius * 0.32 + cup * 0.5,
               centre + d * radius + cup, centre + d * radius * 0.45 - s * radius * 0.32 + cup * 0.5]
        double_face(pm, pts, [(u, 0.02), (u - 0.02, 0.85), (u, 0.98), (u + 0.02, 0.85)], mat, normal)


def rotate_towards(v, axis, angle):
    return (Matrix.Rotation(angle, 3, axis) @ v).normalized()


# --------------------------------------------------------------------------- #
# Recursive branching
# --------------------------------------------------------------------------- #

class Grow:
    """Parameters for a recursive tree/shrub skeleton."""

    def __init__(self, **kw):
        self.levels = 3
        self.children = [5, 4, 4]
        self.angle = [0.6, 0.7, 0.8]            # radians from parent
        self.len_ratio = [0.55, 0.5, 0.5]
        self.rad_ratio = [0.55, 0.55, 0.55]
        self.child_start = [0.35, 0.3, 0.2]
        self.segments = 6
        self.wiggle = 0.12
        self.tropism = 0.05                     # >0 grows up, <0 droops
        self.taper = 0.6
        self.bark = "M_Bark"
        self.bark_tile = 0.5
        self.leaf_levels = {3}                  # which depths carry leaves
        self.leaf_spacing = 0.05
        self.leaf_len = (0.05, 0.07)
        self.leaf_width = 0.45                  # fraction of length
        self.leaf_angle = (0.5, 1.0)
        self.leaf_mat = "M_Leaves"
        self.leaves_per_node = 2
        self.leaf_start = 0.25
        self.min_radius = 0.003
        self.cluster = 0                        # >0: rosette of this many leaves per node
        self.needles = 7
        self.__dict__.update(kw)


def branch(pm, start, direction, length, radius, depth, g, rng, stats):
    pts, radii = [start.copy()], [radius]
    d = direction.normalized()
    seg = length / g.segments
    for i in range(g.segments):
        wob = Vector((rng.gauss(0, 1), rng.gauss(0, 1), rng.gauss(0, 1))) * g.wiggle
        d = (d + wob + UP * g.tropism).normalized()
        pts.append(pts[-1] + d * seg)
        radii.append(max(g.min_radius, radius * (1 - (1 - g.taper) * (i + 1) / g.segments)))
    tube(pm, pts, radii, g.bark, g.bark_tile, cap=True)

    if depth in g.leaf_levels:
        _leaves_along(pm, pts, g, rng, stats)
    if depth >= g.levels:
        return
    n = g.children[depth]
    for c in range(n):
        t = rng.uniform(g.child_start[depth], 1.0)
        f = t * g.segments
        i = min(int(f), g.segments - 1)
        p = pts[i].lerp(pts[i + 1], f - i)
        pd = (pts[i + 1] - pts[i]).normalized()
        axis = rotate_towards(perpendicular(pd), pd, rng.uniform(0, 2 * math.pi) + c * 2.4)
        cd = rotate_towards(pd, axis, g.angle[depth] * rng.uniform(0.7, 1.3))
        cl = length * g.len_ratio[depth] * rng.uniform(0.8, 1.2) * (1.15 - 0.3 * t)
        cr = radii[i] * g.rad_ratio[depth]
        branch(pm, p, cd, cl, cr, depth + 1, g, rng, stats)


def _leaves_along(pm, pts, g, rng, stats):
    total = sum((b - a).length for a, b in zip(pts, pts[1:]))
    count = max(1, int(total / g.leaf_spacing))
    for k in range(count):
        t = g.leaf_start + (1 - g.leaf_start) * (k + rng.random()) / count
        f = t * (len(pts) - 1)
        i = min(int(f), len(pts) - 2)
        p = pts[i].lerp(pts[i + 1], f - i)
        d = (pts[i + 1] - pts[i]).normalized()
        if g.cluster:
            out = rotate_towards(perpendicular(d), d, rng.uniform(0, 2 * math.pi))
            nrm = (UP * 0.8 + out * 0.6 + d * 0.3).normalized()
            t0 = perpendicular(nrm)
            col = rng.randrange(PALETTE_COLS)
            for j in range(g.cluster):
                ang = 2 * math.pi * j / g.cluster + rng.uniform(-0.3, 0.3)
                ax = (rotate_towards(t0, nrm, ang) + nrm * 0.35).normalized()
                L = rng.uniform(*g.leaf_len)
                leaf(pm, p, ax, nrm, L, L * g.leaf_width, g.leaf_mat, col, rng)
                stats["leaves"] += 1
            continue
        for j in range(g.leaves_per_node):
            spin = rng.uniform(0, 2 * math.pi) + j * math.pi
            out = rotate_towards(perpendicular(d), d, spin)
            ax = rotate_towards(d, d.cross(out).normalized(), -rng.uniform(*g.leaf_angle))
            ax = (ax + UP * 0.15).normalized()
            nrm = (UP - ax * UP.dot(ax) + out * 0.3).normalized()
            L = rng.uniform(*g.leaf_len)
            if g.leaf_mat.endswith("Pine"):
                needle_tuft(pm, p, ax, L, g.needles, g.leaf_mat, rng.randrange(PALETTE_COLS), rng)
            else:
                leaf(pm, p, ax, nrm, L, L * g.leaf_width, g.leaf_mat, rng.randrange(PALETTE_COLS), rng)
            stats["leaves"] += 1


# --------------------------------------------------------------------------- #
# The plants
# --------------------------------------------------------------------------- #

def crape_myrtle():
    """Multi-trunk crape myrtle, vase-shaped, ~4 m, small oval leaves."""
    pm, rng, stats = PlantMesh(), random.Random(21), {"leaves": 0}
    g = Grow(levels=3, children=[5, 4, 4], angle=[0.7, 0.75, 0.8], len_ratio=[0.6, 0.55, 0.55],
             rad_ratio=[0.6, 0.55, 0.6], child_start=[0.45, 0.25, 0.2], wiggle=0.07, tropism=0.03,
             bark="M_BarkMyrtle", bark_tile=0.4, leaf_levels={2, 3}, leaf_spacing=0.03,
             leaf_len=(0.06, 0.09), leaf_width=0.5, leaf_mat="M_LeavesMyrtle", leaf_start=0.2,
             cluster=3)
    for i in range(7):
        a = 2 * math.pi * i / 7 + rng.uniform(-0.25, 0.25)
        base = Vector((math.cos(a) * 0.07, math.sin(a) * 0.07, -0.05))
        lean = rng.uniform(0.3, 0.5)
        d = Vector((math.cos(a) * lean, math.sin(a) * lean, 1.0))
        branch(pm, base, d, rng.uniform(2.0, 2.4), rng.uniform(0.035, 0.05), 0, g, rng, stats)
    return pm, stats


def azalea(seed=3, height=1.0, width=1.3):
    """Dense mounded evergreen azalea: short stems, leaf rosettes over a lumpy dome."""
    pm, rng, stats = PlantMesh(), random.Random(seed), {"leaves": 0}
    rx, rz = width / 2, height / 2
    centre = Vector((0, 0, rz * 0.95))
    off = Vector((seed * 3.1, seed * 1.7, 0.3))
    # stems from the base towards the shell
    for i in range(14):
        a = rng.uniform(0, 2 * math.pi)
        tgt = centre + Vector((math.cos(a) * rx * 0.6, math.sin(a) * rx * 0.6, rng.uniform(-0.1, 0.35)))
        pts = [Vector((rng.uniform(-0.05, 0.05), rng.uniform(-0.05, 0.05), -0.03))]
        for s in range(1, 5):
            pts.append(pts[0].lerp(tgt, s / 4) + Vector((rng.gauss(0, 0.02), rng.gauss(0, 0.02), 0)))
        tube(pm, pts, [0.012, 0.01, 0.008, 0.006, 0.004], "M_BarkDark", 0.3, sides=5)
    # leaf rosettes on a noisy ellipsoid shell (a few layers deep)
    n_ros = int(2600 * width * width)
    for _ in range(n_ros):
        z = rng.uniform(-0.35, 1.0)
        a = rng.uniform(0, 2 * math.pi)
        r = math.sqrt(max(0.0, 1 - z * z))
        dirn = Vector((r * math.cos(a), r * math.sin(a), z))
        lump = 1 + 0.18 * noise.noise(dirn * 2.2 + off) + 0.08 * noise.noise(dirn * 5 + off)
        depth = 1 - rng.random() ** 2 * 0.25
        p = centre + Vector((dirn.x * rx, dirn.y * rx, dirn.z * rz)) * lump * depth
        if p.z < 0.05:
            continue
        nrm = Vector((dirn.x / rx, dirn.y / rx, dirn.z / rz)).normalized()
        k = rng.randint(4, 6)
        t = perpendicular(nrm)
        col = rng.randrange(PALETTE_COLS)
        for j in range(k):
            ang = 2 * math.pi * j / k + rng.uniform(-0.3, 0.3)
            radial = rotate_towards(t, nrm, ang)
            ax = (radial + nrm * 0.45).normalized()
            L = rng.uniform(0.035, 0.055)
            leaf(pm, p, ax, nrm, L, L * 0.42, "M_LeavesAzalea", col, rng, curl=0.1)
            stats["leaves"] += 1
    return pm, stats


def urn_flowers():
    """Terracotta urn on a pedestal with a mounded vinca / bougainvillea planting."""
    pm, rng, stats = PlantMesh(), random.Random(4), {"leaves": 0}
    profile = [(0.0, 0.0), (0.17, 0.0), (0.17, 0.04), (0.14, 0.06), (0.08, 0.1), (0.07, 0.3),
               (0.12, 0.36), (0.2, 0.5), (0.24, 0.66), (0.22, 0.78), (0.26, 0.82), (0.26, 0.86),
               (0.22, 0.86), (0.2, 0.8)]
    sides = 40
    m = pm.mat("M_Terracotta")
    rings = [[pm.vert((r * math.cos(2 * math.pi * k / sides), r * math.sin(2 * math.pi * k / sides), z))
              for k in range(sides)] for r, z in profile]
    for i in range(len(rings) - 1):
        for k in range(sides):
            j = (k + 1) % sides
            pm.face((rings[i][k], rings[i][j], rings[i + 1][j], rings[i + 1][k]),
                    [(k / sides, i / 10), ((k + 1) / sides, i / 10), ((k + 1) / sides, (i + 1) / 10),
                     (k / sides, (i + 1) / 10)], m, True)
    soil = pm.vert((0, 0, 0.8))
    for k in range(sides):
        j = (k + 1) % sides
        pm.face((rings[-1][k], rings[-1][j], soil), [(0, 0), (1, 0), (0.5, 1)], pm.mat("M_Mulch"), True)
    centre = Vector((0, 0, 0.98))
    for _ in range(1400):
        z = rng.uniform(-0.45, 1.0)
        a = rng.uniform(0, 2 * math.pi)
        r = math.sqrt(1 - z * z)
        dirn = Vector((r * math.cos(a), r * math.sin(a), z))
        p = centre + Vector((dirn.x * 0.36, dirn.y * 0.36, dirn.z * 0.3)) * rng.uniform(0.8, 1.0)
        if dirn.z < -0.3:                                  # trailing over the rim
            p.z -= rng.uniform(0.0, 0.15)
        ax = (perpendicular(dirn) + dirn * 0.5)
        ax = rotate_towards(ax, dirn, rng.uniform(0, 6.28))
        L = rng.uniform(0.04, 0.06)
        leaf(pm, p, ax, dirn, L, L * 0.55, "M_LeavesVinca", rng.randrange(PALETTE_COLS), rng, curl=0.1)
        stats["leaves"] += 1
    for _ in range(170):
        z = rng.uniform(-0.1, 1.0)
        a = rng.uniform(0, 2 * math.pi)
        r = math.sqrt(1 - z * z)
        dirn = Vector((r * math.cos(a), r * math.sin(a), z))
        p = centre + Vector((dirn.x * 0.4, dirn.y * 0.4, dirn.z * 0.34))
        flower(pm, p, dirn, rng.uniform(0.022, 0.03), "M_Flowers", rng.randrange(PALETTE_COLS), rng)
    return pm, stats


def hardwood(seed=5, height=15.0):
    """Big southern hardwood (water/live oak): low fork, wide spreading crown."""
    pm, rng, stats = PlantMesh(), random.Random(seed), {"leaves": 0}
    g = Grow(levels=3, children=[6, 5, 4], angle=[0.85, 0.8, 0.85], len_ratio=[0.55, 0.5, 0.45],
             rad_ratio=[0.55, 0.5, 0.5], child_start=[0.45, 0.3, 0.25], segments=6, wiggle=0.09,
             tropism=0.025, bark="M_BarkOak", bark_tile=1.2, leaf_levels={2, 3}, leaf_spacing=0.075,
             leaf_len=(0.2, 0.28), leaf_width=0.5, leaf_mat="M_LeavesOak", cluster=7,
             leaf_start=0.15, min_radius=0.006)
    trunk_top = Vector((rng.uniform(-0.3, 0.3), rng.uniform(-0.3, 0.3), height * 0.3))
    tube(pm, [Vector((0, 0, -0.2)), Vector((0, 0, height * 0.12)), trunk_top],
         [0.45, 0.4, 0.34], "M_BarkOak", 1.2, sides=12, cap=False)
    for i in range(5):
        a = 2 * math.pi * i / 5 + rng.uniform(-0.3, 0.3)
        d = Vector((math.cos(a) * 1.0, math.sin(a) * 1.0, 1.0))
        branch(pm, trunk_top, d, height * rng.uniform(0.5, 0.6), 0.24, 0, g, rng, stats)
    return pm, stats


def pine(seed=6, height=21.0):
    """Loblolly pine: tall straight bole, short whorled branches, needle tufts up top."""
    pm, rng, stats = PlantMesh(), random.Random(seed), {"leaves": 0}
    top = Vector((rng.uniform(-0.3, 0.3), rng.uniform(-0.3, 0.3), height))
    pts = [Vector((0, 0, -0.2)).lerp(top, t / 8) for t in range(9)]
    tube(pm, pts, [0.3 - 0.27 * t / 8 for t in range(9)], "M_BarkPine", 1.0, sides=10)
    g = Grow(levels=2, children=[5, 5], angle=[0.8, 0.7], len_ratio=[0.45, 0.5], rad_ratio=[0.5, 0.5],
             child_start=[0.3, 0.3], segments=4, wiggle=0.08, tropism=0.04, bark="M_BarkPine",
             bark_tile=0.8, leaf_levels={1, 2}, leaf_spacing=0.055, leaf_len=(0.2, 0.26),
             leaf_mat="M_LeavesPine", leaves_per_node=1, leaf_start=0.3, min_radius=0.008,
             needles=9)
    whorls = 11
    for w in range(whorls):
        z = height * (0.6 + 0.38 * w / whorls)
        c = pts[0].lerp(top, (z + 0.2) / (height + 0.2))
        k = rng.randint(3, 5)
        for j in range(k):
            a = 2 * math.pi * j / k + rng.uniform(-0.5, 0.5)
            L = (1 - w / whorls) * 3.2 + 0.8
            d = Vector((math.cos(a), math.sin(a), rng.uniform(0.0, 0.35)))
            branch(pm, c, d, L * rng.uniform(0.8, 1.2), 0.07 * (1 - w / whorls) + 0.02, 0, g, rng, stats)
    return pm, stats

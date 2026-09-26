"""
build_house.py - build the house + front yard in Blender from measurements
taken off the photos, then export everything for Unreal Engine.

Run with Blender (4.2+):
    blender -b -P house/build_house.py
or with the bpy Python module:
    python house/build_house.py

Outputs (house/output/):
    MyHouse.blend              the Blender scene (textures referenced from house/textures)
    unreal/meshes/SM_*.fbx     one FBX per mesh, pivot at its base, textures embedded
    unreal/placements.json     where every object goes (Unreal units / axes)
    unreal/plant_swaps.json    edit to swap plants in bulk (see README)
    unreal/import_house.py     script to run inside Unreal
    MyHouse_Full.fbx / .glb    the whole scene in one file (for viewers / other tools)
    renders/*.png              preview renders

Coordinates in Blender: metres, X = right when facing the house, -Y = towards
the street, Z = up. The entry door wall is at Y = 0.
"""

import json
import math
import os
import random
import shutil
import sys

import bpy  # noqa: I001  (bpy must be imported before bmesh when run as a module)
import bmesh
from mathutils import Matrix, Vector, noise

HERE = os.path.dirname(os.path.abspath(__file__))
TEX = os.path.join(HERE, "textures")
OUT = os.path.join(HERE, "output")
RENDER = "--no-render" not in sys.argv
random.seed(8901)

# --------------------------------------------------------------------------- #
# Dimensions (metres) - estimated from the photos (2-car garage door = 4.9 m).
# --------------------------------------------------------------------------- #
WALL_H = 2.95          # top of brick
SLAB = 0.15            # visible slab above grade
PITCH = math.radians(26.6)  # 6/12 roof
OVERHANG = 0.40

WING = dict(x0=0.0, x1=4.3, y0=-2.4)          # bedroom wing that sticks out, left
ENTRY = dict(x0=4.3, x1=5.9)                    # recessed entry (stucco)
GARAGE = dict(x0=5.9, x1=12.7, y0=-1.6)         # 2-car garage block
BODY = dict(x0=0.0, x1=12.7, y0=0.0, y1=14.0)  # main house behind
NEIGHBOURS = [(-17.0, 1.0), (17.5, 0.5)]        # offsets of the neighbouring houses


# --------------------------------------------------------------------------- #
# Scene / materials
# --------------------------------------------------------------------------- #

def reset_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.unit_settings.system = "METRIC"
    scene.unit_settings.scale_length = 1.0
    return scene


def collection(name, parent=None):
    col = bpy.data.collections.new(name)
    (parent or bpy.context.scene.collection).children.link(col)
    return col


MATS = {}


def material(name, tex=None, rough=0.85, colour=None, metallic=0.0, tile=1.0):
    """Principled material, optionally textured. `tile` = metres per texture repeat."""
    if name in MATS:
        return MATS[name]
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    bsdf = nt.nodes["Principled BSDF"]
    bsdf.inputs["Roughness"].default_value = rough
    bsdf.inputs["Metallic"].default_value = metallic
    if tex:
        img = bpy.data.images.load(os.path.join(TEX, tex), check_existing=True)
        node = nt.nodes.new("ShaderNodeTexImage")
        node.image = img
        node.location = (-400, 200)
        nt.links.new(node.outputs["Color"], bsdf.inputs["Base Color"])
    elif colour:
        bsdf.inputs["Base Color"].default_value = (*colour, 1.0)
    m["tile"] = tile
    MATS[name] = m
    return m


def srgb(r, g, b):
    """0-255 sRGB -> linear floats for Blender colour inputs."""
    def f(c):
        c /= 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    return (f(r), f(g), f(b))


def build_materials():
    material("M_Brick", "T_Brick.png", 0.9, tile=1.1)
    material("M_Shingles", "T_Shingles.png", 0.8, tile=2.0)
    material("M_Stucco", "T_Stucco.png", 0.9, tile=2.0)
    material("M_Trim", colour=srgb(236, 229, 212), rough=0.6)
    material("M_Shutter", colour=srgb(38, 40, 42), rough=0.55)
    material("M_Window", "T_Window.png", 0.2, tile=0)
    material("M_FrontDoor", "T_FrontDoor.png", 0.35, tile=0)
    material("M_GarageDoor", "T_GarageDoor.png", 0.5, tile=0)
    material("M_Concrete", "T_Concrete.png", 0.9, tile=2.0)
    material("M_Asphalt", "T_Asphalt.png", 0.95, tile=3.0)
    material("M_Lawn", "T_Lawn.png", 0.95, tile=2.5)
    material("M_Mulch", "T_Mulch.png", 1.0, tile=1.2)
    material("M_RiverRock", "T_RiverRock.png", 0.8, tile=0.8)
    material("M_StoneBlock", "T_StoneBlock.png", 0.9, tile=0.6)
    material("M_BlackMetal", colour=srgb(25, 25, 25), rough=0.4, metallic=0.7)
    material("M_Glass", colour=srgb(230, 220, 170), rough=0.1)
    material("M_Bark", colour=srgb(120, 105, 88), rough=0.9)
    material("M_BarkDark", colour=srgb(70, 60, 52), rough=0.95)
    material("M_LeavesMyrtle", "T_LeavesMyrtle.png", 0.8, tile=1.0)
    material("M_LeavesAzalea", "T_LeavesAzalea.png", 0.8, tile=0.8)
    material("M_TreeCanopy", "T_TreeCanopy.png", 0.85, tile=3.0)
    material("M_Terracotta", colour=srgb(150, 100, 80), rough=0.8)
    material("M_FlowerPink", colour=srgb(222, 30, 110), rough=0.6)
    material("M_LeafGreen", colour=srgb(34, 78, 26), rough=0.7)


# --------------------------------------------------------------------------- #
# Mesh helpers
# --------------------------------------------------------------------------- #

class MeshBuilder:
    """Collects geometry in a bmesh with per-face materials, then makes an object."""

    def __init__(self):
        self.bm = bmesh.new()
        self.mats = []
        self.uv = self.bm.loops.layers.uv.new("UVMap")
        self.fixed_uv = {}   # face -> list of (u, v) for explicitly mapped faces

    def mat_index(self, mat_name):
        if mat_name not in self.mats:
            self.mats.append(mat_name)
        return self.mats.index(mat_name)

    def face(self, pts, mat, uvs=None):
        verts = [self.bm.verts.new(p) for p in pts]
        f = self.bm.faces.new(verts)
        f.material_index = self.mat_index(mat)
        if uvs:
            self.fixed_uv[f] = uvs
        return f

    def box(self, x0, x1, y0, y1, z0, z1, mat, skip=()):
        p = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
             (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
        faces = {
            "bottom": (3, 2, 1, 0), "top": (4, 5, 6, 7),
            "front": (0, 1, 5, 4), "back": (2, 3, 7, 6),
            "left": (3, 0, 4, 7), "right": (1, 2, 6, 5),
        }
        for k, idx in faces.items():
            if k not in skip:
                self.face([p[i] for i in idx], mat)

    def panel(self, origin, right, up, w, h, mat):
        """Flat textured quad (window, door ...) mapped 0..1."""
        o, r, u = Vector(origin), Vector(right).normalized() * w, Vector(up).normalized() * h
        self.face([o, o + r, o + r + u, o + u], mat, [(0, 0), (1, 0), (1, 1), (0, 1)])

    def finish(self, name, col, mesh_name=None, merge=True, smooth=False):
        bm = self.bm
        if merge:
            bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-4)
        bm.normal_update()
        for f in bm.faces:
            mat = MATS[self.mats[f.material_index]]
            if f in self.fixed_uv:
                for loop, uv in zip(f.loops, self.fixed_uv[f]):
                    loop[self.uv].uv = uv
                continue
            tile = mat.get("tile", 1.0) or 1.0
            n = f.normal
            if abs(n.z) > 0.999:
                t, b = Vector((1, 0, 0)), Vector((0, 1, 0))
            else:
                t = Vector((0, 0, 1)).cross(n).normalized()   # horizontal along the face
                b = n.cross(t).normalized()                   # up the face
            for loop in f.loops:
                p = loop.vert.co
                loop[self.uv].uv = (p.dot(t) / tile, p.dot(b) / tile)
        me = bpy.data.meshes.new(mesh_name or ("SM_" + name))
        bm.to_mesh(me)
        bm.free()
        for m in self.mats:
            me.materials.append(MATS[m])
        for poly in me.polygons:
            poly.use_smooth = smooth
        obj = bpy.data.objects.new(name, me)
        col.objects.link(obj)
        return obj


def hip_roof(mb, x0, x1, y0, y1, z, pitch=PITCH, fascia=0.22):
    """Hip roof over a rectangle (already including overhang)."""
    w, d = x1 - x0, y1 - y0
    h = min(w, d) / 2 * math.tan(pitch)
    zc = z + h
    if w >= d:
        r0 = (x0 + d / 2, (y0 + y1) / 2, zc)
        r1 = (x1 - d / 2 + 1e-3, (y0 + y1) / 2, zc)
    else:
        r0 = (x0 + w / 2, y0 + w / 2, zc)
        r1 = (x0 + w / 2, y1 - w / 2 + 1e-3, zc)
    e = [(x0, y0, z), (x1, y0, z), (x1, y1, z), (x0, y1, z)]
    b = [(x, y, z - fascia) for x, y, _ in e]
    if w >= d:
        mb.face([e[0], e[1], r1, r0], "M_Shingles")
        mb.face([e[2], e[3], r0, r1], "M_Shingles")
        mb.face([e[3], e[0], r0], "M_Shingles")
        mb.face([e[1], e[2], r1], "M_Shingles")
    else:
        mb.face([e[1], e[2], r1, r0], "M_Shingles")
        mb.face([e[3], e[0], r0, r1], "M_Shingles")
        mb.face([e[0], e[1], r0], "M_Shingles")
        mb.face([e[2], e[3], r1], "M_Shingles")
    for i in range(4):                                   # fascia boards
        j = (i + 1) % 4
        mb.face([b[i], b[j], e[j], e[i]], "M_Trim")
    mb.face([b[3], b[2], b[1], b[0]], "M_Trim")          # soffit


def smooth_curve(points, samples_per_seg=12, closed=False):
    """Catmull-Rom through 2D points."""
    pts = [Vector((p[0], p[1])) for p in points]
    n = len(pts)
    out = []
    segs = n if closed else n - 1
    for i in range(segs):
        p0 = pts[(i - 1) % n] if (closed or i > 0) else pts[0]
        p1, p2 = pts[i], pts[(i + 1) % n]
        p3 = pts[(i + 2) % n] if (closed or i + 2 < n) else pts[-1]
        for s in range(samples_per_seg):
            t = s / samples_per_seg
            t2, t3 = t * t, t * t * t
            out.append(0.5 * ((2 * p1) + (-p0 + p2) * t + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t2
                              + (-p0 + 3 * p1 - 3 * p2 + p3) * t3))
    if not closed:
        out.append(pts[-1])
    return out


# --------------------------------------------------------------------------- #
# House
# --------------------------------------------------------------------------- #

def build_house(col):
    objs = []
    z0, z1 = SLAB, WALL_H

    # --- brick walls -------------------------------------------------------
    walls = MeshBuilder()
    walls.box(BODY["x0"], BODY["x1"], BODY["y0"], BODY["y1"], 0, z1, "M_Brick", skip=("bottom", "top"))
    walls.box(WING["x0"], WING["x1"], WING["y0"], 0.02, 0, z1, "M_Brick", skip=("bottom", "top", "back"))
    walls.box(GARAGE["x0"], GARAGE["x1"], GARAGE["y0"], 0.02, 0, z1, "M_Brick", skip=("bottom", "top", "back"))
    # Brick rowlock sill under the front window.
    walls.box(1.55, 2.75, WING["y0"] - 0.06, WING["y0"], 0.88, 0.95, "M_Brick")
    objs.append(walls.finish("House_Walls", col))

    # --- stucco entry: recess walls + raised box over the door -------------
    st = MeshBuilder()
    ex0, ex1 = ENTRY["x0"], ENTRY["x1"]
    st.box(ex0, ex1, -0.02, 0.3, 0, 4.1, "M_Stucco", skip=("bottom", "back"))
    st.box(ex0 - 0.25, ex1 + 0.25, -1.2, 0.02, 3.35, 4.1, "M_Stucco", skip=("back",))  # header over porch
    st.box(ex0 - 0.25, ex1 + 0.25, -0.3, 4.0, z1, 4.1, "M_Stucco", skip=("bottom", "top", "front"))
    objs.append(st.finish("House_EntryStucco", col))

    # --- roofs ----------------------------------------------------------------
    rf = MeshBuilder()
    o = OVERHANG
    hip_roof(rf, BODY["x0"] - o, BODY["x1"] + o, BODY["y0"] - o, BODY["y1"] + o, z1)
    hip_roof(rf, WING["x0"] - o, WING["x1"] + o, WING["y0"] - o, 3.2, z1)
    hip_roof(rf, GARAGE["x0"] - o, GARAGE["x1"] + o, GARAGE["y0"] - o, 5.2, z1)
    hip_roof(rf, ex0 - 0.55, ex1 + 0.55, -1.55, 4.3, 4.1)       # raised entry roof
    objs.append(rf.finish("House_Roof", col, merge=False))

    # --- front details --------------------------------------------------------
    det = MeshBuilder()
    wy = WING["y0"] - 0.012
    wx = (WING["x0"] + WING["x1"]) / 2
    det.panel((wx - 0.5, wy, 0.95), (1, 0, 0), (0, 0, 1), 1.0, 1.75, "M_Window")
    for sx in (wx - 0.5 - 0.46, wx + 0.5 + 0.04):                     # shutters
        det.box(sx, sx + 0.42, wy - 0.04, wy, 0.92, 2.74, "M_Shutter")
    # Front door + transom, in the recess.
    det.panel((ex0 + 0.33, -0.012, SLAB), (1, 0, 0), (0, 0, 1), 0.94, 2.55, "M_FrontDoor")
    # Garage door.
    gx = (GARAGE["x0"] + GARAGE["x1"]) / 2
    det.panel((gx - 2.45, GARAGE["y0"] - 0.03, 0.02), (1, 0, 0), (0, 0, 1), 4.9, 2.15, "M_GarageDoor")
    det.box(gx - 2.55, gx + 2.55, GARAGE["y0"] - 0.05, GARAGE["y0"], 2.17, 2.27, "M_Trim")
    # Side and back windows so the model reads from every direction.
    for y in (4.0, 9.5):
        det.panel((-0.012, y + 1.0, 0.95), (0, -1, 0), (0, 0, 1), 1.0, 1.75, "M_Window")
    for x in (1.5, 4.0, 8.2):
        det.panel((x, BODY["y1"] + 0.012, 0.95), (1, 0, 0), (0, 0, 1), 1.0, 1.75, "M_Window")
    det.panel((10.4, BODY["y1"] + 0.012, SLAB), (1, 0, 0), (0, 0, 1), 0.94, 2.3, "M_FrontDoor")
    det.panel((BODY["x1"] + 0.012, 9.0, 0.95), (0, 1, 0), (0, 0, 1), 1.0, 1.75, "M_Window")
    objs.append(det.finish("House_Details", col, merge=False))

    # --- lanterns ---------------------------------------------------------------
    lan = MeshBuilder()
    for x, y in ((GARAGE["x0"] + 0.35, GARAGE["y0"]), (GARAGE["x1"] - 0.35, GARAGE["y0"])):
        lan.box(x - 0.03, x + 0.03, y - 0.1, y, 1.95, 2.25, "M_BlackMetal")
        lan.box(x - 0.12, x + 0.12, y - 0.34, y - 0.1, 1.85, 2.2, "M_Glass")
        lan.box(x - 0.15, x + 0.15, y - 0.37, y - 0.07, 2.2, 2.3, "M_BlackMetal")
    x = (ex0 + ex1) / 2                                            # pendant in entry
    lan.box(x - 0.1, x + 0.1, -0.7, -0.5, 2.55, 2.85, "M_Glass")
    lan.box(x - 0.12, x + 0.12, -0.72, -0.48, 2.85, 2.92, "M_BlackMetal")
    lan.box(x - 0.01, x + 0.01, -0.61, -0.59, 2.92, 3.35, "M_BlackMetal")
    objs.append(lan.finish("House_Lights", col, merge=False))

    # --- slab edge / foundation ----------------------------------------------------
    sl = MeshBuilder()
    sl.box(BODY["x0"] - 0.03, BODY["x1"] + 0.03, BODY["y0"] + 0.02, BODY["y1"] + 0.03, 0, SLAB, "M_Concrete")
    sl.box(WING["x0"] - 0.03, WING["x1"] + 0.03, WING["y0"] - 0.03, 0.1, 0, SLAB, "M_Concrete")
    sl.box(GARAGE["x0"] - 0.03, GARAGE["x1"] + 0.03, GARAGE["y0"] - 0.03, 0.1, 0, 0.03, "M_Concrete")
    objs.append(sl.finish("House_Slab", col))
    return objs


# --------------------------------------------------------------------------- #
# Yard
# --------------------------------------------------------------------------- #

# Flower bed outline, clockwise from where it meets the wing wall at the left.
BED = [(-0.3, -2.4), (-1.2, -2.6), (-1.9, -3.2), (-1.7, -4.0), (-0.6, -4.25),
       (0.5, -4.05), (1.3, -4.5), (2.5, -4.9), (3.7, -4.95), (4.25, -4.4),
       (4.25, -3.2), (4.25, -2.4)]


def build_yard(col):
    objs = []
    g = MeshBuilder()
    g.face([(-600, -13, 0), (600, -13, 0), (600, 600, 0), (-600, 600, 0)], "M_Lawn")
    g.face([(-600, -600, 0), (600, -600, 0), (600, -21, 0), (-600, -21, 0)], "M_Lawn")  # across the street
    objs.append(g.finish("Yard_Lawn", col))

    hard = MeshBuilder()
    dx0, dx1 = 6.4, 12.4
    hard.box(dx0, dx1, -13.0, GARAGE["y0"], -0.05, 0.03, "M_Concrete", skip=("bottom",))  # driveway
    hard.box(ENTRY["x0"], ENTRY["x1"], WING["y0"], -0.01, -0.05, 0.05, "M_Concrete", skip=("bottom",))  # porch
    hard.box(ENTRY["x0"], dx0 + 0.01, -3.3, WING["y0"] + 0.01, -0.05, 0.03, "M_Concrete", skip=("bottom",))  # walk
    for nx, ny in NEIGHBOURS:                                   # neighbours' driveways
        hard.box(dx0 + nx, dx1 + nx, -13.0, GARAGE["y0"] + ny, -0.05, 0.03, "M_Concrete", skip=("bottom",))
    hard.box(-600, 600, -14.0, -13.0, -0.05, 0.02, "M_Concrete", skip=("bottom",))   # curb / gutter
    objs.append(hard.finish("Yard_Driveway", col))

    st = MeshBuilder()
    st.box(-600, 600, -21.0, -14.0, -0.06, 0.0, "M_Asphalt", skip=("bottom",))
    objs.append(st.finish("Yard_Street", col))

    # --- flower bed: mulch mound + river-rock strip along the foundation ------
    outline = smooth_curve(BED, 10)
    bm = MeshBuilder()
    centre = sum(outline, Vector((0, 0))) / len(outline)
    rim = [(p.x, p.y, 0.03) for p in outline]
    for i in range(len(rim) - 1):
        c = (centre.x, centre.y, 0.12)
        bm.face([rim[i], rim[i + 1], c], "M_Mulch")
    bm.face([rim[-1], rim[0], (centre.x, centre.y, 0.12)], "M_Mulch")
    bm.box(WING["x0"] - 0.3, WING["x1"], WING["y0"] - 0.45, WING["y0"], 0.02, 0.1, "M_RiverRock", skip=("bottom",))
    objs.append(bm.finish("Yard_FlowerBed", col))

    # --- tumbled stone edging, two courses along the front edge ---------------
    border = smooth_curve(BED[1:-1], 24)
    lengths = [0.0]
    for a, b in zip(border, border[1:]):
        lengths.append(lengths[-1] + (b - a).length)
    edge = MeshBuilder()
    block = 0.30
    for course, z in ((0, 0.0), (1, 0.13)):
        s = block / 2 * course
        while s < lengths[-1] - block / 2:
            i = next(k for k in range(len(lengths)) if lengths[k] >= s) or 1
            p, q = border[i - 1], border[i]
            d = (q - p).normalized()
            n = Vector((-d.y, d.x))
            jitter = random.uniform(-0.02, 0.02)
            c = p + d * (s - lengths[i - 1]) + n * jitter
            ang = math.atan2(d.y, d.x) + random.uniform(-0.06, 0.06)
            _block(edge, c, ang, block - 0.02, 0.15, 0.13 - random.uniform(0, 0.02), z)
            s += block
    objs.append(edge.finish("Yard_StoneEdging", col, merge=False))
    return objs


def _block(mb, c, ang, lx, ly, lz, z):
    ca, sa = math.cos(ang), math.sin(ang)
    corners = []
    for dx, dy in ((-lx / 2, -ly / 2), (lx / 2, -ly / 2), (lx / 2, ly / 2), (-lx / 2, ly / 2)):
        corners.append((c.x + dx * ca - dy * sa, c.y + dx * sa + dy * ca))
    lo = [(x, y, z) for x, y in corners]
    hi = [(x, y, z + lz) for x, y in corners]
    mb.face([hi[0], hi[1], hi[2], hi[3]], "M_StoneBlock")
    for i in range(4):
        j = (i + 1) % 4
        mb.face([lo[i], lo[j], hi[j], hi[i]], "M_StoneBlock")


# --------------------------------------------------------------------------- #
# Plants - each type is one mesh with its pivot at ground level, so it can be
# swapped for any other plant in Blender (Object Data dropdown) or Unreal.
# --------------------------------------------------------------------------- #

def blob(mb, centre, radius, squash, mat, seed, rough=0.35, subdiv=3):
    bm = bmesh.new()
    bmesh.ops.create_icosphere(bm, subdivisions=subdiv, radius=1.0)
    off = Vector((seed * 13.1, seed * 7.7, seed * 3.3))
    verts = []
    for v in bm.verts:
        d = v.co.copy()
        k = (1 + rough * noise.noise(d * 1.7 + off) + 0.5 * rough * noise.noise(d * 4.1 + off)
             + 0.25 * rough * noise.noise(d * 9.0 + off))
        verts.append(Vector((d.x * radius * k + centre[0], d.y * radius * k + centre[1],
                             d.z * radius * squash * k + centre[2])))
    for f in bm.faces:
        mb.face([verts[v.index] for v in f.verts], mat)
    bm.free()


def trunk(mb, base, top, r0, r1, mat, sides=7):
    base, top = Vector(base), Vector(top)
    axis = (top - base).normalized()
    ref = Vector((1, 0, 0)) if abs(axis.x) < 0.9 else Vector((0, 1, 0))
    u = axis.cross(ref).normalized()
    v = axis.cross(u)
    ring0, ring1 = [], []
    for i in range(sides):
        a = 2 * math.pi * i / sides
        ring0.append(base + (u * math.cos(a) + v * math.sin(a)) * r0)
        ring1.append(top + (u * math.cos(a) + v * math.sin(a)) * r1)
    for i in range(sides):
        j = (i + 1) % sides
        mb.face([ring0[i], ring0[j], ring1[j], ring1[i]], mat)


def plant_crape_myrtle():
    mb = MeshBuilder()
    rng = random.Random(1)
    tops = []
    for i in range(7):                                    # multi-trunk, vase shape
        a = 2 * math.pi * i / 7 + rng.uniform(-0.2, 0.2)
        lean = rng.uniform(0.35, 0.7)
        top = (math.cos(a) * lean, math.sin(a) * lean, rng.uniform(1.7, 2.2))
        trunk(mb, (math.cos(a) * 0.08, math.sin(a) * 0.08, -0.05), top, 0.05, 0.03, "M_Bark")
        tops.append(top)
    for i, t in enumerate(tops):
        blob(mb, (t[0] * 1.3, t[1] * 1.3, t[2] + 0.55), rng.uniform(0.75, 0.95), 0.9,
             "M_LeavesMyrtle", i + 1)
    blob(mb, (0, 0, 3.1), 1.0, 0.8, "M_LeavesMyrtle", 9)
    blob(mb, (0.3, -0.2, 2.2), 0.9, 0.8, "M_LeavesMyrtle", 10)
    return mb


def plant_azalea(seed):
    mb = MeshBuilder()
    rng = random.Random(seed)
    for i in range(5):
        a = 2 * math.pi * i / 5
        r = 0.28
        blob(mb, (math.cos(a) * r, math.sin(a) * r, 0.42 + rng.uniform(-0.05, 0.08)),
             rng.uniform(0.38, 0.48), 0.8, "M_LeavesAzalea", seed * 10 + i, rough=0.3)
    blob(mb, (0, 0, 0.6), 0.45, 0.85, "M_LeavesAzalea", seed * 10 + 7, rough=0.3)
    return mb


def plant_urn_flowers():
    """Terracotta urn on a pedestal with a bougainvillea / vinca planting."""
    mb = MeshBuilder()
    profile = [(0.0, 0.0), (0.17, 0.0), (0.17, 0.05), (0.08, 0.1), (0.07, 0.3), (0.12, 0.36),
               (0.2, 0.5), (0.24, 0.66), (0.22, 0.78), (0.26, 0.82), (0.26, 0.86), (0.0, 0.86)]
    sides = 16
    rings = [[(r * math.cos(2 * math.pi * k / sides), r * math.sin(2 * math.pi * k / sides), z)
              for k in range(sides)] for r, z in profile]
    for a, b in zip(rings, rings[1:]):
        for k in range(sides):
            j = (k + 1) % sides
            if a[k] == a[j] and b[k] == b[j]:
                continue
            pts = [a[k], a[j], b[j], b[k]]
            uniq = [p for i, p in enumerate(pts) if p not in pts[:i]]
            if len(uniq) >= 3:
                mb.face(uniq, "M_Terracotta")
    rng = random.Random(4)
    blob(mb, (0, 0, 1.05), 0.34, 0.8, "M_LeafGreen", 21, rough=0.4)
    blob(mb, (0.15, 0.1, 1.2), 0.22, 1.0, "M_LeafGreen", 22, rough=0.4)
    for i in range(40):                                  # pink flower clusters
        a, e = rng.uniform(0, 2 * math.pi), rng.uniform(-0.2, 1.2)
        r = 0.36
        c = (math.cos(a) * math.cos(e) * r, math.sin(a) * math.cos(e) * r, 1.05 + math.sin(e) * r * 0.8)
        blob(mb, c, rng.uniform(0.035, 0.06), 1.0, "M_FlowerPink", 40 + i, rough=0.2, subdiv=1)
    return mb


def plant_oak(seed, height):
    """Spreading live-oak / water-oak shape: short trunk, limbs, wide clumpy crown."""
    mb = MeshBuilder()
    rng = random.Random(seed)
    top = Vector((rng.uniform(-0.4, 0.4), rng.uniform(-0.4, 0.4), height * 0.45))
    trunk(mb, (0, 0, -0.1), top, 0.38, 0.26, "M_BarkDark", 9)
    for i in range(22):
        a = rng.uniform(0, 2 * math.pi)
        r = height * 0.3 * math.sqrt(rng.random())
        z = height * (0.55 + 0.35 * (1 - r / (height * 0.3)) * rng.uniform(0.6, 1.0))
        c = (top.x + math.cos(a) * r, top.y + math.sin(a) * r, z)
        if i < 6:
            trunk(mb, top, c, 0.18, 0.08, "M_BarkDark", 6)
        blob(mb, c, height * rng.uniform(0.08, 0.12), 0.7, "M_TreeCanopy", seed * 30 + i, rough=0.45)
    return mb


def plant_pine(seed, height):
    """Loblolly pine: tall bare trunk, small irregular crown at the top."""
    mb = MeshBuilder()
    rng = random.Random(seed)
    top = Vector((rng.uniform(-0.3, 0.3), rng.uniform(-0.3, 0.3), height * 0.95))
    trunk(mb, (0, 0, -0.1), top, 0.26, 0.08, "M_BarkDark", 8)
    for i in range(14):
        z = height * rng.uniform(0.62, 0.97)
        spread = (1.0 - (z / height - 0.6)) * 2.2
        a = rng.uniform(0, 2 * math.pi)
        c = (math.cos(a) * spread * rng.random(), math.sin(a) * spread * rng.random(), z)
        blob(mb, c, height * rng.uniform(0.045, 0.07), 0.6, "M_TreeCanopy", seed * 50 + i, rough=0.5)
    return mb


def tree_line():
    """Woods behind and beside the lot, as in the photos."""
    rng = random.Random(77)
    oaks, pines = [], []
    for i in range(46):
        x = rng.uniform(-45, 55)
        y = rng.uniform(17, 45) if abs(x - 6) < 32 else rng.uniform(-10, 45)
        (oaks if rng.random() < 0.5 else pines).append(
            (round(x, 1), round(y, 1), rng.randint(0, 359), round(rng.uniform(0.85, 1.25), 2)))
    return oaks, pines


_OAKS, _PINES = tree_line()


# Where the plants go (x, y, yaw degrees, scale) - read off the photos.
PLANTS = {
    "CrapeMyrtle": {"make": plant_crape_myrtle, "at": [(-0.75, -3.25, 0, 1.0)]},
    "Azalea": {"make": lambda: plant_azalea(3), "at": [
        (3.55, -3.05, 0, 1.05), (3.05, -4.05, 70, 0.95), (2.1, -3.0, 140, 0.6),
        (3.85, -4.25, 200, 0.8)]},
    "UrnFlowers": {"make": plant_urn_flowers, "at": [(4.55, -2.15, 0, 1.0), (6.15, -1.95, 90, 1.1)]},
    "LiveOak": {"make": lambda: plant_oak(5, 14.0), "at": _OAKS},
    "Pine": {"make": lambda: plant_pine(6, 21.0), "at": _PINES},
}


def build_plants(col):
    placed = []
    for kind, spec in PLANTS.items():
        mb = spec["make"]()
        proto = mb.finish("SM_Plant_" + kind, col, mesh_name="SM_Plant_" + kind, merge=True,
                          smooth=True)
        me = proto.data
        bpy.data.objects.remove(proto)
        for i, (x, y, yaw, s) in enumerate(spec["at"], 1):
            obj = bpy.data.objects.new(f"Plant_{kind}_{i:02d}", me)   # shares the mesh
            obj.location = (x, y, 0)
            obj.rotation_euler = (0, 0, math.radians(yaw))
            obj.scale = (s, s, s)
            obj["plant_type"] = kind
            col.objects.link(obj)
            placed.append(obj)
    return placed


# --------------------------------------------------------------------------- #
# Lighting, cameras, renders
# --------------------------------------------------------------------------- #

def build_neighbours(col, house_objs):
    """Simple neighbouring houses (same builder plan) so the street feels right.
    Delete the Neighbours collection / folder if you don't want them."""
    placed = []
    for i, (dx, dy) in enumerate(NEIGHBOURS, 1):
        for src in house_objs:
            obj = bpy.data.objects.new(f"Neighbour{i}_{src.name}", src.data)
            obj.location = (dx, dy, 0)
            col.objects.link(obj)
            placed.append(obj)
    return placed


def setup_world(scene):
    world = bpy.data.worlds.new("Sky")
    scene.world = world
    world.use_nodes = True
    nt = world.node_tree
    sky = nt.nodes.new("ShaderNodeTexSky")
    sky.sky_type = "NISHITA"
    sky.sun_elevation = math.radians(28)
    sky.sun_rotation = math.radians(200)
    nt.links.new(sky.outputs["Color"], nt.nodes["Background"].inputs["Color"])
    nt.nodes["Background"].inputs["Strength"].default_value = 0.35
    sun = bpy.data.lights.new("Sun", "SUN")
    sun.energy = 3.2
    sun.angle = math.radians(1.0)
    sun_obj = bpy.data.objects.new("Sun", sun)
    sun_obj.rotation_euler = (math.radians(62), 0, math.radians(200))
    scene.collection.objects.link(sun_obj)
    scene.view_settings.view_transform = "AgX"
    scene.view_settings.look = "AgX - Medium High Contrast"
    scene.view_settings.exposure = -1.3


def add_camera(scene, name, loc, target, lens=24):
    cam = bpy.data.cameras.new(name)
    cam.lens = lens
    obj = bpy.data.objects.new(name, cam)
    obj.location = loc
    d = Vector(target) - Vector(loc)
    obj.rotation_euler = d.to_track_quat("-Z", "Y").to_euler()
    scene.collection.objects.link(obj)
    return obj


def render_previews(scene):
    os.makedirs(os.path.join(OUT, "renders"), exist_ok=True)
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = 48
    scene.cycles.use_denoising = True
    scene.render.resolution_x, scene.render.resolution_y = 1200, 900
    views = {
        "front": ((6.5, -17.5, 1.6), (6.2, 0, 2.2), 24),        # like photo 3
        "left_corner": ((-3.5, -11, 1.6), (5.5, -1, 1.6), 26),  # like photo 1
        "entry": ((1.0, -8.0, 1.6), (4.5, -1.5, 1.2), 26),      # like photo 2/4
        "aerial": ((-14, -24, 17), (6, 5, 0), 26),
        "back": ((20, 26, 5), (6, 7, 2), 24),
    }
    only = [a.split("=", 1)[1].split(",") for a in sys.argv if a.startswith("--views=")]
    for name, (loc, tgt, lens) in views.items():
        if only and name not in only[0]:
            continue
        scene.camera = add_camera(scene, "Cam_" + name, loc, tgt, lens)
        scene.render.filepath = os.path.join(OUT, "renders", name + ".png")
        bpy.ops.render.render(write_still=True)
        print("rendered", name)


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #

def to_unreal(loc):
    """Blender metres (right-handed, Z up) -> Unreal cm (left-handed, Z up)."""
    return [round(loc[0] * 100, 2), round(-loc[1] * 100, 2), round(loc[2] * 100, 2)]


def export_fbx(path, objects):
    bpy.ops.object.select_all(action="DESELECT")
    for o in objects:
        o.select_set(True)
    bpy.context.view_layer.objects.active = objects[0]
    bpy.ops.export_scene.fbx(
        filepath=path, use_selection=True, object_types={"MESH"},
        apply_scale_options="FBX_SCALE_UNITS", mesh_smooth_type="FACE",
        path_mode="COPY", embed_textures=True, add_leaf_bones=False, bake_anim=False)


def export_unreal(scene, groups):
    udir = os.path.join(OUT, "unreal")
    mdir = os.path.join(udir, "meshes")
    os.makedirs(mdir, exist_ok=True)

    tmp_col = bpy.data.collections.new("_export")
    scene.collection.children.link(tmp_col)
    placements, done = [], set()
    for category, objs in groups.items():
        for obj in objs:
            me = obj.data
            if me.name not in done:
                # Export the bare mesh at the origin so its pivot is preserved.
                tmp = bpy.data.objects.new(me.name, me)
                tmp_col.objects.link(tmp)
                export_fbx(os.path.join(mdir, me.name + ".fbx"), [tmp])
                bpy.data.objects.remove(tmp)
                done.add(me.name)
            placements.append({
                "name": obj.name, "mesh": me.name, "category": category,
                "location": to_unreal(obj.location),
                "yaw": round(-math.degrees(obj.rotation_euler.z), 2),
                "scale": round(obj.scale.x, 3),
                "collision": "simple" if category == "Plant" else "complex",
            })
    bpy.data.collections.remove(tmp_col)

    placements.append({"name": "PlayerStart", "category": "Spawn",
                       "location": to_unreal((9.3, -11.5, 1.0)), "yaw": -90.0})
    with open(os.path.join(udir, "placements.json"), "w") as f:
        json.dump({"units": "cm, Unreal axes", "objects": placements}, f, indent=1)

    swaps = os.path.join(udir, "plant_swaps.json")
    if not os.path.exists(swaps):
        with open(swaps, "w") as f:
            json.dump({
                "_help": "Put an Unreal asset path next to a plant to use it instead "
                         "(Content Browser > right-click mesh > Copy Reference). "
                         "Leave empty to keep the placeholder. Then re-run import_house.py.",
                "by_type": {"SM_Plant_" + k: "" for k in PLANTS},
                "by_object": {},
            }, f, indent=2)
    shutil.copy(os.path.join(HERE, "unreal", "import_house.py"), os.path.join(udir, "import_house.py"))

    everything = [o for objs in groups.values() for o in objs]
    export_fbx(os.path.join(OUT, "MyHouse_Full.fbx"), everything)
    bpy.ops.object.select_all(action="DESELECT")
    for o in everything:
        o.select_set(True)
    bpy.ops.export_scene.gltf(filepath=os.path.join(OUT, "MyHouse_Full.glb"),
                              use_selection=True, export_format="GLB")


def main():
    os.makedirs(OUT, exist_ok=True)
    scene = reset_scene()
    build_materials()
    house_col = collection("House")
    yard_col = collection("Yard")
    plant_col = collection("Plants")
    groups = {
        "House": build_house(house_col),
        "Yard": build_yard(yard_col),
        "Plant": build_plants(plant_col),
    }
    groups["Neighbour"] = build_neighbours(collection("Neighbours"), groups["House"])
    setup_world(scene)
    export_unreal(scene, groups)
    # Save the .blend with textures referenced relatively (house/textures).
    bpy.ops.file.make_paths_relative() if bpy.data.filepath else None
    blend = os.path.join(OUT, "MyHouse.blend")
    bpy.ops.wm.save_as_mainfile(filepath=blend, relative_remap=True)
    if RENDER:
        render_previews(scene)
        bpy.ops.wm.save_as_mainfile(filepath=blend, relative_remap=True)
    print("done ->", OUT)


main()

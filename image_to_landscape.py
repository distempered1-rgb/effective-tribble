#!/usr/bin/env python3
"""
image_to_landscape.py - turn a picture into an Unreal Engine landscape kit.

Given a single image (an aerial / satellite / drone shot, a painted map, or a
grayscale heightmap) this produces everything needed to rebuild the terrain in
Unreal Engine 5 with swappable plants:

  heightmap.png          16-bit grayscale heightmap at an Unreal-friendly size
  layers/<Layer>.png     8-bit paint-layer weightmaps (Grass, Forest, Rock, ...)
  foliage/<Type>.png     8-bit density masks for each plant/scatter category
  foliage_points.csv     every plant instance (position, rotation, scale)
  foliage_config.json    which mesh to use for each category  <-- edit to swap plants
  albedo.png             the source picture cropped/resized to the landscape
  water_mask.png         where the picture showed water
  manifest.json          the exact Unreal import settings
  preview.png            quick 3D preview of the result
  landscape.obj          (optional, --obj) mesh for Blender / other DCC tools

Usage:
  python image_to_landscape.py my_picture.jpg -o output/my_landscape
  python image_to_landscape.py dem.png --mode heightmap --height-range 800
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import shutil
import sys

import numpy as np
from PIL import Image
from scipy import ndimage

# Sizes Unreal recommends for a single landscape (quads fit whole components).
UE_SIZES = [127, 253, 505, 1009, 2017, 4033, 8129]

# Surface classes detected in the picture and the paint layer each one uses.
CLASSES = ["water", "sand", "grass", "forest", "dirt", "rock", "snow"]
LAYERS = ["Grass", "Forest", "Dirt", "Rock", "Sand", "Snow"]

# Relative elevation each surface tends to sit at in a top-down picture.
CLASS_HEIGHT = {
    "water": 0.00, "sand": 0.06, "grass": 0.22, "forest": 0.32,
    "dirt": 0.28, "rock": 0.62, "snow": 0.90,
}

# Plant / scatter categories. spacing_m = grid spacing of candidate spots,
# max_slope = steepest ground (degrees) the category may grow on.
FOLIAGE = {
    "Trees":  {"spacing_m": 6.0, "max_slope": 32.0, "scale": (0.8, 1.3)},
    "Bushes": {"spacing_m": 3.5, "max_slope": 38.0, "scale": (0.7, 1.3)},
    "Grass":  {"spacing_m": 2.0, "max_slope": 42.0, "scale": (0.7, 1.2)},
    "Rocks":  {"spacing_m": 9.0, "max_slope": 90.0, "scale": (0.5, 1.6)},
}

# Placeholder meshes that ship with every Unreal project, so the import works
# immediately. Replace them in foliage_config.json (or in the Foliage Type
# assets inside Unreal) with your own plants.
DEFAULT_MESHES = {
    "Trees":  ["/Engine/BasicShapes/Cone.Cone"],
    "Bushes": ["/Engine/BasicShapes/Sphere.Sphere"],
    "Grass":  ["/Engine/BasicShapes/Cylinder.Cylinder"],
    "Rocks":  ["/Engine/BasicShapes/Cube.Cube"],
}
PLACEHOLDER_SCALE = {"Trees": 4.0, "Bushes": 1.2, "Grass": 0.25, "Rocks": 0.8}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def smoothstep(e0: float, e1: float, x: np.ndarray) -> np.ndarray:
    t = np.clip((x - e0) / (e1 - e0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def normalize(a: np.ndarray) -> np.ndarray:
    lo, hi = float(a.min()), float(a.max())
    if hi - lo < 1e-9:
        return np.zeros_like(a)
    return (a - lo) / (hi - lo)


def blur(a: np.ndarray, sigma: float) -> np.ndarray:
    if sigma <= 0:
        return a
    return ndimage.gaussian_filter(a, sigma, mode="reflect")


def fbm_noise(size: int, rng: np.random.Generator, octaves: int = 6) -> np.ndarray:
    """Cheap fractal noise: sum of blurred white noise at several scales."""
    out = np.zeros((size, size), np.float64)
    amp, total = 1.0, 0.0
    sigma = size / 8.0
    for _ in range(octaves):
        layer = blur(rng.standard_normal((size, size)), sigma)
        layer /= layer.std() + 1e-9
        out += amp * layer
        total += amp
        amp *= 0.5
        sigma /= 2.0
        if sigma < 0.75:
            break
    return normalize(out / total)


def rgb_to_hsv(rgb: np.ndarray) -> np.ndarray:
    return np.asarray(Image.fromarray((rgb * 255).astype(np.uint8)).convert("HSV"),
                      np.float64) / 255.0


def pick_size(requested: int | None, image_size: tuple[int, int]) -> int:
    if requested:
        return requested
    longest = max(image_size)
    # The largest recommended size that doesn't massively upsample the image.
    best = UE_SIZES[0]
    for s in UE_SIZES:
        if s <= max(longest * 2, 505):
            best = s
    return min(best, 2017)


def load_square(path: str, size: int, fit: str) -> np.ndarray:
    img = Image.open(path)
    high_bit = img.mode.startswith("I") or img.mode == "F"  # 16/32-bit heightmaps
    w, h = img.size
    if fit == "crop" and w != h:
        s = min(w, h)
        left, top = (w - s) // 2, (h - s) // 2
        img = img.crop((left, top, left + s, top + s))
    if high_bit:
        arr = np.asarray(img, np.float64)
        arr = np.asarray(Image.fromarray(arr.astype(np.float32), "F")
                         .resize((size, size), Image.BICUBIC), np.float64)
        return normalize(arr)[..., None].repeat(3, axis=2)
    img = img.convert("RGB").resize((size, size), Image.LANCZOS)
    return np.asarray(img, np.float64) / 255.0


# --------------------------------------------------------------------------- #
# Surface classification (what is on the ground in each pixel)
# --------------------------------------------------------------------------- #

def classify_surfaces(rgb: np.ndarray, smooth_px: float) -> dict[str, np.ndarray]:
    """Soft per-pixel membership for each surface class, summing to 1."""
    hsv = rgb_to_hsv(rgb)
    hue = hsv[..., 0] * 360.0
    sat = hsv[..., 1]
    val = hsv[..., 2]
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]

    def hue_band(lo, hi, soft=12.0):
        return smoothstep(lo - soft, lo, hue) * (1.0 - smoothstep(hi, hi + soft, hue))

    green = hue_band(65, 170)
    yellow_brown = hue_band(18, 62)
    blue = hue_band(175, 250, soft=15)
    chroma = smoothstep(0.10, 0.28, sat)

    score = {
        "water": blue * smoothstep(0.08, 0.25, sat) * (1 - smoothstep(0.85, 1.0, val))
                 + 0.6 * smoothstep(0.02, 0.10, b - np.maximum(r, g)),
        "snow":  smoothstep(0.78, 0.92, val) * (1 - smoothstep(0.08, 0.20, sat)),
        "rock":  (1 - chroma) * smoothstep(0.18, 0.40, val) * (1 - smoothstep(0.80, 0.92, val)),
        "sand":  yellow_brown * chroma * smoothstep(0.50, 0.68, val),
        "dirt":  (yellow_brown * chroma * (1 - smoothstep(0.50, 0.68, val))
                  + 0.5 * (1 - chroma) * (1 - smoothstep(0.12, 0.22, val))),
        "grass": green * chroma * smoothstep(0.28, 0.42, val),
        "forest": green * smoothstep(0.06, 0.20, sat) * (1 - smoothstep(0.28, 0.42, val)),
    }
    # Olive / yellow-green belongs to grass rather than dirt.
    olive = hue_band(50, 75, soft=8) * chroma
    score["grass"] = score["grass"] + 0.5 * olive * smoothstep(0.30, 0.45, val)

    stack = np.stack([blur(score[c], smooth_px) for c in CLASSES]) + 1e-4
    stack /= stack.sum(axis=0, keepdims=True)
    return {c: stack[i] for i, c in enumerate(CLASSES)}


def classes_from_height(height: np.ndarray, slope_deg: np.ndarray) -> dict[str, np.ndarray]:
    """For heightmap input: derive surfaces from altitude and slope instead of colour."""
    h = height
    steep = smoothstep(28, 45, slope_deg)
    score = {
        "water": 1 - smoothstep(0.015, 0.04, h),
        "sand":  smoothstep(0.01, 0.04, h) * (1 - smoothstep(0.05, 0.09, h)),
        "grass": smoothstep(0.05, 0.09, h) * (1 - smoothstep(0.35, 0.50, h)),
        "forest": smoothstep(0.12, 0.22, h) * (1 - smoothstep(0.50, 0.65, h)) * 1.2,
        "dirt":  0.15 * np.ones_like(h),
        "rock":  smoothstep(0.55, 0.75, h) * 0.8,
        "snow":  smoothstep(0.75, 0.88, h),
    }
    stack = np.stack([score[c] for c in CLASSES]) + 1e-4
    stack[CLASSES.index("rock")] += steep * 3.0
    stack /= stack.sum(axis=0, keepdims=True)
    return {c: stack[i] for i, c in enumerate(CLASSES)}


# --------------------------------------------------------------------------- #
# Height estimation
# --------------------------------------------------------------------------- #

def height_from_topdown(rgb: np.ndarray, cls: dict[str, np.ndarray], relief: float,
                        detail: float, rng: np.random.Generator) -> np.ndarray:
    size = rgb.shape[0]
    base = sum(cls[c] * CLASS_HEIGHT[c] for c in CLASSES)

    # Land rises with distance from water, giving natural shorelines/valleys.
    water = cls["water"] > 0.5
    if water.any() and not water.all():
        dist = ndimage.distance_transform_edt(~water)
        shore = 1.0 - np.exp(-dist / (size / 8.0))
    else:
        shore = np.full_like(base, 0.5)

    # Rocky / snowy areas become mountains that peak towards their middle
    # instead of flat plateaus with cliff edges.
    highland = blur(cls["rock"] + cls["snow"], size / 200 + 1) > 0.5
    if highland.any():
        inside = ndimage.distance_transform_edt(highland)
        mountain = blur(1.0 - np.exp(-inside / (size / 10.0)), size / 60)
    else:
        mountain = np.zeros_like(base)

    lum = rgb @ np.array([0.299, 0.587, 0.114])
    noise = fbm_noise(size, rng)

    h = (0.30 * blur(base, size / 30)          # broad landforms from surface type
         + 0.08 * blur(base, size / 90)        # softer medium features
         + 0.40 * mountain
         + 0.18 * shore
         + 0.12 * relief * (noise - 0.5)       # hills that the picture can't show
         + 0.04 * detail * (blur(lum, 1.5) - blur(lum, 12)))  # fine surface texture

    # Keep water surfaces flat and slightly below the shore.
    wmask = blur(cls["water"], 2.0)
    shore_level = np.percentile(h[~water], 2) if (~water).any() else h.min()
    h = h * (1 - wmask) + (shore_level - 0.02) * wmask

    h = normalize(h)
    return normalize(h ** (1.0 + 0.6 * (relief - 1.0))) if relief != 1.0 else h


def height_from_depth_model(rgb: np.ndarray) -> np.ndarray:
    """Optional: use a monocular depth model (top-down/drone photos only)."""
    try:
        from transformers import pipeline  # type: ignore
    except ImportError:
        sys.exit("--mode depth needs:  pip install torch transformers")
    pipe = pipeline("depth-estimation", model="depth-anything/Depth-Anything-V2-Small-hf")
    img = Image.fromarray((rgb * 255).astype(np.uint8))
    depth = np.asarray(pipe(img)["depth"].resize(img.size, Image.BICUBIC), np.float64)
    # The model outputs "closer = brighter"; seen from above, closer = higher.
    return normalize(blur(depth, 1.0))


def slope_degrees(height01: np.ndarray, height_range_m: float, cell_m: float) -> np.ndarray:
    gy, gx = np.gradient(height01 * height_range_m, cell_m)
    return np.degrees(np.arctan(np.hypot(gx, gy)))


# --------------------------------------------------------------------------- #
# Paint layers and foliage
# --------------------------------------------------------------------------- #

def build_layers(cls: dict[str, np.ndarray], slope: np.ndarray) -> dict[str, np.ndarray]:
    steep = smoothstep(30, 50, slope)
    layers = {
        "Grass":  cls["grass"],
        "Forest": cls["forest"],
        "Dirt":   cls["dirt"] + 0.5 * cls["water"],   # riverbeds / lake floors
        "Rock":   cls["rock"],
        "Sand":   cls["sand"] + 0.5 * cls["water"],
        "Snow":   cls["snow"],
    }
    for k in layers:
        layers[k] = layers[k] * (1 - steep)
    layers["Rock"] = layers["Rock"] + steep
    stack = np.stack([layers[k] for k in LAYERS])
    stack /= stack.sum(axis=0, keepdims=True) + 1e-9
    return {k: stack[i] for i, k in enumerate(LAYERS)}


def build_foliage_density(cls: dict[str, np.ndarray], slope: np.ndarray,
                          density: float) -> dict[str, np.ndarray]:
    forest = cls["forest"]
    edge = np.clip(blur(forest, 6) - forest, 0, 1) * 3.0  # forest margins
    dens = {
        "Trees":  forest * 0.85 + cls["grass"] * 0.02,
        "Bushes": forest * 0.25 + edge * 0.6 + cls["grass"] * 0.08 + cls["dirt"] * 0.05,
        "Grass":  cls["grass"] * 0.9 + forest * 0.25 + cls["dirt"] * 0.15,
        "Rocks":  cls["rock"] * 0.35 + smoothstep(25, 45, slope) * 0.25 + cls["dirt"] * 0.03,
    }
    no_water = 1 - smoothstep(0.3, 0.6, blur(cls["water"], 1.5))
    no_snow = 1 - smoothstep(0.4, 0.7, cls["snow"])
    out = {}
    for name, d in dens.items():
        slope_ok = 1 - smoothstep(FOLIAGE[name]["max_slope"] - 5, FOLIAGE[name]["max_slope"], slope)
        d = d * no_water * slope_ok * (no_snow if name != "Rocks" else 1.0)
        out[name] = np.clip(d * density, 0, 1)
    return out


def bilinear(a: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    return ndimage.map_coordinates(a, [y, x], order=1, mode="nearest")


def scatter(dens: dict[str, np.ndarray], height16: np.ndarray, cell_m: float,
            spacing_mult: float, rng: np.random.Generator) -> list[dict]:
    size = height16.shape[0]
    rows = []
    for name, d in dens.items():
        spec = FOLIAGE[name]
        step = max(spec["spacing_m"] * spacing_mult / cell_m, 0.25)  # in pixels
        n = int((size - 1) / step)
        gx, gy = np.meshgrid(np.arange(n), np.arange(n))
        x = (gx + rng.random(gx.shape)) * step
        y = (gy + rng.random(gy.shape)) * step
        x, y = x.ravel(), y.ravel()
        keep = rng.random(x.shape) < bilinear(d, x, y)
        x, y = x[keep], y[keep]
        h = bilinear(height16, x, y)
        lo, hi = spec["scale"]
        scale = rng.uniform(lo, hi, x.shape)
        yaw = rng.uniform(0, 360, x.shape)
        variant = rng.random(x.shape)
        for i in range(x.size):
            rows.append({
                "category": name,
                "col": round(float(x[i]), 3), "row": round(float(y[i]), 3),
                "height16": round(float(h[i]), 2),
                "yaw": round(float(yaw[i]), 1), "scale": round(float(scale[i]), 3),
                "variant": round(float(variant[i]), 4),
            })
    return rows


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #

def save_gray8(path: str, a: np.ndarray) -> None:
    Image.fromarray(np.clip(a * 255 + 0.5, 0, 255).astype(np.uint8), "L").save(path)


def save_gray16(path: str, a16: np.ndarray) -> None:
    Image.fromarray(a16.astype(np.uint16)).save(path)


def write_obj(path: str, height01: np.ndarray, cell_m: float, range_m: float,
              max_res: int = 513) -> None:
    step = max(1, int(math.ceil(height01.shape[0] / max_res)))
    h = height01[::step, ::step]
    n = h.shape[0]
    with open(path, "w") as f:
        f.write("# image_to_landscape terrain (Y up, metres)\n")
        for r in range(n):
            for c in range(n):
                f.write(f"v {c*step*cell_m:.2f} {h[r, c]*range_m:.3f} {r*step*cell_m:.2f}\n")
        for r in range(n):
            for c in range(n):
                f.write(f"vt {c/(n-1):.5f} {1 - r/(n-1):.5f}\n")
        for r in range(n - 1):
            for c in range(n - 1):
                a = r * n + c + 1
                b, d, e = a + 1, a + n, a + n + 1
                f.write(f"f {a}/{a} {d}/{d} {b}/{b}\nf {b}/{b} {d}/{d} {e}/{e}\n")


def hillshade(height_m: np.ndarray, cell_m: float, az: float = 315, alt: float = 40) -> np.ndarray:
    gy, gx = np.gradient(height_m, cell_m)
    slope = np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)
    az, alt = np.radians(360 - az + 90), np.radians(alt)
    shade = np.sin(alt) * np.cos(slope) + np.cos(alt) * np.sin(slope) * np.cos(az - aspect)
    return np.clip(shade, 0, 1)


def render_oblique(height_m: np.ndarray, colour: np.ndarray, cell_m: float,
                   width: int = 900, height: int = 560, tilt_deg: float = 35) -> np.ndarray:
    """Tiny front-to-back height-field renderer (bird's-eye view from the south)."""
    n = height_m.shape[0]
    step = max(1, n // 450)
    hm, col = height_m[::step, ::step], colour[::step, ::step]
    m = hm.shape[0]
    t = math.radians(tilt_deg)
    px = width / m                                  # screen px per map cell
    ground = px * math.sin(t)                       # screen px per row of depth
    lift = px / (cell_m * step) * math.cos(t)       # screen px per metre of height
    top = (hm.max() - hm.min()) * lift
    y0 = height - m * ground - 20                   # far edge of the map
    y0 = max(y0, top + 10)
    img = np.ones((height, width, 3)) * np.array([0.78, 0.86, 0.95])  # sky
    ybuf = np.full(width, height, np.int64)
    xs = np.minimum((np.arange(width) / px).astype(int), m - 1)
    hmin = hm.min()
    for r in range(m - 1, -1, -1):                  # near rows first
        sy = (y0 + r * ground - (hm[r, xs] - hmin) * lift).astype(np.int64)
        sy = np.clip(sy, 0, height)
        c = col[r, xs]
        for x in np.nonzero(sy < ybuf)[0]:
            img[sy[x]:ybuf[x], x] = c[x]
        ybuf = np.minimum(ybuf, sy)
    return img


def render_preview(path: str, height01: np.ndarray, albedo: np.ndarray,
                   rows: list[dict], range_m: float, cell_m: float) -> None:
    from PIL import ImageDraw
    height_m = height01 * range_m
    shade = hillshade(height_m, cell_m)[..., None]
    lit = np.clip(albedo * (0.35 + 0.85 * shade), 0, 1)
    view = render_oblique(height_m, lit, cell_m)

    # Top-down panel: shaded terrain with plant positions.
    side = view.shape[0]
    top = Image.fromarray((np.clip(0.55 * lit + 0.45 * shade, 0, 1) * 255).astype(np.uint8))
    top = top.resize((side, side), Image.BILINEAR)
    draw = ImageDraw.Draw(top)
    k = side / height01.shape[0]
    colours = {"Grass": (200, 210, 60), "Rocks": (230, 230, 240),
               "Bushes": (120, 190, 60), "Trees": (10, 70, 20)}
    rng = np.random.default_rng(0)
    for name in ["Grass", "Rocks", "Bushes", "Trees"]:
        pts = [(r["col"], r["row"]) for r in rows if r["category"] == name]
        if len(pts) > 5000:
            pts = [pts[i] for i in rng.choice(len(pts), 5000, replace=False)]
        rad = 1 if name == "Grass" else 2
        for x, y in pts:
            draw.ellipse((x * k - rad, y * k - rad, x * k + rad, y * k + rad), fill=colours[name])
    draw.rectangle((side - 98, 4, side - 4, 82), fill=(255, 255, 255), outline=(0, 0, 0))
    for i, name in enumerate(["Trees", "Bushes", "Grass", "Rocks"]):
        y = 10 + i * 18
        draw.rectangle((side - 90, y, side - 78, y + 12), fill=colours[name], outline=(0, 0, 0))
        draw.text((side - 72, y), name, fill=(0, 0, 0))

    canvas = Image.new("RGB", (view.shape[1] + side + 10, side), (255, 255, 255))
    canvas.paste(Image.fromarray((view * 255).astype(np.uint8)), (0, 0))
    canvas.paste(top, (view.shape[1] + 10, 0))
    canvas.save(path)


def default_foliage_config() -> dict:
    return {
        "_help": ("Map each category to one or more Static Mesh asset paths "
                  "(right-click a mesh in the Content Browser > Copy Reference). "
                  "Several meshes = random mix. When you swap in real plants set "
                  "scale_multiplier to 1 and mesh_pivot_offset_cm to 0 (plant "
                  "meshes normally have their pivot at the base). Re-run unreal/import_foliage.py "
                  "after editing. You can also just open the FT_* Foliage Type "
                  "assets in Unreal and change their Mesh."),
        "categories": {
            name: {
                "meshes": DEFAULT_MESHES[name],
                "scale_multiplier": PLACEHOLDER_SCALE[name],
                "mesh_pivot_offset_cm": 25 if name == "Bushes" else 50,
                "random_yaw": True,
                "align_to_normal": name in ("Grass", "Rocks"),
                "cull_distance_cm": {"Trees": 0, "Bushes": 20000,
                                     "Grass": 6000, "Rocks": 15000}[name],
                "cast_shadow": name != "Grass",
                "enabled": True,
            } for name in FOLIAGE
        },
    }


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("image", help="input picture (jpg/png/tif)")
    p.add_argument("-o", "--out", help="output folder (default: output/<image name>)")
    p.add_argument("--mode", choices=["topdown", "heightmap", "depth"], default="topdown",
                   help="topdown: aerial/satellite/map picture (default); "
                        "heightmap: image already is a grayscale heightmap; "
                        "depth: top-down photo through an AI depth model")
    p.add_argument("--size", type=int, choices=UE_SIZES,
                   help="landscape resolution in vertices (default: picked from image)")
    p.add_argument("--fit", choices=["crop", "stretch"], default="crop",
                   help="how to make a non-square picture square")
    p.add_argument("--world-size", type=float, default=None,
                   help="width of the landscape in metres (default: 1 m per vertex)")
    p.add_argument("--height-range", type=float, default=150.0,
                   help="metres between the lowest and highest point (default 150)")
    p.add_argument("--relief", type=float, default=1.0,
                   help="extra hilliness added to top-down pictures (0-3, default 1)")
    p.add_argument("--detail", type=float, default=1.0,
                   help="fine surface detail taken from the picture (0-3, default 1)")
    p.add_argument("--smooth", type=float, default=1.0,
                   help="terrain smoothing strength (default 1)")
    p.add_argument("--foliage-density", type=float, default=1.0,
                   help="multiplier on how many plants are placed (default 1)")
    p.add_argument("--foliage-spacing", type=float, default=1.0,
                   help="multiplier on plant spacing; >1 = fewer, sparser plants")
    p.add_argument("--seed", type=int, default=1234)
    p.add_argument("--obj", action="store_true", help="also export landscape.obj")
    p.add_argument("--no-preview", action="store_true")
    a = p.parse_args(argv)

    rng = np.random.default_rng(a.seed)
    with Image.open(a.image) as im:
        src_size = im.size
    size = pick_size(a.size, src_size)
    world_m = a.world_size or float(size - 1)
    cell_m = world_m / (size - 1)
    out = a.out or os.path.join("output", os.path.splitext(os.path.basename(a.image))[0])
    os.makedirs(os.path.join(out, "layers"), exist_ok=True)
    os.makedirs(os.path.join(out, "foliage"), exist_ok=True)

    print(f"[1/6] Loading {a.image} ({src_size[0]}x{src_size[1]}) -> {size}x{size}")
    rgb = load_square(a.image, size, a.fit)
    smooth_px = max(0.5, size / 500) * a.smooth

    print(f"[2/6] Building terrain ({a.mode} mode)")
    if a.mode == "heightmap":
        height = normalize(blur(rgb.mean(axis=2), 0.6 * a.smooth))
        slope = slope_degrees(height, a.height_range, cell_m)
        cls = classes_from_height(height, slope)
        albedo = None
    else:
        cls = classify_surfaces(rgb, smooth_px)
        if a.mode == "depth":
            height = height_from_depth_model(rgb)
        else:
            height = height_from_topdown(rgb, cls, a.relief, a.detail, rng)
        height = normalize(blur(height, smooth_px))
        slope = slope_degrees(height, a.height_range, cell_m)
        albedo = rgb

    # 16-bit heightmap using the full range for maximum precision.
    height16 = np.round(height * 65535.0)
    z_scale = a.height_range * 100.0 / 512.0  # Unreal: 512 * ZScale cm spans 0..65535
    xy_scale = cell_m * 100.0

    print("[3/6] Painting material layers")
    layers = build_layers(cls, slope)
    print("[4/6] Placing plants")
    dens = build_foliage_density(cls, slope, a.foliage_density)
    rows = scatter(dens, height16, cell_m, a.foliage_spacing, rng)

    print(f"[5/6] Writing files to {out}")
    save_gray16(os.path.join(out, "heightmap.png"), height16)
    # Quantise so every pixel's layer weights add up to exactly 255.
    q = np.round(np.stack([layers[k] for k in LAYERS]) * 255).astype(np.int32)
    top_layer = q.argmax(axis=0)
    fix = 255 - q.sum(axis=0)
    np.put_along_axis(q, top_layer[None], np.take_along_axis(q, top_layer[None], 0) + fix, 0)
    for i, k in enumerate(LAYERS):
        Image.fromarray(np.clip(q[i], 0, 255).astype(np.uint8), "L").save(
            os.path.join(out, "layers", f"{k}.png"))
    for k, v in dens.items():
        save_gray8(os.path.join(out, "foliage", f"{k}.png"), v)
    save_gray8(os.path.join(out, "water_mask.png"), smoothstep(0.4, 0.6, cls["water"]))
    if albedo is None:
        # Tint a heightmap-only landscape by its layers so there's still a colour map.
        tint = {"Grass": (0.35, 0.50, 0.20), "Forest": (0.15, 0.30, 0.12),
                "Dirt": (0.40, 0.32, 0.22), "Rock": (0.50, 0.49, 0.47),
                "Sand": (0.76, 0.70, 0.50), "Snow": (0.95, 0.95, 0.97)}
        albedo = sum(layers[k][..., None] * np.array(tint[k]) for k in LAYERS)
    Image.fromarray((np.clip(albedo, 0, 1) * 255).astype(np.uint8)).save(
        os.path.join(out, "albedo.png"))

    fields = ["category", "col", "row", "height16", "x_cm", "y_cm", "z_cm",
              "yaw", "scale", "variant"]
    with open(os.path.join(out, "foliage_points.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            # Absolute position assuming the landscape actor sits at 0,0,0.
            r["x_cm"] = round(r["col"] * xy_scale, 1)
            r["y_cm"] = round(r["row"] * xy_scale, 1)
            r["z_cm"] = round((r["height16"] - 32768.0) / 128.0 * z_scale, 1)
            w.writerow(r)

    cfg_path = os.path.join(out, "foliage_config.json")
    if not os.path.exists(cfg_path):  # never clobber the user's plant choices
        with open(cfg_path, "w") as f:
            json.dump(default_foliage_config(), f, indent=2)

    counts = {k: sum(1 for r in rows if r["category"] == k) for k in FOLIAGE}
    manifest = {
        "source_image": os.path.basename(a.image),
        "mode": a.mode,
        "resolution": size,
        "world_size_m": round(world_m, 2),
        "height_range_m": a.height_range,
        "unreal": {
            "heightmap_file": "heightmap.png",
            "landscape_scale": {"x": round(xy_scale, 4), "y": round(xy_scale, 4),
                                "z": round(z_scale, 4)},
            "section_size": "63x63 quads" if size <= 1009 else "127x127 quads",
            "sections_per_component": "1x1" if size <= 1009 else "2x2",
            "paint_layers": {k: f"layers/{k}.png" for k in LAYERS},
        },
        "foliage_counts": counts,
        "seed": a.seed,
    }
    with open(os.path.join(out, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)

    here = os.path.dirname(os.path.abspath(__file__))
    shutil.copy(os.path.join(here, "unreal", "import_foliage.py"),
                os.path.join(out, "import_foliage.py"))

    if a.obj:
        write_obj(os.path.join(out, "landscape.obj"), height, cell_m, a.height_range)
    if not a.no_preview:
        print("[6/6] Rendering preview")
        render_preview(os.path.join(out, "preview.png"), height, albedo, rows,
                       a.height_range, cell_m)

    print("\nDone.")
    print(f"  Landscape: {size}x{size} vertices, {world_m:.0f} m wide, "
          f"{a.height_range:.0f} m tall")
    print(f"  Unreal scale: X={xy_scale:g}  Y={xy_scale:g}  Z={z_scale:g}")
    print("  Plants: " + ", ".join(f"{k} {v:,}" for k, v in counts.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

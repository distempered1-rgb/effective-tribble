"""
make_plant_textures.py - colour palettes and bark textures for the geometric plants.

Leaves/petals are real geometry (Nanite-friendly, no alpha cut-outs). Each leaf
samples one column of a small palette texture (random colour variation) and
runs base -> tip down the rows (darker base, lighter tip). Colours were picked
from the photos.

    python house/make_plant_textures.py
"""

import os

import numpy as np
from PIL import Image
from scipy import ndimage

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "textures")
rng = np.random.default_rng(11)

# name: (base colour, tip colour, [per-column tints]) - sRGB
PALETTES = {
    "Myrtle": ((44, 78, 28), (96, 140, 50), [1.0, 0.9, 1.1, 0.95, 1.05, 0.85, 1.15, 1.0]),
    "Azalea": ((52, 76, 30), (126, 146, 58), [1.0, 0.85, 1.1, 0.95, 1.05, 0.8, 1.15, 0.9]),
    "Oak":    ((34, 52, 26), (78, 100, 48), [1.0, 0.9, 1.1, 0.8, 1.05, 0.95, 1.2, 0.85]),
    "Pine":   ((44, 58, 32), (92, 104, 56), [1.0, 0.9, 1.1, 0.95, 1.05, 0.85, 1.0, 0.9]),
    "Vinca":  ((32, 72, 28), (66, 120, 50), [1.0, 0.9, 1.1, 0.95, 1.05, 0.85, 1.15, 1.0]),
    "Flower": ((240, 120, 180), (190, 0, 88), [1.0, 0.92, 1.05, 0.88, 1.0, 0.95, 1.08, 0.9]),
}


def palette(base, tip, tints, cols=8, colw=8, h=64):
    img = np.zeros((h, cols * colw, 3))
    t = np.linspace(0, 1, h)[:, None]
    grad = np.array(base)[None] * (1 - t) + np.array(tip)[None] * t
    for c, k in enumerate(tints):
        hue_shift = rng.normal(0, 4, 3)
        img[:, c * colw:(c + 1) * colw] = (grad * k + hue_shift)[:, None, :]
    return Image.fromarray(np.clip(img, 0, 255).astype(np.uint8))


def fbm(size, sigmas, weights, wrap=True):
    out = np.zeros((size, size))
    for s, w in zip(sigmas, weights):
        n = ndimage.gaussian_filter(rng.standard_normal((size, size)), s, mode="wrap" if wrap else "reflect")
        out += w * n / (n.std() + 1e-9)
    return out


def bark_myrtle(size=512):
    """Crape myrtle: smooth, mottled tan / cinnamon / grey exfoliating patches."""
    n1 = fbm(size, [18, 6], [1, 0.4])
    n2 = fbm(size, [10, 3], [1, 0.3])
    tan, cinnamon, grey = np.array([176, 150, 120]), np.array([150, 104, 74]), np.array([150, 146, 138])
    a = np.where((n1 > 0.5)[..., None], cinnamon, tan).astype(float)
    a = np.where((n2 > 0.9)[..., None], grey, a)
    a += fbm(size, [1.2], [1])[..., None] * 5
    a = ndimage.gaussian_filter(a, (1.2, 1.2, 0))
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))


def bark_furrowed(size, base, dark, stretch):
    """Oak / pine bark: vertical furrows (v runs up the trunk) and plates."""
    n = ndimage.gaussian_filter(rng.standard_normal((size, size)), (stretch, 3), mode="wrap")
    n = n / n.std()
    ridges = np.abs(np.sin(n * 2.4))
    a = np.array(dark)[None, None] * (1 - ridges[..., None]) + np.array(base)[None, None] * ridges[..., None]
    a += fbm(size, [1.0], [1])[..., None] * 8
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))


def main():
    os.makedirs(OUT, exist_ok=True)
    for name, (b, t, tints) in PALETTES.items():
        palette(b, t, tints).save(os.path.join(OUT, f"T_Palette_{name}.png"))
    bark_myrtle().save(os.path.join(OUT, "T_Bark_Myrtle.png"))
    bark_furrowed(512, (96, 86, 76), (40, 34, 30), 14).save(os.path.join(OUT, "T_Bark_Oak.png"))
    bark_furrowed(512, (132, 92, 70), (52, 40, 34), 6).save(os.path.join(OUT, "T_Bark_Pine.png"))
    print("plant textures written to", OUT)


if __name__ == "__main__":
    main()

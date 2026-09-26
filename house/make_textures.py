"""
make_textures.py - build tileable textures for the house model from the photos.

Photo crops give the real brick / lawn / mulch / leaf look; clean things that
the photos only show partly or with stuff in front (garage door, front door,
roof shingles, stone edging) are drawn procedurally in the photographed colours.

  python house/make_textures.py <folder with 1.jpg..4.jpg>

The photos themselves are not needed after this and are not stored in the repo.
"""

import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "textures")
rng = np.random.default_rng(3)

# (photo, crop box) for each photographic texture.
CROPS = {
    "brick":  ("4.jpg", (725, 395, 885, 560)),
    "lawn":   ("3.jpg", (20, 880, 480, 1110)),
    "mulch":  ("1.jpg", (850, 800, 990, 900)),
    "leaves_myrtle": ("1.jpg", (40, 300, 360, 560)),
    "leaves_azalea": ("2.jpg", (420, 620, 760, 820)),
}


def seamless(img: Image.Image, size: int) -> Image.Image:
    """Make a photo crop tile without visible seams (offset + feathered blend)."""
    a = np.asarray(img.convert("RGB").resize((size, size), Image.LANCZOS), np.float64)
    rolled = np.roll(np.roll(a, size // 2, 0), size // 2, 1)
    t = np.linspace(-1, 1, size)
    w1 = np.clip((1 - np.abs(t)) * 2.2, 0, 1)
    m = np.minimum.outer(w1, w1)[..., None]      # 1 in the middle, 0 at the edges
    out = a * m + rolled * (1 - m)
    return Image.fromarray(out.clip(0, 255).astype(np.uint8))


def even_out(img: Image.Image, radius: int = 60) -> Image.Image:
    """Remove large-scale lighting gradients so tiling doesn't show a pattern."""
    a = np.asarray(img, np.float64)
    low = np.asarray(img.filter(ImageFilter.GaussianBlur(radius)), np.float64)
    out = a - low + low.reshape(-1, 3).mean(0)
    return Image.fromarray(out.clip(0, 255).astype(np.uint8))


def noise(size, sigma, amp):
    from scipy import ndimage
    n = ndimage.gaussian_filter(rng.standard_normal((size, size)), sigma, mode="wrap")
    return n / (n.std() + 1e-9) * amp


def shingles(size=1024):
    """Architectural (laminated) shingles in the photographed brown-grey tones."""
    base = np.array([112, 98, 84])
    img = np.zeros((size, size, 3))
    rows = 16                                   # tile = 2 m -> 12.5 cm exposure
    rh = size // rows
    for r in range(rows):
        x = -int(rng.integers(0, 60))
        while x < size:
            w = int(rng.integers(50, 130))
            tone = base * rng.uniform(0.80, 1.12) + rng.normal(0, 2.5, 3)
            if rng.random() < 0.2:
                tone = tone * np.array([1.06, 1.0, 0.93])   # warm brown tabs
            x0, x1 = max(x, 0), min(x + w, size)
            img[r * rh:(r + 1) * rh, x0:x1] = tone
            img[r * rh:(r + 1) * rh, x0:x0 + 2] *= 0.55     # tab gap
            x += w
        img[r * rh + rh - 4:(r + 1) * rh, :] *= 0.62        # shadow line under row
    img += noise(size, 1.0, 9)[..., None]
    return Image.fromarray(img.clip(0, 255).astype(np.uint8))


def garage_door(w=1024, h=448):
    """Two-car raised-panel garage door, 4 rows x 8 panels, almond colour."""
    col = np.array([214, 200, 182])
    im = Image.new("RGB", (w, h), tuple(col))
    d = ImageDraw.Draw(im)
    rows, cols, m = 4, 8, 10
    ph, pw = h / rows, w / cols
    for r in range(rows):
        d.line((0, int(r * ph), w, int(r * ph)), fill=(150, 140, 126), width=3)
        for c in range(cols):
            x0, y0 = c * pw + m, r * ph + m + 3
            x1, y1 = (c + 1) * pw - m, (r + 1) * ph - m
            d.rectangle((x0, y0, x1, y1), outline=(176, 164, 148), width=4)
            d.rectangle((x0 + 8, y0 + 8, x1 - 8, y1 - 8), fill=(220, 207, 190))
            d.line((x0, y1, x1, y1), fill=(236, 226, 212), width=2)
    a = np.asarray(im, np.float64) + noise(w, 1.2, 3)[:h, :w, None]
    return Image.fromarray(a.clip(0, 255).astype(np.uint8))


def front_door(w=256, h=640):
    """Black front door with a full-length glass lite and a transom above."""
    im = Image.new("RGB", (w, h), (230, 222, 205))            # cream frame
    d = ImageDraw.Draw(im)
    trans_h = int(h * 0.15)
    d.rectangle((10, 8, w - 10, trans_h - 6), fill=(70, 88, 104))       # transom glass
    d.rectangle((10, trans_h, w - 10, h), fill=(24, 24, 26))            # door slab
    d.rectangle((52, trans_h + 60, w - 52, h - 150), fill=(88, 98, 104))  # glass lite
    d.rectangle((52, trans_h + 60, w - 52, h - 150), outline=(12, 12, 12), width=5)
    d.ellipse((w - 45, int(h * 0.58), w - 29, int(h * 0.58) + 16), fill=(150, 130, 90))  # knob
    return im


def window(w=256, h=512):
    """Single-hung window: cream vinyl frame, two sashes, mint-green blinds behind glass."""
    im = Image.new("RGB", (w, h), (232, 226, 206))
    d = ImageDraw.Draw(im)
    f = 16
    mid = h // 2
    for y0, y1 in ((f, mid - 5), (mid + 5, h - f)):
        d.rectangle((f, y0, w - f, y1), fill=(150, 196, 178))
        for y in range(y0 + 6, y1, 11):                       # blind slats
            d.line((f, y, w - f, y), fill=(128, 172, 156), width=2)
        d.line((w // 2, y0, w // 2, y1), fill=(224, 218, 198), width=5)   # grille
        d.line((f, (y0 + y1) // 2, w - f, (y0 + y1) // 2), fill=(224, 218, 198), width=5)
    d.rectangle((0, mid - 8, w, mid + 8), fill=(236, 230, 212))   # meeting rail
    a = np.asarray(im, np.float64)
    # soft sky reflection across the glass
    yy, xx = np.mgrid[0:h, 0:w]
    a += (np.clip(1 - (xx + yy * 0.5) / (w * 1.4), 0, 1) * 28)[..., None]
    return Image.fromarray(a.clip(0, 255).astype(np.uint8))


def stone_block(size=256):
    """Tumbled concrete edging-block colour with a little mottling."""
    base = np.array([214, 196, 164])
    a = base + noise(size, 2.0, 9)[..., None] + noise(size, 12, 5)[..., None]
    return Image.fromarray(a.clip(0, 255).astype(np.uint8))


def flat(colour, size=256, amp=5):
    a = np.array(colour, np.float64) + noise(size, 1.5, amp)[..., None]
    return Image.fromarray(a.clip(0, 255).astype(np.uint8))


def main(photo_dir):
    os.makedirs(OUT, exist_ok=True)
    for name, (photo, box) in CROPS.items():
        crop = Image.open(os.path.join(photo_dir, photo)).crop(box)
        size = 1024 if name in ("brick", "lawn") else 512
        tile = seamless(even_out(crop.resize((size, size), Image.LANCZOS)), size)
        tile.save(os.path.join(OUT, "T_" + "".join(p.title() for p in name.split("_")) + ".png"))
    # Azaleas photographed in full sun read too yellow in the render: deepen them.
    az = np.asarray(Image.open(os.path.join(OUT, "T_LeavesAzalea.png")), np.float64)
    Image.fromarray((az * np.array([0.68, 0.84, 0.62])).clip(0, 255).astype(np.uint8)).save(
        os.path.join(OUT, "T_LeavesAzalea.png"))
    # Background-tree canopy: the myrtle leaves, darker and bluer (live oak / pine).
    leaves = np.asarray(Image.open(os.path.join(OUT, "T_LeavesMyrtle.png")), np.float64)
    Image.fromarray((leaves * np.array([0.62, 0.72, 0.66])).clip(0, 255).astype(np.uint8)).save(
        os.path.join(OUT, "T_TreeCanopy.png"))
    window().save(os.path.join(OUT, "T_Window.png"))
    shingles().save(os.path.join(OUT, "T_Shingles.png"))
    garage_door().save(os.path.join(OUT, "T_GarageDoor.png"))
    front_door().save(os.path.join(OUT, "T_FrontDoor.png"))
    stone_block().save(os.path.join(OUT, "T_StoneBlock.png"))
    flat((205, 205, 200), amp=7).save(os.path.join(OUT, "T_Concrete.png"))
    flat((62, 62, 64), amp=9).save(os.path.join(OUT, "T_Asphalt.png"))
    flat((214, 196, 170), amp=3).save(os.path.join(OUT, "T_Stucco.png"))
    flat((196, 184, 166), amp=18).save(os.path.join(OUT, "T_RiverRock.png"))
    print("textures written to", OUT)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")

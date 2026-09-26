"""Generate a fake aerial picture (lake, meadows, forest, mountain) for testing."""
import numpy as np
from PIL import Image
from scipy import ndimage

rng = np.random.default_rng(7)
N = 768


def noise(sigma):
    n = ndimage.gaussian_filter(rng.standard_normal((N, N)), sigma)
    return (n - n.min()) / (n.max() - n.min())


y, x = np.mgrid[0:N, 0:N] / N
elev = 0.55 * noise(90) + 0.25 * noise(30) + 0.1 * noise(8) + 0.5 * np.clip(x + y - 0.9, 0, 1)
elev = (elev - elev.min()) / (elev.max() - elev.min())
river = np.abs(y - 0.35 - 0.12 * np.sin(x * 9) - 0.05 * (noise(40) - 0.5)) < 0.012 + 0.01 * x
forest = (noise(25) + 0.2 * noise(6)) > 0.58

img = np.zeros((N, N, 3))
tex = noise(1.5)[..., None]
cols = {
    "water": (40, 80, 120), "sand": (205, 190, 140), "grass": (110, 150, 60),
    "forest": (35, 70, 30), "rock": (125, 120, 115), "snow": (240, 242, 245),
}
def paint(mask, key):
    img[mask] = np.array(cols[key]) / 255.0

paint(np.ones((N, N), bool), "grass")
paint((elev < 0.62) & forest, "forest")
paint(elev > 0.62, "rock")
paint(elev > 0.85, "snow")
paint((elev < 0.18) | river, "water")
paint(((elev >= 0.18) & (elev < 0.21)), "sand")
img = np.clip(img * (0.85 + 0.3 * tex), 0, 1)
Image.fromarray((img * 255).astype(np.uint8)).save("examples/sample_aerial.jpg", quality=92)
print("wrote examples/sample_aerial.jpg")

"""DarkSign identity: logo (eclipse) and wordmark.

The symbol is an eclipse: a dark disc passes in front of a sun and only
leaves a ring of light. "bright" becomes "dark".
"""
import numpy as np
from PIL import Image, ImageDraw, ImageFont

FONT_DIR = "/usr/share/fonts/opentype/inter/"

BG = np.array([11, 12, 15], np.float32)          # bluish black
MOON = np.array([6, 7, 9], np.float32)
TEXT = (236, 239, 243)
MUTED = (139, 149, 161)
ACCENT = (255, 176, 84)                           # amber, for texts and UI
# light ramp: amber -> warm white when it saturates
GLOW = np.array([255, 150, 58], np.float32)
HOT = np.array([255, 245, 230], np.float32)
RIM = np.array([1.0, 0.72, 0.42], np.float32)


def font(name, size):
    try:
        return ImageFont.truetype(FONT_DIR + name, size)
    except OSError:
        bold = any(w in name for w in ("Bold", "SemiBold", "Medium"))
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans%s.ttf"
                                  % ("-Bold" if bold else ""), size)


def bg_color():
    return tuple(int(v) for v in BG)


def _smoothstep(x):
    x = np.clip(x, 0, 1)
    return x * x * (3 - 2 * x)


class Eclipse:
    """Eclipse renderer in a (w, h) area, sun at the centre.

    The light fields are computed once; each frame only shifts the moon's
    fields and combines them, which stays fast on a Pi 3.
    """

    def __init__(self, w, h, radius, travel=3.0):
        self.w, self.h, self.radius = w, h, radius
        f32 = np.float32
        cx, cy = (w - 1) / 2, (h - 1) / 2
        yy, xx = np.mgrid[0:h, 0:w].astype(f32)
        dx, dy = xx - cx, yy - cy
        r = np.sqrt(dx * dx + dy * dy) / radius
        out = np.clip(r - 1, 0, None)
        body = np.clip((1 - r) * radius / 1.5, 0, 1) * (1 - 0.35 * np.clip(r, 0, 1) ** 2)
        self.sun = (2.6 * body + 0.9 * np.exp(-out / 0.18)
                    + 0.35 * np.exp(-out / 0.9)).astype(f32)
        self.haze = (0.12 * np.exp(-r / 3.0)).astype(f32)
        # fondu circulaire vers le fond : ni couture ni rectangle visibles
        edge = min(w, h) / 2
        self.window = _smoothstep((edge - np.sqrt(dx * dx + dy * dy))
                                  / (edge * 0.45)).astype(f32)

        # moon fields, computed over a wider area then cropped
        self.max_shift = int(np.ceil(travel * radius))
        mw = w + 2 * self.max_shift
        myy, mxx = np.mgrid[0:h, 0:mw].astype(f32)
        mdx, mdy = mxx - (mw - 1) / 2, myy - cy
        rm = np.sqrt(mdx * mdx + mdy * mdy) / (radius * 1.015)
        mout = np.clip(rm - 1, 0, None)
        ang = np.arctan2(mdy, mdx)
        streaks = 1 + 0.18 * np.sin(ang * 7 + 1.3) * np.sin(ang * 3 - 0.4)
        self.ring = ((np.exp(-mout / 0.045) * 1.9 + np.exp(-mout / 0.16) * 0.8
                      + np.exp(-mout / 0.6) * 0.22) * streaks * (rm > 1)).astype(f32)
        self.inside = np.clip((1 - rm) * radius * 1.015 / 1.2, 0, 1).astype(f32)
        self.rim = (np.exp(-np.clip(1 - rm, 0, None) / 0.02) * 18).astype(f32)

        # "diamond ring" flash: small pattern added at the right position
        s = int(radius * 0.9)
        pyy, pxx = np.mgrid[-s:s + 1, -s:s + 1].astype(f32)
        d2 = (pxx ** 2 + pyy ** 2) / radius ** 2
        rays = (np.exp(-(pyy / (radius * 0.012)) ** 2 - np.abs(pxx) / (radius * 0.28))
                + np.exp(-(pxx / (radius * 0.012)) ** 2 - np.abs(pyy) / (radius * 0.28)))
        self.spark = (4.0 * np.exp(-d2 / 0.004) + 1.2 * np.exp(-d2 / 0.05)
                      + 0.9 * rays).astype(f32)

    # light intensity -> colour, through a table: a single channel to compute
    LUT_MAX, LUT_SIZE = 8.0, 2048
    _lut = None

    @classmethod
    def lut(cls):
        if cls._lut is None:
            light = np.linspace(0, cls.LUT_MAX, cls.LUT_SIZE, dtype=np.float32)
            warm = 1 - np.exp(-light * 1.6)
            hot = np.clip(light - 0.85, 0, None)
            hot /= hot + 0.6
            rgb = BG + (warm * (1 - hot))[:, None] * GLOW + hot[:, None] * HOT
            cls._lut = np.clip(rgb, 0, 255).astype(np.uint8)
        return cls._lut

    def frame(self, moon_dx=0.0, sun=0.0, corona=1.0, spark=0.0,
              spark_angle=np.pi, haze=0.0):
        """Image (h, w, 3) uint8. moon_dx in radii (0 = totality)."""
        shift = int(round(moon_dx * self.radius))
        a = self.max_shift - shift
        sl = np.s_[:, a:a + self.w]

        light = self.sun * sun if sun > 0 else np.zeros((self.h, self.w), np.float32)
        if haze > 0:
            light += self.haze * haze
        if corona > 0:
            light += self.ring[sl] * corona
        if spark > 0:
            r = self.radius * 1.015
            sx = int((self.w - 1) / 2 + shift + np.cos(spark_angle) * r)
            sy = int((self.h - 1) / 2 + np.sin(spark_angle) * r)
            s = self.spark.shape[0] // 2
            x0, y0 = max(sx - s, 0), max(sy - s, 0)
            x1, y1 = min(sx + s + 1, self.w), min(sy + s + 1, self.h)
            if x1 > x0 and y1 > y0:
                light[y0:y1, x0:x1] += spark * self.spark[y0 - sy + s:y1 - sy + s,
                                                          x0 - sx + s:x1 - sx + s]
        light *= self.window

        # the moon hides the light; its edge catches an amber reflection
        inside = self.inside[sl]
        light *= 1 - inside
        light += inside * self.rim[sl] * (corona * 0.02)
        idx = np.minimum(light * (self.LUT_SIZE / self.LUT_MAX),
                         self.LUT_SIZE - 1).astype(np.int16)
        rgb = self.lut()[idx]
        # darken the moon disc, only in front of the light: over the bare
        # background the moon stays invisible (like a real one)
        disc = inside > 0
        behind = np.clip(self.sun[disc] * max(sun, corona * 0.6) * 3 - 0.3, 0, 1)
        rgb[disc] = (rgb[disc] * (1 - 0.45 * inside[disc] * behind)[:, None]
                     ).astype(np.uint8)
        return rgb


def wordmark(height, color=TEXT):
    """"darksign" wordmark: dark in light weight, sign in bold. RGBA image."""
    light = font("InterDisplay-Light.otf", height)
    bold = font("InterDisplay-SemiBold.otf", height)
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    w1 = probe.textlength("dark", font=light)
    w2 = probe.textlength("sign", font=bold)
    img = Image.new("RGBA", (int(w1 + w2 + 4), int(height * 1.3)), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    base = int(height * 1.02)
    d.text((0, base), "dark", font=light, fill=color, anchor="ls")
    d.text((w1, base), "sign", font=bold, fill=color, anchor="ls")
    return img.crop(img.getbbox())


def mark(radius):
    """Symbol alone (total eclipse) on a BG background, with its glow margin."""
    size = int(radius * 5)
    arr = Eclipse(size, size, radius, travel=0).frame(corona=1.0)
    return Image.fromarray(arr, "RGB")


def lockup_geometry(text_height):
    """Dimensions of the full logo for a given text height."""
    radius = text_height * 0.62
    gap = text_height * 0.55
    return radius, gap


def lockup(text_height):
    """Symbol + wordmark on a BG background. Return (image, margin): the
    margin is the glow spilling around the disc, to remove to align the logo."""
    radius, gap = lockup_geometry(text_height)
    m = mark(radius)
    w = wordmark(int(text_height))
    pad = (m.width - 2 * radius) / 2           # marge de halo autour du disque
    x_text = int(m.width - pad + gap)
    img = Image.new("RGB", (x_text + w.width + int(pad), m.height), bg_color())
    img.paste(m, (0, 0))
    img.paste(w, (x_text, (m.height - w.height) // 2), w)
    return img, int(pad)

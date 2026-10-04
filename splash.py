"""DarkSign splash screen, shown as long as no content is scheduled.

It tells where to connect to manage the player (IP address, .local name and
QR code), in the player language (config "language", locales/*.json).
Two renderings:
  - render()  : still image, instant (shown right away);
  - animate() : intro animation (eclipse, then logo) ending exactly on the
                still image. Rendered once per network address, in the
                background, then cached.

The animation has two parts chained seamlessly:
  - assets/intro.mp4: eclipse and logo forming, generic, shipped with the
    project (regenerate with "python3 splash.py intro" if the visual changes);
  - the ending (the logo moves to the header, the information appears),
    specific to the network address, rendered by the player in a separate
    process:
    python3 splash.py static OUTPUT.png PORT [ADDRESS...]
    python3 splash.py animate OUTPUT.mp4 PORT [ADDRESS...]
"""
import multiprocessing
import socket
import subprocess
import sys
from pathlib import Path

import numpy as np
import qrcode
from PIL import Image, ImageDraw

import brand
import network
from brand import ACCENT, MUTED, TEXT, font
from common import load_config, mdns_available, network_status, player_name
from i18n import normalize, t

W, H = 1920, 1080
FPS = 25

CARD = (22, 25, 31)
LINE = (38, 43, 51)
LEFT, RIGHT = 160, W - 160
HEADER_TEXT = 52      # height of the wordmark in the header
HEADER_Y = 128        # vertical centre of the logo in the header


def _(key, **values):
    """Text in the player language."""
    return t(key, normalize(load_config().get("language")), **values)


def _wrap(draw, text, fnt, width):
    lines, line = [], ""
    for word in text.split():
        test = f"{line} {word}".strip()
        if draw.textlength(test, font=fnt) <= width:
            line = test
        else:
            lines.append(line)
            line = word
    return lines + [line]


# --- layout ----------------------------------------------------------------

ERROR = (248, 113, 113)
OK = (74, 222, 128)


def _footer(d, hostname, addresses):
    d.line((LEFT, H - 120, RIGHT, H - 120), fill=LINE, width=2)
    footer = font("Inter-Regular.otf", 24)
    name = player_name(load_config())   # player name ("Hall A")
    where = ", ".join(addresses) or _("splash.footer.offline")
    d.text((LEFT, H - 80), f"{name}  ·  {where}", font=footer, fill=MUTED, anchor="lm")
    d.text((RIGHT, H - 80), _("splash.footer.note"),
           font=footer, fill=MUTED, anchor="rm")


def offline_layer(status):
    """"No connection" screen: diagnosis and things to try."""
    hostname = socket.gethostname()
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    y = 250
    d.text((LEFT, y), _("splash.offline.title"),
           font=font("InterDisplay-SemiBold.otf", 72), fill=TEXT)
    y += 112
    body = font("Inter-Regular.otf", 30)
    for line in _wrap(d, _("splash.offline.intro"), body, 1300):
        d.text((LEFT, y), line, font=body, fill=MUTED)
        y += 44
    y += 34

    wifi, eth = status["wifi"], status["ethernet"]
    rows = []
    if wifi["present"]:
        if wifi["ssid"]:
            rows.append((ERROR, _("splash.offline.wifi"),
                         _("splash.offline.wifi_failed", ssid=wifi["ssid"])))
        else:
            rows.append((ERROR, _("splash.offline.wifi"), _("splash.offline.wifi_none")))
    if eth["present"]:
        rows.append((ACCENT, _("splash.offline.ethernet"), _("splash.offline.ethernet_waiting"))
                    if eth["carrier"]
                    else (MUTED, _("splash.offline.ethernet"), _("splash.offline.ethernet_unplugged")))
    card_h = 40 + 62 * len(rows)
    d.rounded_rectangle((LEFT, y, RIGHT, y + card_h), radius=18, fill=CARD,
                        outline=LINE, width=2)
    label = font("Inter-SemiBold.otf", 30)
    value = font("Inter-Regular.otf", 30)
    ry = y + 20 + 31
    for color, name, text in rows:
        d.ellipse((LEFT + 40, ry - 8, LEFT + 56, ry + 8), fill=color)
        d.text((LEFT + 80, ry), name, font=label, fill=TEXT, anchor="lm")
        d.text((LEFT + 340, ry), text, font=value, fill=MUTED, anchor="lm")
        ry += 62
    y += card_h + 56

    steps = [_("splash.offline.step_cable")]
    if wifi["ssid"]:
        steps.append(_("splash.offline.step_wifi", ssid=wifi["ssid"]))
    steps.append(_("splash.offline.step_wait"))
    _steps(d, y, steps)
    _footer(d, hostname, [])
    return img


def _steps(d, y, steps):
    step_font = font("Inter-Regular.otf", 30)
    num_font = font("Inter-SemiBold.otf", 25)
    for i, step in enumerate(steps, 1):
        cy = y + 22
        d.ellipse((LEFT, cy - 21, LEFT + 42, cy + 21), outline=ACCENT, width=3)
        d.text((LEFT + 21, cy), str(i), font=num_font, fill=ACCENT, anchor="mm")
        d.text((LEFT + 68, cy), step, font=step_font, fill=TEXT, anchor="lm")
        y += 60
    return y


def _qr(img, d, data, max_size, x, y, label):
    """QR code on a white card, top-left corner of the code at (x, y)."""
    qr = qrcode.QRCode(border=0, box_size=1,
                       error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(data)
    qr.make(fit=True)
    modules = qr.modules_count
    size = modules * (max_size // modules)   # whole-pixel modules: sharp
    code = qr.make_image(fill_color=(11, 12, 15), back_color="white")
    code = code.convert("RGB").resize((size, size), Image.NEAREST)
    pad = 34 if max_size > 300 else 24
    d.rounded_rectangle((x - pad, y - pad, x + size + pad, y + size + pad),
                        radius=24, fill=(255, 255, 255, 255))
    img.paste(code, (x, y))
    d.text((x + size / 2, y + size + pad + 36), label,
           font=font("Inter-Medium.otf", 28), fill=MUTED, anchor="mm")
    return size


def _wifi_qr_data(ssid, password):
    """Format phone cameras recognise to join a Wi-Fi network (special
    characters escaped)."""
    esc = lambda v: "".join("\\" + c if c in '\\;,:"' else c for c in v)
    return f"WIFI:T:WPA;S:{esc(ssid)};P:{esc(password)};;"


def ap_layer(ap, addresses, port):
    """Player access point: join its Wi-Fi, then the web UI."""
    hostname = socket.gethostname()
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    col_w = 1000
    y = 250
    d.text((LEFT, y), _("splash.ready.title"),
           font=font("InterDisplay-SemiBold.otf", 72), fill=TEXT)
    y += 112
    body = font("Inter-Regular.otf", 30)
    for line in _wrap(d, _("splash.ap.intro"), body, col_w):
        d.text((LEFT, y), line, font=body, fill=MUTED)
        y += 44
    y += 24

    d.rounded_rectangle((LEFT, y, LEFT + col_w, y + 132), radius=18, fill=CARD,
                        outline=LINE, width=2)
    label = font("Inter-SemiBold.otf", 30)
    value = font("InterDisplay-SemiBold.otf", 38)
    for i, (name, text) in enumerate(((_("splash.ap.wifi"), ap["ssid"]),
                                      (_("splash.ap.password"), ap["password"]))):
        ry = y + 38 + i * 56
        d.text((LEFT + 44, ry), name, font=label, fill=MUTED, anchor="lm")
        d.text((LEFT + 300, ry), text, font=value, fill=TEXT, anchor="lm")
    y += 156

    url = f"http://{ap['address']}:{port}"
    d.rounded_rectangle((LEFT, y, LEFT + col_w, y + 124), radius=18,
                        fill=CARD, outline=LINE, width=2)
    d.text((LEFT + 44, y + 62), url, anchor="lm",
           font=font("InterDisplay-SemiBold.otf", 56), fill=ACCENT)
    y += 164
    _steps(d, y, [_("splash.ap.step_upload"), _("splash.step_save")])

    size = 220
    x = RIGHT - 24 - size
    _qr(img, d, _wifi_qr_data(ap["ssid"], ap["password"]), size, x, 274,
        _("splash.ap.qr_wifi"))
    _qr(img, d, url, size, x, 274 + size + 118, _("splash.ap.qr_ui"))

    _footer(d, hostname, addresses + [_("splash.footer.ap")])
    return img


def info_layer(addresses, port):
    """The whole screen except the logo, on a transparent background (RGBA)."""
    ap = network.ap_info()
    if ap:
        return ap_layer(ap, addresses, port)
    if not addresses:
        return offline_layer(network_status())
    hostname = socket.gethostname()
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    col_w = 980
    y = top = 250

    d.text((LEFT, y), _("splash.ready.title"),
           font=font("InterDisplay-SemiBold.otf", 72), fill=TEXT)
    y += 112

    body = font("Inter-Regular.otf", 30)
    for line in _wrap(d, _("splash.ready.intro"), body, col_w):
        d.text((LEFT, y), line, font=body, fill=MUTED)
        y += 44
    y += 28

    url = f"http://{addresses[0]}:{port}"
    if url:
        d.rounded_rectangle((LEFT, y, LEFT + col_w, y + 124), radius=18,
                            fill=CARD, outline=LINE, width=2)
        d.text((LEFT + 44, y + 62), url, anchor="lm",
               font=font("InterDisplay-SemiBold.otf", 56), fill=ACCENT)
        alt = []
        if mdns_available():
            alt.append(f"http://{hostname}.local:{port}")
        alt += [f"http://{a}:{port}" for a in addresses[1:]]
        y += 142
        if alt:
            d.text((LEFT + 4, y), _("splash.ready.or") + "  " + "   ·   ".join(alt),
                   font=font("Inter-Regular.otf", 28), fill=MUTED)
        y += 74

    bottom = _steps(d, y, [_("splash.ready.step_upload"), _("splash.ready.step_mode"),
                           _("splash.step_save")])

    if url:
        size = 340
        x = RIGHT - 34 - size
        _qr(img, d, url, size, x, int(top + (bottom - top - size - 70) / 2),
            _("splash.ready.qr"))

    _footer(d, hostname, addresses)
    return img


def header_lockup():
    """Header logo and its position (top-left corner) on the screen."""
    img, pad = brand.lockup(HEADER_TEXT)
    return img, (LEFT - pad, HEADER_Y - img.height // 2)


def render(path, addresses, port):
    """Still screen: also the last frame of the animation.
    Saved to path if given; the image is returned in any case."""
    canvas = Image.new("RGB", (W, H), brand.bg_color())
    logo, pos = header_lockup()
    canvas.paste(logo, pos)
    info = info_layer(addresses, port)
    canvas.paste(info, (0, 0), info)
    if path:
        canvas.save(path)
    return canvas


# --- animation ---------------------------------------------------------------

SUN_R = 150                         # sun radius at the centre of the screen
TILE_W, TILE_H = 1300, 960          # eclipse rendering area

_eclipse = None                     # shared with the rendering processes


def _ease(x):                       # smooth acceleration then deceleration
    x = min(max(x, 0.0), 1.0)
    return x * x * x * (x * (6 * x - 15) + 10)


def _ramp(t, t0, t1):
    return _ease((t - t0) / (t1 - t0))


def _eclipse_params(t):
    """Eclipse parameters at time t (seconds)."""
    moon = -3.2 + 3.2 * _ramp(t, 1.2, 3.0)
    return dict(
        moon_dx=moon,
        sun=_ramp(t, 0.1, 0.9) * (1 - _ramp(moon, -0.35, 0.0)),
        haze=_ramp(t, 0.1, 0.9) * (1 - _ramp(moon, -2.2, -0.2)),
        corona=_ramp(moon, -0.7, 0.0) * (1 + 0.08 * np.sin(max(t - 3.0, 0) * 5)
                                         * (1 - _ramp(t, 3.0, 3.8))),
        spark=float(np.exp(-((t - 2.92) / 0.14) ** 2)) * 1.4,
        spark_angle=0.0,            # last sliver of sun: right edge
    )


def _render_eclipse(t):
    return _eclipse.frame(**_eclipse_params(t)).tobytes()


ASSETS = Path(__file__).resolve().parent / "assets"
INTRO = ASSETS / "intro.mp4"          # eclipse + logo forming (generic)
TOTALITY = ASSETS / "totality.png"    # total eclipse, at the intro size
T_FORMED = 4.6                        # end of the intro: logo formed, centred
T_END = 6.6                           # end of the animation


class _Encoder:
    """Feed RGB frames to ffmpeg (x264 tuned for animation: text and
    gradients stay sharp where the hardware encoder produces blocks).

    Same settings for the intro and the ending: both files chain seamlessly
    through the concat demuxer."""

    def __init__(self, path):
        self.proc = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
             "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-preset", "veryfast",
             "-tune", "animation", "-crf", "16", "-profile:v", "high",
             "-level:v", "4.1", "-x264-params", "keyint=50:min-keyint=50:scenecut=0",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart",
             "-f", "mp4", str(path)],
            stdin=subprocess.PIPE)

    def write(self, img):
        self.proc.stdin.write(img.tobytes())

    def close(self):
        self.proc.stdin.close()
        if self.proc.wait() != 0:
            raise RuntimeError("animation encoding failed")


class _Composer:
    """Frames of the 2nd part: the logo forms, then moves to the header."""

    def __init__(self, totality, info=None):
        self.bg = Image.new("RGB", (W, H), brand.bg_color())
        self.tile = totality
        # mask: the glow blends into what lies below (no rectangle)
        diff = np.abs(np.asarray(totality, np.int16) - np.array(brand.bg_color()))
        self.mask = Image.fromarray(np.clip(diff.max(axis=2) * 40, 0, 255)
                                    .astype(np.uint8))
        self.info = info.convert("RGB") if info else None
        self.info_alpha = np.asarray(info.getchannel("A"), np.float32) if info else None
        self.big_text = 104
        self.big_r, self.big_gap = brand.lockup_geometry(self.big_text)
        self.wm = brand.wordmark(self.big_text)
        self.wm_alpha = np.asarray(self.wm.getchannel("A"), np.float32)
        self.head_r, self.head_gap = brand.lockup_geometry(HEADER_TEXT)
        lock_w = 2 * self.big_r + self.big_gap + self.wm.width
        self.center_x = (W - lock_w) / 2 + self.big_r

    def frame(self, t):
        form = _ramp(t, 3.6, 4.6)          # the sun shrinks, the word appears
        move = _ramp(t, 4.9, 5.8)          # the logo moves up to the header
        show = _ramp(t, 5.4, 6.4)          # the information appears
        radius = SUN_R + (self.big_r - SUN_R) * form + (self.head_r - self.big_r) * move
        mx = W / 2 + (self.center_x - W / 2) * form \
            + (LEFT + self.head_r - self.center_x) * move
        my = H / 2 + (HEADER_Y - H / 2) * move
        scale = radius / SUN_R

        frame = self.bg.copy()
        if show > 0 and self.info is not None:
            alpha = Image.fromarray((self.info_alpha * show).astype(np.uint8))
            frame.paste(self.info, (0, int(24 * (1 - show))), alpha)
        size = (int(TILE_W * scale), int(TILE_H * scale))
        frame.paste(self.tile.resize(size, Image.BILINEAR),
                    (int(mx - size[0] / 2), int(my - size[1] / 2)),
                    self.mask.resize(size, Image.BILINEAR))

        text_h = self.big_text + (HEADER_TEXT - self.big_text) * move
        k = text_h / self.big_text
        wsize = (max(1, int(self.wm.width * k)), max(1, int(self.wm.height * k)))
        alpha = Image.fromarray((self.wm_alpha * form).astype(np.uint8))
        gap = self.big_gap + (self.head_gap - self.big_gap) * move
        frame.paste(self.wm.convert("RGB").resize(wsize, Image.LANCZOS),
                    (int(mx + radius + gap + 40 * (1 - form)), int(my - wsize[1] / 2)),
                    alpha.resize(wsize, Image.LANCZOS))
        return frame


def make_intro(path=INTRO, totality_path=TOTALITY):
    """Render the generic intro (done once; shipped in assets/)."""
    global _eclipse
    ASSETS.mkdir(exist_ok=True)
    enc = _Encoder(path)
    bg = Image.new("RGB", (W, H), brand.bg_color())
    ox, oy = (W - TILE_W) // 2, (H - TILE_H) // 2
    _eclipse = brand.Eclipse(TILE_W, TILE_H, SUN_R, travel=3.3)
    times = [i / FPS for i in range(int(3.6 * FPS))]
    with multiprocessing.get_context("fork").Pool(3) as pool:
        for raw in pool.imap(_render_eclipse, times, chunksize=2):
            frame = bg.copy()
            frame.paste(Image.frombytes("RGB", (TILE_W, TILE_H), raw), (ox, oy))
            enc.write(frame)
    totality = Image.frombytes("RGB", (TILE_W, TILE_H), _render_eclipse(3.6))
    _eclipse = None
    totality.save(totality_path)
    comp = _Composer(totality)
    for i in range(int(3.6 * FPS), int(T_FORMED * FPS)):
        enc.write(comp.frame(i / FPS))
    enc.close()


def animate(path, addresses, port):
    """Render the ending of the animation (specific to the network address),
    starting from the formed logo and ending exactly on the still screen."""
    if not INTRO.exists() or not TOTALITY.exists():
        make_intro()
    info = info_layer(addresses, port)
    final = render(None, addresses, port)
    comp = _Composer(Image.open(TOTALITY).convert("RGB"), info)
    enc = _Encoder(path)
    for i in range(int(T_FORMED * FPS), int(T_END * FPS)):
        enc.write(comp.frame(i / FPS))
    for _frame in range(FPS // 2):   # exact final frame; mpv keeps it after
        enc.write(final)
    enc.close()


if __name__ == "__main__":
    cmd, args = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else (None, [])
    if cmd == "intro":
        make_intro()
    elif cmd in ("static", "animate") and len(args) >= 2:
        out, port, addresses = args[0], int(args[1]), args[2:]
        tmp = out + ".tmp"
        if cmd == "static":
            render(None, addresses, port).save(tmp, format="PNG")
        else:
            animate(tmp, addresses, port)
        Path(tmp).replace(out)      # never a half-written file
    else:
        sys.exit(__doc__)

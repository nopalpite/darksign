"""Écran d'accueil DarkSign, affiché tant qu'aucun contenu n'est programmé.

Il indique où se connecter pour administrer le lecteur (adresse IP, nom
.local et QR code). Deux rendus :
  - render()  : image fixe, instantanée (affichée tout de suite) ;
  - animate() : animation d'introduction (éclipse puis logo) qui se termine
                exactement sur l'image fixe. Calculée une fois par adresse
                réseau, en tâche de fond, puis mise en cache.

L'animation est en deux parties enchaînées sans coupure :
  - assets/intro.mp4 : éclipse et formation du logo, générique, livrée avec
    le projet (régénérer avec « python3 splash.py intro » si le visuel change) ;
  - la fin (le logo rejoint l'en-tête, les informations apparaissent), propre
    à l'adresse réseau, calculée par le lecteur dans un processus séparé :
    python3 splash.py static SORTIE.png PORT [ADRESSE...]
    python3 splash.py animate SORTIE.mp4 PORT [ADRESSE...]
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

W, H = 1920, 1080
FPS = 25

CARD = (22, 25, 31)
LINE = (38, 43, 51)
LEFT, RIGHT = 160, W - 160
HEADER_TEXT = 52      # hauteur du mot-symbole dans l'en-tête
HEADER_Y = 128        # centre vertical du logo dans l'en-tête


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


# --- mise en page ----------------------------------------------------------

ERROR = (248, 113, 113)
OK = (74, 222, 128)


def _footer(d, hostname, addresses):
    d.line((LEFT, H - 120, RIGHT, H - 120), fill=LINE, width=2)
    footer = font("Inter-Regular.otf", 24)
    name = player_name(load_config())   # nom du lecteur (« Hall A »)
    d.text((LEFT, H - 80), f"{name}  ·  {', '.join(addresses) or 'hors réseau'}",
           font=footer, fill=MUTED, anchor="lm")
    d.text((RIGHT, H - 80), "Cet écran disparaît dès qu'un contenu est programmé",
           font=footer, fill=MUTED, anchor="rm")


def offline_layer(status):
    """Écran « pas de connexion » : diagnostic et pistes de résolution."""
    hostname = socket.gethostname()
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    y = 250
    d.text((LEFT, y), "Pas de connexion réseau",
           font=font("InterDisplay-SemiBold.otf", 72), fill=TEXT)
    y += 112
    body = font("Inter-Regular.otf", 30)
    for line in _wrap(d, "Aucun contenu n'est programmé et le lecteur n'a pas pu se "
                      "connecter au réseau : l'interface d'administration est "
                      "inaccessible pour le moment.", body, 1300):
        d.text((LEFT, y), line, font=body, fill=MUTED)
        y += 44
    y += 34

    wifi, eth = status["wifi"], status["ethernet"]
    rows = []
    if wifi["present"]:
        if wifi["ssid"]:
            rows.append((ERROR, "Wi-Fi", f"« {wifi['ssid']} » configuré, "
                         "mais pas de connexion"))
        else:
            rows.append((ERROR, "Wi-Fi", "aucun réseau Wi-Fi configuré"))
    if eth["present"]:
        rows.append((ACCENT, "Câble Ethernet", "branché, en attente d'une adresse")
                    if eth["carrier"]
                    else (MUTED, "Câble Ethernet", "non branché"))
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

    steps = ["Branchez un câble Ethernet relié au réseau : la connexion est automatique"]
    if wifi["ssid"]:
        steps.append(f"Ou vérifiez que le Wi-Fi « {wifi['ssid']} » est allumé, à portée, "
                     "et que son mot de passe n'a pas changé")
    steps.append("Cet écran se met à jour dès que le réseau est disponible")
    step_font = font("Inter-Regular.otf", 30)
    num_font = font("Inter-SemiBold.otf", 25)
    for i, step in enumerate(steps, 1):
        cy = y + 22
        d.ellipse((LEFT, cy - 21, LEFT + 42, cy + 21), outline=ACCENT, width=3)
        d.text((LEFT + 21, cy), str(i), font=num_font, fill=ACCENT, anchor="mm")
        d.text((LEFT + 68, cy), step, font=step_font, fill=TEXT, anchor="lm")
        y += 60
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
    """QR code sur carte blanche, coin haut gauche du code en (x, y)."""
    qr = qrcode.QRCode(border=0, box_size=1,
                       error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(data)
    qr.make(fit=True)
    modules = qr.modules_count
    size = modules * (max_size // modules)   # modules de taille entière : net
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
    """Format reconnu par les appareils photo des téléphones pour rejoindre
    un Wi-Fi (caractères spéciaux échappés)."""
    esc = lambda v: "".join("\\" + c if c in '\\;,:"' else c for c in v)
    return f"WIFI:T:WPA;S:{esc(ssid)};P:{esc(password)};;"


def ap_layer(ap, addresses, port):
    """Point d'accès du lecteur : rejoindre son Wi-Fi, puis l'interface."""
    hostname = socket.gethostname()
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    col_w = 1000
    y = 250
    d.text((LEFT, y), "Prêt à être configuré",
           font=font("InterDisplay-SemiBold.otf", 72), fill=TEXT)
    y += 112
    body = font("Inter-Regular.otf", 30)
    for line in _wrap(d, "Le lecteur diffuse son propre réseau Wi-Fi : connectez-y un "
                      "ordinateur ou un téléphone, puis ouvrez l'interface :",
                      body, col_w):
        d.text((LEFT, y), line, font=body, fill=MUTED)
        y += 44
    y += 24

    d.rounded_rectangle((LEFT, y, LEFT + col_w, y + 132), radius=18, fill=CARD,
                        outline=LINE, width=2)
    label = font("Inter-SemiBold.otf", 30)
    value = font("InterDisplay-SemiBold.otf", 38)
    for i, (name, text) in enumerate((("Wi-Fi", ap["ssid"]),
                                      ("Mot de passe", ap["password"]))):
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
    _steps(d, y, ["Envoyez vos vidéos et images, choisissez le mode",
                  "Enregistrez : la lecture démarre sur cet écran"])

    size = 220
    x = RIGHT - 24 - size
    _qr(img, d, _wifi_qr_data(ap["ssid"], ap["password"]), size, x, 274,
        "1 · Rejoindre le Wi-Fi")
    _qr(img, d, url, size, x, 274 + size + 118, "2 · Ouvrir l'interface")

    _footer(d, hostname, addresses + ["point d'accès Wi-Fi"])
    return img


def info_layer(addresses, port):
    """Tout l'écran sauf le logo, sur fond transparent (RGBA)."""
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

    d.text((LEFT, y), "Prêt à être configuré",
           font=font("InterDisplay-SemiBold.otf", 72), fill=TEXT)
    y += 112

    body = font("Inter-Regular.otf", 30)
    intro = ("Aucun contenu n'est encore programmé. Depuis un ordinateur ou un "
             "téléphone connecté au même réseau, ouvrez l'interface "
             "d'administration :")
    for line in _wrap(d, intro, body, col_w):
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
            d.text((LEFT + 4, y), "ou  " + "   ·   ".join(alt),
                   font=font("Inter-Regular.otf", 28), fill=MUTED)
        y += 74

    bottom = _steps(d, y, ["Envoyez vos vidéos et images dans la médiathèque",
                           "Choisissez le mode : playlist ou interactif (boutons)",
                           "Enregistrez : la lecture démarre sur cet écran"])

    if url:
        size = 340
        x = RIGHT - 34 - size
        _qr(img, d, url, size, x, int(top + (bottom - top - size - 70) / 2),
            "Scanner pour administrer")

    _footer(d, hostname, addresses)
    return img


def header_lockup():
    """Logo de l'en-tête et sa position (coin haut gauche) sur l'écran."""
    img, pad = brand.lockup(HEADER_TEXT)
    return img, (LEFT - pad, HEADER_Y - img.height // 2)


def render(path, addresses, port):
    """Écran fixe : c'est aussi la dernière image de l'animation.
    Enregistré dans path si fourni ; l'image est renvoyée dans tous les cas."""
    canvas = Image.new("RGB", (W, H), brand.bg_color())
    logo, pos = header_lockup()
    canvas.paste(logo, pos)
    info = info_layer(addresses, port)
    canvas.paste(info, (0, 0), info)
    if path:
        canvas.save(path)
    return canvas


# --- animation ---------------------------------------------------------------

SUN_R = 150                         # rayon du soleil au centre de l'écran
TILE_W, TILE_H = 1300, 960          # zone de rendu de l'éclipse

_eclipse = None                     # partagé avec les processus de calcul


def _ease(x):                       # accélération puis décélération douces
    x = min(max(x, 0.0), 1.0)
    return x * x * x * (x * (6 * x - 15) + 10)


def _ramp(t, t0, t1):
    return _ease((t - t0) / (t1 - t0))


def _eclipse_params(t):
    """Paramètres de l'éclipse à l'instant t (secondes)."""
    moon = -3.2 + 3.2 * _ramp(t, 1.2, 3.0)
    return dict(
        moon_dx=moon,
        sun=_ramp(t, 0.1, 0.9) * (1 - _ramp(moon, -0.35, 0.0)),
        haze=_ramp(t, 0.1, 0.9) * (1 - _ramp(moon, -2.2, -0.2)),
        corona=_ramp(moon, -0.7, 0.0) * (1 + 0.08 * np.sin(max(t - 3.0, 0) * 5)
                                         * (1 - _ramp(t, 3.0, 3.8))),
        spark=float(np.exp(-((t - 2.92) / 0.14) ** 2)) * 1.4,
        spark_angle=0.0,            # dernier croissant de soleil : bord droit
    )


def _render_eclipse(t):
    return _eclipse.frame(**_eclipse_params(t)).tobytes()


ASSETS = Path(__file__).resolve().parent / "assets"
INTRO = ASSETS / "intro.mp4"          # éclipse + formation du logo (générique)
TOTALITY = ASSETS / "totality.png"    # éclipse en totalité, taille de l'intro
T_FORMED = 4.6                        # fin de l'intro : logo formé, centré
T_END = 6.6                           # fin de l'animation


class _Encoder:
    """Envoie des images RGB à ffmpeg (x264, réglé pour de l'animation : texte
    et dégradés restent nets, là où l'encodeur matériel produit des blocs).

    Réglages identiques pour l'intro et la fin : les deux fichiers
    s'enchaînent sans coupure via le démultiplexeur concat."""

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
            raise RuntimeError("échec de l'encodage de l'animation")


class _Composer:
    """Images de la 2e partie : le logo se forme puis rejoint l'en-tête."""

    def __init__(self, totality, info=None):
        self.bg = Image.new("RGB", (W, H), brand.bg_color())
        self.tile = totality
        # masque : le halo se fond dans ce qui est dessous (pas de rectangle)
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
        form = _ramp(t, 3.6, 4.6)          # le soleil rétrécit, le mot apparaît
        move = _ramp(t, 4.9, 5.8)          # le logo monte dans l'en-tête
        show = _ramp(t, 5.4, 6.4)          # les informations apparaissent
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
    """Génère l'intro générique (à faire une fois ; livrée dans assets/)."""
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
    """Génère la fin de l'animation (propre à l'adresse réseau), qui part du
    logo formé et se termine exactement sur l'écran fixe."""
    if not INTRO.exists() or not TOTALITY.exists():
        make_intro()
    info = info_layer(addresses, port)
    final = render(None, addresses, port)
    comp = _Composer(Image.open(TOTALITY).convert("RGB"), info)
    enc = _Encoder(path)
    for i in range(int(T_FORMED * FPS), int(T_END * FPS)):
        enc.write(comp.frame(i / FPS))
    for _ in range(FPS // 2):   # image finale exacte ; mpv la garde ensuite
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
        Path(tmp).replace(out)      # jamais de fichier à moitié écrit
    else:
        sys.exit(__doc__)

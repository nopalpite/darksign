"""Éléments partagés entre le lecteur (player.py) et le backend web (web.py)."""
import json
import os
import re
import socket
import subprocess
import tempfile
import unicodedata
from pathlib import Path

BASE = Path(__file__).resolve().parent
MEDIA_DIR = BASE / "media"
DATA_DIR = BASE / "data"
CONFIG_FILE = DATA_DIR / "config.json"
SOCKET_PATH = str(DATA_DIR / "player.sock")

VIDEO_EXT = {".mp4", ".m4v", ".mkv", ".mov", ".avi", ".webm", ".mpg", ".mpeg", ".ts"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".gif"}
SUBTITLE_EXT = {".srt", ".vtt", ".ass"}

# messages UDP réservés, valables dans tous les modes
UDP_COMMANDS = {"pause": "pause", "play": "reprise", "restart": "relance"}
UDP_MAX_LEN = 64

IMAGE_DURATION = 6   # s : affichage par défaut d'une image dans une playlist
IMAGE_DURATION_MAX = 3600

# taille des sous-titres (pixels mpv, référence 720 lignes)
SUBTITLE_SIZES = {"small": 36, "medium": 48, "large": 64}

# BCM -> broche physique du connecteur 40 broches
PHYSICAL = {
    2: 3, 3: 5, 4: 7, 5: 29, 6: 31, 7: 26, 8: 24, 9: 21, 10: 19, 11: 23,
    12: 32, 13: 33, 14: 8, 15: 10, 16: 36, 17: 11, 18: 12, 19: 35, 20: 38,
    21: 40, 22: 15, 23: 16, 24: 18, 25: 22, 26: 37, 27: 13,
}

DEFAULT_CONFIG = {
    # nom du lecteur (« Hall A ») : affiché, et décliné en nom d'hôte (hall-a,
    # http://hall-a.local:8080) et en nom du Wi-Fi du point d'accès.
    # Vide : le nom d'hôte actuel.
    "name": None,
    "mode": "loop",                # "loop" (playlist) | "interactive"
    "volume": 100,
    "audio_device": "auto",
    "udp_port": 5000,              # messages UDP : boutons et commandes
    # réseau : Wi-Fi du lieu (client) ou point d'accès du lecteur (ap). Nom du
    # point d'accès vide : celui du lecteur ; mot de passe généré au premier
    # démarrage (network.py)
    "network": {"mode": "client", "ap_ssid": None, "ap_password": None},
    # playlist : une seule entrée tourne en boucle infinie ; sinon les entrées
    # s'enchaînent dans l'ordre, chaque vidéo répétée « repeat » fois, chaque
    # image affichée « duration » secondes
    "loop": {"items": [], "muted": False},   # [{"media": "a.mp4", "repeat": 1}]
    "interactive": {
        "attract": None,
        "attract_muted": True,
        "triggers_muted": False,
        "interruptible": True,
        "active_low": True,        # bouton relié à GND, pull-up interne
        # déclencheurs : broche GPIO et/ou message UDP -> vidéo
        "triggers": [],            # [{"gpio": 17, "udp": "intro", "media": "video.mp4"}]
    },
    "subtitles": {},               # {"video.mp4": "video.srt"}
    "subtitle_style": {"size": "medium", "background": True},
}


def trigger_label(t):
    """Nom lisible d'un déclencheur : « GPIO17 », « UDP intro » ou les deux."""
    parts = []
    if t.get("gpio") is not None:
        parts.append(f"GPIO{t['gpio']}")
    if t.get("udp"):
        parts.append(f"UDP « {t['udp']} »")
    return " / ".join(parts) or "déclencheur vide"


def udp_key(message):
    """Forme comparable d'un message UDP : sans espaces autour ni majuscules."""
    return (message or "").strip().lower()


def media_kind(name):
    ext = Path(name).suffix.lower()
    if ext in VIDEO_EXT:
        return "video"
    if ext in IMAGE_EXT:
        return "image"
    if ext in SUBTITLE_EXT:
        return "subtitle"
    return None


def fps_of(stream):
    """Cadence d'une piste vidéo ffprobe (ex. "30000/1001" -> 29.97)."""
    for key in ("avg_frame_rate", "r_frame_rate"):
        num, _, den = (stream.get(key) or "0/0").partition("/")
        if den and float(den):
            return float(num) / float(den)
    return 0


def player_name(cfg):
    return cfg.get("name") or socket.gethostname()


def hostname_for(name):
    """Nom d'hôte tiré du nom du lecteur : « Hall A (Expo) » -> « hall-a-expo »."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
    return slug[:63].rstrip("-") or "darksign"


def ap_ssid(cfg):
    """Nom du Wi-Fi du point d'accès : réglé, sinon le nom du lecteur."""
    name = cfg["network"].get("ap_ssid") or player_name(cfg)
    while len(name.encode()) > 32:   # limite du Wi-Fi, en octets
        name = name[:-1]
    return name


def hdmi_audio_device(name):
    """Sortie HDMI : périphérique alsa « hdmi » plutôt que « plughw »."""
    # plughw envoie un en-tête IEC958 sans fréquence d'échantillonnage ;
    # certains projecteurs (EPSON) restent alors muets. Le périphérique hdmi
    # le remplit.
    prefix = "alsa/plughw:CARD=vc4hdmi"
    if name.startswith(prefix):
        return "alsa/hdmi:CARD=vc4hdmi" + name[len(prefix):]
    return name


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        saved = json.loads(CONFIG_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return cfg
    for key, value in saved.items():
        if isinstance(value, dict) and isinstance(cfg.get(key), dict):
            cfg[key].update(value)
        else:
            cfg[key] = value
    # nom du point d'accès généré avant le nom du lecteur : il suit désormais
    # ce nom (vide)
    if cfg["network"].get("ap_ssid") == f"darksign-{socket.gethostname()}":
        cfg["network"]["ap_ssid"] = None
    # configurations enregistrées avant le passage au périphérique hdmi : le
    # lecteur et l'interface (liste des sorties) voient la même valeur
    cfg["audio_device"] = hdmi_audio_device(cfg.get("audio_device") or "auto")
    # ancien format de la boucle simple : un seul média
    old = cfg["loop"].pop("media", None)
    if old and not cfg["loop"]["items"]:
        cfg["loop"]["items"] = [{"media": old, "repeat": 1}]
    return cfg


def save_config(cfg):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # écriture atomique : le lecteur ne lit jamais un fichier à moitié écrit
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CONFIG_FILE)


def reserved_gpios():
    """GPIO utilisées par une fonction alternative (UART, I2C, SPI...)."""
    out = subprocess.run(["pinctrl", "get", "2-27"],
                         capture_output=True, text=True).stdout
    reserved = {}
    for line in out.splitlines():
        m = re.match(r"^\s*(\d+):\s+(\S+).*=\s*(.*)$", line)
        if m and m.group(2) not in ("ip", "op"):
            reserved[int(m.group(1))] = m.group(3).strip()
    return reserved


def available_gpios():
    reserved = reserved_gpios()
    return [
        {"gpio": bcm, "pin": pin}
        for bcm, pin in sorted(PHYSICAL.items())
        if bcm not in reserved
    ]


def network_addresses():
    """Adresses IPv4 du Pi (hors boucle locale), interface filaire en tête."""
    out = subprocess.run(["ip", "-4", "-o", "addr", "show", "scope", "global"],
                         capture_output=True, text=True).stdout
    found = []
    for line in out.splitlines():
        parts = line.split()
        iface, addr = parts[1], parts[3].split("/")[0]
        found.append((0 if iface.startswith(("eth", "en")) else 1, addr))
    return [addr for _, addr in sorted(found)]


def network_status():
    """Diagnostic réseau (pour l'écran « pas de connexion ») : Wi-Fi configuré
    et son état, câble Ethernet branché ou non."""
    def nmcli(*args):
        return subprocess.run(["nmcli", "-t", *args], capture_output=True,
                              text=True).stdout.splitlines()

    wifi = {"present": False, "ssid": None, "state": None}
    ethernet = {"present": False, "carrier": False}
    for line in nmcli("-f", "DEVICE,TYPE,STATE", "device"):
        dev, kind, state = (line.split(":") + ["", ""])[:3]
        if kind == "wifi":
            wifi.update(present=True, state=state)
        elif kind == "ethernet":
            ethernet["present"] = True
            try:
                ethernet["carrier"] = open(f"/sys/class/net/{dev}/carrier").read().strip() == "1"
            except OSError:
                pass
    for line in nmcli("-f", "NAME,TYPE", "connection", "show"):
        name, _, kind = line.rpartition(":")
        if kind == "802-11-wireless":
            ssid = nmcli("-g", "802-11-wireless.ssid", "connection", "show", name)
            wifi["ssid"] = ssid[0] if ssid else name
            break
    return {"wifi": wifi, "ethernet": ethernet}


def mdns_available():
    return subprocess.run(["systemctl", "is-active", "--quiet", "avahi-daemon"]
                          ).returncode == 0


def player_request(cmd, **args):
    """Envoie une commande au lecteur via le socket unix, renvoie sa réponse."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(3)
        s.connect(SOCKET_PATH)
        s.sendall((json.dumps({"cmd": cmd, **args}) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    return json.loads(data)

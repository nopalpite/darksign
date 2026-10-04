"""Shared by the player (player.py) and the web backend (web.py)."""
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

# reserved UDP messages, valid in every mode
UDP_COMMANDS = ("pause", "play", "restart")
UDP_MAX_LEN = 64

IMAGE_DURATION = 6   # s: default display time of an image in a playlist
IMAGE_DURATION_MAX = 3600

# subtitle sizes (mpv pixels, for a 720-line reference)
SUBTITLE_SIZES = {"small": 36, "medium": 48, "large": 64}

# BCM -> physical pin of the 40-pin header
PHYSICAL = {
    2: 3, 3: 5, 4: 7, 5: 29, 6: 31, 7: 26, 8: 24, 9: 21, 10: 19, 11: 23,
    12: 32, 13: 33, 14: 8, 15: 10, 16: 36, 17: 11, 18: 12, 19: 35, 20: 38,
    21: 40, 22: 15, 23: 16, 24: 18, 25: 22, 26: 37, 27: 13,
}

DEFAULT_CONFIG = {
    # player name ("Hall A"): displayed, and turned into the hostname (hall-a,
    # http://hall-a.local:8080) and the access point Wi-Fi name.
    # Empty: the current hostname.
    "name": None,
    # player language: TV splash screen (the web UI follows each browser)
    "language": "en",
    "mode": "loop",                # "loop" (playlist) | "interactive"
    "volume": 100,
    "audio_device": "auto",
    "udp_port": 5000,              # UDP messages: triggers and commands
    # network: venue Wi-Fi (client) or the player's own access point (ap).
    # Empty access point name: the player name; password generated on first
    # start (network.py)
    "network": {"mode": "client", "ap_ssid": None, "ap_password": None},
    # playlist: a single entry loops forever; otherwise entries play in
    # order, each video "repeat" times, each image for "duration" seconds
    "loop": {"items": [], "muted": False},   # [{"media": "a.mp4", "repeat": 1}]
    "interactive": {
        "attract": None,
        "attract_muted": True,
        "triggers_muted": False,
        "interruptible": True,
        "active_low": True,        # button wired to GND, internal pull-up
        # triggers: GPIO pin and/or UDP message -> video
        "triggers": [],            # [{"gpio": 17, "udp": "intro", "media": "video.mp4"}]
    },
    "subtitles": {},               # {"video.mp4": "video.srt"}
    "subtitle_style": {"size": "medium", "background": True},
}


def trigger_label(t):
    """Language-neutral trigger name: "GPIO17", 'UDP "intro"' or both."""
    parts = []
    if t.get("gpio") is not None:
        parts.append(f"GPIO{t['gpio']}")
    if t.get("udp"):
        parts.append(f'UDP "{t["udp"]}"')
    return " / ".join(parts) or "-"


def udp_key(message):
    """Comparable form of a UDP message: trimmed, lower case."""
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
    """Frame rate of an ffprobe video stream ("30000/1001" -> 29.97)."""
    for key in ("avg_frame_rate", "r_frame_rate"):
        num, _, den = (stream.get(key) or "0/0").partition("/")
        if den and float(den):
            return float(num) / float(den)
    return 0


def player_name(cfg):
    return cfg.get("name") or socket.gethostname()


def hostname_for(name):
    """Hostname derived from the player name: "Hall A (Expo)" -> "hall-a-expo"."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
    return slug[:63].rstrip("-") or "darksign"


def ap_ssid(cfg):
    """Access point Wi-Fi name: the configured one, else the player name."""
    name = cfg["network"].get("ap_ssid") or player_name(cfg)
    while len(name.encode()) > 32:   # Wi-Fi limit, in bytes
        name = name[:-1]
    return name


def hdmi_audio_device(name):
    """HDMI output: the alsa "hdmi" device rather than "plughw"."""
    # plughw sends an IEC958 header without the sample rate: some projectors
    # (EPSON) then stay silent. The hdmi device fills it in.
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
    # access point name generated before the player name existed: it now
    # follows that name (empty)
    if cfg["network"].get("ap_ssid") == f"darksign-{socket.gethostname()}":
        cfg["network"]["ap_ssid"] = None
    # configurations saved before the switch to the hdmi device: the player
    # and the web UI (list of outputs) see the same value
    cfg["audio_device"] = hdmi_audio_device(cfg.get("audio_device") or "auto")
    # old "simple loop" format: a single media
    old = cfg["loop"].pop("media", None)
    if old and not cfg["loop"]["items"]:
        cfg["loop"]["items"] = [{"media": old, "repeat": 1}]
    return cfg


def save_config(cfg):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # atomic write: the player never reads a half-written file
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CONFIG_FILE)


def reserved_gpios():
    """GPIOs used by an alternate function (UART, I2C, SPI...)."""
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
    """IPv4 addresses of the Pi (no loopback), wired interface first."""
    out = subprocess.run(["ip", "-4", "-o", "addr", "show", "scope", "global"],
                         capture_output=True, text=True).stdout
    found = []
    for line in out.splitlines():
        parts = line.split()
        iface, addr = parts[1], parts[3].split("/")[0]
        found.append((0 if iface.startswith(("eth", "en")) else 1, addr))
    return [addr for _, addr in sorted(found)]


def network_status():
    """Network diagnosis (for the "no connection" screen): configured Wi-Fi
    and its state, Ethernet cable plugged in or not."""
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


def pi_model():
    """Raspberry Pi model ("Raspberry Pi 3 Model B Rev 1.2")."""
    try:
        with open("/proc/device-tree/model") as f:
            return f.read().strip("\0\n ")
    except OSError:
        return ""


def slow_transcode():
    """Pi 3 and Zero 2 (same chip, 1 GB at most): converting a video on the
    Pi saturates it; better upload it in the right format already."""
    model = pi_model()
    return "Raspberry Pi 3" in model or "Zero 2" in model


def sudo_allowed(command):
    """Is this exact command in a password-less sudo rule (install.sh's)?
    "sudo -l COMMAND" is not enough: it also says yes for a rule that asks
    for a password (sudo group)."""
    out = subprocess.run(["sudo", "-n", "-l"], capture_output=True, text=True).stdout
    rules = []   # one rule per "(root) ...", continuation lines appended
    for line in out.splitlines():
        if line.strip().startswith("("):
            rules.append(line.strip())
        elif rules and line.startswith("    ") and line.strip():
            rules[-1] += " " + line.strip()
    for rule in rules:
        _, sep, commands = rule.partition("NOPASSWD:")
        if sep and command in (c.strip() for c in commands.split(",")):
            return True
    return False


def player_request(cmd, **args):
    """Send a command to the player over the unix socket, return its reply."""
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

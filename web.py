#!/usr/bin/env python3
"""Backend web d'administration du lecteur vidéo."""
import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time

from flask import Flask, jsonify, render_template, request
from PIL import Image, ImageOps
from werkzeug.utils import secure_filename

from common import (IMAGE_DURATION, IMAGE_DURATION_MAX, MEDIA_DIR, ap_ssid,
                    hostname_for, player_name, sudo_allowed,
                    SUBTITLE_SIZES, UDP_COMMANDS, UDP_MAX_LEN, available_gpios,
                    fps_of, load_config, media_kind, player_request,
                    save_config, trigger_label, udp_key)
from transcode import INCOMING_DIR, Converter
import network

app = Flask(__name__)
PORT = 8080
SYSTEMCTL = "/usr/bin/systemctl"
SYSTEM_ACTIONS = ("reboot", "poweroff")   # autorisées par /etc/sudoers.d/darksign
HOSTNAME_HELPER = "/usr/local/sbin/darksign-hostname"   # idem (install.sh)
NAME_MAX = 40

_probe_cache = {}  # nom -> (mtime, infos)


def probe(path):
    """Durée, résolution et codec d'un média (via ffprobe, mis en cache)."""
    mtime = path.stat().st_mtime
    cached = _probe_cache.get(path.name)
    if cached and cached[0] == mtime:
        return cached[1]
    info = {}
    if media_kind(path.name) == "subtitle":
        text = path.read_text(errors="replace")
        info = {"cues": text.count("-->") or text.count("Dialogue:")}
        _probe_cache[path.name] = (mtime, info)
        return info
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-of", "json",
             "-show_entries", "format=duration:stream=codec_type,codec_name,width,height,avg_frame_rate",
             str(path)],
            capture_output=True, text=True, errors="replace", timeout=15,
        ).stdout
        data = json.loads(out)
        video = next((s for s in data.get("streams", [])
                      if s.get("codec_type") == "video"), {})
        info = {
            "fps": round(fps_of(video), 2) if video else None,
            "codec": video.get("codec_name"),
            "width": video.get("width"),
            "height": video.get("height"),
            "audio": any(s.get("codec_type") == "audio" for s in data.get("streams", [])),
        }
        if media_kind(path.name) == "video":
            info["duration"] = float(data.get("format", {}).get("duration", 0)) or None
    except (subprocess.SubprocessError, ValueError):
        pass
    _probe_cache[path.name] = (mtime, info)
    return info


def warnings_for(kind, info):
    warn = []
    if kind == "video":
        if info.get("codec") and info["codec"] != "h264":
            warn.append(f"codec {info['codec']} : pas de décodage matériel sur ce Pi, "
                        "risque de saccades (préférez du H.264)")
        if (info.get("width") or 0) > 1920 or (info.get("height") or 0) > 1080:
            warn.append("résolution supérieure à 1080p : non supportée en matériel")
        elif (info.get("height") or 0) > 720 and (info.get("fps") or 0) > 30:
            warn.append(f"{info['fps']:g} i/s en 1080p : au-delà des 30 i/s garantis "
                        "par le décodeur du Pi 3, vérifiez la fluidité")
    if kind == "image" and ((info.get("width") or 0) > 2048 or (info.get("height") or 0) > 2048):
        warn.append("image trop grande pour le GPU du Pi (2048 px max) : renvoyez-la "
                    "pour qu'elle soit redimensionnée")
    return warn


def list_media():
    items = []
    for path in sorted(MEDIA_DIR.iterdir(), key=lambda p: p.name.lower()):
        kind = media_kind(path.name)
        if not kind or not path.is_file():
            continue
        info = probe(path)
        items.append({
            "name": path.name, "kind": kind, "size": path.stat().st_size,
            **info, "warnings": warnings_for(kind, info),
        })
    return items


def media_usage(cfg, name):
    uses = []
    if any(it["media"] == name for it in cfg["loop"]["items"]):
        uses.append("playlist")
    if cfg["interactive"]["attract"] == name:
        uses.append("accroche")
    for t in cfg["interactive"]["triggers"]:
        if t["media"] == name:
            uses.append(trigger_label(t))
    for video, sub in cfg["subtitles"].items():
        if sub == name:
            uses.append(f"sous-titres de {video}")
    return uses


def prepare_image(path):
    """Adapte une image à l'écran : rotation EXIF appliquée, 1920×1080 maximum.

    Le GPU du Pi 3 refuse les textures de plus de 2048 pixels : une photo plus
    grande s'affiche mal. L'image n'est réécrite que si elle doit changer.
    """
    with Image.open(path) as im:
        fmt = im.format
        if getattr(im, "n_frames", 1) > 1:
            return   # GIF animé : laissé tel quel
        if fmt == "JPEG":
            im.draft("RGB", (1920, 1080))   # décodage réduit : moins de mémoire
        orientation = im.getexif().get(0x0112, 1)
        too_big = im.width > 1920 or im.height > 1080
        if orientation == 1 and not too_big and im.mode in ("RGB", "RGBA", "L"):
            return
        out = ImageOps.exif_transpose(im)
        if out.mode not in ("RGB", "RGBA", "L"):
            out = out.convert("RGBA" if "A" in out.mode else "RGB")
        out.thumbnail((1920, 1080), Image.LANCZOS)
        if fmt == "JPEG" and out.mode == "RGBA":
            out = out.convert("RGB")
        options = {"quality": 92} if fmt in ("JPEG", "WEBP") else {}
        out.save(path, format=fmt, **options)


def normalize_subtitle(raw, name):
    """Vérifie un fichier de sous-titres et le convertit en UTF-8."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")   # fichiers Windows
    marker = "[Events]" if name.lower().endswith(".ass") else "-->"
    if marker not in text:
        raise ValueError("fichier de sous-titres illisible ou vide")
    return text.replace("\r\n", "\n").encode("utf-8")


def player_status():
    try:
        return player_request("status")
    except OSError:
        return None


def reload_player():
    try:
        player_request("reload")
    except OSError:
        pass


def media_ready(name):
    if media_usage(load_config(), name):
        reload_player()  # le fichier remplacé est en cours d'utilisation


converter = Converter(on_done=media_ready)


def playlist_item(it):
    """Entrée de playlist nettoyée : répétitions (vidéo) ou durée (image)."""
    if media_kind(it["media"]) == "image":
        duration = float(it.get("duration") or IMAGE_DURATION)
        return {"media": it["media"],
                "duration": round(max(1, min(IMAGE_DURATION_MAX, duration)), 1)}
    return {"media": it["media"], "repeat": max(1, min(999, int(it.get("repeat") or 1)))}


def validate(cfg):
    errors = []
    media = {m["name"]: m["kind"] for m in list_media()}
    gpios = {g["gpio"] for g in available_gpios()}

    if cfg.get("mode") not in ("loop", "interactive"):
        errors.append("mode inconnu")
    try:
        cfg["volume"] = max(0, min(100, int(cfg.get("volume", 100))))
    except (TypeError, ValueError):
        errors.append("volume invalide")

    playable = {n for n, k in media.items() if k in ("video", "image")}
    items = cfg["loop"]["items"]
    for it in items:
        if it["media"] not in playable:
            errors.append(f"playlist : média introuvable ({it['media']})")
    if cfg["mode"] == "loop" and not items:
        errors.append("playlist : ajoutez au moins un média")

    inter = cfg["interactive"]
    if inter.get("attract") and inter["attract"] not in playable:
        errors.append(f"accroche : média introuvable ({inter['attract']})")
    seen, seen_udp = set(), set()
    for t in inter["triggers"]:
        gpio, udp, label = t.get("gpio"), t.get("udp"), trigger_label(t)
        if gpio is None and not udp:
            errors.append("déclencheur sans GPIO ni message UDP")
        if gpio is not None:
            if gpio not in gpios:
                errors.append(f"GPIO{gpio} n'est pas disponible")
            if gpio in seen:
                errors.append(f"GPIO{gpio} est utilisée plusieurs fois")
            seen.add(gpio)
        if udp:
            if udp_key(udp) in UDP_COMMANDS:
                errors.append(f"message UDP « {udp} » réservé aux commandes "
                              f"({', '.join(UDP_COMMANDS)})")
            if udp_key(udp) in seen_udp:
                errors.append(f"message UDP « {udp} » utilisé plusieurs fois")
            seen_udp.add(udp_key(udp))
        if media.get(t.get("media")) != "video":
            errors.append(f"{label} : choisissez une vidéo")

    net = cfg["network"]
    if net.get("mode") not in ("client", "ap"):
        errors.append("réseau : mode inconnu")
    # nom du point d'accès vide : celui du lecteur
    net["ap_ssid"] = str(net.get("ap_ssid") or "").strip() or None
    net["ap_password"] = str(net.get("ap_password") or "")
    if net["ap_ssid"] and len(net["ap_ssid"].encode()) > 32:
        errors.append("point d'accès : nom de 32 caractères au maximum")
    cfg["name"] = str(cfg.get("name") or "").strip() or None
    if cfg["name"] and len(cfg["name"]) > NAME_MAX:
        errors.append(f"nom du lecteur : {NAME_MAX} caractères au maximum")
    if not (8 <= len(net["ap_password"]) <= 63 and net["ap_password"].isascii()
            and net["ap_password"].isprintable()):
        errors.append("point d'accès : mot de passe de 8 à 63 caractères "
                      "(sans accents)")

    try:
        port = int(cfg.get("udp_port"))
        if not 1024 <= port <= 65535:
            raise ValueError
        cfg["udp_port"] = port
    except (TypeError, ValueError):
        errors.append("port UDP invalide (1024 à 65535)")

    for video, sub in cfg["subtitles"].items():
        if media.get(video) != "video" or media.get(sub) != "subtitle":
            errors.append(f"sous-titres invalides pour {video}")
    style = cfg["subtitle_style"]
    if style.get("size") not in SUBTITLE_SIZES:
        errors.append("taille de sous-titres inconnue")
    style["background"] = bool(style.get("background"))
    return errors


@app.get("/")
def index():
    return render_template("index.html")


# --- tests de connectivité des appareils (point d'accès) --------------------------
# Sur le point d'accès, tous les noms pointent vers le lecteur et le port 80 y
# est redirigé (system/darksign-ap). Sans ces réponses, les téléphones jugent
# le Wi-Fi « sans Internet » et passent par les données mobiles, même pour
# joindre le lecteur. Android vérifie aussi Google en HTTPS, impossible ici :
# il propose alors « Connexion limitée : se connecter quand même ».

@app.get("/generate_204")
@app.get("/gen_204")
def probe_android():
    return "", 204


@app.get("/hotspot-detect.html")
@app.get("/library/test/success.html")
def probe_apple():
    return "<HTML><HEAD><TITLE>Success</TITLE></HEAD><BODY>Success</BODY></HTML>"


@app.get("/connecttest.txt")
def probe_windows():
    return "Microsoft Connect Test", 200, {"Content-Type": "text/plain"}


@app.get("/ncsi.txt")
def probe_windows_legacy():
    return "Microsoft NCSI", 200, {"Content-Type": "text/plain"}


@app.get("/api/state")
def api_state():
    return jsonify(
        config=load_config(),
        media=list_media(),
        gpios=available_gpios(),
        player=player_status(),
        jobs=converter.list(),
        system={"power": system_allowed("reboot"), "rename": rename_allowed(),
                "name": player_name(load_config()), "hostname": socket.gethostname()},
    )


@app.get("/api/status")
def api_status():
    return jsonify(player=player_status(), jobs=converter.list(),
                   health=system_health(), network=supervisor.status)


# --- réseau --------------------------------------------------------------------

def network_call(fn, *args):
    try:
        return jsonify(ok=True, result=fn(*args))
    except network.NetworkError as e:
        return jsonify(error=str(e)), 400


@app.get("/api/network")
def api_network():
    try:
        saved = network.saved_networks()
    except network.NetworkError as e:
        saved, supervisor.status["error"] = [], str(e)
    return jsonify(status=supervisor.status, saved=saved, allowed=network_allowed())


@app.get("/api/network/scan")
def api_network_scan():
    return network_call(network.scan)


@app.post("/api/network/wifi")
def api_network_add():
    body = request.get_json(force=True)
    ssid = str(body.get("ssid") or "").strip()
    password = str(body.get("password") or "")
    if not 1 <= len(ssid.encode()) <= 32:
        return jsonify(error="nom de réseau de 1 à 32 caractères"), 400
    if password and not 8 <= len(password) <= 63:
        return jsonify(error="mot de passe Wi-Fi de 8 à 63 caractères"), 400
    return network_call(network.add_network, ssid, password)


@app.delete("/api/network/wifi/<path:name>")
def api_network_forget(name):
    return network_call(network.forget_network, name)


@app.post("/api/network/connect")
def api_network_connect():
    # en tâche de fond : quitter le point d'accès coupe cette requête
    threading.Thread(target=supervisor.connect_now, daemon=True).start()
    return jsonify(ok=True)


@app.post("/api/config")
def api_config():
    cfg = load_config()
    body = request.get_json(force=True)
    for key in ("mode", "volume", "audio_device", "udp_port", "name"):
        if key in body:
            cfg[key] = body[key]
    old_network = network_settings(load_config())
    old_name = load_config()["name"]
    for key in ("loop", "interactive", "subtitle_style", "network"):
        if isinstance(body.get(key), dict):
            cfg[key].update(body[key])
    if isinstance(body.get("subtitles"), dict):
        # association vidéo -> sous-titres ; une valeur vide retire l'association
        cfg["subtitles"] = {v: s for v, s in body["subtitles"].items() if s}
    try:
        cfg["loop"]["items"] = [playlist_item(it)
                                for it in cfg["loop"]["items"] if it.get("media")]
    except (TypeError, ValueError, KeyError):
        return jsonify(errors=["playlist : répétitions ou durée invalide"]), 400
    try:
        cfg["interactive"]["triggers"] = [
            {"gpio": None if t.get("gpio") in (None, "") else int(t["gpio"]),
             "udp": (t.get("udp") or "").strip()[:UDP_MAX_LEN] or None,
             "media": t.get("media")}
            for t in cfg["interactive"]["triggers"]
        ]
    except (TypeError, ValueError):
        return jsonify(errors=["déclencheur : GPIO invalide"]), 400
    errors = validate(cfg)
    network_changed = network_settings(cfg) != old_network
    ap_changed = network_settings(cfg)[1:] != old_network[1:]
    if network_changed and not network_allowed():
        errors.append("réseau : droits manquants, relancez l'installateur "
                      "(sudo ./install.sh)")
    new_hostname = None
    if cfg["name"] and cfg["name"] != old_name:
        new_hostname = hostname_for(cfg["name"])
        if new_hostname == socket.gethostname():
            new_hostname = None
        elif not rename_allowed():
            errors.append("nom du lecteur : droits manquants pour changer le nom "
                          "d'hôte, relancez l'installateur (sudo ./install.sh)")
    if errors:
        return jsonify(errors=errors), 400
    save_config(cfg)

    def apply():
        # en tâche de fond : changer de mode peut couper la connexion en cours
        if new_hostname:
            rename_host(new_hostname)
        if network_changed:
            supervisor.apply(cfg, ap_changed=ap_changed)
        reload_player()   # écran d'accueil : nouveau nom, nouveau réseau

    threading.Thread(target=apply, daemon=True).start()
    return jsonify(ok=True, hostname=new_hostname)


def network_settings(cfg):
    """Réglages réseau effectifs (nom du point d'accès compris)."""
    net = cfg["network"]
    return net["mode"], ap_ssid(cfg), net["ap_password"]


def network_allowed():
    """Wi-Fi du lieu (polkit) et point d'accès (sudo) : règles d'install.sh."""
    return network.allowed() and network.ap_allowed()


def rename_allowed():
    return sudo_allowed(HOSTNAME_HELPER)


def rename_host(hostname):
    res = subprocess.run(["sudo", "-n", HOSTNAME_HELPER, hostname],
                         capture_output=True, text=True, timeout=60)
    if res.returncode:
        logging.getLogger("web").error("nom d'hôte : %s", res.stderr.strip())
    else:
        logging.getLogger("web").info("nom d'hôte : %s", hostname)


@app.put("/api/media/<path:filename>")
def api_upload(filename):
    # Le fichier est envoyé brut et écrit directement sur la carte SD :
    # pas de copie temporaire en RAM (/tmp), indispensable pour les grosses vidéos.
    # Les vidéos passent par la file de conversion, les images sont prêtes.
    name = secure_filename(filename)
    kind = media_kind(name)
    if not name or not kind:
        return jsonify(error="format non supporté"), 400
    if kind == "subtitle":
        limit = 5 * 1024 * 1024
        raw = request.stream.read(limit + 1)
        if len(raw) > limit:
            return jsonify(error="fichier de sous-titres trop gros"), 400
        try:
            data = normalize_subtitle(raw, name)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        tmp = MEDIA_DIR / f".{os.urandom(4).hex()}.upload"
        tmp.write_bytes(data)
        os.replace(tmp, MEDIA_DIR / name)
        media_ready(name)   # rechargement si ces sous-titres sont affichés
        return jsonify(ok=True, name=name)

    dest_dir = INCOMING_DIR if kind == "video" else MEDIA_DIR
    tmp = dest_dir / f".{os.urandom(4).hex()}.upload"
    try:
        with open(tmp, "wb") as f:
            while chunk := request.stream.read(1024 * 1024):
                f.write(chunk)
        if kind == "video":
            job = converter.add(tmp, name)
            return jsonify(ok=True, name=job.name, job=job.id)
        try:
            prepare_image(tmp)
        except OSError as e:
            return jsonify(error=f"image illisible : {e}"), 400
        os.replace(tmp, dest_dir / name)
    finally:
        tmp.unlink(missing_ok=True)
    media_ready(name)
    return jsonify(ok=True, name=name)


@app.post("/api/jobs/<job_id>/retry")
def api_retry_job(job_id):
    return jsonify(ok=converter.retry(job_id))


@app.delete("/api/jobs/<job_id>")
def api_cancel_job(job_id):
    return jsonify(ok=converter.remove(job_id))


@app.delete("/api/media/<path:name>")
def api_delete(name):
    path = MEDIA_DIR / secure_filename(name)
    if not path.is_file():
        return jsonify(error="fichier introuvable"), 404
    cfg = load_config()
    uses = media_usage(cfg, path.name)
    if uses:
        return jsonify(error=f"utilisé par : {', '.join(uses)}"), 409
    path.unlink()
    if cfg["subtitles"].pop(path.name, None):   # vidéo supprimée
        save_config(cfg)
    return jsonify(ok=True)


@app.post("/api/player/pause")
def api_pause():
    paused = bool(request.get_json(force=True).get("paused", True))
    try:
        return jsonify(player_request("pause", paused=paused))
    except OSError:
        return jsonify(error="lecteur injoignable"), 503


@app.post("/api/player/restart")
def api_restart():
    # rechargement de la configuration : la lecture repart du début
    try:
        return jsonify(player_request("reload"))
    except OSError:
        return jsonify(error="lecteur injoignable"), 503


# vcgencmd get_throttled : bits « maintenant » (0-3) et « depuis le démarrage » (16-19)
THROTTLE_FLAGS = [
    (0, "under_voltage", "alimentation insuffisante"),
    (1, "freq_capped", "fréquence plafonnée"),
    (2, "throttled", "processeur ralenti"),
    (3, "temp_limit", "limite de température atteinte"),
]
_health = {"time": 0, "data": None}


def system_health():
    """Santé du Pi (mise en cache 5 s : l'interface interroge chaque seconde)."""
    if time.monotonic() - _health["time"] < 5:
        return _health["data"]
    data = {}
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            data["temp"] = round(int(f.read()) / 1000, 1)
    except (OSError, ValueError):
        data["temp"] = None
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True,
                             text=True, timeout=5).stdout
        bits = int(out.strip().split("=")[1], 16)
        data["power"] = {
            "now": [label for bit, _, label in THROTTLE_FLAGS if bits >> bit & 1],
            "since_boot": [label for bit, _, label in THROTTLE_FLAGS
                           if bits >> (bit + 16) & 1],
        }
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        data["power"] = None
    disk = shutil.disk_usage(MEDIA_DIR)
    data["disk"] = {"free": disk.free, "total": disk.total}
    try:
        mem = dict(line.split(":") for line in open("/proc/meminfo"))
        kb = lambda k: int(mem[k].split()[0]) * 1024
        data["memory"] = {"available": kb("MemAvailable"), "total": kb("MemTotal")}
    except (OSError, KeyError, ValueError):
        data["memory"] = None
    data["load"] = round(os.getloadavg()[0], 2)
    data["cpus"] = os.cpu_count()
    try:
        data["uptime"] = float(open("/proc/uptime").read().split()[0])
    except (OSError, ValueError):
        data["uptime"] = None
    _health.update(time=time.monotonic(), data=data)
    return data


def system_allowed(action):
    """Droit de redémarrer / éteindre (règle sudo posée par install.sh)."""
    return sudo_allowed(f"{SYSTEMCTL} {action}")


@app.post("/api/system/<action>")
def api_system(action):
    if action not in SYSTEM_ACTIONS:
        return jsonify(error="action inconnue"), 404
    if not system_allowed(action):
        return jsonify(error="droits manquants : relancez l'installateur "
                             "(sudo ./install.sh)"), 403
    # la réponse part avant l'arrêt : systemctl rend la main tout de suite
    subprocess.Popen(["sudo", "-n", SYSTEMCTL, action])
    return jsonify(ok=True)


@app.post("/api/trigger-udp")
def api_trigger_udp():
    # test de bout en bout : un vrai datagramme, reçu comme ceux du réseau
    message = str(request.get_json(force=True).get("message") or "").strip()
    if not message:
        return jsonify(error="message vide"), 400
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.sendto(message.encode(), ("127.0.0.1", int(load_config()["udp_port"])))
    return jsonify(ok=True)


@app.post("/api/trigger/<int:gpio>")
def api_trigger(gpio):
    try:
        return jsonify(player_request("trigger", gpio=gpio))
    except OSError:
        return jsonify(error="lecteur injoignable"), 503


def start_network():
    """Mot de passe du point d'accès (généré une fois) et mode réseau."""
    global supervisor
    cfg = load_config()
    if not cfg["network"]["ap_password"]:
        cfg["network"]["ap_password"] = network.generate_password()
        save_config(cfg)
    supervisor = network.Supervisor()
    if network_allowed():
        threading.Thread(target=supervisor.apply, args=(cfg,), daemon=True).start()
    else:
        supervisor.mode = cfg["network"]["mode"]
        supervisor.error = ("droits manquants : relancez l'installateur "
                            "(sudo ./install.sh)")
        logging.getLogger("network").warning(supervisor.error)


supervisor = None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    start_network()
    app.run(host="0.0.0.0", port=PORT, threaded=True)

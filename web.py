#!/usr/bin/env python3
"""Web administration backend of the video player.

Messages for the web UI (errors, warnings, usages...) are structured
messages, {"key": ..., "vars": {...}} (i18n.msg), translated by the browser
in each viewer's language; the translation files are served under /locales.
"""
import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time

from flask import Flask, abort, jsonify, render_template, request, send_from_directory
from PIL import Image, ImageOps
from werkzeug.utils import secure_filename

import i18n
import network
from common import (IMAGE_DURATION, IMAGE_DURATION_MAX, MEDIA_DIR, SUBTITLE_SIZES,
                    UDP_COMMANDS, UDP_MAX_LEN, ap_ssid, available_gpios, fps_of,
                    hostname_for, load_config, media_kind, pi_model,
                    player_name, player_request, save_config, slow_transcode,
                    sudo_allowed, trigger_label, udp_key)
from i18n import msg
from transcode import INCOMING_DIR, Converter

app = Flask(__name__)
log = logging.getLogger("web")
PORT = 8080
SYSTEMCTL = "/usr/bin/systemctl"
SYSTEM_ACTIONS = ("reboot", "poweroff")   # allowed by /etc/sudoers.d/darksign
HOSTNAME_HELPER = "/usr/local/sbin/darksign-hostname"   # likewise (install.sh)
NAME_MAX = 40

_probe_cache = {}  # name -> (mtime, info)


def probe(path):
    """Duration, resolution and codec of a media (ffprobe, cached)."""
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
            warn.append(msg("media.warning.codec", codec=info["codec"]))
        if (info.get("width") or 0) > 1920 or (info.get("height") or 0) > 1080:
            warn.append(msg("media.warning.resolution"))
        elif (info.get("height") or 0) > 720 and (info.get("fps") or 0) > 30:
            warn.append(msg("media.warning.fps", fps=f"{info['fps']:g}"))
    if kind == "image" and ((info.get("width") or 0) > 2048 or (info.get("height") or 0) > 2048):
        warn.append(msg("media.warning.image_size"))
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
    """Where a media is used, as structured messages."""
    uses = []
    if any(it["media"] == name for it in cfg["loop"]["items"]):
        uses.append(msg("media.use.playlist"))
    if cfg["interactive"]["attract"] == name:
        uses.append(msg("media.use.attract"))
    for t in cfg["interactive"]["triggers"]:
        if t["media"] == name:
            uses.append(msg("media.use.trigger", trigger=trigger_label(t)))
    for video, sub in cfg["subtitles"].items():
        if sub == name:
            uses.append(msg("media.use.subtitles", video=video))
    return uses


def prepare_image(path):
    """Fit an image to the screen: EXIF rotation applied, 1920×1080 at most.

    The Pi 3 GPU rejects textures larger than 2048 pixels: a bigger photo
    displays badly. The image is only rewritten when it must change.
    """
    with Image.open(path) as im:
        fmt = im.format
        if getattr(im, "n_frames", 1) > 1:
            return   # animated GIF: left as is
        if fmt == "JPEG":
            im.draft("RGB", (1920, 1080))   # reduced decoding: less memory
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
    """Check a subtitle file and convert it to UTF-8."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")   # Windows files
    marker = "[Events]" if name.lower().endswith(".ass") else "-->"
    if marker not in text:
        raise ValueError("unreadable or empty subtitle file")
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
        reload_player()  # the replaced file is in use


converter = Converter(on_done=media_ready)


def playlist_item(it):
    """Cleaned playlist entry: repeats (video) or duration (image)."""
    if media_kind(it["media"]) == "image":
        duration = float(it.get("duration") or IMAGE_DURATION)
        return {"media": it["media"],
                "duration": round(max(1, min(IMAGE_DURATION_MAX, duration)), 1)}
    return {"media": it["media"], "repeat": max(1, min(999, int(it.get("repeat") or 1)))}


def validate(cfg):
    """Check and clean a configuration; return a list of messages."""
    errors = []
    media = {m["name"]: m["kind"] for m in list_media()}
    gpios = {g["gpio"] for g in available_gpios()}

    if cfg.get("mode") not in ("loop", "interactive"):
        errors.append(msg("config.error.mode"))
    try:
        cfg["volume"] = max(0, min(100, int(cfg.get("volume", 100))))
    except (TypeError, ValueError):
        errors.append(msg("config.error.volume"))
    if cfg.get("language") not in {l["code"] for l in i18n.languages()}:
        errors.append(msg("config.error.language"))

    playable = {n for n, k in media.items() if k in ("video", "image")}
    items = cfg["loop"]["items"]
    for it in items:
        if it["media"] not in playable:
            errors.append(msg("config.error.playlist_missing", media=it["media"]))
    if cfg["mode"] == "loop" and not items:
        errors.append(msg("config.error.playlist_empty"))

    inter = cfg["interactive"]
    if inter.get("attract") and inter["attract"] not in playable:
        errors.append(msg("config.error.attract_missing", media=inter["attract"]))
    seen, seen_udp = set(), set()
    for t in inter["triggers"]:
        gpio, udp, label = t.get("gpio"), t.get("udp"), trigger_label(t)
        if gpio is None and not udp:
            errors.append(msg("config.error.trigger_empty"))
        if gpio is not None:
            if gpio not in gpios:
                errors.append(msg("config.error.gpio_unavailable", gpio=gpio))
            if gpio in seen:
                errors.append(msg("config.error.gpio_duplicate", gpio=gpio))
            seen.add(gpio)
        if udp:
            if udp_key(udp) in UDP_COMMANDS:
                errors.append(msg("config.error.udp_reserved", message=udp,
                                  commands=", ".join(UDP_COMMANDS)))
            if udp_key(udp) in seen_udp:
                errors.append(msg("config.error.udp_duplicate", message=udp))
            seen_udp.add(udp_key(udp))
        if media.get(t.get("media")) != "video":
            errors.append(msg("config.error.trigger_video", trigger=label))

    net = cfg["network"]
    if net.get("mode") not in ("client", "ap"):
        errors.append(msg("config.error.network_mode"))
    # empty access point name: the player name
    net["ap_ssid"] = str(net.get("ap_ssid") or "").strip() or None
    net["ap_password"] = str(net.get("ap_password") or "")
    if net["ap_ssid"] and len(net["ap_ssid"].encode()) > 32:
        errors.append(msg("config.error.ap_ssid"))
    cfg["name"] = str(cfg.get("name") or "").strip() or None
    if cfg["name"] and len(cfg["name"]) > NAME_MAX:
        errors.append(msg("config.error.name", max=NAME_MAX))
    if not (8 <= len(net["ap_password"]) <= 63 and net["ap_password"].isascii()
            and net["ap_password"].isprintable()):
        errors.append(msg("config.error.ap_password"))

    try:
        port = int(cfg.get("udp_port"))
        if not 1024 <= port <= 65535:
            raise ValueError
        cfg["udp_port"] = port
    except (TypeError, ValueError):
        errors.append(msg("config.error.udp_port"))

    for video, sub in cfg["subtitles"].items():
        if media.get(video) != "video" or media.get(sub) != "subtitle":
            errors.append(msg("config.error.subtitles", video=video))
    style = cfg["subtitle_style"]
    if style.get("size") not in SUBTITLE_SIZES:
        errors.append(msg("config.error.subtitle_size"))
    style["background"] = bool(style.get("background"))
    return errors


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/locales/<lang>.json")
def locale_file(lang):
    if lang not in {l["code"] for l in i18n.languages()}:
        abort(404)
    return send_from_directory(i18n.LOCALES_DIR, f"{lang}.json", max_age=0)


# --- device connectivity checks (access point) ------------------------------------
# On the access point, every name points to the player and port 80 is
# redirected to it (system/darksign-ap). Without these answers, phones deem
# the Wi-Fi "without Internet" and use mobile data, even to reach the
# player. Android also checks Google over HTTPS, impossible here: it then
# offers "Limited connectivity: connect anyway".

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
        languages=i18n.languages(),
        system={"power": system_allowed("reboot"), "rename": rename_allowed(),
                "model": pi_model(), "slow_transcode": slow_transcode(),
                "name": player_name(load_config()), "hostname": socket.gethostname()},
    )


@app.get("/api/status")
def api_status():
    return jsonify(player=player_status(), jobs=converter.list(),
                   health=system_health(), network=supervisor.status)


# --- network ---------------------------------------------------------------------

def network_call(fn, *args):
    try:
        return jsonify(ok=True, result=fn(*args))
    except network.NetworkError as e:
        return jsonify(error=e.message), 400


@app.get("/api/network")
def api_network():
    try:
        saved = network.saved_networks()
    except network.NetworkError as e:
        saved, supervisor.status["error"] = [], e.message
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
        return jsonify(error=msg("network.error.ssid_length")), 400
    if password and not 8 <= len(password) <= 63:
        return jsonify(error=msg("network.error.password_length")), 400
    return network_call(network.add_network, ssid, password)


@app.delete("/api/network/wifi/<path:name>")
def api_network_forget(name):
    return network_call(network.forget_network, name)


@app.post("/api/network/connect")
def api_network_connect():
    # in the background: leaving the access point cuts this very request
    threading.Thread(target=supervisor.connect_now, daemon=True).start()
    return jsonify(ok=True)


@app.post("/api/config")
def api_config():
    cfg = load_config()
    body = request.get_json(force=True)
    for key in ("mode", "volume", "audio_device", "udp_port", "name", "language"):
        if key in body:
            cfg[key] = body[key]
    old_network = network_settings(load_config())
    old_name = load_config()["name"]
    for key in ("loop", "interactive", "subtitle_style", "network"):
        if isinstance(body.get(key), dict):
            cfg[key].update(body[key])
    if isinstance(body.get("subtitles"), dict):
        # video -> subtitles; an empty value removes the association
        cfg["subtitles"] = {v: s for v, s in body["subtitles"].items() if s}
    try:
        cfg["loop"]["items"] = [playlist_item(it)
                                for it in cfg["loop"]["items"] if it.get("media")]
    except (TypeError, ValueError, KeyError):
        return jsonify(errors=[msg("config.error.playlist_values")]), 400
    try:
        cfg["interactive"]["triggers"] = [
            {"gpio": None if t.get("gpio") in (None, "") else int(t["gpio"]),
             "udp": (t.get("udp") or "").strip()[:UDP_MAX_LEN] or None,
             "media": t.get("media")}
            for t in cfg["interactive"]["triggers"]
        ]
    except (TypeError, ValueError):
        return jsonify(errors=[msg("config.error.trigger_gpio")]), 400
    errors = validate(cfg)
    network_changed = network_settings(cfg) != old_network
    ap_changed = network_settings(cfg)[1:] != old_network[1:]
    if network_changed and not network_allowed():
        errors.append(msg("config.error.network_rights"))
    new_hostname = None
    if cfg["name"] and cfg["name"] != old_name:
        new_hostname = hostname_for(cfg["name"])
        if new_hostname == socket.gethostname():
            new_hostname = None
        elif not rename_allowed():
            errors.append(msg("config.error.rename_rights"))
    if errors:
        return jsonify(errors=errors), 400
    save_config(cfg)

    def apply():
        # in the background: changing the mode may cut the current connection
        if new_hostname:
            rename_host(new_hostname)
        if network_changed:
            supervisor.apply(cfg, ap_changed=ap_changed)
        reload_player()   # splash screen: new name, new network, new language

    threading.Thread(target=apply, daemon=True).start()
    return jsonify(ok=True, hostname=new_hostname)


def network_settings(cfg):
    """Effective network settings (access point name included)."""
    net = cfg["network"]
    return net["mode"], ap_ssid(cfg), net["ap_password"]


def network_allowed():
    """Venue Wi-Fi (polkit) and access point (sudo): install.sh's rules."""
    return network.allowed() and network.ap_allowed()


def rename_allowed():
    return sudo_allowed(HOSTNAME_HELPER)


def rename_host(hostname):
    res = subprocess.run(["sudo", "-n", HOSTNAME_HELPER, hostname],
                         capture_output=True, text=True, timeout=60)
    if res.returncode:
        log.error("hostname: %s", res.stderr.strip())
    else:
        log.info("hostname: %s", hostname)


@app.put("/api/media/<path:filename>")
def api_upload(filename):
    # The file is sent raw and written straight to the SD card: no temporary
    # copy in RAM (/tmp), which large videos require. Videos go through the
    # conversion queue, images are ready at once.
    name = secure_filename(filename)
    kind = media_kind(name)
    if not name or not kind:
        return jsonify(error=msg("upload.error.format")), 400
    if kind == "subtitle":
        limit = 5 * 1024 * 1024
        raw = request.stream.read(limit + 1)
        if len(raw) > limit:
            return jsonify(error=msg("upload.error.subtitle_size")), 400
        try:
            data = normalize_subtitle(raw, name)
        except ValueError:
            return jsonify(error=msg("upload.error.subtitle_unreadable")), 400
        tmp = MEDIA_DIR / f".{os.urandom(4).hex()}.upload"
        tmp.write_bytes(data)
        os.replace(tmp, MEDIA_DIR / name)
        media_ready(name)   # reload if these subtitles are on screen
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
            return jsonify(error=msg("upload.error.image", detail=str(e))), 400
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
        return jsonify(error=msg("media.error.not_found")), 404
    cfg = load_config()
    uses = media_usage(cfg, path.name)
    if uses:
        return jsonify(error=msg("media.error.in_use"), uses=uses), 409
    path.unlink()
    if cfg["subtitles"].pop(path.name, None):   # video deleted
        save_config(cfg)
    return jsonify(ok=True)


@app.post("/api/player/pause")
def api_pause():
    paused = bool(request.get_json(force=True).get("paused", True))
    try:
        return jsonify(player_request("pause", paused=paused))
    except OSError:
        return jsonify(error=msg("error.player_unreachable")), 503


@app.post("/api/player/restart")
def api_restart():
    # reloading the configuration: playback starts over
    try:
        return jsonify(player_request("reload"))
    except OSError:
        return jsonify(error=msg("error.player_unreachable")), 503


# vcgencmd get_throttled: "now" bits (0-3) and "since boot" bits (16-19);
# the web UI translates the codes (health.flag.<code>)
THROTTLE_FLAGS = [(0, "under_voltage"), (1, "freq_capped"), (2, "throttled"),
                  (3, "temp_limit")]
_health = {"time": 0, "data": None}


def system_health():
    """Health of the Pi (cached 5 s: the web UI polls every second)."""
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
            "now": [code for bit, code in THROTTLE_FLAGS if bits >> bit & 1],
            "since_boot": [code for bit, code in THROTTLE_FLAGS
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
    """Permission to reboot / power off (sudo rule installed by install.sh)."""
    return sudo_allowed(f"{SYSTEMCTL} {action}")


@app.post("/api/system/<action>")
def api_system(action):
    if action not in SYSTEM_ACTIONS:
        return jsonify(error=msg("system.error.action")), 404
    if not system_allowed(action):
        return jsonify(error=msg("error.rights_missing")), 403
    # the reply leaves before the shutdown: systemctl returns at once
    subprocess.Popen(["sudo", "-n", SYSTEMCTL, action])
    return jsonify(ok=True)


@app.post("/api/trigger-udp")
def api_trigger_udp():
    # end-to-end test: a real datagram, received like those from the network
    message = str(request.get_json(force=True).get("message") or "").strip()
    if not message:
        return jsonify(error=msg("udp.error.empty")), 400
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.sendto(message.encode(), ("127.0.0.1", int(load_config()["udp_port"])))
    return jsonify(ok=True)


@app.post("/api/trigger/<int:gpio>")
def api_trigger(gpio):
    try:
        return jsonify(player_request("trigger", gpio=gpio))
    except OSError:
        return jsonify(error=msg("error.player_unreachable")), 503


def start_network():
    """Access point password (generated once) and network mode."""
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
        supervisor.error = msg("error.rights_missing")
        logging.getLogger("network").warning(
            "missing permissions: run the installer again (sudo ./install.sh)")


supervisor = None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    start_network()
    app.run(host="0.0.0.0", port=PORT, threaded=True)

#!/usr/bin/env python3
"""Full-screen video player driven by GPIO and UDP (mpv + gpiod).

Modes:
  - loop        : playlist. A single media (video or image) loops forever;
                  several media play in order (videos repeated a chosen
                  number of times, images shown for a chosen duration), then
                  the list starts over.
  - interactive : an attract media loops; a GPIO button or a UDP message
                  starts a video, then back to the attract loop when it ends.

UDP messages (port udp_port): the triggers' ones, plus pause, play and
restart in every mode.

The web backend talks to this process over a unix socket (see
common.player_request): status, reload, trigger and pause commands. Errors
reported to the web UI are structured messages (i18n.msg).
"""
import datetime
import json
import logging
import os
import queue
import signal
import socketserver
import subprocess
import threading
import time

import glob
import hashlib
import socket
import sys

import gpiod
import mpv

import network
from gpiod.line import Bias, Direction, Edge
from i18n import msg

from common import (BASE, DATA_DIR, IMAGE_DURATION, MEDIA_DIR, SOCKET_PATH,
                    SUBTITLE_SIZES, UDP_MAX_LEN, load_config,
                    mdns_available, media_kind, network_addresses,
                    network_status, player_name, trigger_label, udp_key)

log = logging.getLogger("player")

# DarkSign splash screen (splash.py): still image right away, then the intro
# animation once rendered. Rendered in a separate process (numpy, Pillow) to
# keep the player light; result cached per address.
SPLASH_CHECK = 10    # s: network address check on the splash screen
SPLASH_INTRO = BASE / "assets" / "intro.mp4"
SPLASH_LIST = DATA_DIR / "splash.ffconcat"
SPLASH_NET_WAIT = 30  # s: wait for the network (Wi-Fi) after the boot intro
SPLASH_KEEP = 3       # cached screens (e.g. offline + Wi-Fi + Ethernet)
WEB_PORT = 8080

# Still image: mpv renders it once, and that frame stays in the GPU frame
# queue (swapchain, 3 frames) without being presented: the screen keeps the
# previous image. A few invisible redraws (tiny zoom) flush the queue.
STILL_REDRAWS = 4
STILL_REDRAW_DELAY = 0.25   # s

PRESS_LOCKOUT = 0.3  # s: ignore presses too close together (bounce, double press)

# Seamless loop: instead of going back to the start of the file (mpv's
# loop-file flushes the hardware decoder, ~200 ms frozen frame), the "concat"
# demuxer reads a list repeating the video. The decoder gets a continuous
# stream. mpv only loops the list itself after LOOP_HOURS.
LOOP_LIST = DATA_DIR / "loop.ffconcat"
LOOP_HOURS = 24
LOOP_MAX_ENTRIES = 20000

# Playlist of several videos: same idea for the repeats of one video (one
# concat list per entry), but moving from one video to the next goes through
# mpv's playlist. The concat demuxer cannot chain different files
# (resolution, frame rate, audio sample rate): timestamps jump and the sound
# drifts out of sync.
PLAYLIST_LIST = "playlist-{}.ffconcat"


_durations = {}  # (path, modification time) -> duration


def media_duration(path):
    key = (str(path), path.stat().st_mtime)
    if key not in _durations:
        _durations[key] = _probe_duration(path)
    return _durations[key]


def _probe_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, errors="replace", timeout=30).stdout
    try:
        return float(out.strip())
    except ValueError:
        return 0.0


def write_loop_list(path, duration):
    count = min(LOOP_MAX_ENTRIES, max(2, int(LOOP_HOURS * 3600 / duration)))
    write_concat(LOOP_LIST, path, duration, count)


def write_concat(dest, path, duration, count):
    """Concat list repeating the video "path" "count" times."""
    # "duration" enforces an exact offset between two passes
    quoted = str(path).replace("'", "'\\''")   # ffconcat escaping
    entry = f"file '{quoted}'\n"
    if duration > 0:
        entry += f"duration {duration:.6f}\n"
    content = "ffconcat version 1.0\n" + entry * count
    try:
        if dest.read_text() == content:
            return   # same content: no need to rewrite the SD card
    except FileNotFoundError:
        pass
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(content)
    os.replace(tmp, dest)


DISPLAY_MODE = "1920x1080@60"
DISPLAY_CHECK = 2    # s: no screen at start, wait for one to be plugged in


def hdmi_connectors():
    """HDMI outputs of the DRM driver: [(sysfs folder, connected?)]."""
    found = []
    for status in sorted(glob.glob("/sys/class/drm/card*-HDMI-A-*/status")):
        try:
            found.append((os.path.dirname(status),
                          open(status).read().strip() == "connected"))
        except OSError:
            pass
    return found


def display_mode():
    """Display mode forced on mpv.

    mpv does its own DRM modeset and would pick the screen's "preferred"
    mode (often 4K), ignoring video= in cmdline.txt: 1080p is forced. But a
    screen without that mode (720p projector...) would stay black: its
    preferred mode is kept then.
    """
    width_height = DISPLAY_MODE.split("@")[0]
    for path, connected in hdmi_connectors():
        if connected:
            try:
                modes = open(os.path.join(path, "modes")).read().split()
            except OSError:
                continue
            if width_height in modes:
                return DISPLAY_MODE
            log.warning("screen without %s mode (modes: %s): using its preferred mode",
                        width_height, ", ".join(dict.fromkeys(modes)) or "none")
            return "preferred"
    return DISPLAY_MODE


class GpioWatcher:
    """Watch the input pins and report each press."""

    def __init__(self, gpios, active_low, on_press):
        self.stop_event = threading.Event()
        self.gpios = sorted(gpios)
        self.request = None
        if not gpios:
            return
        settings = gpiod.LineSettings(
            direction=Direction.INPUT,
            edge_detection=Edge.FALLING if active_low else Edge.RISING,
            bias=Bias.PULL_UP if active_low else Bias.PULL_DOWN,
            debounce_period=datetime.timedelta(milliseconds=20),
        )
        self.request = gpiod.request_lines(
            "/dev/gpiochip0", consumer="videoplayer",
            config={tuple(gpios): settings},
        )
        self.on_press = on_press
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stop_event.is_set():
            if self.request.wait_edge_events(datetime.timedelta(milliseconds=500)):
                for ev in self.request.read_edge_events():
                    self.on_press(ev.line_offset)

    def close(self):
        self.stop_event.set()
        if self.request:
            self.thread.join()
            self.request.release()


class UdpListener:
    """Receive UDP messages (one datagram = one text message)."""

    def __init__(self, port, on_message):
        self.port = port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", port))
        self.on_message = on_message
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(1024)
            except OSError:
                return   # socket closed
            text = data.decode("utf-8", errors="replace").strip("\x00\r\n\t ")
            if text:
                self.on_message(text[:UDP_MAX_LEN * 2], addr[0])

    def close(self):
        self.sock.close()


class Player:
    def __init__(self):
        self.events = queue.Queue()
        self.cfg = None
        self.state = "idle"        # boot | idle | setup | loop | attract | triggered
        self.current = None        # name of the media on screen
        self.current_sub = None    # subtitles of the media on screen
        self.current_gpio = None   # pin that started the current video
        self.current_trigger = None  # trigger (index) of the current video
        self.current_source = None   # "GPIO17", 'UDP "intro"'...
        self.udp = None
        self.udp_error = None        # structured message
        self.last_udp = None         # last message received (web UI)
        self.last_udp_seen = (None, 0.0)   # duplicate filter (message, time)
        self.loop_len = None       # duration of one pass of a seamless loop
        self.loop_index = 0        # number of the current pass (subtitles)
        self.playlist = []         # playlist entries (several media)
        self.playlist_pos = None   # entry being played
        self.entry_started = 0.0   # when the entry was shown (images)
        self.last_press = 0.0
        self.paused = False        # paused from the web UI
        self.last_error = None     # structured message
        self.watcher = None
        self.splash_addresses = None
        self.splash_checked = 0.0
        self.splash_key = None
        self.splash_jobs = set()     # keys being rendered
        self.intro_played = False    # the boot intro just played
        self.splash_waiting = False  # logo held on screen, waiting for the network
        self.splash_deadline = 0.0
        self.splash_shown = None     # (key, "anim" | "image") on screen
        self.audio_reopened = 0.0    # last time the audio output was reopened
        self.no_display = False      # mpv could not open the display
        self.display_checked = 0.0

        self.mpv = mpv.MPV(
            vo="gpu", gpu_context="drm", hwdec="v4l2m2m", ao="alsa",
            # Pi 3: decoded video must go to the primary plane; on the
            # overlay plane (mpv's default) every frame is rejected by the
            # vc4 driver and the screen stays black.
            drm_drmprime_video_plane="primary", drm_draw_plane="overlay",
            drm_mode=display_mode(),   # 1080p if the screen offers it
            log_handler=self._mpv_log, loglevel="error",
            # keep-open keeps the last frame at the end of a file; without
            # keep-open-pause=no, mpv also pauses and the next file (attract
            # loop after the intro or a video) would stay frozen
            fullscreen=True, keep_open="yes", keep_open_pause="no",
            idle="yes", force_window="yes",
            # playlist: the next file is opened before the current one ends
            prefetch_playlist="yes",
            image_display_duration="inf", background_color="#000000",
            osc=False, osd_level=0, input_default_bindings=False,
            sub_font="DejaVu Sans", sub_margin_y=50, sub_auto="no",
            input_vo_keyboard=False, cursor_autohide="always", terminal=False,
        )
        self.mpv.observe_property(
            "eof-reached", lambda _n, v: v and self.events.put(("eof",)))
        self.mpv.observe_property("time-pos", self._time_pos_changed)

        @self.mpv.event_callback("end-file")
        def _end_file(event):
            data = getattr(event, "data", None)
            if getattr(data, "reason", None) == mpv.MpvEventEndFile.ERROR:
                self.events.put(("error",))

        @self.mpv.event_callback("file-loaded")
        def _file_loaded(_event):
            self.events.put(("file_loaded",))

        @self.mpv.event_callback("playback-restart")
        def _playback_restart(_event):
            self.events.put(("playback_restart",))

    def _mpv_log(self, level, component, message):
        if "TTY" in message or "VT switcher" in message:
            return
        if "Error opening/initializing the selected video_out" in message:
            self.no_display = True   # see _check_display
        log.error("mpv[%s] %s", component, message.strip())

    # --- main loop: every transition goes through here ----------------------

    def run(self):
        if SPLASH_INTRO.exists():
            # boot sequence: the DarkSign intro, then the content (or the
            # setup screen when nothing is scheduled) once it ends
            self.state = "boot"
            self.mpv.command("loadfile", str(SPLASH_INTRO), "replace")
            log.info("boot intro")
        else:
            self.events.put(("reload",))
        while True:
            try:
                ev, *args = self.events.get(timeout=1)
            except queue.Empty:
                try:
                    if self._check_display():
                        break
                    self._refresh_splash()
                except Exception as e:  # no more here than elsewhere
                    log.exception("error during periodic checks")
                    self.last_error = msg("error.internal", detail=str(e))
                continue
            try:
                if ev == "quit":
                    break
                getattr(self, "_on_" + ev)(*args)
            except Exception as e:  # an error must never stop the player
                log.exception("error on event %s", ev)
                self.last_error = msg("error.internal", detail=str(e))
        self._shutdown()

    def _on_reload(self):
        if self.watcher:
            self.watcher.close()
            self.watcher = None
        self.cfg = load_config()
        self.last_error = None
        self._open_udp(int(self.cfg.get("udp_port") or 0))
        self.mpv.volume = max(0, min(100, int(self.cfg.get("volume", 100))))
        self.mpv.audio_device = self.cfg.get("audio_device") or "auto"
        style = self.cfg["subtitle_style"]
        self.mpv.sub_font_size = SUBTITLE_SIZES.get(style.get("size"), 48)
        if style.get("background"):
            self.mpv.sub_border_style = "opaque-box"
            self.mpv.sub_back_color = "#99000000"   # 60 % black
        else:
            self.mpv.sub_border_style = "outline-and-shadow"

        if self._needs_setup():
            # nothing scheduled: splash screen, without a black screen in
            # between (after the boot intro, its logo stays on screen)
            if self.intro_played:
                self.splash_deadline = time.monotonic() + SPLASH_NET_WAIT
            self._show_splash("boot" if self.intro_played else "enter")
        elif self.cfg["mode"] == "interactive":
            inter = self.cfg["interactive"]
            gpios = [t["gpio"] for t in inter["triggers"]
                     if t.get("media") and t.get("gpio") is not None]
            try:
                self.watcher = GpioWatcher(gpios, inter["active_low"],
                                           self._gpio_pressed)
            except OSError as e:
                self.last_error = msg("player.error.gpio", detail=str(e))
                log.error("GPIO unavailable: %s", e)
            self._show_attract()
        else:
            loop = self.cfg["loop"]
            self._play_playlist(loop["items"], muted=loop["muted"])
            self.state = "loop" if self.current else "idle"
        self.intro_played = False
        log.info("configuration loaded: mode=%s", self.cfg["mode"])

    def _on_pause(self, paused):
        # pause from the web UI: the picture freezes, the sound stops
        if self.state not in ("loop", "attract", "triggered") or not self.current:
            return
        self.paused = bool(paused)
        self.mpv.pause = self.paused
        if not self.paused:
            self._reopen_audio()
        log.info("playback %s", "paused" if self.paused else "resumed")

    def _resume(self):
        # any new content (configuration, button, splash screen) plays again
        if self.paused:
            self.paused = False
            self.mpv.pause = False

    def _open_udp(self, port):
        if self.udp and self.udp.port == port:
            return
        if self.udp:
            self.udp.close()
            self.udp = None
        self.udp_error = None
        if not port:
            return
        try:
            self.udp = UdpListener(port, self._udp_received)
        except OSError as e:
            self.udp_error = msg("player.error.udp_port", port=port,
                                 detail=e.strerror or str(e))
            log.error("UDP port %d unavailable: %s", port, e)

    def _on_playback_restart(self):
        # the new file shows its first frame: see _hide_subtitles
        self.mpv.sub_visibility = True
        self._reopen_audio()

    def _hide_subtitles(self):
        # Until the first frame of the new file, mpv redraws the last frame
        # of the old one at its timestamp (large in a seamless loop) with the
        # new file's subtitles: a random line would show over the attract.
        self.mpv.sub_visibility = False

    def _reopen_audio(self):
        # HDMI audio (vc4 driver): when mpv stops and restarts the stream
        # without closing the output (new file, resume after pause), the
        # sound is sometimes lost until the next reopening. A full reopening
        # is therefore forced each time playback starts.
        now = time.monotonic()
        if (self.mpv.audio_device.startswith("alsa/hdmi:")
                and now - self.audio_reopened > 1):   # never in bursts
            self.audio_reopened = now
            self.mpv.command("ao-reload")

    def _find_trigger(self, match):
        if not self.cfg or self.cfg["mode"] != "interactive":
            return None
        for i, t in enumerate(self.cfg["interactive"]["triggers"]):
            if t.get("media") and match(t):
                return i
        return None

    def _on_button(self, gpio):
        index = self._find_trigger(lambda t: t.get("gpio") == gpio)
        if index is not None:
            self._trigger(index, f"GPIO{gpio}")

    def _on_udp(self, text, sender):
        key = udp_key(text)
        index = self._find_trigger(lambda t: t.get("udp") and udp_key(t["udp"]) == key)
        if index is not None:
            trigger = self.cfg["interactive"]["triggers"][index]
            action = self._trigger(index, trigger_label({"udp": trigger["udp"]}))
        elif key in ("pause", "play"):
            active = self.state in ("loop", "attract", "triggered") and self.current
            self._on_pause(key == "pause")
            action = msg(f"udp.action.{key}" if active else "udp.action.idle")
        elif key == "restart":
            self._on_reload()
            action = msg("udp.action.restart")
        else:
            action = msg("udp.action.unknown")
        log.info('UDP from %s: "%s" (%s)', sender, text, action["key"])
        self.last_udp = {"message": text, "from": sender, "time": time.time(),
                         "action": action}

    def _trigger(self, index, source):
        """Start a trigger's video; return what happened (message)."""
        inter = self.cfg["interactive"]
        trigger = inter["triggers"][index]
        if self.state == "triggered" and not inter["interruptible"]:
            log.info("%s ignored: current video is not interruptible", source)
            return msg("udp.action.not_interruptible")
        log.info("%s -> %s", source, trigger["media"])
        if not self._play(trigger["media"], loop=False, muted=inter["triggers_muted"]):
            return msg("udp.action.missing")
        self.state = "triggered"
        self.current_gpio = trigger.get("gpio")
        self.current_trigger = index
        self.current_source = source
        return msg("udp.action.play_media", media=trigger["media"])

    def _on_eof(self):
        if not self.mpv.eof_reached:
            return   # end of a file already replaced
        if self.state == "boot":
            self.intro_played = True
            self._on_reload()
        elif self.state == "triggered":
            self._show_attract()

    def _on_file_loaded(self):
        if media_kind(self.mpv.path or "") == "image":
            self._flush_still_image()
        # playlist: new entry (or back to the start of the list)
        if self.state != "loop" or not self.playlist:
            return
        pos = self.mpv.playlist_pos
        if pos is None or not 0 <= pos < len(self.playlist):
            return
        entry = self.playlist[pos]
        self.playlist_pos = pos
        self.entry_started = time.monotonic()
        self.current = entry["media"]
        self.loop_len = entry["duration"] or None
        self.loop_index = 0
        self.mpv.sub_delay = 0
        sub = self.cfg["subtitles"].get(entry["media"])
        self.current_sub = None
        if sub and (MEDIA_DIR / sub).is_file():
            self._hide_subtitles()
            self.mpv.command("sub-add", str(MEDIA_DIR / sub), "select")
            self.current_sub = sub

    def _flush_still_image(self):
        def run():
            for i in range(STILL_REDRAWS):
                time.sleep(STILL_REDRAW_DELAY)
                try:   # each change triggers a redraw; ends at 0
                    self.mpv.video_zoom = 0.0001 if i % 2 == 0 else 0
                except Exception:
                    return   # player shutting down

        threading.Thread(target=run, daemon=True).start()

    def _on_loop_pass(self, index):
        # seamless loop: timestamps never go back to zero, so subtitles are
        # shifted by one pass each time around
        if self.loop_len and index != self.loop_index:
            self.loop_index = index
            self.mpv.sub_delay = index * self.loop_len

    def _on_error(self):
        self.last_error = msg("player.error.playback", media=self.current)
        log.error("cannot play %s", self.current)
        if self.state == "triggered":
            self._show_attract()

    # --- actions -------------------------------------------------------------

    def _playable(self, media):
        return bool(media) and media_kind(media) in ("video", "image") \
            and (MEDIA_DIR / media).is_file()

    def _needs_setup(self):
        """Nothing scheduled: no playlist, no attract, no usable trigger.

        An empty attract with triggers configured is a deliberate black
        screen ("black screen" option of the web UI).
        """
        if self.cfg["mode"] == "loop":
            return not any(self._playable(it.get("media"))
                           for it in self.cfg["loop"]["items"])
        inter = self.cfg["interactive"]
        return not self._playable(inter["attract"]) and not any(
            self._playable(t.get("media")) for t in inter["triggers"])

    def _splash_key(self, addresses):
        # any change to the rendering (sources, intro, texts) invalidates it
        sources = [BASE / "splash.py", BASE / "brand.py", SPLASH_INTRO,
                   *sorted((BASE / "locales").glob("*.json"))]
        stamp = [f.stat().st_mtime if f.exists() else 0 for f in sources]
        # offline, the screen shows a diagnosis (Wi-Fi, cable): it is part of
        # the key, so the screen is redrawn when it changes
        diag = None if addresses else network_status()
        cfg = self.cfg or load_config()
        # access point: its name and password are displayed
        data = json.dumps([socket.gethostname(), player_name(cfg),
                           cfg.get("language"), WEB_PORT, addresses,
                           mdns_available(), stamp, diag, network.ap_info()])
        return hashlib.sha1(data.encode()).hexdigest()[:12]

    def _show_splash(self, context="enter"):
        """Show the splash screen.

        context:
          enter  - arriving on the screen (content removed...): animated
                   intro + ending
          boot   - right after the boot intro, stopped on the centred logo:
                   wait for the network (logo held), then play the ending only
          change - address changed or rendering done: just update the
                   picture, never replay the animation
        """
        addresses = network_addresses()
        self.state = "setup"
        self.splash_checked = time.monotonic()
        if context == "boot" and not addresses \
                and time.monotonic() < self.splash_deadline:
            self.splash_waiting = True   # the last frame of the intro stays
            return
        self.splash_waiting = False
        self._resume()

        key = self._splash_key(addresses)
        self.splash_addresses, self.splash_key = addresses, key
        image = DATA_DIR / f"splash-{key}.png"
        outro = DATA_DIR / f"splash-{key}.mp4"
        self.loop_len = None
        self.current_sub = None
        self.playlist = []
        self.mpv.loop_playlist = "no"
        self.mpv["sub-files"] = []

        if context != "change" and outro.exists() and SPLASH_INTRO.exists():
            # generic intro + address-specific ending, chained seamlessly;
            # mpv then keeps the last frame (the splash screen). After the
            # boot intro, only the ending plays: it starts from the centred
            # logo the intro stopped on.
            files = ([SPLASH_INTRO] if context == "enter" else []) + [outro]
            SPLASH_LIST.write_text("ffconcat version 1.0\n"
                                   + "".join(f"file '{f}'\n" for f in files))
            self.mpv.demuxer_lavf_o = "safe=0"
            self.mpv.command("loadfile", str(SPLASH_LIST), "replace")
            self.splash_shown = (key, "anim")
        elif self.splash_shown and self.splash_shown[0] == key:
            pass    # already on screen (animation done or image): nothing to do
        elif image.exists():
            self.mpv.demuxer_lavf_o = ""
            self.mpv.command("loadfile", str(image), "replace")
            self.splash_shown = (key, "image")
        elif context == "enter":
            self.mpv.command("stop")     # black screen while rendering (~3 s)
        # otherwise (boot, change): the current picture stays until rendered
        if not (image.exists() and outro.exists()):
            self._build_splash(key, addresses, image, outro)

    def _build_splash(self, key, addresses, image, outro):
        if key in self.splash_jobs:
            return
        self.splash_jobs.add(key)

        def run():
            script = [sys.executable, str(BASE / "splash.py")]
            args = [str(WEB_PORT), *addresses]
            try:
                for kind, out in (("static", image), ("animate", outro)):
                    if not out.exists():
                        subprocess.run(["nice", "-n", "10", *script, kind, str(out),
                                        *args], check=True, timeout=1800,
                                       stdout=subprocess.DEVNULL)
                        self.events.put(("splash_ready", key))
                # cleanup: keep the most recent screens (boot often goes
                # through "offline" before getting its address)
                keys = sorted({f.stem for f in DATA_DIR.glob("splash-*.png")},
                              key=lambda k: (DATA_DIR / f"{k}.png").stat().st_mtime,
                              reverse=True)
                for old in keys[SPLASH_KEEP:]:
                    for f in DATA_DIR.glob(f"{old}.*"):
                        f.unlink(missing_ok=True)
                log.info("animated splash screen ready")
            except (subprocess.SubprocessError, OSError) as e:
                log.error("cannot render the splash screen: %s", e)
            finally:
                self.splash_jobs.discard(key)

        threading.Thread(target=run, daemon=True).start()

    def _on_splash_ready(self, key):
        if self.state == "setup" and key == self.splash_key:
            self._show_splash("change")

    def _check_display(self):
        """Without a screen at start (projector off, cable unplugged), mpv
        does not open the display and never reopens it: the player waits for
        a screen to be plugged in, then stops; systemd restarts it at once.
        Return True to stop the player."""
        if not self.no_display:
            return False
        now = time.monotonic()
        if now - self.display_checked < DISPLAY_CHECK:
            return False
        self.display_checked = now
        if any(connected for _, connected in hdmi_connectors()):
            log.info("screen detected: restarting the player")
            return True
        error = msg("player.error.no_display")
        if self.last_error != error:
            self.last_error = error
            log.warning("no screen detected: plug in or turn on the HDMI screen")
        return False

    def _refresh_splash(self):
        # the IP address may arrive after boot (DHCP, Wi-Fi) or change
        if self.state != "setup":
            return
        if self.splash_waiting:          # logo held: check every second
            if network_addresses() or time.monotonic() >= self.splash_deadline:
                self._show_splash("boot")
            return
        if time.monotonic() - self.splash_checked < SPLASH_CHECK:
            return
        self.splash_checked = time.monotonic()
        addresses = network_addresses()
        if addresses != self.splash_addresses:
            log.info("network address changed: splash screen updated")
            self._show_splash("change")
        elif self._splash_key(addresses) != self.splash_key:
            # offline diagnosis, access point started or changed, language...
            log.info("splash screen inputs changed: splash screen updated")
            self._show_splash("change")

    def _show_attract(self):
        inter = self.cfg["interactive"]
        self.current_gpio = self.current_trigger = self.current_source = None
        if self._play(inter["attract"], loop=True, muted=inter["attract_muted"]):
            self.state = "attract"
        else:
            self.state = "idle"

    def _play_playlist(self, items, muted):
        """Playlist mode: one entry looping forever, or several media in
        order (video repeated "repeat" times, image shown "duration" seconds),
        then the list starts over."""
        playable = []
        for it in items:
            if self._playable(it.get("media")):
                playable.append(it)
            elif it.get("media"):
                self.last_error = msg("player.error.missing", media=it["media"])
                log.error("media not found: %s", it["media"])
        if len(playable) < 2:
            # a single entry: seamless endless loop
            return self._play(playable[0]["media"] if playable else None,
                              loop=True, muted=muted)

        self._resume()
        self.splash_shown = None
        self.playlist = []
        targets = []   # (file to load, per-entry options)
        for i, it in enumerate(playable):
            path = MEDIA_DIR / it["media"]
            if media_kind(it["media"]) == "image":
                duration = float(it.get("duration") or IMAGE_DURATION)
                targets.append((path, f"image-display-duration={duration:g}"))
                repeat = 1
            else:
                duration = media_duration(path)
                repeat = max(1, int(it.get("repeat") or 1))
                dest = DATA_DIR / PLAYLIST_LIST.format(i)
                write_concat(dest, path, duration, repeat)
                targets.append((dest, "image-display-duration=inf"))
            self.playlist.append({"media": it["media"], "duration": duration,
                                  "repeat": repeat})
        for old in DATA_DIR.glob(PLAYLIST_LIST.format("*")):
            if all(old != t for t, _ in targets):
                old.unlink(missing_ok=True)

        self.mpv.mute = bool(muted)
        self.mpv["sub-files"] = []   # subtitles added for each entry
        self.mpv.sub_delay = 0
        self.loop_index = 0
        self.loop_len = None
        self.current_sub = None
        self.playlist_pos = None
        self.mpv.demuxer_lavf_o = "safe=0"
        # mpv's playlist is replaced by the first command: the loop option
        # therefore only applies to the new entries
        for i, (target, opts) in enumerate(targets):
            self.mpv.command("loadfile", str(target), "append" if i else "replace",
                             "-1", f"loop-file=no,{opts}")
        self.mpv.loop_playlist = "inf"
        self.current = playable[0]["media"]
        return True

    def _play(self, media, loop, muted):
        self._resume()
        self.splash_shown = None
        self.playlist = []
        self.playlist_pos = None
        # before loading: a single file in mpv's list, which must not loop
        # (triggered video) after a playlist
        self.mpv.loop_playlist = "no"
        kind = media_kind(media) if media else None
        if kind not in ("video", "image") or not (MEDIA_DIR / media).is_file():
            if media:
                self.last_error = msg("player.error.missing", media=media)
                log.error("media not found: %s", media)
            self.mpv.command("stop")  # black screen
            self.current = None
            return False
        path = MEDIA_DIR / media
        self.mpv.mute = bool(muted)
        sub = self.cfg["subtitles"].get(media) if kind == "video" else None
        subs = [str(MEDIA_DIR / sub)] if sub and (MEDIA_DIR / sub).is_file() else []
        self._hide_subtitles()
        self.mpv["sub-files"] = subs   # applied when the file loads
        self.current_sub = sub if subs else None
        self.mpv.sub_delay = 0
        self.loop_index = 0

        duration = media_duration(path) if loop and kind == "video" else 0
        if duration > 0.5:
            write_loop_list(path, duration)
            self.loop_len = duration
            # format detected from the "ffconcat" header: do not force
            # demuxer-lavf-format, which would also apply to subtitles
            self.mpv.demuxer_lavf_o = "safe=0"
            target = LOOP_LIST
        else:
            self.loop_len = None
            self.mpv.demuxer_lavf_o = ""
            target = path
        # loop option given to the file itself: changing the global option
        # before loading would loop the previous file (the intro, stopped on
        # its last frame) while the new one is being prepared
        self.mpv.command("loadfile", str(target), "replace", "-1",
                         f"loop-file={'inf' if loop else 'no'}")
        self.current = media
        return True

    def _time_pos_changed(self, _name, pos):
        if self.loop_len and self.current_sub and pos is not None:
            index = int(pos // self.loop_len)
            if index != self.loop_index:
                self.events.put(("loop_pass", index))

    def _gpio_pressed(self, gpio):
        now = time.monotonic()
        if now - self.last_press < PRESS_LOCKOUT:
            return
        self.last_press = now
        self.events.put(("button", gpio))

    def _udp_received(self, text, sender):
        # the same message repeated right away (senders send twice, UDP
        # being unreliable) only counts once
        now = time.monotonic()
        key = udp_key(text)
        last_key, last_time = self.last_udp_seen
        self.last_udp_seen = (key, now)
        if key == last_key and now - last_time < PRESS_LOCKOUT:
            return
        self.events.put(("udp", text, sender))

    def _shutdown(self):
        if self.udp:
            self.udp.close()
        if self.watcher:
            self.watcher.close()
        self.mpv.terminate()

    # --- state for the web backend --------------------------------------------

    def status(self):
        try:
            devices = [{"name": "auto", "description": msg("audio.auto")}]
            for d in self.mpv.audio_device_list:
                # one entry per card: hdmi for HDMI outputs (see
                # common.hdmi_audio_device), plughw (converts the format) otherwise
                name = d["name"]
                if name.startswith("alsa/hdmi:"):
                    label = "HDMI"
                elif name.startswith("alsa/plughw:") and "hdmi" not in name.lower():
                    label = (msg("audio.jack") if "Headphones" in name else
                             d["description"])
                else:
                    continue
                devices.append({"name": name, "description": label})
            position = self.mpv.time_pos
            duration = self.mpv.duration
        except Exception:
            devices, position, duration = [], None, None
        playlist = None
        if self.state == "loop" and self.playlist and self.playlist_pos is not None:
            entry = self.playlist[self.playlist_pos]
            length = entry["duration"]
            if media_kind(entry["media"]) == "image":
                # mpv does not advance the position of a displayed image
                position = min(length, time.monotonic() - self.entry_started)
            done = int(position // length) if length and position else 0
            playlist = {"index": self.playlist_pos, "count": len(self.playlist),
                        "pass": min(done, entry["repeat"] - 1) + 1,
                        "repeat": entry["repeat"]}
            if length and position is not None:
                position, duration = position - done * length, length
        return {
            "state": self.state,
            "mode": self.cfg["mode"] if self.cfg else None,
            "media": self.current,
            "subtitles": self.current_sub,
            "gpio": self.current_gpio,
            "trigger": self.current_trigger,
            "source": self.current_source,
            "udp": {"port": self.udp.port if self.udp else None,
                    "error": self.udp_error, "last": self.last_udp},
            "paused": self.paused,
            "playlist": playlist,
            "position": position,
            "duration": duration,
            "watched_gpios": self.watcher.gpios if self.watcher else [],
            "last_error": self.last_error,
            "audio_devices": devices,
        }


class ControlHandler(socketserver.StreamRequestHandler):
    def handle(self):
        player = self.server.player
        try:
            req = json.loads(self.rfile.readline())
            cmd = req.get("cmd")
            if cmd == "status":
                resp = player.status()
            elif cmd == "reload":
                player.events.put(("reload",))
                resp = {"ok": True}
            elif cmd == "pause":
                player.events.put(("pause", bool(req.get("paused", True))))
                resp = {"ok": True}
            elif cmd == "trigger":
                player.events.put(("button", int(req["gpio"])))
                resp = {"ok": True}
            else:
                resp = {"error": f"unknown command: {cmd}"}
        except Exception as e:
            resp = {"error": str(e)}
        try:
            self.wfile.write((json.dumps(resp) + "\n").encode())
        except BrokenPipeError:
            pass  # the client gave up (timeout during startup)


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)   # socket, lists, splash screens
    player = Player()

    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)
    server = socketserver.ThreadingUnixStreamServer(SOCKET_PATH, ControlHandler)
    server.daemon_threads = True
    server.player = player
    threading.Thread(target=server.serve_forever, daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: player.events.put(("quit",)))

    try:
        player.run()
    finally:
        server.shutdown()
        os.unlink(SOCKET_PATH)


if __name__ == "__main__":
    main()

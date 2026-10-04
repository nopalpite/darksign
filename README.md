# darksign

*[Version française](README.fr.md)*

Video player for Raspberry Pi — a little wink at BrightSign. Full-screen
playback on the Pi's HDMI output (mpv, no desktop), driven by GPIO buttons or
UDP messages, managed from a web interface in your language.

- `player.py`: the player (mpv + gpiod), `videoplayer` service
- `web.py`: web interface on port 8080, `videoplayer-web` service
- `common.py`: configuration, media, available GPIOs, talking to the player
- `transcode.py`: conversion queue for uploaded videos
- `network.py`: venue Wi-Fi (NetworkManager) or standalone access point
- `i18n.py`, `locales/`: translations (see [Languages](#languages))
- `system/`: root scripts installed in /usr/local/sbin (access point,
  hostname), called by the web backend through sudo
- `splash.py`: setup screen (still image and intro animation)
- `brand.py`: DarkSign identity (eclipse logo, wordmark, glow rendering)
- `assets/intro.mp4`: generic animated intro (eclipse), made by `splash.py intro`
- `media/`: videos and images uploaded from the web interface
- `data/config.json`: configuration (written by the web interface)

The two processes talk through the unix socket `data/player.sock`
(`status`, `reload`, `trigger` and `pause` commands).

## Installation

**Requirements**: a Raspberry Pi 3 or 4 and an SD card flashed with
**Raspberry Pi OS Lite (64-bit) "Trixie"**. In Raspberry Pi Imager, customise
the image: hostname, user and password, Wi-Fi (or plan an Ethernet cable) and
SSH enabled. The code needs mpv ≥ 0.38 and libgpiod 2, which Bookworm lacks.

On first boot, connect over SSH and run:

    curl -fsSL https://raw.githubusercontent.com/nopalpite/darksign/main/install.sh | sudo bash

or, from a cloned repository: `sudo ./install.sh`. Reboot at the end: the Pi
boots into the darksign animation, then the setup screen, with no media,
subtitles or configuration.

The installer asks for its language (which is also the language of the
player's screen), the player name and the network mode, then:

1. checks the hardware and the system version;
2. installs the packages (mpv, ffmpeg, Flask, libgpiod, Pillow, numpy, qrcode,
   Inter font, avahi for the `.local` name, hostapd, dnsmasq);
3. clones the repository into `~/videoplayer` (or installs in place when run
   from a cloned repository);
4. creates empty `media/` and `data/` folders;
5. adds the user to the `video`, `render`, `audio` and `gpio` groups, and
   lets the web interface reboot, power off, rename the Pi and manage the
   network (narrow sudo and polkit rules);
6. installs and enables the `videoplayer` and `videoplayer-web` services
   (generated from `systemd/*.service.in`);
7. sets up the quiet boot (see [Boot](#boot)) and keeps the system journal
   across reboots.

Options: `--lang en|fr`, `--name NAME`, `--network client|ap`, `--ap-ssid`,
`--ap-password`, `--user NAME`, `--dir PATH`, `--reboot`, `--dry-run` (show
without changing anything), `--force` (skip the checks), `--reset` (erase media
and configuration, asks for confirmation or `--yes` without a terminal). See
`./install.sh --help`.

**Update**: run the installer again. It fetches the latest version, restarts
the services and keeps the media and configuration.

To test without a wired button: [gpio-web](https://github.com/nopalpite/gpio-web).

## Modes

- **Playlist**: a single media (video or image) loops forever; several media
  play in the chosen order (reorderable), then the list starts over. Each
  video is repeated a set number of times. Repeats of the same video are
  seamless; between two different videos, mpv preloads the next one. An image
  stays on screen for a chosen time (6 s by default).
- **Interactive**: an attract media (video or image) loops; a trigger plays its
  video, then back to the attract loop when it ends. Option: a trigger may or
  may not interrupt the current video. Each video is started by a GPIO button,
  a UDP message, or both.

## Player name

Each player has a name ("Hall A", "Booth 12"...), asked at installation (or
`--name`) and editable in the web interface. It is displayed in the interface
and on the setup screen, and used:

- in its address: `http://hall-a.local:8080` (hostname of the Pi, changed by
  `/usr/local/sbin/darksign-hostname`, which the web backend calls via sudo);
- as the Wi-Fi name of its access point, unless another name is chosen.

Handy to reuse the same players from one event to the next, or to tell
several apart on the same network.

## Network

Two ways of working, chosen at installation (installer question, or
`--network client|ap` with `--ap-ssid` and `--ap-password`) and editable later
in the web interface:

- **Venue Wi-Fi**: the player joins the first saved Wi-Fi network in range.
  Networks are added from the web interface (scan of visible networks,
  password). If no network can be reached for 90 s (at boot or later), the
  player starts its access point as a fallback, until the next reboot or mode
  change: it never becomes unreachable.
- **Standalone access point** (events): the player creates its own WPA2
  Wi-Fi network, with no box or router. It can be reached at
  `http://10.42.0.1:8080`; the name (the player name by default) and password
  (generated at installation, editable) are shown on the setup screen, with a
  QR code to join the network and another one to open the interface.

The Ethernet cable works in both modes. The venue Wi-Fi goes through
NetworkManager (polkit rule `/etc/polkit-1/rules.d/50-darksign.rules`). The
access point is the `darksign-ap` service (hostapd + dnsmasq, script
`system/darksign-ap`): NetworkManager's own access point advertises an
authentication (PSK-SHA256) the Pi 3 chip does not support, and phones refused
to join it. Here: WPA2-PSK only, CCMP, no PMF.

On the access point, every DNS name points to the player and port 80 leads to
the interface: the connectivity checks of phones and computers get the answer
they expect, otherwise they would deem the network "without Internet" and use
mobile data, even to reach the player. Android also checks Google over HTTPS,
impossible offline: if it shows "Limited connectivity", choose "Connect
anyway". A VPN running on the device also captures local traffic: turn it off
while managing the player.

## UDP commands

The player listens for text messages over UDP (port 5000 by default, editable
in the interface), one message per datagram, case-insensitive and ignoring
surrounding spaces or line breaks:

- in interactive mode, a trigger's message plays its video;
- in every mode: `pause`, `play` (resume) and `restart` (start over).

    echo film | nc -u -w1 hall-a.local 5000

The same message repeated within 300 ms (senders that send twice) only counts
once. The last message received, its sender and its effect are shown in the
interface.

## Languages

The web interface follows each browser's language, with a selector at the top
of the page; the player's setup screen on the TV uses the player language,
chosen at installation and editable in the interface. Available: English,
French. Translations are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md).

## Boot

At boot the screen stays black (no rainbow, text, logo or login prompt), then
the DarkSign intro (`assets/intro.mp4`) plays as soon as the graphics card is
ready, without waiting for the network. The player then moves on to the
scheduled content, or to the setup screen when nothing is scheduled.

Matching system settings (backups of the originals:
`/boot/firmware/*.before-darksign`, `*.avant-darksign` for older
installations):

- `cmdline.txt`: `console=tty3` instead of `console=tty1`, and
  `quiet loglevel=3 logo.nologo vt.global_cursor_default=0 consoleblank=0
  systemd.show_status=false rd.udev.log_level=3 udev.log_level=3`;
- `config.txt`: `disable_splash=1`;
- login prompt on the screen disabled: `sudo systemctl disable getty@tty1`
  (SSH and the serial console stay available);
- `videoplayer` service without default dependencies, started after
  `dev-dri-card0.device` (see `systemd/videoplayer.service.in`).

All these settings are applied by `install.sh`. Without a screen at boot
(projector off, cable unplugged), the player waits and restarts by itself as
soon as an HDMI screen is plugged in.

## Setup screen

As long as no content is scheduled (first start, or media deleted), the screen
shows the address of the web interface, the `.local` name and a QR code. It
updates when the network address changes (every 10 s) and disappears as soon
as content is saved. An attract left on "black screen" with triggers
configured stays a black screen.

At boot, if the network is not there yet at the end of the intro (Wi-Fi often
takes ~50 s), the logo stays on screen for up to 30 s, waiting for an address.
After that, a "No network connection" screen shows a diagnosis (Wi-Fi
configured or not, Ethernet cable plugged in or not) and things to try; it
updates as soon as the state changes. A configured player plays its content
with or without a network. Wired, the connection is automatic (DHCP) and the
wired address is shown first.

The screen opens with an animation (~7 s): a sun, the moon eclipsing it, the
corona turning into the logo, then the information. It has two parts chained
seamlessly: `assets/intro.mp4`, generic and shipped with the project, and an
ending specific to the network address, rendered by the player in the
background (~40 s on a Pi 3, ~170 MB of memory) then cached in `data/`.
Meanwhile, the still image is shown.

After changing `brand.py` or the beginning of the animation in `splash.py`,
regenerate the intro: `python3 splash.py intro` (a few minutes on a Pi 3).

## Hardware

By default, a button between the GPIO and GND (internal pull-up enabled by
the player). Wiring to 3V3 (pull-down) can be selected in the interface. GPIOs
used by an alternate function (UART, I2C, SPI...) are not offered.

The System section of the interface shows the state of the Pi: temperature,
under-voltage or throttled CPU (now or since boot), free space on the SD card,
memory, load and uptime. A Pi 3 throttles around 80 °C: plan a heatsink in a
closed case.

### Portrait screen

The player does not rotate the picture: on a Pi 3, video goes straight to a
hardware display plane that cannot rotate it, and rotating on the GPU drops
15 to 25 % of the frames in 1080p. For a screen mounted in portrait, export the
content already rotated (a 1920×1080 video with the picture lying on its
side), for example:

    ffmpeg -i portrait.mp4 -vf transpose=2 -c:a copy for-rotated-screen.mp4

(`transpose=1` if the screen is rotated the other way.) Subtitles must then be
burnt into the picture.

## Automatic video conversion

Each uploaded video is analysed and, if needed, converted to H.264
(hardware-decoded by the Pi) in an MP4, keeping the original resolution and
frame rate. Only exception: above 1080p, the picture is scaled down to 1080p,
the decoder limit. A video already suitable is kept as is, without
re-encoding.

- Hardware encoder of the Pi (`h264_v4l2m2m`), ~10 Mb/s in 1080p25; x264 as a
  fallback when the hardware encoder rejects the source.
- Count ~2.5 s of conversion per second of 1080p video on a Pi 3 (more for
  HEVC or ProRes, which are decoded in software).
- Conversion runs at low priority: playback is not disturbed. One conversion
  at a time, the others wait.
- The original file is deleted once converted. It is kept in
  `media/.incoming/` until the conversion is over: after a reboot, it resumes
  automatically.

On a **Raspberry Pi 3** (or Zero 2), conversion saturates the Pi (CPU, 1 GB of
memory shared with playback): the interface then recommends converting on the
computer before uploading, with this command, whose result is stored as is:

    ffmpeg -i input.mov -vf "yadif=deint=interlaced,scale='min(1920,iw)':'min(1080,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,format=yuv420p" -c:v libx264 -preset medium -crf 20 -maxrate 16M -bufsize 32M -c:a aac -b:a 192k -ac 2 -movflags +faststart output.mp4

Without ffmpeg: [HandBrake](https://handbrake.fr/) (free, with an interface),
**General › Fast 1080p30** preset, MP4 format "Web Optimized" (H.264, AAC,
1080p and 30 frames/s at most).

On a Pi 4, conversion on the player works well.

## Subtitles

.srt files (as well as .vtt and .ass) uploaded from the media library, then
linked to a video (one file per video). An .srt with the same name as a video
is linked automatically. Files are converted to UTF-8 on upload (Windows .srt
files in cp1252 are handled). Size and dark background are set in the
interface; DejaVu Sans font.

## Useful commands

    sudo systemctl restart videoplayer videoplayer-web
    journalctl -u videoplayer -f

## License

[GNU General Public License v3.0](LICENSE).

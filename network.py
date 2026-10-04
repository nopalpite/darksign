"""Player network: venue Wi-Fi (client) or the player's own access point.

The venue Wi-Fi goes through NetworkManager (nmcli), which the player user
may drive thanks to the polkit rule installed by install.sh. The access
point is the darksign-ap service (hostapd + dnsmasq, see system/darksign-ap),
started and stopped through sudo: NetworkManager's own access point
advertises an authentication the Pi 3 chip does not support, and phones
refuse to join it.

  - "client" mode: the Pi joins one of the saved Wi-Fi networks (or uses the
    Ethernet cable). If no network is reachable for FALLBACK_DELAY, it
    starts its access point (fallback) until the next mode change or
    reboot: the player never becomes unreachable.
  - "ap" mode: the Pi creates its own WPA2 Wi-Fi network, with DHCP; it can
    be reached at http://10.42.0.1:8080.

Errors shown in the web UI are structured messages (i18n.msg).
"""
import logging
import secrets
import subprocess
import threading
import time

from common import ap_ssid, load_config, network_addresses, sudo_allowed
from i18n import msg, t

log = logging.getLogger("network")

AP_SERVICE = "darksign-ap.service"   # access point (install.sh)
AP_PROFILE = "darksign-ap"   # former NetworkManager access point profile
AP_ADDRESS = "10.42.0.1"
FALLBACK_DELAY = 90             # s without any network before the fallback
CHECK_EVERY = 5                 # s
# generated password: no ambiguous characters (0/O, 1/l/I) to copy out
PASSWORD_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"


class NetworkError(Exception):
    """Network failure, with a structured message for the web UI."""

    def __init__(self, key, **values):
        self.message = msg(key, **values)
        super().__init__(t(key, **values))   # English, for the logs


def nmcli(*args, timeout=30):
    """Run nmcli in terse mode; return the output lines."""
    try:
        res = subprocess.run(["nmcli", "-t", *args], capture_output=True,
                             text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise NetworkError("network.error.nm_timeout")
    if res.returncode != 0:
        detail = (res.stderr or res.stdout).strip().removeprefix("Error: ")
        if "not authorized" in detail.lower():
            raise NetworkError("error.rights_missing")
        raise NetworkError("network.error.nmcli",
                           detail=detail or f"exit code {res.returncode}")
    return res.stdout.splitlines()


def fields(line):
    """Split a terse nmcli line (":" escaped as "\\:")."""
    out, cur, escaped = [], "", False
    for ch in line:
        if escaped:
            cur += ch
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == ":":
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return out


def allowed():
    """May the user change the network configuration?"""
    try:
        for line in nmcli("general", "permissions"):
            perm, value = fields(line)[:2]
            if perm == "org.freedesktop.NetworkManager.settings.modify.system":
                return value == "yes"
    except NetworkError:
        pass
    return False


def generate_password():
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(10))


def wifi_device():
    for line in nmcli("device"):
        dev, kind = fields(line)[:2]
        if kind == "wifi":
            return dev
    return None


def active_wifi():
    """(name of the active Wi-Fi connection, address) or (None, None)."""
    dev = wifi_device()
    if not dev:
        return None, None
    name, addr = None, None
    for line in nmcli("-f", "GENERAL.CONNECTION,IP4.ADDRESS", "device", "show", dev):
        key, _, value = line.partition(":")
        if key == "GENERAL.CONNECTION" and value and value != "--":
            name = value
        elif key.startswith("IP4.ADDRESS") and value:
            addr = value.split("/")[0]
    return name, addr


def ap_active():
    return subprocess.run(["systemctl", "is-active", "--quiet", AP_SERVICE]
                          ).returncode == 0


def ap_allowed():
    """May the user start the access point (sudo rule)?"""
    return sudo_allowed(f"/usr/bin/systemctl start {AP_SERVICE}")


def ap_control(action):
    """Start, stop or restart the access point."""
    res = subprocess.run(["sudo", "-n", "/usr/bin/systemctl", action, AP_SERVICE],
                         capture_output=True, text=True, timeout=60)
    if res.returncode:
        detail = res.stderr.strip()
        if "password is required" in detail or "not allowed" in detail:
            raise NetworkError("error.rights_missing")
        raise NetworkError("network.error.ap_control", action=action,
                           detail=detail or f"exit code {res.returncode}")
    if action != "stop":
        time.sleep(2)   # hostapd exits at once if its config is rejected
        if not ap_active():
            raise NetworkError("network.error.ap_not_started")


def ap_info():
    """Active access point: {"ssid", "password", "address"}, else None.
    Used by the splash screen (QR code to join the Wi-Fi)."""
    if not ap_active():
        return None
    cfg = load_config()
    return {"ssid": ap_ssid(cfg), "password": cfg["network"]["ap_password"],
            "address": AP_ADDRESS}


def saved_networks():
    """Saved Wi-Fi networks (access point excluded): [{name, ssid}]."""
    out = []
    for line in nmcli("-f", "NAME,TYPE", "connection", "show"):
        name, kind = fields(line)[:2]
        if kind != "802-11-wireless" or name == AP_PROFILE:
            continue
        values = nmcli("-g", "802-11-wireless.ssid,802-11-wireless.mode",
                       "connection", "show", name)
        ssid = values[0] if values else name
        mode = values[1] if len(values) > 1 else ""
        if mode != "ap":
            out.append({"name": name, "ssid": ssid})
    return out


def scan():
    """Visible networks, strongest first, one per name."""
    if ap_active():
        raise NetworkError("network.error.scan_during_ap")
    dev = wifi_device()
    if not dev:
        raise NetworkError("network.error.no_wifi")
    seen = {}
    for line in nmcli("-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list",
                      "ifname", dev, "--rescan", "yes", timeout=40):
        ssid, signal, security = (fields(line) + ["", "", ""])[:3]
        if not ssid:
            continue   # hidden network
        signal = int(signal or 0)
        if ssid not in seen or seen[ssid]["signal"] < signal:
            seen[ssid] = {"ssid": ssid, "signal": signal,
                          "secure": bool(security and security != "--")}
    return sorted(seen.values(), key=lambda n: -n["signal"])


def add_network(ssid, password):
    """Save a Wi-Fi network (or change its password)."""
    existing = next((n for n in saved_networks() if n["ssid"] == ssid), None)
    security = (["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]
                if password else [])
    if existing:
        if not password:
            nmcli("connection", "modify", existing["name"],
                  "remove", "802-11-wireless-security")
        else:
            nmcli("connection", "modify", existing["name"], *security)
        return existing["name"]
    dev = wifi_device()
    nmcli("connection", "add", "type", "wifi", "ifname", dev or "*",
          "con-name", ssid, "ssid", ssid, "connection.autoconnect", "yes",
          *security)
    return ssid


def forget_network(name):
    if name == AP_PROFILE:
        raise NetworkError("network.error.forget_ap")
    nmcli("connection", "delete", name)


def remove_old_profile():
    """Access point profile of previous versions (NetworkManager): left in
    place, it would take the Wi-Fi back (priority 100)."""
    names = [fields(l)[0] for l in nmcli("-f", "NAME", "connection", "show")]
    if AP_PROFILE in names:
        log.info("removing the former NetworkManager access point")
        nmcli("connection", "delete", AP_PROFILE)


class Supervisor:
    """Apply the network mode and handle the access point fallback."""

    def __init__(self):
        self.lock = threading.Lock()
        self.mode = None
        self.fallback = False      # access point started for lack of network
        self.lost_since = None
        self.error = None          # structured message
        self.status = {}
        self.wake = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def apply(self, cfg, ap_changed=False):
        """New settings (at start, or saved from the web UI).
        ap_changed: access point name or password changed."""
        net = cfg["network"]
        with self.lock:
            self.mode = net["mode"]
            self.fallback = False
            self.lost_since = time.monotonic()
            try:
                remove_old_profile()
                active = ap_active()
                if self.mode == "ap" and not active:
                    log.info("access point mode: starting")
                    ap_control("start")
                elif self.mode == "ap" and ap_changed:
                    log.info("access point: restarting with the new name or password")
                    ap_control("restart")
                elif self.mode == "client" and active:
                    log.info("client mode: stopping the access point")
                    ap_control("stop")
                    self._reconnect_client()
                self.error = None
            except NetworkError as e:
                self.error = e.message
                log.error("network: %s", e)
        self.wake.set()

    def connect_now(self):
        """Join a saved network right away (client mode)."""
        with self.lock:
            if ap_active():
                ap_control("stop")
            self.fallback = False
            self.lost_since = time.monotonic()
            self._reconnect_client()
        self.wake.set()

    @staticmethod
    def _reconnect_client():
        """Join the first saved network in range.

        NetworkManager would do it by itself, but only after its own scan:
        activating the first visible network is faster."""
        time.sleep(3)   # the Wi-Fi was just handed back to NetworkManager
        try:
            visible = {n["ssid"] for n in scan()}
            saved = saved_networks()
        except NetworkError as e:
            log.info("cannot scan for networks: %s", e)
            return
        for net in saved:
            if net["ssid"] not in visible:
                continue
            try:
                nmcli("connection", "up", net["name"], timeout=45)
                log.info('venue Wi-Fi: connected to "%s"', net["ssid"])
                return
            except NetworkError as e:
                log.info('cannot connect to "%s": %s', net["ssid"], e)
        log.info("no saved Wi-Fi network in range")

    def _run(self):
        while True:
            self.wake.wait(CHECK_EVERY)
            self.wake.clear()
            try:
                self._check()
            except Exception:   # monitoring must never stop
                log.exception("network monitoring")

    def _check(self):
        with self.lock:
            if self.mode == "client" and not self.fallback:
                if network_addresses():
                    self.lost_since = None
                else:
                    self.lost_since = self.lost_since or time.monotonic()
                    if time.monotonic() - self.lost_since >= FALLBACK_DELAY:
                        log.warning("no network for %d s: starting the access "
                                    "point (fallback)", FALLBACK_DELAY)
                        try:
                            ap_control("start")
                            self.fallback = True
                            self.error = None
                        except NetworkError as e:
                            self.error = msg("network.error.fallback",
                                             detail=e.message)
                            log.error("access point fallback failed: %s", e)
                            self.lost_since = time.monotonic()  # retry later
            self.status = self._read_status()

    def _read_status(self):
        ap = ap_active()
        try:
            name, addr = (None, AP_ADDRESS) if ap else active_wifi()
        except NetworkError as e:
            return {"mode": self.mode, "error": e.message}
        waiting = None
        if self.mode == "client" and not self.fallback and self.lost_since:
            waiting = max(0, int(FALLBACK_DELAY - (time.monotonic() - self.lost_since)))
        return {
            "mode": self.mode,
            "state": ("fallback" if ap and self.fallback else "ap" if ap
                      else "client" if name else "disconnected"),
            "connection": None if ap else name,
            "address": addr,
            "addresses": network_addresses(),
            "fallback_in": waiting,   # s before the fallback (no network)
            "error": self.error,
        }

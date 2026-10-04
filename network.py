"""Réseau du lecteur : Wi-Fi du lieu (client) ou point d'accès autonome.

Le Wi-Fi du lieu passe par NetworkManager (nmcli), auquel l'utilisateur du
lecteur a accès grâce à la règle polkit posée par install.sh. Le point
d'accès est le service darksign-ap (hostapd + dnsmasq, voir
system/darksign-ap), démarré et arrêté via sudo : celui de NetworkManager
annonce une authentification que la puce du Pi 3 ne gère pas, et les
téléphones refusent de s'y connecter.

  - mode « client » : le Pi rejoint un des réseaux Wi-Fi mémorisés (ou le
    câble Ethernet). Si aucun réseau n'est joignable pendant FALLBACK_DELAY,
    il active son point d'accès (repli) jusqu'au prochain changement de mode
    ou redémarrage : on ne perd jamais la main sur le lecteur.
  - mode « ap » : le Pi crée son propre réseau Wi-Fi (WPA2), avec DHCP ;
    il est joignable en http://10.42.0.1:8080.
"""
import logging
import secrets
import subprocess
import threading
import time

from common import ap_ssid, load_config, network_addresses, sudo_allowed

log = logging.getLogger("network")

AP_SERVICE = "darksign-ap.service"   # point d'accès (install.sh)
AP_PROFILE = "darksign-ap"   # ancien profil NetworkManager du point d'accès
AP_ADDRESS = "10.42.0.1"
FALLBACK_DELAY = 90             # s sans aucun réseau avant le repli
CHECK_EVERY = 5                 # s
# mot de passe généré : sans caractères ambigus (0/O, 1/l/I) à recopier
PASSWORD_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"


class NetworkError(Exception):
    pass


def nmcli(*args, timeout=30):
    """Lance nmcli en mode terse ; renvoie les lignes de sortie."""
    try:
        res = subprocess.run(["nmcli", "-t", *args], capture_output=True,
                             text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise NetworkError("NetworkManager ne répond pas")
    if res.returncode != 0:
        msg = (res.stderr or res.stdout).strip().removeprefix("Error: ")
        if "Not authorized" in msg or "not authorized" in msg:
            msg = "droits manquants : relancez l'installateur (sudo ./install.sh)"
        raise NetworkError(msg or f"nmcli a échoué ({res.returncode})")
    return res.stdout.splitlines()


def fields(line):
    """Découpe une ligne terse de nmcli (« : » échappé en « \\: »)."""
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
    """L'utilisateur peut-il modifier la configuration réseau ?"""
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
    """(nom de la connexion active sur le Wi-Fi, adresse) ou (None, None)."""
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
    """L'utilisateur peut-il démarrer le point d'accès (règle sudo) ?"""
    return sudo_allowed(f"/usr/bin/systemctl start {AP_SERVICE}")


def ap_control(action):
    """Démarre, arrête ou relance le point d'accès."""
    res = subprocess.run(["sudo", "-n", "/usr/bin/systemctl", action, AP_SERVICE],
                         capture_output=True, text=True, timeout=60)
    if res.returncode:
        msg = res.stderr.strip()
        if "password is required" in msg or "not allowed" in msg:
            msg = "droits manquants : relancez l'installateur (sudo ./install.sh)"
        raise NetworkError(f"point d'accès : {msg or action + ' impossible'}")
    if action != "stop":
        time.sleep(2)   # hostapd s'arrête aussitôt si la config est refusée
        if not ap_active():
            raise NetworkError("le point d'accès n'a pas démarré "
                               "(journalctl -u darksign-ap)")


def ap_info():
    """Point d'accès actif : {"ssid", "password", "address"}, sinon None.
    Utilisé par l'écran d'accueil (QR code de connexion au Wi-Fi)."""
    if not ap_active():
        return None
    cfg = load_config()
    return {"ssid": ap_ssid(cfg), "password": cfg["network"]["ap_password"],
            "address": AP_ADDRESS}


def saved_networks():
    """Réseaux Wi-Fi mémorisés (hors point d'accès) : [{name, ssid}]."""
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
    """Réseaux visibles, du plus fort au plus faible, un par nom."""
    if ap_active():
        raise NetworkError("recherche impossible pendant le point d'accès : "
                           "saisissez le nom du réseau")
    dev = wifi_device()
    if not dev:
        raise NetworkError("pas d'interface Wi-Fi")
    seen = {}
    for line in nmcli("-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list",
                      "ifname", dev, "--rescan", "yes", timeout=40):
        ssid, signal, security = (fields(line) + ["", "", ""])[:3]
        if not ssid:
            continue   # réseau masqué
        signal = int(signal or 0)
        if ssid not in seen or seen[ssid]["signal"] < signal:
            seen[ssid] = {"ssid": ssid, "signal": signal,
                          "secure": bool(security and security != "--")}
    return sorted(seen.values(), key=lambda n: -n["signal"])


def add_network(ssid, password):
    """Mémorise un réseau Wi-Fi (ou change son mot de passe)."""
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
        raise NetworkError("le point d'accès ne peut pas être supprimé")
    nmcli("connection", "delete", name)


def remove_old_profile():
    """Profil NetworkManager du point d'accès des versions précédentes : il
    reprendrait le Wi-Fi (priorité 100) si on le laissait."""
    names = [fields(l)[0] for l in nmcli("-f", "NAME", "connection", "show")]
    if AP_PROFILE in names:
        log.info("suppression de l'ancien point d'accès NetworkManager")
        nmcli("connection", "delete", AP_PROFILE)


class Supervisor:
    """Applique le mode réseau et assure le repli en point d'accès."""

    def __init__(self):
        self.lock = threading.Lock()
        self.mode = None
        self.fallback = False      # point d'accès activé faute de réseau
        self.lost_since = None
        self.error = None
        self.status = {}
        self.wake = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def apply(self, cfg, ap_changed=False):
        """Nouveau réglage (démarrage ou enregistrement depuis l'interface).
        ap_changed : nom ou mot de passe du point d'accès modifiés."""
        net = cfg["network"]
        with self.lock:
            self.mode = net["mode"]
            self.fallback = False
            self.lost_since = time.monotonic()
            try:
                remove_old_profile()
                active = ap_active()
                if self.mode == "ap" and not active:
                    log.info("mode point d'accès : activation")
                    ap_control("start")
                elif self.mode == "ap" and ap_changed:
                    log.info("point d'accès : relance avec le nouveau nom ou "
                             "mot de passe")
                    ap_control("restart")
                elif self.mode == "client" and active:
                    log.info("mode client : arrêt du point d'accès")
                    ap_control("stop")
                    self._reconnect_client()
                self.error = None
            except NetworkError as e:
                self.error = str(e)
                log.error("réseau : %s", e)
        self.wake.set()

    def connect_now(self):
        """Rejoindre tout de suite un réseau mémorisé (mode client)."""
        with self.lock:
            if ap_active():
                ap_control("stop")
            self.fallback = False
            self.lost_since = time.monotonic()
            self._reconnect_client()
        self.wake.set()

    @staticmethod
    def _reconnect_client():
        """Rejoint le premier réseau mémorisé à portée.

        NetworkManager le ferait seul, mais seulement après sa propre
        recherche : on accélère en activant le premier réseau visible."""
        time.sleep(3)   # le Wi-Fi vient d'être rendu à NetworkManager
        try:
            visible = {n["ssid"] for n in scan()}
            saved = saved_networks()
        except NetworkError as e:
            log.info("recherche des réseaux impossible : %s", e)
            return
        for net in saved:
            if net["ssid"] not in visible:
                continue
            try:
                nmcli("connection", "up", net["name"], timeout=45)
                log.info("Wi-Fi du lieu : connecté à « %s »", net["ssid"])
                return
            except NetworkError as e:
                log.info("connexion à « %s » impossible : %s", net["ssid"], e)
        log.info("aucun réseau Wi-Fi mémorisé à portée")

    def _run(self):
        while True:
            self.wake.wait(CHECK_EVERY)
            self.wake.clear()
            try:
                self._check()
            except Exception:   # la surveillance ne doit jamais s'arrêter
                log.exception("surveillance du réseau")

    def _check(self):
        with self.lock:
            if self.mode == "client" and not self.fallback:
                if network_addresses():
                    self.lost_since = None
                else:
                    self.lost_since = self.lost_since or time.monotonic()
                    if time.monotonic() - self.lost_since >= FALLBACK_DELAY:
                        log.warning("aucun réseau depuis %d s : point d'accès "
                                    "activé (repli)", FALLBACK_DELAY)
                        try:
                            ap_control("start")
                            self.fallback = True
                            self.error = None
                        except NetworkError as e:
                            self.error = f"repli en point d'accès impossible : {e}"
                            log.error(self.error)
                            self.lost_since = time.monotonic()  # nouvel essai
            self.status = self._read_status()

    def _read_status(self):
        ap = ap_active()
        try:
            name, addr = (None, AP_ADDRESS) if ap else active_wifi()
        except NetworkError as e:
            return {"mode": self.mode, "error": str(e)}
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
            "fallback_in": waiting,   # s avant le repli (aucun réseau)
            "error": self.error,
        }

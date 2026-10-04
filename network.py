"""Réseau du lecteur : Wi-Fi du lieu (client) ou point d'accès autonome.

Tout passe par NetworkManager (nmcli). L'utilisateur du lecteur y a accès
grâce à la règle polkit posée par install.sh.

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

from common import ap_ssid, load_config, network_addresses

log = logging.getLogger("network")

AP_CONNECTION = "darksign-ap"   # profil NetworkManager du point d'accès
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


def ap_info():
    """Point d'accès actif : {"ssid", "password", "address"}, sinon None.
    Utilisé par l'écran d'accueil (QR code de connexion au Wi-Fi)."""
    try:
        name, addr = active_wifi()
    except NetworkError:
        return None
    if name != AP_CONNECTION:
        return None
    cfg = load_config()
    return {"ssid": ap_ssid(cfg), "password": cfg["network"]["ap_password"],
            "address": addr or AP_ADDRESS}


def saved_networks():
    """Réseaux Wi-Fi mémorisés (hors point d'accès) : [{name, ssid}]."""
    out = []
    for line in nmcli("-f", "NAME,TYPE", "connection", "show"):
        name, kind = fields(line)[:2]
        if kind != "802-11-wireless" or name == AP_CONNECTION:
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
    if name == AP_CONNECTION:
        raise NetworkError("le point d'accès ne peut pas être supprimé")
    nmcli("connection", "delete", name)


def configure_ap(ssid, password, autoconnect):
    """Crée ou met à jour le profil du point d'accès (WPA2, DHCP partagé).
    Renvoie True si le profil a changé (nom, mot de passe ou démarrage auto)."""
    settings = [
        "802-11-wireless.ssid", ssid, "802-11-wireless.mode", "ap",
        "802-11-wireless.band", "bg",
        "ipv4.method", "shared", "ipv4.addresses", f"{AP_ADDRESS}/24",
        "ipv6.method", "ignore",
        "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password,
        "wifi-sec.proto", "rsn", "wifi-sec.pairwise", "ccmp",
        "wifi-sec.group", "ccmp", "wifi-sec.pmf", "disable",
        # en mode point d'accès, NetworkManager le préfère dès le démarrage
        "connection.autoconnect", "yes" if autoconnect else "no",
        "connection.autoconnect-priority", "100",
    ]
    names = [fields(l)[0] for l in nmcli("-f", "NAME", "connection", "show")]
    if AP_CONNECTION in names:
        current = nmcli("-s", "-g", "802-11-wireless.ssid,802-11-wireless-security.psk,"
                        "connection.autoconnect", "connection", "show", AP_CONNECTION)
        if current == [ssid, password, "yes" if autoconnect else "no"]:
            return False   # inchangé : pas d'écriture, pas de coupure
        nmcli("connection", "modify", AP_CONNECTION, *settings)
    else:
        dev = wifi_device()
        nmcli("connection", "add", "type", "wifi", "ifname", dev or "*",
              "con-name", AP_CONNECTION, *settings)
    return True


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

    def apply(self, cfg):
        """Nouveau réglage (démarrage ou enregistrement depuis l'interface)."""
        net = cfg["network"]
        with self.lock:
            self.mode = net["mode"]
            self.fallback = False
            self.lost_since = time.monotonic()
            try:
                changed = configure_ap(ap_ssid(cfg), net["ap_password"],
                                       autoconnect=self.mode == "ap")
                name, _ = active_wifi()
                if self.mode == "ap" and name != AP_CONNECTION:
                    log.info("mode point d'accès : activation")
                    nmcli("connection", "up", AP_CONNECTION, timeout=60)
                elif self.mode == "client" and name == AP_CONNECTION:
                    log.info("mode client : arrêt du point d'accès")
                    nmcli("connection", "down", AP_CONNECTION)
                    self._reconnect_client()
                elif self.mode == "ap" and changed:
                    # nom ou mot de passe modifiés : relance avec le profil à jour
                    nmcli("connection", "up", AP_CONNECTION, timeout=60)
                self.error = None
            except NetworkError as e:
                self.error = str(e)
                log.error("réseau : %s", e)
        self.wake.set()

    def connect_now(self):
        """Rejoindre tout de suite un réseau mémorisé (mode client)."""
        with self.lock:
            name, _ = active_wifi()
            if name == AP_CONNECTION:
                nmcli("connection", "down", AP_CONNECTION)
            self.fallback = False
            self.lost_since = time.monotonic()
            self._reconnect_client()
        self.wake.set()

    @staticmethod
    def _reconnect_client():
        """Rejoint le premier réseau mémorisé à portée.

        Pas de « nmcli device connect » : NetworkManager y choisit la
        connexion de plus haute priorité… le point d'accès lui-même."""
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
                            nmcli("connection", "up", AP_CONNECTION, timeout=60)
                            self.fallback = True
                            self.error = None
                        except NetworkError as e:
                            self.error = f"repli en point d'accès impossible : {e}"
                            log.error(self.error)
                            self.lost_since = time.monotonic()  # nouvel essai
            self.status = self._read_status()

    def _read_status(self):
        try:
            name, addr = active_wifi()
        except NetworkError as e:
            return {"mode": self.mode, "error": str(e)}
        ap = name == AP_CONNECTION
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

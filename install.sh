#!/usr/bin/env bash
# Installateur DarkSign : d'une carte Raspberry Pi OS Lite (Trixie) fraîche à
# un lecteur qui démarre directement sur l'animation.
#
#   curl -fsSL https://raw.githubusercontent.com/nopalpite/videoplayer/main/install.sh | sudo bash
# ou, depuis un dépôt cloné :
#   sudo ./install.sh [options]
#
# Options :
#   --user NOM    utilisateur qui fait tourner le lecteur (défaut : celui qui
#                 lance sudo, sinon « pi »)
#   --dir CHEMIN  dossier d'installation (défaut : le dossier du script s'il est
#                 lancé depuis un dépôt cloné, sinon ~NOM/videoplayer)
#   --reset       efface médias, sous-titres et configuration d'une
#                 installation existante (retour à l'état « premier démarrage ») ;
#                 demande confirmation, ou exige --yes sans terminal
#   --yes         confirme --reset sans poser la question
#   --name NOM    nom du lecteur (« Hall A ») : affiché, et repris dans son
#                 adresse (hall-a.local) et le nom de son point d'accès ;
#                 sans cette option, la question est posée
#   --network MODE  réseau : « client » (Wi-Fi du lieu, avec point d'accès de
#                 secours) ou « ap » (point d'accès autonome) ; sans cette
#                 option, la question est posée (réglage actuel par défaut)
#   --ap-ssid NOM, --ap-password MOT
#                 nom et mot de passe du point d'accès (défaut : le nom du
#                 lecteur et un mot de passe généré)
#   --reboot      redémarre à la fin sans demander
#   --dry-run     affiche ce qui serait fait, sans rien modifier
#   --force       ignore les vérifications de matériel et de version
#
# Ré-exécutable sans risque : une installation existante est mise à jour, ses
# médias et sa configuration sont conservés (sauf --reset).
set -euo pipefail

REPO_URL="${DARKSIGN_REPO:-https://github.com/nopalpite/videoplayer.git}"
BRANCH="${DARKSIGN_BRANCH:-main}"
PACKAGES=(git mpv python3-mpv python3-flask python3-libgpiod python3-pil
          python3-qrcode python3-numpy fonts-inter ffmpeg avahi-daemon
          raspi-utils-core rsync dnsmasq-base polkitd hostapd)
BOOT_PARAMS=(quiet loglevel=3 logo.nologo vt.global_cursor_default=0
             consoleblank=0 systemd.show_status=false rd.udev.log_level=3
             udev.log_level=3)
BACKUP_SUFFIX=".avant-darksign"

TARGET_USER="${SUDO_USER:-}"
INSTALL_DIR=""
RESET=0; REBOOT=0; DRY_RUN=0; FORCE=0; YES=0
NET_MODE=""; AP_SSID=""; AP_PASSWORD=""; PLAYER_NAME=""

# --- affichage -----------------------------------------------------------------
if [ -t 1 ]; then B=$'\e[1m'; A=$'\e[33m'; R=$'\e[31m'; G=$'\e[32m'; N=$'\e[0m'
else B=""; A=""; R=""; G=""; N=""; fi
step=0
title() { step=$((step + 1)); echo; echo "${B}${A}[$step]${N}${B} $*${N}"; }
info()  { echo "    $*"; }
warn()  { echo "    ${A}!${N} $*"; }
die()   { echo "${R}Erreur :${N} $*" >&2; exit 1; }
run()   { if [ "$DRY_RUN" = 1 ]; then echo "    (simulation) $*"; else "$@"; fi; }
has_tty() { ( exec < /dev/tty ) 2>/dev/null; }   # vrai terminal (même via curl | bash)
confirm() {   # confirm "question" : non si aucun terminal ne permet de répondre
    has_tty || return 1
    local answer; read -r -p "    $1 [o/N] " answer < /dev/tty || return 1
    [[ "$answer" =~ ^[oOyY] ]]
}

# --- options -------------------------------------------------------------------
while [ $# -gt 0 ]; do
    case "$1" in
        --user) TARGET_USER="$2"; shift 2 ;;
        --dir) INSTALL_DIR="$2"; shift 2 ;;
        --reset) RESET=1; shift ;;
        --reboot) REBOOT=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        --force) FORCE=1; shift ;;
        --yes) YES=1; shift ;;
        --network) NET_MODE="$2"; shift 2 ;;
        --name) PLAYER_NAME="$2"; shift 2 ;;
        --ap-ssid) AP_SSID="$2"; shift 2 ;;
        --ap-password) AP_PASSWORD="$2"; shift 2 ;;
        -h|--help) sed -n '2,32p' "${BASH_SOURCE[0]:-/dev/null}" 2>/dev/null \
                   | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "option inconnue : $1 (voir --help)" ;;
    esac
done

case "$NET_MODE" in ""|client|ap) ;; *) die "--network : « client » ou « ap »" ;; esac
if [ -n "$AP_PASSWORD" ] && { [ ${#AP_PASSWORD} -lt 8 ] || [ ${#AP_PASSWORD} -gt 63 ]; }; then
    die "--ap-password : 8 à 63 caractères"
fi
[ ${#AP_SSID} -le 32 ] || die "--ap-ssid : 32 caractères au maximum"
[ ${#PLAYER_NAME} -le 40 ] || die "--name : 40 caractères au maximum"

echo "${B}darksign${N} — installation du lecteur"

# --- 1. vérifications --------------------------------------------------------------
title "Vérifications"
[ "$(id -u)" = 0 ] || die "lancez l'installateur avec sudo."
TARGET_USER="${TARGET_USER:-pi}"
[ "$TARGET_USER" != root ] || die "le lecteur ne doit pas tourner en root : utilisez --user."
id "$TARGET_USER" >/dev/null 2>&1 || die "l'utilisateur « $TARGET_USER » n'existe pas."
TARGET_GROUP="$(id -gn "$TARGET_USER")"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
SRC_DIR=""     # dépôt d'où le script est lancé (vide avec curl | bash)
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    candidate="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    [ -f "$candidate/player.py" ] && SRC_DIR="$candidate"
fi
# lancé depuis un dépôt : on installe sur place, jamais ailleurs par défaut
INSTALL_DIR="$(realpath -m "${INSTALL_DIR:-${SRC_DIR:-$TARGET_HOME/videoplayer}}")"
info "utilisateur : $TARGET_USER    dossier : $INSTALL_DIR"

model="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || true)"
if [[ "$model" != *"Raspberry Pi"* ]]; then
    [ "$FORCE" = 1 ] || die "ce n'est pas un Raspberry Pi (« ${model:-inconnu} ») : --force pour passer outre."
    warn "matériel non reconnu : « ${model:-inconnu} »"
else
    info "matériel : $model"
    [[ "$model" == *"Raspberry Pi 3"* ]] || warn "testé sur Raspberry Pi 3 ; sur ce modèle, le décodage matériel et l'affichage peuvent différer."
fi

. /etc/os-release
if [ "${VERSION_ID:-}" != 13 ]; then
    # mpv ≥ 0.38 (syntaxe loadfile) et libgpiod 2 sont requis : Debian 13 « Trixie »
    [ "$FORCE" = 1 ] || die "Raspberry Pi OS « Trixie » (Debian 13) requis, trouvé : ${PRETTY_NAME:-inconnu}."
    warn "version non prise en charge : ${PRETTY_NAME:-inconnue}"
else
    info "système : $PRETTY_NAME"
fi

BOOT_DIR=/boot/firmware
[ -f "$BOOT_DIR/cmdline.txt" ] || BOOT_DIR=/boot
[ -f "$BOOT_DIR/cmdline.txt" ] || die "cmdline.txt introuvable (ni /boot/firmware, ni /boot)."

# --- 2. paquets ------------------------------------------------------------------
title "Paquets"
export DEBIAN_FRONTEND=noninteractive
run apt-get update -qq
run apt-get install -y -qq --no-install-recommends "${PACKAGES[@]}"
info "installés : ${PACKAGES[*]}"
# hostapd ne sert qu'au point d'accès du lecteur (darksign-ap.service) : son
# service par défaut prendrait le Wi-Fi au démarrage
run systemctl disable --now -q hostapd.service 2>/dev/null || true
run systemctl mask -q hostapd.service

# --- 3. code ---------------------------------------------------------------------
title "Code du lecteur"
if [ -n "$SRC_DIR" ] && [ "$SRC_DIR" = "$INSTALL_DIR" ]; then
    info "installation sur place : $INSTALL_DIR"
elif [ -n "$SRC_DIR" ]; then
    # copie du code du dépôt local (modifications comprises) : jamais les
    # médias, l'état ni l'historique git (qui écraserait celui de la cible)
    info "copie de $SRC_DIR vers $INSTALL_DIR"
    run mkdir -p "$INSTALL_DIR"
    run rsync -a --exclude .git/ --exclude media/ --exclude data/ \
        --exclude __pycache__/ "$SRC_DIR/" "$INSTALL_DIR/"
elif [ -d "$INSTALL_DIR/.git" ]; then
    info "mise à jour du dépôt existant"
    run sudo -u "$TARGET_USER" git -C "$INSTALL_DIR" pull --ff-only
elif [ -e "$INSTALL_DIR" ] && [ -n "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]; then
    die "$INSTALL_DIR existe déjà et n'est pas un dépôt darksign : choisissez --dir."
else
    info "clonage de $REPO_URL"
    run mkdir -p "$INSTALL_DIR"      # dossier préparé : il peut être hors du home
    run chown "$TARGET_USER:$TARGET_GROUP" "$INSTALL_DIR"
    run sudo -u "$TARGET_USER" git clone -q --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi

# --- 4. état « premier démarrage » ------------------------------------------------
title "Médias et configuration"
if [ "$RESET" = 1 ] && { [ -d "$INSTALL_DIR/media" ] || [ -d "$INSTALL_DIR/data" ]; }; then
    count=$(find "$INSTALL_DIR/media" -maxdepth 1 -type f 2>/dev/null | wc -l)
    warn "--reset : effacement de $count fichier(s) de médias et de la configuration"
    warn "dossier visé : ${B}$INSTALL_DIR${N}"
    if [ "$DRY_RUN" = 1 ] || [ "$YES" = 1 ]; then
        :
    elif ! has_tty; then
        die "--reset sans terminal pour confirmer : ajoutez --yes pour effacer $INSTALL_DIR."
    elif ! confirm "Effacer les médias et la configuration de $INSTALL_DIR ?"; then
        die "effacement annulé."
    fi
    run systemctl stop videoplayer videoplayer-web 2>/dev/null || true
    run rm -rf "$INSTALL_DIR/media" "$INSTALL_DIR/data"
fi
if [ -d "$INSTALL_DIR/media" ] && [ -n "$(ls -A "$INSTALL_DIR/media" 2>/dev/null)" ]; then
    info "installation existante : médias et configuration conservés (--reset pour repartir de zéro)"
else
    info "aucun média, aucun sous-titre, configuration vierge : écran d'accueil au démarrage"
fi
run mkdir -p "$INSTALL_DIR/media" "$INSTALL_DIR/data"
run chown -R "$TARGET_USER:$TARGET_GROUP" "$INSTALL_DIR"

# --- 5. droits -------------------------------------------------------------------
title "Droits de l'utilisateur"
for group in video render audio gpio; do
    if getent group "$group" >/dev/null; then
        run usermod -aG "$group" "$TARGET_USER"
    else
        warn "groupe « $group » absent"
    fi
done
info "$TARGET_USER : accès à l'écran (video, render), au son (audio) et aux GPIO (gpio)"

# interface web : redémarrer / éteindre le Pi et changer son nom, rien d'autre.
# Le script de renommage est copié hors du dépôt (modifiable par l'utilisateur)
# et appartient à root : sinon la règle sudo permettrait de devenir root.
helper=/usr/local/sbin/darksign-hostname
run install -o root -g root -m 0755 "$INSTALL_DIR/system/darksign-hostname" "$helper"
# point d'accès : hostapd + dnsmasq, démarré à la demande par l'interface web
run install -o root -g root -m 0755 "$INSTALL_DIR/system/darksign-ap" /usr/local/sbin/darksign-ap
ap="/usr/bin/systemctl start darksign-ap.service, /usr/bin/systemctl stop darksign-ap.service, /usr/bin/systemctl restart darksign-ap.service"
sudoers=/etc/sudoers.d/darksign
rule="$TARGET_USER ALL=(root) NOPASSWD: /usr/bin/systemctl reboot, /usr/bin/systemctl poweroff, $helper, $ap"
if [ "$DRY_RUN" = 1 ]; then
    echo "    (simulation) $sudoers : $rule"
elif [ "$(cat "$sudoers" 2>/dev/null)" != "$rule" ]; then
    tmp="$(mktemp)"
    echo "$rule" > "$tmp"
    visudo -cqf "$tmp" || { rm -f "$tmp"; die "règle sudo invalide : $rule"; }
    install -m 0440 "$tmp" "$sudoers"
    rm -f "$tmp"
fi
info "$TARGET_USER : redémarrage, extinction, nom et point d'accès du Pi depuis l'interface web"

# interface web : réseau (Wi-Fi du lieu, point d'accès autonome) via NetworkManager
polkit_rule=/etc/polkit-1/rules.d/50-darksign.rules
rule_js="// darksign : l'interface web du lecteur gère le réseau (Wi-Fi, point d'accès)
polkit.addRule(function(action, subject) {
    if (action.id.indexOf(\"org.freedesktop.NetworkManager.\") === 0 &&
        subject.user === \"$TARGET_USER\") {
        return polkit.Result.YES;
    }
});"
if [ "$DRY_RUN" = 1 ]; then
    echo "    (simulation) $polkit_rule"
elif [ -d /etc/polkit-1/rules.d ]; then
    if [ "$(cat "$polkit_rule" 2>/dev/null)" != "$rule_js" ]; then
        echo "$rule_js" > "$polkit_rule"
        chmod 0644 "$polkit_rule"
    fi
    info "$TARGET_USER : gestion du réseau (Wi-Fi, point d'accès) depuis l'interface web"
else
    warn "polkit absent : le réseau ne pourra pas être géré depuis l'interface"
fi

# --- 6. nom et réseau -----------------------------------------------------------
title "Nom et réseau"
config_py() { sudo -u "$TARGET_USER" env -C "$INSTALL_DIR" python3 -c "$1" "${@:2}"; }
SEP=$'\x1f'   # séparateur des champs (pas un blanc : les champs vides restent)
# réglage courant (installation existante) : mode, nom, point d'accès
current="$(config_py 'from common import load_config, CONFIG_FILE, player_name
cfg = load_config()
n = cfg["network"]
print(n["mode"] if CONFIG_FILE.exists() and n.get("ap_password") else "",
      player_name(cfg), n.get("ap_ssid") or "", n.get("ap_password") or "", sep="\x1f")' \
    2>/dev/null || true)"
IFS="$SEP" read -r CURRENT_MODE CURRENT_NAME CURRENT_SSID CURRENT_PASSWORD <<< "$current" || true

if [ -z "$PLAYER_NAME" ] && has_tty && [ "$DRY_RUN" = 0 ]; then
    echo "    Nom du lecteur, affiché et repris dans son adresse (ex. « Hall A » :"
    echo "    http://hall-a.local:8080) et dans le nom de son point d'accès Wi-Fi."
    read -r -p "    Nom du lecteur [${CURRENT_NAME:-$(hostname)}] : " PLAYER_NAME < /dev/tty \
        || PLAYER_NAME=""
    [ ${#PLAYER_NAME} -le 40 ] || { warn "nom trop long (40 caractères max.) : inchangé"; PLAYER_NAME=""; }
fi

if [ -z "$NET_MODE" ] && has_tty && [ "$DRY_RUN" = 0 ]; then
    [ "$CURRENT_MODE" = ap ] && default=2 || default=1
    echo "    Comment le lecteur se connecte-t-il ?"
    echo "      1) Wi-Fi du lieu : réseau configuré dans Raspberry Pi Imager ou ajouté"
    echo "         depuis l'interface ; point d'accès de secours si aucun n'est joignable"
    echo "      2) Point d'accès autonome : le lecteur crée son propre réseau Wi-Fi"
    echo "         (événementiel, sans box ni routeur)"
    read -r -p "    Choix [$default] : " answer < /dev/tty || answer=""
    [ "${answer:-$default}" = 2 ] && NET_MODE=ap || NET_MODE=client
    if [ "$NET_MODE" = ap ] && [ -z "$AP_SSID$AP_PASSWORD" ]; then
        ssid_default="${CURRENT_SSID:-le nom du lecteur}"
        read -r -p "    Nom du réseau Wi-Fi [$ssid_default] : " AP_SSID < /dev/tty || AP_SSID=""
        [ ${#AP_SSID} -le 32 ] || { warn "nom trop long : inchangé"; AP_SSID=""; }
        pass_hint="${CURRENT_PASSWORD:+vide = garder « $CURRENT_PASSWORD »}"
        while :; do
            read -r -p "    Mot de passe (8 à 63 caractères, ${pass_hint:-vide = généré}) : " \
                AP_PASSWORD < /dev/tty || AP_PASSWORD=""
            [ -z "$AP_PASSWORD" ] || { [ ${#AP_PASSWORD} -ge 8 ] && [ ${#AP_PASSWORD} -le 63 ]; } \
                && break
            warn "8 à 63 caractères"
        done
    fi
fi
NET_MODE="${NET_MODE:-${CURRENT_MODE:-client}}"

if [ "$DRY_RUN" = 1 ]; then
    echo "    (simulation) nom : ${PLAYER_NAME:-inchangé}, réseau : $NET_MODE"
else
    # écrit dans la configuration du lecteur ; l'interface web applique le
    # réseau au démarrage (network.py), comme un changement fait depuis la page
    result="$(config_py 'import sys
from common import ap_ssid, hostname_for, load_config, player_name, save_config
from network import generate_password
mode, ssid, password, name = sys.argv[1:5]
cfg = load_config()
net = cfg["network"]
if name:
    cfg["name"] = name
net["mode"] = mode
net["ap_ssid"] = ssid or net.get("ap_ssid")   # vide : le nom du lecteur
net["ap_password"] = password or net.get("ap_password") or generate_password()
save_config(cfg)
print(hostname_for(cfg["name"]) if cfg["name"] else "", player_name(cfg),
      ap_ssid(cfg), net["ap_password"], sep="\x1f")' \
        "$NET_MODE" "$AP_SSID" "$AP_PASSWORD" "$PLAYER_NAME")"
    IFS="$SEP" read -r NEW_HOSTNAME PLAYER_NAME AP_SSID AP_PASSWORD <<< "$result"
    if [ -n "$NEW_HOSTNAME" ] && [ "$NEW_HOSTNAME" != "$(hostname)" ]; then
        /usr/local/sbin/darksign-hostname "$NEW_HOSTNAME"
    fi
    info "nom : « $PLAYER_NAME »  ·  adresse : http://$(hostname).local:8080"
fi
if [ "$NET_MODE" = ap ]; then
    info "point d'accès autonome : « $AP_SSID », mot de passe « $AP_PASSWORD »"
else
    info "Wi-Fi du lieu ; point d'accès de secours « $AP_SSID » si aucun réseau n'est joignable"
fi

# --- 7. services -----------------------------------------------------------------
title "Services"
for unit in videoplayer videoplayer-web darksign-ap; do
    template="$INSTALL_DIR/systemd/$unit.service.in"
    [ -f "$template" ] || [ "$DRY_RUN" = 1 ] || die "modèle introuvable : $template"
    if [ "$DRY_RUN" = 1 ]; then
        echo "    (simulation) $template -> /etc/systemd/system/$unit.service"
    else
        sed -e "s#@USER@#$TARGET_USER#g" -e "s#@GROUP@#$TARGET_GROUP#g" \
            -e "s#@DIR@#$INSTALL_DIR#g" "$template" > "/etc/systemd/system/$unit.service"
    fi
done
run systemctl daemon-reload
run systemctl enable -q videoplayer videoplayer-web
info "videoplayer (lecteur, démarré dès que l'écran est prêt) et videoplayer-web (port 8080)"
# point d'accès des versions précédentes (profil NetworkManager) : remplacé
# par darksign-ap.service, il reprendrait le Wi-Fi s'il restait
if [ "$DRY_RUN" = 0 ] && nmcli -t -f NAME connection show 2>/dev/null | grep -qx darksign-ap; then
    nmcli connection delete darksign-ap >/dev/null && info "ancien point d'accès NetworkManager supprimé"
fi
for unit in videoplayer videoplayer-web darksign-ap; do   # mise à jour : nouveau code chargé
    if systemctl is-active -q "$unit"; then
        run systemctl restart "$unit"
        info "$unit relancé"
    fi
done

# --- 8. démarrage silencieux --------------------------------------------------------
title "Démarrage silencieux"
backup() {   # garde une seule sauvegarde : l'original d'avant darksign
    [ -f "$1$BACKUP_SUFFIX" ] || run cp -p "$1" "$1$BACKUP_SUFFIX"
}
cmdline="$BOOT_DIR/cmdline.txt"
backup "$cmdline"
current="$(tr -d '\n' < "$cmdline")"
new="$(echo "$current" | sed -E 's/(^| )console=tty1( |$)/\1console=tty3\2/')"
for param in "${BOOT_PARAMS[@]}"; do
    [[ " $new " == *" $param "* ]] || new="$new $param"
done
if [ "$new" != "$current" ]; then
    [[ "$new" == *"root="* ]] || die "cmdline.txt inattendu (pas de root=), rien n'est modifié."
    if [ "$DRY_RUN" = 1 ]; then echo "    (simulation) cmdline.txt : $new"
    else echo "$new" > "$cmdline"; fi
    info "noyau : messages sur tty3, logos et curseur masqués"
else
    info "cmdline.txt déjà configuré"
fi

config="$BOOT_DIR/config.txt"
backup "$config"
if ! grep -qE '^\s*dtoverlay=vc4-kms-v3d' "$config"; then
    warn "pilote graphique KMS absent de config.txt : ajout de dtoverlay=vc4-kms-v3d"
    [ "$DRY_RUN" = 1 ] || printf '\n[all]\ndtoverlay=vc4-kms-v3d\n' >> "$config"
fi
if ! grep -qE '^\s*disable_splash=1' "$config"; then
    [ "$DRY_RUN" = 1 ] || printf '\n[all]\n# darksign : pas d'"'"'écran arc-en-ciel au démarrage\ndisable_splash=1\n' >> "$config"
    info "écran arc-en-ciel du firmware désactivé"
else
    info "config.txt déjà configuré"
fi

run systemctl disable -q getty@tty1.service 2>/dev/null || true
info "invite de connexion à l'écran désactivée (SSH reste disponible)"
if [ "$(systemctl get-default)" = graphical.target ]; then
    warn "image avec bureau : le bureau est désactivé, il occuperait l'écran"
    run systemctl set-default multi-user.target
    run systemctl disable -q display-manager.service 2>/dev/null || true
fi

# journal conservé entre les redémarrages : diagnostic après un incident
if ! grep -rqs '^Storage=persistent' /etc/systemd/journald.conf.d/; then
    run mkdir -p /etc/systemd/journald.conf.d /var/log/journal
    if [ "$DRY_RUN" = 0 ]; then
        printf '[Journal]\nStorage=persistent\nSystemMaxUse=100M\n' \
            > /etc/systemd/journald.conf.d/darksign.conf
    fi
    run systemctl restart systemd-journald
    info "journal système conservé entre les redémarrages (100 Mo max)"
fi

# --- fin -------------------------------------------------------------------------
echo
echo "${G}${B}Installation terminée.${N}"
host="$(hostname)"
addr="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo "    Au démarrage : écran noir, animation darksign, puis l'écran d'accueil."
if [ "$NET_MODE" = ap ]; then
    echo "    Point d'accès : « $AP_SSID », mot de passe « $AP_PASSWORD »"
    echo "    Administration (une fois connecté à ce réseau) : http://10.42.0.1:8080"
else
    echo "    Administration : http://${addr:-<adresse-du-pi>}:8080  ou  http://$host.local:8080"
fi
echo "    Sauvegardes de la configuration de démarrage : $BOOT_DIR/*$BACKUP_SUFFIX"
if [ "$DRY_RUN" = 1 ]; then
    echo "    (simulation : rien n'a été modifié)"
elif [ "$REBOOT" = 1 ] || confirm "Redémarrer maintenant pour lancer le lecteur ?"; then
    echo "    Redémarrage…"
    systemctl reboot
else
    echo "    Redémarrez pour terminer : sudo reboot"
fi

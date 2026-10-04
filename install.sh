#!/usr/bin/env bash
# DarkSign installer: from a fresh Raspberry Pi OS Lite (Trixie) card to a
# player that boots straight into the animation.
#
#   curl -fsSL https://raw.githubusercontent.com/nopalpite/videoplayer/main/install.sh | sudo bash
# or, from a cloned repository:
#   sudo ./install.sh [options]
#
# Options:
#   --lang CODE   installer and player screen language (en, fr); without this
#                 option, the question is asked
#   --user NAME   user running the player (default: the one running sudo,
#                 else "pi")
#   --dir PATH    installation folder (default: the script folder when run
#                 from a cloned repository, else ~NAME/videoplayer)
#   --reset       erase media, subtitles and configuration of an existing
#                 installation (back to the "first boot" state); asks for
#                 confirmation, or requires --yes without a terminal
#   --yes         confirm --reset without asking
#   --name NAME   player name ("Hall A"): displayed, and used in its address
#                 (hall-a.local) and its access point name; without this
#                 option, the question is asked
#   --network MODE  network: "client" (venue Wi-Fi, with a fallback access
#                 point) or "ap" (standalone access point); without this
#                 option, the question is asked (current setting as default)
#   --ap-ssid NAME, --ap-password PASSWORD
#                 access point name and password (default: the player name
#                 and a generated password)
#   --reboot      reboot at the end without asking
#   --dry-run     show what would be done, without changing anything
#   --force       skip the hardware and version checks
#
# Safe to run again: an existing installation is updated, its media and
# configuration are kept (unless --reset).
#
# Translations: the installer runs before the project is downloaded (curl |
# bash), so its texts are below (T_en, T_fr), not in locales/. To add a
# language, add a T_xx array and the language to LANGUAGES.
set -euo pipefail

REPO_URL="${DARKSIGN_REPO:-https://github.com/nopalpite/videoplayer.git}"
BRANCH="${DARKSIGN_BRANCH:-main}"
PACKAGES=(git mpv python3-mpv python3-flask python3-libgpiod python3-pil
          python3-qrcode python3-numpy fonts-inter ffmpeg avahi-daemon
          raspi-utils-core rsync dnsmasq-base polkitd hostapd)
BOOT_PARAMS=(quiet loglevel=3 logo.nologo vt.global_cursor_default=0
             consoleblank=0 systemd.show_status=false rd.udev.log_level=3
             udev.log_level=3)
BACKUP_SUFFIX=".before-darksign"
OLD_BACKUP_SUFFIX=".avant-darksign"   # backups of earlier versions
LANGUAGES=(en fr)

TARGET_USER="${SUDO_USER:-}"
INSTALL_DIR=""
RESET=0; REBOOT=0; DRY_RUN=0; FORCE=0; YES=0
NET_MODE=""; AP_SSID=""; AP_PASSWORD=""; PLAYER_NAME=""; LANG_CODE=""

# --- texts (printf formats: %s = argument) -----------------------------------------
declare -A T_en=(
    [title]="installing the player"
    [error]="Error:"
    [dry]="(dry run)"
    [yes_no]="[y/N]"
    [unknown_option]="unknown option: %s (see --help)"
    [opt_network]="--network: \"client\" or \"ap\""
    [opt_lang]="--lang: one of %s"
    [opt_ap_password]="--ap-password: 8 to 63 characters"
    [opt_ap_ssid]="--ap-ssid: 32 characters at most"
    [opt_name]="--name: 40 characters at most"
    [step_checks]="Checks"
    [need_root]="run the installer with sudo."
    [no_root_player]="the player must not run as root: use --user."
    [no_user]="the user \"%s\" does not exist."
    [user_dir]="user: %s    folder: %s"
    [not_pi]="this is not a Raspberry Pi (\"%s\"): --force to go ahead anyway."
    [unknown_hw]="unknown hardware: \"%s\""
    [hardware]="hardware: %s"
    [tested_pi3]="tested on Raspberry Pi 3 and 4; on this model, hardware decoding and display may differ."
    [need_trixie]="Raspberry Pi OS \"Trixie\" (Debian 13) required, found: %s."
    [unsupported_os]="unsupported version: %s"
    [system]="system: %s"
    [no_cmdline]="cmdline.txt not found (neither /boot/firmware nor /boot)."
    [step_packages]="Packages"
    [installed]="installed: %s"
    [step_code]="Player code"
    [in_place]="installing in place: %s"
    [copying]="copying %s to %s"
    [updating]="updating the existing repository"
    [not_darksign]="%s already exists and is not a darksign repository: choose --dir."
    [cloning]="cloning %s"
    [step_media]="Media and configuration"
    [reset_erase]="--reset: erasing %s media file(s) and the configuration"
    [reset_folder]="target folder: %s"
    [reset_no_tty]="--reset without a terminal to confirm: add --yes to erase %s."
    [reset_confirm]="Erase the media and configuration of %s?"
    [reset_cancelled]="erasing cancelled."
    [existing_kept]="existing installation: media and configuration kept (--reset to start over)"
    [fresh]="no media, no subtitles, blank configuration: setup screen at boot"
    [step_rights]="User permissions"
    [no_group]="group \"%s\" missing"
    [groups]="%s: access to the screen (video, render), sound (audio) and GPIO (gpio)"
    [bad_sudo]="invalid sudo rule: %s"
    [sudo_rule]="%s: reboot, power off, name and access point of the Pi from the web interface"
    [polkit_rule]="%s: network management (Wi-Fi, access point) from the web interface"
    [no_polkit]="polkit missing: the network cannot be managed from the web interface"
    [step_name]="Name, language and network"
    [lang_question]="Language / Langue:"
    [choice]="Choice [%s]: "
    [name_intro1]="Player name, displayed and used in its address (e.g. \"Hall A\":"
    [name_intro2]="http://hall-a.local:8080) and in its Wi-Fi access point name."
    [name_prompt]="Player name [%s]: "
    [name_too_long]="name too long (40 characters max.): unchanged"
    [network_question]="How does the player connect?"
    [network_client1]="1) Venue Wi-Fi: network set in Raspberry Pi Imager or added from the"
    [network_client2]="   web interface; fallback access point if none can be reached"
    [network_ap1]="2) Standalone access point: the player creates its own Wi-Fi network"
    [network_ap2]="   (events, no box or router)"
    [player_name_default]="the player name"
    [ssid_prompt]="Wi-Fi network name [%s]: "
    [ssid_too_long]="name too long: unchanged"
    [keep_password]="empty = keep \"%s\""
    [generated_password]="empty = generated"
    [password_prompt]="Password (8 to 63 characters, %s): "
    [password_length]="8 to 63 characters"
    [dry_name]="name: %s, network: %s"
    [unchanged]="unchanged"
    [name_address]="name: \"%s\"  ·  address: http://%s.local:8080"
    [ap_mode]="standalone access point: \"%s\", password \"%s\""
    [client_mode]="venue Wi-Fi; fallback access point \"%s\" if no network can be reached"
    [step_services]="Services"
    [no_template]="template not found: %s"
    [services]="videoplayer (player, started as soon as the screen is ready) and videoplayer-web (port 8080)"
    [old_ap_removed]="former NetworkManager access point removed"
    [restarted]="%s restarted"
    [step_boot]="Quiet boot"
    [bad_cmdline]="unexpected cmdline.txt (no root=), nothing changed."
    [kernel]="kernel: messages on tty3, logos and cursor hidden"
    [cmdline_ok]="cmdline.txt already configured"
    [no_kms]="KMS graphics driver missing from config.txt: adding dtoverlay=vc4-kms-v3d"
    [rainbow]="firmware rainbow screen disabled"
    [config_ok]="config.txt already configured"
    [no_getty]="login prompt on the screen disabled (SSH stays available)"
    [desktop]="image with a desktop: the desktop is disabled, it would take the screen"
    [journal]="system journal kept across reboots (100 MB max)"
    [done]="Installation complete."
    [boot_sequence]="At boot: black screen, darksign animation, then the setup screen."
    [ap_summary]="Access point: \"%s\", password \"%s\""
    [ap_admin]="Web interface (once connected to that network): http://10.42.0.1:8080"
    [admin]="Web interface: http://%s:8080  or  http://%s.local:8080"
    [backups]="Backups of the boot configuration: %s"
    [dry_done]="(dry run: nothing was changed)"
    [reboot_confirm]="Reboot now to start the player?"
    [rebooting]="Rebooting…"
    [reboot_later]="Reboot to finish: sudo reboot"
)
declare -A T_fr=(
    [title]="installation du lecteur"
    [error]="Erreur :"
    [dry]="(simulation)"
    [yes_no]="[o/N]"
    [unknown_option]="option inconnue : %s (voir --help)"
    [opt_network]="--network : « client » ou « ap »"
    [opt_lang]="--lang : une langue parmi %s"
    [opt_ap_password]="--ap-password : 8 à 63 caractères"
    [opt_ap_ssid]="--ap-ssid : 32 caractères au maximum"
    [opt_name]="--name : 40 caractères au maximum"
    [step_checks]="Vérifications"
    [need_root]="lancez l'installateur avec sudo."
    [no_root_player]="le lecteur ne doit pas tourner en root : utilisez --user."
    [no_user]="l'utilisateur « %s » n'existe pas."
    [user_dir]="utilisateur : %s    dossier : %s"
    [not_pi]="ce n'est pas un Raspberry Pi (« %s ») : --force pour passer outre."
    [unknown_hw]="matériel non reconnu : « %s »"
    [hardware]="matériel : %s"
    [tested_pi3]="testé sur Raspberry Pi 3 et 4 ; sur ce modèle, le décodage matériel et l'affichage peuvent différer."
    [need_trixie]="Raspberry Pi OS « Trixie » (Debian 13) requis, trouvé : %s."
    [unsupported_os]="version non prise en charge : %s"
    [system]="système : %s"
    [no_cmdline]="cmdline.txt introuvable (ni /boot/firmware, ni /boot)."
    [step_packages]="Paquets"
    [installed]="installés : %s"
    [step_code]="Code du lecteur"
    [in_place]="installation sur place : %s"
    [copying]="copie de %s vers %s"
    [updating]="mise à jour du dépôt existant"
    [not_darksign]="%s existe déjà et n'est pas un dépôt darksign : choisissez --dir."
    [cloning]="clonage de %s"
    [step_media]="Médias et configuration"
    [reset_erase]="--reset : effacement de %s fichier(s) de médias et de la configuration"
    [reset_folder]="dossier visé : %s"
    [reset_no_tty]="--reset sans terminal pour confirmer : ajoutez --yes pour effacer %s."
    [reset_confirm]="Effacer les médias et la configuration de %s ?"
    [reset_cancelled]="effacement annulé."
    [existing_kept]="installation existante : médias et configuration conservés (--reset pour repartir de zéro)"
    [fresh]="aucun média, aucun sous-titre, configuration vierge : écran d'accueil au démarrage"
    [step_rights]="Droits de l'utilisateur"
    [no_group]="groupe « %s » absent"
    [groups]="%s : accès à l'écran (video, render), au son (audio) et aux GPIO (gpio)"
    [bad_sudo]="règle sudo invalide : %s"
    [sudo_rule]="%s : redémarrage, extinction, nom et point d'accès du Pi depuis l'interface web"
    [polkit_rule]="%s : gestion du réseau (Wi-Fi, point d'accès) depuis l'interface web"
    [no_polkit]="polkit absent : le réseau ne pourra pas être géré depuis l'interface"
    [step_name]="Nom, langue et réseau"
    [lang_question]="Language / Langue :"
    [choice]="Choix [%s] : "
    [name_intro1]="Nom du lecteur, affiché et repris dans son adresse (ex. « Hall A » :"
    [name_intro2]="http://hall-a.local:8080) et dans le nom de son point d'accès Wi-Fi."
    [name_prompt]="Nom du lecteur [%s] : "
    [name_too_long]="nom trop long (40 caractères max.) : inchangé"
    [network_question]="Comment le lecteur se connecte-t-il ?"
    [network_client1]="1) Wi-Fi du lieu : réseau configuré dans Raspberry Pi Imager ou ajouté"
    [network_client2]="   depuis l'interface ; point d'accès de secours si aucun n'est joignable"
    [network_ap1]="2) Point d'accès autonome : le lecteur crée son propre réseau Wi-Fi"
    [network_ap2]="   (événementiel, sans box ni routeur)"
    [player_name_default]="le nom du lecteur"
    [ssid_prompt]="Nom du réseau Wi-Fi [%s] : "
    [ssid_too_long]="nom trop long : inchangé"
    [keep_password]="vide = garder « %s »"
    [generated_password]="vide = généré"
    [password_prompt]="Mot de passe (8 à 63 caractères, %s) : "
    [password_length]="8 à 63 caractères"
    [dry_name]="nom : %s, réseau : %s"
    [unchanged]="inchangé"
    [name_address]="nom : « %s »  ·  adresse : http://%s.local:8080"
    [ap_mode]="point d'accès autonome : « %s », mot de passe « %s »"
    [client_mode]="Wi-Fi du lieu ; point d'accès de secours « %s » si aucun réseau n'est joignable"
    [step_services]="Services"
    [no_template]="modèle introuvable : %s"
    [services]="videoplayer (lecteur, démarré dès que l'écran est prêt) et videoplayer-web (port 8080)"
    [old_ap_removed]="ancien point d'accès NetworkManager supprimé"
    [restarted]="%s relancé"
    [step_boot]="Démarrage silencieux"
    [bad_cmdline]="cmdline.txt inattendu (pas de root=), rien n'est modifié."
    [kernel]="noyau : messages sur tty3, logos et curseur masqués"
    [cmdline_ok]="cmdline.txt déjà configuré"
    [no_kms]="pilote graphique KMS absent de config.txt : ajout de dtoverlay=vc4-kms-v3d"
    [rainbow]="écran arc-en-ciel du firmware désactivé"
    [config_ok]="config.txt déjà configuré"
    [no_getty]="invite de connexion à l'écran désactivée (SSH reste disponible)"
    [desktop]="image avec bureau : le bureau est désactivé, il occuperait l'écran"
    [journal]="journal système conservé entre les redémarrages (100 Mo max)"
    [done]="Installation terminée."
    [boot_sequence]="Au démarrage : écran noir, animation darksign, puis l'écran d'accueil."
    [ap_summary]="Point d'accès : « %s », mot de passe « %s »"
    [ap_admin]="Administration (une fois connecté à ce réseau) : http://10.42.0.1:8080"
    [admin]="Administration : http://%s:8080  ou  http://%s.local:8080"
    [backups]="Sauvegardes de la configuration de démarrage : %s"
    [dry_done]="(simulation : rien n'a été modifié)"
    [reboot_confirm]="Redémarrer maintenant pour lancer le lecteur ?"
    [rebooting]="Redémarrage…"
    [reboot_later]="Redémarrez pour terminer : sudo reboot"
)

# msg KEY [ARG...]: text in the chosen language (English if missing)
msg() {
    local key="$1" format
    shift
    local -n table="T_${LANG_CODE:-en}"
    format="${table[$key]:-${T_en[$key]:-$key}}"
    # shellcheck disable=SC2059   # the format comes from the tables above
    printf -- "$format" "$@"
}

# --- output -----------------------------------------------------------------------
if [ -t 1 ]; then B=$'\e[1m'; A=$'\e[33m'; R=$'\e[31m'; G=$'\e[32m'; N=$'\e[0m'
else B=""; A=""; R=""; G=""; N=""; fi
step=0
title() { step=$((step + 1)); echo; echo "${B}${A}[$step]${N}${B} $*${N}"; }
info()  { echo "    $*"; }
warn()  { echo "    ${A}!${N} $*"; }
die()   { echo "${R}$(msg error)${N} $*" >&2; exit 1; }
run()   { if [ "$DRY_RUN" = 1 ]; then echo "    $(msg dry) $*"; else "$@"; fi; }
has_tty() { ( exec < /dev/tty ) 2>/dev/null; }   # real terminal (even through curl | bash)
confirm() {   # confirm "question": no when no terminal can answer
    has_tty || return 1
    local answer; read -r -p "    $1 $(msg yes_no) " answer < /dev/tty || return 1
    [[ "$answer" =~ ^[oOyY] ]]
}

# --- options ---------------------------------------------------------------------
while [ $# -gt 0 ]; do
    case "$1" in
        --lang) LANG_CODE="$2"; shift 2 ;;
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
        -h|--help) sed -n '2,38p' "${BASH_SOURCE[0]:-/dev/null}" 2>/dev/null \
                   | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "$(msg unknown_option "$1")" ;;
    esac
done

# installer language: option, else question (system language as default)
if [ -n "$LANG_CODE" ]; then
    [[ " ${LANGUAGES[*]} " == *" $LANG_CODE "* ]] \
        || { LANG_CODE=""; die "$(msg opt_lang "${LANGUAGES[*]}")"; }
else
    system_lang="${LC_ALL:-${LC_MESSAGES:-${LANG:-}}}"
    system_lang="${system_lang%%[_.@]*}"
    default=1
    for i in "${!LANGUAGES[@]}"; do
        [ "${LANGUAGES[$i]}" = "$system_lang" ] && default=$((i + 1))
    done
    LANG_CODE="${LANGUAGES[$((default - 1))]}"
    if has_tty && [ "$DRY_RUN" = 0 ]; then
        echo "$(msg lang_question)"
        echo "  1) English"
        echo "  2) Français"
        read -r -p "$(msg choice "$default")" answer < /dev/tty || answer=""
        answer="${answer:-$default}"
        [[ "$answer" =~ ^[0-9]+$ ]] && [ "$answer" -ge 1 ] && [ "$answer" -le ${#LANGUAGES[@]} ] \
            && LANG_CODE="${LANGUAGES[$((answer - 1))]}"
    fi
fi

case "$NET_MODE" in ""|client|ap) ;; *) die "$(msg opt_network)" ;; esac
if [ -n "$AP_PASSWORD" ] && { [ ${#AP_PASSWORD} -lt 8 ] || [ ${#AP_PASSWORD} -gt 63 ]; }; then
    die "$(msg opt_ap_password)"
fi
[ ${#AP_SSID} -le 32 ] || die "$(msg opt_ap_ssid)"
[ ${#PLAYER_NAME} -le 40 ] || die "$(msg opt_name)"

echo "${B}darksign${N} — $(msg title)"

# --- 1. checks -------------------------------------------------------------------
title "$(msg step_checks)"
[ "$(id -u)" = 0 ] || die "$(msg need_root)"
TARGET_USER="${TARGET_USER:-pi}"
[ "$TARGET_USER" != root ] || die "$(msg no_root_player)"
id "$TARGET_USER" >/dev/null 2>&1 || die "$(msg no_user "$TARGET_USER")"
TARGET_GROUP="$(id -gn "$TARGET_USER")"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
SRC_DIR=""     # repository the script runs from (empty with curl | bash)
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    candidate="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    [ -f "$candidate/player.py" ] && SRC_DIR="$candidate"
fi
# run from a repository: install in place, never elsewhere by default
INSTALL_DIR="$(realpath -m "${INSTALL_DIR:-${SRC_DIR:-$TARGET_HOME/videoplayer}}")"
info "$(msg user_dir "$TARGET_USER" "$INSTALL_DIR")"

model="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || true)"
if [[ "$model" != *"Raspberry Pi"* ]]; then
    [ "$FORCE" = 1 ] || die "$(msg not_pi "${model:-?}")"
    warn "$(msg unknown_hw "${model:-?}")"
else
    info "$(msg hardware "$model")"
    [[ "$model" == *"Raspberry Pi 3"* || "$model" == *"Raspberry Pi 4"* ]] \
        || warn "$(msg tested_pi3)"
fi

. /etc/os-release
if [ "${VERSION_ID:-}" != 13 ]; then
    # mpv >= 0.38 (loadfile syntax) and libgpiod 2 are required: Debian 13 "Trixie"
    [ "$FORCE" = 1 ] || die "$(msg need_trixie "${PRETTY_NAME:-?}")"
    warn "$(msg unsupported_os "${PRETTY_NAME:-?}")"
else
    info "$(msg system "$PRETTY_NAME")"
fi

BOOT_DIR=/boot/firmware
[ -f "$BOOT_DIR/cmdline.txt" ] || BOOT_DIR=/boot
[ -f "$BOOT_DIR/cmdline.txt" ] || die "$(msg no_cmdline)"

# --- 2. packages -----------------------------------------------------------------
title "$(msg step_packages)"
export DEBIAN_FRONTEND=noninteractive
run apt-get update -qq
run apt-get install -y -qq --no-install-recommends "${PACKAGES[@]}"
info "$(msg installed "${PACKAGES[*]}")"
# hostapd only serves the player access point (darksign-ap.service): its
# default service would take the Wi-Fi at boot
run systemctl disable --now -q hostapd.service 2>/dev/null || true
run systemctl mask -q hostapd.service

# --- 3. code ---------------------------------------------------------------------
title "$(msg step_code)"
if [ -n "$SRC_DIR" ] && [ "$SRC_DIR" = "$INSTALL_DIR" ]; then
    info "$(msg in_place "$INSTALL_DIR")"
elif [ -n "$SRC_DIR" ]; then
    # copy of the local repository code (changes included): never the media,
    # the state nor the git history (which would overwrite the target's)
    info "$(msg copying "$SRC_DIR" "$INSTALL_DIR")"
    run mkdir -p "$INSTALL_DIR"
    run rsync -a --exclude .git/ --exclude media/ --exclude data/ \
        --exclude __pycache__/ "$SRC_DIR/" "$INSTALL_DIR/"
elif [ -d "$INSTALL_DIR/.git" ]; then
    info "$(msg updating)"
    run sudo -u "$TARGET_USER" git -C "$INSTALL_DIR" pull --ff-only
elif [ -e "$INSTALL_DIR" ] && [ -n "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]; then
    die "$(msg not_darksign "$INSTALL_DIR")"
else
    info "$(msg cloning "$REPO_URL")"
    run mkdir -p "$INSTALL_DIR"      # folder created first: it may be outside home
    run chown "$TARGET_USER:$TARGET_GROUP" "$INSTALL_DIR"
    run sudo -u "$TARGET_USER" git clone -q --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi

# --- 4. "first boot" state -------------------------------------------------------
title "$(msg step_media)"
if [ "$RESET" = 1 ] && { [ -d "$INSTALL_DIR/media" ] || [ -d "$INSTALL_DIR/data" ]; }; then
    count=$(find "$INSTALL_DIR/media" -maxdepth 1 -type f 2>/dev/null | wc -l)
    warn "$(msg reset_erase "$count")"
    warn "$(msg reset_folder "${B}$INSTALL_DIR${N}")"
    if [ "$DRY_RUN" = 1 ] || [ "$YES" = 1 ]; then
        :
    elif ! has_tty; then
        die "$(msg reset_no_tty "$INSTALL_DIR")"
    elif ! confirm "$(msg reset_confirm "$INSTALL_DIR")"; then
        die "$(msg reset_cancelled)"
    fi
    run systemctl stop videoplayer videoplayer-web 2>/dev/null || true
    run rm -rf "$INSTALL_DIR/media" "$INSTALL_DIR/data"
fi
if [ -d "$INSTALL_DIR/media" ] && [ -n "$(ls -A "$INSTALL_DIR/media" 2>/dev/null)" ]; then
    info "$(msg existing_kept)"
else
    info "$(msg fresh)"
fi
run mkdir -p "$INSTALL_DIR/media" "$INSTALL_DIR/data"
run chown -R "$TARGET_USER:$TARGET_GROUP" "$INSTALL_DIR"

# --- 5. permissions --------------------------------------------------------------
title "$(msg step_rights)"
for group in video render audio gpio; do
    if getent group "$group" >/dev/null; then
        run usermod -aG "$group" "$TARGET_USER"
    else
        warn "$(msg no_group "$group")"
    fi
done
info "$(msg groups "$TARGET_USER")"

# web interface: reboot / power off the Pi and change its name, nothing more.
# The scripts are copied out of the repository (editable by the user) and
# owned by root: otherwise the sudo rule would allow becoming root.
helper=/usr/local/sbin/darksign-hostname
run install -o root -g root -m 0755 "$INSTALL_DIR/system/darksign-hostname" "$helper"
# access point: hostapd + dnsmasq, started on demand by the web backend
run install -o root -g root -m 0755 "$INSTALL_DIR/system/darksign-ap" /usr/local/sbin/darksign-ap
ap="/usr/bin/systemctl start darksign-ap.service, /usr/bin/systemctl stop darksign-ap.service, /usr/bin/systemctl restart darksign-ap.service"
sudoers=/etc/sudoers.d/darksign
rule="$TARGET_USER ALL=(root) NOPASSWD: /usr/bin/systemctl reboot, /usr/bin/systemctl poweroff, $helper, $ap"
if [ "$DRY_RUN" = 1 ]; then
    echo "    $(msg dry) $sudoers: $rule"
elif [ "$(cat "$sudoers" 2>/dev/null)" != "$rule" ]; then
    tmp="$(mktemp)"
    echo "$rule" > "$tmp"
    visudo -cqf "$tmp" || { rm -f "$tmp"; die "$(msg bad_sudo "$rule")"; }
    install -m 0440 "$tmp" "$sudoers"
    rm -f "$tmp"
fi
info "$(msg sudo_rule "$TARGET_USER")"

# web interface: network (venue Wi-Fi, standalone access point) through NetworkManager
polkit_rule=/etc/polkit-1/rules.d/50-darksign.rules
rule_js="// darksign: the player web interface manages the network (Wi-Fi, access point)
polkit.addRule(function(action, subject) {
    if (action.id.indexOf(\"org.freedesktop.NetworkManager.\") === 0 &&
        subject.user === \"$TARGET_USER\") {
        return polkit.Result.YES;
    }
});"
if [ "$DRY_RUN" = 1 ]; then
    echo "    $(msg dry) $polkit_rule"
elif [ -d /etc/polkit-1/rules.d ]; then
    if [ "$(cat "$polkit_rule" 2>/dev/null)" != "$rule_js" ]; then
        echo "$rule_js" > "$polkit_rule"
        chmod 0644 "$polkit_rule"
    fi
    info "$(msg polkit_rule "$TARGET_USER")"
else
    warn "$(msg no_polkit)"
fi

# --- 6. name, language and network ----------------------------------------------
title "$(msg step_name)"
config_py() { sudo -u "$TARGET_USER" env -C "$INSTALL_DIR" python3 -c "$1" "${@:2}"; }
SEP=$'\x1f'   # field separator (not a blank: empty fields are kept)
# current settings (existing installation): mode, name, access point
current="$(config_py 'from common import load_config, CONFIG_FILE, player_name
cfg = load_config()
n = cfg["network"]
print(n["mode"] if CONFIG_FILE.exists() and n.get("ap_password") else "",
      player_name(cfg), n.get("ap_ssid") or "", n.get("ap_password") or "", sep="\x1f")' \
    2>/dev/null || true)"
IFS="$SEP" read -r CURRENT_MODE CURRENT_NAME CURRENT_SSID CURRENT_PASSWORD <<< "$current" || true

if [ -z "$PLAYER_NAME" ] && has_tty && [ "$DRY_RUN" = 0 ]; then
    info "$(msg name_intro1)"
    info "$(msg name_intro2)"
    read -r -p "    $(msg name_prompt "${CURRENT_NAME:-$(hostname)}")" PLAYER_NAME < /dev/tty \
        || PLAYER_NAME=""
    [ ${#PLAYER_NAME} -le 40 ] || { warn "$(msg name_too_long)"; PLAYER_NAME=""; }
fi

if [ -z "$NET_MODE" ] && has_tty && [ "$DRY_RUN" = 0 ]; then
    [ "$CURRENT_MODE" = ap ] && default=2 || default=1
    info "$(msg network_question)"
    info "  $(msg network_client1)"
    info "  $(msg network_client2)"
    info "  $(msg network_ap1)"
    info "  $(msg network_ap2)"
    read -r -p "    $(msg choice "$default")" answer < /dev/tty || answer=""
    [ "${answer:-$default}" = 2 ] && NET_MODE=ap || NET_MODE=client
    if [ "$NET_MODE" = ap ] && [ -z "$AP_SSID$AP_PASSWORD" ]; then
        ssid_default="${CURRENT_SSID:-$(msg player_name_default)}"
        read -r -p "    $(msg ssid_prompt "$ssid_default")" AP_SSID < /dev/tty || AP_SSID=""
        [ ${#AP_SSID} -le 32 ] || { warn "$(msg ssid_too_long)"; AP_SSID=""; }
        if [ -n "$CURRENT_PASSWORD" ]; then pass_hint="$(msg keep_password "$CURRENT_PASSWORD")"
        else pass_hint="$(msg generated_password)"; fi
        while :; do
            read -r -p "    $(msg password_prompt "$pass_hint")" AP_PASSWORD < /dev/tty \
                || AP_PASSWORD=""
            [ -z "$AP_PASSWORD" ] || { [ ${#AP_PASSWORD} -ge 8 ] && [ ${#AP_PASSWORD} -le 63 ]; } \
                && break
            warn "$(msg password_length)"
        done
    fi
fi
NET_MODE="${NET_MODE:-${CURRENT_MODE:-client}}"

if [ "$DRY_RUN" = 1 ]; then
    echo "    $(msg dry) $(msg dry_name "${PLAYER_NAME:-$(msg unchanged)}" "$NET_MODE")"
else
    # written to the player configuration; the web backend applies the
    # network at start (network.py), like a change made from the web page
    result="$(config_py 'import sys
from common import ap_ssid, hostname_for, load_config, player_name, save_config
from network import generate_password
mode, ssid, password, name, language = sys.argv[1:6]
cfg = load_config()
net = cfg["network"]
if name:
    cfg["name"] = name
cfg["language"] = language   # player screen language = installer language
net["mode"] = mode
net["ap_ssid"] = ssid or net.get("ap_ssid")   # empty: the player name
net["ap_password"] = password or net.get("ap_password") or generate_password()
save_config(cfg)
print(hostname_for(cfg["name"]) if cfg["name"] else "", player_name(cfg),
      ap_ssid(cfg), net["ap_password"], sep="\x1f")' \
        "$NET_MODE" "$AP_SSID" "$AP_PASSWORD" "$PLAYER_NAME" "$LANG_CODE")"
    IFS="$SEP" read -r NEW_HOSTNAME PLAYER_NAME AP_SSID AP_PASSWORD <<< "$result"
    if [ -n "$NEW_HOSTNAME" ] && [ "$NEW_HOSTNAME" != "$(hostname)" ]; then
        /usr/local/sbin/darksign-hostname "$NEW_HOSTNAME"
    fi
    info "$(msg name_address "$PLAYER_NAME" "$(hostname)")"
fi
if [ "$NET_MODE" = ap ]; then
    info "$(msg ap_mode "$AP_SSID" "$AP_PASSWORD")"
else
    info "$(msg client_mode "$AP_SSID")"
fi

# --- 7. services -----------------------------------------------------------------
title "$(msg step_services)"
for unit in videoplayer videoplayer-web darksign-ap; do
    template="$INSTALL_DIR/systemd/$unit.service.in"
    [ -f "$template" ] || [ "$DRY_RUN" = 1 ] || die "$(msg no_template "$template")"
    if [ "$DRY_RUN" = 1 ]; then
        echo "    $(msg dry) $template -> /etc/systemd/system/$unit.service"
    else
        sed -e "s#@USER@#$TARGET_USER#g" -e "s#@GROUP@#$TARGET_GROUP#g" \
            -e "s#@DIR@#$INSTALL_DIR#g" "$template" > "/etc/systemd/system/$unit.service"
    fi
done
run systemctl daemon-reload
run systemctl enable -q videoplayer videoplayer-web
info "$(msg services)"
# access point of earlier versions (NetworkManager profile): replaced by
# darksign-ap.service, it would take the Wi-Fi back if it stayed
if [ "$DRY_RUN" = 0 ] && nmcli -t -f NAME connection show 2>/dev/null | grep -qx darksign-ap; then
    nmcli connection delete darksign-ap >/dev/null && info "$(msg old_ap_removed)"
fi
for unit in videoplayer videoplayer-web darksign-ap; do   # update: load the new code
    if systemctl is-active -q "$unit"; then
        run systemctl restart "$unit"
        info "$(msg restarted "$unit")"
    fi
done

# --- 8. quiet boot ---------------------------------------------------------------
title "$(msg step_boot)"
backup() {   # keep a single backup: the original from before darksign
    [ -f "$1$BACKUP_SUFFIX" ] || [ -f "$1$OLD_BACKUP_SUFFIX" ] \
        || run cp -p "$1" "$1$BACKUP_SUFFIX"
}
cmdline="$BOOT_DIR/cmdline.txt"
backup "$cmdline"
current="$(tr -d '\n' < "$cmdline")"
new="$(echo "$current" | sed -E 's/(^| )console=tty1( |$)/\1console=tty3\2/')"
for param in "${BOOT_PARAMS[@]}"; do
    [[ " $new " == *" $param "* ]] || new="$new $param"
done
if [ "$new" != "$current" ]; then
    [[ "$new" == *"root="* ]] || die "$(msg bad_cmdline)"
    if [ "$DRY_RUN" = 1 ]; then echo "    $(msg dry) cmdline.txt: $new"
    else echo "$new" > "$cmdline"; fi
    info "$(msg kernel)"
else
    info "$(msg cmdline_ok)"
fi

config="$BOOT_DIR/config.txt"
backup "$config"
if ! grep -qE '^\s*dtoverlay=vc4-kms-v3d' "$config"; then
    warn "$(msg no_kms)"
    [ "$DRY_RUN" = 1 ] || printf '\n[all]\ndtoverlay=vc4-kms-v3d\n' >> "$config"
fi
if ! grep -qE '^\s*disable_splash=1' "$config"; then
    [ "$DRY_RUN" = 1 ] || printf '\n[all]\n# darksign: no rainbow screen at boot\ndisable_splash=1\n' >> "$config"
    info "$(msg rainbow)"
else
    info "$(msg config_ok)"
fi

run systemctl disable -q getty@tty1.service 2>/dev/null || true
info "$(msg no_getty)"
if [ "$(systemctl get-default)" = graphical.target ]; then
    warn "$(msg desktop)"
    run systemctl set-default multi-user.target
    run systemctl disable -q display-manager.service 2>/dev/null || true
fi

# journal kept across reboots: diagnosis after an incident
if ! grep -rqs '^Storage=persistent' /etc/systemd/journald.conf.d/; then
    run mkdir -p /etc/systemd/journald.conf.d /var/log/journal
    if [ "$DRY_RUN" = 0 ]; then
        printf '[Journal]\nStorage=persistent\nSystemMaxUse=100M\n' \
            > /etc/systemd/journald.conf.d/darksign.conf
    fi
    run systemctl restart systemd-journald
    info "$(msg journal)"
fi

# --- end -------------------------------------------------------------------------
echo
echo "${G}${B}$(msg done)${N}"
host="$(hostname)"
addr="$(hostname -I 2>/dev/null | awk '{print $1}')"
info "$(msg boot_sequence)"
if [ "$NET_MODE" = ap ]; then
    info "$(msg ap_summary "$AP_SSID" "$AP_PASSWORD")"
    info "$(msg ap_admin)"
else
    info "$(msg admin "${addr:-<pi-address>}" "$host")"
fi
info "$(msg backups "$BOOT_DIR/*$BACKUP_SUFFIX")"
if [ "$DRY_RUN" = 1 ]; then
    info "$(msg dry_done)"
elif [ "$REBOOT" = 1 ] || confirm "$(msg reboot_confirm)"; then
    info "$(msg rebooting)"
    systemctl reboot
else
    info "$(msg reboot_later)"
fi

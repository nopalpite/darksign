# darksign

Lecteur vidéo pour Raspberry Pi — un petit pied de nez à BrightSign. Lecture plein écran sur la sortie HDMI du Pi (mpv, sans bureau) pilotée par
des boutons sur les GPIO, administrée depuis une interface web.

- `player.py` : lecteur (mpv + gpiod), service `videoplayer`
- `web.py` : interface d'administration sur le port 8080, service `videoplayer-web`
- `common.py` : configuration, médias, GPIO disponibles, dialogue avec le lecteur
- `transcode.py` : file de conversion des vidéos importées
- `network.py` : Wi-Fi du lieu (NetworkManager) ou point d'accès autonome
- `system/` : scripts root installés dans /usr/local/sbin (point d'accès,
  nom d'hôte), appelés par l'interface via sudo
- `splash.py` : écran d'accueil (image fixe et animation d'intro)
- `brand.py` : identité DarkSign (logo éclipse, mot-symbole, rendu du halo)
- `assets/intro.mp4` : intro animée générique (éclipse), générée par `splash.py intro`
- `media/` : vidéos et images envoyées depuis l'interface
- `data/config.json` : configuration (écrite par l'interface)

Les deux processus dialoguent via le socket unix `data/player.sock`
(commandes `status`, `reload`, `trigger`, `pause`).

## Installation

**Prérequis** : Raspberry Pi 3 et une carte SD flashée avec **Raspberry Pi OS
Lite (64-bit) « Trixie »**. Dans Raspberry Pi Imager, personnalisez l'image :
nom d'hôte, utilisateur et mot de passe, Wi-Fi (ou prévoir un câble Ethernet)
et SSH activé. Le code utilise mpv ≥ 0.38 et libgpiod 2, absents de Bookworm.

Au premier démarrage, connectez-vous en SSH puis lancez :

    curl -fsSL https://raw.githubusercontent.com/nopalpite/videoplayer/main/install.sh | sudo bash

ou, depuis un dépôt cloné : `sudo ./install.sh`. Redémarrez à la fin : le Pi
démarre sur l'animation darksign puis sur l'écran d'accueil, sans aucun média,
sous-titre ni configuration.

L'installateur :

1. vérifie le matériel et la version du système ;
2. installe les paquets (mpv, ffmpeg, Flask, libgpiod, Pillow, numpy, qrcode,
   police Inter, avahi pour le nom `.local`) ;
3. clone le dépôt dans `~/videoplayer` (ou installe sur place s'il est lancé
   depuis un dépôt cloné) ;
4. crée `media/` et `data/` vides ;
5. ajoute l'utilisateur aux groupes `video`, `render`, `audio` et `gpio` ;
6. installe et active les services `videoplayer` et `videoplayer-web`
   (générés depuis `systemd/*.service.in`) ;
7. configure le démarrage silencieux (voir « Démarrage ») et conserve le
   journal système entre les redémarrages.

Options : `--user NOM`, `--dir CHEMIN`, `--reboot`, `--dry-run` (affiche sans
rien modifier), `--force` (ignore les vérifications), `--reset` (efface
médias et configuration, confirmation demandée ou `--yes` sans terminal).
Voir `./install.sh --help`.

**Mise à jour** : relancer l'installateur. Il récupère la dernière version,
relance les services et conserve médias et configuration.

Pour tester sans bouton câblé : [gpio-web](https://github.com/nopalpite/gpio-web).

## Modes

- **Playlist** : un seul média (vidéo ou image) tourne en boucle sans fin ;
  plusieurs médias s'enchaînent dans l'ordre choisi (réorganisable), puis la
  liste recommence. Chaque vidéo est répétée un nombre de fois défini. Les répétitions
  d'une même vidéo sont sans coupure ; entre deux vidéos différentes, mpv
  précharge la suivante. Une image reste affichée une durée choisie (6 s par
  défaut).
- **Interactif** : une accroche (vidéo ou image) tourne en boucle ; un appui
  sur un bouton lance la vidéo associée, puis retour à l'accroche à la fin.
  Option : un appui peut ou non interrompre la vidéo en cours.
  Chaque vidéo se lance par un bouton GPIO, un message UDP, ou les deux.

## Nom du lecteur

Chaque lecteur a un nom (« Hall A », « Stand 12 »…), demandé à l'installation
(ou `--name`) et modifiable dans l'interface. Il est affiché dans l'interface
et sur l'écran d'accueil, et repris :

- dans son adresse : `http://hall-a.local:8080` (nom d'hôte du Pi, changé par
  `/usr/local/sbin/darksign-hostname`, que l'interface appelle via sudo) ;
- dans le nom du Wi-Fi de son point d'accès, sauf si un autre nom est choisi.

Pratique pour réutiliser les mêmes lecteurs d'un événement à l'autre, ou pour
en distinguer plusieurs sur un même réseau.

## Réseau

Deux fonctionnements, choisis à l'installation (question posée par
install.sh, ou `--network client|ap`, avec `--ap-ssid` et `--ap-password`)
et modifiables ensuite dans l'interface (section Réseau) :

- **Wi-Fi du lieu** : le lecteur rejoint le premier réseau Wi-Fi mémorisé à
  portée. Les réseaux s'ajoutent depuis l'interface (recherche des réseaux
  visibles, mot de passe). Si aucun réseau n'est joignable pendant 90 s (au
  démarrage ou plus tard), le lecteur active son point d'accès en secours,
  jusqu'au prochain redémarrage ou changement de mode : on ne le perd jamais.
- **Point d'accès autonome** (événementiel) : le lecteur crée son propre
  réseau Wi-Fi WPA2, sans box ni routeur. Il est joignable en
  `http://10.42.0.1:8080` ; nom (celui du lecteur par défaut) et mot de passe
  (généré à l'installation, modifiable) sont affichés sur l'écran d'accueil, avec un QR code pour
  rejoindre le réseau et un autre pour ouvrir l'interface.

Le câble Ethernet fonctionne dans les deux modes. Le Wi-Fi du lieu passe par
NetworkManager (install.sh autorise l'utilisateur du lecteur à le piloter :
règle polkit `/etc/polkit-1/rules.d/50-darksign.rules`). Le point d'accès est
le service `darksign-ap` (hostapd + dnsmasq, script `system/darksign-ap`) :
celui de NetworkManager annonce une authentification (PSK-SHA256) que la puce
du Pi 3 ne gère pas, et les téléphones refusaient de s'y connecter. Ici :
WPA2-PSK seul, CCMP, sans PMF.

Sur le point d'accès, tous les noms DNS pointent vers le lecteur et le port 80
mène à l'interface : les tests de connectivité des téléphones et ordinateurs
reçoivent la réponse attendue, sinon ils jugeraient le réseau « sans Internet »
et passeraient par les données mobiles, même pour joindre le lecteur. Android
vérifie aussi Google en HTTPS, impossible hors ligne : s'il affiche
« Connexion limitée », choisir « Se connecter quand même ». Un VPN actif sur
l'appareil capte aussi le trafic local : le couper le temps de l'administration.

## Commandes UDP

Le lecteur écoute des messages texte en UDP (port 5000 par défaut, modifiable
dans l'interface), un message par datagramme, sans tenir compte des majuscules
ni des espaces ou retours à la ligne autour :

- en mode interactif, le message d'un déclencheur lance sa vidéo ;
- dans tous les modes : `pause`, `play` (reprise) et `restart` (relance
  depuis le début).

    echo film | nc -u -w1 hall-a.local 5000

Un même message répété dans les 300 ms (émetteurs qui doublent l'envoi) ne
compte qu'une fois. Le dernier message reçu, son expéditeur et son effet
s'affichent dans l'interface.

## Démarrage

Au démarrage, l'écran reste noir (ni arc-en-ciel, ni texte, ni logo, ni
invite de connexion), puis l'intro DarkSign (`assets/intro.mp4`) est jouée dès
que la carte graphique est prête, sans attendre le réseau. Ensuite, le lecteur
enchaîne sur le contenu programmé, ou sur le tutoriel si rien ne l'est.

Réglages système correspondants (sauvegardes des originaux :
`/boot/firmware/*.avant-darksign`) :

- `cmdline.txt` : `console=tty3` au lieu de `console=tty1`, et
  `quiet loglevel=3 logo.nologo vt.global_cursor_default=0 consoleblank=0
  systemd.show_status=false rd.udev.log_level=3 udev.log_level=3` ;
- `config.txt` : `disable_splash=1` ;
- invite de connexion à l'écran désactivée : `sudo systemctl disable getty@tty1`
  (SSH et console série restent disponibles) ;
- service `videoplayer` sans dépendances par défaut, lancé après
  `dev-dri-card0.device` (voir `systemd/videoplayer.service.in`).

Tous ces réglages sont appliqués par `install.sh`.

## Écran d'accueil

Tant qu'aucun contenu n'est programmé (premier lancement, ou média
supprimé), l'écran affiche l'adresse de l'interface d'administration, le nom
`.local` et un QR code. Il se met à jour si l'adresse réseau change (toutes
les 10 s) et disparaît dès qu'un contenu est enregistré. Une accroche laissée
sur « écran noir » avec des boutons configurés reste un écran noir.

Au démarrage, si le réseau n'est pas encore là à la fin de l'intro (le
Wi-Fi met souvent ~50 s), le logo reste affiché jusqu'à 30 s en attendant une
adresse. Passé ce délai, un écran « Pas de connexion réseau » affiche un
diagnostic (Wi-Fi configuré ou non, câble Ethernet branché ou non) et les
pistes de résolution ; il se met à jour dès que l'état change. Un lecteur
configuré, lui, lit son contenu avec ou sans réseau. En filaire, la connexion
est automatique (DHCP) et l'adresse filaire est affichée en priorité.

L'écran s'ouvre sur une animation (~7 s) : un soleil, la lune qui l'éclipse,
la couronne qui devient le logo, puis les informations. Elle est en deux
parties enchaînées sans coupure : `assets/intro.mp4`, générique et livrée
avec le projet, et une fin propre à l'adresse réseau, calculée par le lecteur
en tâche de fond (~40 s sur un Pi 3, ~170 Mo de mémoire) puis mise en cache
dans `data/`. En attendant, l'image fixe est affichée.

Après une retouche de `brand.py` ou du début de l'animation dans `splash.py`,
régénérer l'intro : `python3 splash.py intro` (quelques minutes sur un Pi 3).

## Matériel

Par défaut, bouton entre la GPIO et GND (pull-up interne activé par le
lecteur). Le câblage vers 3V3 (pull-down) est sélectionnable dans l'interface.
Les GPIO occupées par une fonction (UART, I2C, SPI...) ne sont pas proposées.

La section Système de l'interface affiche l'état du Pi : température,
alimentation insuffisante ou processeur ralenti (maintenant ou depuis le
démarrage), espace libre sur la carte SD, mémoire, charge et temps depuis le
démarrage. Un Pi 3 ralentit vers 80 °C : prévoir un dissipateur dans un
boîtier fermé.

### Écran en portrait

Le lecteur ne tourne pas l'image : sur un Pi 3, la vidéo passe directement
sur un plan d'affichage matériel qui ne sait pas la tourner, et la rotation
par le GPU fait perdre 15 à 25 % des images en 1080p. Pour un écran monté en
portrait, exportez le contenu déjà tourné (vidéo 1920×1080 dont l'image est
couchée), par exemple :

    ffmpeg -i portrait.mp4 -vf transpose=2 -c:a copy pour-ecran-tourne.mp4

(`transpose=1` si l'écran est tourné dans l'autre sens.) Les sous-titres
doivent alors être incrustés dans l'image.

## Conversion automatique des vidéos

Chaque vidéo importée est analysée puis, si besoin, convertie en H.264
(décodé matériellement par le Pi 3) dans un MP4, en conservant la résolution
et la cadence d'origine. Seule exception : au-delà de 1080p, l'image est
réduite à 1080p, limite du décodeur. Une vidéo déjà adaptée est gardée telle
quelle, sans réencodage.

- Encodeur matériel du Pi (`h264_v4l2m2m`), ~10 Mb/s en 1080p25 ;
  x264 en secours si l'encodeur matériel refuse la source.
- Compter ~2,5 s de conversion par seconde de vidéo 1080p (plus pour du
  HEVC ou du ProRes, dont le décodage est logiciel).
- La conversion tourne en priorité basse : la lecture en cours n'est pas
  perturbée. Une seule conversion à la fois, les autres attendent.
- Le fichier d'origine est supprimé une fois converti. Il est gardé dans
  `media/.incoming/` tant que la conversion n'est pas terminée : après un
  redémarrage, elle reprend automatiquement.

## Sous-titres

Fichiers .srt (ainsi que .vtt et .ass) envoyés depuis la médiathèque, puis
associés à une vidéo (un fichier par vidéo). Un .srt portant le même nom
qu'une vidéo lui est proposé automatiquement. Les fichiers sont convertis en
UTF-8 à l'envoi (les .srt Windows en cp1252 sont gérés). Taille et fond sombre
se règlent dans l'interface ; police DejaVu Sans.

## Commandes utiles

    sudo systemctl restart videoplayer videoplayer-web
    journalctl -u videoplayer -f

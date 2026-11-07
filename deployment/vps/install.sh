#!/usr/bin/env bash
# Install or update Minecraft Distributed Hosting on the VPS, one step at a time.
#
#   sudo bash install.sh check              read-only checks: changes nothing
#   sudo bash install.sh app                python3-venv, user "mdh", code, service on 127.0.0.1:8090
#   sudo bash install.sh nginx              self-signed certificate + Nginx site on port 8443
#   sudo bash install.sh firewall           UFW: allow 8443/tcp and the relay port (25565/tcp)
#   sudo bash install.sh address            print the addresses to give to the group
#   sudo bash install.sh create-user NAME   first account (registration is by invitation)
#   sudo bash install.sh reset-password NAME   new password for someone who forgot theirs
#   sudo bash install.sh status             read-only: service, disk, memory, last errors
#   sudo bash install.sh publish-client FILE... offer a new version of the application
#                                              (Windows .exe and/or Mac .zip, each with FILE.json)
#
# Another relay port at the first installation: sudo MDH_RELAY_PORT=25570 bash install.sh app
# (then firewall). Afterwards the port is the one in /etc/mdh/mdh.env.
#
# Updating = unpacking a newer package and running "app" again (database saved first).
# Every step can be undone with rollback.sh. Nothing else is touched: Pi-hole, Unbound,
# WireGuard, the other Nginx sites and the existing UFW rules stay exactly as they are.
set -euo pipefail
export LC_ALL=C

HERE="$(cd "$(dirname "$0")" && pwd)"
VERSION="$(tr -d '[:space:]' < "$HERE/VERSION")"
APP=/opt/mdh
DATA=/var/lib/mdh
CONF=/etc/mdh
UNIT=/etc/systemd/system/mdh.service
SITE=/etc/nginx/sites-available/mdh
SITE_LINK=/etc/nginx/sites-enabled/mdh
API_PORT=8090
HTTPS_PORT=8443
# Relay port: the one of the existing configuration (never rewritten), else MDH_RELAY_PORT for
# a first installation, else 25565. The firewall rule always follows the service.
relay_port() {
    local p from_env="${MDH_RELAY_PORT:-}"
    p="$(sed -n 's/^MDH_RELAY_PORT=//p' "$CONF/mdh.env" 2> /dev/null | tail -n 1)"
    if [ -n "$p" ] && [ -n "$from_env" ] && [ "$p" != "$from_env" ]; then
        printf '   !!  MDH_RELAY_PORT=%s ignoré : %s/mdh.env dit %s (modifie ce fichier pour changer)\n' \
            "$from_env" "$CONF" "$p" >&2
    fi
    p="${p:-${from_env:-25565}}"
    case "$p" in ''|*[!0-9]*) printf 'ERREUR : port du relais invalide : %s\n' "$p" >&2; return 1 ;; esac
    if [ "$p" -lt 1024 ] || [ "$p" -gt 65535 ] || [ "$p" = "$API_PORT" ] || [ "$p" = "$HTTPS_PORT" ]; then
        printf 'ERREUR : port du relais impossible : %s (1024-65535, ni %s ni %s)\n' "$p" "$API_PORT" "$HTTPS_PORT" >&2
        return 1
    fi
    echo "$p"
}
RELAY_PORT="$(relay_port)"

say()  { printf '\n== %s\n' "$*"; }
ok()   { printf '   ok  %s\n' "$*"; }
warn() { printf '   !!  %s\n' "$*"; }
die()  { printf '\nERREUR : %s\n' "$*" >&2; exit 1; }
stamp() { date -u +%Y%m%dT%H%M%S; }

need_root() { [ "$(id -u)" -eq 0 ] || die "lance ce script avec sudo"; }

public_ip() {
    if [ -n "${MDH_PUBLIC_IP:-}" ]; then echo "$MDH_PUBLIC_IP"; return; fi
    ip -4 route get 1.1.1.1 | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}'
}

# Name of the process listening on TCP port $1: nothing if the port is free.
port_owner() {
    local line name
    line="$(ss -Hltnp "sport = :$1" | head -n 1)"
    [ -n "$line" ] || return 0
    name="$(sed -n 's/.*users:(("\([^"]*\)".*/\1/p' <<< "$line")"
    echo "${name:-inconnu}"
}

wait_url() {  # wait_url URL SECONDS [curl options]
    local url="$1" seconds="$2"; shift 2
    for _ in $(seq "$seconds"); do
        curl -fsS --max-time 2 "$@" "$url" > /dev/null 2>&1 && return 0
        sleep 1
    done
    return 1
}

pin() {
    openssl x509 -in "$CONF/tls/cert.pem" -outform DER | openssl dgst -sha256 -binary \
        | openssl base64 -A | tr '+/' '-_' | tr -d '='
}

# --------------------------------------------------------------------------------- check

cmd_check() {
    need_root
    say "Vérifications (aucune modification)"
    local os  # in a subshell: os-release defines its own VERSION
    os="$(. /etc/os-release && echo "$ID $PRETTY_NAME")"
    if [ "${os%% *}" = ubuntu ]; then ok "${os#* }"; else warn "système non testé : ${os#* }"; fi
    python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))' || die "Python 3.12 ou plus récent requis"
    ok "$(python3 --version)"
    compgen -G "$HERE/wheels/mdh_server-$VERSION-*.whl" > /dev/null \
        || die "dossier wheels/ incomplet : décompresse toute l'archive et lance le script depuis son dossier"
    ok "paquet mdh-server $VERSION complet"

    local port owner
    for port in $API_PORT $RELAY_PORT $HTTPS_PORT; do
        owner="$(port_owner "$port")"
        if [ -z "$owner" ]; then
            ok "port $port libre"
        elif [ "$port" != "$HTTPS_PORT" ] && systemctl is-active --quiet mdh; then
            ok "port $port : déjà utilisé par le service mdh (mise à jour)"
        elif [ "$port" = "$HTTPS_PORT" ] && [ -e "$SITE_LINK" ]; then
            ok "port $port : déjà le site Nginx mdh (mise à jour)"
        else
            die "le port $port est déjà utilisé par « $owner » : rien n'a été modifié"
        fi
    done

    systemctl is-active --quiet nginx || die "Nginx n'est pas actif"
    nginx -t > /dev/null 2>&1 || die "« nginx -t » échoue déjà, avant toute modification : à corriger d'abord"
    ok "Nginx actif, configuration actuelle valide"
    if ufw status | grep -q '^Status: active'; then ok "UFW actif"; else warn "UFW inactif (ce script ne l'active pas)"; fi
    local avail
    avail="$(awk '/^MemAvailable/ {print int($2 / 1024)}' /proc/meminfo)"
    if [ "$avail" -ge 300 ]; then ok "mémoire disponible : $avail Mo"; else warn "seulement $avail Mo de mémoire disponible"; fi
    if dpkg -s python3-venv > /dev/null 2>&1; then ok "python3-venv installé"; else ok "python3-venv : sera installé à l'étape app"; fi
    echo
    echo "Adresse publique détectée : $(public_ip)"
    echo "(si elle est fausse : sudo MDH_PUBLIC_IP=a.b.c.d bash install.sh ...)"
}

# ----------------------------------------------------------------------------------- app

cmd_app() {
    cmd_check
    say "Étape app : service mdh $VERSION"
    if ! dpkg -s python3-venv > /dev/null 2>&1; then
        # NEEDRESTART_MODE=l: list services to restart, never restart them (Pi-hole, WireGuard...).
        DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l apt-get install -y python3-venv > /dev/null || die "installation de python3-venv impossible (essaie : sudo apt update)"
        ok "python3-venv installé"
    fi
    if ! id mdh > /dev/null 2>&1; then
        useradd --system --home-dir "$DATA" --no-create-home --shell /usr/sbin/nologin mdh
        ok "utilisateur système mdh créé (sans connexion possible)"
    fi
    install -d -o root -g root -m 755 "$APP" "$APP/releases"
    install -d -o mdh -g mdh -m 750 "$DATA" "$DATA/database" "$DATA/storage" "$DATA/backups"
    install -d -o root -g mdh -m 750 "$CONF"
    if [ -f "$CONF/mdh.env" ]; then
        ok "configuration existante conservée : $CONF/mdh.env"
    else
        sed -e "s/@PUBLIC_IP@/$(public_ip)/g" -e "s/@API_PORT@/$API_PORT/g" -e "s/@RELAY_PORT@/$RELAY_PORT/g" \
            "$HERE/mdh.env.template" > "$CONF/mdh.env"
        chown root:mdh "$CONF/mdh.env"
        chmod 640 "$CONF/mdh.env"
        ok "configuration écrite : $CONF/mdh.env"
    fi

    # A new folder per release: the running one is never modified, going back is a link switch.
    local rel previous=""
    rel="$APP/releases/$VERSION-$(stamp)"
    python3 -m venv "$rel"
    "$rel/bin/pip" install --quiet --no-index --no-cache-dir --find-links "$HERE/wheels" "mdh-server==$VERSION"
    "$rel/bin/python" -c "import mdh_server, uvicorn, argon2, zstandard"
    ok "code installé : $rel"

    if [ -L "$APP/current" ]; then
        previous="$(readlink -f "$APP/current")"
        systemctl stop mdh 2> /dev/null || true
    fi
    if [ -f "$DATA/database/mdh.sqlite3" ]; then
        # The new version may migrate the schema: keep a consistent copy to come back to.
        local backup
        backup="$DATA/backups/mdh-$(stamp).sqlite3"
        (cd "$DATA" && runuser -u mdh -- "$rel/bin/python" -c '
import sqlite3, sys
src, dst = sqlite3.connect(sys.argv[1]), sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close()
src.close()' "$DATA/database/mdh.sqlite3" "$backup")
        ok "base de données sauvegardée : $backup"
    fi
    if [ -n "$previous" ] && [ -n "${backup:-}" ]; then
        echo "$previous $backup" > "$APP/previous"  # read by rollback.sh release
    else
        rm -f "$APP/previous"
    fi

    ln -sfn "$rel" "$APP/current"
    install -m 644 "$HERE/mdh.service" "$UNIT"
    systemctl daemon-reload
    systemctl enable mdh > /dev/null 2>&1
    systemctl restart mdh
    if ! wait_url "http://127.0.0.1:$API_PORT/api/v1/health" 30; then
        journalctl -u mdh -n 30 --no-pager || true
        die "le service ne répond pas (journal ci-dessus). Retour arrière : sudo bash rollback.sh $([ -n "$previous" ] && echo release || echo app)"
    fi
    ok "service mdh actif ; API sur 127.0.0.1:$API_PORT (non exposée)"
    ok "relais Minecraft sur le port $RELAY_PORT (fermé par UFW tant que l'étape firewall n'est pas faite)"

    # Keep the three newest releases, plus the current and the previous one.
    local old
    while read -r old; do
        if [ -n "$old" ] && [ "$old" != "$rel" ] && [ "$old" != "$previous" ]; then rm -rf -- "$old"; fi
    done < <(ls -1dt "$APP"/releases/*/ | sed 's:/$::' | tail -n +4)

    echo
    if [ -n "$previous" ]; then
        echo "Retour à la version précédente : sudo bash rollback.sh release"
    else
        echo "Retour arrière de cette étape : sudo bash rollback.sh app"
    fi
}

# --------------------------------------------------------------------------------- nginx

cmd_nginx() {
    need_root
    { [ -L "$APP/current" ] && systemctl is-active --quiet mdh; } || die "fais d'abord l'étape app"
    say "Étape nginx : HTTPS sur le port $HTTPS_PORT"
    local ip
    ip="$(public_ip)"
    install -d -o root -g root -m 755 "$CONF/tls"
    if [ -f "$CONF/tls/cert.pem" ]; then
        ok "certificat existant conservé (l'adresse donnée au groupe ne change pas)"
    else
        (umask 077 && openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 3650 \
            -subj "/CN=$ip" -addext "subjectAltName=IP:$ip" \
            -keyout "$CONF/tls/key.pem" -out "$CONF/tls/cert.pem" 2> /dev/null)
        chmod 644 "$CONF/tls/cert.pem"
        ok "certificat créé pour $ip (valable 10 ans)"
    fi

    local backup=""
    if [ -f "$SITE" ] && ! cmp -s "$HERE/nginx-mdh.conf" "$SITE"; then
        backup="$SITE.bak-$(stamp)"
        cp -p "$SITE" "$backup"
    fi
    install -m 644 "$HERE/nginx-mdh.conf" "$SITE"
    ln -sfn "$SITE" "$SITE_LINK"
    local out
    if ! out="$(nginx -t 2>&1)"; then
        if [ -n "$backup" ]; then mv -f "$backup" "$SITE"; else rm -f "$SITE_LINK" "$SITE"; fi
        echo "$out"
        die "configuration refusée par « nginx -t » : remise dans l'état d'avant, Nginx n'a pas été rechargé"
    fi
    systemctl reload nginx
    wait_url "https://127.0.0.1:$HTTPS_PORT/api/v1/health" 10 --insecure \
        || die "Nginx ne transmet pas à l'API. Retour arrière : sudo bash rollback.sh nginx"
    ok "site Nginx mdh actif (les autres sites et Pi-hole ne sont pas touchés)"
    echo
    echo "Retour arrière de cette étape : sudo bash rollback.sh nginx"
    cmd_address
}

# ------------------------------------------------------------------------------ firewall

cmd_firewall() {
    need_root
    say "Étape firewall : ouvrir $HTTPS_PORT/tcp et $RELAY_PORT/tcp"
    ufw status | grep -q '^Status: active' || warn "UFW est inactif : les règles sont ajoutées mais il n'est pas activé"
    ufw allow "$HTTPS_PORT/tcp" comment 'mdh control plane (nginx https)'
    ufw allow "$RELAY_PORT/tcp" comment 'mdh minecraft relay'
    ok "règles ajoutées ; les autres règles sont inchangées"
    echo
    echo "À faire aussi : ouvrir les ports TCP $HTTPS_PORT et $RELAY_PORT dans le pare-feu du panneau"
    echo "de l'hébergeur (IONOS : Réseau > Stratégies de pare-feu), sinon rien n'arrivera au VPS."
    echo "Retour arrière de cette étape : sudo bash rollback.sh firewall"
}

# ------------------------------------------------------------------------------- address

cmd_address() {
    need_root
    [ -f "$CONF/tls/cert.pem" ] || die "pas encore de certificat : fais d'abord l'étape nginx"
    local domain
    domain="$(sed -n 's/^MDH_RELAY_DOMAIN=//p' "$CONF/mdh.env")"
    echo
    echo "Adresse du serveur central, à coller dans l'application (champ « Serveur central ») :"
    echo
    echo "    https://$(public_ip):$HTTPS_PORT#$(pin)"
    echo
    echo "La partie après # est l'empreinte du certificat : l'application refuse tout autre serveur."
    echo "Adresse Minecraft (un seul serveur) : $domain"
}

# --------------------------------------------------------------------------- create-user

cmd_create_user() {
    run_admin_command create-user "${1:-}"
}

run_admin_command() {  # run_admin_command create-user|reset-password NAME
    need_root
    local name="${2:-}"
    [ -n "$name" ] || die "usage : sudo bash install.sh $1 NOM"
    [ -L "$APP/current" ] || die "fais d'abord l'étape app"
    set -a
    # shellcheck disable=SC1091
    . "$CONF/mdh.env"
    set +a
    cd "$DATA"
    runuser -u mdh -- "$APP/current/bin/python" -m mdh_server "$1" "$name"
}

# -------------------------------------------------------------------------------- status

cmd_status() {  # read-only: what to look at when the application says the VPS does not answer
    need_root
    say "État du serveur central (aucune modification)"
    if systemctl is-active --quiet mdh; then ok "service mdh actif"; else warn "service mdh ARRÊTÉ (relancer : sudo systemctl restart mdh)"; fi
    if curl -fsS --max-time 3 "http://127.0.0.1:$API_PORT/api/v1/health"; then echo; ok "API joignable"; else warn "API muette"; fi
    if systemctl is-active --quiet nginx; then ok "Nginx actif"; else warn "Nginx arrêté"; fi
    local mem
    mem="$(systemctl show -p MemoryCurrent --value mdh 2> /dev/null || true)"
    case "$mem" in ''|*[!0-9]*) ;; *) ok "mémoire du service : $((mem / 1024 / 1024)) Mo (limite 384 Mo)" ;; esac
    ok "disque : $(df -h --output=avail,size "$DATA" | tail -n 1 | awk '{print $1 " libres sur " $2}')"
    ok "mémoire disponible du VPS : $(awk '/^MemAvailable/ {print int($2 / 1024)}' /proc/meminfo) Mo"
    ok "démarrages du service : $(systemctl show -p NRestarts --value mdh 2> /dev/null || echo ?) redémarrage(s) automatique(s) depuis le boot"
    echo
    echo "Derniers avertissements et erreurs du service :"
    journalctl -u mdh -p warning -n 15 --no-pager 2> /dev/null || true
}

# ------------------------------------------------------------------------ publish-client

release_version() {  # version written in a release.json (one key per line, as build.ps1 writes it)
    sed -n 's/^ *"version": *"\([0-9.]*\)".*/\1/p' "$1"
}

cmd_publish_client() {  # one or several files: the Windows installer and the Mac .zip of a version
    need_root
    [ $# -gt 0 ] || die "usage : sudo bash install.sh publish-client MinecraftDistributedHosting-Setup-X.Y.Z.exe MinecraftDistributedHosting-X.Y.Z-macos-arm64.zip"
    local f
    for f in "$@"; do [ -f "$f" ] && [ -f "$f.json" ] || die "fichier ou signature introuvable : $f (rien n'a été publié)"; done
    for f in "$@"; do publish_one "$f"; done
}

publish_one() {
    local exe="$1"
    [ -f "$exe" ] || die "fichier introuvable : $exe"
    [ -f "$exe.json" ] || die "signature introuvable : $exe.json (créée par sign_release.py à côté du fichier)"
    [ -L "$APP/current" ] || die "fais d'abord l'étape app"
    # Windows at the root of the updates folder (as before 0.9.0), macOS in its own subfolder.
    local platform dir prefix suffix label
    case "$(basename "$exe")" in
        MinecraftDistributedHosting-Setup-*.exe)
            platform=windows; dir="$DATA/updates"; prefix="MinecraftDistributedHosting-Setup-"; suffix=".exe"
            label="Windows" ;;
        MinecraftDistributedHosting-*-macos-arm64.zip)
            platform=macos-arm64; dir="$DATA/updates/macos-arm64"; prefix="MinecraftDistributedHosting-"
            suffix="-macos-arm64.zip"; label="Mac" ;;
        *) die "nom de fichier inattendu : $(basename "$exe")" ;;
    esac
    say "Publication d'une nouvelle version de l'application $label"
    # Same checks as the service. The signature itself is checked by each computer with the
    # developer's public key: the VPS has no key and cannot sign anything.
    local version
    version="$("$APP/current/bin/python" - "$exe" "$exe.json" "$platform" <<'PY'
import hashlib, sys
from pathlib import Path
from mdh_shared.models.api import ClientRelease
exe, manifest, platform = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
release = ClientRelease.model_validate_json(manifest.read_bytes())
if (release.platform or "windows") != platform:
    sys.exit(f"la signature décrit une version {release.platform or 'windows'}, pas {platform}")
digest = hashlib.sha256()
with exe.open("rb") as fh:
    while chunk := fh.read(1 << 20):
        digest.update(chunk)
if exe.stat().st_size != release.size or digest.hexdigest() != release.sha256:
    sys.exit("le fichier ne correspond pas à sa signature (copie incomplète ?)")
print(release.version)
PY
)" || die "publication refusée, rien n'a été modifié"

    local previous=""
    install -d -o root -g mdh -m 750 "$DATA/updates" "$dir"  # readable by the service, not writable
    install -o root -g mdh -m 640 "$exe" "$dir/$prefix$version$suffix"
    if [ -f "$dir/release.json" ]; then
        previous="$(release_version "$dir/release.json")"
        if [ "$previous" != "$version" ]; then cp -p "$dir/release.json" "$dir/release.json.previous"; fi
    fi
    install -o root -g mdh -m 640 "$exe.json" "$dir/release.json.new"
    mv -f "$dir/release.json.new" "$dir/release.json"
    # Keep the files of this release and of the one before (for rollback.sh client).
    local keep="$version" f
    if [ -f "$dir/release.json.previous" ]; then keep="$(release_version "$dir/release.json.previous")"; fi
    for f in "$dir/$prefix"*"$suffix"; do
        [ -e "$f" ] || continue
        case "$(basename "$f")" in
            "$prefix$version$suffix" | "$prefix$keep$suffix") ;;
            *) rm -f -- "$f" ;;
        esac
    done
    ok "version $label $version publiée${previous:+ (remplace la $previous)}"
    echo
    echo "Chaque ordinateur $label du groupe la télécharge, vérifie la signature et l'installe tout"
    echo "seul dès qu'il n'héberge pas le serveur. Retirer cette publication :"
    if [ "$platform" = windows ]; then echo "   sudo bash rollback.sh client"; else echo "   sudo bash rollback.sh client mac"; fi
}

case "${1:-}" in
    check) cmd_check ;;
    app) cmd_app ;;
    nginx) cmd_nginx ;;
    firewall) cmd_firewall ;;
    address) cmd_address ;;
    create-user) shift; cmd_create_user "$@" ;;
    publish-client) shift; cmd_publish_client "$@" ;;
    reset-password) shift; run_admin_command reset-password "${1:-}" ;;
    status) cmd_status ;;
    *) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac

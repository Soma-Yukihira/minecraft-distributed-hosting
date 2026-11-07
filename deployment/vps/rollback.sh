#!/usr/bin/env bash
# Undo install.sh, one step at a time (reverse order of installation) or all at once.
#
#   sudo bash rollback.sh firewall        remove the two UFW rules added by install.sh
#   sudo bash rollback.sh nginx           remove the Nginx site (certificate kept: same address later)
#   sudo bash rollback.sh app             stop and remove the service and its code
#   sudo bash rollback.sh release         go back to the previous version and its database
#   sudo bash rollback.sh client [mac]    withdraw the last published version of the Windows (or Mac) app
#   sudo bash rollback.sh all             firewall + nginx + app
#   sudo bash rollback.sh all --purge     ... and also delete worlds, accounts, configuration,
#                                         certificate and the "mdh" user (asks for confirmation)
#
# Without --purge, /var/lib/mdh (worlds, accounts) and /etc/mdh (configuration, certificate)
# are kept: reinstalling gives back the same server at the same address.
# Only what install.sh created is removed; Pi-hole, Unbound, WireGuard, the other Nginx sites
# and the other UFW rules are never touched.
set -euo pipefail
export LC_ALL=C

APP=/opt/mdh
DATA=/var/lib/mdh
CONF=/etc/mdh
UNIT=/etc/systemd/system/mdh.service
SITE=/etc/nginx/sites-available/mdh
SITE_LINK=/etc/nginx/sites-enabled/mdh
API_PORT=8090
HTTPS_PORT=8443
RELAY_PORT=25565

say()  { printf '\n== %s\n' "$*"; }
ok()   { printf '   ok  %s\n' "$*"; }
die()  { printf '\nERREUR : %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "lance ce script avec sudo"

undo_firewall() {
    say "Retrait des règles UFW de mdh"
    local port
    for port in $HTTPS_PORT $RELAY_PORT; do
        if ufw status | grep -qE "^$port/tcp( \(v6\))? +ALLOW"; then
            ufw delete allow "$port/tcp" > /dev/null
            ok "règle $port/tcp retirée"
        else
            ok "pas de règle $port/tcp"
        fi
    done
    echo "   Pense à refermer aussi ces ports dans le pare-feu du panneau de l'hébergeur."
}

undo_nginx() {
    say "Retrait du site Nginx mdh"
    if [ ! -e "$SITE_LINK" ] && [ ! -e "$SITE" ]; then
        ok "pas de site mdh"
        return
    fi
    rm -f "$SITE_LINK" "$SITE"
    local out
    if out="$(nginx -t 2>&1)"; then
        systemctl reload nginx
        ok "site retiré, Nginx rechargé (autres sites inchangés)"
    else
        echo "$out"
        die "« nginx -t » échoue sans le site mdh : un autre site est en cause, Nginx n'a pas été rechargé"
    fi
}

undo_app() {
    say "Arrêt et retrait du service mdh"
    if [ -f "$UNIT" ]; then
        systemctl disable --now mdh > /dev/null 2>&1 || true
        rm -f "$UNIT"
        systemctl daemon-reload
        ok "service arrêté et retiré"
    else
        ok "pas de service mdh"
    fi
    if [ -d "$APP" ]; then
        rm -rf -- "$APP"
        ok "code retiré ($APP)"
    fi
    ok "données conservées : $DATA ; configuration et certificat conservés : $CONF"
}

undo_release() {
    say "Retour à la version précédente"
    [ -f "$APP/previous" ] || die "aucune mise à jour à annuler"
    local previous backup db="$DATA/database/mdh.sqlite3"
    read -r previous backup < "$APP/previous"
    [ -d "$previous" ] || die "version précédente introuvable : $previous"
    [ -f "$backup" ] || die "sauvegarde de la base introuvable : $backup"
    systemctl stop mdh
    # The current database is kept aside, never deleted.
    local aside
    aside="$DATA/backups/mdh-before-rollback-$(date -u +%Y%m%dT%H%M%S).sqlite3"
    mv "$db" "$aside"
    rm -f "$db-wal" "$db-shm"
    install -o mdh -g mdh -m 640 "$backup" "$db"
    ln -sfn "$previous" "$APP/current"
    rm -f "$APP/previous"
    systemctl start mdh
    local _
    for _ in $(seq 30); do
        if curl -fsS --max-time 2 "http://127.0.0.1:$API_PORT/api/v1/health" > /dev/null 2>&1; then
            ok "version $(basename "$previous") active, base restaurée ($aside mise de côté)"
            return
        fi
        sleep 1
    done
    journalctl -u mdh -n 30 --no-pager || true
    die "le service ne répond pas après le retour arrière (journal ci-dessus)"
}

undo_client() {
    local dir="$DATA/updates" label="Windows"
    if [ "${1:-}" = mac ]; then dir="$DATA/updates/macos-arm64"; label="Mac"; fi
    say "Retrait de la dernière version publiée de l'application $label"
    if [ ! -f "$dir/release.json" ]; then
        ok "aucune version publiée"
        return
    fi
    if [ -f "$dir/release.json.previous" ]; then
        mv -f "$dir/release.json.previous" "$dir/release.json"
        ok "version précédente de nouveau proposée : $(sed -n 's/^ *"version": *"\([0-9.]*\)".*/\1/p' "$dir/release.json")"
    else
        rm -f "$dir/release.json"
        ok "plus aucune mise à jour proposée"
    fi
    echo "   Les PC déjà mis à jour gardent leur version (jamais de retour en arrière automatique) :"
    echo "   pour corriger un problème, publie une version plus récente."
}

purge() {
    say "Suppression définitive des données"
    echo "Ceci supprime TOUS les mondes, comptes et sauvegardes stockés sur le VPS ($DATA),"
    echo "la configuration et le certificat ($CONF) : les membres devront recevoir une nouvelle adresse."
    local answer
    read -r -p "Tape SUPPRIMER pour confirmer : " answer
    [ "$answer" = SUPPRIMER ] || die "annulé, rien n'a été supprimé"
    rm -rf -- "$DATA" "$CONF"
    if id mdh > /dev/null 2>&1; then userdel mdh; fi
    ok "données, configuration et utilisateur mdh supprimés"
    echo "   python3-venv reste installé (inoffensif) ; pour le retirer : sudo apt remove python3-venv"
}

case "${1:-}" in
    firewall) undo_firewall ;;
    nginx) undo_nginx ;;
    app) undo_app ;;
    release) undo_release ;;
    client) undo_client "${2:-}" ;;
    all)
        undo_firewall
        undo_nginx
        undo_app
        if [ "${2:-}" = --purge ]; then purge; fi
        ;;
    *) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac

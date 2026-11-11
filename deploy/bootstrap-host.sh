#!/usr/bin/env bash
# bootstrap-host.sh - prepare a fresh Debian (13 "trixie") VPS for dockerised apps behind one Caddy.
# Idempotent: safe to re-run; every step says whether it changed something. Run as root:
#
#   bash bootstrap-host.sh                 1) base setup (everything below except SSH hardening)
#   bash bootstrap-host.sh --harden-ssh    2) only after `ssh deploy@<vps>` with a key has worked
#
# Step 1:
#   - packages: ca-certificates curl git jq nftables unattended-upgrades sqlite3
#   - time zone Europe/Prague
#   - Docker Engine + compose plugin from Docker's apt repository (download.docker.com)
#   - /etc/docker/daemon.json: json-file logs with rotation (10 MB x 5 per container) and
#     "ip": "127.0.0.1" = a port published without an explicit address is reachable from
#     localhost only (only the proxy publishes 80/443 on purpose, with explicit addresses)
#   - journald capped at 200 MB
#   - Docker network "proxy" (10.250.0.0/24; Caddy fixed at .2, apps get .128-.255) shared by Caddy and the apps
#   - user "deploy" (group docker, passwordless sudo) with root's SSH authorized_keys (or DEPLOY_PUBKEY)
#   - /srv/proxy and /srv/lkhc-odecty layout
#   - unattended upgrades of everything (Debian main/updates/security/backports + Docker repo),
#     automatic reboot at 04:45 when a package requires it
#   - nftables host firewall, INPUT only: SSH, 80, 443 (+443/udp), ICMP; everything else dropped.
#     It never touches the forward path / Docker's own tables (no `flush ruleset`).
# Step 2 (--harden-ssh): refuses unless the journal shows a successful public-key login of the
# deploy user (or --force) and `sudo` works for it, then: no password / keyboard-interactive login,
# root only with a key (ROOT_LOGIN=no disables root SSH login completely).
#
# Environment: DEPLOY_USER [deploy], DEPLOY_PUBKEY [""; a public key line to authorize],
# TIMEZONE [Europe/Prague], PROXY_SUBNET [10.250.0.0/24], PROXY_IP_RANGE [10.250.0.128/25], APPS ["lkhc-odecty"],
# ROOT_LOGIN [prohibit-password | no] (only for --harden-ssh).
set -Eeuo pipefail

DEPLOY_USER="${DEPLOY_USER:-deploy}"
DEPLOY_PUBKEY="${DEPLOY_PUBKEY:-}"
TIMEZONE="${TIMEZONE:-Europe/Prague}"
PROXY_SUBNET="${PROXY_SUBNET:-10.250.0.0/24}"
PROXY_IP_RANGE="${PROXY_IP_RANGE:-10.250.0.128/25}"
APPS="${APPS:-lkhc-odecty}"
ROOT_LOGIN="${ROOT_LOGIN:-prohibit-password}"

step() { printf '\n==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}
have_systemd() { [[ -d /run/systemd/system ]]; }

# write_file PATH MODE  (content on stdin) - writes only when different; keeps a .orig of the first version
write_file() {
    local path=$1 mode=$2 tmp
    tmp=$(mktemp)
    cat >"$tmp"
    if [[ -f $path ]] && cmp -s "$tmp" "$path"; then
        info "unchanged: $path"
        rm -f "$tmp"
        return 1
    fi
    if [[ -f $path && ! -f $path.orig ]]; then cp -p "$path" "$path.orig"; fi
    install -D -m "$mode" "$tmp" "$path"
    rm -f "$tmp"
    info "written:   $path"
    return 0
}

base_packages() {
    step "Base packages"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends \
        ca-certificates curl gnupg git jq nftables unattended-upgrades sqlite3 util-linux sudo >/dev/null
    info "ok"
}

timezone() {
    step "Time zone $TIMEZONE"
    [[ -f /usr/share/zoneinfo/$TIMEZONE ]] || die "unknown time zone $TIMEZONE"
    if [[ $(readlink -f /etc/localtime) == "/usr/share/zoneinfo/$TIMEZONE" ]]; then
        info "unchanged"
    elif have_systemd && timedatectl set-timezone "$TIMEZONE" 2>/dev/null; then
        info "set with timedatectl"
    else
        ln -sf "/usr/share/zoneinfo/$TIMEZONE" /etc/localtime
        echo "$TIMEZONE" >/etc/timezone
        info "set /etc/localtime"
    fi
    info "now: $(date)"
}

docker_engine() {
    step "Docker Engine + compose plugin (download.docker.com)"
    local codename pkg
    codename=$(. /etc/os-release && echo "$VERSION_CODENAME")
    for pkg in docker.io docker-doc docker-compose podman-docker containerd runc; do
        if dpkg -s "$pkg" >/dev/null 2>&1; then
            info "removing conflicting distro package $pkg"
            apt-get remove -y -qq "$pkg" >/dev/null
        fi
    done
    install -m 0755 -d /etc/apt/keyrings
    if [[ ! -s /etc/apt/keyrings/docker.asc ]]; then
        curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
        chmod a+r /etc/apt/keyrings/docker.asc
        info "added Docker's signing key"
    fi
    write_file /etc/apt/sources.list.d/docker.sources 0644 <<EOF || true
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: $codename
Components: stable
Signed-By: /etc/apt/keyrings/docker.asc
EOF
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
    info "$(docker --version); $(docker compose version)"

    local changed=0
    write_file /etc/docker/daemon.json 0644 <<'EOF' && changed=1
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "5" },
  "ip": "127.0.0.1"
}
EOF
    if have_systemd; then
        systemctl enable --quiet docker.service containerd.service
        if ((changed)) && systemctl is-active --quiet docker; then
            info "restarting Docker (daemon.json changed; log limits apply to containers created from now on)"
            systemctl restart docker
        else
            systemctl start docker
        fi
    fi
}

journald_limit() {
    step "journald size limit"
    if write_file /etc/systemd/journald.conf.d/50-size.conf 0644 <<'EOF'; then
[Journal]
SystemMaxUse=200M
EOF
        if have_systemd; then systemctl restart systemd-journald; fi
    fi
}

proxy_network() {
    step "Docker network 'proxy' ($PROXY_SUBNET, dynamic addresses only from $PROXY_IP_RANGE)"
    if ! docker info >/dev/null 2>&1; then
        info "Docker is not running - skipped"
        return 0
    fi
    # Caddy has a fixed address (.2) that apps trust for X-Forwarded-*. Dynamic addresses must never
    # hand it out: after a reboot Docker may start an app before Caddy (seen on the real VPS).
    local create=(docker network create --driver bridge --subnet "$PROXY_SUBNET" --ip-range "$PROXY_IP_RANGE" proxy)
    if docker network inspect proxy >/dev/null 2>&1; then
        local cfg attached
        cfg=$(docker network inspect -f '{{range .IPAM.Config}}{{.Subnet}} {{.IPRange}}{{end}}' proxy)
        if [[ $cfg == "$PROXY_SUBNET $PROXY_IP_RANGE" ]]; then
            info "exists: $cfg"
            return 0
        fi
        attached=$(docker network inspect -f '{{len .Containers}}' proxy)
        if ((attached == 0)); then
            docker network rm proxy >/dev/null
            "${create[@]}" >/dev/null
            info "re-created with the dynamic range (was: $cfg)"
        else
            info "WARNING: 'proxy' is '$cfg' and has $attached container(s). Re-create it during a short stop:"
            info "  (cd /srv/lkhc-odecty && docker compose down) && (cd /srv/proxy && docker compose down)"
            info "  docker network rm proxy && ${create[*]}"
            info "  (cd /srv/proxy && docker compose up -d) && /srv/lkhc-odecty/deploy.sh --restart"
        fi
    else
        "${create[@]}" >/dev/null
        info "created"
    fi
}

deploy_user() {
    step "User '$DEPLOY_USER' (docker group, SSH key)"
    getent group docker >/dev/null || groupadd --system docker
    if id "$DEPLOY_USER" >/dev/null 2>&1; then
        info "exists"
    else
        useradd --create-home --shell /bin/bash "$DEPLOY_USER"
        info "created (no password: log in with an SSH key)"
    fi
    if id -nG "$DEPLOY_USER" | tr ' ' '\n' | grep -qx docker; then
        info "already in group docker"
    else
        usermod -aG docker "$DEPLOY_USER"
        info "added to group docker (NOTE: docker access is root-equivalent)"
    fi
    # The user has no password (key-only SSH), so sudo is passwordless. This adds nothing beyond what
    # the docker group already grants (docker = root), but makes admin work possible without root SSH.
    write_file "/etc/sudoers.d/90-$DEPLOY_USER" 0440 <<EOF || true
$DEPLOY_USER ALL=(ALL:ALL) NOPASSWD: ALL
EOF
    visudo -cf "/etc/sudoers.d/90-$DEPLOY_USER" >/dev/null || die "sudoers syntax check failed"
    local home ak
    home=$(getent passwd "$DEPLOY_USER" | cut -d: -f6)
    ak="$home/.ssh/authorized_keys"
    install -d -m 0700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$home/.ssh"
    touch "$ak"
    if [[ -n $DEPLOY_PUBKEY ]] && ! grep -qxF "$DEPLOY_PUBKEY" "$ak"; then
        printf '%s\n' "$DEPLOY_PUBKEY" >>"$ak"
        info "added DEPLOY_PUBKEY"
    fi
    if [[ ! -s $ak && -s /root/.ssh/authorized_keys ]]; then
        cp /root/.ssh/authorized_keys "$ak"
        info "copied root's authorized_keys"
    fi
    chown "$DEPLOY_USER:$DEPLOY_USER" "$ak"
    chmod 0600 "$ak"
    local n
    n=$(grep -cE '^(ssh-|ecdsa-|sk-)' "$ak" || true)
    if ((n == 0)); then
        info "WARNING: $ak has no key. Add one (DEPLOY_PUBKEY=... or ssh-copy-id) before --harden-ssh."
    else
        info "$n key(s) in $ak"
    fi
}

srv_layout() {
    step "/srv layout"
    install -d -m 0755 -o "$DEPLOY_USER" -g "$DEPLOY_USER" /srv/proxy /srv/proxy/caddy /srv/proxy/caddy/sites \
        /srv/proxy/caddy/global.d
    local app
    for app in $APPS; do
        install -d -m 0750 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "/srv/$app"
        install -d -m 0700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "/srv/$app/backup" "/srv/$app/backup/rclone"
    done
    info "$(find /srv -maxdepth 3 -type d | sort | tr '\n' ' ')"
}

auto_upgrades() {
    step "Unattended upgrades: everything (Debian + Docker repo), auto-reboot 04:45 when required"
    write_file /etc/apt/apt.conf.d/20auto-upgrades 0644 <<'EOF' || true
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
    # Debian's 50unattended-upgrades only allows security updates; the lists below are merged with it.
    # Docker's repository has no "codename" field, hence archive=. Backports only ever upgrade packages
    # that were installed from backports. The kernel is the host's (vpsAdminOS): "reboot required"
    # comes from libc/systemd/dbus updates.
    write_file /etc/apt/apt.conf.d/52unattended-upgrades-local 0644 <<'EOF' || true
// Written by bootstrap-host.sh
Unattended-Upgrade::Origins-Pattern {
        "origin=Debian,codename=${distro_codename},label=Debian";
        "origin=Debian,codename=${distro_codename}-updates,label=Debian";
        "origin=Debian,codename=${distro_codename}-security,label=Debian-Security";
        "origin=Debian Backports,codename=${distro_codename}-backports,label=Debian Backports";
        "origin=Docker,archive=${distro_codename},label=Docker CE";
};
Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";
Unattended-Upgrade::Remove-New-Unused-Dependencies "true";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-WithUsers "true";
Unattended-Upgrade::Automatic-Reboot-Time "04:45";
Dpkg::Options {
        "--force-confdef";
        "--force-confold";
};
EOF
    # Night schedule (Europe/Prague): lists ~01:00, upgrade 04:15-04:25 (after the 03:30 backup),
    # reboot at 04:45 only if /var/run/reboot-required exists.
    local changed=0
    write_file /etc/systemd/system/apt-daily.timer.d/50-night.conf 0644 <<'EOF' && changed=1
[Timer]
OnCalendar=
OnCalendar=*-*-* 01:00
RandomizedDelaySec=30min
EOF
    write_file /etc/systemd/system/apt-daily-upgrade.timer.d/50-night.conf 0644 <<'EOF' && changed=1
[Timer]
OnCalendar=
OnCalendar=*-*-* 04:15
RandomizedDelaySec=10min
EOF
    if have_systemd; then
        if ((changed)); then systemctl daemon-reload; fi
        systemctl enable --quiet --now apt-daily.timer apt-daily-upgrade.timer
        systemctl enable --quiet --now unattended-upgrades.service 2>/dev/null || true
        systemctl list-timers apt-daily.timer apt-daily-upgrade.timer --no-pager | sed -n '1,3p' | sed 's/^/    /'
    fi
    info "allowed origins: unattended-upgrade --dry-run --debug 2>&1 | grep -A8 'Allowed origins'"
    info "log: /var/log/unattended-upgrades/ ; reboot pending: ls /var/run/reboot-required"
}

ssh_ports() {
    local ports
    ports=$(sshd -T 2>/dev/null | awk '$1 == "port" {print $2}' | sort -u | paste -sd, -)
    echo "${ports:-22}"
}

firewall() {
    local ports
    ports=$(ssh_ports)
    step "nftables host firewall (input: SSH $ports, 80, 443; Docker's forward rules untouched)"
    local changed=0
    write_file /etc/nftables.conf 0755 <<EOF && changed=1
#!/usr/sbin/nft -f
# Host firewall - INPUT only (written by bootstrap-host.sh).
# Do NOT add "flush ruleset" here: Docker keeps its own tables (NAT for published ports, container
# forwarding). Only this table is replaced on reload.
#
# Traffic to published container ports (IPv4) is DNATed before it reaches this chain and goes
# through FORWARD, i.e. Docker's rules decide - which is why only the proxy publishes ports and the
# Docker daemon binds unspecified port publications to 127.0.0.1 (/etc/docker/daemon.json).

table inet host_fw
delete table inet host_fw

table inet host_fw {
    chain input {
        type filter hook input priority filter; policy drop;

        ct state established,related accept
        ct state invalid drop
        iif "lo" accept
        # containers -> host (e.g. host.docker.internal, docker-proxy hairpin)
        iifname "docker0" accept
        iifname "br-*" accept

        meta l4proto icmp accept
        meta l4proto ipv6-icmp accept

        tcp dport { $ports } accept comment "ssh"
        tcp dport { 80, 443 } accept comment "caddy"
        udp dport 443 accept comment "caddy http/3"

        counter comment "dropped"
    }
}
EOF
    nft -c -f /etc/nftables.conf || die "nftables syntax check failed"
    if have_systemd; then systemctl enable --quiet nftables.service; fi
    if ((changed)) || ! nft list table inet host_fw >/dev/null 2>&1; then
        nft -f /etc/nftables.conf || die "applying nftables rules failed (does this VPS allow nftables?)"
        info "applied; open sessions keep working (established connections are accepted)"
    fi
    info "rules: nft list table inet host_fw   remove: nft delete table inet host_fw"
}

harden_ssh() {
    local force=${1:-} ak
    step "SSH hardening (key-only login)"
    ak="$(getent passwd "$DEPLOY_USER" | cut -d: -f6)/.ssh/authorized_keys"
    [[ -s $ak ]] || die "$ak is empty - add a key first"
    if [[ $force != --force ]]; then
        # (no `grep -q` after a pipe: with pipefail, journalctl would die of SIGPIPE and fail the test)
        if journalctl --no-pager -o cat --since -30d 2>/dev/null |
            grep -F "Accepted publickey for $DEPLOY_USER from" >/dev/null ||
            grep -qsF "Accepted publickey for $DEPLOY_USER from" /var/log/auth.log; then
            info "verified: the journal shows a public-key login of '$DEPLOY_USER'"
        else
            die "no successful 'ssh $DEPLOY_USER@<this host>' with a key found in the journal.
       Log in as $DEPLOY_USER with your key from another terminal first, then re-run.
       (--harden-ssh --force skips this check)"
        fi
    fi
    sudo -u "$DEPLOY_USER" sudo -n true || die "sudo does not work for $DEPLOY_USER - run step 1 again"
    info "verified: sudo works for '$DEPLOY_USER'"
    [[ $ROOT_LOGIN == prohibit-password || $ROOT_LOGIN == no ]] || die "ROOT_LOGIN must be prohibit-password or no"
    if [[ ! -s /root/.ssh/authorized_keys && $ROOT_LOGIN != no ]]; then
        info "NOTE: root has no authorized_keys: after this, root can log in only via the vpsAdmin console."
    fi
    if write_file /etc/ssh/sshd_config.d/00-hardening.conf 0644 <<EOF; then
# Written by bootstrap-host.sh. The first value of a setting wins in sshd, hence "00-".
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin $ROOT_LOGIN
PubkeyAuthentication yes
EOF
        sshd -t || {
            rm -f /etc/ssh/sshd_config.d/00-hardening.conf
            die "sshd config test failed - change reverted"
        }
        if have_systemd; then systemctl reload ssh; fi
        info "sshd reloaded (open sessions stay connected)"
    fi
    sshd -T | grep -E '^(passwordauthentication|kbdinteractiveauthentication|permitrootlogin|pubkeyauthentication) ' |
        sed 's/^/    effective: /'
    info "Test NOW from a new terminal: ssh $DEPLOY_USER@<vps>  (keep this session open until it works)"
    info "Undo: rm /etc/ssh/sshd_config.d/00-hardening.conf && systemctl reload ssh"
}

main() {
    [[ $(id -u) -eq 0 ]] || die "run as root"
    if ! grep -q '^ID=debian' /etc/os-release; then
        printf 'WARNING: not Debian (%s) - the Docker repository URL assumes Debian\n' \
            "$(. /etc/os-release && echo "$PRETTY_NAME")"
    fi
    case "${1:-}" in
    --harden-ssh)
        harden_ssh "${2:-}"
        return 0
        ;;
    "") ;;
    *)
        sed -n '2,/^set -E/p' "$0" | sed '$d; s/^# \{0,1\}//'
        exit 2
        ;;
    esac
    base_packages
    timezone
    docker_engine
    journald_limit
    proxy_network
    deploy_user
    srv_layout
    auto_upgrades
    firewall
    step "Done. Next: from your computer run  ssh $DEPLOY_USER@<vps>  and then (as root)  $0 --harden-ssh"
}

main "$@"

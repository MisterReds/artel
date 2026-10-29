#!/bin/sh
# artel-root: the only thing Artel may do as root. Installed by install.sh with the owner's consent as
# /usr/local/sbin/artel-root (root:root 0755); /etc/sudoers.d/artel lets the installing user run exactly this file.
#
#   artel-root apt-install <package>...   apt-get install of signed-repository packages, names validated, deny list
#   artel-root apparmor-bwrap             AppArmor profile that lets /usr/bin/bwrap create user namespaces
#
# Nothing else: no removals, no sources, no options, no shell. Everything on the command line is data.
set -eu
unset IFS APT_CONFIG LD_PRELOAD LD_LIBRARY_PATH BASH_ENV ENV
LC_ALL=C
export LC_ALL
PATH=/usr/sbin:/usr/bin:/sbin:/bin
CONF=/etc/artel-root.conf
PROFILE=/etc/apparmor.d/artel-bwrap
ABI=/etc/apparmor.d/abi/4.0
BWRAP=/usr/bin/bwrap
CHECK_CONF=1
NEED_ROOT=1
# TESTHOOK-BEGIN (removed from the installed copy)
if [ -n "${ARTEL_ROOT_TEST_BIN:-}" ]; then
  PATH="$ARTEL_ROOT_TEST_BIN:$PATH"; CONF="$ARTEL_ROOT_TEST_CONF"; PROFILE="$ARTEL_ROOT_TEST_PROFILE"
  ABI="$ARTEL_ROOT_TEST_ABI"; BWRAP="$ARTEL_ROOT_TEST_BWRAP"; CHECK_CONF=0; NEED_ROOT=0
fi
# TESTHOOK-END
cd /
umask 022

die() { echo "artel-root: $*" >&2; exit 1; }

# Never installed, whatever the config says: services that open ports, privilege and security tooling, boot/kernel.
builtin_denied() {
  case "$1" in
    openssh-*|*-server|*-daemon|*-dkms|sudo|sudo-*|doas|apparmor|apparmor-*|libapparmor*|polkit*|policykit-*|\
    pam-*|libpam*|login|passwd|shadow|systemd|systemd-*|cron|anacron|at|snapd|grub*|linux-image*|linux-headers*|linux-modules*|\
    docker*|containerd*|podman*|nginx*|apache2*|postgresql*|mysql-*|mariadb-*|redis-*|mongodb*|bind9*|dnsmasq*|\
    samba*|nfs-*|vsftpd|proftpd*|telnetd|inetutils-*|openvpn*|wireguard*|fail2ban|ufw|iptables*|nftables|unattended-upgrades|\
    ca-certificates|apt|apt-*|dpkg|dpkg-*|libc6|libc-bin|coreutils|bash|dash|util-linux) return 0 ;;
  esac
  return 1
}

# /etc/artel-root.conf (root-owned, not group/other writable): lines `deny=<glob>` add to the deny list. Nothing else
# in the file is interpreted, and values are matched as patterns, never executed.
conf_denied() {
  [ -f "$CONF" ] || return 1
  if [ "$CHECK_CONF" = 1 ]; then
    [ "$(stat -c %u "$CONF")" = 0 ] || die "$CONF is not owned by root"
    case "$(stat -c %a "$CONF")" in *[2367]?|*[2367]) die "$CONF is writable by others" ;; esac
  fi
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      deny=*)
        pat=${line#deny=}
        case "$pat" in ''|*[!a-z0-9+.*?-]*) continue ;; esac
        # shellcheck disable=SC2254
        case "$1" in $pat) return 0 ;; esac ;;
    esac
  done < "$CONF"
  return 1
}

valid_name() {
  case "$1" in
    ''|?|*[!a-z0-9+.-]*|[!a-z0-9]*|*-|*.deb) return 1 ;;
  esac
  [ "${#1}" -le 100 ]
}

apt_install() {
  [ $# -ge 1 ] && [ $# -le 20 ] || die "apt-install takes 1 to 20 package names"
  for p in "$@"; do
    valid_name "$p" || die "invalid package name"
    if builtin_denied "$p" || conf_denied "$p"; then die "package denied by policy: $p"; fi
  done
  command -v apt-get >/dev/null 2>&1 || die "apt-get not found"
  [ "$(id -u)" = 0 ] || [ "$NEED_ROOT" = 0 ] || die "must run as root (through sudo)"
  DEBIAN_FRONTEND=noninteractive
  export DEBIAN_FRONTEND
  if ! apt-get install -y --no-install-recommends --no-remove -- "$@"; then
    apt-get update -qq || die "apt-get update failed"
    apt-get install -y --no-install-recommends --no-remove -- "$@" || die "apt-get install failed"
  fi
}

apparmor_bwrap() {
  [ $# -eq 0 ] || die "apparmor-bwrap takes no arguments"
  [ -f "$ABI" ] || { echo "artel-root: AppArmor 4 user-namespace policy not present; nothing to do"; return 0; }
  command -v apparmor_parser >/dev/null 2>&1 || { echo "artel-root: apparmor_parser missing; nothing to do"; return 0; }
  [ -x "$BWRAP" ] || die "$BWRAP is not installed"
  [ "$(id -u)" = 0 ] || [ "$NEED_ROOT" = 0 ] || die "must run as root (through sudo)"
  tmp="$PROFILE.new.$$"
  cat > "$tmp" <<PROF
abi <abi/4.0>,
include <tunables/global>

profile artel-bwrap $BWRAP flags=(unconfined) {
  userns,

  include if exists <local/artel-bwrap>
}
PROF
  mv -f "$tmp" "$PROFILE"
  apparmor_parser -r "$PROFILE"
}

[ $# -ge 1 ] || die "usage: artel-root apt-install <package>... | apparmor-bwrap"
action=$1
shift
case "$action" in
  apt-install) apt_install "$@" ;;
  apparmor-bwrap) apparmor_bwrap "$@" ;;
  *) die "unknown action" ;;
esac

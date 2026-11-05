#!/bin/sh
# artel-root: the only thing Arteliq may do as root. Installed by install.sh with the owner's consent as
# /usr/local/sbin/artel-root (root:root 0755); /etc/sudoers.d/artel lets the installing user run exactly this file.
#
#   artel-root apt-install <package>...   apt-get install of signed-repository packages: names validated, only names
#                                         in the root-owned allowlist (/etc/artel-root.conf `allow=` lines), the deny
#                                         list as defence in depth, and a simulated install checked before the real one
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
CONF_OWNER=0:0
# TESTHOOK-BEGIN (removed from the installed copy)
if [ -n "${ARTEL_ROOT_TEST_BIN:-}" ]; then
  PATH="$ARTEL_ROOT_TEST_BIN:$PATH"; CONF="$ARTEL_ROOT_TEST_CONF"; PROFILE="$ARTEL_ROOT_TEST_PROFILE"
  ABI="$ARTEL_ROOT_TEST_ABI"; BWRAP="$ARTEL_ROOT_TEST_BWRAP"; CHECK_CONF=0; NEED_ROOT=0
  if [ -n "${ARTEL_ROOT_TEST_CONF_OWNER:-}" ]; then CHECK_CONF=1; CONF_OWNER="$ARTEL_ROOT_TEST_CONF_OWNER"; fi
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

# Names that look like daemons. Used on the packages a simulated install would pull in. Whether a package ships a
# systemd service cannot be seen without unpacking it; this name heuristic is the known limitation, and the allowlist
# (a handful of tools written by the installer at the owner's consent) is what actually bounds the risk.
service_like() {
  case "$1" in
    *-server|*-daemon|*-service|*-agent|*-broker|*-relay|*-httpd|*sshd*|*-common-server) return 0 ;;
  esac
  return 1
}

check_conf() {
  [ -f "$CONF" ] || return 1
  if [ "$CHECK_CONF" = 1 ]; then
    # owner uid 0 AND group gid 0, mode exactly 0644 or 0600 (stat does not follow a symlink: a link has mode 777)
    st=$(stat -c %u:%g:%a "$CONF" 2>/dev/null || stat -f %u:%g:%Lp "$CONF") || die "cannot stat $CONF"
    case "$st" in
      "$CONF_OWNER":644|"$CONF_OWNER":600) ;;
      *) die "$CONF must be owned by root:root (uid 0, gid 0) with mode 0644 or 0600" ;;
    esac
  fi
}

# /etc/artel-root.conf (uid 0, gid 0, mode 0644 or 0600). `allow=<name>` lines (exact package names, written by
# the installer from the package map the owner consented to) are the only packages ever installed; no file, no
# allowlist: nothing is installed.
conf_allowed() {
  check_conf || return 1
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      allow=*) [ "${line#allow=}" = "$1" ] && return 0 ;;
    esac
  done < "$CONF"
  return 1
}

# lines `deny=<glob>` add to the deny list. Values are matched as patterns, never executed.
conf_denied() {
  check_conf || return 1
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

# Upper bound for the packages an install would add (the requested ones included). `maxdeps=<n>` lines in the conf
# lower or raise it; several lines: the smallest wins. Default 25.
max_deps() {
  max=25
  best=""
  if check_conf; then
    while IFS= read -r line || [ -n "$line" ]; do
      case "$line" in
        maxdeps=*)
          n=${line#maxdeps=}
          case "$n" in ''|*[!0-9]*) continue ;; esac
          [ "${#n}" -le 4 ] || continue
          if [ "$best" = "" ] || [ "$n" -lt "$best" ]; then best=$n; fi ;;
      esac
    done < "$CONF"
    [ "$best" = "" ] || max=$best
  fi
  echo "$max"
}

# The name must be exactly one real package: `apt-cache show` accepts globs and regular expressions, so its answer
# is read, not trusted: one of its stanzas must carry `Package: <name>` (a virtual or unknown name fails outright).
exact_package() {
  out=$(apt-cache show --no-all-versions -- "$1" 2>/dev/null) || return 1
  printf '%s\n' "$out" | awk -v n="$1" '$1 == "Package:" && $2 == n { found = 1 } END { exit !found }'
}

exact_names() {
  for p in "$@"; do exact_package "$p" || return 1; done
}

# the install is simulated and every package it would add must pass the policy and the cap (dependencies included)
simulate() {
  sim=$(apt-get -s install --no-install-recommends -- "$@" 2>/dev/null)
}

check_simulation() {
  total=0
  for p in $(printf '%s\n' "$sim" | awk '/^Inst /{print $2}'); do
    if builtin_denied "$p" || conf_denied "$p" || service_like "$p"; then die "install would pull in a denied package: $p"; fi
    total=$((total + 1))
  done
  limit=$(max_deps)
  [ "$total" -le "$limit" ] || die "install would add $total packages, more than the cap of $limit"
}

# refresh the lists, then judge again from scratch: the names, the simulated dependency set, its size
refresh_and_check() {
  apt-get update -qq || die "apt-get update failed"
  exact_names "$@" || die "not an exact package name in the repositories: $*"
  simulate "$@" || die "apt-get cannot resolve: $*"
  check_simulation
}

apt_install() {
  [ $# -ge 1 ] && [ $# -le 20 ] || die "apt-install takes 1 to 20 package names"
  for p in "$@"; do
    valid_name "$p" || die "invalid package name"
    if builtin_denied "$p" || conf_denied "$p"; then die "package denied by policy: $p"; fi
    conf_allowed "$p" || die "package not in the allowlist ($CONF): $p"
  done
  command -v apt-get >/dev/null 2>&1 || die "apt-get not found"
  command -v apt-cache >/dev/null 2>&1 || die "apt-cache not found"
  [ "$(id -u)" = 0 ] || [ "$NEED_ROOT" = 0 ] || die "must run as root (through sudo)"
  DEBIAN_FRONTEND=noninteractive
  export DEBIAN_FRONTEND
  if exact_names "$@" && simulate "$@"; then
    check_simulation
  else
    refresh_and_check "$@"      # stale lists: update, then every check again
  fi
  if ! apt-get install -y --no-install-recommends --no-remove -- "$@"; then
    refresh_and_check "$@"      # the lists changed under us: the new simulation is vetted before the second try
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

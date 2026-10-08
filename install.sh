#!/usr/bin/env bash
# Artel installer for Linux and macOS.
#
#   bash install.sh [--dir DIR] [--with-codex] [--with-claude] [--service] [--no-start] [--yes]
#
# Installs only what Artel itself needs (git, Python 3.10+, GitHub access for the private repository), clones Artel
# into ~/.artel/app as a git clone (so the built-in updater works), creates a virtual environment, puts `artel` on
# PATH and starts the dashboard. Safe to run again: it updates in place.
# Agent CLIs (Codex, Claude Code) are NOT installed by default: install them when you need them from the dashboard
# ("Провайдеры" → "Установить") or with `artel providers install codex|claude` — into ~/.artel/tools, no sudo.
# --with-codex / --with-claude do that right away.
# Secrets are never asked for here: GitHub and Codex sign in through their own device-code flows.
set -euo pipefail

REPO="${ARTEL_REPO:-https://github.com/MisterReds/artel.git}"
GH_REPO="MisterReds/artel"
DIR="${ARTEL_DIR:-$HOME/.artel/app}"
VENV="${ARTEL_VENV:-$HOME/.artel/venv}"
BIN="${ARTEL_BIN:-$HOME/.local/bin}"
CODEX=0 CLAUDE=0 SERVICE=0 START=1 YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dir) DIR="$2"; shift ;;
    --with-codex) CODEX=1 ;;
    --no-codex) CODEX=0 ;;  # kept for compatibility: the default now
    --with-claude) CLAUDE=1 ;;
    --service) SERVICE=1 ;;
    --no-start) START=0 ;;
    --yes|-y) YES=1 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then C=$'\033[1;38;2;181;82;43m'; M=$'\033[38;2;110;102;92m'; R=$'\033[0m'; else C=''; M=''; R=''; fi
step() { printf '%s▲%s %s\n' "$C" "$R" "$*"; }
note() { printf '  %s%s%s\n' "$M" "$*" "$R"; }
die() { printf '%s✗%s %s\n' "$C" "$R" "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
confirm() { [ "$YES" = 1 ] && return 0; [ -t 0 ] || return 0; read -r -p "  $1 [Y/n] " a; [ -z "$a" ] || [[ "$a" =~ ^[YyДд] ]]; }

OS="$(uname -s)"
SUDO=""; [ "$(id -u)" = 0 ] || SUDO="sudo"
PM=""
if [ "$OS" = Darwin ]; then PM=brew
elif have apt-get; then PM=apt
elif have dnf; then PM=dnf
elif have pacman; then PM=pacman
elif have zypper; then PM=zypper
fi

pkg_install() {  # pkg_install <generic name>...
  local names=()
  for n in "$@"; do
    case "$PM:$n" in
      apt:python) names+=(python3 python3-venv python3-pip) ;;
      dnf:python|zypper:python) names+=(python3 python3-pip) ;;
      pacman:python) names+=(python python-pip) ;;
      brew:python) names+=(python@3.12) ;;
      pacman:gh) names+=(github-cli) ;;
      *) names+=("$n") ;;
    esac
  done
  case "$PM" in
    brew) brew install "${names[@]}" ;;
    apt) $SUDO apt-get update -qq && $SUDO apt-get install -y -qq "${names[@]}" ;;
    dnf) $SUDO dnf install -y -q "${names[@]}" ;;
    pacman) $SUDO pacman -S --noconfirm --needed "${names[@]}" ;;
    zypper) $SUDO zypper -n install "${names[@]}" ;;
    *) die "no supported package manager; install manually: $*" ;;
  esac
}

python_ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; }
find_python() { for p in python3.13 python3.12 python3.11 python3.10 python3; do have "$p" && python_ok "$p" && { echo "$p"; return; }; done; }

# ---------------------------------------------------------------------------------------------------------------
step "Artel installer — $OS"
if [ "$PM" = brew ] && ! have brew; then
  confirm "Homebrew is needed to install dependencies on macOS. Install it?" || die "install Homebrew: https://brew.sh"
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null || /usr/local/bin/brew shellenv)"
fi

step "Dependencies"
missing=()
have git || missing+=(git)
PY="$(find_python || true)"; [ -n "$PY" ] || missing+=(python)
if [ ${#missing[@]} -gt 0 ]; then
  note "installing: ${missing[*]}"
  pkg_install "${missing[@]}"
fi
PY="$(find_python || true)"; [ -n "$PY" ] || die "Python 3.10+ is required"
note "git $(git --version | awk '{print $3}') · $($PY --version)"
if [ "$PM" = apt ] && ! "$PY" -c 'import venv, ensurepip' 2>/dev/null; then pkg_install python; fi

# GitHub access: the repository is private, and the updater pulls from it later with the same credentials.
step "Access to $GH_REPO"
if [ -d "$REPO" ] || git ls-remote "$REPO" HEAD >/dev/null 2>&1; then
  note "ok"
else
  have gh || { note "installing GitHub CLI for sign-in"; pkg_install gh || die "install GitHub CLI: https://cli.github.com"; }
  if ! gh auth status >/dev/null 2>&1; then
    note "sign in to GitHub (device code: open the link, enter the code)"
    gh auth login --hostname github.com --git-protocol https --web
  fi
  gh auth setup-git
  git ls-remote "$REPO" HEAD >/dev/null 2>&1 || die "no access to $GH_REPO with this GitHub account"
  note "ok"
fi

step "Artel → $DIR"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" pull --ff-only --quiet && note "updated to $(git -C "$DIR" rev-parse --short HEAD)"
else
  mkdir -p "$(dirname "$DIR")"
  git clone --quiet "$REPO" "$DIR"
  note "cloned $(git -C "$DIR" rev-parse --short HEAD)"
fi

step "Python environment → $VENV"
[ -x "$VENV/bin/python" ] || "$PY" -m venv "$VENV"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet -e "$DIR[dev]"  # [dev] = pytest: the updater checks a new version before installing it
mkdir -p "$BIN"
ln -sf "$VENV/bin/artel" "$BIN/artel"
case ":$PATH:" in *":$BIN:"*) ;; *)
  for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
    [ -f "$rc" ] || [ "$rc" = "$HOME/.bashrc" ] || continue
    grep -qs 'artel installer' "$rc" || printf '\nexport PATH="%s:$PATH"  # artel installer\n' "$BIN" >> "$rc"
  done
  export PATH="$BIN:$PATH"
  note "added $BIN to PATH (new terminals pick it up)" ;;
esac

if [ "$CODEX" = 1 ]; then step "Codex CLI (ChatGPT subscription)"; "$VENV/bin/artel" providers install codex; fi
if [ "$CLAUDE" = 1 ]; then step "Claude Code CLI (Claude subscription)"; "$VENV/bin/artel" providers install claude; fi

if [ "$SERVICE" = 1 ]; then
  step "Dashboard as a background service"
  if [ "$OS" = Linux ] && have systemctl; then
    mkdir -p "$HOME/.config/systemd/user"
    cat > "$HOME/.config/systemd/user/artel.service" <<UNIT
[Unit]
Description=Artel dashboard
[Service]
ExecStart=$VENV/bin/artel web
Restart=on-failure
Environment=PATH=$BIN:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
[Install]
WantedBy=default.target
UNIT
    systemctl --user daemon-reload && systemctl --user enable --now artel.service && note "systemd --user: artel.service"
    START=0
  elif [ "$OS" = Darwin ]; then
    plist="$HOME/Library/LaunchAgents/ai.artel.dashboard.plist"
    mkdir -p "$(dirname "$plist")"
    cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>ai.artel.dashboard</string>
  <key>ProgramArguments</key><array><string>$VENV/bin/artel</string><string>web</string></array>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>$BIN:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
</dict></plist>
PLIST
    launchctl unload "$plist" 2>/dev/null || true
    launchctl load "$plist" && note "launchd: ai.artel.dashboard"
    START=0
  else
    note "no systemd/launchd here; start it with: artel web"
  fi
fi

step "Check"
"$VENV/bin/artel" providers || true
printf '\n%s▲ Artel установлен.%s\n' "$C" "$R"
note "дашборд:   artel web  →  http://127.0.0.1:8765"
note "Codex / Claude Code: по необходимости — «Провайдеры» → «Установить», затем вход"
note "обновление: кнопка в дашборде или artel update"
if [ "$START" = 1 ] && [ -t 1 ]; then
  url="http://127.0.0.1:8765"
  (sleep 2; { have xdg-open && xdg-open "$url"; } || { have open && open "$url"; } || true) >/dev/null 2>&1 &
  exec "$VENV/bin/artel" web
fi

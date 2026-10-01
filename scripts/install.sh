#!/usr/bin/env bash
# Omarchy iPhone Connect installer.
#
# Checks the system, shows missing packages (and installs them only if you
# agree), sets up the user service, optionally adds the WirePlumber drop-in,
# enables the bar widget and runs a diagnosis. Safe to run again.

set -uo pipefail

PLUGIN_ID="io.github.badr-emil.iphone-connect"
PLUGIN_DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
CLI="$PLUGIN_DIR/backend/iphone-connect"
UNIT_SRC="$PLUGIN_DIR/systemd/iphone-connect.service"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_DST="$UNIT_DIR/iphone-connect.service"
WP_SRC="$PLUGIN_DIR/config/51-iphone-connect-no-a2dp-sink.conf"
WP_DIR="$HOME/.config/wireplumber/wireplumber.conf.d"
WP_DST="$WP_DIR/51-iphone-connect-no-a2dp-sink.conf"
# What the installer wrote, so uninstall.sh only removes its own drop-in:
# line 1 is the sha256 of the installed file, line 2 the backup it replaced.
WP_RECORD="$HOME/.local/state/iphone-connect/wireplumber-dropin"
EXPECTED_DIR="$HOME/.config/omarchy/plugins/$PLUGIN_ID"

bold() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok() { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$*"; }
ask() { local reply; read -r -p "  $1 [y/N] " reply; [[ $reply =~ ^([yY]|[jJ]) ]]; }
unit_is_ours() { [[ $(readlink -f "$1") == "$(readlink -f "$UNIT_SRC")" ]]; }
dropin_is_ours() {
  [[ -f $WP_DST && -f $WP_RECORD ]] && [[ $(sha256sum <"$WP_DST" | cut -d' ' -f1) == "$(sed -n 1p "$WP_RECORD")" ]]
}

bold "1/7  System"
if [[ ! -f /etc/arch-release ]] && ! grep -qiE 'arch|omarchy' /etc/os-release 2>/dev/null; then
  warn "not an Arch/Omarchy system - package names below may differ"
fi
ok "$(. /etc/os-release; echo "${PRETTY_NAME:-unknown}"), kernel $(uname -r)"
if [[ $PLUGIN_DIR != "$EXPECTED_DIR" ]]; then
  warn "plugin folder is $PLUGIN_DIR"
  warn "expected $EXPECTED_DIR (install with: omarchy plugin add <git-url>)"
fi

bold "2/7  Packages"
# Official Arch packages only (checked with pacman -Si).
packages=(bluez bluez-utils bluez-obex pipewire pipewire-pulse wireplumber python-gobject util-linux)
missing=()
for pkg in "${packages[@]}"; do
  if pacman -Q "$pkg" >/dev/null 2>&1; then
    ok "$pkg $(pacman -Q "$pkg" | cut -d' ' -f2)"
  else
    fail "$pkg missing"
    missing+=("$pkg")
  fi
done
if ((${#missing[@]})); then
  echo
  echo "  Missing packages: ${missing[*]}"
  if ask "Install them now with 'sudo pacman -S --needed ${missing[*]}'?"; then
    sudo pacman -S --needed "${missing[@]}" || { fail "package installation failed"; exit 1; }
  else
    fail "cannot continue without: ${missing[*]}"
    exit 1
  fi
fi

pw_version=$(pipewire --version 2>/dev/null | sed -n 's/.*libpipewire \([0-9.]*\).*/\1/p' | head -1)
if [[ -n $pw_version ]] && [[ $(printf '%s\n1.4.0\n' "$pw_version" | sort -V | head -1) != "1.4.0" ]]; then
  fail "PipeWire $pw_version is too old (1.4+ provides the telephony service)"
  exit 1
fi

bold "3/7  Bluetooth and telephony"
if systemctl is-active --quiet bluetooth; then
  ok "bluetooth.service running"
else
  warn "bluetooth.service is not running"
  if ask "Enable and start it now (sudo systemctl enable --now bluetooth)?"; then
    sudo systemctl enable --now bluetooth || fail "could not start bluetooth"
  fi
fi
if busctl --user status org.pipewire.Telephony >/dev/null 2>&1; then
  ok "PipeWire telephony service (org.pipewire.Telephony) available"
else
  warn "org.pipewire.Telephony not on the session bus yet - restarting WirePlumber usually fixes this"
fi

bold "4/7  Background service"
# A unit of the same name that is not this plugin's is never replaced.
foreign_unit=""
if [[ -e $UNIT_DST || -L $UNIT_DST ]]; then
  unit_is_ours "$UNIT_DST" || foreign_unit=$UNIT_DST
else
  loaded_unit=$(systemctl --user show -p FragmentPath --value iphone-connect.service 2>/dev/null)
  [[ -n $loaded_unit ]] && ! unit_is_ours "$loaded_unit" && foreign_unit=$loaded_unit
fi
if [[ -n $foreign_unit ]]; then
  fail "another iphone-connect.service already exists: $foreign_unit"
  fail "it does not belong to this plugin and was left untouched - remove or rename it, then run this installer again"
elif mkdir -p "$UNIT_DIR" && ln -sf "$UNIT_SRC" "$UNIT_DST" && systemctl --user daemon-reload &&
  systemctl --user enable --now iphone-connect.service >/dev/null 2>&1; then
  systemctl --user restart iphone-connect.service
  ok "iphone-connect.service enabled (logs: journalctl --user -u iphone-connect -f)"
else
  fail "could not start iphone-connect.service"
fi

bold "5/7  Keep music and videos on the iPhone (optional)"
echo "  Without this, the iPhone also uses the PC as a Bluetooth speaker (YouTube, music)."
echo "  File: $WP_DST"
echo "  Effect: removes the a2dp_sink role; calls (HFP) and your own Bluetooth"
echo "  headphones/speakers keep working. Undo: delete the file and restart WirePlumber."
if [[ -f $WP_DST ]] && cmp -s "$WP_SRC" "$WP_DST"; then
  ok "already installed"
elif ask "Install the WirePlumber drop-in and restart WirePlumber (audio pauses ~2 s)?"; then
  mkdir -p "$WP_DIR" "$(dirname "$WP_RECORD")"
  # An older drop-in of ours is simply updated; a file the installer did not
  # write (or that was edited since) is kept as a backup for uninstall.sh.
  backup=""
  if dropin_is_ours; then
    backup=$(sed -n 2p "$WP_RECORD")
  elif [[ -e $WP_DST ]]; then
    backup="$WP_DST.bak.$(date +%Y%m%d%H%M%S)"
    cp -p "$WP_DST" "$backup"
  fi
  cp "$WP_SRC" "$WP_DST"
  printf '%s\n%s\n' "$(sha256sum <"$WP_DST" | cut -d' ' -f1)" "$backup" >"$WP_RECORD"
  systemctl --user restart wireplumber && ok "installed, WirePlumber restarted"
else
  warn "skipped - media from the iPhone will also play on the PC"
fi

bold "6/7  Bar widget"
if omarchy plugin validate "$PLUGIN_DIR" >/dev/null 2>&1; then
  ok "plugin manifest valid"
else
  fail "omarchy plugin validate failed"
fi
# The shell answers plugin commands over IPC and can be briefly busy (for
# example while it reloads plugins), so retry for a few seconds.
enabled=0
for _ in 1 2 3 4 5 6; do
  if omarchy plugin enable "$PLUGIN_ID" >/dev/null 2>&1; then
    enabled=1
    break
  fi
  sleep 2
done
if ((enabled)); then
  ok "iPhone icon enabled in the bar"
else
  warn "could not enable automatically - run: omarchy plugin enable $PLUGIN_ID"
fi

bold "7/7  Diagnosis"
"$CLI" status || true

bold "Next steps"
if "$CLI" devices 2>/dev/null | grep -q paired; then
  echo "  Your iPhone is paired. Click the iPhone icon in the bar to call."
else
  echo "  1. Pair your iPhone:  $CLI pair"
  echo "  2. On the iPhone: Settings > Bluetooth > (i) next to this PC > turn on 'Sync Contacts'"
  echo "  3. Click the iPhone icon in the bar."
fi
echo

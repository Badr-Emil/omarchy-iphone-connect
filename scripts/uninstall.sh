#!/usr/bin/env bash
# Omarchy iPhone Connect uninstaller: undoes what install.sh set up, and only
# that. A service or WirePlumber file of the same name that the installer did
# not create is left untouched.
# The plugin folder itself is removed with: omarchy plugin remove io.github.badr-emil.iphone-connect

set -uo pipefail

PLUGIN_ID="io.github.badr-emil.iphone-connect"
PLUGIN_DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
UNIT_SRC="$PLUGIN_DIR/systemd/iphone-connect.service"
UNIT_DST="$HOME/.config/systemd/user/iphone-connect.service"
WP_SRC="$PLUGIN_DIR/config/51-iphone-connect-no-a2dp-sink.conf"
WP_DST="$HOME/.config/wireplumber/wireplumber.conf.d/51-iphone-connect-no-a2dp-sink.conf"
# Written by install.sh: line 1 is the sha256 of the drop-in it installed,
# line 2 the backup of the file it replaced (if any).
WP_RECORD="$HOME/.local/state/iphone-connect/wireplumber-dropin"
DATA_DIRS=("$HOME/.local/share/iphone-connect" "$HOME/.local/state/iphone-connect" "$HOME/.config/iphone-connect")

ok() { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
ask() { local reply; read -r -p "  $1 [y/N] " reply; [[ $reply =~ ^([yY]|[jJ]) ]]; }
unit_is_ours() { [[ $(readlink -f "$1") == "$(readlink -f "$UNIT_SRC")" ]]; }

echo "Removing Omarchy iPhone Connect"

# The service is ours only if the unit is the symlink install.sh created, which
# points into this plugin folder.
loaded_unit=$(systemctl --user show -p FragmentPath --value iphone-connect.service 2>/dev/null)
if [[ -L $UNIT_DST ]] && unit_is_ours "$UNIT_DST"; then
  # Stop it only if systemd really runs our unit, not one that shadows it.
  if [[ -z $loaded_unit ]] || unit_is_ours "$loaded_unit"; then
    systemctl --user disable --now iphone-connect.service >/dev/null 2>&1
  fi
  rm -f "$UNIT_DST"
  systemctl --user daemon-reload
  ok "background service stopped and removed (call audio settings restored)"
elif [[ -e $UNIT_DST || -L $UNIT_DST ]]; then
  warn "$UNIT_DST was not installed by this plugin - left untouched"
elif [[ -n $loaded_unit ]]; then
  warn "iphone-connect.service ($loaded_unit) was not installed by this plugin - left untouched"
fi

# The drop-in is ours only if install.sh recorded writing it and it is unchanged
# since. 1.0.0 kept no record: there, only a file identical to the shipped one.
if [[ -f $WP_DST ]]; then
  ours=0
  backup=""
  if [[ -f $WP_RECORD ]]; then
    { read -r recorded_hash; read -r backup; } <"$WP_RECORD"
    [[ $(sha256sum <"$WP_DST" | cut -d' ' -f1) == "$recorded_hash" ]] && ours=1
  elif cmp -s "$WP_SRC" "$WP_DST"; then
    ours=1
    backup=$(ls -t "$WP_DST".bak.* 2>/dev/null | head -1)
  fi
  if ((ours)); then
    rm -f "$WP_DST" "$WP_RECORD"
    [[ -n $backup && -f $backup ]] && mv "$backup" "$WP_DST" && ok "restored previous $(basename "$WP_DST")"
    systemctl --user restart wireplumber
    ok "WirePlumber drop-in removed (the PC is a Bluetooth speaker again)"
  else
    warn "$WP_DST was not installed by this plugin or was changed since - left untouched"
  fi
fi

# The shell can be briefly busy after the WirePlumber restart; retry.
for _ in 1 2 3 4 5 6; do
  if omarchy plugin disable "$PLUGIN_ID" >/dev/null 2>&1; then
    ok "bar widget disabled"
    break
  fi
  sleep 2
done

echo
echo "  Local data: contacts cache, call log, settings, ringtone:"
printf '    %s\n' "${DATA_DIRS[@]}"
if ask "Delete this local data?"; then
  rm -rf "${DATA_DIRS[@]}"
  ok "local data deleted"
fi

echo
echo "  The iPhone stays paired. To unpair: bluetoothctl devices, then bluetoothctl remove <address>"
echo "  To delete the plugin folder: omarchy plugin remove $PLUGIN_ID"

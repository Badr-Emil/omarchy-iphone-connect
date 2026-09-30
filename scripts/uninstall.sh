#!/usr/bin/env bash
# Omarchy iPhone Connect uninstaller: undoes everything install.sh set up.
# The plugin folder itself is removed with: omarchy plugin remove io.github.badr-emil.iphone-connect

set -uo pipefail

PLUGIN_ID="io.github.badr-emil.iphone-connect"
WP_DST="$HOME/.config/wireplumber/wireplumber.conf.d/51-iphone-connect-no-a2dp-sink.conf"
DATA_DIRS=("$HOME/.local/share/iphone-connect" "$HOME/.local/state/iphone-connect" "$HOME/.config/iphone-connect")

ok() { printf '  \033[32m✓\033[0m %s\n' "$*"; }
ask() { local reply; read -r -p "  $1 [y/N] " reply; [[ $reply =~ ^([yY]|[jJ]) ]]; }

echo "Removing Omarchy iPhone Connect"

if systemctl --user list-unit-files iphone-connect.service >/dev/null 2>&1; then
  systemctl --user disable --now iphone-connect.service >/dev/null 2>&1
  rm -f "$HOME/.config/systemd/user/iphone-connect.service"
  systemctl --user daemon-reload
  ok "background service stopped and removed (call audio settings restored)"
fi

if [[ -f $WP_DST ]]; then
  rm -f "$WP_DST"
  latest_backup=$(ls -t "$WP_DST".bak.* 2>/dev/null | head -1)
  [[ -n $latest_backup ]] && mv "$latest_backup" "$WP_DST" && ok "restored previous $(basename "$WP_DST")"
  systemctl --user restart wireplumber
  ok "WirePlumber drop-in removed (the PC is a Bluetooth speaker again)"
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

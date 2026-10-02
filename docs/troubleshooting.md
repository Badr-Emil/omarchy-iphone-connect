# Troubleshooting

Start with `backend/iphone-connect status` and `backend/iphone-connect diagnostics`.

| Symptom | Cause / fix |
|---|---|
| Panel shows **Finish setup** | The user service is not running. Run `scripts/install.sh`, check `journalctl --user -u iphone-connect`. |
| `HFP not connected` | `iphone-connect connect`. If it keeps failing, remove and re-pair (`bluetoothctl remove <addr>`, `iphone-connect pair`). |
| `PipeWire telephony service is not running` | `systemctl --user restart wireplumber`. PipeWire must be 1.4+. |
| No caller names / empty contacts | On the iPhone: *Settings → Bluetooth → (i) next to the PC → Sync Contacts*, then `iphone-connect contacts sync`. If the switch is missing, disconnect and reconnect once. |
| `BlueZ OBEX service missing` | `sudo pacman -S bluez-obex` (needed for contacts and recent calls). |
| Recent calls miss the newest calls | The iPhone's PBAP call log can lag behind. Calls the PC saw live are merged in from `~/.local/share/iphone-connect/calllog.json`. |
| Caller can't hear you | Check the panel is not **Muted**. Every call starts unmuted (WirePlumber would otherwise restore the last mute state). |
| Fan noise / echo | Turn on *Noise suppression* in the panel (`iphone-connect noise on`). |
| YouTube/music from the iPhone plays on the PC | Install the WirePlumber drop-in via `scripts/install.sh` (removes the `a2dp_sink` role), then reconnect the iPhone once. |
| Call started on the iPhone plays on the PC | The service sets `RejectSCO` when idle; check it runs. `busctl --user get-property org.pipewire.Telephony /org/pipewire/Telephony/ag1 org.pipewire.Telephony.AudioGatewayTransport1 RejectSCO` should be `true` between calls. |
| `too many client application connections` from pipewire-pulse | Leftover `pactl subscribe` processes from very old versions; fixed since processes end with their parent. `pkill -x pactl` clears them. |
| Notifications tab says the iPhone **refused the data channel** | The iPhone accepts one data channel per computer. Switch Bluetooth off and on in the iPhone's *Settings* (the Control Centre only pauses it) and wait for the reconnect; the service retries by itself. |
| Notifications tab says the iPhone **has not allowed it yet** | iPhone: *Settings → Bluetooth → (i) next to the PC* → share system notifications. The service asks again every 30 s. |
| Notifications are listed but nothing pops up | `iphone-connect mirror` shows whether pop-ups are off or the app is muted. Notifications that were already on the phone at connect time never pop up. |
| No "now playing" row | Nothing has played on the iPhone since it connected, or the app does not report its track. |
| Plugin development: panel acts on stale state | Editing files in the plugin folder hot-reloads it and old instances may keep running. Restart the shell (`omarchy-restart-shell`) and check it came back (`pgrep -x quickshell`). |

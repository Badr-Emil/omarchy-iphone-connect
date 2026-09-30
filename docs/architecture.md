# Architecture

## Decision: PipeWire native HFP + `org.pipewire.Telephony` instead of oFono

| Option | Result |
|---|---|
| **PipeWire native backend + `org.pipewire.Telephony`** | ✅ Already running (PipeWire 1.6.8 / WirePlumber 0.5.17), user session bus, no root, no config change, D-Bus signals for every call state, SCO audio handled in the same process. |
| oFono (+ PipeWire `hfphsp-backend = ofono`) | ❌ Only in AUR, needs a system daemon and a system-bus policy, and it must take over the HF role from PipeWire (WirePlumber config change). More moving parts for the same result. |
| hsphfpd | ❌ Unmaintained. |

Reason in one sentence: since PipeWire 1.4 the native backend implements the
HF role *including* call control on D-Bus, so oFono is no longer needed for a
"phone → PC hands-free" setup.

## Data flow

```
iPhone (Audio Gateway, has SIM)
   │  Bluetooth BR/EDR
   │  RFCOMM: AT commands (ATA, ATD, AT+CHUP, +CLIP, +CIEV)
   │  SCO/eSCO: voice (CVSD 8 kHz / mSBC 16 kHz)
   ▼
BlueZ 5.87 (bluetoothd, system)      ← pairing, trust, connect (org.bluez, system bus)
   ▼
PipeWire libspa-bluez5 (native HFP, HF role, inside WirePlumber)
   ├── org.pipewire.Telephony (session bus) ← call control + signals, RejectSCO
   └── SCO streams while the voice link is open (linked by WirePlumber):
         ├── bluez_input.XX.0   Stream/Output/Audio  phone voice → default sink
         └── bluez_output.XX.1  Stream/Input/Audio   default source → phone
               (optionally through module-echo-cancel, WebRTC noise suppression)
BlueZ obexd (session bus) ← PBAP: contacts (pb) and call history (cch)
   ▼
iphone-connect backend (Python, Gio D-Bus)
   ├── daemon (user service): audio snapshot/unmute/filter, RejectSCO when idle,
   │   PC ringtone, live call log, contact/history sync
   ├── CLI:  status|devices|pair|connect|call|answer|reject|hangup|take|mute|…
   └── `watch`: JSON status line per D-Bus / PipeWire event for the panel
   ▼
Omarchy plugin (QML, runs inside the existing omarchy-shell; no second Quickshell)
```

## Components

- `backend/iphone_connect/phone.py` – number validation/normalisation, masking.
- `backend/iphone_connect/state.py` – call state machine (IDLE → DIALING → ALERTING → ACTIVE → DISCONNECTED, IDLE → INCOMING → ACTIVE/DISCONNECTED).
- `backend/iphone_connect/events.py` – maps D-Bus property/object events to state-machine events.
- `backend/iphone_connect/audio.py` – observes the SCO streams, mute, WebRTC filter, restores default sink/source.
- `backend/iphone_connect/contacts.py` – PBAP contacts and call history, local call log, merge.
- `backend/iphone_connect/daemon.py` – background service (ringtone, audio, RejectSCO, sync, call log).
- `backend/iphone_connect/bluez.py` – BlueZ devices on the system bus (paired iPhone, connect).
- `backend/iphone_connect/telephony.py` – `org.pipewire.Telephony` client (ObjectManager signals, no polling).
- `backend/iphone_connect/cli.py` – command line interface.

## Capabilities (no fake buttons)

The backend reports a capability object; the UI shows only what is true right now:

| Capability | Source |
|---|---|
| `canDial` | an `AudioGateway1` object exists |
| `canAnswer` / `canReject` | a call in state `incoming`/`waiting` exists |
| `canHangup` | any call not `disconnected` |
| `hasCallerId` | `LineIdentification` non-empty |
| `hasWidebandAudio` | transport `Codec` == 2 (mSBC) or higher |
| `hasPhonebook` | contacts were synced via PBAP |
| `canMute` | the SCO capture stream exists |
| `canTakeOver` | a call runs without PC audio (dialed/answered on the iPhone) |
| `hasOperator` / `hasSignal` | always `false` (not exported by PipeWire 1.6.8) |

## Who owns the call audio

`RejectSCO=true` while idle: the PC refuses the phone's voice link, so calls
dialed or answered on the iPhone stay there. `call`, `answer` and `take` set
`RejectSCO=false` for that call (`take` also calls `Activate()`); the service
sets it back when no call is left. Incoming calls ring locally on the PC (a
sound file), not via the phone's in-band ringtone, which would need the voice
link and pull the call to the PC even when answered on the iPhone.

## Security

- Everything runs as the user; only BlueZ (already running) is a system service.
- No changes to `/etc`, no D-Bus policy, no root daemon. The only optional
  system-wide effect is the user-level WirePlumber drop-in (asked for by the installer).
- Pairing uses a BlueZ agent that shows the passkey and requires explicit confirmation.
- Diagnostics mask phone numbers by default.

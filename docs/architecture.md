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
   ├── org.pipewire.Telephony (session bus) ← call control + signals
   └── bluez_card.XX  profile headset-head-unit(-msbc)
         ├── bluez_input.XX   (phone → PC speaker)
         └── bluez_output.XX  (PC mic → phone)
   ▼
iphone-connect backend (Python, Gio D-Bus, user service)
   ├── CLI:  iphone-connect status|devices|connect|call|answer|reject|hangup|…
   └── JSON event stream on stdout (`iphone-connect watch`) for the QML plugin
   ▼
Omarchy plugin (QML, runs inside the existing omarchy-shell; no second Quickshell)
```

## Components

- `backend/iphone_connect/phone.py` – number validation/normalisation, masking.
- `backend/iphone_connect/state.py` – call state machine (IDLE → DIALING → ALERTING → ACTIVE → DISCONNECTED, IDLE → INCOMING → ACTIVE/DISCONNECTED).
- `backend/iphone_connect/events.py` – maps D-Bus property/object events to state-machine events.
- `backend/iphone_connect/audio.py` – snapshot/restore of default sink/source/profile around a call.
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
| `hasPhonebook` | always `false` until PBAP is implemented |
| `hasOperator` / `hasSignal` | always `false` (not exported by PipeWire 1.6.8) |

## Security

- Everything runs as the user; only BlueZ (already running) is a system service.
- No changes to `/etc`, no D-Bus policy, no root daemon.
- Pairing uses a BlueZ agent that shows the passkey and requires explicit confirmation.
- Diagnostics mask phone numbers by default.

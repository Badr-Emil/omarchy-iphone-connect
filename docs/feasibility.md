# Feasibility – Omarchy iPhone Connect

Status: Phase 1/2 analysis, 2026-09-30.

## System (measured)

| Component | Value |
|---|---|
| OS | Omarchy (Arch based), kernel 7.2.5-3-omarchy |
| Compositor | Hyprland 0.56.2 |
| Bluetooth adapter | `hci0`, Intel AX201 (USB 8087:0026, `btusb`), Bluetooth 5.2 (HCI version 0x0b) |
| BlueZ | 5.87 (`bluez`, `bluez-utils`, `bluez-libs`) – `bluetooth.service` active |
| PipeWire | 1.6.8 (`pipewire`, `pipewire-pulse`, `pipewire-alsa`, `pipewire-jack`) – active |
| WirePlumber | 0.5.17 – active |
| oFono | **not installed**; not in official repos, only AUR `ofono 2.19-1` |
| Paired devices | none (iPhone not yet paired) |

## HFP roles currently registered with BlueZ

`bluetoothctl show` lists both HFP UUIDs on the adapter:

- `0000111e` **Handsfree** (HF role – what the PC must be for the iPhone)
- `0000111f` Handsfree Audio Gateway (AG role)

Both are registered by PipeWire's **native HFP/HSP backend** (`libspa-bluez5`),
i.e. PipeWire/WirePlumber already claim HFP. Nothing else (no oFono) registers
HFP profiles, so there is currently **no conflict**.

## Key finding: PipeWire has its own telephony D-Bus service

`libspa-bluez5` in PipeWire 1.6.8 contains `spa/plugins/bluez5/telephony.c`, and
WirePlumber already owns the **session-bus** name `org.pipewire.Telephony`:

```
$ busctl --user list | grep Telephony
org.pipewire.Telephony  1080 wireplumber legon ...
```

It exports (verified from the library's introspection data):

- `/org/pipewire/Telephony` – `org.freedesktop.DBus.ObjectManager`, `org.ofono.Manager`
- `/org/pipewire/Telephony/agN` (one per connected phone):
  - `org.pipewire.Telephony.AudioGateway1`: `Dial(s)`, `HangupAll()`, `SendTones(s)`,
    `SwapCalls`, `HoldAndAnswer`, `ReleaseAndAnswer`, `ReleaseAndSwap`,
    `CreateMultiparty`; properties `Address`, `SpeakerVolume`, `MicrophoneVolume`
  - `org.pipewire.Telephony.AudioGatewayTransport1`: `State`, `Codec`, `RejectSCO`, `Activate()`
  - `org.ofono.VoiceCallManager` (compatibility layer)
- `/org/pipewire/Telephony/agN/callM`:
  - `org.pipewire.Telephony.Call1`: `Answer()`, `Hangup()`; properties
    `LineIdentification`, `IncomingLine`, `Name`, `Multiparty`, `State`
    (`dialing`, `alerting`, `incoming`, `waiting`, `active`, `held`, `disconnected`)
  - `org.ofono.VoiceCall` (compatibility layer)

The HF implementation enables caller ID (`AT+CLIP=1`), reads operator (`AT+COPS?`)
and indicators (`+CIEV`), and supports mSBC (`bluez5.enable-msbc`).

## Conclusion

Telephony over standard **Bluetooth HFP 1.x (HF role)** is feasible **without
oFono and without any root-level configuration change**:

- Call control, caller ID and call-state signals: `org.pipewire.Telephony` (D-Bus, user session)
- SCO audio (CVSD / mSBC): PipeWire's native backend, exposed as a normal
  PipeWire card (`bluez_card.*`, profile `headset-head-unit*`)

### What is *not* possible via standard HFP (said plainly)

- **Operator name and signal strength** are read by PipeWire internally but are
  **not exported** on the D-Bus API in 1.6.8 → the GUI will not show them.
- **Contact names**: HFP only delivers the number (+ sometimes a name via `+CLIP`,
  iPhones usually send none). Names need PBAP (phase 2 of the project).
- **Call history / favourites**: PBAP, phase 2.
- **Mac-style Continuity (Wi-Fi calling relay, Handoff)**: Apple proprietary – not attempted.
- Battery level of the iPhone: only via BlueZ Battery Provider (experimental), not shown yet.

### Remaining blocker to verify on hardware

The iPhone is **not paired yet**. The first milestone (incoming call, caller ID,
answer, audio, hangup) requires pairing, which needs a confirmation on the iPhone.
